"""A source's field mark as its container states it, for the package's record
and for a copied rendition's init segment.

A container states whether its video is in fields, and in which order, apart
from the stream itself: a Matroska track's FlagInterlaced and FieldOrder, an
MP4 or QuickTime sample entry's `fiel` box, an AVI's OpenDML video properties
(`vprp`: fields per frame). None of these is in the packets. A copied
rendition is the original's stream, packaged by shaka-packager into fMP4,
whose sample entries it writes anew with no `fiel`: the mark is gone from the
package unless the packager keeps it. So it is read here from the original,
recorded beside the transcoder's scan (`sourceFields`), and written into the
init segment of every copied rendition as an MP4 `fiel` box, FFmpeg's (its
mov muxer and demuxer agree on the values; it goes where FFmpeg writes it,
right after the decoder configuration), wherever the container gave a field
order. FFmpeg itself writes no `fiel` for H.264, whose bitstream may say it;
a copy takes the original container's all the same, as that is what the
original said, and readers of `fiel` (FFmpeg's among them) read it whatever
the codec. An MPEG-TS, an MPEG-PS or an elementary stream has no such mark:
whatever its stream says about fields stays in its packets, which a copy
keeps as they are.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from . import hdr10

log = structlog.get_logger(__name__)

# The most of the file read for its header: Matroska's Tracks and an AVI's
# header list lie at its start; an MP4's moov is found by its box sizes.
HEADER_BYTES = 8 << 20

# FFmpeg's field orders (AVFieldOrder: tt, bb, tb, bt) as its MP4/MOV muxer
# writes them into `fiel` and its demuxer reads them back (fields, detail).
FIEL = {"progressive": b"\x01\x00", "tt": b"\x02\x01", "bb": b"\x02\x06",
        "tb": b"\x02\x09", "bt": b"\x02\x0e"}
_FIEL_ORDER = {v: k for k, v in FIEL.items()}
# Matroska's FieldOrder (its specification's values, which FFmpeg's demuxer
# reads as the same four orders) and FlagInterlaced.
_MKV_ORDER = {0: "progressive", 1: "tt", 6: "bb", 9: "tb", 14: "bt"}
_MKV_INTERLACED = {1: True, 2: False}

# The visual sample entries a `fiel` may be written into: every one a copy
# of the packager's codecs can take.
VISUAL_ENTRIES = frozenset({b"avc1", b"avc3", b"hvc1", b"hev1", b"dvh1", b"dvhe",
                            b"av01", b"vp09", b"mp4v"})


@dataclass(frozen=True)
class ContainerMark:
    """What a container states of its video's fields: `interlaced` (True,
    False, or None for not stated), `field_order` ("progressive", "tt", "bb",
    "tb", "bt", or None for not stated), and the values as read (`raw`)."""
    container: str
    interlaced: bool | None = None
    field_order: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        return {"container": self.container, "interlaced": self.interlaced,
                "fieldOrder": self.field_order, "raw": self.raw}


def container_mark(path: Path, video_n: int = 0) -> ContainerMark | None:
    """The field mark the container of `path` states for its `video_n`-th
    video track, or None where it states none it can read (MPEG-TS, MPEG-PS,
    an elementary stream, a file that can't be read)."""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
            fh.seek(0)
            if head.startswith(b"\x1a\x45\xdf\xa3"):
                return _matroska(fh.read(HEADER_BYTES), video_n)
            if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
                return _avi(fh.read(HEADER_BYTES), video_n)
            if head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"):
                return _mp4(fh, video_n)
    except (OSError, ValueError, struct.error, IndexError) as e:
        log.warning("packager.fields.mark_unread", path=str(path), error=str(e)[:200])
    return None


def source_fields(path: Path, probe_field_order: str | None, video_n: int = 0) -> dict[str, Any]:
    """The manifest's `sourceFields`: the original's container mark (null
    where its container states none) and its video's field order as ffprobe
    gives it (container and stream together; null where it says none)."""
    mark = container_mark(path, video_n)
    return {"containerMark": mark.record() if mark else None,
            "fieldOrder": probe_field_order or None}


# ------------------------------------------------------------------ Matroska
def _vint(buf: bytes, pos: int, *, strip: bool) -> tuple[int, int]:
    """An EBML variable-size integer at `pos`: (value, length). `strip`
    drops the length marker (a size); an ID keeps it. A size of all ones is
    unknown (-1)."""
    first = buf[pos]
    length = 1
    while length <= 8 and not first & (0x80 >> (length - 1)):
        length += 1
    if length > 8:
        raise ValueError(f"no EBML integer at {pos}")
    value = first & ((0x80 >> (length - 1)) - 1) if strip else first
    for b in buf[pos + 1:pos + length]:
        value = (value << 8) | b
    if strip and value == (1 << (7 * length)) - 1:
        value = -1
    return value, length


def _elements(buf: bytes, start: int, end: int):
    """(id, data start, data end) of each EBML element in [start, end); an
    element of unknown size runs to `end`."""
    pos = start
    while pos < end:
        eid, n = _vint(buf, pos, strip=False)
        size, m = _vint(buf, pos + n, strip=True)
        data = pos + n + m
        stop = end if size < 0 else min(data + size, end)
        yield eid, data, stop
        if size < 0:
            return
        pos = data + size


def _uint(buf: bytes, start: int, end: int) -> int:
    return int.from_bytes(buf[start:end], "big") if end > start else 0


def _matroska(buf: bytes, video_n: int) -> ContainerMark | None:
    for eid, s, e in _elements(buf, 0, len(buf)):
        if eid != 0x18538067:          # Segment
            continue
        for sid, ss, se in _elements(buf, s, e):
            if sid == 0x1F43B675:      # a Cluster: the header is over
                return None
            if sid != 0x1654AE6B:      # Tracks
                continue
            n = 0
            for tid, ts, te in _elements(buf, ss, se):
                if tid != 0xAE:        # TrackEntry
                    continue
                kids = list(_elements(buf, ts, te))
                kind = next((_uint(buf, a, b) for i, a, b in kids if i == 0x83), None)
                if kind != 1:          # TrackType video
                    continue
                if n < video_n:
                    n += 1
                    continue
                video = next(((a, b) for i, a, b in kids if i == 0xE0), None)
                raw: dict[str, Any] = {}
                if video:
                    for vid, vs, ve in _elements(buf, *video):
                        if vid == 0x9A:
                            raw["FlagInterlaced"] = _uint(buf, vs, ve)
                        elif vid == 0x9D:
                            raw["FieldOrder"] = _uint(buf, vs, ve)
                interlaced = _MKV_INTERLACED.get(raw.get("FlagInterlaced", 0))
                order = _MKV_ORDER.get(raw["FieldOrder"]) if "FieldOrder" in raw else None
                if order is None and interlaced is False:
                    order = "progressive"
                return ContainerMark("matroska", interlaced, order, raw)
    return None


# ------------------------------------------------------------------ MP4, MOV
def _mp4(fh, video_n: int) -> ContainerMark | None:
    size_of = fh.seek(0, 2)
    pos = 0
    while pos + 8 <= size_of:
        fh.seek(pos)
        size, kind = struct.unpack(">I4s", fh.read(8))
        header = 8
        if size == 1:
            size = struct.unpack(">Q", fh.read(8))[0]
            header = 16
        elif size == 0:
            size = size_of - pos
        if size < header:
            return None
        if kind == b"moov":
            fh.seek(pos + header)
            return _moov(fh.read(size - header), video_n)
        pos += size
    return None


def _moov(buf: bytes, video_n: int) -> ContainerMark | None:
    n = 0
    for p, h, s, kind in hdr10._boxes(buf, 0, len(buf)):
        if kind != b"trak":
            continue
        entry = _video_entry(buf, p + h, p + s)
        if entry is None:
            continue
        if n < video_n:
            n += 1
            continue
        ep, eh, es = entry
        for cp, ch, cs, ck in hdr10._boxes(buf, ep + eh + hdr10._VISUAL_FIELDS, ep + es):
            if ck == b"fiel" and cs >= ch + 2:
                value = bytes(buf[cp + ch:cp + ch + 2])
                order = _FIEL_ORDER.get(value)
                interlaced = {1: False, 2: True}.get(value[0])
                return ContainerMark("mp4", interlaced, order, {"fiel": value.hex()})
        return ContainerMark("mp4")
    return None


def _video_entry(buf: bytes, start: int, end: int) -> tuple[int, int, int] | None:
    """The visual sample entry of a video trak's stsd: (offset, header, size)."""
    for kind in (b"mdia", b"minf", b"stbl", b"stsd"):
        found = [(p, h, s) for p, h, s, k in hdr10._boxes(buf, start, end) if k == kind]
        if not found:
            return None
        p, h, s = found[0]
        if kind == b"mdia":
            hdlr = [(q, g) for q, g, _s, k in hdr10._boxes(buf, p + h, p + s) if k == b"hdlr"]
            if not hdlr or bytes(buf[hdlr[0][0] + hdlr[0][1] + 8:hdlr[0][0] + hdlr[0][1] + 12]) \
                    != b"vide":
                return None
        start, end = p + h, p + s
    entries = list(hdr10._boxes(buf, start + hdr10._STSD_FIELDS, end))
    return (entries[0][0], entries[0][1], entries[0][2]) if entries else None


# ----------------------------------------------------------------------- AVI
def _avi(buf: bytes, video_n: int) -> ContainerMark | None:
    """An AVI's OpenDML video properties (`vprp`) in its video stream's
    list: fields per frame (2: interlaced, 1: progressive); no field order."""
    def chunks(start: int, end: int):
        pos = start
        while pos + 8 <= end:
            cid, size = struct.unpack_from("<4sI", buf, pos)
            yield cid, pos + 8, min(pos + 8 + size, end)
            pos += 8 + size + (size & 1)

    n = 0
    for cid, s, e in chunks(12, len(buf)):
        if cid != b"LIST" or buf[s:s + 4] != b"hdrl":
            continue
        for lid, ls, le in chunks(s + 4, e):
            if lid != b"LIST" or buf[ls:ls + 4] != b"strl":
                continue
            kids = list(chunks(ls + 4, le))
            strh = next(((a, b) for k, a, b in kids if k == b"strh"), None)
            if strh is None or buf[strh[0]:strh[0] + 4] != b"vids":
                continue
            if n < video_n:
                n += 1
                continue
            vprp = next(((a, b) for k, a, b in kids if k == b"vprp"), None)
            if vprp is None or vprp[1] - vprp[0] < 36:
                return ContainerMark("avi")
            fields = struct.unpack_from("<I", buf, vprp[0] + 32)[0]
            return ContainerMark("avi", {1: False, 2: True}.get(fields),
                                 "progressive" if fields == 1 else None,
                                 {"vprpFieldsPerFrame": fields})
        return None
    return None


# ------------------------------------------------------- into the init segment
def fiel_box(order: str | None) -> bytes | None:
    """The `fiel` box for a field order (FIEL), None for an order not stated."""
    value = FIEL.get(order or "")
    return struct.pack(">I4s", 10, b"fiel") + value if value else None


def write_fiel(path: Path, order: str | None) -> bool:
    """Write the `fiel` box of `order` into the visual sample entry of the
    init segment at `path` (in place, through a temporary file and a
    rename), unless it holds one. True when it holds one after the call;
    False, logged, for an order not stated or an init segment that can't
    take it (it is left as it was)."""
    box = fiel_box(order)
    if box is None:
        return False
    try:
        before = path.read_bytes()
        after = hdr10.insert_boxes(before, box, entries=VISUAL_ENTRIES, after_config=True)
    except (OSError, ValueError, struct.error) as e:
        log.warning("packager.fields.fiel_not_written", init=str(path), error=str(e)[:200])
        return False
    if after == before:
        return True
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(after)
        tmp.replace(path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        log.warning("packager.fields.fiel_not_written", init=str(path), error=str(e)[:200])
        return False
    return True
