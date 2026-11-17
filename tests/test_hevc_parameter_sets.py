"""An HEVC source whose decoder configuration record (the hvcC of its
Matroska CodecPrivate or MP4 sample entry) names no parameter sets — they
are in its stream only — without binaries: how the record is read and
judged; that such an MP4 is copied through Annex B (hevc_mp4toannexb), so
FFmpeg's MP4 muxer builds the hvcC from the first frame; that such a
Matroska file is read through a head with a whole record for the layouts
a test proves, and every other one, and a copy through the head that fails
or comes out off, is copied through Annex B as before. Every other source's
remux command is as it was. The real runs are in
test_hevc_parameter_sets_real.py."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from packager import packager as pk
from packager.renditions import VideoInput

# The 22 bytes before numOfArrays of a Main 10, level 5.0 record with
# four-byte NAL lengths.
HEADER = bytes.fromhex("01022000000090000000000096f000fcfdfafa00000f")
VPS = bytes.fromhex("40010c01ffff022000000300900000030000030096")
SPS = bytes.fromhex("420101022000000300900000030000030096a001e020")
PPS = bytes.fromhex("4401c172f692fc90")
SEI = bytes.fromhex("4e0105ffffff")


def record(*arrays: tuple[int, list[bytes]]) -> bytes:
    """An HEVCDecoderConfigurationRecord with the given arrays."""
    body = b"".join(
        bytes([0x80 | t]) + len(nals).to_bytes(2, "big")
        + b"".join(len(n).to_bytes(2, "big") + n for n in nals)
        for t, nals in arrays)
    return HEADER + bytes([len(arrays)]) + body


WHOLE = record((32, [VPS]), (33, [SPS]), (34, [PPS]))


@pytest.mark.parametrize(("data", "lacks"), [
    (WHOLE, False),
    (record((32, [VPS]), (33, [SPS]), (34, [PPS]), (39, [SEI])), False),
    (record((32, [VPS]), (33, [SPS]), (34, [PPS, PPS])), False),
    (HEADER + b"\x00", True),                                  # numOfArrays 0: in-band only
    (record((32, [VPS]), (33, [SPS])), True),                   # no PPS
    (record((33, [SPS]), (34, [PPS])), True),                   # no VPS
    (record((32, [VPS]), (33, []), (34, [PPS])), True),         # an empty array
    (record((32, [VPS]), (33, [b""]), (34, [PPS])), True),      # an empty NAL unit
    # Parameter sets of another layer (nuh_layer_id 1) don't count.
    (record((32, [VPS]), (33, [bytes([0x42, 0x09]) + SPS[2:]]), (34, [PPS])), True),
    (WHOLE[:-3], True),                                         # ends inside an array
    (HEADER + b"\x03", True),                                   # names arrays it hasn't
    (b"\x00\x00\x00\x01" + VPS + b"\x00\x00\x00\x01" + SPS, False),  # Annex B: no record
    (b"", False),
    (HEADER, False),                                            # too short to be one
])
def test_a_record_that_names_no_parameter_sets(data: bytes, lacks: bool) -> None:
    assert pk._hvcc_lacks_parameter_sets(data) is lacks


DUMP = ("\n00000000: 0102 2000 0000 9000 0000 0000 96f0 00fc  .. .............\n"
        "00000010: fdfa fa00 000f 00                        .......\n")


def test_ffprobes_dump_of_a_record() -> None:
    assert pk._dump_bytes(DUMP) == HEADER + b"\x00"
    assert pk._dump_bytes("") == b""
    assert pk._dump_bytes("00000000: 01zz 2000\n") == b""


def _ffprobe_answers(monkeypatch: pytest.MonkeyPatch, answer) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args, **_kw):
        calls.append(args)
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")

    monkeypatch.setattr(pk.subprocess, "run", run)
    return calls


def test_the_codec_private_as_ffprobe_dumps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _ffprobe_answers(monkeypatch, {"streams": [
        {"extradata_size": 23, "extradata": DUMP}]})
    assert pk._codec_private(Path("/m/src.mkv"), 3) == HEADER + b"\x00"
    [args] = calls
    assert args[:4] == ["ffprobe", "-v", "error", "-select_streams"] and args[4] == "3"
    assert "-show_data" in args and args[-1] == "/m/src.mkv"
    assert pk._parameter_sets_in_band_only(Path("/m/src.mkv"), 3) is True
    # An MP4 keeps its decode times: copied through Annex B, as it is.
    assert pk._hevc_input(Path("/m/src.mkv"), 3, "mov,mp4,m4a,3gp,3g2,mj2", Path("/nowhere")) == (
        "/m/src.mkv", ["-bsf:v", "hevc_mp4toannexb"])


@pytest.mark.parametrize("answer", [
    {"streams": [{"extradata_size": 24, "extradata": DUMP}]},   # a dump of another size
    {"streams": [{}]},                                           # no extradata
    {"streams": []},
    {"streams": [{"extradata_size": 23, "extradata": DUMP}] * 2},
    FileNotFoundError("ffprobe"),
    subprocess.CalledProcessError(1, "ffprobe"),
    subprocess.TimeoutExpired("ffprobe", 120),
])
def test_a_codec_private_that_cant_be_read_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, answer,
) -> None:
    _ffprobe_answers(monkeypatch, answer)
    assert pk._codec_private(Path("/m/src.mkv"), 0) is None
    assert pk._hevc_input(Path("/m/src.mkv"), 0, "matroska,webm", Path("/nowhere")) == (
        "/m/src.mkv", [])


def _probe(codec: str) -> pk._Probe:
    return pk._Probe(container="matroska,webm", duration_ms=60_000,
                     video={"codec_name": codec, "index": 0},
                     audio=[{"index": 1, "codec_name": "aac", "channels": 2,
                             "tags": {"language": "eng"}}],
                     subtitles=[], video_index=0)


def _transmux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, codec: str,
              in_band_only: bool) -> tuple[list[str], list]:
    calls: list[list[str]] = []
    asked: list = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda _label, args: calls.append(args))

    def in_band(path: Path, index: int | None) -> bool:
        asked.append((path, index))
        return in_band_only

    monkeypatch.setattr(pk, "_parameter_sets_in_band_only", in_band)
    pk._prepare_source(Path("/m/src.mkv"), _probe(codec), tmp_path)
    [args] = calls
    return args, asked


def test_a_matroska_source_with_in_band_parameter_sets_only_is_read_through_a_whole_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    url = f"concat:{tmp_path}/v0-head.mkv|subfile,,start,4242,end,0,,:/m/src.mkv"
    made: list = []
    monkeypatch.setattr(pk, "_whole_record_input",
                        lambda path, index, tmpdir, name: made.append((path, index, name)) or url)
    monkeypatch.setattr(pk, "_head_copy_off", lambda *_a: None)
    args, asked = _transmux(monkeypatch, tmp_path, "hevc", True)
    assert asked == [(Path("/m/src.mkv"), 0)] and made == [(Path("/m/src.mkv"), 0, "v0")]
    head = args[:args.index("-filter_complex")]
    # No Annex B round trip: the demuxer reads a record with the parameter sets.
    assert head[-8:] == ["-i", url, "-map", "0:0", "-c:v", "copy", "-tag:v", "hvc1"]


def test_one_whose_head_cant_be_made_is_copied_through_annex_b_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(pk, "_whole_record_input", lambda *_a: None)
    args, asked = _transmux(monkeypatch, tmp_path, "hevc", True)
    assert asked == [(Path("/m/src.mkv"), 0)]
    head = args[:args.index("-filter_complex")]
    assert head[-10:] == ["-i", "/m/src.mkv", "-map", "0:0", "-c:v", "copy",
                          "-bsf:v", "hevc_mp4toannexb", "-tag:v", "hvc1"]


def test_a_copy_through_the_head_that_fails_is_run_again_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    url = f"concat:{tmp_path}/v0-head.mkv|subfile,,start,4242,end,0,,:/m/src.mkv"
    monkeypatch.setattr(pk, "_whole_record_input", lambda *_a: url)
    monkeypatch.setattr(pk, "_parameter_sets_in_band_only", lambda _p, _i: True)
    calls: list[list[str]] = []

    def ffmpeg(_label: str, args: list[str]) -> None:
        calls.append(args)
        if url in args:
            raise pk.PackageError("concat: read failed")

    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", ffmpeg)
    pk._prepare_source(Path("/m/src.mkv"), _probe("hevc"), tmp_path)
    first, second = calls
    assert first[first.index("-i") + 1] == url and "-bsf:v" not in first
    head = second[:second.index("-filter_complex")]
    assert head[-10:] == ["-i", "/m/src.mkv", "-map", "0:0", "-c:v", "copy",
                          "-bsf:v", "hevc_mp4toannexb", "-tag:v", "hvc1"]
    assert second[second.index("-filter_complex"):] == first[first.index("-filter_complex"):]


def test_a_copy_of_the_file_itself_that_fails_is_not_run_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(pk, "_parameter_sets_in_band_only", lambda _p, _i: False)
    calls: list[list[str]] = []

    def ffmpeg(_label: str, args: list[str]) -> None:
        calls.append(args)
        raise pk.PackageError("failed")

    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", ffmpeg)
    with pytest.raises(pk.PackageError):
        pk._prepare_source(Path("/m/src.mkv"), _probe("hevc"), tmp_path)
    assert len(calls) == 1


def _vint(n: int) -> bytes:
    width = next(w for w in range(1, 9) if n < (1 << 7 * w) - 1)
    return ((1 << 7 * width) | n).to_bytes(width, "big")


def _el(eid: int, payload: bytes, width: int | None = None) -> bytes:
    raw = eid.to_bytes((eid.bit_length() + 7) // 8, "big")
    size = (((1 << 7 * width) | len(payload)).to_bytes(width, "big") if width
            else _vint(len(payload)))
    return raw + size + payload


def _matroska(tracks: list[bytes], *, crc: bool = True, cluster_first: bool = False) -> bytes:
    """The start of a Matroska file: its EBML header, then a Segment of an
    8-byte size holding a SeekHead, an Info, the Tracks (a CRC-32 first) and a
    Cluster."""
    import zlib
    body = b"".join(tracks)
    if crc:
        body = _el(0xBF, zlib.crc32(body).to_bytes(4, "little")) + body
    seek = _el(0x114D9B74, _el(0x4DBB, _el(0x53AB, bytes.fromhex("1654ae6b"))
                               + _el(0x53AC, b"\x40")))
    info = _el(0x1549A966, _el(0x2AD7B1, b"\x0f\x42\x40"))
    cluster = _el(0x1F43B675, _el(0xE7, b"\x00") + _el(0xA3, b"\x81\x00\x00\x80" + b"frame"))
    children = [seek, info, _el(0x1654AE6B, body), cluster]
    if cluster_first:
        children = [seek, info, cluster, _el(0x1654AE6B, body)]
    segment = b"".join(children)
    return _el(0x1A45DFA3, _el(0x4282, b"matroska")) + _el(0x18538067, segment, width=8)


def _entry(number: int, codec: bytes, private: bytes) -> bytes:
    return _el(0xAE, _el(0xD7, bytes([number])) + _el(0x83, b"\x01") + _el(0x86, codec)
               + _el(0x63A2, private))


WHOLE = record((32, [VPS]), (33, [SPS]), (34, [PPS]))


def _walk(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out, p = [], start
    while p < end:
        eid, _, payload, stop, _ = pk._ebml_element(data, p)
        out.append((eid, payload, stop))
        p = stop
    return out


def test_the_head_holds_a_whole_record_and_the_rest_follows_where_it_was(tmp_path: Path) -> None:
    import zlib
    src = tmp_path / "src.mkv"
    data = _matroska([_entry(1, b"V_MPEGH/ISO/HEVC", record()), _entry(2, b"A_AAC", b"\x12\x10")])
    src.write_bytes(data)
    head = tmp_path / "head.mkv"
    rest = pk._matroska_head(src, WHOLE, head)
    assert rest is not None and data[rest:rest + 4] == bytes.fromhex("1f43b675")   # the Cluster
    joined = head.read_bytes() + data[rest:]
    [(ebml, _, ebml_end)] = _walk(joined, 0, pk._ebml_element(joined, 0)[3])
    seg_id, _, seg_payload, seg_end, width = pk._ebml_element(joined, ebml_end)
    assert seg_id == 0x18538067 and width == 8 and seg_end == len(joined)
    kids = _walk(joined, seg_payload, seg_end)
    assert [k[0] for k in kids] == [0xEC, 0x1549A966, 0x1654AE6B, 0x1F43B675]
    void_total = kids[0][2] - (seg_payload)
    assert void_total == len(_el(0x114D9B74, _el(0x4DBB, _el(0x53AB, bytes.fromhex("1654ae6b"))
                                                       + _el(0x53AC, b"\x40"))))
    tracks = _walk(joined, kids[2][1], kids[2][2])
    assert tracks[0][0] == 0xBF and joined[tracks[0][1]:tracks[0][2]] == zlib.crc32(
        joined[tracks[0][2]:kids[2][2]]).to_bytes(4, "little")
    fields = {fid: joined[fp:fe] for fid, fp, fe in _walk(joined, tracks[1][1], tracks[1][2])}
    assert fields[0x63A2] == WHOLE and fields[0x86] == b"V_MPEGH/ISO/HEVC"
    audio = {fid: joined[fp:fe] for fid, fp, fe in _walk(joined, tracks[2][1], tracks[2][2])}
    assert audio[0x63A2] == b"\x12\x10"


@pytest.mark.parametrize("tracks,kw", [
    ([_entry(1, b"V_MPEGH/ISO/HEVC", WHOLE)], {}),                      # nothing lacks them
    ([_entry(1, b"V_MPEGH/ISO/HEVC", record()), _entry(2, b"V_MPEGH/ISO/HEVC", record())], {}),
    ([_entry(1, b"V_MPEGH/ISO/HEVC", record())], {"cluster_first": True}),  # Tracks after a Cluster
])
def test_no_head_where_the_file_is_not_so_laid_out(tmp_path: Path, tracks, kw) -> None:
    src = tmp_path / "src.mkv"
    src.write_bytes(_matroska(tracks, **kw))
    assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is None


def test_every_other_source_is_copied_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    args, asked = _transmux(monkeypatch, tmp_path, "hevc", False)
    assert asked == [(Path("/m/src.mkv"), 0)]
    assert "-bsf:v" not in args
    head = args[:args.index("-filter_complex")]
    assert head[-8:] == ["-i", "/m/src.mkv", "-map", "0:0", "-c:v", "copy", "-tag:v", "hvc1"]
    # H.264 is never asked about.
    args, asked = _transmux(monkeypatch, tmp_path, "h264", True)
    assert asked == [] and "-bsf:v" not in args and "-tag:v" not in args


@pytest.mark.parametrize(("in_band_only", "bsf"), [(True, True), (False, False)])
def test_a_lower_rung_is_copied_the_same_way(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, in_band_only: bool, bsf: bool,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_ffprobe", lambda _path: _probe("hevc"))
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda _label, args: calls.append(args))
    monkeypatch.setattr(pk, "_parameter_sets_in_band_only", lambda _p, _i: in_band_only)
    monkeypatch.setattr(pk, "_whole_record_input", lambda *_a: None)
    staged = pk._remux_video(VideoInput("v1", Path("/inbox/v1.mkv"), timeline="keep"),
                             tmp_path, 0.0)
    assert staged is not None
    [args] = calls
    copy = args[args.index("-c:v"):args.index("-an")]
    assert copy == ["-c:v", "copy", *(["-bsf:v", "hevc_mp4toannexb"] if bsf else []),
                    "-tag:v", "hvc1"]
    # Nothing but the video: no global metadata, no chapters.
    assert args[args.index("-an"):args.index("-movflags")] == [
        "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]


# ---- the layouts a head is made for, and every other one (main's path)

SEEK = _el(0x114D9B74, _el(0x4DBB, _el(0x53AB, bytes.fromhex("1654ae6b")) + _el(0x53AC, b"\x40")))
INFO = _el(0x1549A966, _el(0x2AD7B1, b"\x0f\x42\x40"))
INFO_100US = _el(0x1549A966, _el(0x2AD7B1, b"\x01\x86\xa0"))      # a timestamp scale of 100 us
TAGS = _el(0x1254C367, _el(0x7373, _el(0x67C8, _el(0x45A3, b"TITLE") + _el(0x4487, b"x"))))
CHAPTERS = _el(0x1043A770, _el(0x45B9, _el(0xB6, _el(0x73C4, b"\x01") + _el(0x91, b"\x00"))))
ATTACHMENTS = _el(0x1941A469, _el(0x61A7, _el(0x466E, b"a.bin") + _el(0x4660, b"application/x")
                                         + _el(0x465C, b"bytes") + _el(0x46AE, b"\x01")))
CUES = _el(0x1C53BB6B, _el(0xBB, _el(0xB3, b"\x00") + _el(0xB7, _el(0xF7, b"\x01"))))
CLUSTER = _el(0x1F43B675, _el(0xE7, b"\x00") + _el(0xA3, b"\x81\x00\x00\x80" + b"frame"))


def _void(total: int) -> bytes:
    return pk._ebml_void(total)


def _crc(body: bytes, wide: bool = False) -> bytes:
    import zlib
    return (b"\xbf\x40\x04" if wide else b"\xbf\x84") + zlib.crc32(body).to_bytes(4, "little")


def _tracks(*entries: bytes, crc: bool = True, wide: bool = False) -> bytes:
    body = b"".join(entries)
    return _el(0x1654AE6B, (_crc(body, wide) if crc else b"") + body)


HEVC = _entry(1, b"V_MPEGH/ISO/HEVC", record())
AUDIO = _entry(2, b"A_AAC", b"\x12\x10")


def _layout(before: list[bytes], tracks: bytes, after: list[bytes],
            tail: list[bytes] = ()) -> tuple[bytes, int]:
    """A Matroska file's start: the EBML header, then a Segment of an 8-byte
    size holding `before`, `tracks`, `after`, a Cluster and `tail`; and where
    its Tracks ends."""
    ebml = _el(0x1A45DFA3, _el(0x4282, b"matroska"))
    lead = b"".join(before) + tracks
    segment = lead + b"".join(after) + CLUSTER + b"".join(tail)
    data = ebml + _el(0x18538067, segment, width=8)
    return data, len(ebml) + 4 + 8 + len(lead)


FFMPEG = ([SEEK, _void(60), INFO], [TAGS])                        # FFmpeg's muxer
MKVMERGE = ([SEEK, _void(60), INFO], [_void(40), ATTACHMENTS, CHAPTERS, _void(20)])
TIGHT = ([SEEK, _void(60), INFO], [])                             # a Cluster right after Tracks


def _ids(data: bytes, start: int, end: int) -> list[int]:
    return [eid for eid, _, _ in _walk(data, start, end)]


@pytest.mark.parametrize("before,after", [FFMPEG, MKVMERGE, TIGHT],
                         ids=["ffmpeg", "mkvmerge", "cluster-after-tracks"])
def test_a_head_is_made_for_the_layouts_the_real_tests_read(
    tmp_path: Path, before, after,
) -> None:
    data, tracks_end = _layout(before, _tracks(HEVC, AUDIO), after)
    src = tmp_path / "src.mkv"
    src.write_bytes(data)
    head = tmp_path / "head.mkv"
    assert pk._matroska_head(src, WHOLE, head) == tracks_end
    joined = head.read_bytes() + data[tracks_end:]
    ebml_end = pk._ebml_element(joined, 0)[3]
    _, _, payload, end, _ = pk._ebml_element(joined, ebml_end)
    assert end == len(joined)
    # The SeekHead is a Void now; every other element where it was, the Tracks grown.
    assert _ids(joined, payload, end) == [0xEC, *(_ids(b"".join(before[1:]), 0, len(
        b"".join(before[1:])))), 0x1654AE6B, *_ids(b"".join(after), 0, len(b"".join(after))),
        0x1F43B675]


def test_the_input_is_the_head_then_the_file_from_the_end_of_its_tracks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    data, tracks_end = _layout(*FFMPEG[:1], _tracks(HEVC, AUDIO), FFMPEG[1])
    src = tmp_path / "in" / "src.mkv"
    src.parent.mkdir()
    src.write_bytes(data)
    monkeypatch.setattr(pk, "_whole_hvcc", lambda *_a: WHOLE)
    monkeypatch.setattr(pk, "_first_packets_differ", lambda *_a: None)
    url = pk._whole_record_input(src, 0, tmp_path, "v0")
    assert url == f"concat:{tmp_path}/v0-head.mkv|subfile,,start,{tracks_end},end,0,,:{src}"
    # What follows the head there is the Tags, then the Cluster: nothing lost, nothing twice.
    assert data[tracks_end:tracks_end + 4] == bytes.fromhex("1254c367")


@pytest.mark.parametrize("where", ["source", "folder"])
def test_a_path_with_the_concat_separator_gets_no_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, where: str,
) -> None:
    data, _ = _layout(*FFMPEG[:1], _tracks(HEVC), FFMPEG[1])
    src = tmp_path / ("a|b.mkv" if where == "source" else "src.mkv")
    src.write_bytes(data)
    work = tmp_path / ("w|x" if where == "folder" else "w")
    work.mkdir()
    built: list = []
    monkeypatch.setattr(pk, "_whole_hvcc", lambda *a: built.append(a) or WHOLE)
    assert pk._whole_record_input(src, 0, work, "v0") is None and built == []


@pytest.mark.parametrize("error", [OSError("gone"), ValueError("bad"), IndexError("short"),
                                   StopIteration()])
def test_an_error_of_the_head_builder_takes_mains_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error,
) -> None:
    src = tmp_path / "src.mkv"
    src.write_bytes(_layout(*FFMPEG[:1], _tracks(HEVC), FFMPEG[1])[0])
    monkeypatch.setattr(pk, "_whole_hvcc", lambda *_a: WHOLE)

    def builder(*_a):
        raise error

    monkeypatch.setattr(pk, "_matroska_head", builder)
    assert pk._whole_record_input(src, 0, tmp_path, "v0") is None


def test_zero_padding_where_an_element_starts_takes_mains_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A 0x00 where an id or a size starts is no EBML: the walk's next() finds
    no length marker (StopIteration), and the file is copied as on main."""
    data, _ = _layout([SEEK, bytes(60), INFO], _tracks(HEVC), [TAGS])
    src = tmp_path / "src.mkv"
    src.write_bytes(data)
    monkeypatch.setattr(pk, "_whole_hvcc", lambda *_a: WHOLE)
    assert pk._whole_record_input(src, 0, tmp_path, "v0") is None


