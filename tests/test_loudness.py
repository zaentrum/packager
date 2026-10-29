"""The stereo rendition's loudness (no ffmpeg run: the runner and the
measurements are stubbed): the signal a rendition is made from, split off
beside it for the tracks a client offers, where the temp folder has the
space; the decision to lift it (lift, no lift, the cap, silent, too
short); the lift's filters and its ffmpeg run; the measurement's parsing;
a rendition made again where its true peak is over, and kept only with the
timing it had; the shaka input of a lifted rendition; the setting."""

from __future__ import annotations

import math
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
    assert pk._stereo_split(_s(6, "5.1(side)"), 0) == (
        f"{PAN_5_1},aresample=48000,asplit=2[l0][r0];[r0]volume=4.65dB,{LIMITER},{FORMAT}")
    assert pk._stereo_split(_s(2, "stereo"), 3) == f"{FORMAT},asplit=2[l3]"


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
        "-i", "/t/stereo1.nut", "-map", "0:a:0", "-af", "volume=3dB",
        "-c:a", "aac", "-b:a", "192k", "-metadata:s:a:0", "language=ger",
        "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", "/t/stereo1.mp4"]


# ------------------------------------------------------------ the remux

Usage = namedtuple("Usage", "total used free")
GB = 1e9


@pytest.fixture
def space(monkeypatch):
    """The temp folder's file system as shutil.disk_usage has it: a node's
    256 GB, `free` of them free (set it), plenty by default."""
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
        {1: tmp_path / "stereo1.mp4"}))
    audio = [_s(6, "5.1(side)", codec="dts"), _s(2, "stereo", "ger")]
    surround = pk._surround_plan(audio, {0, 1}, pk.PackageOptions())
    mp4, meta, surround_meta = pk._prepare_source(
        Path("/m/src.mkv"), _probe(audio), tmp_path, surround=surround, default_index=0,
        timeline="offset", ts_offset=0.021, stereo_loudness=-20.0)
    [args] = calls
    graph = args[args.index("-filter_complex") + 1]
    assert graph == ";".join([
        "[0:a:0]asplit=2[as0][am0]",
        f"[as0]{PAN_5_1},aresample=48000,asplit=2[l0][r0];[r0]volume=4.65dB,{LIMITER},{FORMAT}[s0]",
        "[am0]aformat=sample_rates=48000:channel_layouts=5.1(side)|5.1[m0]",
        f"[0:a:1]{FORMAT},asplit=2[l1][s1]",
    ])
    # The MP4 is made as before; the signals follow it, on its timeline.
    mp4_end = args.index(str(tmp_path / "transmux.mp4"))
    assert args[mp4_end - 4:mp4_end] == ["-output_ts_offset", "0.021000", "-movflags", "+faststart"]
    flac = ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24", "-frame_size",
            "1024"]
    assert args[mp4_end + 1:] == [
        "-map", "[l0]", *flac, "-output_ts_offset", "0.021000", "-f", "nut",
        str(tmp_path / "stereo0.nut"),
        "-map", "[l1]", *flac, "-output_ts_offset", "0.021000", "-f", "nut",
        str(tmp_path / "stereo1.nut")]
    assert lifts == [{"signals": {0: tmp_path / "stereo0.nut", 1: tmp_path / "stereo1.nut"},
                      "mp4": mp4, "target": -20.0}]
    # The lifted rendition's entry names its MP4; the 5.1 companion is as it was.
    assert [m.get("_input") for m in meta] == [None, tmp_path / "stereo1.mp4"]
    assert [(m["idx"], m["mode"]) for m in surround_meta] == [(0, "encode")]


