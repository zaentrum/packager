"""HLS master playlist assembly.

shaka-packager writes the media playlists (one per video rung, audio
track and subtitle track) plus I-frame playlists; this module writes the
MASTER from them. shaka's own master can't express what we need — its
DEFAULT=YES follows a language flag (and then lands on subtitles too),
FRAME-RATE comes out of millisecond MKV timestamps (23.810 for 23.976),
and variant order follows thread start order — so we keep only what it
knows best (the CODECS strings it read from the bitstreams, RESOLUTION,
LANGUAGE normalisation, the I-frame lines) and compute the rest:

  * BANDWIDTH / AVERAGE-BANDWIDTH per RFC 8216 §4.3.4.2 from the actual
    segment sizes: peak = the highest bit rate over any run of
    consecutive segments lasting 0.5-1.5x the target duration; variant =
    video + the largest audio rendition of its group (+ subtitles).
  * Variant order: stereo group first, top rung first — the first
    variant is what players start on and what client prefetchers warm.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# `KEY=value` / `KEY="quoted, with commas"` pairs of an attribute list.
_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attributes(attr_list: str) -> dict[str, str]:
    """`BANDWIDTH=1,CODECS="a,b"` -> {"BANDWIDTH": "1", "CODECS": "a,b"}."""
    out: dict[str, str] = {}
    for key, value in _ATTR.findall(attr_list):
        out[key] = value[1:-1] if value.startswith('"') else value
    return out


def quoted(value: str) -> str:
    """HLS quoted-string: no double quotes, CR or LF allowed inside."""
    return '"' + value.replace('"', "'").replace("\r", " ").replace("\n", " ") + '"'


# ---------------------------------------------------------------- stats
@dataclass(frozen=True)
class PlaylistStats:
    peak_bps: int
    avg_bps: int
    segments: int
    target_duration: int
    duration: float


def playlist_stats(path: Path) -> PlaylistStats:
    """Peak + average bit rate of one media playlist, from its EXTINF
    durations and the segment sizes (EXT-X-BYTERANGE length, or the
    segment file's size). The init section (EXT-X-MAP) isn't counted."""
    lines = path.read_text().splitlines()
    target = 0
    segs: list[tuple[float, int]] = []
    dur: float | None = None
    byterange: int | None = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target = int(float(line.split(":", 1)[1]))
        elif line.startswith("#EXTINF:"):
            dur = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-BYTERANGE:"):
            byterange = int(line.split(":", 1)[1].split("@", 1)[0])
        elif line and not line.startswith("#") and dur is not None:
            size = byterange if byterange is not None else _size(path.parent / line)
            segs.append((dur, size))
            dur, byterange = None, None
    total_d = sum(d for d, _ in segs)
    total_b = sum(b for _, b in segs)
    avg = round(total_b * 8 / total_d) if total_d > 0 else 0
    return PlaylistStats(
        peak_bps=_peak_bps(segs, target) or avg,
        avg_bps=avg,
        segments=len(segs),
        target_duration=target,
        duration=total_d,
    )


def _size(path: Path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _peak_bps(segs: list[tuple[float, int]], target: int) -> int:
    """RFC 8216: the largest bit rate of any contiguous set of segments
    whose total duration is between 0.5 and 1.5 times the target
    duration. Falls back to the largest single segment when no window
    qualifies (a one-segment playlist shorter than half the target)."""
    lo, hi = 0.5 * target, 1.5 * target
    best = 0.0
    for i in range(len(segs)):
        total_d, total_b = 0.0, 0
        for d, b in segs[i:]:
            total_d += d
            total_b += b
            if total_d > hi + 1e-9:
                break
            if total_d + 1e-9 >= lo and total_d > 0:
                best = max(best, total_b * 8 / total_d)
    if best == 0.0:
        best = max((b * 8 / d for d, b in segs if d > 0), default=0.0)
    return round(best)


# ------------------------------------------------------- shaka's master
@dataclass
class ShakaMaster:
    """What we keep from shaka's own master playlist."""
    video: dict[str, dict[str, str]] = field(default_factory=dict)   # URI -> STREAM-INF attrs
    media: dict[str, dict[str, str]] = field(default_factory=dict)   # URI -> EXT-X-MEDIA attrs
    group_codecs: dict[str, list[str]] = field(default_factory=dict)  # audio group -> codecs
    iframes: list[str] = field(default_factory=list)                 # raw I-FRAME attr lists
    header_comment: str = ""


def read_shaka_master(path: Path) -> ShakaMaster:
    out = ShakaMaster()
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            out.header_comment = out.header_comment or line
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            if "URI" in attrs:
                out.media[attrs["URI"]] = attrs
        elif line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            out.iframes.append(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            uri = next((u.strip() for u in lines[i + 1:] if u.strip() and not u.startswith("#")),
                       "")
            codecs = [c.strip() for c in attrs.get("CODECS", "").split(",") if c.strip()]
            if uri not in out.video:
                out.video[uri] = attrs
            group = attrs.get("AUDIO")
            if group and len(codecs) > 1 and group not in out.group_codecs:
                out.group_codecs[group] = codecs[1:]
    return out


# ------------------------------------------------------------ the model
@dataclass(frozen=True)
class VideoVariant:
    uri: str                 # "v0/playlist.m3u8"
    codec: str               # "hvc1.2.4.L150.90"
    width: int
    height: int
    frame_rate: float | None
    video_range: str | None  # SDR | PQ | HLG
    stats: PlaylistStats


@dataclass(frozen=True)
class AudioRendition:
    uri: str
    group: str
    language: str
    name: str
    default: bool
    autoselect: bool
    channels: str
    codec: str
    stats: PlaylistStats


@dataclass(frozen=True)
class SubtitleRendition:
    uri: str
    language: str
    name: str
    forced: bool
    autoselect: bool
    stats: PlaylistStats


def unique_names(names: list[str]) -> list[str]:
    """RFC 8216: NAME must be unique within a group. Second "English"
    becomes "English (2)"."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        n = seen.get(name, 0) + 1
        seen[name] = n
        out.append(name if n == 1 else f"{name} ({n})")
    return out


def build_master(
    videos: list[VideoVariant],
    audio_groups: dict[str, list[AudioRendition]],
    subtitles: list[SubtitleRendition] | None,
    iframes: list[str],
    *,
    subtitle_group: str = "subs",
    header_comment: str = "",
) -> str:
    """Serialise the master. `audio_groups` is ordered: the first group's
    variants come first. `subtitles=None` (or empty) leaves the SUBTITLES
    group out entirely."""
    out = ["#EXTM3U"]
    if header_comment:
        out.append(header_comment)
    out += ["", "#EXT-X-INDEPENDENT-SEGMENTS", ""]

    for group, renditions in audio_groups.items():
        for r in renditions:
            attrs = [
                "TYPE=AUDIO",
                f"URI={quoted(r.uri)}",
                f"GROUP-ID={quoted(group)}",
            ]
            if r.language:
                attrs.append(f"LANGUAGE={quoted(r.language)}")
            attrs += [
                f"NAME={quoted(r.name)}",
                f"DEFAULT={'YES' if r.default else 'NO'}",
            ]
            if r.autoselect or r.default:
                attrs.append("AUTOSELECT=YES")
            if r.channels:
                attrs.append(f"CHANNELS={quoted(r.channels)}")
            out.append("#EXT-X-MEDIA:" + ",".join(attrs))
    if audio_groups:
        out.append("")

    subs = subtitles or []
    for s in subs:
        attrs = [
            "TYPE=SUBTITLES",
            f"URI={quoted(s.uri)}",
            f"GROUP-ID={quoted(subtitle_group)}",
        ]
        if s.language:
            attrs.append(f"LANGUAGE={quoted(s.language)}")
        attrs += [f"NAME={quoted(s.name)}", "DEFAULT=NO"]
        if s.autoselect or s.forced:
            attrs.append("AUTOSELECT=YES")
        if s.forced:
            attrs.append("FORCED=YES")
        out.append("#EXT-X-MEDIA:" + ",".join(attrs))
    if subs:
        out.append("")

    sub_peak = max((s.stats.peak_bps for s in subs), default=0)
    sub_avg = max((s.stats.avg_bps for s in subs), default=0)
    groups: list[tuple[str | None, list[AudioRendition]]] = (
        list(audio_groups.items()) if audio_groups else [(None, [])]
    )
    for group, renditions in groups:
        audio_peak = max((r.stats.peak_bps for r in renditions), default=0)
        audio_avg = max((r.stats.avg_bps for r in renditions), default=0)
        codecs_audio: list[str] = []
        for r in renditions:
            if r.codec and r.codec not in codecs_audio:
                codecs_audio.append(r.codec)
        for v in videos:
            attrs = [
                f"BANDWIDTH={v.stats.peak_bps + audio_peak + sub_peak}",
                f"AVERAGE-BANDWIDTH={v.stats.avg_bps + audio_avg + sub_avg}",
                f"CODECS={quoted(','.join([v.codec, *codecs_audio]))}",
                f"RESOLUTION={v.width}x{v.height}",
            ]
            if v.frame_rate:
                attrs.append(f"FRAME-RATE={v.frame_rate:.3f}")
            if v.video_range:
                attrs.append(f"VIDEO-RANGE={v.video_range}")
            if group:
                attrs.append(f"AUDIO={quoted(group)}")
            if subs:
                attrs.append(f"SUBTITLES={quoted(subtitle_group)}")
            attrs.append("CLOSED-CAPTIONS=NONE")
            out.append("#EXT-X-STREAM-INF:" + ",".join(attrs))
            out.append(v.uri)
        out.append("")

    for line in iframes:
        out.append("#EXT-X-I-FRAME-STREAM-INF:" + line)
    if iframes:
        out.append("")
    return "\n".join(out)