@pytest.mark.parametrize("before,after,tail", [
    ([SEEK, _void(60)], [_void(40)], [INFO_100US]),   # Info only after the Clusters
    ([SEEK, _void(60)], [INFO_100US], []),            # Info after Tracks
    ([SEEK, _void(60)], [], []),                      # no Info at all
], ids=["info-after-clusters", "info-after-tracks", "no-info"])
def test_no_head_unless_an_info_comes_before_tracks(
    tmp_path: Path, before, after, tail,
) -> None:
    """The head makes the SeekHead a Void: an Info it names behind the
    Clusters, with a timestamp scale other than 1 ms, would no longer be
    read, and every time would come out wrong."""
    src = tmp_path / "src.mkv"
    src.write_bytes(_layout(before, _tracks(HEVC), after, tail)[0])
    assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is None


@pytest.mark.parametrize("before,after", [
    ([SEEK, _void(60), INFO], [SEEK]),                   # a SeekHead between Tracks and Cluster
    ([SEEK, _void(60), INFO], [_void(30), SEEK, TAGS]),
    ([SEEK, _void(60), INFO], [CUES]),                   # Cues before the Clusters
    ([SEEK, TAGS, INFO], []),                            # Tags before Tracks: no test reads it
    ([SEEK, CUES, INFO], []),
], ids=["seekhead-after", "seekhead-after-void", "cues-after", "tags-before", "cues-before"])
def test_no_head_for_elements_no_test_reads_around_tracks(tmp_path: Path, before, after) -> None:
    src = tmp_path / "src.mkv"
    src.write_bytes(_layout(before, _tracks(HEVC), after)[0])
    assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is None


