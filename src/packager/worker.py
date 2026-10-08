"""Kafka event-consumer worker loop.

The packager is the TERMINAL stage of the catalog pipeline. It CONSUMES
`stube.catalog.item.transcoded` (group `packager-workers`), packages the
item the transcoder just finished, and produces NOTHING downstream.

Lifecycle contract (mirrors the analyzer + transcoder consumers):
  * Block on `consumer.poll`; exit cleanly on SIGTERM / SIGINT (the
    `stop` event set by main's signal handler).
  * Per message: parse envelope → itemId → get_item detail →
    idempotency guard on the `package` step (finished: done,
    not_applicable or skipped — nothing to do, also for a retry the
    catalog sent before the step finished) → a handover whose transcode
    is not finished is stale (sent before a re-encode reset the step; the
    run now waiting sends its own) and only acked → run the EXISTING packaging
    body (unchanged) with all its katalog HTTP writes → commit the
    offset. The offset is committed ONLY after the item is fully
    processed (or definitively failed), so a crash mid-work reprocesses
    the message; that reprocess is safe because the katalog
    (item_id, step) unique index + the step-status guard make packaging
    idempotent.
  * A malformed / unknown-item message is committed and skipped so a
    poison message can't wedge the partition.

Per-item work is fully serial inside one worker — packaging is
CPU-bound and disk-bound. To scale, add Deployment replicas; the Kafka
consumer group rebalances partitions across them.

Source-file selection: when katalog-transcoder ran ahead of us it
leaves a handoff under `{PACKAGES_ROOT}/_inbox/{itemId}/` —
`renditions.json` listing N video rungs (prepared.mkv / v1.mkv / ... or
the original for a stream-copied rung), or just `prepared.mkv` from an
older transcoder. `packager.renditions.resolve_inputs` reads it. When no
handoff exists (the transcoder marked the source not_applicable because
it was already HEVC) we fall through to the original source path.

Library v2: a worker record with a `library` block is packaged into the
title's record in the library tree instead (library.package_version):
from the record's inboxDir, else from a handoff left in
`{PACKAGES_ROOT}/_inbox/{itemId}/` before the layout switched — never
from the original while the transcode step says done and its handoff is
gone. Its packaging-complete takes the v2 payload, and the step is done
only once the catalog has taken it.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import structlog

from . import library
from .events import build_consumer, is_retry, parse_envelope, parse_item_id
from .katalog import ClaimedItem, KatalogClient
from .packager import PACKAGES_ROOT, PackageOptions, package_item, probe_source
from .renditions import ContractError, PackageInputs, resolve_inputs

log = structlog.get_logger(__name__)

# The owning step for this worker. If it's already finished on a
# redelivered event we skip the (expensive) packaging work.
PACKAGE_STEP = "package"
# The transcoder's step: done means it left a handoff.
TRANSCODE_STEP = "transcode"

# The package step's statuses that need no run: `done` (packaged), and
# `not_applicable` / `skipped` (final by the catalog's word). The catalog
# retries none of them.
FINISHED_STATUSES = frozenset({"done", "not_applicable", "skipped"})

# How long a single consumer.poll() blocks before returning None. Short
# enough that the stop event is honoured promptly on SIGTERM.
_POLL_TIMEOUT_SECONDS = 1.0


_INBOX_ROOT = PACKAGES_ROOT / "_inbox"


def _cleanup_inbox(item_id: str) -> None:
    """Drop the transcoder handoff dir after successful packaging.

    Best-effort — the transcoder will overwrite on the next run anyway,
    but freeing the disk space immediately keeps the _inbox bounded by
    the number of in-flight items, not the lifetime of the cluster.
    """
    inbox_dir = _INBOX_ROOT / item_id
    if not inbox_dir.exists():
        return
    try:
        shutil.rmtree(inbox_dir)
    except OSError as e:
        log.warning("packager.inbox.cleanup_failed", item_id=item_id, error=str(e))


# What the catalog keeps of a title's source, in the names of the
# transcoder's renditions.json source block (packaging-complete reads
# them, as it reads the v1 manifest's names).
SOURCE_KEYS = ("codec", "width", "height", "durationMs", "bitRate")


def _source_block(inputs: PackageInputs, original: str) -> dict[str, Any]:
    """The source block of the packaging-complete call: renditions.json's
    `source` (the transcoder's probe of the original), completed by a
    probe of the original for whatever it doesn't say — all of it when
    there is no handoff (a source that needed no encode), the duration and
    bit rate when the transcoder predates them. The package's own probe
    can't stand in: for an encoded v0 it reads the encode. Only the
    catalog gets this; the on-disk manifest (v2) keeps no source block."""
    raw = inputs.contract.get("source")
    # null is unknown, and so is a 0 / "" for one of the five (a size the
    # transcoder's probe didn't get); other keys (hdr: false) pass as they are.
    block = {k: v for k, v in raw.items()
             if v is not None and (v or k not in SOURCE_KEYS)} if isinstance(raw, dict) else {}
    if any(k not in block for k in SOURCE_KEYS) and os.path.exists(original):
        for key, value in probe_source(Path(original)).items():
            block.setdefault(key, value)
    return block


def _parse_settings(raw: dict[str, str]) -> tuple[list[str], bool]:
    """Extract the packager-visible bits from the settings map. Bad
    values fall back to safe defaults — a malformed setting must never
    take down the worker, since the operator may be mid-edit when we
    claim the next item."""
    csv = raw.get("packager.language_whitelist", "")
    whitelist = [
        t.strip().lower() for t in csv.split(",")
        if t.strip()
    ]
    fallback_raw = raw.get("packager.keep_original_if_single", "true").strip().lower()
    keep_original = fallback_raw in ("true", "1", "yes")
    return whitelist, keep_original


def _details(manifest: dict[str, Any], seconds: float) -> str:
    """The `done` step's details: the top rung's codec, the audio and
    subtitle counts and the run's seconds, the ladder and the 5.1 group
    where there are any. Read from a manifest or a package.json alike."""
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


def _process_one(
    item: ClaimedItem,
    client: KatalogClient,
    options: PackageOptions | None = None,
) -> None:
    """Run packaging for one item. Heartbeats the package step at start
    (in_progress) and end (done / failed). The step-status writes here
    are the STATE the Activity monitor reads — they are the source of
    truth for pipeline progress, not the (now removed) claim state.

    A record with a library block packages into the library v2 tree
    (_process_library); one whose block can't be worked from fails the
    step, and is never packaged into the package store instead."""
    if item.library_error is not None:
        log.warning("packager.item.library_refused", item_id=item.id, error=item.library_error)
        client.upsert_step(item.id, "failed", error=item.library_error[:500])
        return
    if item.library is not None:
        _process_library(item, client, options)
        return
    try:
        inputs = resolve_inputs(_INBOX_ROOT / item.id, item.path)
    except ContractError as e:
        log.warning("packager.item.bad_handoff", item_id=item.id, error=str(e))
        client.upsert_step(item.id, "failed", error=f"transcoder handoff: {e}"[:500])
        return
    effective_path = str(inputs.primary.path)
    log.info(
        "packager.item.start",
        item_id=item.id,
        title=item.title,
        type=item.type,
        path=effective_path,
        source=inputs.kind,
        video_renditions=len(inputs.video),
    )

    if not os.path.exists(effective_path):
        # Either the original source vanished between scan and claim,
        # or — if we'd picked up a prepared.mkv — the file disappeared
        # between the existence check and now. Don't keep retrying;
        # flag it so an operator can re-scan / re-run the transcoder.
        msg = f"source file missing: {effective_path}"
        log.warning("packager.item.missing_file", item_id=item.id, path=effective_path)
        client.upsert_step(item.id, "failed", error=msg)
        return

    client.upsert_step(item.id, "in_progress")

    # Fetch the language whitelist + anime fallback at claim time so
    # an operator's Settings edit takes effect on the very next item.
    # Failures here return {} (logged warning) and package_item falls
    # through to "all tracks visible" — which is the legacy behaviour
    # and never wrong, just verbose.
    language_whitelist, keep_original = _parse_settings(client.settings())

    t0 = time.monotonic()
    try:
        manifest = package_item(
            # The source as the catalog has it: v0 (inputs.primary) may be
            # the transcoder's encode of it, and the catalog's per-track
            # languages count the source's tracks.
            item.id, item.path, item_type=item.type,
            language_whitelist=language_whitelist,
            keep_original_if_single=keep_original,
            track_languages=item.track_languages,
            subtitle_files=item.subtitle_files,
            # Catalog identity passed through to the manifest so the
            # package self-describes even if the DB is later lost.
            # See ClaimedItem.tmdb_id for the movie-vs-episode rule.
            title=item.title,
            year=item.year,
            series_title=item.series_title,
            season_number=item.season_number,
            episode_number=item.episode_number,
            tmdb_id=item.tmdb_id,
            inputs=inputs,
            options=options,
        )
    except Exception as e:
        # package_item already wrote `.failed` to the package dir and
        # logged the trace; surface the message into the audit row so
        # ops can see why it failed without grepping pod logs.
        log.exception("packager.item.failed", item_id=item.id, error=str(e)[:300])
        client.upsert_step(item.id, "failed", error=str(e)[:500])
        return

    # `package_item` returns the manifest dict on success and raises on
    # failure — no second-class status field. The `.complete` sentinel
    # is also written by package_item itself before it returns.
    seconds = round(time.monotonic() - t0, 2)
    renditions = manifest.get("renditions", {}) if isinstance(manifest, dict) else {}
    video_renditions = renditions.get("video", []) if isinstance(renditions, dict) else []
    audio_renditions = renditions.get("audio", []) if isinstance(renditions, dict) else []
    subtitles = manifest.get("subtitles", []) if isinstance(manifest, dict) else []
    surround_renditions = (
        renditions.get("audioSurround", []) if isinstance(renditions, dict) else []
    )
    video_codec = (
        video_renditions[0].get("codec")
        if video_renditions and isinstance(video_renditions[0], dict)
        else "?"
    )
    details = (
        f"v={video_codec} a={len(audio_renditions)} "
        f"subs={len(subtitles)} dur_s={seconds}"
        + (f" vr={len(video_renditions)}" if len(video_renditions) > 1 else "")
        + (f" a51={len(surround_renditions)}" if surround_renditions else "")
    )
    # Mirror the manifest into the catalog DB so the Object Page Files
    # facet picks up codec/resolution/bitrate + the packaged-asset row
    # + per-track SubtitleAssets without a separate scan pass, and the
    # source asset its exact probe (the source block). Step
    # bookkeeping happens after — if the manifest ingest fails, the
    # packaging itself is still "done" (data is on disk; the operator
    # can re-trigger a Validate to repair).
    source = _source_block(inputs, item.path)
    client.packaging_complete(item.id, {**manifest, "source": source} if source else manifest)

    client.upsert_step(item.id, "done", details=details)
    # Drop the transcoder handoff (if any) only after the row is
    # marked done — keeps the files around for forensics if any of the
    # bookkeeping calls above raised.
    if inputs.kind != "original" or (_INBOX_ROOT / item.id).exists():
        _cleanup_inbox(item.id)
    log.info(
        "packager.item.done",
        item_id=item.id,
        title=item.title,
        seconds=seconds,
        video_codec=video_codec,
        audio_tracks=len(audio_renditions),
        subtitles=len(subtitles),
    )


def _process_library(
    item: ClaimedItem,
    client: KatalogClient,
    options: PackageOptions | None = None,
) -> None:
    """Package an item whose worker record carries a library block into
    its version folder (library.package_version) and hand the version to
    the catalog (contract section 2.5). The step is done only once the
    catalog has taken it, a 2xx; until then the staging folder and the
    handoff stay. A version in the record the catalog refused (a stale
    version, a broken chain) stays as it is, and the step fails."""
    lib = item.library
    assert lib is not None
    legacy_inbox = _INBOX_ROOT / item.id
    try:
        inputs, inbox = library.handoff(lib.inbox_dir, legacy_inbox, item.path)
    except ContractError as e:
        log.warning("packager.item.bad_handoff", item_id=item.id, error=str(e))
        client.upsert_step(item.id, "failed", error=f"transcoder handoff: {e}"[:500])
        return
    if inbox is None and client.get_steps(item.id).get(TRANSCODE_STEP) == "done":
        # Its handoff is gone: packaging the original instead would make a
        # package of another encode than the one the step reported.
        msg = (f"the transcode step is done, but its handoff is in neither {lib.inbox_dir} nor "
               f"{legacy_inbox}: transcode the item again")
        log.warning("packager.item.handoff_missing", item_id=item.id)
        client.upsert_step(item.id, "failed", error=msg[:500])
        return
    effective_path = str(inputs.primary.path)
    log.info("packager.item.start", item_id=item.id, title=item.title, type=item.type,
             path=effective_path, source=inputs.kind, video_renditions=len(inputs.video),
             layout="v2", version_id=lib.build.version_id)
    if not os.path.exists(effective_path):
        log.warning("packager.item.missing_file", item_id=item.id, path=effective_path)
        client.upsert_step(item.id, "failed", error=f"source file missing: {effective_path}")
        return

    client.upsert_step(item.id, "in_progress")
    language_whitelist, keep_original = _parse_settings(client.settings())
    t0 = time.monotonic()
    try:
        placed = library.package_version(
            item, inputs, options=options or PackageOptions(),
            language_whitelist=language_whitelist, keep_original_if_single=keep_original)
    except Exception as e:
        log.exception("packager.item.failed", item_id=item.id, error=str(e)[:300])
        client.upsert_step(item.id, "failed", error=str(e)[:500])
        return
    seconds = round(time.monotonic() - t0, 2)

    payload = library.version_payload(lib, placed, _source_block(inputs, item.path))
    handed = client.packaging_complete_v2(item.id, payload)
    if not handed.taken:
        # In the record, unknown to the catalog: nothing plays it. The
        # version folder stays as it is; a retry reports it again.
        client.upsert_step(item.id, "failed", error=f"packaging-complete: {handed.error}"[:500])
        return
    details = _details(placed.package, seconds)
    client.upsert_step(item.id, "done", details=details)
    library.finish(lib.build.staging_dir, inbox, Path(lib.inbox_dir))
    log.info("packager.item.done", item_id=item.id, title=item.title, seconds=seconds,
             version_id=lib.build.version_id, package_id=placed.package.get("packageId"),
             reported_again=placed.reported_again or None, details=details,
             superseded=handed.answer.get("superseded"))


def _handle_message(
    item_id: str,
    client: KatalogClient,
    options: PackageOptions | None = None,
    *,
    retry: bool = False,
) -> None:
    """Resolve, guard, and package a single item. Any error that the
    packaging body owns is already attributed to the `package` step by
    `_process_one`; this wrapper only owns the resolve + idempotency
    guard, and it never lets an exception escape (the caller commits the
    offset regardless to avoid a poison loop)."""
    # 1. Resolve the full item detail from the itemId on the event.
    item = client.get_item(item_id)
    if item is None:
        log.warning("packager.item.unknown", item_id=item_id)
        return

    # 2. Idempotency guard: on a redelivered event whose package step has
    #    already finished, skip the expensive work. The packager is
    #    TERMINAL, so there is nothing downstream to re-emit — just
    #    return and let the caller commit. A `retry` (the catalog sent
    #    the trigger again for a failed or silent step) finds its step
    #    finished when the run the reaper took for dead reported done
    #    after all: one log line, nothing else.
    steps = client.get_steps(item_id)
    status = steps.get(PACKAGE_STEP)
    if status in FINISHED_STATUSES:
        if retry:
            log.info("packager.retry.already_finished", item_id=item_id, status=status)
        else:
            log.info("packager.item.already_done", item_id=item_id, status=status)
        return

    # 3. A stale handover: the transcoder writes its step's end before it
    #    sends `transcoded`, so an event whose transcode is not finished
    #    was sent before that step was reset — a re-encode, or package
    #    now, after a pause left the event unconsumed. Packaging it would
    #    package the old handoff (or the original) and finish the package
    #    step, and the handover of the run now waiting would find it done:
    #    the new encode would never be packaged. That run sends its own.
    transcode = steps.get(TRANSCODE_STEP)
    if transcode is not None and transcode not in FINISHED_STATUSES:
        log.info("packager.item.stale_handover", item_id=item_id,
                 transcode=transcode, retry=retry)
        return

    # 4. Run the packaging body with all its katalog HTTP writes
    #    (upsert_step, packaging_complete).
    _process_one(item, client, options)


def run_worker(
    client: KatalogClient,
    brokers: str,
    group_id: str,
    consume_topic: str,
    security_protocol: str,
    error_sleep: float,
    stop: threading.Event,
    options: PackageOptions | None = None,
) -> None:
    """Blocking Kafka consumer loop. Exits when `stop` is set (SIGTERM
    handler in main). Offsets are committed manually only after an item
    is fully processed (or definitively skipped/failed)."""
    consumer = build_consumer(
        brokers=brokers,
        group_id=group_id,
        security_protocol=security_protocol,
    )
    consumer.subscribe([consume_topic])
    log.info(
        "packager.consumer.start",
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
                log.exception("packager.consumer.poll_failed", error=str(e)[:300])
                stop.wait(error_sleep)
                continue

            if msg is None:
                continue
            if msg.error():
                # Rebalance notices etc. surface as errors; log and skip
                # (no offset to commit — nothing was delivered).
                log.warning("packager.consumer.msg_error", error=str(msg.error()))
                continue

            item_id = parse_item_id(msg.value())
            if item_id is None:
                # Malformed / missing itemId: commit + skip so a poison
                # message can't wedge the partition.
                consumer.commit(message=msg)
                continue

            try:
                _handle_message(item_id, client, options,
                                retry=is_retry(parse_envelope(msg.value())))
            except Exception as e:
                # _process_one already attributed any packaging error to
                # the package step; anything that escapes is a bug in the
                # resolve/guard path. Mark failed + commit to avoid a
                # poison loop (reprocessing wouldn't help).
                log.exception(
                    "packager.process_unexpected",
                    item_id=item_id,
                    error=str(e)[:300],
                )
                try:
                    client.fail(item_id, f"worker bug: {e}"[:500])
                except Exception:
                    log.exception("packager.fail_report_failed", item_id=item_id)

            # Commit only AFTER the item is fully processed (or
            # definitively failed). A crash before this line reprocesses
            # the message on the next poll — safe by the katalog
            # (item_id, step) unique index + the done-step guard above.
            consumer.commit(message=msg)
    finally:
        consumer.close()
        log.info("packager.consumer.stopped")
