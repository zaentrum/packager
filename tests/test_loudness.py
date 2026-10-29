"""The stereo rendition's loudness (no ffmpeg run: the runner and the
measurements are stubbed): the signal a rendition is made from, split off
beside it for the tracks a client offers and the default one, where the
temp folder has the space; the decision to lift it (lift, no lift, the cap,
silent, too short); the lift's filters, its make-up and its ffmpeg runs,
each within its timeout; the measurement's parsing; a rendition made again
where its true peak is over or its loudness off, and kept only with the
timing it had; a lift that fails, however, costing its track the lift
only; the count the done step's details say; the shaka input of a lifted
rendition; the setting."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from packager import packager as pk
from packager import worker
from packager.katalog import ClaimedItem

FORMAT = "aformat=sample_rates=48000:channel_layouts=stereo"
LIMITER = "alimiter=limit=-3dB:attack=10:release=100:level=disabled:latency=enabled"
PAN_5_1 = "pan=stereo|FL=0.4142*FL+0.2929*FC+0.2929*SL|FR=0.4142*FR+0.2929*FC+0.2929*SR"
FLTP = "aformat=sample_fmts=fltp"


def _s(channels: int, layout: str | None, language: str = "eng", codec: str = "eac3") -> dict:
    return {"codec_name": codec, "channels": channels,
            **({"channel_layout": layout} if layout is not None else {}),
            "tags": {"language": language}, "disposition": {"default": 0, "comment": 0}}


def _loudness(integrated: float, seconds: float = 600.0, true_peak: float = -9.0) -> pk._Loudness:
    return pk._Loudness(integrated, 12.0, true_peak, seconds)


# ------------------------------------------------------------ the signal

@pytest.mark.parametrize(("channels", "layout", "signal", "makeup", "rest"), [
    (2, "stereo", FORMAT, 0.0, ""),
    (1, "mono", FORMAT, 0.0, ""),
    (6, "5.1(side)", f"{PAN_5_1},aresample=48000", 4.65, f"volume=4.65dB,{LIMITER},{FORMAT}"),
    (6, None, f"aresample=48000:rematrix_maxval=1,{FORMAT}", 0.0, f"{LIMITER},{FORMAT}"),
    (3, "2.1", f"pan=stereo|FL=1*FL|FR=1*FR,{FORMAT}", 0.0, ""),
])
def test_a_renditions_signal_is_its_filters_before_the_make_up(
        channels: int, layout: str | None, signal: str, makeup: float, rest: str) -> None:
    chain = pk._stereo_chain(_s(channels, layout))
    assert (chain.signal, chain.makeup, chain.rest) == (signal, makeup, rest)
    # The rendition is made as before: the two parts are its filters.
    assert ",".join(p for p in (signal, rest) if p) == pk._stereo_filter(_s(channels, layout))


def test_the_signal_is_split_off_before_the_rest() -> None:
    # With a rest, [l0] is the signal and the rest makes the rendition of
    # it; without one, the signal is the rendition: the same frames.
    # The sample format fixed before the split, as the AAC encoder takes it.
    assert pk._stereo_split(_s(6, "5.1(side)"), 0) == (
        f"{PAN_5_1},aresample=48000,{FLTP},asplit=2[l0][r0];[r0]volume=4.65dB,{LIMITER},{FORMAT}")
    assert pk._stereo_split(_s(2, "stereo"), 3) == f"{FORMAT},{FLTP},asplit=2[l3]"


# ------------------------------------------------------------ the decision

@pytest.mark.parametrize(("integrated", "makeup", "lift"), [
    (-33.3, 4.65, 8.65),    # a downmixed TV episode, -28.65 as made before: to -20
    (-29.7, 0.0, 9.7),      # a stereo film
    (-21.1, 0.0, 1.1),      # just under the target's margin: lifted
    (-21.0, 0.0, None),     # at the target less 1 LU: as it is
    (-25.65, 4.65, None),   # the same, made up by the downmix
    (-15.0, 0.0, None),     # louder than the target: never turned down
    (-45.0, 0.0, 14.0),     # a near-silent mix: no more than +14 dB
    (-69.9, 4.65, 14.0),
    (-70.0, 0.0, None),     # silent: nothing over the absolute gate
])
def test_the_lift(integrated: float, makeup: float, lift: float | None) -> None:
    assert pk._stereo_lift(_loudness(integrated), makeup, -20.0) == lift


def test_no_lift_without_a_measurement_a_target_or_the_length_to_measure() -> None:
    assert pk._stereo_lift(None, 0.0, -20.0) is None
    assert pk._stereo_lift(_loudness(-30.0), 0.0, None) is None
    assert pk._stereo_lift(_loudness(-30.0, seconds=2.9), 0.0, -20.0) is None
    assert pk._stereo_lift(_loudness(-30.0, seconds=3.0), 0.0, -20.0) == 10.0
    # Another target: the same rules.
    assert pk._stereo_lift(_loudness(-30.0), 0.0, -16.0) == 14.0
    assert pk._stereo_lift(_loudness(-24.5), 0.0, -23.0) == 1.5


# ------------------------------------------------------------ the lift's filters

def test_the_lift_filter() -> None:
    # Gain 8.65 dB to -24.65 LUFS: the compressor's threshold 8 dB over it.
    threshold = 10 ** ((-24.65 + pk._LIFT_THRESHOLD) / 20)
    assert pk._lift_filter(8.65, -24.65, 0.93) == (
        f"volume=8.65dB,acompressor=threshold={threshold:.5f}:{pk._LIFT_COMPRESSOR},"
        f"volume=0.93dB,alimiter=limit={pk._LIFT_CEILING:.1f}dB:attack=10:release=100"
        f":level=disabled:latency=enabled,{FORMAT}")
    assert ",alimiter=limit=-5.3dB:" in pk._lift_filter(8.65, -24.65, 0.93, -5.3)
    # Retimed: its timestamps counted from the first sample, before all else.
    assert pk._lift_filter(8.65, -24.65, 0.93, retime=True) == (
        f"asettb=1/sr,asetpts=STARTPTS+N,{pk._lift_filter(8.65, -24.65, 0.93)}")
    # A threshold ffmpeg takes, however low or high the level.
    assert "threshold=0.00098:" in pk._lift_compressor(14.0, -80.0)
    assert "threshold=1.00000:" in pk._lift_compressor(0.0, 0.0)


def test_the_lift_is_encoded_as_every_stereo_rendition_on_its_signals_timeline() -> None:
    args = pk._lift_args(Path("/t/stereo1.nut"), "volume=3dB", Path("/t/stereo1.mp4"), "ger")
    assert args == [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-copyts",
        "-threads", "1", "-i", "/t/stereo1.nut", "-map", "0:a:0", "-af", "volume=3dB",
        "-c:a", "aac", "-b:a", "192k", "-metadata:s:a:0", "language=ger",
        "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", "/t/stereo1.mp4"]


# ------------------------------------------------------------ the remux

Usage = namedtuple("Usage", "total used free")
GB = 1e9


@pytest.fixture
def space(monkeypatch):
    """The temp folder's file system as shutil.disk_usage has it: 256 GB,
    `free` of them free (set it), plenty by default."""
    usage = {"free": 200 * GB}
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda path: Usage(256 * GB, 256 * GB - usage["free"], usage["free"]))
    return usage


def _probe(audio: list[dict]) -> pk._Probe:
    return pk._Probe(container="matroska,webm", duration_ms=60_000,
                     video={"codec_name": "hevc", "index": 0}, audio=audio, subtitles=[],
                     video_index=0)


def test_the_remux_writes_every_renditions_signal_beside_it(monkeypatch, tmp_path: Path,
                                                            space) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    lifts: list[dict] = []
    monkeypatch.setattr(pk, "_lift_stereo", lambda probe, signals, mp4, target: (
        lifts.append({"signals": signals, "mp4": mp4, "target": target}) or
        {0: (pk.KEPT, None), 1: (pk.LIFTED, tmp_path / "stereo1.mp4")}))
    audio = [_s(6, "5.1(side)", codec="dts"), _s(2, "stereo", "ger")]
    surround = pk._surround_plan(audio, {0, 1}, pk.PackageOptions())
    mp4, meta, surround_meta = pk._prepare_source(
        Path("/m/src.mkv"), _probe(audio), tmp_path, surround=surround, default_index=0,
        timeline="offset", ts_offset=0.021, stereo_loudness=-20.0)
    [args] = calls
    graph = args[args.index("-filter_complex") + 1]
    assert graph == ";".join([
        "[0:a:0]asplit=2[as0][am0]",
        f"[as0]{PAN_5_1},aresample=48000,{FLTP},asplit=2[l0][r0];"
        f"[r0]volume=4.65dB,{LIMITER},{FORMAT}[s0]",
        "[am0]aformat=sample_rates=48000:channel_layouts=5.1(side)|5.1[m0]",
        f"[0:a:1]{FORMAT},{FLTP},asplit=2[l1][s1]",
    ])
    # The MP4 is made as before; the signals follow it, on its timeline.
    mp4_end = args.index(str(tmp_path / "transmux.mp4"))
    assert args[mp4_end - 4:mp4_end] == ["-output_ts_offset", "0.021000", "-movflags", "+faststart"]
    # FLAC of 24 bits, none of the source's metadata in it.
    flac = ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24", "-frame_size",
            "1024", "-map_metadata", "-1", "-map_chapters", "-1"]
    assert args[mp4_end + 1:] == [
        "-map", "[l0]", *flac, "-output_ts_offset", "0.021000", "-f", "nut",
        str(tmp_path / "stereo0.nut"),
        "-map", "[l1]", *flac, "-output_ts_offset", "0.021000", "-f", "nut",
        str(tmp_path / "stereo1.nut")]
    assert lifts == [{"signals": {0: tmp_path / "stereo0.nut", 1: tmp_path / "stereo1.nut"},
                      "mp4": mp4, "target": -20.0}]
    # The lifted rendition's entry names its MP4, every shown one says what
    # became of it; the 5.1 companion is as it was.
    assert [m.get("_input") for m in meta] == [None, tmp_path / "stereo1.mp4"]
    assert [m.get("_lift") for m in meta] == ["keep", "lift"]
    assert [(m["idx"], m["mode"]) for m in surround_meta] == [(0, "encode")]


def test_only_the_tracks_a_client_offers_get_a_signal(monkeypatch, tmp_path: Path,
                                                      space) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    lifts: list[dict] = []
    monkeypatch.setattr(pk, "_lift_stereo", lambda probe, signals, mp4, target: (
        lifts.append(signals) or {i: (pk.KEPT, None) for i in signals}))
    audio = [_s(2, "stereo", "eng"), _s(2, "stereo", "ger"), _s(6, None, "fre", "dts"),
             _s(2, "stereo", "ita")]
    _mp4, meta, _ = pk._prepare_source(Path("/m/src.mkv"), _probe(audio), tmp_path,
                                       audio_visible_indices={0, 2}, default_index=0,
                                       stereo_loudness=-20.0)
    assert [m.get("_lift") for m in meta] == ["keep", None, "keep", None]
    [args] = calls
    graph = args[args.index("-filter_complex") + 1]
    # The hidden German and Italian tracks are made as before, no signal of them.
    assert f"[0:a:1]{FORMAT}[s1]" in graph.split(";")
    assert lifts == [{0: tmp_path / "stereo0.nut", 2: tmp_path / "stereo2.nut"}]
    assert "[l1]" not in args and "[l0]" in args and "[l2]" in args


def test_the_default_track_is_measured_though_no_client_offers_it(monkeypatch, tmp_path: Path,
                                                                  space) -> None:
    # A whitelist none of two languages is in shows none; the master marks
    # one DEFAULT=YES all the same, and it plays: it is measured and lifted.
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: None)
    lifts: list[dict] = []
    monkeypatch.setattr(pk, "_lift_stereo", lambda probe, signals, mp4, target: (
        lifts.append(signals) or {i: (pk.LIFTED, tmp_path / f"stereo{i}.mp4") for i in signals}))
    audio = [_s(2, "stereo", "fre"), _s(2, "stereo", "ita")]
    shown = pk._visible_indices(audio, ["en", "de"], keep_original_if_single=True)
    default = pk._pick_default_audio(audio, shown, ["en", "de"])
    assert (shown, default) == (set(), 0)
    _mp4, meta, _ = pk._prepare_source(Path("/m/src.mkv"), _probe(audio), tmp_path,
                                       audio_visible_indices=shown, default_index=default,
                                       stereo_loudness=-20.0)
    assert lifts == [{0: tmp_path / "stereo0.nut"}]
    assert [m.get("_lift") for m in meta] == ["lift", None]
    assert pk._stereo_loudness_of(meta, -20.0).details == " st_lift=1 st_keep=0 st_skip=0"


# A file system of 256 GB: the floor max(30 GB, 12 %) = 30.72 GB. A shown
# track of 1.8 h: 1.91 GB of signal at most (24-bit PCM and FLAC's frames).
@pytest.mark.parametrize(("free", "minutes", "shown", "signals"), [
    (40, 108, 4, True),    # 7.65 GB: 32.35 left
    (40, 108, 5, False),   # 9.56 GB: 30.44 left, under 30.72
    (33, 108, 1, True),    # 1.91 GB: 31.09 left
    (33, 108, 2, False),   # 3.83 GB: 29.17 left
    (200, 180, 12, True),
    (31, 1, 1, True),      # 0.02 GB: 30.98 left
    (30.7, 1, 1, False),   # under the floor before any signal
])
def test_the_signals_need_the_temp_space(monkeypatch, tmp_path: Path, space, free: float,
                                         minutes: int, shown: int, signals: bool) -> None:
    space["free"] = free * GB
    probe = pk._Probe(container="matroska,webm", duration_ms=minutes * 60_000,
                      video={"codec_name": "hevc", "index": 0},
                      audio=[_s(2, "stereo")] * 12, subtitles=[], video_index=0)
    with capture_logs() as logs:
        got = pk._stereo_signals(probe, tmp_path, -20.0, set(range(shown)))
    assert sorted(got) == (list(range(shown)) if signals else [])
    skipped = [e for e in logs if e["event"] == "packager.stereo.lift_skipped"]
    if signals:
        assert not skipped
    else:
        [e] = skipped
        assert (e["reason"], e["tracks"], e["floor"]) == ("temp space", shown, round(30.72 * GB))
        assert e["bound"] == round(minutes * 60 * 48000 * 2 * 3 * 1.025 * shown)


def test_the_floor_is_30_gb_on_a_small_file_system_and_12_percent_on_a_large_one(
        monkeypatch, tmp_path: Path) -> None:
    probe = pk._Probe(container="mov", duration_ms=60_000, video={}, audio=[_s(2, "stereo")],
                      subtitles=[])
    # 1 TB: 120 GB.
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(1000 * GB, 879 * GB, 121 * GB))
    assert pk._stereo_signals(probe, tmp_path, -20.0, None) == {0: tmp_path / "stereo0.nut"}
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(1000 * GB, 880 * GB, 120 * GB))
    assert pk._stereo_signals(probe, tmp_path, -20.0, None) == {}
    # 100 GB, 12 % of it 12 GB: the floor is 30.
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(100 * GB, 69 * GB, 31 * GB))
    assert pk._stereo_signals(probe, tmp_path, -20.0, None) == {0: tmp_path / "stereo0.nut"}
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(100 * GB, 70.1 * GB, 29.9 * GB))
    assert pk._stereo_signals(probe, tmp_path, -20.0, None) == {}


def test_no_signals_without_a_target_a_length_or_the_temp_folders_size(monkeypatch,
                                                                       tmp_path: Path,
                                                                       space) -> None:
    probe = pk._Probe(container="mov", duration_ms=60_000, video={}, audio=[_s(2, "stereo")],
                      subtitles=[])
    assert pk._stereo_signals(probe, tmp_path, None, None) == {}
    assert pk._stereo_signals(probe, tmp_path, -20.0, set()) == {}
    with capture_logs() as logs:
        assert pk._stereo_signals(pk._Probe("mov", 0, {}, probe.audio, []), tmp_path, -20.0,
                                  None) == {}

        def fails(path):
            raise OSError("gone")

        monkeypatch.setattr(shutil, "disk_usage", fails)
        assert pk._stereo_signals(probe, tmp_path, -20.0, None) == {}
    assert [(e["event"], e["reason"]) for e in logs] == [
        ("packager.stereo.lift_skipped", "length unknown"),
        ("packager.stereo.lift_skipped", "temp space unknown")]


def test_without_the_temp_space_the_remux_is_as_before(monkeypatch, tmp_path: Path,
                                                       space) -> None:
    space["free"] = 30.5 * GB
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    monkeypatch.setattr(pk, "_lift_stereo", lambda *a: pytest.fail("nothing to lift"))
    _mp4, meta, _ = pk._prepare_source(Path("/m/src.mkv"), _probe([_s(2, "stereo")]), tmp_path,
                                       stereo_loudness=-20.0)
    [args] = calls
    assert args[args.index("-filter_complex") + 1] == f"[0:a:0]{FORMAT}[s0]"
    assert args[-1] == str(tmp_path / "transmux.mp4") and "_input" not in meta[0]


def test_without_a_target_the_remux_is_as_before(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    monkeypatch.setattr(pk, "_lift_stereo", lambda *a: pytest.fail("nothing to lift"))
    _mp4, meta, _ = pk._prepare_source(Path("/m/src.mkv"), _probe([_s(2, "stereo")]), tmp_path)
    [args] = calls
    assert args[args.index("-filter_complex") + 1] == f"[0:a:0]{FORMAT}[s0]"
    assert args[-1] == str(tmp_path / "transmux.mp4") and "nut" not in args
    assert "_input" not in meta[0]


# ------------------------------------------------------------ measuring

SUMMARY = """[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -31.4 LUFS
    Threshold: -42.0 LUFS

  Loudness range:
    LRA:        17.2 LU
    Threshold:  -52.1 LUFS
    LRA low:    -41.0 LUFS
    LRA high:   -23.8 LUFS

  True peak:
    Peak:       -6.3 dBFS
"""


PROGRESS = "out_time_us=N/A\nout_time_us=1000000\nout_time_us=3500000\nout_time_us=7212480000\n"


def _ran(monkeypatch, stderr: str, stdout: str = PROGRESS, code: int = 0) -> list[tuple]:
    seen: list[tuple] = []

    def run(args, **kw):
        seen.append((args, kw))
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    monkeypatch.setattr(pk.subprocess, "run", run)
    return seen


def test_the_measurement(monkeypatch) -> None:
    seen = _ran(monkeypatch, "frame lines\nI: -70.0 LUFS\n" + SUMMARY)
    # Its length from the last progress, the true peak where asked.
    assert pk._measure_loudness(Path("/t/s.nut"), true_peak=True) == (
        pk._Loudness(-31.4, 17.2, -6.3, 7212.48))
    [(args, kw)] = seen
    assert args[args.index("-af") + 1] == "ebur128=peak=true:framelog=quiet"
    assert args[args.index("-i") - 2:args.index("-i")] == ["-threads", "1"]
    # Read as UTF-8 whatever bytes a file's tags bring into ffmpeg's log.
    assert (kw["encoding"], kw["errors"]) == ("utf-8", "replace")
    # Through filters first; no true peak but where asked.
    seen = _ran(monkeypatch, SUMMARY.replace("  True peak:\n    Peak:       -6.3 dBFS\n", ""))
    assert pk._measure_loudness(Path("/t/s.nut"), "volume=3dB", timeout=99.0) == (
        pk._Loudness(-31.4, 17.2, -math.inf, 7212.48))
    [(args, kw)] = seen
    assert args[args.index("-af") + 1] == "volume=3dB,ebur128=peak=none:framelog=quiet"
    assert kw["timeout"] == 99.0
    # Silence: a true peak of -inf.
    _ran(monkeypatch, SUMMARY.replace("-6.3 dBFS", "-inf dBFS").replace("-31.4", "-70.0"))
    assert pk._measure_loudness(Path("/t/s.nut"), true_peak=True) == (
        pk._Loudness(-70.0, 17.2, -math.inf, 7212.48))


@pytest.mark.parametrize(("stderr", "stdout", "code", "true_peak"), [
    (SUMMARY, PROGRESS, 1, False),                   # ffmpeg failed
    ("no summary", PROGRESS, 0, False),
    (SUMMARY, "out_time_us=N/A\n", 0, False),        # no length
    (SUMMARY.split("  True peak")[0], PROGRESS, 0, True),
])
def test_what_cant_be_measured_is_none(monkeypatch, stderr: str, stdout: str, code: int,
                                       true_peak: bool) -> None:
    _ran(monkeypatch, stderr, stdout, code)
    assert pk._measure_loudness(Path("/t/s.nut"), true_peak=true_peak) is None


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["ffmpeg"], 9.0), OSError("gone")])
def test_a_measurement_that_runs_over_or_cant_run_is_none(monkeypatch, error) -> None:
    def run(args, **kw):
        raise error

    monkeypatch.setattr(pk.subprocess, "run", run)
    assert pk._measure_loudness(Path("/t/s.nut"), timeout=9.0) is None
    assert pk._lift_makeup(Path("/t/s.nut"), 13.3, -20.0, timeout=9.0) is None
    assert pk._audio_timing(Path("/t/s.mp4"), "a:0") is None


def test_an_encode_that_runs_over_is_killed(monkeypatch) -> None:
    def run(args, **kw):
        assert kw["timeout"] == 9.0
        raise subprocess.TimeoutExpired(args, 9.0)

    monkeypatch.setattr(pk.subprocess, "run", run)
    with pytest.raises(pk.PackageError, match="ran over 9 s and was killed"):
        pk._run_ffmpeg_capturing("stereo 0", ["ffmpeg"], timeout=9.0)


def _probes(low: float, high: float) -> str:
    """ffmpeg's log of the make-up's run: the two probes' summaries, the
    high one first (the order is ffmpeg's)."""
    return "".join(f"[ebur128@probe{n} @ 0x{n}] " + SUMMARY.split("] ", 1)[1].replace(
        "-31.4", f"{level}") for n, level in ((1, high), (0, low)))


def test_the_make_up_is_on_the_line_between_its_two_probes(monkeypatch) -> None:
    seen = _ran(monkeypatch, _probes(-23.6, -20.1))
    # At 0 dB -23.6, at 4 dB -20.1: -20 is 4.11 dB of make-up.
    assert pk._lift_makeup(Path("/t/s.nut"), 13.3, -20.0, timeout=9.0) == 4.11
    [(args, kw)] = seen
    graph = args[args.index("-filter_complex") + 1]
    assert graph.startswith(f"[0:a:0]{pk._lift_compressor(13.3, -20.0)},asplit=2[p0][p1];")
    assert (f"[p0]volume=0.00dB,{pk._lift_limiter()},ebur128@probe0=peak=none:framelog=quiet[m0]"
            in graph and f"[p1]volume=4.00dB,{pk._lift_limiter()},ebur128@probe1" in graph)
    assert args[args.index("-i") - 2:args.index("-i")] == ["-threads", "1"]
    assert (kw["timeout"], kw["encoding"], kw["errors"]) == (9.0, "utf-8", "replace")
    # Past them, on their line, to twice the higher one at most; none under.
    _ran(monkeypatch, _probes(-22.0, -20.4))
    assert pk._lift_makeup(Path("/t/s.nut"), 13.3, -20.0) == 5.0
    _ran(monkeypatch, _probes(-26.0, -25.0))
    assert pk._lift_makeup(Path("/t/s.nut"), 13.3, -20.0) == 8.0
    _ran(monkeypatch, _probes(-19.0, -15.0))
    assert pk._lift_makeup(Path("/t/s.nut"), 13.3, -20.0) == 0.0
    # Retimed, the compressor's input too.
    seen = _ran(monkeypatch, _probes(-23.6, -20.1))
    pk._lift_makeup(Path("/t/s.nut"), 13.3, -20.0, retime=True)
    assert f"[0:a:0]{pk._LIFT_RETIME}," in seen[0][0][seen[0][0].index("-filter_complex") + 1]


@pytest.mark.parametrize(("stderr", "code"), [
    (_probes(-23.6, -20.1), 1),                       # ffmpeg failed
    (_probes(-23.6, -20.1).split("[ebur128@probe0")[0], 0),   # one probe's summary only
    (_probes(-23.6, -23.5), 0),                       # the lift moves nothing: no line
])
def test_a_make_up_that_cant_be_measured_is_none(monkeypatch, stderr: str, code: int) -> None:
    _ran(monkeypatch, stderr, code=code)
    assert pk._lift_makeup(Path("/t/s.nut"), 13.3, -20.0) is None


# ------------------------------------------------------------ the lift, run

TIMING = {"time_base": "1/48000", "start_pts": -1024, "duration_ts": 2880000, "nb_frames": "2814"}


def _lift_run(monkeypatch, measured: list[pk._Loudness | None], *, makeup: float | None = 1.2,
              timing: dict | None = None, stream: dict | None = None
              ) -> tuple[tuple, list[str], list[tuple]]:
    """_lift_one on a track's signal (a 5.1(side) one's by default), its
    make-up `makeup`, its measurements `measured` in turn: the filters of
    every encode, and the filters and true peak of every measurement."""
    encoded: list[str] = []
    measures: list[tuple] = []

    def measure(path: Path, filters: str = "", *, true_peak: bool = False,
                timeout: float | None = None) -> pk._Loudness | None:
        assert timeout == 123.0
        measures.append((path.suffix, filters, true_peak))
        return measured.pop(0)

    def encode(label: str, args: list[str], timeout: float | None = None) -> None:
        assert timeout == 123.0
        encoded.append(args[args.index("-af") + 1])

    monkeypatch.setattr(pk, "_measure_loudness", measure)
    monkeypatch.setattr(pk, "_lift_makeup", lambda signal, gain, level, retime, timeout: makeup)
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", encode)
    monkeypatch.setattr(pk, "_audio_timing", lambda path, stream: (
        timing if timing is not None and path.name == "stereo0.mp4" else TIMING))
    out = pk._lift_one(0, stream or _s(6, "5.1(side)"), Path("/t/stereo0.nut"),
                       Path("/t/transmux.mp4"), -20.0, timeout=123.0)
    return out, encoded, measures


def test_a_quiet_rendition_is_lifted(monkeypatch) -> None:
    # The signal at -33.3 (-28.65 as made before): 13.3 dB to -20, made up
    # by 1.2 dB. The AAC at -20.1, -2.4 dBTP: kept.
    out, encoded, measures = _lift_run(monkeypatch, [
        _loudness(-33.3, true_peak=-math.inf), _loudness(-20.1, true_peak=-2.4)])
    assert out == (pk.LIFTED, Path("/t/stereo0.mp4"))
    assert encoded == [pk._lift_filter(13.3, -20.0, 1.2)]
    # The true peak measured once, on the decoded AAC.
    assert measures == [(".nut", "", False), (".mp4", "", True)]


def test_a_rendition_over_the_true_peak_or_off_its_level_is_made_again(monkeypatch) -> None:
    # 0.8 dB over: the limiter 1.3 dB lower, the make-up as it was.
    out, encoded, _ = _lift_run(monkeypatch, [
        _loudness(-33.3), _loudness(-20.0, true_peak=-1.2), _loudness(-20.2, true_peak=-2.6)])
    assert out == (pk.LIFTED, Path("/t/stereo0.mp4"))
    assert encoded == [pk._lift_filter(13.3, -20.0, 1.2),
                       pk._lift_filter(13.3, -20.0, 1.2, pk._LIFT_CEILING - 1.3)]
    # At -2.0 as measured (to 0.1 dB): not under, made again.
    out, encoded, _ = _lift_run(monkeypatch, [
        _loudness(-33.3), _loudness(-20.0, true_peak=-2.0), _loudness(-20.2, true_peak=-2.5)])
    assert out[0] == pk.LIFTED and len(encoded) == 2 and "limit=-4.5dB" in encoded[1]
    # 0.9 LU under its level (what the limiter takes too): the make-up 0.9 more.
    out, encoded, _ = _lift_run(monkeypatch, [
        _loudness(-33.3), _loudness(-20.9, true_peak=-3.0), _loudness(-20.1, true_peak=-2.9)])
    assert out[0] == pk.LIFTED
    assert encoded == [pk._lift_filter(13.3, -20.0, 1.2), pk._lift_filter(13.3, -20.0, 2.1)]
    # Within 0.5 LU and under the true peak: once.
    out, encoded, _ = _lift_run(monkeypatch, [_loudness(-33.3), _loudness(-20.4, true_peak=-2.1)])
    assert out[0] == pk.LIFTED and len(encoded) == 1
    # Off its level again, made again once only: kept.
    out, encoded, _ = _lift_run(monkeypatch, [
        _loudness(-33.3), _loudness(-21.0, true_peak=-3.0), _loudness(-20.7, true_peak=-2.9)])
    assert out[0] == pk.LIFTED and len(encoded) == 2
    # Over the true peak again: the rendition made before stays.
    out, encoded, _ = _lift_run(monkeypatch, [
        _loudness(-33.3), _loudness(-20.0, true_peak=-1.2), _loudness(-20.0, true_peak=-1.9)])
    assert out == (pk.SKIPPED, None) and len(encoded) == 2  # a lift not kept


def test_the_fallback_has_a_limiter_its_lift_is_not_retimed(monkeypatch) -> None:
    out, encoded, _ = _lift_run(monkeypatch, [_loudness(-30.0), _loudness(-20.1, true_peak=-2.5)],
                                makeup=1.0, stream=_s(6, None))
    assert out == (pk.LIFTED, Path("/t/stereo0.mp4"))
    assert encoded == [pk._lift_filter(10.0, -20.0, 1.0)]


def test_a_rendition_without_a_limiter_is_lifted_retimed(monkeypatch) -> None:
    out, encoded, _ = _lift_run(monkeypatch, [_loudness(-30.0), _loudness(-20.1, true_peak=-2.5)],
                                makeup=1.0, stream=_s(2, "stereo"))
    assert out == (pk.LIFTED, Path("/t/stereo0.mp4"))
    assert encoded == [pk._lift_filter(10.0, -20.0, 1.0, retime=True)]


@pytest.mark.parametrize(("change", "same"), [
    ({}, True),
    ({"duration_ts": 2880000 + 96}, True),     # 2 ms longer: the source's rounding
    ({"duration_ts": 2880000 - 96}, True),
    ({"duration_ts": 2880000 + 97}, False),
    ({"duration_ts": 2880000 + 479}, False),   # a limiter's 10 ms
    ({"start_pts": 0}, False),
    ({"nb_frames": "2815"}, False),
    ({"time_base": "1/44100"}, False),
    ({"duration_ts": None}, False),
])
def test_the_timing_a_lift_keeps(change: dict, same: bool) -> None:
    assert pk._same_timing({**TIMING, **change}, TIMING) is same
    assert pk._same_timing(None, TIMING) is False and pk._same_timing(TIMING, None) is False


def test_a_lift_with_another_timing_is_not_kept(monkeypatch) -> None:
    out, encoded, _ = _lift_run(monkeypatch, [_loudness(-33.3), _loudness(-20.1, true_peak=-2.4)],
                                timing={**TIMING, "start_pts": 0})
    assert out == (pk.SKIPPED, None) and len(encoded) == 1


@pytest.mark.parametrize(("measured", "makeup", "outcome", "encodes"), [
    ([_loudness(-24.0)], 1.2, "keep", 0),                  # loud enough: -19.35 as made
    ([_loudness(-40.0, seconds=2.0)], 1.2, "keep", 0),     # too short to measure
    ([_loudness(-70.0)], 1.2, "keep", 0),                  # silent
    ([None], 1.2, "skip", 0),                              # not measured
    ([_loudness(-33.3)], None, "skip", 0),                 # the make-up not measured
    ([_loudness(-33.3), None], 1.2, "skip", 1),            # nor the AAC
])
def test_no_lift_leaves_the_rendition(monkeypatch, measured: list, makeup: float | None,
                                      outcome: str, encodes: int) -> None:
    out, encoded, _ = _lift_run(monkeypatch, list(measured), makeup=makeup)
    assert out == (outcome, None) and len(encoded) == encodes


def test_every_signal_is_removed_and_a_failed_lift_keeps_its_rendition(
        monkeypatch, tmp_path: Path) -> None:
    signals = {i: tmp_path / f"stereo{i}.nut" for i in range(3)}
    for path in signals.values():
        path.write_bytes(b"x")

    signals[3] = tmp_path / "stereo3.nut"
    for path in [*signals.values(), *(p.with_suffix(".mp4") for p in signals.values())]:
        path.write_bytes(b"x")
    timeouts: set[float] = set()

    def lift_one(i, stream, signal, mp4, target, *, timeout):
        timeouts.add(timeout)
        if i == 1:
            raise pk.PackageError("ffmpeg stereo 1 exited 1")
        if i == 3:  # anything else: a tag ffmpeg's log can't be read in, a bug
            raise UnicodeDecodeError("utf-8", b"Caf\xe9", 3, 4, "invalid continuation byte")
        return (pk.LIFTED, signal.with_suffix(".mp4")) if i == 0 else (pk.KEPT, None)

    monkeypatch.setattr(pk, "_lift_one", lift_one)
    probe = _probe([_s(2, "stereo")] * 4)
    assert pk._lift_stereo(probe, signals, tmp_path / "transmux.mp4", -20.0) == {
        0: (pk.LIFTED, tmp_path / "stereo0.mp4"), 1: (pk.SKIPPED, None), 2: (pk.KEPT, None),
        3: (pk.SKIPPED, None)}
    # Every signal is removed, and every MP4 but the one kept.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stereo0.mp4"]
    # Every run within 10 min and twice the track's length (60 s).
    assert timeouts == {600.0 + 2 * 60}


# ------------------------------------------------------------ the count

def _marked(lifts: dict, visible: set[int] | None, monkeypatch, tmp_path: Path, space,
            free: float = 200) -> list[dict]:
    """The stereo entries _prepare_source leaves for four stereo tracks,
    `visible` of them shown, their lifts `lifts` (by track; a track missing
    raises, as an encode that fails does)."""
    space["free"] = free * GB
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: None)

    def lift_one(i, stream, signal, mp4, target, **_kw):
        if i not in lifts:
            raise RuntimeError(f"stereo {i}: not a PackageError")
        return lifts[i]

    monkeypatch.setattr(pk, "_lift_one", lift_one)
    audio = [_s(2, "stereo", lang) for lang in ("eng", "ger", "fre", "ita")]
    _mp4, meta, _ = pk._prepare_source(Path("/m/src.mkv"), _probe(audio), tmp_path,
                                       audio_visible_indices=visible, default_index=0,
                                       stereo_loudness=-20.0)
    return meta


def test_every_shown_track_is_counted_once(monkeypatch, tmp_path: Path, space) -> None:
    # Track 0 lifted, 1 at the target, 2 failing (not a PackageError), 3 hidden.
    meta = _marked({0: (pk.LIFTED, tmp_path / "stereo0.mp4"), 1: (pk.KEPT, None)}, {0, 1, 2},
                   monkeypatch, tmp_path, space)
    assert [m.get("_lift") for m in meta] == ["lift", "keep", "skip", None]
    stereo = pk._stereo_loudness_of(meta, -20.0)
    assert stereo == pk.StereoLoudness(known=True, lifted=1, kept=1, skipped=1)
    assert stereo.details == " st_lift=1 st_keep=1 st_skip=1"
    # The marks are taken off: nothing of them reaches the manifest.
    assert not any("_lift" in m for m in meta)


def test_a_title_the_temp_space_leaves_out_counts_every_shown_track_skipped(
        monkeypatch, tmp_path: Path, space) -> None:
    # Tracks 1 and 3 shown, 0 the default: three measured, none with a signal.
    meta = _marked({}, {1, 3}, monkeypatch, tmp_path, space, free=30.5)
    assert [m.get("_lift") for m in meta] == ["skip", "skip", None, "skip"]
    assert pk._stereo_loudness_of(meta, -20.0).details == " st_lift=0 st_keep=0 st_skip=3"


def test_with_the_setting_off_the_count_says_so(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: None)
    _mp4, meta, _ = pk._prepare_source(Path("/m/src.mkv"), _probe([_s(2, "stereo")]), tmp_path)
    assert "_lift" not in meta[0]
    assert pk._stereo_loudness_of(meta, None) == pk.StereoLoudness(known=True, off=True)
    assert pk._stereo_loudness_of(meta, None).details == " st=off"


def test_a_count_no_run_made_says_nothing() -> None:
    # The worker's own, before a run fills it in: no st tokens, never 0/0/0.
    assert pk.StereoLoudness().details == ""
    assert pk.StereoLoudness(off=True).details == ""


def test_the_done_steps_details_say_it() -> None:
    manifest = {"renditions": {"video": [{"codec": "hvc1.2.4.L150.90"}], "audio": [{}, {}],
                               "audioSurround": [{}]}, "subtitles": [{}] * 4}
    base = "v=hvc1.2.4.L150.90 a=2 subs=4 dur_s=12.5 a51=1"
    assert worker._details(manifest, 12.5) == base  # a version reported again: no run's count
    assert worker._details(manifest, 12.5, pk.StereoLoudness(
        known=True, lifted=1, kept=0, skipped=1)) == base + " st_lift=1 st_keep=0 st_skip=1"
    assert worker._details(manifest, 12.5, pk.StereoLoudness(known=True, off=True)) == (
        base + " st=off")
    assert worker._details(manifest, 12.5, pk.StereoLoudness()) == base


@pytest.mark.parametrize(("setting", "tail"), [
    ("-20", " st_lift=1 st_keep=0 st_skip=1"), ("off", " st=off"),
    ("unfilled", "")])  # a package_item that says nothing of it
def test_the_item_steps_details_say_it(monkeypatch, tmp_path: Path, setting: str,
                                       tail: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x")
    monkeypatch.setattr(worker, "_INBOX_ROOT", tmp_path / "_inbox")
    monkeypatch.setattr(worker, "probe_source", lambda _path: {})

    def package_item(*_a, stereo, options, **_kw):
        if setting == "unfilled":
            pass
        elif options.stereo_loudness is None:
            stereo.known, stereo.off = True, True
        else:
            stereo.known, stereo.lifted, stereo.skipped = True, 1, 1
        return {"renditions": {"video": [{"codec": "hvc1"}], "audio": [{}, {}]}, "subtitles": []}

    monkeypatch.setattr(worker, "package_item", package_item)
    done: list[str] = []

    class Client:
        def settings(self) -> dict:
            return {"packager.stereo_loudness": setting}

        def upsert_step(self, _id, status, **kw) -> None:
            if status == "done":
                done.append(kw["details"])

        def packaging_complete(self, *_args) -> None:
            pass

    item = ClaimedItem(id="c0ffee00-0000-4000-8000-0000000000ab", type="movie", title="Clip",
                       year=None, duration_ms=None, path=str(source))
    worker._process_one(item, Client())  # type: ignore[arg-type]
    [details] = done
    assert re.fullmatch(r"v=hvc1 a=2 subs=0 dur_s=[0-9.]+" + re.escape(tail), details)


# ------------------------------------------------------------ shaka

def test_shaka_packages_a_lifted_rendition_from_its_own_mp4(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    class Ran(Exception):
        pass

    def run(cmd, cwd, label):
        commands.append(cmd)
        raise Ran

    monkeypatch.setattr(pk, "_run_shaka", run)
    audio = [{"idx": 0, "language": "eng", "title": "", "channels": 2, "default": True,
              "_input": tmp_path / "stereo0.mp4"},
             {"idx": 1, "language": "ger", "title": "", "channels": 2, "default": False}]
    surround = [{"idx": 0, "language": "eng", "title": "", "channels": 6, "default": True,
                 "codec": "ec-3"}]
    with pytest.raises(Ran):
        pk._run_shaka_packager(tmp_path / "transmux.mp4", [], audio, surround, tmp_path)
    [cmd] = commands
    ins = [d.split(",")[:2] for d in cmd if d.startswith("in=")]
    assert ins == [[f"in={tmp_path / 'stereo0.mp4'}", "stream=audio"],
                   [f"in={tmp_path / 'transmux.mp4'}", "stream=2"],
                   [f"in={tmp_path / 'transmux.mp4'}", "stream=3"]]
    assert "_input" not in audio[0]


# ------------------------------------------------------------ the setting

OWN = object()  # the options' own target


@pytest.mark.parametrize(("value", "target"), [
    (None, OWN), ("", OWN), ("  ", OWN),
    ("-20", -20.0), ("-18.5", -18.5), (" -23 LUFS ", -23.0), ("-16", -16.0),
    ("off", None), ("OFF", None), ("none", None),
    ("loud", OWN), ("-50", OWN), ("5", OWN), ("nan", OWN), ("0", OWN),
])
def test_the_stereo_loudness_setting(value: str | None, target: object) -> None:
    raw = {} if value is None else {"packager.stereo_loudness": value}
    for options, own in ((None, pk.STEREO_LOUDNESS),
                         (pk.PackageOptions(stereo_loudness=-18.0), -18.0)):
        assert worker._with_settings(options, raw).stereo_loudness == (
            own if target is OWN else target)
    # The other options are the run's own.
    options = pk.PackageOptions(surround_codec="ac3")
    assert worker._with_settings(options, raw).surround_codec == "ac3"


def test_the_setting_reaches_the_run(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x")
    monkeypatch.setattr(worker, "_INBOX_ROOT", tmp_path / "_inbox")
    monkeypatch.setattr(worker, "probe_source", lambda _path: {})
    runs: list[dict] = []
    monkeypatch.setattr(worker, "package_item", lambda *_a, **kw: runs.append(kw) or {
        "renditions": {"video": [{"codec": "hvc1"}], "audio": []}, "subtitles": []})

    class Client:
        def settings(self) -> dict:
            return {"packager.stereo_loudness": "-18", "packager.language_whitelist": "en"}

        def upsert_step(self, *_args, **_kw) -> None:
            pass

        def packaging_complete(self, *_args) -> None:
            pass

    item = ClaimedItem(id="c0ffee00-0000-4000-8000-0000000000aa", type="movie", title="Clip",
                       year=None, duration_ms=None, path=str(source))
    worker._process_one(item, Client(), pk.PackageOptions(surround_codec="ac3"))  # type: ignore[arg-type]
    [run] = runs
    assert (run["options"].stereo_loudness, run["options"].surround_codec) == (-18.0, "ac3")
    assert run["language_whitelist"] == ["en"]