def test_no_head_for_a_crc32_the_head_can_not_rewrite(tmp_path: Path) -> None:
    """Only a CRC-32 of the six-byte form, at the head of Tracks, is made true
    again; one coded wider (BF 40 04 ...), one elsewhere in Tracks and one in
    the HEVC TrackEntry would be left false."""
    entry_crc = _el(0xAE, _crc(b"x") + _el(0xD7, b"\x01") + _el(0x83, b"\x01")
                    + _el(0x86, b"V_MPEGH/ISO/HEVC") + _el(0x63A2, record()))
    for tracks in (_tracks(HEVC, wide=True),
                   _el(0x1654AE6B, HEVC + _crc(HEVC)),
                   _tracks(entry_crc)):
        src = tmp_path / "src.mkv"
        src.write_bytes(_layout(*FFMPEG[:1], tracks, FFMPEG[1])[0])
        assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is None
    # The usual form is made true (test_the_head_holds_a_whole_record...).
    src.write_bytes(_layout(*FFMPEG[:1], _tracks(HEVC), FFMPEG[1])[0])
    assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is not None


def test_no_head_for_a_track_whose_codec_private_may_be_compressed(tmp_path: Path) -> None:
    encodings = _el(0x6D80, _el(0x6240, _el(0x5034, _el(0x4254, b"\x00"))))
    entry = _el(0xAE, _el(0xD7, b"\x01") + _el(0x83, b"\x01") + _el(0x86, b"V_MPEGH/ISO/HEVC")
                + encodings + _el(0x63A2, record()))
    src = tmp_path / "src.mkv"
    src.write_bytes(_layout(*FFMPEG[:1], _tracks(entry), FFMPEG[1])[0])
    assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is None


