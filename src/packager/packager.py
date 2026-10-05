"""Per-item CMAF packager.

Takes the transcoder's handoff (one or more video renditions, see
`packager.renditions`) or the original source, runs shaka-packager to
emit a streaming-friendly CMAF/HLS tree under
/var/lib/katalog/packages/{category}/{shard}/{itemId}/, and writes the
manifest.json that chino-stream reads when deciding whether to serve a
pre-packaged item or fall through to on-demand transcode.

Design rules:
* Video is passthrough. We never re-encode here. Every rendition must be
  hevc/h264 (the transcoder produced or chose them); anything else fails
  the package job.
* N video renditions -> one master with one variant per rendition and
  audio group, plus one I-frame playlist per rendition. The master is
  assembled by `packager.hls` from shaka's media playlists.
* Audio: every source track becomes an AAC-LC 48 kHz stereo rendition
  (group "audio" — what every browser decodes). A visible source track
  with >= 6 channels additionally gets a 5.1 E-AC-3 (or AC-3) rendition,
  first per language, in group "audio-surround". Exactly one rendition
  per group is DEFAULT=YES: the preferred-language track; in the 5.1
  group its companion, else the 5.1 rendition of its language, else that
  of the first preferred language with one, else the first.
* Subtitles are extracted to sidecar files (WebVTT for text, native
  bitmap formats for PGS/VobSub/DVB) exactly as before. WebVTT tracks are
  additionally packaged as HLS subtitle renditions (hls/sN/); the master
  references them (TYPE=SUBTITLES, FORCED=YES where flagged) only when
  HLS_SUBTITLES is on, so clients that draw their own sidecar subtitles
  aren't surprised by in-manifest ones.
* All state is on disk under the per-item output directory, which is
  never moved or created again. `.complete` says the package in it is
  whole and live. A run builds the next one in `.next/` (its sentinel
  `.next/.packaging`) and swaps it in only once it is complete, so
  readers get the old package or the new one, never a half-written one,
  and a run that fails (`.failed`) leaves the live package as it was.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from . import hls
from .renditions import PackageInputs, VideoInput

log = structlog.get_logger("packager")

# Where katalog-stream looks for packaged items. Must match the
# mountPath in k8s/{analyzer,stream}-deployment.yaml.
PACKAGES_ROOT = Path("/var/lib/katalog/packages")

# Per-category sharding under PACKAGES_ROOT keeps any single directory
# under ~50 entries even at tens of thousands of items per category —
# filesystem directory ops (readdir on NFS, especially) get visibly
# slow once a directory holds thousands of entries. Layout:
#   /var/lib/katalog/packages/{category}/{shard}/{itemId}/...
# where category is movies/shows/music (derived from katalog's item
# type) and shard is the first two hex chars of the item uuid. The
# stream service probes categories on read to find the package — it
# only knows the item id, not the type.
_CATEGORY_BY_TYPE = {
    "movie": "movies",
    "episode": "shows",
    "series": "shows",
    "season": "shows",
    "album": "music",
    "track": "music",
    "song": "music",
}


def _item_root(item_id: str, item_type: str | None) -> Path:
    """Return the on-disk package root for one item."""
    category = _CATEGORY_BY_TYPE.get((item_type or "").lower(), "other")
    shard = (item_id[:2] or "00").lower()
    return PACKAGES_ROOT / category / shard / item_id


def _find_existing_root(item_id: str) -> Path | None:
    """Probe every category for an existing package directory for the
    given item id. Returns the first hit, or None when nothing is on
    disk yet. Used by package_status when the caller doesn't know
    item.type (chino-api admin GET hits this path)."""
    shard = (item_id[:2] or "00").lower()
    categories = [*set(_CATEGORY_BY_TYPE.values()), "other"]
    for category in categories:
        path = PACKAGES_ROOT / category / shard / item_id
        if path.exists():
            return path
    return None


# A run builds the next package in this folder inside the item folder, in
# the item folder's own layout (hls/, subs/, trickplay/, manifest.json),
# and moves it into place only once it is complete (_swap_in). Its
# sentinel, .packaging, is in there too.
STAGING_DIR = ".next"
SENTINEL = ".packaging"
MANIFEST_FILE = "manifest.json"
# A live entry the swap replaces is renamed <name>.old-<UTC stamp> beside
# it, in the item folder, and removed once the grace period is over.
_OLD = ".old-"
_STAMP = "%Y%m%dT%H%M%S.%fZ"
_REPLACED = re.compile(r"^(?P<name>.+)\.old-(?P<stamp>\d{8}T\d{6}\.\d{6}Z)$")
# How long a replaced package stays by default: well past a segment read
# under NFS load and an NFS client's cache of the folder it was in
# (acdirmax, 60 s by default), so a request that started on the old
# package ends on it.
OLD_PACKAGE_GRACE_SECONDS = 600.0
# A staging folder whose run started longer ago than this is a dead run's:
# no run lasts that long, as one that did would have lost its Kafka
# partition (max.poll.interval.ms, 24 h). A younger one may be another
# replica's live run.
STALE_STAGING_SECONDS = 24 * 3600.0

# Segment length in seconds. Same value the legacy on-demand pipeline
# used; long enough to amortize HTTP overhead, short enough for snappy
# seeks. shaka-packager cuts at the first keyframe of each window, so
# the transcoder forces keyframes at this interval (and tells us the
# value it used in renditions.json, which wins over this default).
SEGMENT_SECONDS = 6

# Manifest schema version. Bump in lockstep with
# stream/internal/pkgmanifest/manifest.go's CurrentVersion when the
# on-disk shape changes incompatibly.
#
# v2 (this version): drops the `source` block entirely (source files
# may not be retained long-term), lifts the catalog identity onto the
# manifest itself (title / type / year / tmdbId / for episodes
# seriesTitle + seasonNumber + episodeNumber + episodeCode). The
# package directory then self-describes the item even if the catalog
# DB is lost. durationMs moves to the top level since the stream
# service needs it for the HLS playlist. Additive since: several
# renditions.video entries, renditions.audioSurround, subtitles[].hls
# and the hls block — readers that don't know them ignore them.
#
# v1: had source.{path,mtime,size,container,videoCodec,resolution,
# frameRate,bitrateBps}. The stream side still reads v1 packages
# unchanged (Source struct in manifest.go is optional now).
MANIFEST_VERSION = 2

# HLS group ids. "audio" is what shaka has always written for the stereo
# group; clients key on nothing else.
AUDIO_GROUP = "audio"
SURROUND_GROUP = "audio-surround"
SUBTITLE_GROUP = "subs"

STEREO_BITRATE = "192k"
_STEREO_FORMAT = "aformat=sample_rates=48000:channel_layouts=stereo"
_SURROUND_FORMAT = "aformat=sample_rates=48000:channel_layouts=5.1(side)|5.1"
_SURROUND_CODECS = {"eac3": "ec-3", "ac3": "ac-3"}


@dataclass(frozen=True)
class PackageOptions:
    """Packager-wide knobs (env, see config.py)."""
    segment_seconds: int = SEGMENT_SECONDS
    # "eac3" | "ac3" | "off" — the 5.1 companion of >= 6-channel tracks.
    surround_codec: str = "eac3"
    surround_bitrate: str = "448k"
    # Reference the WebVTT renditions from the master (TYPE=SUBTITLES).
    hls_subtitles: bool = False
    # Language preference for DEFAULT=YES; empty = the whitelist order.
    preferred_languages: tuple[str, ...] = field(default_factory=tuple)
    # Seconds a package replaced by a new one stays on disk.
    old_package_grace_seconds: float = OLD_PACKAGE_GRACE_SECONDS


def _episode_code(season: int | None, episode: int | None) -> str | None:
    """Format season/episode as S01E03, S101E233, … — at least two
    digits per field, more when the number is wider. Returns None if
    either component is missing (we'd be writing a malformed code)."""
    if season is None or episode is None:
        return None
    return f"S{season:02d}E{episode:02d}"

# Trickplay (scrub-preview thumbnails) parameters. The defaults match
# the common convention (and shaka's own trickplay docs): one ~16:9
# frame every 10 s, tiled 10x10 per sprite-sheet JPG. That's ~30 KB
# per sprite and 1 sprite per ~16.7 min of source, so a 90 min movie
# produces ~6 sprites + a small VTT. The player loads the VTT once
# and pulls one tiny sprite as the cursor enters each 1000 s window.
TRICKPLAY_INTERVAL_SEC = 10
TRICKPLAY_THUMB_WIDTH = 320
TRICKPLAY_THUMB_HEIGHT = 180
TRICKPLAY_GRID_COLS = 10
TRICKPLAY_GRID_ROWS = 10


class PackageError(RuntimeError):
    """Raised when a packaging job fails. The message is written to
    .failed so operators can see what went wrong."""


def _run_ffmpeg_capturing(label: str, args: list[str]) -> None:
    """Run an ffmpeg invocation capturing stderr so failures surface
    the actual diagnostic instead of the bare exit code.

    subprocess.run(check=True) raises CalledProcessError on non-zero
    exit but its str(e) is a one-line `Command '[...]' returned
    non-zero exit status 1.` — useless when triaging failures on the
    bulk-packaging queue. Capturing stderr + raising PackageError
    with a 1500-char snippet keeps the .failed sentinel actionable.
    """
    result = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise PackageError(
            f"ffmpeg {label} exited {result.returncode}: {stderr[-1500:] or '(no stderr)'}"
        )


@dataclass
class _Probe:
    container: str
    duration_ms: int
    video: dict[str, Any]
    audio: list[dict[str, Any]]
    subtitles: list[dict[str, Any]]
    # Absolute stream index of `video` (the first non-cover-art video).
    video_index: int | None = None
    # The container's overall bit rate (bit/s), None when ffprobe has none.
    bit_rate: int | None = None


def package_item(
    item_id: str,
    source_path: str,
    item_type: str | None = None,
    *,
    language_whitelist: list[str] | None = None,
    keep_original_if_single: bool = True,
    title: str | None = None,
    year: int | None = None,
    series_title: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    tmdb_id: str | None = None,
    inputs: PackageInputs | None = None,
    options: PackageOptions | None = None,
) -> dict[str, Any]:
    """Package one item synchronously. Returns the written manifest.

    The new package is built in the item folder's staging folder and
    swapped in only once it is complete; until then an existing package
    stays live, untouched, and a run that fails leaves it so. A package
    it replaces is removed after options.old_package_grace_seconds. Safe
    to retry: what an earlier run left in the staging folder is cleared
    first. Concurrent calls for the same item_id are NOT serialised here
    — the caller owns the queue.

    item_type is katalog's classification (movie/episode/album/…) and
    decides which top-level category directory the package lands under.

    `inputs` is the resolved transcoder handoff (packager.renditions);
    None packages `source_path` as the single rendition, as before.
    `inputs.primary` (v0) carries the audio and subtitle tracks.

    language_whitelist is a list of lowercased ISO 639-1/2 codes
    (`en`, `de`, `zh`, …). Tracks (audio + subtitle) whose language
    tag is in the list get `visible: True` in the manifest; the rest
    get `visible: False`. Every audio track is still encoded into the
    HLS tree — the player consults the visibility flag when building its
    language menu, but a power-user / future toggle can opt back in
    without re-packaging. An empty/None list marks every track
    visible. When the whitelist would mark nothing visible AND
    `keep_original_if_single` is True AND the source has exactly one
    distinct language tag, every track is marked visible — covers
    the anime / foreign-only case where en/de/zh wouldn't otherwise
    match. The whitelist order is also the language preference for the
    DEFAULT=YES audio track unless options.preferred_languages is set.
    Loaded from the Settings entity by the worker on each item so an
    operator edit takes effect on the next item."""
    options = options or PackageOptions()
    if inputs is None:
        inputs = PackageInputs(video=[VideoInput("v0", Path(source_path))], kind="original")
    src = inputs.primary.path
    if not src.exists():
        raise PackageError(f"source not found: {src}")
    segment_seconds = inputs.segment_seconds or options.segment_seconds

    out_root = _item_root(item_id, item_type)
    # Everything below writes into the staging folder; the live package,
    # if there is one, plays on until _swap_in.
    stage = out_root / STAGING_DIR

    try:
        _open_staging(out_root, options.old_package_grace_seconds)
        probe = _ffprobe(src)
        if probe.video.get("codec_name") not in ("hevc", "h264"):
            raise PackageError(
                f"video codec {probe.video.get('codec_name')!r} not supported "
                "(passthrough only; HEVC and H.264 are the allowed input codecs)"
            )

        # Resolve client-visibility windows for audio + subtitle
        # tracks from the language whitelist. Tracks ALWAYS get
        # packaged — `visible` is just a hint for the player UI.
        audio_visible = _visible_indices(
            probe.audio, language_whitelist,
            keep_original_if_single=keep_original_if_single,
        )
        sub_visible = _visible_indices(
            probe.subtitles, language_whitelist,
            keep_original_if_single=keep_original_if_single,
        )
        preferred = list(options.preferred_languages) or list(language_whitelist or [])
        default_audio = _pick_default_audio(probe.audio, audio_visible, preferred)
        surround = _surround_plan(probe.audio, audio_visible, options)
        default_surround = _pick_default_surround(surround, probe.audio, default_audio, preferred)
        log.info(
            "packager.lang_filter",
            whitelist=language_whitelist or None,
            preferred=preferred or None,
            audio_total=len(probe.audio),
            audio_visible=len(audio_visible),
            default_audio=default_audio,
            surround=[s.source_index for s in surround] or None,
            default_surround=default_surround,
            sub_total=len(probe.subtitles),
            sub_visible=len(sub_visible),
            video_renditions=len(inputs.video),
            inputs=inputs.kind,
        )

        # Stage everything through ffmpeg: shaka-packager doesn't encode
        # audio — it only packages — and doesn't read MKV for HEVC/H.264.
        # Subtitles are extracted next to the package.
        with tempfile.TemporaryDirectory(prefix=f"pkg-{item_id}-") as tmp:
            tmpdir = Path(tmp)
            packaging_source, audio_meta, surround_meta = _prepare_source(
                src, probe, tmpdir,
                audio_visible_indices=audio_visible,
                default_index=default_audio,
                surround=surround,
                surround_default=default_surround,
                timeline=inputs.primary.timeline,
                ts_offset=inputs.timestamp_offset,
            )
            videos = [_StagedVideo(inputs.primary, packaging_source, probe)]
            for rung in inputs.video[1:]:
                staged = _remux_video(rung, tmpdir, inputs.timestamp_offset)
                if staged is not None:
                    videos.append(staged)
            subtitle_meta = _extract_subtitles(
                src, probe, stage / "subs",
                visible_indices=sub_visible,
            )
            video_meta, audio_meta, surround_meta = _run_shaka_packager(
                packaging_source, videos, audio_meta, surround_meta, stage,
                segment_seconds=segment_seconds,
                hls_subtitles=options.hls_subtitles,
                subtitle_meta=subtitle_meta,
            )

        # Trickplay runs against the original source — only 1 frame
        # per TRICKPLAY_INTERVAL_SEC, so HEVC decode cost is small
        # (~30 s on a 90 min movie) and we don't need the
        # transmuxed intermediate to still exist.
        trickplay_meta = _generate_trickplay(src, probe, stage / "trickplay")

        # v2 manifest: self-describing catalog metadata at the top
        # level, no `source` block. If the catalog DB is ever lost,
        # the on-disk package alone tells you what the item is
        # (title, TMDB ID, episode coordinates) and how to reconstruct
        # the DB row from TMDB.
        manifest: dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "itemId": item_id,
            "type": item_type or "",
            "title": title or "",
            "year": year,
            "tmdbId": tmdb_id,
            "durationMs": probe.duration_ms,
            "packagedAt": datetime.now(UTC).isoformat(),
            "packager": _packager_version(),
            "renditions": {
                "video": video_meta,
                # Stereo AAC, one per source track: what /info lists.
                "audio": audio_meta,
                # 5.1 companions (group audio-surround); a separate key so
                # readers that count or list `audio` see the same tracks
                # as before.
                "audioSurround": surround_meta,
            },
            "subtitles": subtitle_meta,
            "hls": {
                "master": "hls/master.m3u8",
                "segmentSeconds": segment_seconds,
                "audioGroups": ([AUDIO_GROUP] if audio_meta else [])
                + ([SURROUND_GROUP] if surround_meta else []),
                "subtitleGroup": SUBTITLE_GROUP if (
                    options.hls_subtitles and any(s.get("hls") for s in subtitle_meta)
                ) else None,
            },
        }
        if item_type == "episode":
            # Episodes need their own coordinates + the parent series
            # title so the package self-describes as "Ghosts S01E03
            # Spies" with no DB lookup needed.
            manifest["seriesTitle"] = series_title or ""
            manifest["seasonNumber"] = season_number
            manifest["episodeNumber"] = episode_number
            ec = _episode_code(season_number, episode_number)
            if ec is not None:
                manifest["episodeCode"] = ec
        if trickplay_meta is not None:
            manifest["trickplay"] = trickplay_meta
        _write_atomic(stage / MANIFEST_FILE, json.dumps(manifest, indent=2).encode())

        # Only a whole package replaces the live one. The live one is kept
        # beside it for the grace period, for the requests that started on
        # it, then removed.
        _verify_staged(stage)
        replaced = _swap_in(out_root, stage)
        log.info("packager.complete", item_id=item_id, dir=str(out_root),
                 replaced=[p.name for p in replaced] or None)
        _remove_after(replaced, options.old_package_grace_seconds)
        return manifest

    except Exception as e:
        # The live package is as it was (a swap that fails moves it back);
        # only this run's staging folder goes.
        shutil.rmtree(stage, ignore_errors=True)
        _write_failed(out_root, e)
        log.exception("packager.failed", item_id=item_id, error=str(e))
        raise


