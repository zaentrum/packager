"""HDR10 static metadata in the init segments shaka-packager writes
(hdr10.py), without binaries: the mdcv and clli boxes are FFmpeg's, byte
for byte, from ffprobe's side data in the form a container gives it and in
the form a frame gives it; they go into the sample entry after its colr,
and every box around them grows by their size and nothing else moves; an
init segment that holds them is left as it is, and one the module can't
take — media data, chunk offsets, another track or sample entry — is left
as it was, as is one whose write fails halfway. A value is rounded as
FFmpeg rounds it. Which rungs get them — every HEVC rung in PQ, or the run
fails — and from where: the container's, and what it leaves out from the
first frames, each kind from the frame that has it; without running
ffprobe. The real runs, through the packager and back through ffprobe, are
in test_hdr10_real.py."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from packager import hdr10
from packager import packager as pk
from packager.renditions import PackageInputs, VideoInput

# The boxes FFmpeg 7.1's MP4 muxer wrote for a stream whose mastering display
# is BT.2020's primaries, D65, 0.0001-1000 cd/m², with MaxCLL 1404 and
# MaxFALL 1000 (a remux of a Matroska original, its stream side data).
CLLI = bytes.fromhex("0000000c636c6c69057c03e8")
MDCV = bytes.fromhex("000000206d646376" "33c286c41d4c0bb884d03e803d134042" "0098968000000001")

# ffprobe's side data of that stream: as a frame's SEI and an MP4 give it,
# and as a Matroska track's colour gives it (its rationals reduced).
FRAME_FORM = [
    {"side_data_type": "Mastering display metadata",
     "red_x": "34000/50000", "red_y": "16000/50000", "green_x": "13250/50000",
     "green_y": "34500/50000", "blue_x": "7500/50000", "blue_y": "3000/50000",
     "white_point_x": "15635/50000", "white_point_y": "16450/50000",
     "min_luminance": "1/10000", "max_luminance": "10000000/10000"},
    {"side_data_type": "Content light level metadata", "max_content": 1404, "max_average": 1000},
]
MATROSKA_FORM = [
    {"side_data_type": "Content light level metadata", "max_content": 1404, "max_average": 1000},
    {"side_data_type": "Mastering display metadata",
     "red_x": "17/25", "red_y": "8/25", "green_x": "53/200", "green_y": "69/100",
     "blue_x": "3/20", "blue_y": "3/50", "white_point_x": "3127/10000",
     "white_point_y": "329/1000", "min_luminance": "1/10000", "max_luminance": "1000/1"},
]


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def large(kind: bytes, payload: bytes) -> bytes:
    """A box with a 64-bit size."""
    return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload


def full(kind: bytes, payload: bytes = b"") -> bytes:
    return box(kind, b"\0\0\0\0" + payload)


def entry(kind: bytes = b"hvc1", *children: bytes) -> bytes:
    """A visual sample entry, 1920x1080, with its own boxes."""
    fields = bytes(6) + struct.pack(">H", 1) + bytes(16) + struct.pack(">HH", 1920, 1080) \
        + struct.pack(">II", 0x480000, 0x480000) + bytes(4) + struct.pack(">H", 1) + bytes(32) \
        + struct.pack(">Hh", 24, -1)
    assert len(fields) == 78
    return box(kind, fields + b"".join(children))


HVCC = box(b"hvcC", bytes.fromhex("01022000000090000000000096f000fcfdfafa00000f00"))
COLR = box(b"colr", b"nclx" + bytes.fromhex("00090010000900"))
PASP = box(b"pasp", struct.pack(">II", 1, 1))


def init(sample_entry: bytes | None = None, *, handler: bytes = b"vide", stco: int = 0,
         entries: int = 1, moov=box, extra: bytes = b"") -> bytes:
    """An init segment as shaka-packager writes one: ftyp, then a moov of
    one track with one sample entry and no chunk offsets, and its mvex."""
    sample_entry = sample_entry if sample_entry is not None else entry(b"hvc1", HVCC, COLR)
    stsd = full(b"stsd", struct.pack(">I", entries) + sample_entry * entries)
    stbl = box(b"stbl", stsd + full(b"stts", bytes(4)) + full(b"stsc", bytes(4))
               + full(b"stsz", bytes(8)) + full(b"stco", struct.pack(">I", stco) + bytes(4 * stco)))
    minf = box(b"minf", full(b"vmhd", bytes(8)) + stbl)
    mdia = box(b"mdia", full(b"mdhd", bytes(20)) + full(b"hdlr", bytes(4) + handler + bytes(13))
               + minf)
    trak = box(b"trak", full(b"tkhd", bytes(80)) + mdia)
    return box(b"ftyp", b"isom" + bytes(4)) + moov(b"moov", full(b"mvhd", bytes(96)) + trak
                                                   + box(b"mvex", full(b"trex", bytes(20)))) + extra


METADATA = hdr10.StaticMetadata(
    mastering=(13250, 34500, 7500, 3000, 34000, 16000, 15635, 16450, 10_000_000, 1),
    light_level=(1404, 1000))


def test_the_boxes_are_ffmpegs() -> None:
    assert METADATA.boxes() == CLLI + MDCV
    assert hdr10.StaticMetadata(light_level=(1404, 1000)).boxes() == CLLI
    assert hdr10.StaticMetadata(mastering=METADATA.mastering).boxes() == MDCV


@pytest.mark.parametrize("side_data", [FRAME_FORM, MATROSKA_FORM])
def test_side_data_in_either_form(side_data: list[dict]) -> None:
    got = hdr10.from_side_data(side_data)
    assert got is not None
    assert (got.mastering, got.light_level, got.boxes()) == (
        METADATA.mastering, METADATA.light_level, CLLI + MDCV)


def test_what_side_data_does_not_say_is_not_written() -> None:
    # A light level alone, a mastering display without its luminance (FFmpeg
    # writes no mdcv then), a value no box field can hold, other side data.
    light = hdr10.from_side_data([FRAME_FORM[1]])
    assert light is not None and light.boxes() == CLLI
    no_luminance = {k: v for k, v in FRAME_FORM[0].items() if "luminance" not in k}
    assert hdr10.from_side_data([no_luminance]) is None
    assert hdr10.from_side_data([{**FRAME_FORM[0], "red_x": "2/1"}]) is None
    assert hdr10.from_side_data([{**FRAME_FORM[1], "max_content": 70000}]) is None
    assert hdr10.from_side_data([{"side_data_type": "DOVI configuration record"}]) is None
    assert hdr10.from_side_data(None) is None
    # The first of each kind counts.
    second = {**FRAME_FORM[1], "max_content": 4000}
    assert hdr10.from_side_data([*FRAME_FORM, second]).light_level == (1404, 1000)


def test_a_value_is_rounded_as_ffmpeg_rounds_it() -> None:
    # A Matroska colour value is a 32-bit float: D65's 0.329 is
    # 689963/2097152, a hair under 16450/50000. FFmpeg's av_rescale rounds
    # it to the nearest, 16450, which the box then says; cut off, it would
    # be 16449. Half of a unit goes up.
    d65 = hdr10.from_side_data([{**MATROSKA_FORM[1], "white_point_y": "689963/2097152"}])
    assert d65 is not None and d65.mastering is not None
    assert d65.mastering[7] == 16450 and d65.boxes() == MDCV
    half = hdr10.from_side_data([{**FRAME_FORM[0], "red_x": "1/100000"}])
    assert half is not None and half.mastering is not None and half.mastering[4] == 1


def children(segment: bytes) -> list[bytes]:
    """The types of the boxes in the sample entry of an init segment."""
    return sorted(hdr10.held_boxes(segment))


def sizes(segment: bytes) -> dict[bytes, int]:
    """The size of every box on the way to the sample entry, and its own."""
    out: dict[bytes, int] = {}
    start, end = 0, len(segment)
    for kind in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"):
        for pos, header, size, k in hdr10._boxes(segment, start, end):
            if k == kind:
                out[kind] = size
                start, end = pos + header, pos + size
                break
    [(pos, header, size, k)] = list(hdr10._boxes(segment, start + 8, end))
    out[k] = size
    return out


def test_the_boxes_go_after_colr_and_every_box_around_them_grows() -> None:
    before = init(entry(b"hvc1", HVCC, COLR, PASP))
    after = hdr10.insert_boxes(before, CLLI + MDCV)
    assert len(after) == len(before) + 44
    assert {k: v + 44 for k, v in sizes(before).items()} == sizes(after)
    # FFmpeg's order: hvcC, colr, clli, mdcv, pasp. Nothing else moved: the
    # bytes before them differ in the seven sizes alone.
    at = before.index(COLR) + len(COLR)
    assert after[at:at + 44] == CLLI + MDCV
    assert after[at + 44:] == before[at:]
    starts = [before.index(k) - 4 for k in (b"moov", b"trak", b"mdia", b"minf", b"stbl",
                                             b"stsd", b"hvc1")]
    differ = [i for i in range(at) if after[i] != before[i]]
    assert differ and all(any(s <= i < s + 4 for s in starts) for i in differ)
    assert children(after) == sorted([b"hvcC", b"colr", b"clli", b"mdcv", b"pasp"])


def test_without_colr_the_boxes_end_the_sample_entry() -> None:
    before = init(entry(b"hvc1", HVCC))
    after = hdr10.insert_boxes(before, CLLI + MDCV)
    end = before.index(HVCC) + len(HVCC)
    assert after[end:end + 44] == CLLI + MDCV and after[end + 44:] == before[end:]


@pytest.mark.parametrize("kind", [b"hvc1", b"hev1", b"dvh1", b"dvhe"])
def test_every_hevc_sample_entry_takes_them(kind: bytes) -> None:
    after = hdr10.insert_boxes(init(entry(kind, HVCC, COLR)), CLLI + MDCV)
    assert children(after) == sorted([b"hvcC", b"colr", b"clli", b"mdcv"])


def test_a_large_box_grows_in_its_large_size() -> None:
    before = init(moov=large)
    after = hdr10.insert_boxes(before, CLLI + MDCV)
    moov = before.index(b"moov") - 4
    assert struct.unpack_from(">Q", after, moov + 8)[0] == \
        struct.unpack_from(">Q", before, moov + 8)[0] + 44
    assert struct.unpack_from(">I", after, moov)[0] == 1
    assert children(after) == sorted([b"hvcC", b"colr", b"clli", b"mdcv"])


def test_a_box_that_runs_to_the_end_of_the_file_stays_so() -> None:
    # A last box of size 0 runs to the end of the file, however long it is.
    def to_the_end(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 0, kind) + payload

    before = init(moov=to_the_end)
    after = hdr10.insert_boxes(before, CLLI + MDCV)
    moov = before.index(b"moov") - 4
    assert struct.unpack_from(">I", after, moov)[0] == 0
    assert len(after) == len(before) + 44
    assert children(after) == sorted([b"hvcC", b"colr", b"clli", b"mdcv"])


def test_a_sample_entry_that_holds_them_is_left_as_it_is() -> None:
    once = hdr10.insert_boxes(init(), CLLI + MDCV)
    assert hdr10.insert_boxes(once, CLLI + MDCV) == once
    # One of them there already: the other goes in, after colr.
    half = init(entry(b"hvc1", HVCC, COLR, CLLI))
    both = hdr10.insert_boxes(half, CLLI + MDCV)
    assert len(both) == len(half) + 32
    assert children(both) == sorted([b"hvcC", b"colr", b"clli", b"mdcv"])


@pytest.mark.parametrize(("segment", "why"), [
    (init(extra=box(b"mdat", b"\0" * 16)), "media data"),
    (init(extra=box(b"moof", full(b"mfhd", bytes(4)))), "points into it"),
    (init(extra=full(b"sidx", bytes(24))), "points into it"),
    (init(stco=1), "chunk offsets"),
    (init(entry(b"avc1", HVCC)), "no HEVC"),
    (init(handler=b"soun"), "no video"),
    (init(entries=2), "more than one sample entry"),
    (init()[:-10], "past its parent"),
])
def test_what_it_cannot_take_is_refused(segment: bytes, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        hdr10.insert_boxes(segment, CLLI + MDCV)


def test_writing_into_a_file(tmp_path: Path) -> None:
    good = tmp_path / "init.mp4"
    good.write_bytes(init())
    assert hdr10.write_into(good, METADATA)
    assert children(good.read_bytes()) == sorted([b"hvcC", b"colr", b"clli", b"mdcv"])
    assert hdr10.write_into(good, METADATA)  # holds them: True, unchanged
    bad = tmp_path / "audio-init.mp4"
    bad.write_bytes(init(handler=b"soun"))
    before = bad.read_bytes()
    assert not hdr10.write_into(bad, METADATA)
    assert bad.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_a_write_that_fails_halfway_leaves_the_init_segment_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The boxes go into a file beside it, which replaces it once whole: a
    # disk that fills up halfway costs the boxes, never the init segment.
    segment = tmp_path / "init.mp4"
    segment.write_bytes(init())
    write = Path.write_bytes

    def halfway(path: Path, data: bytes) -> int:
        write(path, data[:len(data) // 2])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", halfway)
    assert not hdr10.write_into(segment, METADATA)
    assert segment.read_bytes() == init()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["init.mp4"]


# ---- which rungs, and from where

def _stream(transfer: str | None, codec: str = "hevc", side_data: list | None = None) -> dict:
    return {"index": 0, "codec_type": "video", "codec_name": codec,
            **({"color_transfer": transfer} if transfer else {}),
            **({"side_data_list": side_data} if side_data else {})}


def _probe(stream: dict) -> pk._Probe:
    return pk._Probe(container="matroska,webm", duration_ms=1000, video=stream, audio=[],
                     subtitles=[], video_index=0)


def _staged(rung: str, stream: dict) -> pk._StagedVideo:
    return pk._StagedVideo(VideoInput(rung, Path(f"/w/{rung}.mkv")), Path(f"/t/{rung}.mp4"),
                           _probe(stream))


def test_every_pq_rung_takes_them_or_the_run_fails(tmp_path: Path) -> None:
    hls = tmp_path / "hls"
    videos = [_staged("v0", _stream("smpte2084")), _staged("v1", _stream("smpte2084")),
              _staged("v2", _stream("bt709"))]

    def fresh(broken: str | None = None) -> None:
        for rung in ("v0", "v1", "v2"):
            (hls / rung).mkdir(parents=True, exist_ok=True)
            (hls / rung / "init.mp4").write_bytes(init(handler=b"soun") if rung == broken
                                                  else init())

    fresh()
    assert pk._write_hdr10(videos, hls, METADATA) == {"v0", "v1"}
    assert children((hls / "v0" / "init.mp4").read_bytes()) == \
        children((hls / "v1" / "init.mp4").read_bytes()) == \
        sorted([b"hvcC", b"colr", b"clli", b"mdcv"])
    assert (hls / "v2" / "init.mp4").read_bytes() == init()
    # One that can't take them, whichever it is, fails the run: a package
    # never says its rungs carry the metadata while one of them lacks it.
    for broken in ("v0", "v1"):
        fresh(broken)
        with pytest.raises(pk.PackageError, match=f"init segment of {broken} can't take"):
            pk._write_hdr10(videos, hls, METADATA)
    # Without metadata, nothing is written and none says it.
    fresh()
    assert pk._write_hdr10(videos, hls, None) == set()
    assert all((hls / rung / "init.mp4").read_bytes() == init() for rung in ("v0", "v1", "v2"))


def test_only_an_hevc_rung_in_pq_takes_them() -> None:
    assert pk._carries_hdr10(_stream("smpte2084"))
    assert not pk._carries_hdr10(_stream("arib-std-b67"))       # HLG
    assert not pk._carries_hdr10(_stream("bt709"))               # SDR, a tone-mapped rung
    assert not pk._carries_hdr10(_stream(None))
    assert not pk._carries_hdr10(_stream("smpte2084", "h264"))


def test_a_copy_is_read_as_probed_and_nothing_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hdr10, "_probe", lambda args: pytest.fail(f"ffprobe ran: {args}"))
    monkeypatch.setattr(pk, "_ffprobe", lambda path: pytest.fail(f"ffprobe ran: {path}"))
    stream = _stream("smpte2084", side_data=MATROSKA_FORM)
    inputs = PackageInputs(video=[VideoInput("v0", Path("/m/original.mkv"))], kind="original")
    got = pk._hdr10_metadata(inputs, _probe(stream), Path("/m/original.mkv"),
                             [_staged("v0", stream)])
    assert got is not None and got.boxes() == CLLI + MDCV and got.found_in == "stream"
    # An SDR or HLG source: nothing to read, nothing read.
    for transfer in ("bt709", "arib-std-b67"):
        sdr = _stream(transfer, side_data=MATROSKA_FORM)
        assert pk._hdr10_metadata(inputs, _probe(sdr), Path("/m/original.mkv"),
                                  [_staged("v0", sdr)]) is None


def test_an_encode_takes_the_originals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = tmp_path / "original.mkv"
    original.write_bytes(b"x")
    probed: list[Path] = []

    def ffprobe(path: Path) -> pk._Probe:
        probed.append(path)
        return _probe(_stream("smpte2084", side_data=FRAME_FORM))

    monkeypatch.setattr(pk, "_ffprobe", ffprobe)
    monkeypatch.setattr(hdr10, "_probe", lambda args: {"frames": []})
    encode = _stream("smpte2084")  # its own bitstream and container carry nothing
    inputs = PackageInputs(video=[VideoInput("v0", tmp_path / "prepared.mkv",
                                             encoder="hevc_nvenc")], kind="contract")
    got = pk._hdr10_metadata(inputs, _probe(encode), original, [_staged("v0", encode)])
    assert probed == [original]
    assert got is not None and got.boxes() == CLLI + MDCV
    # Without the original there, the encode's own: none.
    assert pk._hdr10_metadata(inputs, _probe(encode), tmp_path / "gone.mkv",
                              [_staged("v0", encode)]) is None
    # A rung in PQ is enough, v0 in SDR or not; none at all, nothing is read.
    sdr = _stream("bt709")
    assert pk._hdr10_metadata(inputs, _probe(sdr), original,
                              [_staged("v0", sdr), _staged("v1", encode)]) is not None
    probed.clear()
    assert pk._hdr10_metadata(inputs, _probe(sdr), original, [_staged("v0", sdr)]) is None
    assert probed == []


def test_a_copy_the_transcoder_remuxed_takes_the_originals_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The transcoder's v0 is the original's own stream, remuxed: its file is
    # not the original, whose container is read all the same.
    original = tmp_path / "original.mkv"
    original.write_bytes(b"x")
    monkeypatch.setattr(pk, "_ffprobe", lambda path: _probe(
        _stream("smpte2084", side_data=MATROSKA_FORM)))
    monkeypatch.setattr(hdr10, "_probe", lambda args: pytest.fail(f"ffprobe ran: {args}"))
    copy = _stream("smpte2084")
    inputs = PackageInputs(video=[VideoInput("v0", tmp_path / "prepared.mkv", encoder="copy")],
                           kind="contract")
    got = pk._hdr10_metadata(inputs, _probe(copy), original, [_staged("v0", copy)])
    assert got is not None and got.boxes() == CLLI + MDCV and got.found_in == "stream"


def test_without_stream_side_data_the_first_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def probe(args: list[str]) -> dict:
        calls.append(args)
        return {"frames": [{"side_data_list": []},
                           {"side_data_list": [{"side_data_type": "H.26[45] User Data "
                                                "Unregistered SEI message"}, *FRAME_FORM]}]}

    monkeypatch.setattr(hdr10, "_probe", probe)
    got = hdr10.of_original(Path("/m/original.mkv"), _stream("smpte2084"), 0)
    assert got is not None and got.boxes() == CLLI + MDCV and got.found_in == "frames"
    [args] = calls
    assert args[:4] == ["-select_streams", "0", "-read_intervals", "%+#10"]
    assert "frame=side_data_list" in args


def test_the_first_frames_give_each_kind_from_the_frame_that_has_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hdr10, "_probe", lambda args: {"frames": [
        {"side_data_list": [FRAME_FORM[0]]}, {"side_data_list": []},
        {"side_data_list": [FRAME_FORM[1]]}]})
    got = hdr10.of_original(Path("/m/original.mkv"), _stream("smpte2084"), 0)
    assert got is not None and got.boxes() == CLLI + MDCV and got.found_in == "frames"


@pytest.mark.parametrize(("container", "frames"), [
    ([MATROSKA_FORM[1]], [FRAME_FORM[1]]),   # the mastering display; MaxCLL in the SEI alone
    ([MATROSKA_FORM[0]], FRAME_FORM),        # the content light level; the rest in the SEI
])
def test_what_the_container_leaves_out_comes_from_the_first_frames(
    monkeypatch: pytest.MonkeyPatch, container: list[dict], frames: list[dict],
) -> None:
    calls: list[list[str]] = []

    def probe(args: list[str]) -> dict:
        calls.append(args)
        return {"frames": [{"side_data_list": frames}]}

    monkeypatch.setattr(hdr10, "_probe", probe)
    got = hdr10.of_original(Path("/m/original.mkv"), _stream("smpte2084", side_data=container), 0)
    assert got is not None and got.boxes() == CLLI + MDCV and got.found_in == "stream+frames"
    assert len(calls) == 1
    # What the frames don't have either stays out: the container's alone.
    monkeypatch.setattr(hdr10, "_probe", lambda args: {"frames": []})
    alone = hdr10.of_original(Path("/m/original.mkv"), _stream("smpte2084", side_data=container), 0)
    assert alone is not None and alone.found_in == "stream"
    assert alone.boxes() in (CLLI, MDCV)
