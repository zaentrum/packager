"""The package's origin, without binaries (the runs are recorded): every
ffmpeg pass that writes a track or a script of the package puts the first
video frame at the package's 0, or where the contract puts it. A source
read as starting before its first frame (ffmpeg 7.1 reads a Matroska AAC
track from its codec delay before 0) is normalised as ffmpeg normalises it
and moved by that distance, every output alike: the transmux, the subtitle
sidecars, the styled scripts. A contract's input keeps its timestamps
(-copyts) and its offset, the subtitles' runs too. The signals the stereo
loudness lifts from are written that lead later, as NUT holds no time
before 0, and the lift moves its rendition back. A source that starts with
its first frame, or whose frame has no time, is made as before."""

from __future__ import annotations

from pathlib import Path

import pytest

from packager import packager as pk
from packager.renditions import VideoInput

SRC = Path("/m/src.mkv")


def _probe(start: float | None, video_start: float | None, *, audio: list[dict] | None = None,
           subtitles: tuple[str, ...] = ()) -> pk._Probe:
    video = {"codec_name": "h264", "index": 0,
             **({"start_time": f"{video_start:.6f}"} if video_start is not None else {})}
    return pk._Probe(container="matroska,webm", duration_ms=60_000, video=video,
                     audio=audio if audio is not None else [],
                     subtitles=[{"index": 1 + i, "codec_name": c, "tags": {"language": "eng"}}
                                for i, c in enumerate(subtitles)],
                     video_index=0, start_time=start)


@pytest.mark.parametrize(("timeline", "ts_offset", "start", "video", "origin"), [
    # As is: the first frame at 0. A file that starts with it is as before.
    ("normalize", 0.0, 0.0, 0.0, pk._Origin()),
    ("normalize", 0.0, 0.5, 0.5, pk._Origin()),
    # ... one read from its audio's codec delay before it, that much earlier.
    ("normalize", 0.0, -0.021, 0.0, pk._Origin(offset=-0.021, lead=0.021)),
    ("normalize", 0.0, 11.378667, 11.4, pk._Origin(offset=-0.021333, lead=0.021333)),
    # ... a frame without a time: ffmpeg's normalisation alone, as before.
    ("normalize", 0.0, -0.021, None, pk._Origin()),
    # A contract's: its timestamps kept, moved by its offset.
    ("offset", -0.5, 0.479, 0.5, pk._Origin(copyts=True, offset=-0.5, lead=0.021)),
    ("offset", -0.5, 0.5, 0.5, pk._Origin(copyts=True, offset=-0.5)),
    ("offset", 0.021, 0.0, 0.0, pk._Origin(copyts=True, offset=0.021)),
    ("keep", 0.021, -0.021, 0.0, pk._Origin(copyts=True, lead=0.021)),
    ("keep", 0.0, 0.0, 0.0, pk._Origin(copyts=True)),
])
def test_the_origin_puts_the_first_frame_at_0(timeline: str, ts_offset: float,
                                              start: float | None, video: float | None,
                                              origin: pk._Origin) -> None:
    assert pk._origin(timeline, ts_offset, _probe(start, video)) == origin


def test_the_origins_arguments() -> None:
    assert (pk._Origin().input_args, pk._Origin().output_args, pk._Origin().signal_args) == (
        [], [], [])
    early = pk._Origin(offset=-0.021, lead=0.021)
    assert (early.input_args, early.output_args, early.signal_args) == (
        [], ["-output_ts_offset", "-0.021000"], [])
    kept = pk._Origin(copyts=True, offset=-0.5, lead=0.021)
    assert (kept.input_args, kept.output_args, kept.signal_args) == (
        ["-copyts"], ["-output_ts_offset", "-0.500000"], ["-output_ts_offset", "-0.479000"])


def _a() -> dict:
    return {"codec_name": "aac", "channels": 2, "channel_layout": "stereo",
            "tags": {"language": "eng"}, "disposition": {"default": 1}}


