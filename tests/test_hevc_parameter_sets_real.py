"""Real ffmpeg + shaka-packager runs of an HEVC source whose parameter
sets are in its stream only (skipped unless both are on PATH, ffmpeg is
7 or newer and has libx265).

The source is made as some web releases come: libx265 writes the VPS,
SPS and PPS into the stream at every keyframe (repeat-headers), and the
Matroska CodecPrivate is then rewritten in place as the stream's decoder
configuration record without them (numOfArrays 0). FFmpeg's MP4 muxer
writes an empty hvcC for such a stream copied as hvc1, which
shaka-packager can't parse. The packager reads such a file through a head
whose CodecPrivate is a whole record: in every layout a test here proves
(FFmpeg's, mkvmerge's, with attachments and chapters, a Cluster right after
Tracks, a CRC-32 in Tracks) the package holds every one of the original's
packets, byte for byte, each at its time. Two layouts are refused and
copied through Annex B, as before (two-byte NAL lengths, an Info behind the
Clusters). A source whose CodecPrivate holds its parameter sets is remuxed
exactly as before."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from packager import packager as pk
from packager.hls import parse_attributes


def _ffmpeg_major() -> int:
    first = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.split("\n", 1)[0]
    m = re.search(r"version n?(\d+)\.", first)
    return int(m.group(1)) if m else 99


def _has_x265() -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL).stdout
    return "libx265" in out


pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe", "packager"))
    or _ffmpeg_major() < 7 or not _has_x265(),
    reason="needs ffmpeg/ffprobe >= 7 with libx265 and shaka-packager on PATH",
)

ITEM = "c0ffee00-0000-4000-8000-0000000000c5"
FRAMES = 96  # 4 s at 24 fps


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _clip(path: Path, *x265: str) -> Path:
    """A 4 s HEVC clip with a stereo AAC track, in Matroska."""
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-c:v", "libx265", "-preset", "ultrafast",
        "-x265-params", ":".join(["log-level=error", "keyint=48", *x265]),
        "-c:a", "aac", "-metadata:s:a:0", "language=eng", str(path))
    return path


def _vint(n: int) -> bytes:
    """The shortest EBML size that holds n (all ones is reserved)."""
    width = next(w for w in range(1, 9) if n < (1 << 7 * w) - 1)
    return ((1 << 7 * width) | n).to_bytes(width, "big")


def _drop_parameter_sets(mkv: Path) -> None:
    """Rewrite the clip's HEVC CodecPrivate in place as its decoder
    configuration record with no arrays (numOfArrays 0): its parameter
    sets are then in the stream only. What the record no longer holds
    becomes an EBML Void of the same size, so nothing else moves."""
    data = bytearray(mkv.read_bytes())
    at = data.find(b"\x63\xa2")                       # CodecPrivate
    width = 9 - data[at + 2].bit_length()
    size = int.from_bytes(data[at + 2:at + 2 + width], "big") & ((1 << 7 * width) - 1)
    record = bytes(data[at + 2 + width:at + 2 + width + size])
    assert record[0] == 1 and size > 23, "not an hvcC"
    total = 2 + width + size
    head = b"\x63\xa2" + _vint(23) + record[:22] + b"\x00"
    rest = total - len(head)
    w = next(w for w in range(1, 9) if len(_vint(rest - 1 - w)) == w)
    void = b"\xec" + _vint(rest - 1 - w) + bytes(rest - 1 - w)
    data[at:at + total] = head + void
    mkv.write_bytes(bytes(data))


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("hevc")
    whole = _clip(d / "whole.mkv")
    in_band = _clip(d / "in-band.mkv", "repeat-headers=1")
    _drop_parameter_sets(in_band)
    return whole, in_band


def _in_band(path: Path) -> dict[int, int]:
    """How many VPS, SPS and PPS the stream itself carries."""
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
                          "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
                         capture_output=True, check=True).stdout
    counts = {32: 0, 33: 0, 34: 0}
    for m in re.finditer(b"\x00\x00\x01", out):
        t = (out[m.end()] >> 1) & 0x3F
        if t in counts:
            counts[t] += 1
    return counts


def _package(tmp_path: Path, monkeypatch, src: Path) -> tuple[dict, Path, list[list[str]]]:
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    real = pk._run_ffmpeg_capturing
    calls: list[list[str]] = []

    def ffmpeg(label: str, args: list[str], timeout: float | None = None) -> None:
        calls.append(args)
        return real(label, args, timeout)

    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", ffmpeg)
    manifest = pk.package_item(ITEM, str(src), "movie", title="clip",
                               options=pk.PackageOptions(surround_codec="off"))
    return manifest, tmp_path / "packages" / "movies" / ITEM[:2] / ITEM, calls


def _decoded_frames(rendition: Path) -> int:
    joined = rendition / ".joined.mp4"
    joined.write_bytes((rendition / "init.mp4").read_bytes() + b"".join(
        seg.read_bytes() for seg in sorted(rendition.glob("seg-*.m4s"))))
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                              "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
                              str(joined)], capture_output=True, text=True, check=True)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(joined),
                        "-f", "null", "-"], capture_output=True, check=True)
    finally:
        joined.unlink()
    return int(out.stdout.strip())


def test_the_sample_has_its_parameter_sets_in_band_only(clips) -> None:
    _whole, in_band = clips
    record = pk._codec_private(in_band, 0)
    assert record is not None and len(record) == 23 and record[22] == 0
    assert all(n > 0 for n in _in_band(in_band).values())
    assert pk._parameter_sets_in_band_only(in_band, 0) is True


def test_an_hevc_source_with_in_band_parameter_sets_only_packages(
    tmp_path: Path, monkeypatch, clips,
) -> None:
    _whole, in_band = clips
    manifest, root, calls = _package(tmp_path, monkeypatch, in_band)
    transmux = next(args for args in calls if any(a.endswith("transmux.mp4") for a in args))
    copy = transmux[transmux.index("-c:v"):transmux.index("-filter_complex")]
    # Read through a head whose CodecPrivate is a whole record: no Annex B round trip.
    assert copy == ["-c:v", "copy", "-tag:v", "hvc1"]
    source = transmux[transmux.index("-i") + 1]
    assert source.startswith("concat:") and source.endswith(f",,:{in_band}")
    [video] = manifest["renditions"]["video"]
    assert video["codec"].startswith("hvc1.") and (video["width"], video["height"]) == (320, 240)
    master = (root / "hls" / "master.m3u8").read_text().splitlines()
    variants = [parse_attributes(line.split(":", 1)[1]) for line in master
                if line.startswith("#EXT-X-STREAM-INF:")]
    assert variants and all(a["CODECS"].startswith("hvc1.") for a in variants)
    # The init segment's decoder configuration is whole, and the picture
    # decodes, every frame of it.
    record = pk._codec_private(root / "hls" / "v0" / "init.mp4", 0)
    assert record is not None and not pk._hvcc_lacks_parameter_sets(record)
    assert _decoded_frames(root / "hls" / "v0") == FRAMES
    assert (root / ".complete").exists()


def _presented(path: Path) -> list[float]:
    """Every picture's presentation time, in decode order, from the first
    picture shown."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=pts_time", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.split()
    times = [float(t) for t in out]
    return [t - min(times) for t in times]


