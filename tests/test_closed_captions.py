"""A package's record says it carries closed captions when its top video
rendition's own stream does, as the packager read them from windows across
the rendition — no longer never, whatever it carried —, what its video
shows: the deletion gate does not use it, and keeps an original that has
captions, or whose captions are not known, whatever its package carries. The
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
            "encoder": "copy" if rung == "v0" else "hevc_nvenc"} | said


def _record(video: list[dict], closed_captions: int = 1,
            windows: list[dict] | None = None) -> tuple[dict, dict]:
    """(the package record of a package of these video renditions, its
    source record, its picture's caption windows those given)."""
    raw = _raw(closed_captions)
    source, _ = rec.source_record(
        SOURCE, ORIGINAL, 1, taken_at=NOW, taken_by="packager",
        library_path=f"Example Film (2024)/{ORIGINAL}", qh1="sha256:" + "0" * 64,
        probe=raw, probe_version="ffprobe test", captions={0: windows} if windows else None)
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
    # the gate does not use the package's captions: an original that has
    # them is kept, whatever its package carries
    assert rec.deletion_gate([source["essence"]], package["essence"]) == ["closedCaptions"]


def test_the_top_rendition_alone_is_enough() -> None:
    # What a lower rung lacks, the top one still carries.
    package, source = _record([_video("v0", 1080, closedCaptions=True), _video("v1", 720)])
    assert [v.get("closedCaptions") for v in package["renditions"]["video"]] == [True, None]
    assert package["essence"]["closedCaptions"] is True


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
    # Its picture read in five windows, none of which shows any: it knows
    # it has none. One read from its probe alone can't tell, and its
    # package keeps it, a copy as an encode.
    none = [{"at": at, "start": at * 60, "end": at * 60 + 2, "frames": 48, "captions": False}
            for at in rec.CAPTION_WINDOWS]
    package, source = _record([_video("v0", 1080, encoder="hevc_nvenc")], closed_captions=0,
                              windows=none)
    assert source["essence"]["closedCaptions"] is package["essence"]["closedCaptions"] is False
    assert source["essence"]["closedCaptionsKnown"] is True
    assert not _dropped(package)
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []
    package, source = _record([_video("v0", 1080, encoder="hevc_nvenc")], closed_captions=0)
    assert "closedCaptionsKnown" not in source["essence"]
    assert rec.deletion_gate([source["essence"]], package["essence"]) == ["closedCaptions"]
    package, source = _record([_video("v0", 1080)], closed_captions=0)
    assert package["renditions"]["video"][0]["encoder"] == "copy"
    assert rec.deletion_gate([source["essence"]], package["essence"]) == ["closedCaptions"]


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


TEN = [6.0] * 10  # 60 s, segment n covering (n - 1) * 6 to n * 6 from the first picture


class _Ffprobe:
    """subprocess.run, as ffprobe answers: of a rendition of TEN, its first
    picture at `first` seconds, one frame a second of every segment it
    reads, those of the segments `captioned` names with captions; of the
    original, its first picture at `original`. `failing` segments make it
    fail, or give what it names."""

    def __init__(self, first: float = 0.0, captioned: tuple[int, ...] = (),
                 failing: dict[int, object] | None = None, original: float | None = None) -> None:
        self.first, self.captioned, self.failing = first, captioned, failing or {}
        self.original = original
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        target = args[-1]
        if "-show_streams" in args:  # _ffprobe of the original
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "format": {"duration": "60"},
                "streams": [{"index": 0, "codec_type": "video", "disposition": {}}]}), "")
        assert kw["timeout"] == pk.CLOSED_CAPTION_TIMEOUT
        if not target.startswith("concat:"):
            frames = [] if self.original is None else [
                {"best_effort_timestamp_time": f"{self.original:.6f}"}]
            return subprocess.CompletedProcess(args, 0, json.dumps({"frames": frames}), "")
        numbers = [int(Path(p).stem.split("-")[1]) for p in target.split("|")[1:]]
        for n in numbers:
            answer = self.failing.get(n)
            if isinstance(answer, BaseException):
                raise answer
            if answer is not None:
                return subprocess.CompletedProcess(args, 0, str(answer), "")
        frames = [{"best_effort_timestamp_time": f"{self.first + (n - 1) * 6 + k:.6f}",
                   "duration_time": "1.000000",
                   "side_data_list": [{"side_data_type": CAPTIONS}] if n in self.captioned else []}
                  for n in numbers for k in range(6)]
        if args[args.index("-read_intervals") + 1] == "%+#8":
            frames = frames[:1]
        return subprocess.CompletedProcess(args, 0, json.dumps({"frames": frames}), "")