def test_no_head_where_the_samples_frame_their_units_otherwise(tmp_path: Path) -> None:
    """The record built from the first packet says four-byte NAL lengths; a
    file whose record says two frames its samples with two, and a head with
    the built record would make every sample unreadable."""
    two = HEADER[:21] + bytes([(HEADER[21] & 0xFC) | 1]) + b"\x00"
    src = tmp_path / "src.mkv"
    src.write_bytes(_layout(*FFMPEG[:1], _tracks(_entry(1, b"V_MPEGH/ISO/HEVC", two)),
                            FFMPEG[1])[0])
    assert pk._hvcc_lacks_parameter_sets(two)
    assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is None
    assert pk._matroska_head(src, WHOLE[:21] + bytes([two[21]]) + WHOLE[22:],
                             tmp_path / "head.mkv") is not None


def test_the_element_walk_holds_a_bounded_number_of_elements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(pk, "_whole_hvcc", lambda *_a: WHOLE)
    monkeypatch.setattr(pk, "_first_packets_differ", lambda *_a: None)   # only the bound refuses
    many = pk._HEAD_ELEMENTS + 1
    level = _layout([SEEK, *[_void(2)] * many, INFO], _tracks(HEVC), [TAGS])[0]
    entry = _el(0xAE, _el(0xD7, b"\x01") + _el(0x83, b"\x01") + _el(0x86, b"V_MPEGH/ISO/HEVC")
                + _void(2) * many + _el(0x63A2, record()))
    nested = _layout(*FFMPEG[:1], _tracks(entry), FFMPEG[1])[0]
    for data in (level, nested):
        src = tmp_path / "src.mkv"
        src.write_bytes(data)
        assert pk._whole_record_input(src, 0, tmp_path, "v0") is None


