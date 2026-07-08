"""Kafka event-consumer worker loop.

The packager is the TERMINAL stage of the catalog pipeline. It CONSUMES
`stube.catalog.item.transcoded` (group `packager-workers`), packages the
item the transcoder just finished, and produces NOTHING downstream.

Lifecycle contract (mirrors the analyzer + transcoder consumers):
  * Block on `consumer.poll`; exit cleanly on SIGTERM / SIGINT (the
    `stop` event set by main's signal handler).
  * Per message: parse envelope → itemId → get_item detail →
    idempotency guard on the `package` step → run the EXISTING packaging
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
leaves a handoff MKV at `{PACKAGES_ROOT}/_inbox/{itemId}/prepared.mkv`.
That file is HEVC (NVENC) with every subtitle track from the source
intact — the only thing the packager needs to know is to read *that*
instead of `item.path`. When no prepared file exists (transcoder
marked the source not_applicable because it was already HEVC) we fall
through to the original source path.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import structlog

from .events import build_consumer, parse_item_id
from .katalog import ClaimedItem, KatalogClient
from .packager import PACKAGES_ROOT, package_item

log = structlog.get_logger(__name__)

# The owning step for this worker. If it's already `done` on a
# redelivered event we skip the (expensive) packaging work.
PACKAGE_STEP = "package"

# How long a single consumer.poll() blocks before returning None. Short
# enough that the stop event is honoured promptly on SIGTERM.
_POLL_TIMEOUT_SECONDS = 1.0


_INBOX_ROOT = PACKAGES_ROOT / "_inbox"


def _prepared_source(item_id: str) -> Path | None:
    """Return the transcoder's prepared.mkv for this item if it exists.

    The transcoder writes atomically (`prepared.mkv.partial` → rename)
    so the presence of `prepared.mkv` itself implies a complete file.
    """
    candidate = _INBOX_ROOT / item_id / "prepared.mkv"
    return candidate if candidate.exists() else None


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


def _process_one(item: ClaimedItem, client: KatalogClient) -> None:
    """Run packaging for one item. Heartbeats the package step at start
    (in_progress) and end (done / failed). The step-status writes here
    are the STATE the Activity monitor reads — they are the source of
    truth for pipeline progress, not the (now removed) claim state."""
    prepared = _prepared_source(item.id)
    effective_path = str(prepared) if prepared is not None else item.path
    log.info(
        "packager.item.start",
        item_id=item.id,
        title=item.title,
        type=item.type,
        path=effective_path,
        source="transcoder_prepared" if prepared is not None else "original",
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
            item.id, effective_path, item_type=item.type,
            language_whitelist=language_whitelist,
            keep_original_if_single=keep_original,
            # Catalog identity passed through to the manifest so the
            # package self-describes even if the DB is later lost.
            # See ClaimedItem.tmdb_id for the movie-vs-episode rule.
            title=item.title,
            year=item.year,
            series_title=item.series_title,
            season_number=item.season_number,
            episode_number=item.episode_number,
            tmdb_id=item.tmdb_id,
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
    video_codec = (
        video_renditions[0].get("codec")
        if video_renditions and isinstance(video_renditions[0], dict)
        else "?"
    )
    details = (
        f"v={video_codec} a={len(audio_renditions)} "
        f"subs={len(subtitles)} dur_s={seconds}"
    )
    # Mirror the manifest into the catalog DB so the Object Page Files
    # facet picks up codec/resolution/bitrate + the packaged-asset row
    # + per-track SubtitleAssets without a separate scan pass. Step
    # bookkeeping happens after — if the manifest ingest fails, the
    # packaging itself is still "done" (data is on disk; the operator
    # can re-trigger a Validate to repair).
    client.packaging_complete(item.id, manifest)

    client.upsert_step(item.id, "done", details=details)
    # Drop the transcoder handoff (if any) only after the row is
    # marked done — keeps the file around for forensics if any of the
    # bookkeeping calls above raised.
    if prepared is not None:
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


def _handle_message(item_id: str, client: KatalogClient) -> None:
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

    # 2. Idempotency guard: on a redelivered event whose package step is
    #    already `done`, skip the expensive work. The packager is
    #    TERMINAL, so there is nothing downstream to re-emit — just
    #    return and let the caller commit.
    steps = client.get_steps(item_id)
    if steps.get(PACKAGE_STEP) == "done":
        log.info("packager.item.already_done", item_id=item_id)
        return

    # 3. Run the existing packaging body (unchanged) with all its
    #    katalog HTTP writes (upsert_step, packaging_complete).
    _process_one(item, client)


def run_worker(
    client: KatalogClient,
    brokers: str,
    group_id: str,
    consume_topic: str,
    security_protocol: str,
    error_sleep: float,
    stop: threading.Event,
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
                _handle_message(item_id, client)
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