def _windows(found: list[dict]) -> list[tuple]:
    return [(w["at"], w["start"], w["end"], w["frames"], w["captions"]) for w in found]


def test_a_rendition_is_read_at_the_titles_fractions_without_its_sources_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist = _rendition(tmp_path / "hls" / "v0", TEN)
    ffprobe = _Ffprobe(captioned=(8,))
    monkeypatch.setattr(pk.subprocess, "run", ffprobe)
    targets = pk._caption_targets(None, None, (0.0, 60.0), 90.0)
    assert targets == [(0.1, 6.0, 8.0), (0.3, 18.0, 20.0), (0.5, 30.0, 32.0), (0.7, 42.0, 44.0),
                       (0.9, 54.0, 56.0)]
    # across the rendition's own span, its first picture to its end: one
    # that starts at 10 s and ends at 70 s is read at 16, 28, 40, 52, 64 s;
    # one whose span can't be read at the title's duration from 0
    assert [t[1] for t in pk._caption_targets(None, None, (10.0, 70.0), 90.0)] == [
        16.0, 28.0, 40.0, 52.0, 64.0]
    assert [t[1] for t in pk._caption_targets(None, None, None, 60.0)] == [
        6.0, 18.0, 30.0, 42.0, 54.0]
    # each window the segments that cover its span, read after the init
    # segment, its frames those of the span: captions where they start late
    assert _windows(pk._caption_windows_of(playlist, targets)) == [
        (0.1, 6.0, 8.0, 2, False), (0.3, 18.0, 20.0, 2, False), (0.5, 30.0, 32.0, 2, False),
        (0.7, 42.0, 44.0, 2, True), (0.9, 54.0, 56.0, 2, False)]
    reads = [a for a in ffprobe.calls if a[-1].startswith("concat:") and "%+#8" not in a]
    assert [[Path(p).name for p in a[-1].split("|")] for a in reads] == [
        ["init.mp4", f"seg-{n:05d}.m4s"] for n in (2, 4, 6, 8, 10)]
    assert all(a[a.index("-select_streams") + 1] == "v:0" for a in reads)