# ---- the join proven before the run: the first packets through the head are the file's

def _ffprobe_packets(monkeypatch: pytest.MonkeyPatch, own: list | None, read: list | None,
                     streams: list | None = None) -> list:
    asked: list = []

    def run(args, **_kw):
        assert args[0] == "ffprobe" and "%+#56" in args
        asked.append(args[-1])
        packets = own if not args[-1].startswith("concat:") else read
        if packets is None:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, json.dumps(
            {"packets": packets, "streams": streams or []}), "")
    monkeypatch.setattr(pk.subprocess, "run", run)
    return asked


def _packet(stream: int, pts: str, size: str, md5: str) -> dict:
    return {"stream_index": stream, "pts_time": pts, "size": size, "data_hash": f"MD5:{md5}"}


FIRST = [_packet(0, "0.000000", "34770", "a1"), _packet(1, "0.000000", "371", "b1"),
         _packet(0, "0.167000", "17149", "a2"), _packet(1, "0.021000", "372", "b2")]


@pytest.mark.parametrize("read,why", [
    (FIRST, None),
    ([*FIRST[:2], _packet(0, "1.670000", "17149", "a2"), FIRST[3]], "packet 2 differs"),  # times
    ([*FIRST[:2], _packet(0, "0.167000", "17149", "zz"), FIRST[3]], "packet 2 differs"),  # bytes
    (FIRST[1:], "packet 0 differs"),                                   # the first one lost
    (FIRST[:3], "packet 3 differs"),                                   # one short
    (None, "the first packets can't be read"),
])
def test_the_join_is_proven_by_the_first_packets(
    monkeypatch: pytest.MonkeyPatch, read, why,
) -> None:
    url = "concat:/w/v0-head.mkv|subfile,,start,4242,end,0,,:/m/src.mkv"
    asked = _ffprobe_packets(monkeypatch, FIRST, read)
    got = pk._first_packets_differ("/m/src.mkv", url)
    assert asked == ["/m/src.mkv", url]
    assert (got is None) if why is None else (got is not None and got.startswith(why)), got