def _open_staging(out_root: Path, grace_seconds: float) -> Path:
    """Create the run's staging folder, empty but for its sentinel.

    The item folder itself is never moved, emptied or created again (NFS
    bind mount lives here): chino-stream caches its path for the life of
    a pod and stats its .complete, other pods' NFS clients hold its handle
    (a folder created again is another inode, and their next request on
    the old handle fails), and chino-stream lists the shard folders,
    taking every folder in them with a .complete for an item, so nothing
    may go beside it. The next package is built inside it instead.

    What an earlier run left in it goes first: its staging folder (a run
    that crashed; an item packages on one replica at a time, as its
    events share a Kafka partition) and the packages it replaced whose
    grace period is over."""
    out_root.mkdir(parents=True, exist_ok=True)
    _remove_replaced(out_root, grace_seconds)
    stage = out_root / STAGING_DIR
    if os.path.lexists(stage):
        shutil.rmtree(stage)
    stage.mkdir()
    (stage / SENTINEL).write_text(json.dumps({
        "started_at": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "host": socket.gethostname(),
    }))
    return stage


def _verify_staged(stage: Path) -> None:
    """Refuse a staged package that isn't whole: every playlist the master
    or the manifest names is there and ends (#EXT-X-ENDLIST), and every
    init section, segment, sidecar and trickplay sprite they reference is
    there, inside the package and, but for a sidecar (a track without a
    cue extracts to an empty file), not empty. Raises PackageError."""
    manifest = json.loads((stage / MANIFEST_FILE).read_text())
    listings: dict[str, dict[str, int]] = {}
    missing: dict[str, None] = {}  # an ordered set: I-frame playlists name the segments again

    def size(rel: str) -> int | None:
        # One listing per folder: a film has thousands of segments, and
        # a folder listing is one NFS round trip where a stat each is not.
        folder, name = posixpath.split(rel)
        if folder not in listings:
            try:
                with os.scandir(stage / folder) as it:
                    listings[folder] = {e.name: e.stat(follow_symlinks=False).st_size
                                        for e in it if e.is_file(follow_symlinks=False)}
            except OSError:
                listings[folder] = {}
        return listings[folder].get(name)

    def need(base: str, uri: str, *, empty_ok: bool = False) -> str | None:
        rel = posixpath.normpath(posixpath.join(base, uri))
        if "://" in uri or posixpath.isabs(uri) or rel == ".." or rel.startswith("../"):
            missing[f"{uri} (outside the package)"] = None
            return None
        n = size(rel)
        if n is None or (n == 0 and not empty_ok):
            missing[rel if n is None else f"{rel} (empty)"] = None
            return None
        return rel

    playlists: dict[str, str] = {}  # rel path -> the folder its URIs resolve from
    master = need("", (manifest.get("hls") or {}).get("master") or "hls/master.m3u8")
    if master:
        for uri in hls.playlist_uris((stage / master).read_text()):
            rel = need(posixpath.dirname(master), uri)
            if rel:
                playlists[rel] = posixpath.dirname(rel)
    renditions = manifest.get("renditions") or {}
    for entry in [*renditions.get("video", []), *renditions.get("audio", []),
                  *renditions.get("audioSurround", [])]:
        if rel := need(entry["dir"], "playlist.m3u8"):
            playlists[rel] = entry["dir"]
    for sub in manifest.get("subtitles") or []:
        need("", sub["path"], empty_ok=True)
        if sub.get("format") == "vobsub":
            need("", posixpath.splitext(sub["path"])[0] + ".sub", empty_ok=True)
        if sub.get("hls") and (rel := need(sub["hls"], "playlist.m3u8")):
            playlists[rel] = sub["hls"]
    for rel, folder in playlists.items():
        text = (stage / rel).read_text()
        if "#EXT-X-ENDLIST" not in text:
            missing[f"{rel} (no #EXT-X-ENDLIST)"] = None
        for uri in dict.fromkeys(hls.playlist_uris(text)):
            need(folder, uri)
    trickplay = manifest.get("trickplay")
    if trickplay and (vtt := need("", trickplay["vttPath"])):
        sprites = re.findall(r"^([^#\s]+\.jpg)#", (stage / vtt).read_text(), re.MULTILINE)
        for sprite in dict.fromkeys(sprites):
            need(posixpath.dirname(vtt), sprite)
    if missing:
        names = list(missing)
        raise PackageError(
            f"package incomplete, not swapped in: {len(names)} missing: "
            + ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
        )


