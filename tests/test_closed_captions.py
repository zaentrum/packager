"""A package's record says it carries closed captions when its top video
rendition's own stream does, as the packager read them from the
rendition's first frames — no longer never, whatever it carried. The
manifest is made as the packager makes it, the record through
records.package_record, as package.json is written; ffprobe's answers are
made up, and nothing runs ffmpeg."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from packager import libv2_records as rec
from packager import packager as pk
from packager import records

NOW = "2026-11-16T10:00:00Z"
SOURCE = "0b6c3d2e-3333-4a2b-8c3d-4e5f607182cc"
ORIGINAL = "Example Film (2024).mkv"
CAPTIONS = "ATSC A53 Part 4 Closed Captions"


def _raw(closed_captions: int) -> dict:
    """The original's probe: a video, an audio. closed_captions is what a
    probe that decoded a frame of the video says of it."""
    return {
        "format": {"format_name": "matroska,webm", "duration": "60.000000"}, "chapters": [],
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920,
             "height": 1080, "pix_fmt": "yuv420p", "closed_captions": closed_captions,
             "disposition": {"default": 1}, "tags": {}},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
             "disposition": {"default": 1}, "tags": {"language": "eng"}},
        ],
    }


def _video(rung: str, height: int, **said: object) -> dict:
    return {"id": rung, "dir": f"hls/{rung}", "codec": "hvc1.1.6.L120.90",
            "width": height * 16 // 9, "height": height,
            "encoder": "copy" if rung == "v0" else "hevc_nvenc", **said}


def _record(video: list[dict], closed_captions: int = 1) -> tuple[dict, dict]:
    """(the package record of a package of these video renditions, its
    source record)."""
    raw = _raw(closed_captions)
    source, _ = rec.source_record(
        SOURCE, ORIGINAL, 1, taken_at=NOW, taken_by="packager",
        library_path=f"Example Film (2024)/{ORIGINAL}", qh1="sha256:" + "0" * 64,
        probe=raw, probe_version="ffprobe test")
    streams = raw["streams"]
    probe = pk._Probe(container="matroska,webm", duration_ms=60_000, video=streams[0],
                      audio=[streams[1]], subtitles=[], video_index=0)
    manifest = {
        "packagedAt": NOW, "packager": "packager-test", "durationMs": 60_000,
        "renditions": {"video": video,
                       "audio": [{"idx": 0, "id": "a0", "dir": "hls/a0", "codec": "mp4a.40.2",
                                  "language": "eng", "channels": 2, "default": True}],
                       "audioSurround": []},
        "subtitles": [],
    }
    package = records.package_record(
        "c0ffee00-0000-4000-8000-0000000000cd", pk.Built(manifest, probe, {}),
        [("hls/v0/init.mp4", "0" * 64, 1)], source, records.OriginalProbe(raw, "ffprobe test"),
        {}, None, NOW)
    return package, source


def _dropped(package: dict) -> bool:
    return "closed-captions-dropped" in [x["kind"] for x in package["fidelity"]["losses"]]


def test_a_package_whose_top_rendition_carries_them_has_them() -> None:
    package, source = _record([_video("v0", 1080, closedCaptions=True),
                               _video("v1", 720, closedCaptions=True)])
    assert [v.get("closedCaptions") for v in package["renditions"]["video"]] == [True, True]
    assert source["essence"]["closedCaptions"] is package["essence"]["closedCaptions"] is True
    assert not _dropped(package)
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []


def test_the_top_rendition_alone_is_enough() -> None:
    # What a lower rung lacks, the top one still carries.
    package, source = _record([_video("v0", 1080, closedCaptions=True), _video("v1", 720)])
    assert [v.get("closedCaptions") for v in package["renditions"]["video"]] == [True, None]
    assert package["essence"]["closedCaptions"] is True
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []


def test_a_package_whose_top_rendition_says_none_has_none() -> None:
    for video in ([_video("v0", 1080), _video("v1", 720, closedCaptions=True)],
                  [_video("v0", 1080)],
                  [_video("v0", 1080, closedCaptions=False)],
                  [_video("v0", 1080, closedCaptions="true")]):
        package, source = _record(video)
        assert "closedCaptions" not in package["renditions"]["video"][0]
        assert package["essence"]["closedCaptions"] is False
        assert _dropped(package)
        assert rec.deletion_gate([source["essence"]], package["essence"]) == ["closedCaptions"]


def test_a_package_of_an_original_without_them() -> None:
    package, source = _record([_video("v0", 1080)], closed_captions=0)
    assert source["essence"]["closedCaptions"] is package["essence"]["closedCaptions"] is False
    assert not _dropped(package)
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []


def test_the_record_keeps_a_renditions_captions_only_when_it_says_it_carries_them() -> None:
    base = sorted(rec.video_rendition(_video("v0", 1080)))
    assert sorted(rec.video_rendition(_video("v0", 1080, closedCaptions=True))) \
        == sorted([*base, "closedCaptions"])
    for said in (False, None, "true", 1):
        assert sorted(rec.video_rendition(_video("v0", 1080, closedCaptions=said))) == base


# ---------------------------------------------------------------- the reading

class _Ffprobe:
    """subprocess.run, as ffprobe answers the reading: `answer` is its JSON,
    or the exception it raises."""

    def __init__(self, answer: dict | str | BaseException) -> None:
        self.answer = answer
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kw))
        if isinstance(self.answer, BaseException):
            raise self.answer
        out = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return subprocess.CompletedProcess(args, 0, out, "")


def _frames(*side_data: list[str]) -> list[dict]:
    return [{"media_type": "video", "side_data_list": [{"side_data_type": t} for t in types]}
            for types in side_data]


@pytest.mark.parametrize(("answer", "carries"), [
    # 9.0: the frames' side data alone; 7.1 says the stream's too.
    ({"frames": _frames([], [CAPTIONS]), "streams": [{}]}, True),
    ({"frames": _frames([CAPTIONS]), "streams": [{"closed_captions": 1}]}, True),
    ({"frames": [], "streams": [{"closed_captions": 1}]}, True),
    # x264's own SEI is no caption.
    ({"frames": _frames(["H.26[45] User Data Unregistered SEI message"], []),
      "streams": [{"closed_captions": 0}]}, False),
    ({"frames": [], "streams": [{}]}, False),
    ({}, False),
])
def test_a_rendition_carries_them_when_its_frames_do(
    monkeypatch: pytest.MonkeyPatch, answer: dict, carries: bool,
) -> None:
    ffprobe = _Ffprobe(answer)
    monkeypatch.setattr(pk.subprocess, "run", ffprobe)
    assert pk._closed_captions(Path("/p/hls/v0/playlist.m3u8")) is carries
    [(args, kw)] = ffprobe.calls
    # The media playlist, as a player reads it: its init segment, then the
    # first packets of the first segment, decoded, and no more.
    assert args[0] == "ffprobe" and args[-1] == "/p/hls/v0/playlist.m3u8"
    assert args[args.index("-read_intervals") + 1] == f"%+#{pk.CLOSED_CAPTION_PACKETS}"
    assert args[args.index("-select_streams") + 1] == "v:0"
    assert "-show_frames" in args
    assert kw["timeout"] == pk.CLOSED_CAPTION_TIMEOUT


@pytest.mark.parametrize("answer", [
    subprocess.CalledProcessError(1, ["ffprobe"]),
    subprocess.TimeoutExpired(["ffprobe"], pk.CLOSED_CAPTION_TIMEOUT),
    FileNotFoundError("ffprobe"),
    "not json",
])
def test_a_rendition_that_cant_be_read_says_none(
    monkeypatch: pytest.MonkeyPatch, answer: BaseException | str,
) -> None:
    monkeypatch.setattr(pk.subprocess, "run", _Ffprobe(answer))
    with capture_logs() as logs:
        assert pk._closed_captions(Path("/p/hls/v0/playlist.m3u8")) is False
    assert [x["event"] for x in logs] == ["packager.closed_captions.unread"]