def test_a_cover_pictures_packet_is_left_out_of_the_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cover attached behind the Clusters is found in the file through its
    SeekHead, not through the head, which makes that a Void; no copy maps it."""
    cover = _packet(2, None, "65542", "cc")
    _ffprobe_packets(monkeypatch, [cover, *FIRST], FIRST,
                     streams=[{"index": 0}, {"index": 1},
                              {"index": 2, "disposition": {"attached_pic": 1}}])
    assert pk._first_packets_differ("/m/src.mkv", "concat:x") is None


def test_only_the_first_packets_are_compared(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [_packet(0, f"{i / 24:.6f}", "100", f"m{i}") for i in range(60)]
    later = [*many[:48], *[_packet(0, "9.0", "1", "x")] * 12]
    _ffprobe_packets(monkeypatch, many, later)
    assert pk._first_packets_differ("/m/src.mkv", "concat:x") is None
    _ffprobe_packets(monkeypatch, many, [*many[:47], _packet(0, "9.0", "1", "x"), *many[48:]])
    assert pk._first_packets_differ("/m/src.mkv", "concat:x").startswith("packet 47 differs")


def test_a_list_of_the_files_own_that_ends_early_is_a_difference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More cover pictures behind the Clusters than the read leaves room for
    leave the file's own list the shorter one: not proven, and no error."""
    _ffprobe_packets(monkeypatch, FIRST[:3], FIRST)
    assert pk._first_packets_differ("/m/src.mkv", "concat:x") == (
        f"packet 3 differs: {tuple(FIRST[3].values())} for None")


