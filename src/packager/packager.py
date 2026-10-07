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
  bitmap formats for PGS/VobSub/DVB) exactly as before; the subtitle
  files next to the source (the item record's subtitleFiles) are
  converted to WebVTT and join them. WebVTT tracks are
  additionally packaged as HLS subtitle renditions (hls/sN/); the master
  references them (TYPE=SUBTITLES, FORCED=YES where flagged) only when
  HLS_SUBTITLES is on, so clients that draw their own sidecar subtitles
  aren't surprised by in-manifest ones. At most one subtitle is
  `default` in the manifest: a forced one, not in a foreign language.
* All state is on disk under the per-item output directory, which is
  never moved or created again. `.complete` says the package in it is
  whole and live. A run builds the next one in `.next/` (its sentinel
  `.next/.packaging`) and swaps it in only once it is complete, so
  readers get the old package or the new one, never a half-written one,
  and a run that fails (`.failed`) leaves the live package as it was.
"""

from __future__ import annotations

import codecs
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
from dataclasses import dataclass, field, replace
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
# type) or extras, and shard is the first two hex chars of the item
# uuid. The stream service probes categories on read to find the
# package — it only knows the item id, not the type.
_CATEGORY_BY_TYPE = {
    "movie": "movies",
    "episode": "shows",
    "series": "shows",
    "season": "shows",
    "album": "music",
    "track": "music",
    "song": "music",
    # An extra of a title (a trailer, a featurette: extras.py) has a
    # category of its own, so its package is never inside its title's
    # folder: a title packaged again retires everything in its folder that
    # its new manifest doesn't name (_swap_in).
    "extra": "extras",
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

# The video codecs a rendition may have: passthrough only, never an
# encode here. A library v2 package takes HEVC only (library.py).
VIDEO_CODECS = ("hevc", "h264")
_CODEC_NAMES = {"hevc": "HEVC", "h264": "H.264"}

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
    track_languages: list[Any] | None = None,
    subtitle_files: list[Any] | None = None,
    trickplay: bool = True,
    manifest_extra: dict[str, Any] | None = None,
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

    `source_path` is the item's source file, as the catalog has it.
    `inputs` is the resolved transcoder handoff (packager.renditions);
    None packages `source_path` as the single rendition, as before.
    `inputs.primary` (v0) carries the audio and subtitle tracks: the
    source itself, or the transcoder's encode of it.

    track_languages is the item record's trackLanguages: per-track
    language overrides, [{"kind": "audio" | "subtitle", "ordinal": N,
    "language": "eng"}], N the track's place among the source's tracks
    of that kind in ffprobe order. A track's language is its override,
    else its tag, else und, everywhere: the remux, the manifest, the
    playlists, the whitelist and the DEFAULT pick. Malformed entries are
    ignored (_track_overrides).

    subtitle_files is the item record's subtitleFiles: subtitle files
    next to the source, [{"path": "/abs/movie.en.srt", "language": "eng",
    "label": "English", "forced": false}] (.srt, .vtt, .ass, .ssa). Each
    is converted to WebVTT and packaged after the source's own subtitle
    tracks, as one of them (_subtitle_files, _convert_subtitle_files).

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
    operator edit takes effect on the next item.

    trickplay False leaves the scrub-preview sprites out: no trickplay/
    folder, no `trickplay` in the manifest. An extra of a title (a
    trailer) is packaged so: the clients play it without scrub previews.

    manifest_extra adds keys to the manifest's top level, as they are:
    an extra's `parentId` and `extraKind`. It never replaces a key the
    packager writes; one that would fails the run (PackageError)."""
    options = options or PackageOptions()
    inputs = _inputs_of(source_path, inputs)

    out_root = _item_root(item_id, item_type)
    # Everything below writes into the staging folder; the live package,
    # if there is one, plays on until _swap_in.
    stage = out_root / STAGING_DIR

    try:
        _open_staging(out_root, options.old_package_grace_seconds)
        manifest = build_package(
            item_id, source_path, item_type, stage, inputs=inputs, options=options,
            language_whitelist=language_whitelist,
            keep_original_if_single=keep_original_if_single,
            title=title, year=year, series_title=series_title,
            season_number=season_number, episode_number=episode_number, tmdb_id=tmdb_id,
            track_languages=track_languages, subtitle_files=subtitle_files,
            trickplay=trickplay, manifest_extra=manifest_extra,
        ).manifest
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


def _inputs_of(source_path: str, inputs: PackageInputs | None) -> PackageInputs:
    """The inputs a run packages: the transcoder's handoff, or the source
    as the single rendition. Raises PackageError when v0 is not there."""
    if inputs is None:
        inputs = PackageInputs(video=[VideoInput("v0", Path(source_path))], kind="original")
    src = inputs.primary.path
    if not src.exists():
        raise PackageError(f"source not found: {src}")
    return inputs


@dataclass
class Built:
    """What build_package left in its folder: the manifest that describes
    it (the v2 manifest, written by the caller), the probe of v0 it was
    made from (with the catalog's track languages), and the subtitle file
    next to the source each of its subtitles from one was converted from,
    by subtitle id ("sub3")."""
    manifest: dict[str, Any]
    probe: _Probe
    from_files: dict[str, _SubtitleFile]


def build_package(
    item_id: str,
    source_path: str,
    item_type: str | None,
    stage: Path,
    *,
    inputs: PackageInputs,
    options: PackageOptions,
    language_whitelist: list[str] | None = None,
    keep_original_if_single: bool = True,
    title: str | None = None,
    year: int | None = None,
    series_title: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    tmdb_id: str | None = None,
    track_languages: list[Any] | None = None,
    subtitle_files: list[Any] | None = None,
    trickplay: bool = True,
    manifest_extra: dict[str, Any] | None = None,
    codecs: tuple[str, ...] = VIDEO_CODECS,
) -> Built:
    """Build one package into `stage`, a folder the caller made and owns:
    hls/ (the media playlists, the master), subs/ and trickplay/. Writes
    nothing beside them and no manifest; package_item's arguments, as it
    passes them (see there). `codecs` are the video codecs a rendition may
    have: v0 in another fails the run, a lower rung in another is left out.
    Raises on any failure, leaving what it wrote."""
    src = inputs.primary.path
    segment_seconds = inputs.segment_seconds or options.segment_seconds
    probe = _ffprobe(src)
    if probe.video.get("codec_name") not in codecs:
        names = " and ".join(_CODEC_NAMES.get(c, c) for c in codecs)
        allowed = ("are the allowed input codecs" if len(codecs) > 1
                   else "is the allowed input codec")
        raise PackageError(
            f"video codec {probe.video.get('codec_name')!r} not supported "
            f"(passthrough only; {names} {allowed})"
        )
    # The catalog's language for a track wins over the file's tag.
    overrides = _track_overrides(track_languages)
    if overrides:
        probe = _with_track_languages(
            probe, overrides, _probe_of_source(src, Path(source_path)))

    # The subtitle files next to the source come after its own
    # subtitle tracks, as tracks like them.
    sub_files = _subtitle_files(subtitle_files, Path(source_path))

    # Resolve client-visibility windows for audio + subtitle
    # tracks from the language whitelist. Tracks ALWAYS get
    # packaged — `visible` is just a hint for the player UI.
    audio_visible = _visible_indices(
        probe.audio, language_whitelist,
        keep_original_if_single=keep_original_if_single,
    )
    sub_visible = _visible_indices(
        [*probe.subtitles, *({"tags": {"language": f.language}} for f in sub_files)],
        language_whitelist,
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
        subtitle_files=len(sub_files) or None,
        sub_visible=len(sub_visible),
        track_languages=len(overrides) or None,
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
            staged = _remux_video(rung, tmpdir, inputs.timestamp_offset, codecs)
            if staged is not None:
                videos.append(staged)
        subtitle_meta = _extract_subtitles(
            src, probe, stage / "subs",
            visible_indices=sub_visible,
        )
        converted = _convert_subtitle_files(
            sub_files, stage / "subs", tmpdir,
            first=len(probe.subtitles), visible_indices=sub_visible,
        )
        # subN of a file is numbered on from the source's own tracks, in
        # the order of the files (a file that failed keeps its number).
        from_files = {e["id"]: sub_files[int(e["id"].removeprefix("sub")) - len(probe.subtitles)]
                      for e in converted}
        subtitle_meta += converted
        audio_language = (_track_language(probe.audio[default_audio])
                          if default_audio is not None else None)
        default_subtitle = _pick_default_subtitle(subtitle_meta, audio_language)
        for i, entry in enumerate(subtitle_meta):
            entry["default"] = i == default_subtitle
        log.info("packager.subs.default", audio_language=audio_language,
                 subtitle=subtitle_meta[default_subtitle]["id"]
                 if default_subtitle is not None else None)
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
    trickplay_meta = (_generate_trickplay(src, probe, stage / "trickplay")
                      if trickplay else None)

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
    if manifest_extra:
        # Added, never replacing: the playback service and the catalog
        # read the packager's keys, and the swap checks what they name.
        clash = sorted(set(manifest_extra) & set(manifest))
        if clash:
            raise PackageError(f"manifest_extra would replace {', '.join(clash)}")
        manifest.update(manifest_extra)
    return Built(manifest, probe, from_files)


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


def _verify_staged(stage: Path, manifest: dict[str, Any] | None = None) -> None:
    """Refuse a staged package that isn't whole: every playlist the master
    or the manifest names is there and ends (#EXT-X-ENDLIST), and every
    init section, segment, sidecar and trickplay sprite they reference is
    there, inside the package and, but for a sidecar (a track without a
    cue extracts to an empty file), not empty. The manifest is the staged
    manifest.json unless given (a library v2 package.json, which names its
    files under the same keys). Raises PackageError."""
    if manifest is None:
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


# ISO 639-1 -> ISO 639-2: _ISO639_1 turned round, the terminology code
# where a language has two ('de' -> 'deu'), as the table lists it first.
_ISO639_2 = {two: three for three, two in reversed(_ISO639_1.items())}


def iso639_2(tag: str | None) -> str | None:
    """A language as BCP 47 or ISO 639-2 names it ('en', 'pt-BR', 'eng',
    'zxx') as the ISO 639-2 code a trackLanguages override takes: its
    primary subtag, a two-letter one by _ISO639_2, a three-letter one as
    it is. None for und, a two-letter code the table doesn't know, and
    anything else."""
    t = (tag or "").strip().lower().replace("_", "-").split("-")[0]
    if len(t) == 2:
        return _ISO639_2.get(t)
    return t if _LANGUAGE_CODE.match(t) and t != "und" else None


def _track_language(stream: dict[str, Any]) -> str:
    """The track's language: the lowercased language tag of a probed
    audio/subtitle stream, which is its trackLanguages override once
    _with_track_languages has run. Falls back to 'und' (the IETF
    undefined tag) for tracks without an explicit tag — those are
    *always* kept regardless of the whitelist so a missing/wrong tag
    doesn't silently hide a source's only track."""
    tags = stream.get("tags") or {}
    return (tags.get("language") or "und").lower()


# The kinds of track a trackLanguages entry names.
_TRACK_KINDS = ("audio", "subtitle")
_LANGUAGE_CODE = re.compile(r"^[a-z]{3}$")


def _language_code(value: Any) -> str | None:
    """A language as the item record spells it: an ISO 639-2 code, B or T
    form ('ger', 'deu'), zxx and und included, lowercased. None for
    anything else."""
    if not isinstance(value, str):
        return None
    code = value.strip().lower()
    return code if _LANGUAGE_CODE.match(code) else None


def _track_overrides(entries: list[Any] | None) -> dict[tuple[str, int], str]:
    """The item record's trackLanguages as {(kind, ordinal): language}. An
    entry counts when its kind is "audio" or "subtitle", its ordinal an
    int >= 0 (the track's place among the source's tracks of that kind,
    in ffprobe order) and its language a code (_language_code); any other
    is ignored, logged. A track named twice takes the last entry."""
    out: dict[tuple[str, int], str] = {}
    for entry in entries or []:
        ok = isinstance(entry, dict)
        kind = entry.get("kind") if ok else None
        ordinal = entry.get("ordinal") if ok else None
        language = _language_code(entry.get("language")) if ok else None
        if (kind not in _TRACK_KINDS or not isinstance(ordinal, int)
                or isinstance(ordinal, bool) or ordinal < 0 or language is None):
            log.warning("packager.track_language.ignored", entry=str(entry)[:200])
            continue
        out[(kind, ordinal)] = language
    return out


def _source_ordinals(
    packaged: list[dict[str, Any]], source: list[dict[str, Any]] | None,
) -> list[int]:
    """Each packaged track's ordinal among the source's tracks of its kind.
    The packaged file (v0) is the source itself or the transcoder's encode
    of it, which carries every audio track and the subtitle tracks
    Matroska can stream-copy, in the source's order: each of its tracks is
    the source's next one of the same codec. Without the source's tracks,
    or when they don't line up, the packaged order is the source's."""
    same = list(range(len(packaged)))
    if source is None:
        return same
    out: list[int] = []
    j = 0
    for stream in packaged:
        codec = stream.get("codec_name")
        while j < len(source) and source[j].get("codec_name") != codec:
            j += 1
        if j == len(source):
            return same
        out.append(j)
        j += 1
    return out


def _with_track_languages(
    probe: _Probe, overrides: dict[tuple[str, int], str], source: _Probe | None,
) -> _Probe:
    """The probe with each audio and subtitle track's language tag replaced
    by its override, found by the track's ordinal in the source
    (_source_ordinals; `source` is the source's probe when v0 is an encode
    of it). A track without an override keeps its tag; an override that
    names no packaged track is ignored, logged."""
    applied: list[str] = []
    used: set[tuple[str, int]] = set()

    def apply(kind: str, streams: list[dict[str, Any]],
              in_source: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for stream, ordinal in zip(streams, _source_ordinals(streams, in_source), strict=True):
            language = overrides.get((kind, ordinal))
            if language is None:
                out.append(stream)
                continue
            used.add((kind, ordinal))
            applied.append(f"{kind} {ordinal}: {_track_language(stream)} -> {language}")
            out.append({**stream, "tags": {**(stream.get("tags") or {}), "language": language}})
        return out

    audio = apply("audio", probe.audio, source.audio if source else None)
    subtitles = apply("subtitle", probe.subtitles, source.subtitles if source else None)
    log.info("packager.track_languages", applied=applied or None,
             unmatched=[f"{k} {o}" for k, o in sorted(set(overrides) - used)] or None)
    return replace(probe, audio=audio, subtitles=subtitles)


def _probe_of_source(packaged: Path, source: Path) -> _Probe | None:
    """The source's own probe when the packaged file (v0) is the
    transcoder's encode of it, for _source_ordinals: the trackLanguages
    ordinals count the source's tracks. None when v0 is the source, or the
    source can't be probed (gone, unreadable)."""
    if packaged == source or not source.exists():
        return None
    try:
        return _ffprobe(source)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        log.warning("packager.source_probe_failed", path=str(source), error=str(e)[:200])
        return None


# The tags that name no language: undetermined, and no linguistic content
# (a track without dialogue). No whitelist hides such a track, and no
# subtitle is foreign to it.
_NO_LANGUAGE = frozenset({"und", "zxx"})


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
        if key in _NO_LANGUAGE or key in wanted:
            visible.add(i)
    if visible:
        return visible
    if keep_original_if_single:
        distinct = {_lang_key(_track_language(s)) for s in streams}
        distinct -= _NO_LANGUAGE
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


def _pick_default_subtitle(
    entries: list[dict[str, Any]], audio_language: str | None,
) -> int | None:
    """The one subtitle track marked default, if any (the manifest's
    `default`): a visible forced track (it shows what the audio doesn't
    say in its language: a line in another one, a sign) that isn't in a
    foreign language. In the language of the default audio track first,
    else in no known language; any forced track when the audio's language
    isn't known (und, zxx, no audio). No other track is default, whatever
    the source flags: ffmpeg flags the first subtitle default when a file
    has several and flags none (Sintel's German on an English film, after
    the transcoder's remux), and full subtitles shown by themselves are the
    viewer's choice, not the package's."""
    forced = [i for i, e in enumerate(entries) if e.get("forced") and e.get("visible", True)]
    audio = _lang_key(audio_language)
    if audio in _NO_LANGUAGE:
        return forced[0] if forced else None
    keys = {i: _lang_key(entries[i].get("language")) for i in forced}
    same = [i for i in forced if keys[i] == audio]
    unknown = [i for i in forced if keys[i] in _NO_LANGUAGE]
    return (same or unknown or [None])[0]


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
        args += _hevc_copy_args(src, probe.video_index)
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
            "language": _track_language(stream),
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


def _remux_video(
    rung: VideoInput, tmpdir: Path, ts_offset: float, codecs: tuple[str, ...] = VIDEO_CODECS,
) -> _StagedVideo | None:
    """Stream-copy a lower rung's video into an MP4 for shaka, on the
    shared timeline. A rung that can't be packaged (or whose codec isn't
    one of `codecs`) is skipped (logged): the item still gets its top
    rendition."""
    try:
        probe = _ffprobe(rung.path)
    except (subprocess.CalledProcessError, ValueError) as e:
        log.warning("packager.rung.probe_failed", rung=rung.id, error=str(e)[:300])
        return None
    codec = probe.video.get("codec_name")
    if codec not in codecs:
        log.warning("packager.rung.unsupported_codec", rung=rung.id, codec=codec)
        return None
    target = tmpdir / f"{rung.id}.mp4"
    vmap = f"0:{probe.video_index}" if probe.video_index is not None else "0:v:0"
    args = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        *_timeline_input_args(rung.timeline),
        "-i", str(rung.path),
        "-map", vmap, "-c:v", "copy",
        *([*_hevc_copy_args(rung.path, probe.video_index), "-tag:v", "hvc1"]
          if codec == "hevc" else []),
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


# An HEVC stream's parameter sets (VPS, SPS, PPS) are in its decoder
# configuration record, the hvcC its container carries (a Matroska
# CodecPrivate), and may be in the stream as well. Some files' hvcC names
# none of them: their parameter sets are in the stream only. FFmpeg's MP4
# muxer (since 7.1) rebuilds the hvcC it is handed, needs a VPS, an SPS
# and a PPS in it for an hvc1 track, and without them writes an empty hvcC
# box, which shaka-packager can't parse ("Failed to parse hevc"). The
# stream is then copied through Annex B (hevc_mp4toannexb): it reaches the
# muxer without a decoder configuration, and the muxer builds a whole one
# from the parameter sets of the first frame. Nothing is re-encoded, and
# every other stream is copied exactly as before.
_HEVC_PARAMETER_SETS = frozenset({32, 33, 34})  # VPS, SPS, PPS
_DUMP_LINE = re.compile(r"[0-9a-f]{8}: ")


def _hevc_copy_args(path: Path, stream_index: int | None) -> list[str]:
    """What a stream copy of the HEVC stream at stream_index needs on top
    to become an hvc1 track shaka-packager reads: the Annex B round trip
    when its decoder configuration has no parameter sets, else nothing."""
    if not _parameter_sets_in_band_only(path, stream_index):
        return []
    log.info("packager.hevc.in_band_parameter_sets", path=str(path), stream=stream_index)
    return ["-bsf:v", "hevc_mp4toannexb"]


def _parameter_sets_in_band_only(path: Path, stream_index: int | None) -> bool:
    """Whether the HEVC stream's decoder configuration record, the hvcC its
    container carries, lacks a VPS, an SPS or a PPS. False when it has all
    three, when it is none (no record, or Annex B parameter sets, which the
    muxer reads itself) and when it can't be read."""
    record = _codec_private(path, stream_index)
    return record is not None and _hvcc_lacks_parameter_sets(record)


def _codec_private(path: Path, stream_index: int | None) -> bytes | None:
    """A stream's extradata (a Matroska CodecPrivate, an MP4 sample entry's
    record), as ffprobe dumps it; None without one, or when it can't be
    read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams",
             str(stream_index) if stream_index is not None else "v:0",
             "-show_entries", "stream=extradata_size,extradata", "-show_data",
             "-print_format", "json", str(path)],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=120)
        streams = json.loads(out.stdout).get("streams") or []
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as e:
        log.warning("packager.extradata_probe_failed", path=str(path), error=str(e)[:200])
        return None
    if len(streams) != 1 or not isinstance(streams[0], dict):
        return None
    size = streams[0].get("extradata_size")
    data = _dump_bytes(streams[0].get("extradata") or "")
    return data if isinstance(size, int) and size > 0 and len(data) == size else None


def _dump_bytes(dump: str) -> bytes:
    """The bytes of an ffprobe -show_data dump: lines of an offset, then up
    to 16 bytes in hex, two to a group, in the 41 columns after it, then
    their text. b"" for a dump it can't read."""
    out = bytearray()
    for line in dump.splitlines():
        if _DUMP_LINE.match(line):
            try:
                out += bytes.fromhex(line[10:51])
            except ValueError:
                return b""
    return bytes(out)


def _hvcc_lacks_parameter_sets(record: bytes) -> bool:
    """Whether an HEVCDecoderConfigurationRecord (ISO/IEC 14496-15) names
    no VPS, no SPS or no PPS of the base layer (nuh_layer_id 0), or ends
    before its arrays do: the record FFmpeg's MP4 muxer can't rebuild.
    False for what isn't one (none, or Annex B parameter sets)."""
    if len(record) < 23 or record[0] != 1:
        return False
    found: set[int] = set()
    pos = 23
    for _ in range(record[22]):              # numOfArrays
        if pos + 3 > len(record):
            return True
        nal_type = record[pos] & 0x3F
        count = int.from_bytes(record[pos + 1:pos + 3], "big")
        pos += 3
        for _ in range(count):
            if pos + 2 > len(record):
                return True
            length = int.from_bytes(record[pos:pos + 2], "big")
            pos += 2
            if pos + length > len(record):
                return True
            if length >= 2 and ((record[pos] & 1) << 5 | record[pos + 1] >> 3) == 0:
                found.add(nal_type)
            pos += length
    return not _HEVC_PARAMETER_SETS <= found


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
    # The codes that name no language: no linguistic content (a film
    # without dialogue), undetermined (no tag, or one nobody checked) and
    # one without a code of its own (named as the clients name it).
    "zxx": "No dialogue",
    "und": "Unknown",
    "mis": "Other language",
}

# What a track's language is called in that language, where a source's
# title is likely to say it so ("Deutsch" on a German track).
_ENDONYMS = {
    "Arabic": "العربية", "Bulgarian": "Български", "Catalan": "Català",
    "Chinese": "中文", "Croatian": "Hrvatski", "Czech": "Čeština", "Danish": "Dansk",
    "Dutch": "Nederlands", "Estonian": "Eesti", "Finnish": "Suomi", "French": "Français",
    "German": "Deutsch", "Greek": "Ελληνικά", "Hebrew": "עברית", "Hindi": "हिन्दी",
    "Hungarian": "Magyar", "Icelandic": "Íslenska", "Indonesian": "Bahasa Indonesia",
    "Italian": "Italiano", "Japanese": "日本語", "Korean": "한국어", "Latvian": "Latviešu",
    "Lithuanian": "Lietuvių", "Norwegian": "Norsk", "Norwegian Bokmål": "Norsk bokmål",
    "Norwegian Nynorsk": "Norsk nynorsk", "Persian": "فارسی", "Polish": "Polski",
    "Portuguese": "Português", "Romanian": "Română", "Russian": "Русский",
    "Serbian": "Српски", "Slovak": "Slovenčina", "Slovenian": "Slovenščina",
    "Spanish": "Español", "Swedish": "Svenska", "Thai": "ไทย", "Turkish": "Türkçe",
    "Ukrainian": "Українська", "Vietnamese": "Tiếng Việt",
}


def _language_name(tag: str | None) -> str:
    """The English name of a track's language: 'eng' -> 'English', 'ger',
    'deu', 'de' and 'de-CH' -> 'German', 'zxx' -> 'No dialogue', 'und' or
    no tag -> 'Unknown'. A code it doesn't know is named by itself."""
    code = (tag or "").strip()
    primary = code.lower().replace("_", "-").split("-")[0] or "und"
    return _LANGUAGE_NAMES.get(primary) or code


# What a source's title says of a track besides its language, by the rule
# the clients label tracks with (chino-web's lib/languages.ts, ported to
# the TV and mobile apps), so a NAME reads as their menus do. A title that
# names the source's audio format (a codec, a bit rate, a sample rate or
# depth: "AC3 5.1 @ 640 Kbps", "DTS-HD MA 5.1") says nothing of the track,
# which is AAC whatever the file had.
_FORMAT_WORDS = re.compile(
    r"(^|[^A-Za-z0-9])(dts(-hd)?|truehd|atmos|dolby|e?-?ac-?3|ddp?\+?|aac|flac|l?pcm|opus|mp3"
    r"|vorbis|lossless|master audio|\d+ ?k?hz|\d* ?[km]bps|kb/s|\d+[- ]?bit)(?![A-Za-z0-9])",
    re.IGNORECASE)
# A channel layout goes from a title that says more ("Commentary 5.1").
_LAYOUT_WORDS = re.compile(
    r"(^|[^A-Za-z0-9.])(mono|stereo|surround|[1-9]\.[0-2]|\d{1,2} ?ch(annels?)?)(?![A-Za-z0-9.])",
    re.IGNORECASE)
# A title that only numbers the track ("Track 2", "Audio Track 1", "#2").
_NUMBERED = re.compile(r"^(audio|sound|track|stream|[\s#])*\d*$", re.IGNORECASE)
_CODE_LIKE = re.compile(r"^[a-z]{2,3}([-_][a-z0-9]+)*$", re.IGNORECASE)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
# The separators around a title's words: \u2013 and \u2014 are the en and em dash,
# \u00b7 the middle dot.
_EDGE_SEPARATORS = re.compile(r"^[\s\-\u2013\u2014:\u00b7,|/]+|[\s\-\u2013\u2014:\u00b7,|/]+$")
_LEADING_SEPARATORS = re.compile(r"^[\s\-\u2013\u2014:\u00b7,|]+")
_QUALIFIER_START = re.compile(r"^([\s\-\u2013\u2014:\u00b7,|(\[]|$)")
# The codes that name no one language to follow.
_NO_LANGUAGE_CODES = frozenset({"und", "zxx", "mul", "mis"})
# How long what a title adds to a NAME may be.
_TITLE_WORDS_MAX = 40


def _title_words(title: str | None, language: str | None) -> str:
    """What a track's source title says besides its language: "Commentary",
    "Director's commentary", "SDH", "Audio description", "Signs & Songs",
    "Forced" ("English (SDH)" on an English track: "SDH"); '' when it says
    nothing: no title, a format ("AC3 5.1 @ 640 Kbps", "DTS-HD MA", "AAC
    2.0"), a number ("Track 0", "#2"), the track's language by its name or
    code ("English", "Deutsch", "eng") or a code that names no language.
    A layout goes from it ("Commentary 5.1"); more than _TITLE_WORDS_MAX
    characters are cut at a word."""
    raw = (title or "").strip()
    if not raw or _FORMAT_WORDS.search(raw):
        return ""
    words = _LAYOUT_WORDS.sub(r"\1", raw)
    words = _EDGE_SEPARATORS.sub("", " ".join(_EMPTY_BRACKETS.sub("", words).split()))
    if not words or _NUMBERED.match(words) or _is_language_code(words, language):
        return ""
    if _lang_key(language) != "und":
        name = _language_name(language)
        for said in (name, _ENDONYMS.get(name, "")):
            head, rest = words[:len(said)], words[len(said):]
            if said and head.casefold() == said.casefold() and _QUALIFIER_START.match(rest):
                words = _LEADING_SEPARATORS.sub("", rest).strip()
                if words[:1] + words[-1:] in ("()", "[]"):
                    words = words[1:-1].strip()
                break
    if not words or _NUMBERED.match(words):
        return ""
    if len(words) > _TITLE_WORDS_MAX:
        # At a word, unless that leaves less than half; with the ellipsis.
        room = _TITLE_WORDS_MAX - 1
        cut = words[:room + 1].rsplit(" ", 1)[0]
        if not room // 2 <= len(cut) <= room:
            cut = words[:room]
        words = _EDGE_SEPARATORS.sub("", cut) + "…"
    return words


def _is_language_code(title: str, language: str | None) -> bool:
    """A title that is only a language code: its track's own ('eng' or 'en'
    on an English track) or one that names no language ('und')."""
    if not _CODE_LIKE.match(title):
        return False
    primary = title.lower().replace("_", "-").split("-")[0]
    return primary in _NO_LANGUAGE_CODES or _language_name(title) == _language_name(language)


def _track_display_name(language: str | None, title: str | None, *, layout: str = "") -> str:
    """A rendition's NAME: the name of its language, its layout (" 5.1"),
    then what its source title says besides ("English · Commentary"). A
    track of no known language is called what its title says, else
    "Unknown"; one without dialogue "No dialogue"."""
    words = _title_words(title, language)
    if _lang_key(language) == "und" and words:
        return words + layout
    name = _language_name(language) + layout
    return f"{name} · {words}" if words else name


def _audio_display_name(meta: dict[str, Any], *, surround: bool = False) -> str:
    """The NAME of an audio rendition: the name of its language ("English",
    "No dialogue", "Unknown"), " 5.1" in the 5.1 group, then what the
    source's title says besides ("English · Commentary"). Not a title that
    only names a format ("AC3 5.1 @ 640 Kbps"): the track is AAC stereo
    whatever the file had. A second track that reads the same is told
    apart by hls.unique_names."""
    return _track_display_name(meta.get("language"), meta.get("title"),
                               layout=" 5.1" if surround else "")


def _audio_meta_from_stream(
    idx: int, stream: dict[str, Any], transcoded: bool = False
) -> dict[str, Any]:
    tags = stream.get("tags") or {}
    disp = stream.get("disposition") or {}
    return {
        "idx": idx,
        "codec": "aac" if transcoded else stream.get("codec_name", "aac"),
        "language": _track_language(stream),
        "title": tags.get("title") or "",
        # Output channels: always 2 after the stereo downmix in
        # _prepare_source. Falls back to the source channel count only
        # for the (currently unreachable) passthrough path.
        "channels": 2 if transcoded else (stream.get("channels") or 2),
        "default": bool(disp.get("default")),
    }


@dataclass(frozen=True)
class _Sidecar:
    """How one embedded subtitle stream becomes a sidecar file: its name
    in subs/, the manifest's `format` for it, ffmpeg's output options for
    it, and whether it is written in the one pass over the source with the
    others (_extract_subtitles)."""
    index: int             # among the source's subtitle streams
    name: str
    format: str
    options: tuple[str, ...]
    one_pass: bool


# Subtitle codecs that are bitmaps but not PGS: WebVTT can't be made of
# them (XSUB, and DVB teletext, which libzvbi decodes to bitmaps by
# default), and FFmpeg has no muxer for a VobSub .idx/.sub pair or a .dvb
# file. Their extraction fails while ffmpeg sets its outputs up, before
# it reads the source, so each keeps its own attempt, outside the pass
# with the others, which it would otherwise fail.
_SETUP_FAILS = frozenset({"dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext"})


def _sidecar(index: int, codec: str) -> _Sidecar:
    if codec == "hdmv_pgs_subtitle":
        # PGS — stream-copy to a raw .sup file. The PGS bitstream IS the
        # .sup container (sequence of PCS/WDS/PDS/ODS/END segments);
        # ffmpeg -c:s copy preserves every byte. Clients that ship a PGS
        # renderer (Media3's PgsDecoder on Android, a libpgs-based canvas
        # overlay on web, a Swift PGS layer on iOS) can decode + composite
        # the bitmaps frame-accurate.
        return _Sidecar(index, f"{index}.sup", "pgs", ("-c:s", "copy", "-f", "sup"), True)
    if codec == "dvd_subtitle":
        # VobSub — a .sub + .idx pair (.idx is the palette + index, .sub
        # the bitmap stream), were ffmpeg to write format=vobsub.
        return _Sidecar(index, f"{index}.idx", "vobsub", ("-c:s", "copy", "-f", "vobsub"),
                        False)
    if codec == "dvb_subtitle":
        # DVB bitmap subs — rare outside broadcast recordings: a raw .dvb
        # file, for the same renderer-on-the-client story as PGS.
        return _Sidecar(index, f"{index}.dvb", "dvb", ("-c:s", "copy"), False)
    return _Sidecar(index, f"{index}.vtt", "webvtt", ("-c:s", "webvtt"),
                    codec not in _SETUP_FAILS)


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

    The PGS and text tracks are written in one ffmpeg run, one output
    each: the source is read once, not once per track — a title with
    dozens of tracks on slow storage took one full read of the file each.
    When that run fails, each track is extracted alone, as before, so a
    track ffmpeg can't extract costs that track only. A track of a codec
    that fails while ffmpeg sets its outputs up keeps an attempt of its
    own (_SETUP_FAILS): VobSub and DVB tracks so are never extracted, as
    ffmpeg can write neither.

    Returns the list of subtitle entries for the manifest. Each
    entry carries `format` so the catalog (and downstream clients)
    can distinguish what's on disk.

    Every track is extracted regardless of the language whitelist
    (we don't lose data). `visible_indices` controls only the
    `visible` flag on each manifest entry — the client uses that
    to decide which to surface in the picker menu. None means every
    extracted track is visible. No entry is `default` here; package_item
    marks the one there may be (_pick_default_subtitle): the source's
    default flag doesn't count."""
    if not probe.subtitles:
        return []
    subs_dir.mkdir(parents=True, exist_ok=True)
    codecs = [s.get("codec_name", "") for s in probe.subtitles]
    sidecars = [_sidecar(i, codec) for i, codec in enumerate(codecs)]
    together = [c for c in sidecars if c.one_pass]
    written = _extract_together(src, subs_dir, together) if len(together) > 1 else None
    if written is None:
        written = {c.index for c in together if _extract_alone(src, subs_dir, c, codecs[c.index])}
    written |= {c.index for c in sidecars
                if not c.one_pass and _extract_alone(src, subs_dir, c, codecs[c.index])}
    out: list[dict[str, Any]] = []
    for c, s in zip(sidecars, probe.subtitles, strict=True):
        if c.index not in written:
            continue
        if c.format == "pgs":
            log.info("packager.subs.pgs_sidecar", idx=c.index,
                     bytes=(subs_dir / c.name).stat().st_size)
        elif c.format == "vobsub":
            log.info("packager.subs.vobsub_sidecar", idx=c.index)
        elif c.format == "dvb":
            log.info("packager.subs.dvb_sidecar", idx=c.index)
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        out.append({
            "id": f"sub{c.index}",
            "language": _track_language(s),
            "title": tags.get("title") or "",
            "default": False,
            "forced": bool(disp.get("forced")),
            # Client-side visibility hint. None means show every entry
            # (no whitelist configured); a set narrows to the source
            # indices the language filter accepted. We still write the
            # file for invisible tracks so a power-user feature can opt
            # back in without re-packaging.
            "visible": visible_indices is None or c.index in visible_indices,
            "path": f"subs/{c.name}",
            "format": c.format,
        })
    return out


def _extract_together(src: Path, subs_dir: Path, sidecars: list[_Sidecar]) -> set[int] | None:
    """Write every one of the sidecars in one ffmpeg run over the source,
    an output each. Returns the indices it wrote — all of them — or None
    when the run failed, with what it wrote removed."""
    args = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-i", str(src)]
    for c in sidecars:
        args += ["-map", f"0:s:{c.index}", *c.options, str(subs_dir / c.name)]
    t0 = time.monotonic()
    try:
        result = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as e:
        result = subprocess.CompletedProcess(args, -1, "", str(e))
    stderr = (result.stderr or "").strip()
    if result.returncode == 0 and all((subs_dir / c.name).is_file() for c in sidecars):
        log.info("packager.subs.one_pass", tracks=len(sidecars),
                 elapsed_s=round(time.monotonic() - t0, 1), warnings=stderr[-300:] or None)
        return {c.index for c in sidecars}
    log.warning("packager.subs.one_pass_failed", tracks=len(sidecars), code=result.returncode,
                error=stderr[-500:] or None)
    for c in sidecars:
        (subs_dir / c.name).unlink(missing_ok=True)
    return None


def _extract_alone(src: Path, subs_dir: Path, c: _Sidecar, codec: str) -> bool:
    """Write one sidecar in a run of its own, as every one was once.
    False, logged, when ffmpeg fails."""
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
             "-i", str(src), "-map", f"0:s:{c.index}", *c.options, str(subs_dir / c.name)],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        log.warning("packager.subs.failed", idx=c.index, codec=codec, error=str(e))
        return False
    return True


# The subtitle files next to a source the packager takes, by extension;
# ffmpeg then reads each by its content. A file is read whole, so one far
# larger than any subtitle file (a video by a wrong name) is refused.
_SUBTITLE_FILE_SUFFIXES = frozenset({".srt", ".vtt", ".ass", ".ssa"})
_SUBTITLE_FILE_MAX_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class _SubtitleFile:
    """A subtitle file next to the source (the item record's subtitleFiles)."""
    path: Path
    language: str  # an ISO 639-2 code, und when the record names none
    label: str     # the catalog's label: the manifest entry's `title`
    forced: bool
    # The catalog's id of the file (the record's `id`), None when it names
    # none: the library v2 handover maps it to the file's rendition.
    asset_id: str | None = None


def _subtitle_files(entries: list[Any] | None, source: Path) -> list[_SubtitleFile]:
    """The item record's subtitleFiles the packager takes: an absolute
    `path` to a .srt, .vtt, .ass or .ssa file in the source's folder or
    below it; its `language` when that is a code (_language_code), else
    und; its `label`; `forced` only when true. Any other entry is
    ignored, logged."""
    folder = Path(os.path.normpath(source.parent))
    out: list[_SubtitleFile] = []
    for entry in entries or []:
        raw = entry.get("path") if isinstance(entry, dict) else None
        path = Path(os.path.normpath(raw)) if isinstance(raw, str) and raw.strip() else None
        if (path is None or not path.is_absolute() or not path.is_relative_to(folder)
                or path.suffix.lower() not in _SUBTITLE_FILE_SUFFIXES):
            log.warning("packager.subtitle_file.ignored", entry=str(entry)[:300])
            continue
        label = entry.get("label")
        asset_id = entry.get("id")
        out.append(_SubtitleFile(
            path=path,
            language=_language_code(entry.get("language")) or "und",
            label=label.strip() if isinstance(label, str) else "",
            forced=entry.get("forced") is True,
            asset_id=asset_id if isinstance(asset_id, str) and asset_id else None,
        ))
    return out


def _convert_subtitle_files(
    files: list[_SubtitleFile], subs_dir: Path, tmpdir: Path,
    *,
    first: int,
    visible_indices: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Convert each subtitle file next to the source to WebVTT at
    subs_dir/N.vtt, N counting on from the source's own subtitle tracks
    (`first` of them), and return their manifest entries: those of an
    embedded WebVTT track, with the catalog's label for `title`, and
    `external`. They are segmented into HLS renditions like the embedded
    ones. A file that can't be read or converted is left out, logged;
    the package goes on without it."""
    if files:
        subs_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for k, f in enumerate(files):
        n = first + k
        target = subs_dir / f"{n}.vtt"
        try:
            _convert_subtitle_file(f, target, tmpdir / f"subtitle-file-{n}{f.path.suffix.lower()}")
        except (OSError, PackageError) as e:
            log.warning("packager.subs.file_failed", idx=n, path=str(f.path), error=str(e)[:300])
            target.unlink(missing_ok=True)
            continue
        log.info("packager.subs.file", idx=n, path=str(f.path), language=f.language)
        out.append({
            "id": f"sub{n}",
            "language": f.language,
            "title": f.label,
            "default": False,
            "forced": f.forced,
            "visible": visible_indices is None or n in visible_indices,
            "path": f"subs/{n}.vtt",
            "format": "webvtt",
            "external": True,
        })
    return out


def _convert_subtitle_file(f: _SubtitleFile, target: Path, utf8: Path) -> None:
    """One subtitle file as WebVTT at target: its text decoded
    (_subtitle_text) and written to `utf8` as UTF-8, then through ffmpeg's
    WebVTT encoder, as the source's own text tracks are. ffmpeg reads SRT,
    ASS/SSA and WebVTT by their content, and fails on a file that is none
    of them. Raises OSError or PackageError."""
    size = f.path.stat().st_size
    if size > _SUBTITLE_FILE_MAX_BYTES:
        raise PackageError(f"{size} bytes: no subtitle file is that large")
    utf8.write_text(_subtitle_text(f.path.read_bytes(), f.language), encoding="utf-8")
    _run_ffmpeg_capturing(f"subtitle file {f.path.name}", [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(utf8), "-map", "0:s:0", "-c:s", "webvtt", "-f", "webvtt", str(target),
    ])


# The legacy code page a subtitle file that isn't UTF-8 is most likely in,
# by its language; Windows-1252 (Western European) for the others.
_LEGACY_ENCODINGS = {
    **dict.fromkeys(("bos", "ces", "cze", "hrv", "hun", "pol", "ron", "rum", "slk", "slo",
                     "slv"), "cp1250"),
    **dict.fromkeys(("bel", "bul", "mac", "mkd", "rus", "srp", "ukr"), "cp1251"),
    **dict.fromkeys(("ell", "gre"), "cp1253"),
    "tur": "cp1254",
    "heb": "cp1255",
    **dict.fromkeys(("ara", "fas", "per", "urd"), "cp1256"),
    **dict.fromkeys(("est", "lav", "lit"), "cp1257"),
    "vie": "cp1258",
    "tha": "cp874",
    "jpn": "cp932",
    "kor": "cp949",
    **dict.fromkeys(("chi", "zho"), "gb18030"),
}

_BOMS = (
    # UTF-32 first: its little-endian mark starts with UTF-16's.
    (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
)


def _subtitle_text(raw: bytes, language: str = "und") -> str:
    """A subtitle file's text, decoded by its byte order mark (UTF-8,
    UTF-16, UTF-32), else as UTF-8, else in the legacy code page of its
    language (_LEGACY_ENCODINGS), else as Windows-1252, else as Latin-1,
    which reads any bytes. Without the mark, with \\n line ends."""
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            text = raw.decode(codec, errors="replace")
            break
    else:
        for codec in ("utf-8", _LEGACY_ENCODINGS.get(language, "cp1252"), "cp1252"):
            try:
                text = raw.decode(codec)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("latin-1")
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


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
        # Named by its language first.
        name = _audio_display_name(meta, surround=group == SURROUND_GROUP)
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
    """The NAME of a subtitle rendition: the name of its language, then
    what its title says besides ("English · SDH"), and "(forced)" for a
    forced track whose name doesn't say so ("English (forced)"; "English ·
    Forced" when its title did). A second track that reads the same is
    told apart by hls.unique_names."""
    name = _track_display_name(entry.get("language"), entry.get("title"))
    if entry.get("forced") and "forced" not in name.casefold():
        name = f"{name} (forced)"
    return name


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

    Probes all category dirs (movies/shows/music/extras/other) so the
    caller doesn't have to know item.type. Cheap — at most 5 stat calls.
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