def test_every_picture_is_shown_when_the_original_shows_it(
    tmp_path: Path, monkeypatch, clips,
) -> None:
    """Matroska keeps no decode times, and FFmpeg's demuxer can't derive them
    for the first pictures without the record's SPS: read as the file is, the
    copy's fourth picture came 40 ms late (at 82 ms, the original's at 42 ms),
    with no edit list. Read through a whole record, every picture of the
    package is where the original's is (Matroska rounds to the millisecond)."""
    _whole, in_band = clips
    _manifest, root, _calls = _package(tmp_path, monkeypatch, in_band)
    rendition = root / "hls" / "v0"
    joined = rendition / ".joined.mp4"
    joined.write_bytes((rendition / "init.mp4").read_bytes() + b"".join(
        seg.read_bytes() for seg in sorted(rendition.glob("seg-*.m4s"))))
    try:
        package = _presented(joined)
    finally:
        joined.unlink()
    original = _presented(in_band)
    assert len(package) == len(original) == FRAMES
    worst = max(abs(a - b) for a, b in zip(package, original, strict=True))
    assert worst <= 0.002, f"a picture {worst * 1000:.1f} ms from the original's time"


def test_an_hevc_source_with_its_parameter_sets_is_remuxed_as_before(
    tmp_path: Path, monkeypatch, clips,
) -> None:
    whole, _in_band = clips
    assert pk._parameter_sets_in_band_only(whole, 0) is False
    manifest, root, calls = _package(tmp_path, monkeypatch, whole)
    transmux = next(args for args in calls if any(a.endswith("transmux.mp4") for a in args))
    copy = transmux[transmux.index("-c:v"):transmux.index("-filter_complex")]
    assert copy == ["-c:v", "copy", "-tag:v", "hvc1"]
    assert manifest["renditions"]["video"][0]["codec"].startswith("hvc1.")
    assert _decoded_frames(root / "hls" / "v0") == FRAMES