def _swap_in(out_root: Path, stage: Path) -> list[Path]:
    """Move the staged package into the item folder in place of the live
    one. Returns what it replaced, renamed <name>.old-<stamp> beside it.

    Entry by entry, subs/ and trickplay/ first and hls/ (the master's
    tree) last: the live one is renamed within the item folder, so NFS
    clients that have it cached keep reading it (the grace period), and
    the staged one moved up from the staging folder, which no reader has
    ever looked into. Between the two renames the entry is missing.
    manifest.json is replaced last, in one rename: readers get the old
    manifest or the new one, whole, and the new one only once everything
    it names is in place. A failure up to there moves everything back.
    Then what the old package had beyond the new one is retired too, and
    .complete is written."""
    stamp = datetime.now(UTC).strftime(_STAMP)
    names = sorted((p.name for p in stage.iterdir() if p.name not in (SENTINEL, MANIFEST_FILE)),
                   key=lambda name: (name == "hls", name))
    replaced: list[Path] = []
    moved: list[tuple[Path, Path]] = []
    try:
        for name in names:
            live = out_root / name
            if os.path.lexists(live):
                old = out_root / f"{name}{_OLD}{stamp}"
                os.rename(live, old)
                moved.append((live, old))
                replaced.append(old)
            os.rename(stage / name, live)
            moved.append((stage / name, live))
        os.replace(stage / MANIFEST_FILE, out_root / MANIFEST_FILE)
    except BaseException:
        for src, dst in reversed(moved):
            try:
                os.rename(dst, src)
            except OSError as e:
                log.error("packager.swap.undo_failed", path=str(dst), error=str(e)[:200])
        raise
    # The new manifest names none of what is left: a trickplay folder the
    # new run didn't write, a stray file of an older packager.
    keep = {*names, MANIFEST_FILE, STAGING_DIR, ".complete", ".failed"}
    for entry in sorted(out_root.iterdir()):
        if entry.name in keep or _REPLACED.match(entry.name):
            continue
        old = out_root / f"{entry.name}{_OLD}{stamp}"
        try:
            os.rename(entry, old)
            replaced.append(old)
        except OSError as e:
            log.warning("packager.swap.retire_failed", path=str(entry), error=str(e)[:200])
    _write_atomic(out_root / ".complete", (datetime.now(UTC).isoformat() + "\n").encode())
    (out_root / ".failed").unlink(missing_ok=True)
    shutil.rmtree(stage, ignore_errors=True)
    return replaced


def _remove_after(paths: list[Path], seconds: float) -> None:
    """Remove a replaced package once the grace period is over, on a timer
    thread. When the process exits first, the item's next run or the
    startup sweep (sweep_leftovers) removes it."""
    if not paths:
        return

    def remove() -> None:
        gone = [p.name for p in paths if _remove(p)]
        log.info("packager.replaced.removed", dir=str(paths[0].parent), removed=gone)

    if seconds <= 0:
        remove()
        return
    timer = threading.Timer(seconds, remove)
    timer.daemon = True
    timer.start()


def _remove_replaced(item_root: Path, grace_seconds: float) -> int:
    """Remove the entries of replaced packages in one item folder whose
    grace period is over (by the stamp in their names). Returns how many
    went."""
    now = time.time()
    removed = 0
    try:
        with os.scandir(item_root) as it:
            names = [e.name for e in it]
    except OSError:
        return 0
    for name in names:
        m = _REPLACED.match(name)
        if not m:
            continue
        try:
            at = datetime.strptime(m["stamp"], _STAMP).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
        if at + grace_seconds <= now and _remove(item_root / name):
            removed += 1
    return removed


def sweep_leftovers(
    grace_seconds: float, *, stale_after_seconds: float = STALE_STAGING_SECONDS,
) -> int:
    """Clear what runs that ended uncleanly left in the item folders: the
    packages swaps replaced whose grace period is over (their timer died
    with its process) and the staging folders of runs that started more
    than stale_after_seconds ago (the run died; a younger one may be
    another replica's, at work). Returns how many entries went. Walks
    every item folder, so it runs at startup off the worker thread."""
    t0 = time.monotonic()
    cutoff = time.time() - stale_after_seconds
    items = removed = 0
    for category in sorted({*_CATEGORY_BY_TYPE.values(), "other"}):
        for shard in _subdirs(PACKAGES_ROOT / category):
            for item in _subdirs(shard):
                items += 1
                removed += _remove_replaced(item, grace_seconds)
                stage = item / STAGING_DIR
                if _started_before(stage, cutoff) and _remove(stage):
                    log.info("packager.sweep.dead_run", dir=str(item))
                    removed += 1
    log.info("packager.sweep.done", items=items, removed=removed,
             elapsed_s=round(time.monotonic() - t0, 1))
    return removed