def test_a_source_read_from_before_its_first_frame_is_moved_by_that_much(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # ffmpeg 7.1's reading of a Matroska AAC track: the file from 21 ms
    # before its first frame. ffmpeg's normalisation as before, the MP4 that
    # much earlier: its first frame at 0, the track's priming before it.
    calls: list[list[str]] = []
    lifts: list[dict] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    monkeypatch.setattr(pk, "_stereo_signals", lambda probe, tmpdir, *a: {0: tmpdir / "s0.nut"})
    monkeypatch.setattr(pk, "_lift_stereo", lambda probe, signals, mp4, target, **kw: (
        lifts.append(kw) or {0: (pk.KEPT, None)}))
    pk._prepare_source(SRC, _probe(-0.021, 0.0, audio=[_a()]), tmp_path, default_index=0,
                       stereo_loudness=-20.0)
    [args] = calls
    assert "-copyts" not in args
    mp4 = args.index(str(tmp_path / "transmux.mp4"))
    assert args[mp4 - 4:mp4] == ["-output_ts_offset", "-0.021000", "-movflags", "+faststart"]
    # The signal on the MP4's timeline 21 ms later, from 0; the lift moves
    # its rendition back by as much.
    assert args[mp4 + 1:mp4 + 3] == ["-map", "[l0]"]
    assert "-output_ts_offset" not in args[mp4 + 1:]
    assert lifts == [{"lead": 0.021}]
    assert pk._lift_args(Path("/t/s0.nut"), "anull", Path("/t/s0.mp4"), "eng", 0.021)[-5:] == [
        "-output_ts_offset", "-0.021000", "-movflags", "+faststart", "/t/s0.mp4"]


def test_a_source_that_starts_with_its_first_frame_is_made_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    pk._prepare_source(SRC, _probe(0.0, 0.0, audio=[_a()]), tmp_path)
    [args] = calls
    assert "-copyts" not in args and "-output_ts_offset" not in args
    assert pk._lift_args(Path("/t/s0.nut"), "anull", Path("/t/s0.mp4"), "eng") == pk._lift_args(
        Path("/t/s0.nut"), "anull", Path("/t/s0.mp4"), "eng", 0.0)


class _Runs:
    """Stand-in for subprocess.run: records every run, writes each output."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.runs: list[list[str]] = []
        monkeypatch.setattr(pk.subprocess, "run", self)

    def __call__(self, args: list[str], **kw):
        self.runs.append(args)
        for i, a in enumerate(args):
            if a == "-map":
                out = next(x for x in args[i + 1:] if x.startswith("/"))
                Path(out).write_text("WEBVTT\n")
        return pk.subprocess.CompletedProcess(args, 0, "", "")


@pytest.mark.parametrize(("timeline", "ts_offset", "start", "head", "each"), [
    ("normalize", 0.0, -0.021, [], ["-output_ts_offset", "-0.021000"]),
    ("normalize", 0.0, 0.5, [], []),
    ("offset", -0.5, 0.479, ["-copyts"], ["-output_ts_offset", "-0.500000"]),
    ("keep", 0.0, 0.0, ["-copyts"], []),
])
def test_every_subtitle_output_is_on_the_transmuxs_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, timeline: str, ts_offset: float,
    start: float, head: list[str], each: list[str],
) -> None:
    # The one pass over the source: every sidecar and the styled script.
    runs = _Runs(monkeypatch)
    subs = tmp_path / "subs"
    video = 0.5 if start == 0.5 or timeline == "offset" else 0.0
    pk._extract_subtitles(SRC, _probe(start, video, subtitles=("ass", "hdmv_pgs_subtitle")), subs,
                          timeline=timeline, ts_offset=ts_offset)
    assert runs.runs == [[
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", *head,
        "-i", str(SRC),
        "-map", "0:s:0", "-c:s", "webvtt", *each, str(subs / "0.vtt"),
        "-map", "0:s:0", "-c:s", "copy", "-f", "ass", *each, str(subs / "0.ass"),
        "-map", "0:s:1", "-c:s", "copy", "-f", "sup", *each, str(subs / "1.sup"),
    ]]


def test_a_track_alone_and_its_script_alone_are_on_it_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runs = _Runs(monkeypatch)
    origin = pk._Origin(copyts=True, offset=-0.5)
    c = pk._sidecar(0, "ass")
    assert pk._extract_alone(SRC, tmp_path, c, "ass", 60.0, origin)
    assert pk._extract_styled(SRC, tmp_path, c, 60.0, origin)
    assert runs.runs == [
        ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-copyts",
         "-i", str(SRC), "-map", "0:s:0", "-c:s", "webvtt", "-output_ts_offset", "-0.500000",
         str(tmp_path / "0.vtt")],
        ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-copyts",
         "-i", str(SRC), "-map", "0:s:0", "-c:s", "copy", "-f", "ass",
         "-output_ts_offset", "-0.500000", str(tmp_path / "0.ass")],
    ]


def test_a_rung_keeps_the_contracts_timeline(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    # A contract's rung is on its shared timeline already, wherever it starts.
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_ffprobe", lambda _path: _probe(-0.021, 0.0))
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda _label, args: calls.append(args))
    assert pk._remux_video(VideoInput("v1", Path("/inbox/v1.mkv"), timeline="keep"),
                           tmp_path, 0.021) is not None
    [args] = calls
    assert "-copyts" in args and "-output_ts_offset" not in args