def test_the_stream_of_each_packet_is_part_of_the_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    swapped = [FIRST[0], {**FIRST[1], "stream_index": 0}, *FIRST[2:]]
    _ffprobe_packets(monkeypatch, FIRST, swapped)
    assert pk._first_packets_differ("/m/src.mkv", "concat:x").startswith("packet 1 differs")


def test_a_proof_that_fails_takes_mains_path_and_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    data, _ = _layout(*FFMPEG[:1], _tracks(HEVC, AUDIO), FFMPEG[1])
    src = tmp_path / "src.mkv"
    src.write_bytes(data)
    monkeypatch.setattr(pk, "_whole_hvcc", lambda *_a: WHOLE)

    def proof(*_a):
        raise IndexError("list index out of range")

    said: list = []

    class Log:
        def warning(self, event, **kw):
            said.append((event, kw.get("why")))

        def info(self, *_a, **_kw):
            pass

    monkeypatch.setattr(pk, "_first_packets_differ", proof)
    monkeypatch.setattr(pk, "log", Log())
    assert pk._whole_record_input(src, 0, tmp_path, "v0") is None
    assert said == [("packager.hevc.head_unproven",
                     "the proof failed: IndexError: list index out of range")]


def test_no_packets_of_the_file_itself_is_no_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    _ffprobe_packets(monkeypatch, [], [])
    assert pk._first_packets_differ("/m/src.mkv", "concat:x") == "the first packets can't be read"


def test_a_head_whose_first_packets_differ_is_not_taken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    data, tracks_end = _layout(*FFMPEG[:1], _tracks(HEVC, AUDIO), FFMPEG[1])
    src = tmp_path / "src.mkv"
    src.write_bytes(data)
    monkeypatch.setattr(pk, "_whole_hvcc", lambda *_a: WHOLE)
    proved: list = []
    monkeypatch.setattr(pk, "_first_packets_differ",
                        lambda source, url: proved.append((source, url)) or "packet 0 differs")
    assert pk._whole_record_input(src, 0, tmp_path, "v0") is None
    assert proved == [(str(src), f"concat:{tmp_path}/v0-head.mkv|subfile,,start,{tracks_end},"
                                 f"end,0,,:{src}")]


# ---- after the run: the copy's video as long as the original's

def _copy_probe(monkeypatch: pytest.MonkeyPatch, duration: str) -> None:
    def run(args, **_kw):
        assert args[0] == "ffprobe"
        return subprocess.CompletedProcess(args, 0, json.dumps(
            {"streams": [{"duration": duration}]}), "")
    monkeypatch.setattr(pk.subprocess, "run", run)


def _original(duration: str = "00:00:04.000000000", start: str | None = None,
              whole_ms: int = 60_000) -> pk._Probe:
    probe = _probe("hevc")
    probe.video["tags"] = {"DURATION": duration}
    if start is not None:
        probe.video["start_time"] = start
    return replace(probe, duration_ms=whole_ms)


