"""The extras mode: the trailers, teasers, featurettes and other bonus
material of a title.

The catalog keeps an extra apart from its title, as a row of its own
keyed by its extraId, and the packager packages it on its own, in a
package category of its own: `extras/<aa>/<extraId>/` under
PACKAGES_ROOT, never inside the title's folder. A title packaged again
retires everything in its folder that its new manifest doesn't name, and
the playback service takes every folder with a `.complete` in a title
category for a title. The chain is short, with no analyzer and no
trickplay:

    <prefix>catalog.extra.queued       catalog -> transcoder
    <prefix>catalog.extra.transcoded   transcoder -> packager (this loop)

This consumer runs on a thread of its own, in a consumer group of its
own (`packager-extras`): the item loop takes one message at a time, so
a long film's run would otherwise hold every trailer up behind it. The
price is up to two runs at once per pod, one item's and one extra's.

Per message, as the item loop does it (worker.run_worker):
  1. The extraId from the envelope, a lower-case UUID. Malformed -> warn,
     commit, skip.
  2. `GET /api/analyze/extras/{id}`. Unknown or removed (404), a record
     that says it was removed, or an extra in state `missing` (its file
     is gone) -> log, commit, skip: nothing written.
  3. `ready` is finished: nothing runs. The transcoder sends `transcoded`
     again for an extra past its transcode whenever a trigger reaches it
     again, so this is the duplicate of a run that finished; a retry the
     catalog sent for it is only acked. Every other state packages, as
     an unfinished item does: `packaging` too, a run that died (an
     extra's events share one partition, so no other run is at work).
  4. The package step goes `in_progress`, and the transcoder's handoff
     in `_inbox/extra-<extraId>/` (the unchanged renditions.json
     contract; a `"file": null` rung, or no handoff at all, is the
     extra's source as it is) is packaged with package_item: type
     `extra`, no trickplay, the manifest naming its title (`parentId`)
     and its kind (`extraKind`). The packager's options and language
     settings are an item's.
  5. `POST /api/extras/{id}/packaging-complete` with the manifest and the
     source block. Only once the catalog has taken it is the step `done`
     and the handoff removed. When it hasn't, the step fails, unlike an
     item's, which is done regardless: only the catalog's word makes an
     extra playable, so its retry runs the chain again.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import structlog

from .events import build_consumer, is_retry, parse_envelope, parse_extra_id
from .katalog import ClaimedExtra, KatalogClient
from .packager import PACKAGES_ROOT, PackageOptions, iso639_2, package_item
from .renditions import ContractError, resolve_inputs
from .worker import _parse_settings, _source_block

log = structlog.get_logger(__name__)

# The type an extra's package is of: its manifest's `type`, and its
# package category `extras` (packager._CATEGORY_BY_TYPE).
EXTRA_TYPE = "extra"

# The state of an extra whose package the catalog has taken. A
# `transcoded` event for it is a duplicate of a run that finished.
FINISHED_STATES = frozenset({"ready"})

# The scanner's state for an extra whose file is gone; the catalog
# queues the extra again if the file comes back.
MISSING_STATE = "missing"

# How long a single consumer.poll() blocks before returning None, as in
# the item loop: the stop event is honoured promptly on SIGTERM.
_POLL_TIMEOUT_SECONDS = 1.0

_INBOX_ROOT = PACKAGES_ROOT / "_inbox"


def extra_inbox_dir(extra_id: str) -> Path:
    """An extra's handoff from the transcoder, beside the items'
    `_inbox/<itemId>/`: `_inbox/extra-<extraId>/`."""
    return _INBOX_ROOT / f"extra-{extra_id}"


def _cleanup_inbox(extra_id: str) -> None:
    """Drop the extra's handoff once its package is in the catalog.
    Best-effort, as for an item: the transcoder clears it before its next
    run anyway."""
    inbox = extra_inbox_dir(extra_id)
    if not inbox.exists():
        return
    try:
        shutil.rmtree(inbox)
    except OSError as e:
        log.warning("packager.extra.inbox.cleanup_failed", extra_id=extra_id, error=str(e))


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _details(manifest: dict[str, Any], seconds: float) -> str:
    """The `done` step's details, as an item's read."""
    renditions = manifest.get("renditions") or {}
    video = renditions.get("video") or []
    surround = renditions.get("audioSurround") or []
    codec = video[0].get("codec") if video else "?"
    return (
        f"v={codec} a={len(renditions.get('audio') or [])} "
        f"subs={len(manifest.get('subtitles') or [])} dur_s={seconds}"
        + (f" vr={len(video)}" if len(video) > 1 else "")
        + (f" a51={len(surround)}" if surround else "")
    )


def _process_extra(
    extra: ClaimedExtra,
    envelope: dict[str, Any],
    client: KatalogClient,
    options: PackageOptions | None = None,
) -> None:
    """Package one extra into extras/<aa>/<extraId>/ and hand its package
    to the catalog. Every outcome is written to the extra's package step:
    in_progress at the start, then done or failed."""
    # The record names the title and the kind; the event's are the fallback.
    parent_id = extra.parent_id or _text(envelope.get("parentId"))
    kind = extra.kind or _text(envelope.get("kind"))
    if not parent_id:
        # The playback service serves an extra's package under its title
        # only: one that names none would never play.
        log.warning("packager.extra.no_parent", extra_id=extra.id)
        client.upsert_extra_step(extra.id, "failed", error="the extra's record has no parentId")
        return
    if not extra.path:
        log.warning("packager.extra.no_path", extra_id=extra.id)
        client.upsert_extra_step(extra.id, "failed", error="the extra's record has no path")
        return

    inbox = extra_inbox_dir(extra.id)
    try:
        inputs = resolve_inputs(inbox, extra.path)
    except ContractError as e:
        log.warning("packager.extra.bad_handoff", extra_id=extra.id, error=str(e))
        client.upsert_extra_step(extra.id, "failed", error=f"transcoder handoff: {e}"[:500])
        return
    effective_path = str(inputs.primary.path)
    log.info(
        "packager.extra.start",
        extra_id=extra.id,
        parent_id=parent_id,
        parent_title=extra.parent_title or None,
        kind=kind,
        title=extra.title,
        path=effective_path,
        source=inputs.kind,
        video_renditions=len(inputs.video),
    )
    if not os.path.exists(effective_path):
        # The extra's file, or a rung the transcoder wrote, is gone: flag
        # it rather than retry blindly; the scanner marks a gone file
        # missing.
        msg = f"source file missing: {effective_path}"
        log.warning("packager.extra.missing_file", extra_id=extra.id, path=effective_path)
        client.upsert_extra_step(extra.id, "failed", error=msg)
        return

    client.upsert_extra_step(extra.id, "in_progress")
    # The language settings at claim time, as for an item: the DEFAULT=YES
    # pick and the visibility of a trailer's tracks follow the operator's.
    language_whitelist, keep_original = _parse_settings(client.settings())
    # What the extra speaks, when the catalog names it, names its first
    # audio track over the file's own tag, as a title's track languages
    # do: a trailer is one picture with one sound, and the file's tag is
    # often und, or wrong (one registered as zxx has no dialogue).
    language = iso639_2(extra.language)
    track_languages = [{"kind": "audio", "ordinal": 0, "language": language}] if language else None

    t0 = time.monotonic()
    try:
        manifest = package_item(
            extra.id, extra.path, item_type=EXTRA_TYPE,
            language_whitelist=language_whitelist,
            keep_original_if_single=keep_original,
            title=extra.title,
            inputs=inputs,
            options=options,
            track_languages=track_languages,
            trickplay=False,
            manifest_extra={"parentId": parent_id, "extraKind": kind},
        )
    except Exception as e:
        # package_item wrote `.failed` into the extra's folder and logged
        # the trace; the live package, if any, is as it was.
        log.exception("packager.extra.failed", extra_id=extra.id, error=str(e)[:300])
        client.upsert_extra_step(extra.id, "failed", error=str(e)[:500])
        return
    seconds = round(time.monotonic() - t0, 2)

    source = _source_block(inputs, extra.path)
    answer = client.extra_packaging_complete(
        extra.id, {**manifest, "source": source} if source else manifest)
    if answer is None:
        # The package is on disk, swapped in, but the catalog didn't take
        # it, and only the catalog's word makes an extra playable: fail
        # the step, so its retry runs the chain again. The handoff stays,
        # as after any failure.
        client.upsert_extra_step(
            extra.id, "failed", error="packaging-complete: the catalog did not take the package")
        return
    details = _details(manifest, seconds)
    client.upsert_extra_step(extra.id, "done", details=details)
    _cleanup_inbox(extra.id)
    log.info(
        "packager.extra.done",
        extra_id=extra.id,
        parent_id=parent_id,
        kind=kind,
        details=details,
        packaged=answer.get("packaged"),
    )


def _handle_extra(
    extra_id: str,
    envelope: dict[str, Any],
    client: KatalogClient,
    options: PackageOptions | None = None,
) -> None:
    """Resolve, guard and package one extra. The packaging body attributes
    its own errors to the package step; this wrapper owns the resolve and
    the guards. The caller commits."""
    retry = is_retry(envelope)
    extra = client.get_extra(extra_id)
    if extra is None:
        log.info("packager.extra.unresolved", extra_id=extra_id, retry=retry)
        return
    if extra.removed:
        log.info("packager.extra.removed_skip", extra_id=extra_id)
        return
    if extra.state == MISSING_STATE:
        log.info("packager.extra.missing_skip", extra_id=extra_id, path=extra.path)
        return
    if extra.state in FINISHED_STATES:
        if retry:
            # The catalog took a slow run for dead and sent the trigger
            # again; the run finished since.
            log.info("packager.extra.retry.already_finished", extra_id=extra_id,
                     state=extra.state)
        else:
            log.info("packager.extra.already_done", extra_id=extra_id, state=extra.state)
        return
    _process_extra(extra, envelope, client, options)


def run_extras_worker(
    client: KatalogClient,
    brokers: str,
    group_id: str,
    consume_topic: str,
    security_protocol: str,
    error_sleep: float,
    stop: threading.Event,
    options: PackageOptions | None = None,
) -> None:
    """Blocking consume loop for the extras, the twin of the item loop
    (worker.run_worker): one message at a time, the offset committed only
    once the extra is fully processed (or definitively skipped or
    failed), so a crash mid-run packages it again. Exits when `stop` is
    set."""
    consumer = build_consumer(
        brokers=brokers,
        group_id=group_id,
        security_protocol=security_protocol,
    )
    consumer.subscribe([consume_topic])
    log.info(
        "packager.extra.consumer.start",
        brokers=brokers,
        group_id=group_id,
        topic=consume_topic,
        security_protocol=security_protocol,
    )
    try:
        while not stop.is_set():
            try:
                msg = consumer.poll(_POLL_TIMEOUT_SECONDS)
            except Exception as e:
                # Transient broker / client error — back off, then retry.
                log.exception("packager.extra.consumer.poll_failed", error=str(e)[:300])
                stop.wait(error_sleep)
                continue

            if msg is None:
                continue
            if msg.error():
                log.warning("packager.extra.consumer.msg_error", error=str(msg.error()))
                continue

            raw = msg.value()
            extra_id = parse_extra_id(raw)
            if extra_id is None:
                # Malformed / no extraId: commit + skip so a poison message
                # can't wedge the partition.
                log.warning("packager.extra.malformed", value=str(raw)[:200])
                consumer.commit(message=msg)
                continue

            try:
                _handle_extra(extra_id, parse_envelope(raw), client, options)
            except Exception as e:
                # A catalog that would not answer the record, or a bug in
                # this loop: the packaging body reports its own errors.
                # Fail the step + commit to avoid a poison loop.
                log.exception("packager.extra.process_unexpected", extra_id=extra_id,
                              error=str(e)[:300])
                try:
                    client.upsert_extra_step(extra_id, "failed", error=f"worker bug: {e}"[:500])
                except Exception:
                    log.exception("packager.extra.fail_report_failed", extra_id=extra_id)

            consumer.commit(message=msg)
    finally:
        consumer.close()
        log.info("packager.extra.consumer.stopped")
