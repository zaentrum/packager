"""An HEVC source whose decoder configuration record (the hvcC of its
Matroska CodecPrivate) names no parameter sets — they are in its stream
only — without binaries: how the record is read and judged, and that only
such a source's stream copy goes through Annex B (hevc_mp4toannexb), so
FFmpeg's MP4 muxer builds the hvcC from the first frame instead of
writing an empty one shaka-packager can't parse. Every other source's
remux command is as it was. The real runs are in test_package_real.py."""

from __future__ import annotations

import json
import subprocess
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
    assert pk._hevc_copy_args(Path("/m/src.mkv"), 3) == ["-bsf:v", "hevc_mp4toannexb"]


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
    assert pk._hevc_copy_args(Path("/m/src.mkv"), 0) == []


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


def test_an_hevc_source_with_in_band_parameter_sets_only_is_copied_through_annex_b(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    args, asked = _transmux(monkeypatch, tmp_path, "hevc", True)
    assert asked == [(Path("/m/src.mkv"), 0)]
    head = args[:args.index("-filter_complex")]
    assert head[-10:] == ["-i", "/m/src.mkv", "-map", "0:0", "-c:v", "copy",
                          "-bsf:v", "hevc_mp4toannexb", "-tag:v", "hvc1"]


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
    staged = pk._remux_video(VideoInput("v1", Path("/inbox/v1.mkv"), timeline="keep"),
                             tmp_path, 0.0)
    assert staged is not None
    [args] = calls
    copy = args[args.index("-c:v"):args.index("-an")]
    assert copy == ["-c:v", "copy", *(["-bsf:v", "hevc_mp4toannexb"] if bsf else []),
                    "-tag:v", "hvc1"]
