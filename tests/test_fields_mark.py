"""A source's field mark as its container states it (fields.py): read from
Matroska, MP4/MOV and AVI files ffmpeg makes here, recorded beside the scan
for every copy (`sourceFields`), and written into a copied rendition's init
segment as an MP4 `fiel`, which FFmpeg reads back."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from test_hdr10 import COLR, HVCC, PASP, box, entry, init

from packager import fields, hdr10
from packager import packager as pk
from packager.renditions import VideoInput

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg and ffprobe on PATH")
AVCC = box(b"avcC", bytes.fromhex("0164001fffe1000a6764001facd9405005b0"))


def _make(path: Path, order: str | None, codec: list[str], fmt: list[str] = ()) -> Path:
    vf = f"testsrc2=s=720x576:r=25:d=1{',setfield=' + order if order else ''}"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", vf, *codec,
                    *fmt, str(path)], check=True, stdin=subprocess.DEVNULL)
    return path


MPEG2_FIELDS = ["-c:v", "mpeg2video", "-flags", "+ildct+ilme", "-q:v", "3"]


@needs_ffmpeg
@pytest.mark.parametrize(("name", "order", "codec", "container", "want", "raw"), [
    ("tff.mkv", "tff", MPEG2_FIELDS, "matroska", (True, "tb"), {"FlagInterlaced": 1,
                                                                 "FieldOrder": 9}),
    ("bff.mkv", "bff", MPEG2_FIELDS, "matroska", (True, "bt"), {"FlagInterlaced": 1,
                                                                 "FieldOrder": 14}),
    ("p.mkv", None, ["-c:v", "libx264", "-preset", "ultrafast"], "matroska",
     (False, "progressive"), {"FlagInterlaced": 2}),
    ("tff.mp4", "tff", MPEG2_FIELDS, "mp4", (True, "tb"), {"fiel": "0209"}),
    ("bff.mov", "bff", MPEG2_FIELDS, "mp4", (True, "bt"), {"fiel": "020e"}),
    ("tff.avi", "tff", MPEG2_FIELDS, "avi", (True, None), {"vprpFieldsPerFrame": 2}),
])
def test_each_containers_mark_is_read_as_ffmpeg_wrote_it(
        tmp_path: Path, name: str, order: str | None, codec: list[str], container: str,
        want: tuple, raw: dict) -> None:
    mark = fields.container_mark(_make(tmp_path / name, order, codec))
    assert mark is not None
    assert (mark.container, (mark.interlaced, mark.field_order), mark.raw) == (container, want, raw)


@needs_ffmpeg
def test_a_container_without_a_mark_states_none(tmp_path: Path) -> None:
    # An MPEG-TS has no field mark of its own: what its stream says stays in
    # its packets, which a copy keeps.
    assert fields.container_mark(_make(tmp_path / "tff.ts", "tff", MPEG2_FIELDS)) is None
    progressive_mp4 = _make(tmp_path / "p.mp4", None, ["-c:v", "libx264", "-preset", "ultrafast"])
    assert fields.container_mark(progressive_mp4) == fields.ContainerMark("mp4")


def test_a_file_it_cannot_read_states_none(tmp_path: Path) -> None:
    (tmp_path / "x.mkv").write_bytes(b"\x1a\x45\xdf\xa3" + b"\xff" * 12)
    assert fields.container_mark(tmp_path / "x.mkv") is None
    assert fields.container_mark(tmp_path / "gone.mkv") is None


def test_the_fiel_box_is_ffmpegs() -> None:
    assert fields.fiel_box("tb") == bytes.fromhex("0000000a6669656c0209")
    assert fields.fiel_box("progressive") == bytes.fromhex("0000000a6669656c0100")
    assert fields.fiel_box(None) is None


@pytest.mark.parametrize("sample", [entry(b"avc1", AVCC, PASP), entry(b"hvc1", HVCC, COLR)])
def test_fiel_goes_right_after_the_decoder_configuration(sample: bytes, tmp_path: Path) -> None:
    path = tmp_path / "init.mp4"
    path.write_bytes(init(sample))
    assert fields.write_fiel(path, "bt")
    after = path.read_bytes()
    config = AVCC if b"avc1" in sample else HVCC
    at = after.index(config) + len(config)
    assert after[at:at + 10] == fields.fiel_box("bt")
    assert len(after) == len(init(sample)) + 10
    # Once: written again, nothing changes; an order not stated is no box.
    assert fields.write_fiel(path, "bt") and path.read_bytes() == after
    assert not fields.write_fiel(path, None)


@needs_ffmpeg
def test_ffmpeg_reads_the_fiel_of_a_copied_init_back(tmp_path: Path) -> None:
    # A fragmented MP4 of progressive H.264 (no fiel), its init segment given
    # the original container's top-first order: ffprobe says tb, and every
    # frame still decodes.
    frag = tmp_path / "frag.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=720x576:r=25:d=2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4",
                    str(frag)], check=True, stdin=subprocess.DEVNULL)
    data = frag.read_bytes()
    cut = data.index(b"moof") - 4
    (tmp_path / "init.mp4").write_bytes(data[:cut])
    assert fields.write_fiel(tmp_path / "init.mp4", "tb")
    joined = tmp_path / "joined.mp4"
    joined.write_bytes((tmp_path / "init.mp4").read_bytes() + data[cut:])
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=field_order", "-of", "csv=p=0", str(joined)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "tb"
    decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(joined), "-f", "null", "-"],
                            capture_output=True, text=True)
    assert decode.returncode == 0 and not decode.stderr.strip()
    assert sorted(hdr10.held_boxes((tmp_path / "init.mp4").read_bytes())) == [
        b"avcC", b"fiel", b"pasp"]


def _staged(rid: str, encoder: str | None) -> pk._StagedVideo:
    return pk._StagedVideo(rung=VideoInput(rid, Path(f"/x/{rid}.mkv"), encoder=encoder),
                           mp4=Path(f"/x/{rid}.mp4"), probe=pk._Probe(
                               container="", duration_ms=0, video={}, audio=[], subtitles=[],
                               video_index=0, bit_rate=None))


def test_only_copied_rungs_take_the_originals_order(tmp_path: Path) -> None:
    for rid in ("v0", "v1"):
        (tmp_path / rid).mkdir()
        (tmp_path / rid / "init.mp4").write_bytes(init(entry(b"avc1", AVCC)))
    videos = [_staged("v0", "copy"), _staged("v1", "libx264")]
    mark = fields.ContainerMark("matroska", True, "tb", {"FlagInterlaced": 1, "FieldOrder": 9})
    assert pk._write_fiel(videos, tmp_path, mark) == {"v0": "tb"}
    assert b"fiel" in hdr10.held_boxes((tmp_path / "v0" / "init.mp4").read_bytes())
    assert (tmp_path / "v1" / "init.mp4").read_bytes() == init(entry(b"avc1", AVCC))
    # No order stated (an AVI's vprp, an MPEG-TS): no box anywhere.
    assert pk._write_fiel(videos, tmp_path, fields.ContainerMark("avi", True, None)) == {}
    assert pk._write_fiel(videos, tmp_path, None) == {}


@needs_ffmpeg
def test_the_record_carries_the_originals_mark_and_ffprobes_order(tmp_path: Path) -> None:
    src = _make(tmp_path / "tff.mkv", "tff", MPEG2_FIELDS)
    probe = pk._ffprobe(src)
    assert pk._source_fields(src, src, probe) == {
        "containerMark": {"container": "matroska", "interlaced": True, "fieldOrder": "tb",
                          "raw": {"FlagInterlaced": 1, "FieldOrder": 9}},
        "fieldOrder": "tb"}
    ts = _make(tmp_path / "tff.ts", "tff", MPEG2_FIELDS)
    assert pk._source_fields(ts, ts, pk._ffprobe(ts)) == {"containerMark": None,
                                                           "fieldOrder": "tt"}