def test_a_rendition_is_read_at_the_times_of_its_sources_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The package's times 10 s behind the original's (an original whose
    # first picture is at 10 s, packaged from 0): its windows at 16-18 s, ...,
    # 34.5-36.5 s (one that could not be read at 50 s) are the package's at
    # 6-8 s, ..., 24.5-26.5 s and 40-42 s; one spans two segments.
    playlist = _rendition(tmp_path / "hls" / "v0", TEN)
    monkeypatch.setattr(pk.subprocess, "run", _Ffprobe(captioned=(4, 5), original=10.0))
    assert pk._caption_offset(tmp_path / "original.mkv", playlist) == ((0.0, 60.0), None)
    (tmp_path / "original.mkv").write_bytes(b"")
    assert pk._caption_offset(tmp_path / "original.mkv", playlist) == ((0.0, 60.0), -10.0)
    source = [{"at": 0.1, "start": 16.0, "end": 18.0, "frames": 48, "captions": False},
              {"at": 0.3, "start": 28.0, "end": 30.0, "frames": 48, "captions": True},
              {"at": 0.5, "start": 34.5, "end": 36.5, "frames": 48, "captions": True},
              {"at": 0.7, "start": 50.0, "end": 50.0, "frames": 0, "captions": None},
              {"at": 0.9, "start": 64.0, "end": 66.0, "frames": 48, "captions": True}]
    targets = pk._caption_targets(source, -10.0, (0.0, 60.0), 60.0)
    assert targets == [(0.1, 6.0, 8.0), (0.3, 18.0, 20.0), (0.5, 24.5, 26.5), (0.7, 40.0, 42.0),
                       (0.9, 54.0, 56.0)]
    found = pk._caption_windows_of(playlist, targets)
    assert _windows(found) == [(0.1, 6.0, 8.0, 2, False), (0.3, 18.0, 20.0, 2, True),
                               (0.5, 24.0, 27.0, 3, True), (0.7, 40.0, 42.0, 2, False),
                               (0.9, 54.0, 56.0, 2, False)]
    # each at its source window's time, less the offset
    assert [round(t[1] - (-10.0), 3) for t in targets] == [w["start"] for w in source]


@pytest.mark.parametrize("answer", [
    subprocess.CalledProcessError(1, ["ffprobe"]),
    subprocess.TimeoutExpired(["ffprobe"], pk.CLOSED_CAPTION_TIMEOUT),
    FileNotFoundError("ffprobe"),
    "not json",
    json.dumps({"frames": []}),
])
def test_a_window_that_cant_be_read_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: object,
) -> None:
    playlist = _rendition(tmp_path / "hls" / "v0", TEN)
    monkeypatch.setattr(pk.subprocess, "run", _Ffprobe(captioned=(8,), failing={4: answer}))
    with capture_logs() as logs:
        found = pk._caption_windows_of(playlist, pk._caption_targets(None, None, (0.0, 60.0), 60.0))
    # recorded as failed, no frames; the windows after it read
    assert [(w["at"], w["frames"], w["captions"]) for w in found] == [
        (0.1, 2, False), (0.3, 0, False), (0.5, 2, False), (0.7, 2, True), (0.9, 2, False)]
    assert found[1]["start"] == found[1]["end"] == 18.0
    assert "packager.closed_captions.unread" in [x["event"] for x in logs]


def test_a_rendition_without_its_playlist_init_or_segments_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffprobe = _Ffprobe(captioned=(1,))
    monkeypatch.setattr(pk.subprocess, "run", ffprobe)
    playlist = _rendition(tmp_path / "a" / "v0", [4.0])
    no_map = tmp_path / "b" / "playlist.m3u8"
    no_map.parent.mkdir()
    no_map.write_text(playlist.read_text().replace('#EXT-X-MAP:URI="init.mp4"\n', ""))
    targets = pk._caption_targets(None, None, (0.0, 4.0), 4.0)
    piped = _rendition(tmp_path / "c|d" / "v0", [4.0])  # a path the concat protocol can't take
    for gone in (tmp_path / "gone" / "playlist.m3u8", no_map, piped):
        assert [(w["frames"], w["captions"]) for w in pk._caption_windows_of(gone, targets)] \
            == [(0, False)] * 5
    assert not any(a[-1].startswith("concat:") for a in ffprobe.calls)
    assert [w["captions"] for w in pk._caption_windows_of(playlist, targets)] == [True] * 5


def test_every_rendition_is_read_and_says_its_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hls_dir = tmp_path / "hls"
    _rendition(hls_dir / "v0", TEN)
    _rendition(hls_dir / "v1", TEN)
    (tmp_path / "original.mkv").write_bytes(b"")
    monkeypatch.setattr(pk.subprocess, "run", _Ffprobe(captioned=(2, 4, 6, 8, 10), original=1.4))
    meta = [{"id": "v0"}, {"id": "v1"}]
    pk._read_captions(hls_dir, meta, tmp_path / "original.mkv", None, 60.0)
    said = [(m.get("closedCaptions"), m.get("timestampOffset"),
             [w["captions"] for w in m["captionWindows"]]) for m in meta]
    assert said == [(True, -1.4, [True] * 5), (True, -1.4, [True] * 5)]
    # an original that is gone: no offset, the windows read as if both
    # timelines were one
    monkeypatch.setattr(pk.subprocess, "run", _Ffprobe())
    meta = [{"id": "v0"}]
    pk._read_captions(hls_dir, meta, tmp_path / "gone.mkv", None, 60.0)
    assert "closedCaptions" not in meta[0] and "timestampOffset" not in meta[0]
    assert [w["captions"] for w in meta[0]["captionWindows"]] == [False] * 5