@pytest.mark.parametrize("copy,original,start,kept", [
    ("4.000", "00:00:04.000000000", None, True),
    ("4.400", "00:00:04.000000000", None, True),        # within the floor of 0.5 s
    ("4.600", "00:00:04.000000000", None, False),
    ("1.375", "00:00:04.000000000", None, False),       # cut short
    ("40.000", "00:00:04.000000000", None, False),      # read with another timestamp scale
    ("2.500", "00:00:04.000000000", "1.500000", True),  # late video, a tag counted from 0
    ("4.000", "00:00:04.000000000", "1.500000", True),  # late video, the track's own length
    ("1.000", "00:00:04.000000000", "1.500000", False), # neither
    ("7207.1", "02:00:00.000000000", None, True),       # within 0.1 % of two hours (7.2 s)
    ("7207.3", "02:00:00.000000000", None, False),
    ("7210.0", "02:00:00.000000000", None, False),      # 0.14 %: what a 1 % tolerance would pass
])
def test_a_copy_through_the_head_is_kept_only_when_its_video_is_the_originals_length(
    monkeypatch: pytest.MonkeyPatch, copy, original, start, kept,
) -> None:
    _copy_probe(monkeypatch, copy)
    got = pk._head_copy_off(Path("/tmp/transmux.mp4"), _original(original, start))
    assert (got is None) == kept, got


@pytest.mark.parametrize("copy,whole_ms,kept", [
    # FFmpeg's muxer: the tag counts from 0, the file ends where the tag does
    ("7140.0", 7_200_000, True),
    ("7200.0", 7_200_000, False),    # as it is would be 60 s too long
    # mkvmerge: the tag is the track's own length, the file ends at start plus tag
    ("7200.0", 7_260_000, True),
    ("7140.0", 7_260_000, False),    # short by exactly the start
], ids=["from-0-right", "from-0-off-by-the-start", "own-length-right",
        "own-length-short-by-the-start"])
def test_the_files_own_length_tells_the_tags_readings_apart(
    monkeypatch: pytest.MonkeyPatch, copy, whole_ms, kept,
) -> None:
    """Two hours of video starting 60 s in: which reading of its DURATION tag
    can be true follows from the file's own duration."""
    _copy_probe(monkeypatch, copy)
    got = pk._head_copy_off(Path("/tmp/transmux.mp4"),
                            _original("02:00:00.000000000", "60.000000", whole_ms))
    assert (got is None) == kept, got


def test_where_other_streams_outlast_the_video_both_readings_stay() -> None:
    probe = _original("01:56:40.000000000", "60.000000", 7_300_000)   # 7000 s of tag
    assert pk._video_seconds(probe) == [7000.0, 6940.0]


def test_a_late_video_may_last_the_tag_or_the_tag_less_its_start() -> None:
    """A copy counts from its first picture; a Matroska DURATION tag may count
    from 0 or the track's own length: 4 s of tag, the video starting at
    1.5 s, is 2.5 s or 4 s of copy."""
    assert pk._video_seconds(_original("00:00:04.000000000", "1.500000")) == [4.0, 2.5]
    assert pk._video_seconds(_original("00:00:04.000000000")) == [4.0]


def test_a_copy_through_the_head_found_off_is_made_again_as_on_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    url = f"concat:{tmp_path}/v0-head.mkv|subfile,,start,4242,end,0,,:/m/src.mkv"
    monkeypatch.setattr(pk, "_whole_record_input", lambda *_a: url)
    monkeypatch.setattr(pk, "_parameter_sets_in_band_only", lambda _p, _i: True)
    checked: list = []
    monkeypatch.setattr(pk, "_head_copy_off",
                        lambda made, probe: checked.append(made) or "cut short")
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda _label, args: calls.append(args))
    pk._prepare_source(Path("/m/src.mkv"), _probe("hevc"), tmp_path)
    first, second = calls
    assert checked == [tmp_path / "transmux.mp4"]
    assert first[first.index("-i") + 1] == url
    assert second == pk._annex_b_instead(first, url, Path("/m/src.mkv"))
    assert "-bsf:v" in second and url not in second


# ---- refusals that had no case

def test_no_head_for_a_child_of_tracks_that_is_neither_a_track_nor_its_crc(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_bytes(_layout(*FFMPEG[:1], _el(0x1654AE6B, HEVC + _void(4)), FFMPEG[1])[0])
    assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is None


def test_no_head_for_a_second_tracks_before_the_cluster(tmp_path: Path) -> None:
    """The head would patch the first Tracks and leave a second one, read
    after it, as it was: the allow-list after Tracks holds no Tracks."""
    src = tmp_path / "src.mkv"
    src.write_bytes(_layout([SEEK, INFO], _tracks(HEVC), [_tracks(AUDIO), TAGS])[0])
    assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is None


def test_no_head_for_a_track_with_two_codec_privates(tmp_path: Path) -> None:
    entry = _el(0xAE, _el(0xD7, b"\x01") + _el(0x83, b"\x01") + _el(0x86, b"V_MPEGH/ISO/HEVC")
                + _el(0x63A2, record()) + _el(0x63A2, record()))
    src = tmp_path / "src.mkv"
    src.write_bytes(_layout(*FFMPEG[:1], _tracks(entry), FFMPEG[1])[0])
    assert pk._matroska_head(src, WHOLE, tmp_path / "head.mkv") is None
