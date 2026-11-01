"""HDR10 static metadata in a package's video init segments.

An HDR10 stream's static metadata is its mastering display colour volume
(SMPTE ST 2086: the display's primaries, its white point, its luminance
range) and its content light level (the brightest pixel and the brightest
frame average, MaxCLL and MaxFALL). A file carries it in its container (a
Matroska track's MasteringMetadata, MaxCLL and MaxFALL; an MP4 sample
entry's `mdcv` and `clli` boxes, ISO/IEC 23001-8) and in the bitstream's
SEI. FFmpeg's MP4 muxer writes both boxes from a stream's side data, so the
remux shaka-packager packages from has them, but shaka-packager rewrites
every init segment's sample entry with the decoder configuration (`hvcC`)
and the colour (`colr`) alone: none of its releases writes either box. A
player that reads the metadata from the sample entry, as most do with
fMP4 in HLS, gets none, and an encoded rung's bitstream may carry no SEI
either.

So after shaka the packager writes the two boxes into the visual sample
entry of every HEVC rung in PQ, with the original's values: its stream
side data, else that of its first frames (the SEI). An encoded rung takes
the original's whatever its own bitstream carries. The boxes are FFmpeg's
(movenc.c): `clli` then `mdcv`, after `colr`, big-endian; every box around
them grows by their size, and nothing else in the file moves. An init
segment holds no media data and no chunk offsets, so nothing points past
them. A source without the metadata, an SDR or an HLG rung, gets nothing:
its init segment is shaka's, byte for byte.
"""

from __future__ import annotations

import json
import struct
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ffprobe's names of the two side data types.
MASTERING = "Mastering display metadata"
LIGHT_LEVEL = "Content light level metadata"

# The units of the mdcv box: chromaticity in 0.00002, luminance in 0.0001
# cd/m² (ISO/IEC 23001-8, SMPTE ST 2086, the HEVC SEI's).
CHROMA_UNITS = 50000
LUMA_UNITS = 10000

# The sample entries of an HEVC stream, Dolby Vision's among them (whose
# HDR10 base layer the boxes describe).
HEVC_ENTRIES = frozenset({b"hvc1", b"hev1", b"dvh1", b"dvhe"})

# The boxes on the way from the top of an init segment to its sample entry.
_PATH = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd")
# What follows a visual sample entry's box header before its own boxes
# (ISO/IEC 14496-12 VisualSampleEntry), and an stsd's (version, flags,
# entry_count).
_VISUAL_FIELDS = 78
_STSD_FIELDS = 8

# The frames whose side data is read when the container has none: the
# first access unit carries the SEI, a few more cover a stream that starts
# on another one.
_FIRST_FRAMES = 10
_PROBE_TIMEOUT = 120


@dataclass(frozen=True)
class StaticMetadata:
    """HDR10 static metadata in the units of its boxes: the mastering
    display's primaries (green, blue, red: x, y each) and white point (x, y)
    in 0.00002, its maximum and minimum luminance in 0.0001 cd/m²; the
    content light level's MaxCLL and MaxFALL in cd/m². Either may be None:
    a source can carry one without the other."""
    mastering: tuple[int, ...] | None = None
    light_level: tuple[int, int] | None = None
    # Where the values were read: "stream" (the container's) or "frames".
    found_in: str = "stream"

    def boxes(self) -> bytes:
        """The clli and mdcv boxes, in FFmpeg's order: those it has."""
        out = b""
        if self.light_level is not None:
            out += struct.pack(">I4sHH", 12, b"clli", *self.light_level)
        if self.mastering is not None:
            out += struct.pack(">I4s8HII", 32, b"mdcv", *self.mastering)
        return out


def _ratio(value: Any) -> Fraction | None:
    """ffprobe's rational ('34000/50000', '17/25', '1000/1') or number."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        q = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        return None
    return q if q >= 0 else None


def _units(q: Fraction, units: int, limit: int) -> int | None:
    """q in `units`, rounded to the nearest (half up, as av_rescale), when it
    fits the box's field."""
    n = int(q * units + Fraction(1, 2))
    return n if 0 <= n <= limit else None


