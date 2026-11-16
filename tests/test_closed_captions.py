"""A package's record says it carries closed captions when its top video
rendition's own stream does, as the packager read them from windows across
the rendition — no longer never, whatever it carried. The
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

def _rendition(folder: Path, durations: list[float]) -> Path:
    """A rendition's folder as shaka-packager writes it: its media playlist,
    its init segment and its segments, of these durations (empty files: the
    made-up ffprobe reads none)."""
    folder.mkdir(parents=True)
    (folder / "init.mp4").write_bytes(b"")
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:8", '#EXT-X-MAP:URI="init.mp4"']
    for n, d in enumerate(durations, 1):
        (folder / f"seg-{n:05d}.m4s").write_bytes(b"")
        lines += [f"#EXTINF:{d:.3f},", f"seg-{n:05d}.m4s"]
    (folder / "playlist.m3u8").write_text("\n".join([*lines, "#EXT-X-ENDLIST", ""]))
    return folder / "playlist.m3u8"


def _frames(*side_data: list[str]) -> list[dict]:
    return [{"media_type": "video", "side_data_list": [{"side_data_type": t} for t in types]}
            for types in side_data]


# x264's own SEI is no caption: a window of frames without any.
NONE = {"frames": _frames(["H.26[45] User Data Unregistered SEI message"], []), "streams": [{}]}


class _Ffprobe:
    """subprocess.run, as ffprobe answers each window: by the name of the
    segment it reads, its JSON or the exception it raises; frames without
    captions for a segment `answers` does not name."""

    def __init__(self, answers: dict[str, dict | str | BaseException] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kw))
        answer = self.answers.get(Path(args[-1].split("|", 1)[1]).name, NONE)
        if isinstance(answer, BaseException):
            raise answer
        out = answer if isinstance(answer, str) else json.dumps(answer)
        return subprocess.CompletedProcess(args, 0, out, "")

    def read(self) -> list[str]:
        return [Path(args[-1].split("|", 1)[1]).name for args, _ in self.calls]


TEN = [6.0] * 10  # 60 s: windows at 6, 18, 30, 42 and 54 s, in segments 2, 4, 6, 8 and 10


@pytest.mark.parametrize(("durations", "read"), [
    (TEN, ["seg-00002.m4s", "seg-00004.m4s", "seg-00006.m4s", "seg-00008.m4s", "seg-00010.m4s"]),
    # 12 s cut at its keyframes: 1.2, 3.6 and 6 s in the first, 8.4 and 10.8 in the second
    ([7.92, 4.0], ["seg-00001.m4s", "seg-00002.m4s"]),
    ([4.0], ["seg-00001.m4s"]),
])
def test_the_windows_are_the_segments_that_hold_10_to_90_percent_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durations: list[float], read: list[str],
) -> None:
    playlist = _rendition(tmp_path / "hls" / "v0", durations)
    ffprobe = _Ffprobe()
    monkeypatch.setattr(pk.subprocess, "run", ffprobe)
    assert pk._closed_captions(playlist) is False
    assert ffprobe.read() == read
    for args, kw in ffprobe.calls:
        # each window the first packets of its segment, from its start (a
        # keyframe), read after the init segment, nothing copied
        assert args[0] == "ffprobe"
        assert args[-1].startswith(f"concat:{playlist.parent / 'init.mp4'}|{playlist.parent}/seg-")
        assert args[args.index("-read_intervals") + 1] == f"%+#{pk.CLOSED_CAPTION_PACKETS}"
        assert args[args.index("-select_streams") + 1] == "v:0"
        assert "-show_frames" in args
        assert kw["timeout"] == pk.CLOSED_CAPTION_TIMEOUT


@pytest.mark.parametrize("answer", [
    {"frames": _frames([], [CAPTIONS]), "streams": [{}]},             # 9.0: the frames' side data
    {"frames": _frames([CAPTIONS]), "streams": [{"closed_captions": 1}]},
    {"frames": _frames([]), "streams": [{"closed_captions": 1}]},     # 7.1 says the stream's too
])
def test_captions_in_any_window_are_the_renditions_and_end_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: dict,
) -> None:
    # captions that start late: none in the first windows, some in the fourth
    playlist = _rendition(tmp_path / "hls" / "v0", TEN)
    ffprobe = _Ffprobe({"seg-00008.m4s": answer})
    monkeypatch.setattr(pk.subprocess, "run", ffprobe)
    assert pk._closed_captions(playlist) is True
    assert ffprobe.read() == ["seg-00002.m4s", "seg-00004.m4s", "seg-00006.m4s", "seg-00008.m4s"]


@pytest.mark.parametrize("answer", [
    subprocess.CalledProcessError(1, ["ffprobe"]),
    subprocess.TimeoutExpired(["ffprobe"], pk.CLOSED_CAPTION_TIMEOUT),
    FileNotFoundError("ffprobe"),
    "not json",
    {"frames": [], "streams": [{"closed_captions": 0}]},             # no frame decoded
])
def test_a_window_that_cant_be_read_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: BaseException | str | dict,
) -> None:
    playlist = _rendition(tmp_path / "hls" / "v0", TEN)
    monkeypatch.setattr(pk.subprocess, "run", _Ffprobe({"seg-00004.m4s": answer}))
    with capture_logs() as logs:
        assert pk._closed_captions(playlist) is False
    assert [(x["event"], Path(x["segment"]).name) for x in logs] == [
        ("packager.closed_captions.unread", "seg-00004.m4s")]
    # the windows after it are read all the same
    captions = {"frames": _frames([CAPTIONS]), "streams": [{}]}
    monkeypatch.setattr(pk.subprocess, "run",
                        _Ffprobe({"seg-00004.m4s": answer, "seg-00008.m4s": captions}))
    assert pk._closed_captions(playlist) is True


def test_a_rendition_without_its_playlist_init_or_segments_says_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffprobe = _Ffprobe({name: {"frames": _frames([CAPTIONS])} for name in ("seg-00001.m4s",)})
    monkeypatch.setattr(pk.subprocess, "run", ffprobe)
    playlist = _rendition(tmp_path / "a", [4.0])
    no_map = tmp_path / "b" / "playlist.m3u8"
    no_map.parent.mkdir()
    no_map.write_text(playlist.read_text().replace('#EXT-X-MAP:URI="init.mp4"\n', ""))
    no_segments = tmp_path / "c" / "playlist.m3u8"
    no_segments.parent.mkdir()
    no_segments.write_text('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXT-X-ENDLIST\n')
    gone = tmp_path / "gone" / "playlist.m3u8"
    with capture_logs() as logs:
        assert [pk._closed_captions(p) for p in (gone, no_map, no_segments)] == [False] * 3
    assert [x["event"] for x in logs] == ["packager.closed_captions.unread"] * 3
    assert ffprobe.calls == []
    assert pk._closed_captions(playlist) is True


def test_a_path_the_concat_protocol_cant_take_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffprobe = _Ffprobe({"seg-00001.m4s": {"frames": _frames([CAPTIONS])}})
    monkeypatch.setattr(pk.subprocess, "run", ffprobe)
    with capture_logs() as logs:
        assert pk._closed_captions(_rendition(tmp_path / "a|b" / "v0", [4.0])) is False
    assert ffprobe.calls == [] and [x["event"] for x in logs] == ["packager.closed_captions.unread"]