def test_only_the_tracks_a_client_offers_get_a_signal(monkeypatch, tmp_path: Path,
                                                      space) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    lifts: list[dict] = []
    monkeypatch.setattr(pk, "_lift_stereo", lambda probe, signals, mp4, target: (
        lifts.append(signals) or {}))
    audio = [_s(2, "stereo", "eng"), _s(2, "stereo", "ger"), _s(6, None, "fre", "dts")]
    pk._prepare_source(Path("/m/src.mkv"), _probe(audio), tmp_path, audio_visible_indices={0, 2},
                       stereo_loudness=-20.0)
    [args] = calls
    graph = args[args.index("-filter_complex") + 1]
    # The hidden German track is made as before, no signal of it.
    assert f"[0:a:1]{FORMAT}[s1]" in graph.split(";")
    assert lifts == [{0: tmp_path / "stereo0.nut", 2: tmp_path / "stereo2.nut"}]
    assert "[l1]" not in args and "[l0]" in args and "[l2]" in args


# The production node: 256 GB, the floor max(30 GB, 15 %) = 38.4 GB.
@pytest.mark.parametrize(("free", "minutes", "shown", "signals"), [
    (44, 108, 4, False),   # 9.95 GB of signals at most: 34.05 GB left, under 38.4
    (44, 108, 1, True),    # 2.49 GB: 41.5 left
    (49, 108, 4, True),    # 39.05 left
    (200, 180, 12, True),
    (40, 1, 1, True),      # 0.01 GB: 39.99 left
    (38, 1, 1, False),     # under the floor before any signal
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
        assert (e["reason"], e["tracks"], e["floor"]) == ("temp space", shown, round(38.4 * GB))
        assert e["bound"] == round(minutes * 60 * 48000 * 2 * 4 * shown)


def test_the_floor_is_30_gb_on_a_small_file_system(monkeypatch, tmp_path: Path) -> None:
    probe = pk._Probe(container="mov", duration_ms=60_000, video={}, audio=[_s(2, "stereo")],
                      subtitles=[])
    # 100 GB, 15 % of it 15 GB: the floor is 30.
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
    space["free"] = 31 * GB
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


def _ran(monkeypatch, stderr: str, stdout: str = "out_time_us=N/A\nout_time_us=7212480000\n",
         code: int = 0) -> list[list[str]]:
    seen: list[list[str]] = []

    def run(args, **_kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    monkeypatch.setattr(pk.subprocess, "run", run)
    return seen


def test_the_measurement(monkeypatch) -> None:
    seen = _ran(monkeypatch, "frame lines\nI: -70.0 LUFS\n" + SUMMARY)
    assert pk._measure_loudness(Path("/t/s.nut")) == pk._Loudness(-31.4, 17.2, -6.3, 7212.48)
    assert seen[0][seen[0].index("-af") + 1] == "ebur128=peak=true:framelog=quiet"
    # Through filters first, without the true peak.
    seen = _ran(monkeypatch, SUMMARY.replace("  True peak:\n    Peak:       -6.3 dBFS\n", ""))
    assert pk._measure_loudness(Path("/t/s.nut"), "volume=3dB", true_peak=False) == (
        pk._Loudness(-31.4, 17.2, -math.inf, 7212.48))
    assert seen[0][seen[0].index("-af") + 1] == "volume=3dB,ebur128=peak=none:framelog=quiet"
    # Silence: a true peak of -inf.
    _ran(monkeypatch, SUMMARY.replace("-6.3 dBFS", "-inf dBFS").replace("-31.4", "-70.0"))
    assert pk._measure_loudness(Path("/t/s.nut")) == pk._Loudness(-70.0, 17.2, -math.inf, 7212.48)


@pytest.mark.parametrize(("stderr", "stdout", "code"), [
    (SUMMARY, "out_time_us=7212480000\n", 1),       # ffmpeg failed
    ("no summary", "out_time_us=7212480000\n", 0),
    (SUMMARY, "out_time_us=N/A\n", 0),              # no length
    (SUMMARY.split("  True peak")[0], "out_time_us=1\n", 0),
])
def test_what_cant_be_measured_is_none(monkeypatch, stderr: str, stdout: str, code: int) -> None:
    _ran(monkeypatch, stderr, stdout, code)
    assert pk._measure_loudness(Path("/t/s.nut")) is None


# ------------------------------------------------------------ the lift, run

TIMING = {"time_base": "1/48000", "start_pts": -1024, "duration_ts": 2880000, "nb_frames": "2814"}


def _lift_run(monkeypatch, measured: list[pk._Loudness | None], *,
              timing: dict | None = None) -> tuple[Path | None, list[str], list[str]]:
    """_lift_one on a 5.1(side) track's signal, its measurements `measured`
    in turn: the filters of every encode, and of every measurement."""
    encoded: list[str] = []
    measures: list[str] = []

    def measure(path: Path, filters: str = "", **_kw) -> pk._Loudness | None:
        measures.append(filters)
        return measured.pop(0)

    monkeypatch.setattr(pk, "_measure_loudness", measure)
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing",
                        lambda label, args: encoded.append(args[args.index("-af") + 1]))
    monkeypatch.setattr(pk, "_audio_timing", lambda path, stream: (
        timing if timing is not None and path.name == "stereo0.mp4" else TIMING))
    out = pk._lift_one(0, _s(6, "5.1(side)"), Path("/t/stereo0.nut"), Path("/t/transmux.mp4"),
                       -20.0)
    return out, encoded, measures


def test_a_quiet_rendition_is_lifted(monkeypatch) -> None:
    # The signal at -33.3 (-28.65 as made before): 13.3 dB to -20. The
    # compressor's output 1.2 LU under it, the whole lift then at -20.1:
    # made up by 1.2 dB. The AAC at -20.1, -2.4 dBTP.
    out, encoded, measures = _lift_run(monkeypatch, [
        _loudness(-33.3, true_peak=-11.0), _loudness(-21.2), _loudness(-20.1),
        _loudness(-20.1, true_peak=-2.4)])
    assert out == Path("/t/stereo0.mp4")
    assert encoded == [pk._lift_filter(13.3, -20.0, 1.2)]
    assert measures == ["", pk._lift_compressor(13.3, -20.0), pk._lift_filter(13.3, -20.0, 1.2),
                        ""]


def test_the_make_up_takes_in_what_the_limiter_takes(monkeypatch) -> None:
    measured = [_loudness(-21.2), _loudness(-20.9), _loudness(-20.2)]
    monkeypatch.setattr(pk, "_measure_loudness", lambda *_a, **_kw: measured.pop(0))
    # 1.2 for the compressor, 0.9 more for the limiter; then within 0.3 LU.
    assert pk._lift_makeup(Path("/t/s.nut"), 13.3, -20.0) == 2.1 and not measured
    # Three rounds at most.
    measured = [_loudness(-21.2)] + [_loudness(-21.0)] * 4
    assert pk._lift_makeup(Path("/t/s.nut"), 13.3, -20.0) == 4.2 and len(measured) == 1


def test_a_rendition_over_the_true_peak_is_made_again_lower(monkeypatch) -> None:
    out, encoded, _ = _lift_run(monkeypatch, [
        _loudness(-33.3), _loudness(-21.2), _loudness(-20.0), _loudness(-20.0, true_peak=-1.2),
        _loudness(-20.2, true_peak=-2.6)])
    assert out == Path("/t/stereo0.mp4")
    # 0.8 dB over: the limiter 1.3 dB lower.
    assert [f.split("alimiter=limit=")[1].split("dB")[0] for f in encoded] == [
        f"{pk._LIFT_CEILING:.1f}", f"{pk._LIFT_CEILING - 1.3:.1f}"]
    # At -2.0 as measured (to 0.1 dB): not under, made again.
    out, encoded, _ = _lift_run(monkeypatch, [
        _loudness(-33.3), _loudness(-21.2), _loudness(-20.0), _loudness(-20.0, true_peak=-2.0),
        _loudness(-20.2, true_peak=-2.5)])
    assert out is not None and len(encoded) == 2 and "limit=-4.5dB" in encoded[1]
    # Over again: the rendition made before stays.
    out, _, _ = _lift_run(monkeypatch, [
        _loudness(-33.3), _loudness(-21.2), _loudness(-20.0), _loudness(-20.0, true_peak=-1.2),
        _loudness(-20.0, true_peak=-1.9)])
    assert out is None


def test_the_fallback_has_a_limiter_its_lift_is_not_retimed(monkeypatch) -> None:
    encoded: list[str] = []
    measured = [_loudness(-30.0), _loudness(-21.0), _loudness(-20.0),
                _loudness(-20.1, true_peak=-2.5)]
    monkeypatch.setattr(pk, "_measure_loudness", lambda *_a, **_kw: measured.pop(0))
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing",
                        lambda label, args: encoded.append(args[args.index("-af") + 1]))
    monkeypatch.setattr(pk, "_audio_timing", lambda path, stream: TIMING)
    out = pk._lift_one(2, _s(6, None), Path("/t/stereo2.nut"), Path("/t/transmux.mp4"), -20.0)
    assert out == Path("/t/stereo2.mp4")
    assert encoded == [pk._lift_filter(10.0, -20.0, 1.0)]


def test_a_rendition_without_a_limiter_is_lifted_retimed(monkeypatch) -> None:
    encoded: list[str] = []
    measured = [_loudness(-30.0), _loudness(-21.0), _loudness(-20.0),
                _loudness(-20.1, true_peak=-2.5)]
    monkeypatch.setattr(pk, "_measure_loudness", lambda *_a, **_kw: measured.pop(0))
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing",
                        lambda label, args: encoded.append(args[args.index("-af") + 1]))
    monkeypatch.setattr(pk, "_audio_timing", lambda path, stream: TIMING)
    out = pk._lift_one(1, _s(2, "stereo"), Path("/t/stereo1.nut"), Path("/t/transmux.mp4"), -20.0)
    assert out == Path("/t/stereo1.mp4")
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
    out, encoded, _ = _lift_run(monkeypatch, [
        _loudness(-33.3), _loudness(-21.2), _loudness(-20.0), _loudness(-20.1, true_peak=-2.4)],
        timing={**TIMING, "start_pts": 0})
    assert out is None and len(encoded) == 1