def _mastering(entry: dict[str, Any]) -> tuple[int, ...] | None:
    """An mdcv's fields from ffprobe's mastering display side data: every
    primary, the white point and both luminances, or none (FFmpeg writes no
    mdcv without them)."""
    out: list[int] = []
    for key in ("green_x", "green_y", "blue_x", "blue_y", "red_x", "red_y",
                "white_point_x", "white_point_y"):
        q = _ratio(entry.get(key))
        n = _units(q, CHROMA_UNITS, 0xFFFF) if q is not None else None
        if n is None:
            return None
        out.append(n)
    for key in ("max_luminance", "min_luminance"):
        q = _ratio(entry.get(key))
        n = _units(q, LUMA_UNITS, 0xFFFFFFFF) if q is not None else None
        if n is None:
            return None
        out.append(n)
    return tuple(out)


def _light_level(entry: dict[str, Any]) -> tuple[int, int] | None:
    values = []
    for key in ("max_content", "max_average"):
        v = entry.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 0xFFFF:
            return None
        values.append(v)
    return values[0], values[1]


def from_side_data(entries: list[dict[str, Any]] | None, found_in: str = "stream",
                   ) -> StaticMetadata | None:
    """The static metadata ffprobe's side data list holds, the first of
    each kind; None when it holds neither."""
    mastering = light = None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("side_data_type") or ""
        if kind == MASTERING and mastering is None:
            mastering = _mastering(entry)
        elif kind == LIGHT_LEVEL and light is None:
            light = _light_level(entry)
    if mastering is None and light is None:
        return None
    return StaticMetadata(mastering, light, found_in)


def _probe(args: list[str]) -> dict[str, Any] | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-of", "json", *args],
                             capture_output=True, text=True, check=True,
                             stdin=subprocess.DEVNULL, timeout=_PROBE_TIMEOUT)
        data = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.warning("packager.hdr10.probe_failed", error=str(e)[:200])
        return None
    return data if isinstance(data, dict) else None


def of_original(path: Path, stream: dict[str, Any] | None = None,
                stream_index: int | None = None) -> StaticMetadata | None:
    """The static metadata of the original's video stream: its stream side
    data (`stream`, its ffprobe entry, when the caller has one), else the
    side data of its first frames. None when it has none."""
    select = str(stream_index) if stream_index is not None else "v:0"
    if stream is None:
        data = _probe(["-select_streams", select, "-show_streams", str(path)])
        streams = (data or {}).get("streams") or []
        stream = streams[0] if streams and isinstance(streams[0], dict) else {}
    found = from_side_data(stream.get("side_data_list"))
    if found is not None:
        return found
    data = _probe(["-select_streams", select, "-read_intervals", f"%+#{_FIRST_FRAMES}",
                   "-show_frames", "-show_entries", "frame=side_data_list", str(path)])
    mastering = light = None
    for frame in (data or {}).get("frames") or []:
        if not isinstance(frame, dict):
            continue
        got = from_side_data(frame.get("side_data_list"), "frames")
        if got is not None:
            mastering = mastering or got.mastering
            light = light or got.light_level
        if mastering is not None and light is not None:
            break
    if mastering is None and light is None:
        return None
    return StaticMetadata(mastering, light, "frames")


class _Malformed(ValueError):
    pass


def _boxes(buf: bytes | bytearray, start: int, end: int):
    """(offset, header size, total size, type) of each box in [start, end)."""
    pos = start
    while pos < end:
        if pos + 8 > end:
            raise _Malformed(f"a box header runs past its parent at {pos}")
        size, kind = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise _Malformed(f"a large box header runs past its parent at {pos}")
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise _Malformed(f"box {kind!r} at {pos} is {size} bytes, past its parent")
        yield pos, header, size, kind
        pos += size


def _child(buf: bytearray, start: int, end: int, kind: bytes) -> tuple[int, int, int]:
    found = [(p, h, s) for p, h, s, k in _boxes(buf, start, end) if k == kind]
    if len(found) != 1:
        raise _Malformed(f"{len(found)} {kind.decode()} boxes where one was expected")
    return found[0]