def _subdirs(path: Path) -> list[Path]:
    try:
        with os.scandir(path) as it:
            return [Path(e.path) for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _started_before(stage: Path, cutoff: float) -> bool:
    """Whether the run a staging folder belongs to started before cutoff,
    by its sentinel (the folder itself when the run died before writing
    one). False without a staging folder."""
    for path in (stage / SENTINEL, stage):
        try:
            return path.stat().st_mtime < cutoff
        except FileNotFoundError:
            continue
        except OSError:
            return False
    return False


def _remove(path: Path) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("packager.remove_failed", path=str(path), error=str(e)[:200])
        return False
    return True


def _write_failed(out_root: Path, error: Exception) -> None:
    """The .failed sentinel, for operators. Best effort: an item folder
    that can't be written must not hide the error that failed the run."""
    try:
        (out_root / ".failed").write_text(
            json.dumps({"error": str(error), "at": datetime.now(UTC).isoformat()}, indent=2)
        )
    except OSError as e:
        log.warning("packager.failed_unwritable", dir=str(out_root), error=str(e)[:200])


def _ffprobe(path: Path) -> _Probe:
    out = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    raw = json.loads(out.stdout)
    fmt = raw.get("format", {})
    duration_ms = int(float(fmt.get("duration", "0")) * 1000)
    streams = raw.get("streams", [])

    videos = [s for s in streams if s.get("codec_type") == "video"]
    # Cover art rides along as a "video" stream with attached_pic set;
    # the picture is the first video stream that isn't one.
    video = next(
        (s for s in videos if not (s.get("disposition") or {}).get("attached_pic")),
        videos[0] if videos else {},
    )
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    try:
        bit_rate = int(fmt.get("bit_rate") or 0) or None
    except (TypeError, ValueError):
        bit_rate = None
    return _Probe(
        container=fmt.get("format_name", ""),
        duration_ms=duration_ms,
        video=video,
        audio=audio,
        subtitles=subtitles,
        video_index=video.get("index") if video else None,
        bit_rate=bit_rate,
    )


def probe_source(path: Path) -> dict[str, Any]:
    """What the catalog keeps of a title's source file, in the names of
    the transcoder's renditions.json source block: ffprobe's codec name
    and the picture's coded size (`codec`, `width`, `height`), the
    container's duration and overall bit rate (`durationMs`, `bitRate`).
    What ffprobe can't tell is left out; {} when the file can't be probed."""
    try:
        probe = _ffprobe(path)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        log.warning("packager.source_probe_failed", path=str(path), error=str(e)[:200])
        return {}
    out: dict[str, Any] = {}
    if probe.video.get("codec_name"):
        out["codec"] = str(probe.video["codec_name"]).lower()
    width, height = int(probe.video.get("width") or 0), int(probe.video.get("height") or 0)
    if width > 0 and height > 0:
        out["width"], out["height"] = width, height
    if probe.duration_ms > 0:
        out["durationMs"] = probe.duration_ms
    if probe.bit_rate:
        out["bitRate"] = probe.bit_rate
    return out


# ISO 639-2 (bibliographic + terminology) -> 639-1 for the languages a
# catalog realistically carries, so a whitelist / preference of "de"
# matches tracks tagged "ger" or "deu" (and "en" matches "eng").
_ISO639_1 = {
    "eng": "en", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr",
    "spa": "es", "ita": "it", "jpn": "ja", "zho": "zh", "chi": "zh",
    "por": "pt", "rus": "ru", "nld": "nl", "dut": "nl", "swe": "sv",
    "nor": "no", "nob": "nb", "nno": "nn", "dan": "da", "fin": "fi",
    "pol": "pl", "ces": "cs", "cze": "cs", "slk": "sk", "slo": "sk",
    "hun": "hu", "ron": "ro", "rum": "ro", "tur": "tr", "ell": "el",
    "gre": "el", "heb": "he", "ara": "ar", "hin": "hi", "kor": "ko",
    "tha": "th", "vie": "vi", "ukr": "uk", "hrv": "hr", "srp": "sr",
    "slv": "sl", "bul": "bg", "cat": "ca", "ind": "id", "msa": "ms",
    "may": "ms", "fas": "fa", "per": "fa", "isl": "is", "ice": "is",
    "roh": "rm", "lat": "la", "est": "et", "lav": "lv", "lit": "lt",
}


def _lang_key(tag: str | None) -> str:
    """Comparable language key: ISO 639-1 when known ('ger' -> 'de'),
    the bare primary subtag otherwise ('de-CH' -> 'de', 'gsw' -> 'gsw')."""
    t = (tag or "und").strip().lower().replace("_", "-").split("-")[0]
    return _ISO639_1.get(t, t) or "und"


def _track_language(stream: dict[str, Any]) -> str:
    """Pull the lowercased language tag off a probed audio/subtitle
    stream. Falls back to 'und' (the IETF undefined tag) for tracks
    without an explicit tag — those are *always* kept regardless of
    the whitelist so a missing/wrong tag doesn't silently drop the
    only track on a clean source rip."""
    tags = stream.get("tags") or {}
    return (tags.get("language") or "und").lower()


# Tags no language whitelist hides: undetermined, and no linguistic
# content (a track without dialogue).
_ALWAYS_VISIBLE = frozenset({"und", "zxx"})


def _visible_indices(
    streams: list[dict[str, Any]],
    whitelist: list[str] | None,
    *,
    keep_original_if_single: bool,
) -> set[int]:
    """Return the set of stream indices marked *visible to the client*
    given the language whitelist. We never *drop* tracks at the
    packager level — every audio and every text subtitle is still
    encoded into the packaged output so a power-user / future feature
    can opt back into the hidden tracks. The whitelist only controls
    which tracks the standard UI lists in its menu.

    Rules, in order:
      1. Empty/None whitelist → every track is visible.
      2. Streams tagged 'und' (undefined) are always visible — better
         to surface a wrongly-tagged track than hide the only one — and
         so are those tagged 'zxx' (no linguistic content): no language
         to filter by, and often a dialogue-free film's only track.
      3. Streams whose language is in the whitelist are visible, compared
         as ISO 639-1 keys ('ger'/'deu' match 'de').
      4. If the result is empty AND `keep_original_if_single` is True
         AND the source has exactly one distinct language, mark every
         stream visible. Anime / foreign-only fallback.
    """
    if not whitelist:
        return set(range(len(streams)))
    wanted = {_lang_key(w) for w in whitelist}
    visible: set[int] = set()
    for i, s in enumerate(streams):
        key = _lang_key(_track_language(s))
        if key in _ALWAYS_VISIBLE or key in wanted:
            visible.add(i)
    if visible:
        return visible
    if keep_original_if_single:
        distinct = {_lang_key(_track_language(s)) for s in streams}
        distinct -= _ALWAYS_VISIBLE
        if len(distinct) == 1:
            return set(range(len(streams)))
    return visible


def _is_commentary(stream: dict[str, Any]) -> bool:
    disp = stream.get("disposition") or {}
    title = ((stream.get("tags") or {}).get("title") or "").lower()
    return bool(disp.get("comment")) or "comment" in title or "kommentar" in title


def _pick_default_audio(
    streams: list[dict[str, Any]],
    visible: set[int],
    preferred: list[str],
) -> int | None:
    """The one audio track marked DEFAULT=YES: the first preferred
    language that has a track wins; within a language, a visible
    non-commentary track flagged default in the source, else the first
    one. No preference match -> the source's default-flagged track, else
    the first visible track."""
    if not streams:
        return None

    def rank(i: int) -> tuple[bool, bool, bool, int]:
        s = streams[i]
        return (
            i not in visible,
            _is_commentary(s),
            not (s.get("disposition") or {}).get("default"),
            i,
        )

    keys = [_lang_key(_track_language(s)) for s in streams]
    for pref in preferred:
        want = _lang_key(pref)
        candidates = [i for i, k in enumerate(keys) if k == want]
        if candidates:
            return min(candidates, key=rank)
    return min(range(len(streams)), key=rank)


@dataclass(frozen=True)
class _SurroundTrack:
    source_index: int
    mode: str        # "copy" | "encode"
    codec: str       # ffmpeg codec name: eac3 | ac3
    hls_codec: str   # CODECS token: ec-3 | ac-3
    bitrate: str


def _surround_plan(
    streams: list[dict[str, Any]],
    visible: set[int],
    options: PackageOptions,
) -> list[_SurroundTrack]:
    """Which source tracks get a 5.1 companion: visible, >= 6 channels,
    not a commentary, first such track per language (a second English
    5.1 would be a duplicate). Stream-copied when the source already is
    the target codec (an E-AC-3 track), encoded otherwise (DTS, TrueHD,
    FLAC, PCM, AC-3 when the target is E-AC-3)."""
    codec = (options.surround_codec or "off").lower()
    if codec not in _SURROUND_CODECS:
        return []
    plan: list[_SurroundTrack] = []
    seen: set[str] = set()
    for i, s in enumerate(streams):
        if i not in visible or int(s.get("channels") or 0) < 6 or _is_commentary(s):
            continue
        key = _lang_key(_track_language(s))
        if key in seen:
            continue
        seen.add(key)
        mode = "copy" if (s.get("codec_name") or "").lower() == codec else "encode"
        plan.append(_SurroundTrack(i, mode, codec, _SURROUND_CODECS[codec],
                                   options.surround_bitrate))
    return plan


def _pick_default_surround(
    plan: list[_SurroundTrack],
    streams: list[dict[str, Any]],
    default_index: int | None,
    preferred: list[str],
) -> int | None:
    """The source index of the one 5.1 rendition marked DEFAULT=YES, so the
    5.1 group has exactly one, as the stereo group does: the default stereo
    track's own companion; else the 5.1 rendition in the default track's
    language (a companion of another track of it); else the one in the
    first preferred language that has one; else the first. None without 5.1
    renditions. (The plan holds at most one per language.)"""
    if not plan:
        return None
    default_key = (_lang_key(_track_language(streams[default_index]))
                   if default_index is not None else None)
    wanted = [_lang_key(p) for p in preferred]

    def rank(s: _SurroundTrack) -> tuple[bool, bool, int]:
        key = _lang_key(_track_language(streams[s.source_index]))
        return (s.source_index != default_index, key != default_key,
                wanted.index(key) if key in wanted else len(wanted))

    return min(plan, key=rank).source_index


def _timeline_input_args(timeline: str) -> list[str]:
    """`keep` / `offset` inputs are on the contract's shared timeline (or
    moved onto it): don't let ffmpeg renormalise them per file."""
    return ["-copyts"] if timeline in ("keep", "offset") else []


def _timeline_output_args(timeline: str, ts_offset: float) -> list[str]:
    if timeline == "offset" and ts_offset:
        return ["-output_ts_offset", f"{ts_offset:.6f}"]
    return []


def _prepare_source(
    src: Path, probe: _Probe, tmpdir: Path,
    *,
    audio_visible_indices: set[int] | None = None,
    default_index: int | None = None,
    surround: list[_SurroundTrack] | None = None,
    surround_default: int | None = None,
    timeline: str = "normalize",
    ts_offset: float = 0.0,
) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return an MP4 shaka-packager can consume (v0 video stream-copied,
    then one AAC stereo track per source audio track, then the 5.1
    companions) + the stereo and surround track metadata.

    shaka-packager only accepts MP4/fMP4/TS as input containers for
    HEVC — feeding it an MKV makes the WebM demuxer choke ("Unsupported
    video codec"). So we always remux (video copy, no re-encode).

    Stereo: every source track is re-encoded to AAC-LC 48 kHz stereo
    192 kbps — even tracks that are already AAC — for a correctness
    guarantee: every stereo rendition has identical channel count,
    sample rate, and known channel layout. 5.1 / 7.1 source audio left
    as-is with channel_layout=unknown is what Chrome's MSE rejects with
    CHUNK_DEMUXER_ERROR_APPEND_FAILED. The channel conversion is done in
    the filter graph (aformat), never by per-stream `-ac:a:N`, which
    silently no-ops for channel counts.

    Surround: per `_surround_plan`, a stream copy or an E-AC-3/AC-3 5.1
    encode at 48 kHz of the same decoded audio (asplit, one decode). The
    one marked default is `surround_default` (`_pick_default_surround`).

    audio_meta is a list of dicts with keys {idx, codec, language,
    title, channels, default, visible}, in source order (audio_meta[N]
    is the Nth audio stream of the MP4 after the video)."""
    surround = surround or []
    log.info(
        "packager.prepare",
        strategy="remux_to_mp4",
        container=probe.container,
        audio_count=len(probe.audio),
        non_aac=[s.get("codec_name") for s in probe.audio if s.get("codec_name") != "aac"],
        surround=[f"{s.source_index}:{s.mode}" for s in surround] or None,
        timeline=timeline,
    )
    transmuxed = tmpdir / "transmux.mp4"
    vmap = f"0:{probe.video_index}" if probe.video_index is not None else "0:v:0"
    args = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        *_timeline_input_args(timeline),
        "-i", str(src),
        "-map", vmap,
        "-c:v", "copy",
    ]
    if probe.video.get("codec_name") == "hevc":
        # Tag HEVC as hvc1 so MP4 readers (and shaka-packager) recognise
        # the codec — many MKV→MP4 muxers leave it as hev1, which some
        # tools then reject. ONLY for HEVC sources; forcing it on H.264
        # makes ffmpeg refuse with "Tag hvc1 incompatible with output
        # codec id '27' (avc1)".
        args += ["-tag:v", "hvc1"]

    encode_surround = {s.source_index for s in surround if s.mode == "encode"}
    chains: list[str] = []
    for i in range(len(probe.audio)):
        if i in encode_surround:
            chains += [
                f"[0:a:{i}]asplit=2[as{i}][am{i}]",
                f"[as{i}]{_STEREO_FORMAT}[s{i}]",
                f"[am{i}]{_SURROUND_FORMAT}[m{i}]",
            ]
        else:
            chains.append(f"[0:a:{i}]{_STEREO_FORMAT}[s{i}]")
    if chains:
        args += ["-filter_complex", ";".join(chains)]

    out = 0
    for i, stream in enumerate(probe.audio):
        args += [
            "-map", f"[s{i}]",
            f"-c:a:{out}", "aac", f"-b:a:{out}", STEREO_BITRATE,
            f"-metadata:s:a:{out}", f"language={_track_language(stream)}",
        ]
        out += 1
    for s in surround:
        if s.mode == "copy":
            args += ["-map", f"0:a:{s.source_index}", f"-c:a:{out}", "copy"]
        else:
            args += ["-map", f"[m{s.source_index}]", f"-c:a:{out}", s.codec,
                     f"-b:a:{out}", s.bitrate]
        args += [f"-metadata:s:a:{out}",
                 f"language={_track_language(probe.audio[s.source_index])}"]
        out += 1

    # No subtitle / data streams in the intermediate — subtitles are
    # extracted separately to sidecar files.
    args += ["-sn", "-dn", *_timeline_output_args(timeline, ts_offset),
             "-movflags", "+faststart", str(transmuxed)]
    _run_ffmpeg_capturing("transmux", args)

    # Visibility is attached per-meta-entry, not by dropping tracks —
    # every audio stream is packaged into the HLS tree; the client
    # decides which to show in the language menu using the `visible`
    # flag. Exactly one entry is the default.
    meta: list[dict[str, Any]] = []
    for i, s in enumerate(probe.audio):
        entry = _audio_meta_from_stream(i, s, transcoded=True)
        entry["visible"] = (
            audio_visible_indices is None or i in audio_visible_indices
        )
        entry["default"] = i == default_index
        meta.append(entry)
    surround_meta: list[dict[str, Any]] = []
    for s in surround:
        stream = probe.audio[s.source_index]
        tags = stream.get("tags") or {}
        surround_meta.append({
            "idx": s.source_index,
            "codec": s.hls_codec,
            "language": tags.get("language") or "und",
            "title": tags.get("title") or "",
            "channels": int(stream.get("channels") or 6) if s.mode == "copy" else 6,
            "default": s.source_index == surround_default,
            "visible": True,
            "mode": s.mode,
        })
    return transmuxed, meta, surround_meta


@dataclass(frozen=True)
class _StagedVideo:
    rung: VideoInput
    mp4: Path
    probe: _Probe


def _remux_video(rung: VideoInput, tmpdir: Path, ts_offset: float) -> _StagedVideo | None:
    """Stream-copy a lower rung's video into an MP4 for shaka, on the
    shared timeline. A rung that can't be packaged is skipped (logged):
    the item still gets its top rendition."""
    try:
        probe = _ffprobe(rung.path)
    except (subprocess.CalledProcessError, ValueError) as e:
        log.warning("packager.rung.probe_failed", rung=rung.id, error=str(e)[:300])
        return None
    codec = probe.video.get("codec_name")
    if codec not in ("hevc", "h264"):
        log.warning("packager.rung.unsupported_codec", rung=rung.id, codec=codec)
        return None
    target = tmpdir / f"{rung.id}.mp4"
    vmap = f"0:{probe.video_index}" if probe.video_index is not None else "0:v:0"
    args = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        *_timeline_input_args(rung.timeline),
        "-i", str(rung.path),
        "-map", vmap, "-c:v", "copy",
        *(["-tag:v", "hvc1"] if codec == "hevc" else []),
        "-an", "-sn", "-dn",
        *_timeline_output_args(rung.timeline, ts_offset),
        "-movflags", "+faststart", str(target),
    ]
    try:
        _run_ffmpeg_capturing(f"remux {rung.id}", args)
    except PackageError as e:
        log.warning("packager.rung.remux_failed", rung=rung.id, error=str(e)[:300])
        return None
    return _StagedVideo(rung, target, probe)


# The English name of every ISO 639-1 language, by its 639-1 code, its
# ISO 639-2/T code and, where it differs, its 639-2/B code; then 639-2
# codes media files carry that have no 639-1 code. Rendition NAMEs are
# these names.
_LANGUAGE_TABLE = """\
aa aar - Afar
ab abk - Abkhazian
ae ave - Avestan
af afr - Afrikaans
ak aka - Akan
am amh - Amharic
an arg - Aragonese
ar ara - Arabic
as asm - Assamese
av ava - Avaric
ay aym - Aymara
az aze - Azerbaijani
ba bak - Bashkir
be bel - Belarusian
bg bul - Bulgarian
bi bis - Bislama
bm bam - Bambara
bn ben - Bengali
bo bod tib Tibetan
br bre - Breton
bs bos - Bosnian
ca cat - Catalan
ce che - Chechen
ch cha - Chamorro
co cos - Corsican
cr cre - Cree
cs ces cze Czech
cu chu - Church Slavic
cv chv - Chuvash
cy cym wel Welsh
da dan - Danish
de deu ger German
dv div - Divehi
dz dzo - Dzongkha
ee ewe - Ewe
el ell gre Greek
en eng - English
eo epo - Esperanto
es spa - Spanish
et est - Estonian
eu eus baq Basque
fa fas per Persian
ff ful - Fula
fi fin - Finnish
fj fij - Fijian
fo fao - Faroese
fr fra fre French
fy fry - Western Frisian
ga gle - Irish
gd gla - Scottish Gaelic
gl glg - Galician
gn grn - Guarani
gu guj - Gujarati
gv glv - Manx
ha hau - Hausa
he heb - Hebrew
hi hin - Hindi
ho hmo - Hiri Motu
hr hrv - Croatian
ht hat - Haitian Creole
hu hun - Hungarian
hy hye arm Armenian
hz her - Herero
ia ina - Interlingua
id ind - Indonesian
ie ile - Interlingue
ig ibo - Igbo
ii iii - Sichuan Yi
ik ipk - Inupiaq
io ido - Ido
is isl ice Icelandic
it ita - Italian
iu iku - Inuktitut
ja jpn - Japanese
jv jav - Javanese
ka kat geo Georgian
kg kon - Kongo
ki kik - Kikuyu
kj kua - Kuanyama
kk kaz - Kazakh
kl kal - Kalaallisut
km khm - Khmer
kn kan - Kannada
ko kor - Korean
kr kau - Kanuri
ks kas - Kashmiri
ku kur - Kurdish
kv kom - Komi
kw cor - Cornish
ky kir - Kyrgyz
la lat - Latin
lb ltz - Luxembourgish
lg lug - Ganda
li lim - Limburgish
ln lin - Lingala
lo lao - Lao
lt lit - Lithuanian
lu lub - Luba-Katanga
lv lav - Latvian
mg mlg - Malagasy
mh mah - Marshallese
mi mri mao Maori
mk mkd mac Macedonian
ml mal - Malayalam
mn mon - Mongolian
mr mar - Marathi
ms msa may Malay
mt mlt - Maltese
my mya bur Burmese
na nau - Nauru
nb nob - Norwegian Bokmål
nd nde - North Ndebele
ne nep - Nepali
ng ndo - Ndonga
nl nld dut Dutch
nn nno - Norwegian Nynorsk
no nor - Norwegian
nr nbl - South Ndebele
nv nav - Navajo
ny nya - Nyanja
oc oci - Occitan
oj oji - Ojibwa
om orm - Oromo
or ori - Odia
os oss - Ossetic
pa pan - Punjabi
pi pli - Pali
pl pol - Polish
ps pus - Pashto
pt por - Portuguese
qu que - Quechua
rm roh - Romansh
rn run - Rundi
ro ron rum Romanian
ru rus - Russian
rw kin - Kinyarwanda
sa san - Sanskrit
sc srd - Sardinian
sd snd - Sindhi
se sme - Northern Sami
sg sag - Sango
si sin - Sinhala
sk slk slo Slovak
sl slv - Slovenian
sm smo - Samoan
sn sna - Shona
so som - Somali
sq sqi alb Albanian
sr srp - Serbian
ss ssw - Swati
st sot - Southern Sotho
su sun - Sundanese
sv swe - Swedish
sw swa - Swahili
ta tam - Tamil
te tel - Telugu
tg tgk - Tajik
th tha - Thai
ti tir - Tigrinya
tk tuk - Turkmen
tl tgl - Tagalog
tn tsn - Tswana
to ton - Tongan
tr tur - Turkish
ts tso - Tsonga
tt tat - Tatar
tw twi - Twi
ty tah - Tahitian
ug uig - Uyghur
uk ukr - Ukrainian
ur urd - Urdu
uz uzb - Uzbek
ve ven - Venda
vi vie - Vietnamese
vo vol - Volapük
wa wln - Walloon
wo wol - Wolof
xh xho - Xhosa
yi yid - Yiddish
yo yor - Yoruba
za zha - Zhuang
zh zho chi Chinese
zu zul - Zulu
- ast - Asturian
- fil - Filipino
- gsw - Swiss German
- haw - Hawaiian
- nds - Low German
- sco - Scots
- yue - Cantonese
- mul - Multiple languages
"""


def _parse_language_table(table: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in table.splitlines():
        *codes, name = row.split(None, 3)
        names.update(dict.fromkeys((c for c in codes if c != "-"), name))
    return names


_LANGUAGE_NAMES = {
    **_parse_language_table(_LANGUAGE_TABLE),
    # The two codes that name no language: no linguistic content (a film
    # without dialogue) and undetermined (no tag, or one nobody checked).
    "zxx": "No dialogue",
    "und": "Unknown",
}


def _language_name(tag: str | None) -> str:
    """The English name of a track's language: 'eng' -> 'English', 'ger',
    'deu', 'de' and 'de-CH' -> 'German', 'zxx' -> 'No dialogue', 'und' or
    no tag -> 'Unknown'. A code it doesn't know is named by itself."""
    code = (tag or "").strip()
    primary = code.lower().replace("_", "-").split("-")[0] or "und"
    return _LANGUAGE_NAMES.get(primary) or code


def _audio_display_name(meta: dict[str, Any]) -> str:
    """The NAME of an audio rendition: the name of its language
    ("English", "No dialogue", "Unknown"). Never the source's title — a
    free text, often a codec descriptor ("AC3 5.1 @ 640 Kbps") that is
    wrong anyway once the track is AAC stereo. The 5.1 group adds " 5.1";
    a second track of a language is told apart by hls.unique_names."""
    return _language_name(meta.get("language"))


def _audio_meta_from_stream(
    idx: int, stream: dict[str, Any], transcoded: bool = False
) -> dict[str, Any]:
    tags = stream.get("tags") or {}
    disp = stream.get("disposition") or {}
    return {
        "idx": idx,
        "codec": "aac" if transcoded else stream.get("codec_name", "aac"),
        "language": tags.get("language") or "und",
        "title": tags.get("title") or "",
        # Output channels: always 2 after the stereo downmix in
        # _prepare_source. Falls back to the source channel count only
        # for the (currently unreachable) passthrough path.
        "channels": 2 if transcoded else (stream.get("channels") or 2),
        "default": bool(disp.get("default")),
    }


def _extract_subtitles(
    src: Path, probe: _Probe, subs_dir: Path,
    *,
    visible_indices: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Extract every embedded subtitle stream as a sidecar file in
    `subs_dir/`. Text codecs (subrip, ass, mov_text, etc) get
    transcoded to WebVTT — that's what HLS clients consume natively.
    Image codecs (PGS / VobSub / DVB) are stream-copied to their
    native container (.sup for PGS, .sub+.idx for VobSub, .dvb for
    DVB) so a client with an image-subtitle renderer can overlay
    them frame-accurate; clients without one can ignore the
    `format` hint and fall back to the WebVTT tracks for the same
    language. HLS can't carry the image formats; they stay sidecar-only.

    Returns the list of subtitle entries for the manifest. Each
    entry carries `format` so the catalog (and downstream clients)
    can distinguish what's on disk.

    Every track is extracted regardless of the language whitelist
    (we don't lose data). `visible_indices` controls only the
    `visible` flag on each manifest entry — the client uses that
    to decide which to surface in the picker menu. None means every
    extracted track is visible."""
    if not probe.subtitles:
        return []
    subs_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for i, s in enumerate(probe.subtitles):
        codec = s.get("codec_name", "")
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        common = {
            "id": f"sub{i}",
            "language": tags.get("language") or "und",
            "title": tags.get("title") or "",
            "default": bool(disp.get("default")),
            "forced": bool(disp.get("forced")),
            "visible": visible_indices is None or i in visible_indices,
        }
        if codec == "hdmv_pgs_subtitle":
            # PGS — stream-copy to a raw .sup file. The PGS bitstream
            # IS the .sup container (sequence of PCS/WDS/PDS/ODS/END
            # segments); ffmpeg -c:s copy preserves every byte. Clients
            # that ship a PGS renderer (Media3's PgsDecoder on Android,
            # a libpgs-based canvas overlay on web, a Swift PGS layer
            # on iOS) can decode + composite the bitmaps frame-accurate.
            target = subs_dir / f"{i}.sup"
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
                        "-i", str(src),
                        "-map", f"0:s:{i}",
                        "-c:s", "copy",
                        "-f", "sup",
                        str(target),
                    ],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                log.warning("packager.subs.failed", idx=i, codec=codec, error=str(e))
                continue
            log.info("packager.subs.pgs_sidecar", idx=i, bytes=target.stat().st_size)
            out.append({**common, "path": f"subs/{i}.sup", "format": "pgs"})
            continue
        if codec == "dvd_subtitle":
            # VobSub — ffmpeg writes a .sub + .idx pair when format=vobsub
            # is requested. Both files travel together (.idx is the
            # palette + index, .sub is the bitmap stream).
            target = subs_dir / f"{i}.idx"
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
                        "-i", str(src),
                        "-map", f"0:s:{i}",
                        "-c:s", "copy",
                        "-f", "vobsub",
                        str(target),
                    ],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                log.warning("packager.subs.failed", idx=i, codec=codec, error=str(e))
                continue
            log.info("packager.subs.vobsub_sidecar", idx=i)
            out.append({**common, "path": f"subs/{i}.idx", "format": "vobsub"})
            continue
        if codec == "dvb_subtitle":
            # DVB bitmap subs — rare for ripped content but possible
            # for broadcast captures. Stream-copy to a raw .dvb file
            # for the same renderer-on-the-client story as PGS.
            target = subs_dir / f"{i}.dvb"
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
                        "-i", str(src),
                        "-map", f"0:s:{i}",
                        "-c:s", "copy",
                        str(target),
                    ],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                log.warning("packager.subs.failed", idx=i, codec=codec, error=str(e))
                continue
            log.info("packager.subs.dvb_sidecar", idx=i)
            out.append({**common, "path": f"subs/{i}.dvb", "format": "dvb"})
            continue
        target = subs_dir / f"{i}.vtt"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
                    "-i", str(src),
                    "-map", f"0:s:{i}",
                    "-c:s", "webvtt",
                    str(target),
                ],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            log.warning("packager.subs.failed", idx=i, codec=codec, error=str(e))
            continue
        # Client-side visibility hint. None means show every entry
        # (no whitelist configured); a set narrows to the source
        # indices the language filter accepted. We still write the
        # WebVTT file for invisible tracks so a power-user feature
        # can opt back in without re-packaging.
        out.append({**common, "path": f"subs/{i}.vtt", "format": "webvtt"})
    return out


def _generate_trickplay(src: Path, probe: _Probe, out_dir: Path) -> dict[str, Any] | None:
    """Build scrub-preview sprite sheets + WebVTT for the source.

    Strategy: one ffmpeg invocation samples the source at
    fps=1/INTERVAL, scales each frame, and tiles GRID_COLS x GRID_ROWS
    of them per output JPG. That gives ffmpeg the freedom to do
    everything in a single decode pass — much faster than
    image-per-frame extraction. After ffmpeg writes the sprites we
    walk the disk to learn how many frames actually came out (the
    last sprite is usually partial) and emit a VTT mapping each
    timestamp range to its sprite + xywh fragment.

    Returns the manifest's trickplay block, or None when the source
    is too short to produce even one thumbnail (skip the section
    rather than write an empty VTT)."""
    if probe.duration_ms < TRICKPLAY_INTERVAL_SEC * 1000:
        log.info("packager.trickplay.skip_short", duration_ms=probe.duration_ms)
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    sprite_template = out_dir / "sprite-%04d.jpg"
    # ffmpeg filter: sample 1 frame per interval, scale to fit a 320x180
    # box (preserving aspect ratio), pad to exact 320x180, then tile
    # 10x10 frames per output JPG.
    vf = (
        f"fps=1/{TRICKPLAY_INTERVAL_SEC},"
        f"scale={TRICKPLAY_THUMB_WIDTH}:{TRICKPLAY_THUMB_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={TRICKPLAY_THUMB_WIDTH}:{TRICKPLAY_THUMB_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
        f"tile={TRICKPLAY_GRID_COLS}x{TRICKPLAY_GRID_ROWS}"
    )
    args = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        # -skip_frame nokey makes the HEVC decoder discard non-keyframe
        # samples instead of fully decoding them. We only need 1 frame
        # every 10 s and HEVC GOPs are typically <= 10 s long, so
        # keyframe-only decode is effectively free. Without this the
        # decoder was burning ~10 min per movie decoding everything just
        # to throw 99% away. -an / -sn drop audio + subtitles entirely.
        "-skip_frame", "nokey",
        "-an", "-sn",
        "-i", str(src),
        "-vf", vf,
        # qscale ~5 is a sweet spot for thumbnail JPGs: indistinguishable
        # from higher quality at 320 px but ~50 KB per cell.
        "-qscale:v", "5",
        # ffmpeg's numbered-output writer defaults to start at 1 (so
        # we'd get sprite-0001.jpg first). Forcing 0 keeps the file
        # names aligned with the cue indices the VTT generates below,
        # which use 0-based math (sprite_idx = i // cells_per_sprite).
        "-start_number", "0",
        str(sprite_template),
    ]
    t0 = time.monotonic()
    try:
        subprocess.run(args, check=True)
    except subprocess.CalledProcessError as e:
        log.warning("packager.trickplay.failed", error=str(e))
        # Trickplay is a nice-to-have, not a blocker. Drop the section
        # and continue rather than failing the whole package.
        return None
    elapsed = round(time.monotonic() - t0, 1)

    sprites = sorted(out_dir.glob("sprite-*.jpg"))
    if not sprites:
        log.warning("packager.trickplay.no_output")
        return None

    # Total thumbnails actually produced. The last sprite may be a
    # partial tile, so we count by inspecting its dimensions instead
    # of assuming the full grid is filled.
    cells_per_sprite = TRICKPLAY_GRID_COLS * TRICKPLAY_GRID_ROWS
    # Conservatively assume every sprite is full except the last;
    # cap by total expected thumbs from the source duration.
    expected_thumbs = probe.duration_ms // (TRICKPLAY_INTERVAL_SEC * 1000)
    total_thumbs = min(expected_thumbs, len(sprites) * cells_per_sprite)

    # Write the WebVTT cue file. Each cue spans INTERVAL_SEC and points
    # at one cell of one sprite via the WebVTT xywh fragment.
    vtt_lines = ["WEBVTT", ""]
    for i in range(total_thumbs):
        sprite_idx = i // cells_per_sprite
        cell = i % cells_per_sprite
        row = cell // TRICKPLAY_GRID_COLS
        col = cell % TRICKPLAY_GRID_COLS
        x = col * TRICKPLAY_THUMB_WIDTH
        y = row * TRICKPLAY_THUMB_HEIGHT
        start_ms = i * TRICKPLAY_INTERVAL_SEC * 1000
        end_ms = min((i + 1) * TRICKPLAY_INTERVAL_SEC * 1000, probe.duration_ms)
        vtt_lines.append(f"{_ms_to_vtt(start_ms)} --> {_ms_to_vtt(end_ms)}")
        vtt_lines.append(
            f"sprite-{sprite_idx:04d}.jpg"
            f"#xywh={x},{y},{TRICKPLAY_THUMB_WIDTH},{TRICKPLAY_THUMB_HEIGHT}"
        )
        vtt_lines.append("")
    vtt_path = out_dir / "thumbnails.vtt"
    _write_atomic(vtt_path, ("\n".join(vtt_lines)).encode())

    log.info(
        "packager.trickplay.done",
        elapsed_s=elapsed,
        sprites=len(sprites),
        thumbs=total_thumbs,
    )
    return {
        "vttPath": "trickplay/thumbnails.vtt",
        "spritePattern": "trickplay/sprite-%04d.jpg",
        "intervalSec": TRICKPLAY_INTERVAL_SEC,
        "thumbWidth": TRICKPLAY_THUMB_WIDTH,
        "thumbHeight": TRICKPLAY_THUMB_HEIGHT,
        "gridCols": TRICKPLAY_GRID_COLS,
        "gridRows": TRICKPLAY_GRID_ROWS,
    }


def _ms_to_vtt(ms: int) -> str:
    """Format milliseconds as a WebVTT cue timestamp HH:MM:SS.mmm."""
    s, mmm = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{mmm:03d}"


def _descriptor_value(value: str) -> str:
    """shaka-packager uses ',' and '=' as stream-descriptor field
    separators with no escape mechanism."""
    return value.replace(",", ";").replace("=", "-")


_LANG_TAG = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")

# shaka writes every WebVTT segment with X-TIMESTAMP-MAP=LOCAL:00:00:00.000,
# MPEGTS:<--transport_stream_timestamp_offset_ms x 90>: by default 9000, the
# 100 ms it shifts MPEG-TS output by to keep timestamps positive. Our media
# are fMP4, which it doesn't shift, so the map put every cue 100 ms after its
# frame in players that honour it (hls.js). With 0 shaka writes no map, which
# HLS (RFC 8216 3.5) reads as cue time 0 = media time 0: the sidecar's cue
# times are the media's, as both come from the same input timeline.
_TEXT_TIMING = ["--transport_stream_timestamp_offset_ms", "0"]


def _shaka_command(
    descriptors: list[str], segment_seconds: int, master: str,
) -> list[str]:
    return [
        "packager", *descriptors,
        "--segment_duration", str(segment_seconds),
        "--hls_master_playlist_output", master,
        "--hls_playlist_type", "VOD",
    ]


def _run_shaka(cmd: list[str], cwd: Path, label: str) -> None:
    log.info("packager.shaka.start", label=label, cmd=cmd, cwd=str(cwd))
    t0 = time.monotonic()
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise PackageError(
            f"shaka-packager ({label}) exited {result.returncode}: "
            f"{result.stderr.strip()[-500:]}"
        )
    log.info("packager.shaka.done", label=label, elapsed_s=round(time.monotonic() - t0, 1))


def _frame_rate(stream: dict[str, Any]) -> tuple[str, float | None]:
    """ffprobe's fraction string and its value ('24000/1001', 23.976)."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = stream.get(key) or ""
        num, _, den = rate.partition("/")
        try:
            value = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return rate, value
    return "", None


def _video_range(stream: dict[str, Any]) -> str:
    transfer = (stream.get("color_transfer") or "").lower()
    return {"smpte2084": "PQ", "arib-std-b67": "HLG"}.get(transfer, "SDR")


def _run_shaka_packager(
    primary: Path,
    videos: list[_StagedVideo],
    audio_meta: list[dict[str, Any]],
    surround_meta: list[dict[str, Any]],
    out_root: Path,
    *,
    segment_seconds: int = SEGMENT_SECONDS,
    hls_subtitles: bool = False,
    subtitle_meta: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Package every video rung + audio rendition in one shaka run (media
    playlists + one I-frame playlist per rung), the WebVTT subtitles in
    one isolated run each, then assemble hls/master.m3u8. Returns the
    manifest entries for video, stereo audio and surround audio; the
    subtitle entries gain an `hls` dir when their rendition was written.

    shaka-packager selects streams by absolute MP4 stream index when
    given a number, or by type ("video"/"audio"/"text") to pick the first
    stream of that type. Each rung MP4 has exactly one video stream; the
    primary MP4 has video at index 0, stereo tracks at 1..N and the
    surround tracks after them."""
    hls_dir = out_root / "hls"
    hls_dir.mkdir(parents=True, exist_ok=True)

    descriptors = [
        ",".join([
            f"in={v.mp4}",
            "stream=video",
            f"init_segment=hls/{v.rung.id}/init.mp4",
            f"segment_template=hls/{v.rung.id}/seg-$Number%05d$.m4s",
            f"playlist_name=hls/{v.rung.id}/playlist.m3u8",
            f"iframe_playlist_name=hls/{v.rung.id}/iframes.m3u8",
        ])
        for v in videos
    ]
    audio_entries = [(AUDIO_GROUP, m) for m in audio_meta] + [
        (SURROUND_GROUP, m) for m in surround_meta
    ]
    for n, (group, meta) in enumerate(audio_entries):
        # Named by its language, never by the source's title.
        name = _audio_display_name(meta)
        if group == SURROUND_GROUP:
            name = f"{name} 5.1"
        meta["_name"] = name
        descriptors.append(",".join([
            f"in={primary}",
            f"stream={n + 1}",
            f"init_segment=hls/a{n}/init.mp4",
            f"segment_template=hls/a{n}/seg-$Number%05d$.m4s",
            f"playlist_name=hls/a{n}/playlist.m3u8",
            f"hls_group_id={group}",
            f"hls_name={_descriptor_value(name)}",
        ]))
    scratch = "hls/.shaka-master.m3u8"
    _run_shaka(_shaka_command(descriptors, segment_seconds, scratch), out_root, "media")
    shaka = hls.read_shaka_master(out_root / scratch)
    (out_root / scratch).unlink(missing_ok=True)

    # ---- video
    video_meta: list[dict[str, Any]] = []
    variants: list[hls.VideoVariant] = []
    for v in videos:
        uri = f"{v.rung.id}/playlist.m3u8"
        stats = hls.playlist_stats(hls_dir / v.rung.id / "playlist.m3u8")
        attrs = shaka.video.get(uri, {})
        codecs = [c for c in attrs.get("CODECS", "").split(",") if c]
        codec = codecs[0] if codecs else _codec_string_for_video(v.probe.video)
        width = int(v.probe.video.get("width") or 0)
        height = int(v.probe.video.get("height") or 0)
        if "RESOLUTION" in attrs:
            w, _, h = attrs["RESOLUTION"].partition("x")
            width, height = int(w), int(h)
        rate, fps = _frame_rate(v.probe.video)
        if fps is None and attrs.get("FRAME-RATE"):
            fps = float(attrs["FRAME-RATE"])
        video_range = _video_range(v.probe.video)
        variants.append(hls.VideoVariant(
            uri=uri, codec=codec, width=width, height=height, frame_rate=fps,
            video_range=video_range, stats=stats,
        ))
        video_meta.append({
            "id": v.rung.id,
            "dir": f"hls/{v.rung.id}",
            "codec": codec,
            "width": width,
            "height": height,
            "bitrateBps": stats.avg_bps,
            "peakBitrateBps": stats.peak_bps,
            "hdr": video_range != "SDR",
            "videoRange": video_range,
            "frameRate": rate,
            "segments": stats.segments,
            "targetDuration": stats.target_duration or segment_seconds,
            "label": v.rung.label,
            "encoder": v.rung.encoder,
        })

    # ---- audio (exactly one DEFAULT per group; AUTOSELECT on the first
    # visible rendition per language, so commentary isn't auto-picked)
    groups: dict[str, list[hls.AudioRendition]] = {}
    out_audio: dict[str, list[dict[str, Any]]] = {AUDIO_GROUP: [], SURROUND_GROUP: []}
    for n, (group, meta) in enumerate(audio_entries):
        uri = f"a{n}/playlist.m3u8"
        stats = hls.playlist_stats(hls_dir / f"a{n}" / "playlist.m3u8")
        media = shaka.media.get(uri, {})
        codec_list = shaka.group_codecs.get(group) or []
        codec = codec_list[0] if codec_list else (
            "mp4a.40.2" if group == AUDIO_GROUP else meta["codec"])
        groups.setdefault(group, []).append(hls.AudioRendition(
            uri=uri, group=group, language=media.get("LANGUAGE", ""),
            name=meta.pop("_name"), default=bool(meta["default"]),
            autoselect=False, channels=media.get("CHANNELS", str(meta["channels"])),
            codec=codec, stats=stats,
        ))
        out_audio[group].append({
            **meta,
            "id": f"a{n}",
            "dir": f"hls/a{n}",
            "codec": codec,
            "bitrateBps": stats.avg_bps,
            "segments": stats.segments,
            "group": group,
            # Client visibility hint propagated from _prepare_source.
            "visible": meta.get("visible", True),
        })
    for group, renditions in groups.items():
        groups[group] = _finish_group(renditions, out_audio[group])

    # ---- subtitles: WebVTT -> HLS renditions, one isolated shaka run
    # each so a cue file shaka rejects costs that track, not the package.
    subtitle_renditions = _package_text_tracks(subtitle_meta or [], out_root, segment_seconds)

    master = hls.build_master(
        variants,
        groups,
        subtitle_renditions if hls_subtitles else None,
        shaka.iframes,
        subtitle_group=SUBTITLE_GROUP,
        header_comment=(
            f"## Media playlists by {_packager_version()}; master assembled by the "
            "zaentrum packager"
        ),
    )
    _write_atomic(hls_dir / "master.m3u8", master.encode())
    return video_meta, out_audio[AUDIO_GROUP], out_audio[SURROUND_GROUP]


def _finish_group(
    renditions: list[hls.AudioRendition], metas: list[dict[str, Any]],
) -> list[hls.AudioRendition]:
    """Unique NAMEs, AUTOSELECT on the first visible rendition of each
    language (plus the default)."""
    names = hls.unique_names([r.name for r in renditions])
    seen: set[str] = set()
    out: list[hls.AudioRendition] = []
    for r, name, meta in zip(renditions, names, metas, strict=True):
        key = _lang_key(r.language or meta.get("language"))
        autoselect = bool(meta.get("visible", True)) and key not in seen
        if autoselect:
            seen.add(key)
        meta["name"] = name
        out.append(hls.AudioRendition(**{**r.__dict__, "name": name, "autoselect": autoselect}))
    return out


def _package_text_tracks(
    subtitle_meta: list[dict[str, Any]], out_root: Path, segment_seconds: int,
) -> list[hls.SubtitleRendition]:
    """Segment every visible WebVTT sidecar into an HLS subtitle
    rendition at hls/sN/ (N = the sidecar's source index). Marks the
    manifest entry with `hls`. PGS / VobSub / DVB stay sidecar-only."""
    renditions: list[hls.SubtitleRendition] = []
    seen: set[str] = set()
    for entry in subtitle_meta:
        if entry.get("format") != "webvtt" or not entry.get("visible", True):
            continue
        rid = f"s{entry['id'].removeprefix('sub')}"
        lang = (entry.get("language") or "und").lower()
        name = _subtitle_display_name(entry)
        fields = [
            f"in={entry['path']}",
            "stream=text",
            f"segment_template=hls/{rid}/seg-$Number%05d$.vtt",
            f"playlist_name=hls/{rid}/playlist.m3u8",
            f"hls_group_id={SUBTITLE_GROUP}",
            f"hls_name={_descriptor_value(name)}",
        ]
        if _LANG_TAG.match(lang) and lang != "und":
            fields.append(f"language={lang}")
        if entry.get("forced"):
            fields.append("forced_subtitle=1")
        scratch = f"hls/{rid}/.shaka-master.m3u8"
        try:
            _run_shaka(_shaka_command([",".join(fields)], segment_seconds, scratch)
                       + _TEXT_TIMING, out_root, f"text {rid}")
            media = hls.read_shaka_master(out_root / scratch).media.get(
                f"{rid}/playlist.m3u8", {})
            stats = hls.playlist_stats(out_root / "hls" / rid / "playlist.m3u8")
        except (PackageError, OSError, ValueError) as e:
            log.warning("packager.subs.hls_failed", rendition=rid, error=str(e)[:300])
            shutil.rmtree(out_root / "hls" / rid, ignore_errors=True)
            continue
        (out_root / scratch).unlink(missing_ok=True)
        if stats.segments == 0:
            shutil.rmtree(out_root / "hls" / rid, ignore_errors=True)
            continue
        forced = bool(entry.get("forced"))
        key = (_lang_key(lang), forced)
        autoselect = forced or key not in seen
        seen.add(key)
        entry["hls"] = f"hls/{rid}"
        renditions.append(hls.SubtitleRendition(
            uri=f"{rid}/playlist.m3u8",
            language=media.get("LANGUAGE", _lang_key(lang) if lang != "und" else ""),
            name=name, forced=forced, autoselect=autoselect, stats=stats,
        ))
    names = hls.unique_names([r.name for r in renditions])
    return [hls.SubtitleRendition(**{**r.__dict__, "name": n})
            for r, n in zip(renditions, names, strict=True)]


def _subtitle_display_name(entry: dict[str, Any]) -> str:
    """The NAME of a subtitle rendition: the name of its language, and
    "(forced)" for a forced track ("English (forced)"). Never the
    source's title; a second track of a language is told apart by
    hls.unique_names."""
    name = _language_name(entry.get("language"))
    return f"{name} (forced)" if entry.get("forced") else name


def _codec_string_for_video(stream: dict[str, Any]) -> str:
    """Fallback codec string when shaka's master didn't carry one; the
    real one (read from the bitstream by shaka) is preferred."""
    codec = stream.get("codec_name", "")
    if codec == "h264":
        return "avc1.640028"
    if codec == "hevc":
        return "hev1.1.6.L120.B0"
    return codec


def _is_hdr(stream: dict[str, Any]) -> bool:
    transfer = (stream.get("color_transfer") or "").lower()
    return transfer in ("smpte2084", "arib-std-b67")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _write_atomic(path: Path, content: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


def _packager_version() -> str:
    try:
        out = subprocess.run(
            ["packager", "--version"],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return out.stdout.strip().splitlines()[0] if out.stdout else "shaka-packager"
    except Exception:
        return "shaka-packager"


# ---------------------------------------------------------------------------
# Package-state inspector (used by the GET status endpoint).
# ---------------------------------------------------------------------------

def package_status(item_id: str) -> dict[str, Any]:
    """Report the current packaging state of one item. Filesystem-only;
    cheap to call frequently. Possible states:
      - "absent": no output directory exists yet
      - "complete": .complete sentinel present (manifest.json is canonical),
        also while a run builds its replacement
      - "packaging": a run's .next/.packaging sentinel present
      - "failed": .failed sentinel present

    Probes all category dirs (movies/shows/music/other) so the caller
    doesn't have to know item.type. Cheap — at most 4 stat calls.
    """
    out_root = _find_existing_root(item_id)
    if out_root is None:
        return {"state": "absent", "item_id": item_id}
    if not out_root.exists():
        return {"state": "absent", "item_id": item_id}
    if (out_root / ".complete").exists():
        try:
            manifest = json.loads((out_root / "manifest.json").read_text())
        except Exception:
            manifest = None
        return {
            "state": "complete",
            "item_id": item_id,
            "completedAt": (out_root / ".complete").read_text().strip(),
            "manifest": manifest,
        }
    if (out_root / STAGING_DIR / SENTINEL).exists():
        try:
            info = json.loads((out_root / STAGING_DIR / SENTINEL).read_text())
        except Exception:
            info = {}
        return {"state": "packaging", "item_id": item_id, **info}
    if (out_root / ".failed").exists():
        try:
            info = json.loads((out_root / ".failed").read_text())
        except Exception:
            info = {}
        return {"state": "failed", "item_id": item_id, **info}
    return {"state": "unknown", "item_id": item_id}