@pytest.mark.parametrize(("measured", "encodes"), [
    ([_loudness(-24.0)], 0),                  # loud enough: -19.35 as made
    ([None], 0),                              # not measured
    ([_loudness(-33.3), None], 0),            # the compressor's output not measured
    ([_loudness(-33.3), _loudness(-21.2), None], 0),
    ([_loudness(-33.3), _loudness(-21.2), _loudness(-20.0), None], 1),  # nor the AAC
])
def test_no_lift_leaves_the_rendition(monkeypatch, measured: list, encodes: int) -> None:
    out, encoded, _ = _lift_run(monkeypatch, list(measured))
    assert out is None and len(encoded) == encodes


def test_every_signal_is_removed_and_a_failed_lift_keeps_its_rendition(
        monkeypatch, tmp_path: Path) -> None:
    signals = {i: tmp_path / f"stereo{i}.nut" for i in range(3)}
    for path in signals.values():
        path.write_bytes(b"x")

    def lift_one(i, stream, signal, mp4, target):
        if i == 1:
            raise pk.PackageError("ffmpeg stereo 1 exited 1")
        return signal.with_suffix(".mp4") if i == 0 else None

    monkeypatch.setattr(pk, "_lift_one", lift_one)
    probe = _probe([_s(2, "stereo")] * 3)
    assert pk._lift_stereo(probe, signals, tmp_path / "transmux.mp4", -20.0) == {
        0: tmp_path / "stereo0.mp4"}
    assert not any(p.exists() for p in signals.values())


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