def _grow(buf: bytearray, pos: int, header: int, by: int) -> None:
    """The box at `pos` `by` bytes larger: in its 64-bit size where it has
    one; one whose size is 0 (it runs to the end of the file) stays so."""
    if header == 16:
        struct.pack_into(">Q", buf, pos + 8, struct.unpack_from(">Q", buf, pos + 8)[0] + by)
        return
    size = struct.unpack_from(">I", buf, pos)[0]
    if size == 0:
        return
    if size + by > 0xFFFFFFFF:
        raise _Malformed("a box would outgrow its 32-bit size")
    struct.pack_into(">I", buf, pos, size + by)


# The top-level boxes of media data and of what points into it by offset:
# an init segment holds none of them, and nothing moved could stay right.
_MEDIA = frozenset({b"mdat", b"moof", b"sidx", b"ssix", b"mfra"})


def insert_boxes(init: bytes, boxes: bytes) -> bytes:
    """`init`, an init segment of one video track with an HEVC sample entry,
    with every box of `boxes` its sample entry doesn't hold yet, after its
    colr (else at its end), each box around them grown by their size.
    Raises ValueError for anything else: more than one track, media data,
    fragments or an index of them, chunk offsets, another sample entry, a
    box that doesn't fit."""
    buf = bytearray(init)
    top = list(_boxes(buf, 0, len(buf)))
    if any(k in _MEDIA for _p, _h, _s, k in top):
        raise _Malformed("the file holds media data or points into it: no init segment")
    chain: list[tuple[int, int]] = []
    start, end = 0, len(buf)
    for kind in _PATH:
        pos, header, size = _child(buf, start, end, kind)
        chain.append((pos, header))
        start, end = pos + header, pos + size
        if kind == b"mdia":
            hpos, hheader, _ = _child(buf, start, end, b"hdlr")
            if bytes(buf[hpos + hheader + 8:hpos + hheader + 12]) != b"vide":
                raise _Malformed("the track is no video track")
        if kind == b"stbl":
            for p, h, _s, k in _boxes(buf, start, end):
                if k in (b"stco", b"co64") and struct.unpack_from(">I", buf, p + h + 4)[0]:
                    raise _Malformed("the track has chunk offsets: no init segment")
    if struct.unpack_from(">I", buf, start + 4)[0] != 1:
        raise _Malformed("the stsd holds more than one sample entry")
    entries = list(_boxes(buf, start + _STSD_FIELDS, end))
    if len(entries) != 1 or entries[0][3] not in HEVC_ENTRIES:
        raise _Malformed("the sample entry is no HEVC one")
    pos, header, size, _kind = entries[0]
    chain.append((pos, header))
    first = pos + header + _VISUAL_FIELDS
    children = list(_boxes(buf, first, pos + size))
    held = {k for _p, _h, _s, k in children}
    add = b"".join(bytes(boxes[p:p + s]) for p, _h, s, k in _boxes(boxes, 0, len(boxes))
                   if k not in held)
    if not add:
        return bytes(buf)
    at = next((p + s for p, _h, s, k in children if k == b"colr"), pos + size)
    buf[at:at] = add
    for p, h in chain:
        _grow(buf, p, h, len(add))
    return bytes(buf)


def held_boxes(init: bytes) -> set[bytes]:
    """The types of the boxes an init segment's sample entry holds; empty
    for one this module can't read."""
    try:
        buf = bytearray(init)
        start, end = 0, len(buf)
        for kind in _PATH:
            pos, header, size = _child(buf, start, end, kind)
            start, end = pos + header, pos + size
        [(pos, header, size, _k)] = list(_boxes(buf, start + _STSD_FIELDS, end))
        return {k for _p, _h, _s, k in _boxes(buf, pos + header + _VISUAL_FIELDS, pos + size)}
    except (ValueError, struct.error):
        return set()


def write_into(path: Path, metadata: StaticMetadata) -> bool:
    """Write the boxes of `metadata` into the init segment at `path` (in
    place, through a temporary file and a rename). True when it holds every
    one of them after the call; False, logged, for an init segment it can't
    take them (it is left as it was)."""
    boxes = metadata.boxes()
    try:
        before = path.read_bytes()
        after = insert_boxes(before, boxes)
    except (OSError, ValueError, struct.error) as e:
        log.warning("packager.hdr10.not_written", init=str(path), error=str(e)[:200])
        return False
    if after == before:
        return True
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(after)
        tmp.replace(path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        log.warning("packager.hdr10.not_written", init=str(path), error=str(e)[:200])
        return False
    return True