# ---- the layouts the head reads, and two it must not

MKVTOOLNIX = all(shutil.which(b) for b in ("mkvmerge", "mkvpropedit"))
needs_mkvtoolnix = pytest.mark.skipif(not MKVTOOLNIX, reason="needs mkvmerge and mkvpropedit")


def _level_one(data: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
    """(the Segment's start, its payload's start, its elements: id, start, end)."""
    ebml_end = pk._ebml_element(data, 0)[3]
    _, seg_at, payload, end, _ = pk._ebml_element(data, ebml_end)
    end = len(data) if end < 0 else end
    out, p = [], payload
    while p < end:
        eid, start, _, stop, _ = pk._ebml_element(data, p)
        out.append((eid, start, stop))
        p = stop
    return seg_at, payload, out


def _el(eid: int, payload: bytes) -> bytes:
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big") + _vint(len(payload)) + payload


def _mkvmerge(src: Path, dst: Path, *options: str) -> Path:
    subprocess.run(["mkvmerge", "-q", "-o", str(dst), *options, str(src)], check=True)
    return dst


def _cluster_after_tracks(src: Path, dst: Path) -> Path:
    """mkvmerge's layout with what stands between Tracks and the first Cluster
    moved before Tracks, and its SeekHead a Void: the same bytes, the Clusters
    where they were, a Cluster right after Tracks."""
    data = src.read_bytes()
    _, _, els = _level_one(data)
    t = next(i for i, e in enumerate(els) if e[0] == pk._TRACKS)
    c = next(i for i, e in enumerate(els) if e[0] == pk._CLUSTER)
    assert c > t + 1, "nothing between Tracks and the first Cluster to move"
    piece = lambda e: (pk._ebml_void(e[2] - e[1]) if e[0] == pk._SEEKHEAD  # noqa: E731
                       else data[e[1]:e[2]])
    new = (data[:els[0][1]] + b"".join(piece(e) for e in els[:t]) + b"".join(
        piece(e) for e in els[t + 1:c]) + piece(els[t]) + data[els[c][1]:])
    assert len(new) == len(data)
    dst.write_bytes(new)
    return dst


def _crc_in_tracks(src: Path, dst: Path) -> Path:
    """mkvmerge's layout with a CRC-32 (the six-byte form) at the head of
    Tracks, the Void after Tracks six bytes shorter."""
    import zlib
    data = src.read_bytes()
    _, _, els = _level_one(data)
    t = next(i for i, e in enumerate(els) if e[0] == pk._TRACKS)
    tracks, void = els[t], els[t + 1]
    assert void[0] == pk._EBML_VOID
    _, _, payload, end, width = pk._ebml_element(data, tracks[1])
    body = data[payload:end]
    crc = b"\xbf\x84" + zlib.crc32(body).to_bytes(4, "little")
    new_tracks = pk._TRACKS.to_bytes(4, "big") + pk._ebml_size(len(crc) + len(body), width) \
        + crc + body
    grow = len(new_tracks) - (tracks[2] - tracks[1])
    new = (data[:tracks[1]] + new_tracks + pk._ebml_void(void[2] - void[1] - grow)
           + data[void[2]:])
    assert len(new) == len(data)
    dst.write_bytes(new)
    return dst


def _two_byte_lengths(src: Path, dst: Path) -> Path:
    """The clip with its video's NAL units framed by two-byte lengths in every
    block and its record (lengthSizeMinusOne 1); its SeekHead and Cues Voids,
    their offsets no longer true."""
    data = src.read_bytes()
    seg_at, _, els = _level_one(data)

    def relen(frame: bytes) -> bytes:
        out, p = bytearray(), 0
        while p < len(frame):
            n = int.from_bytes(frame[p:p + 4], "big")
            assert n < 65536 and p + 4 + n <= len(frame)
            out += n.to_bytes(2, "big") + frame[p + 4:p + 4 + n]
            p += 4 + n
        return bytes(out)

    def block(payload: bytes) -> bytes:
        if payload[0] & 0x7F != 1:          # not the video track
            return payload
        assert not payload[3] & 0x06, "laced"
        return payload[:4] + relen(payload[4:])

    out = bytearray()
    for eid, start, stop in els:
        if eid in (pk._SEEKHEAD, 0x1C53BB6B):
            out += pk._ebml_void(stop - start)
        elif eid == pk._CLUSTER:
            _, _, cp, ce, _ = pk._ebml_element(data, start)
            body = bytearray()
            for cid, cs, cpp, cee, _ in pk._ebml_children(data, cp, ce):
                if cid == 0xA3:
                    body += _el(0xA3, block(data[cpp:cee]))
                elif cid == 0xA0:
                    group = bytearray()
                    for gid, gs, gp, ge, _ in pk._ebml_children(data, cpp, cee):
                        group += _el(0xA1, block(data[gp:ge])) if gid == 0xA1 else data[gs:ge]
                    body += _el(0xA0, bytes(group))
                else:
                    body += data[cs:cee]
            out += _el(pk._CLUSTER, bytes(body))
        else:
            out += data[start:stop]
    new = bytearray(data[:seg_at] + _el(pk._SEGMENT, bytes(out)))
    at = new.find(b"\x63\xa2")
    width = 9 - new[at + 2].bit_length()
    assert new[at + 2 + width] == 1
    new[at + 2 + width + 21] = (new[at + 2 + width + 21] & 0xFC) | 1
    dst.write_bytes(bytes(new))
    return dst


def _info_after_clusters(src: Path, dst: Path) -> Path:
    """mkvmerge's file with a timestamp scale of 100 us, and a title too long
    for the room before Tracks: mkvpropedit writes the Info behind the
    Clusters, where only the SeekHead names it."""
    _mkvmerge(src, dst, "--timestamp-scale", "100000")
    subprocess.run(["mkvpropedit", "-q", str(dst), "--edit", "info", "--set",
                    "title=" + "x" * 4500], check=True)
    ids = [e[0] for e in _level_one(dst.read_bytes())[2]]
    assert ids.index(0x1549A966) > ids.index(pk._CLUSTER), ids
    return dst


@pytest.fixture(scope="module")
def layouts(tmp_path_factory) -> dict[str, Path]:
    """Clips of one picture stream (parameter sets in the stream at every
    keyframe), in every layout, their records then emptied of them."""
    d = tmp_path_factory.mktemp("layouts")
    rh = _clip(d / "ffmpeg.mkv", "repeat-headers=1")
    out = {"ffmpeg": rh}
    if MKVTOOLNIX:
        mm = _mkvmerge(rh, d / "mkvmerge.mkv")
        (d / "chapters.txt").write_text("CHAPTER01=00:00:00.000\nCHAPTER01NAME=One\n"
                                        "CHAPTER02=00:00:02.000\nCHAPTER02NAME=Two\n")
        (d / "attachment.bin").write_bytes(b"an attachment")
        out["mkvmerge"] = mm
        out["mkvmerge-attachments-chapters"] = _mkvmerge(
            rh, d / "attachments.mkv", "--chapters", str(d / "chapters.txt"),
            "--attachment-mime-type", "application/octet-stream",
            "--attach-file", str(d / "attachment.bin"))
        out["cluster-after-tracks"] = _cluster_after_tracks(mm, d / "tight.mkv")
        out["crc32-in-tracks"] = _crc_in_tracks(mm, d / "crc.mkv")
        out["image-subtitle"] = _image_subtitle_track(mm, d / "pgs.mkv")
        cover = _mkvmerge(rh, d / "cover.mkv")
        # too large for the room before the Clusters: mkvpropedit writes it at the end
        (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + bytes(64 << 10) + b"\xff\xd9")
        subprocess.run(["mkvpropedit", "-q", str(cover), "--attachment-name", "cover.jpg",
                        "--attachment-mime-type", "image/jpeg", "--add-attachment",
                        str(d / "cover.jpg")], check=True)
        ids = [e[0] for e in _level_one(cover.read_bytes())[2]]
        assert ids.index(pk._ATTACHMENTS) > ids.index(pk._CLUSTER), ids
        out["cover-at-end"] = cover
        out["audio-first"] = _mkvmerge(rh, d / "audio-first.mkv", "--track-order", "0:1,0:0")
        covers = _mkvmerge(rh, d / "covers.mkv")    # nine covers, more than the proof reads past
        for n in range(9):
            jpeg = d / f"cover{n}.jpg"
            jpeg.write_bytes(b"\xff\xd8\xff\xe0" + bytes([n]) * (64 << 10) + b"\xff\xd9")
            subprocess.run(["mkvpropedit", "-q", str(covers), "--attachment-name", jpeg.name,
                            "--attachment-mime-type", "image/jpeg", "--add-attachment",
                            str(jpeg)], check=True)
        out["nine-covers-at-end"] = covers
        out["video-starts-late"] = _mkvmerge(rh, d / "late.mkv", "--sync", "0:1500")
        cut = d / "cut.mkv"                 # its statistics tags still count the whole
        _ff("-i", str(mm), "-map", "0", "-c", "copy", "-t", "2", str(cut))
        out["cut-by-stream-copy"] = cut
        out["info-after-clusters"] = _info_after_clusters(rh, d / "late-info.mkv")
    out["two-byte-lengths"] = _two_byte_lengths(rh, d / "two-byte.mkv")
    for path in out.values():
        _drop_hevc_parameter_sets(path)
    return out


def _drop_hevc_parameter_sets(mkv: Path) -> None:
    """The HEVC TrackEntry's CodecPrivate rewritten in place as its record with
    no arrays, the rest a Void (_drop_parameter_sets, for any track order)."""
    data = bytearray(mkv.read_bytes())
    _, _, els = _level_one(bytes(data))
    tracks = next(e for e in els if e[0] == pk._TRACKS)
    _, _, payload, end, _ = pk._ebml_element(bytes(data), tracks[1])
    for cid, _, cp, ce, _ in pk._ebml_children(bytes(data), payload, end):
        fields = pk._ebml_children(bytes(data), cp, ce) if cid == pk._TRACK_ENTRY else []
        if any(f[0] == pk._CODEC_ID and data[f[2]:f[3]] == b"V_MPEGH/ISO/HEVC" for f in fields):
            fid, fs, fp, fe, _ = next(f for f in fields if f[0] == pk._CODEC_PRIVATE)
            new = b"\x63\xa2" + _vint(23) + bytes(data[fp:fp + 22]) + b"\x00"
            data[fs:fe] = new + pk._ebml_void(fe - fs - len(new))
            mkv.write_bytes(bytes(data))
            return
    raise AssertionError("no HEVC track")


def _image_subtitle_track(src: Path, dst: Path) -> Path:
    """mkvmerge's layout with a PGS subtitle track that holds no block (nothing
    in the probe window), its TrackEntry taken out of the Void after Tracks."""
    data = src.read_bytes()
    _, _, els = _level_one(data)
    t = next(i for i, e in enumerate(els) if e[0] == pk._TRACKS)
    tracks, void = els[t], els[t + 1]
    assert void[0] == pk._EBML_VOID
    _, _, payload, end, width = pk._ebml_element(data, tracks[1])
    pgs = _el(0xAE, _el(0xD7, b"\x03") + _el(0x73C5, b"\x33\x44\x55\x66") + _el(0x83, b"\x11")
              + _el(0x9C, b"\x00") + _el(0x86, b"S_HDMV/PGS"))
    body = data[payload:end] + pgs
    new_tracks = pk._TRACKS.to_bytes(4, "big") + pk._ebml_size(len(body), width) + body
    grow = len(new_tracks) - (tracks[2] - tracks[1])
    dst.write_bytes(data[:tracks[1]] + new_tracks + pk._ebml_void(void[2] - void[1] - grow)
                    + data[void[2]:])
    return dst


def _sample_md5s(path: Path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=data_hash", "-show_data_hash", "md5", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return out.split()


def _joined(rendition: Path) -> Path:
    joined = rendition / ".joined.mp4"
    joined.write_bytes((rendition / "init.mp4").read_bytes() + b"".join(
        seg.read_bytes() for seg in sorted(rendition.glob("seg-*.m4s"))))
    return joined


def _transmuxes(calls: list[list[str]]) -> list[list[str]]:
    return [args for args in calls if any(a.endswith("transmux.mp4") for a in args)]


@pytest.mark.parametrize("layout", [
    "ffmpeg",
    pytest.param("mkvmerge", marks=needs_mkvtoolnix),
    pytest.param("mkvmerge-attachments-chapters", marks=needs_mkvtoolnix),
    pytest.param("cluster-after-tracks", marks=needs_mkvtoolnix),
    pytest.param("crc32-in-tracks", marks=needs_mkvtoolnix),
    pytest.param("image-subtitle", marks=needs_mkvtoolnix),
    pytest.param("cover-at-end", marks=needs_mkvtoolnix),
    pytest.param("audio-first", marks=needs_mkvtoolnix),
    pytest.param("video-starts-late", marks=needs_mkvtoolnix),
    pytest.param("cut-by-stream-copy", marks=needs_mkvtoolnix),
])
def test_through_the_head_every_sample_is_the_originals_bytes_at_its_time(
    tmp_path: Path, monkeypatch, layouts, layout: str,
) -> None:
    """Each layout the head is made for: the package's video holds every one
    of the original's packets, byte for byte, in order, each shown when the
    original shows it. A join off by a byte (the rest of the file read from
    the wrong place) loses or bends packets here."""
    src = layouts[layout]
    _manifest, root, calls = _package(tmp_path, monkeypatch, src)
    [transmux] = _transmuxes(calls)
    assert transmux[transmux.index("-i") + 1].startswith("concat:") and "-bsf:v" not in transmux
    joined = _joined(root / "hls" / "v0")
    try:
        package, times = _sample_md5s(joined), _presented(joined)
    finally:
        joined.unlink()
    original = _sample_md5s(src)
    assert original and package == original
    worst = max(abs(a - b) for a, b in zip(times, _presented(src), strict=True))
    assert worst <= 0.002, f"a picture {worst * 1000:.1f} ms from the original's time"


@pytest.mark.parametrize("layout", [
    "two-byte-lengths",
    pytest.param("info-after-clusters", marks=needs_mkvtoolnix),
    pytest.param("nine-covers-at-end", marks=needs_mkvtoolnix),
])
def test_a_layout_no_test_proves_is_copied_as_on_main(
    tmp_path: Path, monkeypatch, layouts, layout: str,
) -> None:
    """Two-byte NAL lengths: the record built for the head says four, and a
    head would make every sample unreadable (exit 0). An Info only behind the
    Clusters: the head hides the SeekHead that names it, and with another
    timestamp scale every time would be wrong (exit 0). Nine cover pictures
    behind the Clusters leave the file's own first packets fewer than the
    head's: not proven. None gets a head; each is copied through Annex B, as
    on main, from the first run, and the package is made."""
    src = layouts[layout]
    _manifest, root, calls = _package(tmp_path, monkeypatch, src)
    [transmux] = _transmuxes(calls)
    assert transmux[transmux.index("-i") + 1] == str(src) and "-bsf:v" in transmux
    assert _decoded_frames(root / "hls" / "v0") == FRAMES
    joined = _joined(root / "hls" / "v0")
    try:
        times = _presented(joined)
    finally:
        joined.unlink()
    assert abs(max(times) + 1 / 24 - FRAMES / 24) < 0.25


def _shifted_head(monkeypatch, how) -> list:
    '''The head builder made to return another start for the rest of the file;
    the proof's verdicts, recorded.'''
    made, proof = pk._matroska_head, pk._first_packets_differ
    said: list = []
    monkeypatch.setattr(pk, "_matroska_head", lambda path, record, head: (
        lambda rest: None if rest is None else how(path, rest))(made(path, record, head)))
    monkeypatch.setattr(pk, "_first_packets_differ",
                        lambda source, url: (lambda v: said.append(v) or v)(proof(source, url)))
    return said


def _second_cluster(path: Path, rest: int) -> int:
    els = _level_one(path.read_bytes())[2]
    return next(e for e in els if e[0] == pk._CLUSTER)[2]


@pytest.mark.parametrize("layout,how", [
    pytest.param("cluster-after-tracks", lambda path, rest: rest + 1, id="join-one-byte-late",
                 marks=needs_mkvtoolnix),
    pytest.param("mkvmerge", _second_cluster, id="first-cluster-dropped", marks=needs_mkvtoolnix),
])
def test_a_join_the_first_packets_disprove_takes_mains_path(
    tmp_path: Path, monkeypatch, layouts, layout: str, how,
) -> None:
    '''A head whose rest starts in the wrong place: its first packets are not
    the file's, the head is not taken, and the copy is main's from the first run.'''
    said = _shifted_head(monkeypatch, how)
    src = layouts[layout]
    _manifest, root, calls = _package(tmp_path, monkeypatch, src)
    assert said and said[0] is not None
    transmuxes = _transmuxes(calls)
    assert len(transmuxes) == 1 and transmuxes[0][transmuxes[0].index("-i") + 1] == str(src)
    assert _decoded_frames(root / "hls" / "v0") == FRAMES


@needs_mkvtoolnix
def test_a_head_read_with_another_timestamp_scale_is_disproven(
    tmp_path: Path, monkeypatch, layouts,
) -> None:
    '''Were the refusal of an Info behind the Clusters taken out, the head's
    times would be read at 1 ms where the file's are at 100 us: the first
    packets' times differ, and the head is not taken.'''
    monkeypatch.setattr(pk, "_INFO", pk._EBML_VOID)     # any Void before Tracks passes for an Info
    said = _shifted_head(monkeypatch, lambda path, rest: rest)
    src = layouts["info-after-clusters"]
    _manifest, _root, calls = _package(tmp_path, monkeypatch, src)
    assert said and said[0] is not None and "differs" in said[0]
    transmuxes = _transmuxes(calls)
    assert len(transmuxes) == 1 and transmuxes[0][transmuxes[0].index("-i") + 1] == str(src)
