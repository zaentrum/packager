"""Real ffmpeg runs of the stereo rendition's loudness (skipped unless ffmpeg
and ffprobe are on PATH, 5.1 or newer: the limiter's delay compensation;
the packaged check needs shaka-packager too).

A quiet film: a dialogue band at four syllables a second, under a burst of
low-passed pink noise every 8 s in every channel whose peaks are 30 dB over
the mix's loudness, as a 5.1(side) (FLAC, -29.8 LUFS as its stereo
rendition was made) and as a stereo AAC track (-34.1 LUFS), and a loud
stereo AAC track (pink noise) beside them. The packager's stereo
renditions of the two quiet tracks are lifted to -20 LUFS (+-1 LU), their
true peaks under -2 dBTP as a player decodes them, each as long as the
rendition made before and from the same first sample, on the MP4's
timeline and on the remux's offset alike; the 5.1 companion and the loud
track's rendition are what they were, sample for sample. Packaged, the
lifted renditions' segments are cut at the same instants as before. A
track whose timestamps are not contiguous (a container's rounding) is
lifted with its timing: its first sample and its samples, its length to
the rounding — where the limiter, given those timestamps, puts the rest
10 ms late."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from packager import packager as pk


def _ffmpeg_version() -> tuple[int, int]:
    """'ffmpeg version 7.1.5-0+deb13u1' / 'n7.1.5-…' / '8.1.2' -> (7, 1) / (7, 1) / (8, 1)."""
    first = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.split("\n", 1)[0]
    m = re.search(r"version n?(\d+)\.(\d+)", first)
    return (int(m.group(1)), int(m.group(2))) if m else (99, 0)  # git builds are new


pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe")) or _ffmpeg_version() < (5, 1),
    reason="needs ffmpeg and ffprobe 5.1 or newer on PATH",
)

SECONDS = 24
ITEM = "c0ffee00-0000-4000-8000-00000000001d"


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _quiet(names: list[str], dialogue: float) -> str:
    """The quiet film in the layout of `names`, a lavfi graph to [a]: the
    dialogue band (300-3400 Hz noise, four syllables a second, a pause
    every 5 s) in the centre, or in both sides of a stereo track, and a
    burst every 8 s (2 ms attack, 0.12 s decay) in every channel."""
    n = len(names)
    centre = names.index("FC") if "FC" in names else None
    graph = [
        f"anoisesrc=color=pink:seed=3:duration={SECONDS},lowpass=f=4000[noise]",
        f"aevalsrc=exprs='0.9*exp(-mod(t+4,8)/0.12)*(1-exp(-mod(t+4,8)/0.002))'"
        f":sample_rate=48000:duration={SECONDS}[decay]",
        f"[noise][decay]amultiply,asplit={n}" + "".join(f"[b{i}]" for i in range(n)),
        f"anoisesrc=color=white:seed=5:duration={SECONDS},highpass=f=300,lowpass=f=3400[band]",
        f"aevalsrc=exprs='{dialogue}*(0.5+0.5*sin(2*PI*4*t))*lt(mod(t,5),4)':sample_rate=48000"
        f":duration={SECONDS}[syllables]",
        "[band][syllables]amultiply" + ("[voice]" if centre is not None else ",asplit=2[v0][v1]"),
    ]
    if centre is not None:
        graph.append(f"[b{centre}][voice]amix=inputs=2:normalize=0[c{centre}]")
        voiced = {centre}
    else:
        graph += [f"[b{i}][v{i}]amix=inputs=2:normalize=0[c{i}]" for i in (0, 1)]
        voiced = {0, 1}
    layout = {6: "5.1(side)", 2: "stereo"}[n]
    graph.append("".join(f"[c{i}]" if i in voiced else f"[b{i}]" for i in range(n))
                 + f"join=inputs={n}:channel_layout={layout}:map="
                 + "|".join(f"{i}.0-{name}" for i, name in enumerate(names)) + "[a]")
    return ";".join(graph)


@pytest.fixture(scope="module")
def film(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("loudness")
    _ff("-filter_complex", _quiet("FL FR FC LFE SL SR".split(), 0.15), "-map", "[a]",
        "-c:a", "flac", str(d / "surround.mka"))
    _ff("-filter_complex", _quiet(["FL", "FR"], 0.05), "-map", "[a]",
        "-c:a", "aac", "-b:a", "256k", str(d / "stereo.mka"))
    _ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.5:seed=9:duration={SECONDS}",
        "-ac", "2", "-c:a", "aac", "-b:a", "256k", str(d / "loud.mka"))
    _ff("-f", "lavfi", "-i", f"color=c=black:s=64x64:r=24:d={SECONDS}",
        "-i", str(d / "surround.mka"), "-i", str(d / "stereo.mka"), "-i", str(d / "loud.mka"),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-c:a", "copy",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=ger",
        "-metadata:s:a:2", "language=fre", str(d / "film.mkv"))
    return d / "film.mkv"


def _prepare(film: Path, d: Path, target: float | None, timeline: str = "normalize",
             ts_offset: float = 0.0) -> tuple[Path, list[dict]]:
    probe = pk._ffprobe(film)
    assert [(s["channels"], s.get("channel_layout")) for s in probe.audio] == [
        (6, "5.1(side)"), (2, "stereo"), (2, "stereo")]
    surround = pk._surround_plan(probe.audio, {0, 1, 2}, pk.PackageOptions())
    assert [(s.source_index, s.mode) for s in surround] == [(0, "encode")]
    mp4, meta, _surround = pk._prepare_source(film, probe, d, surround=surround, timeline=timeline,
                                              ts_offset=ts_offset, stereo_loudness=target)
    return mp4, meta


@pytest.fixture(scope="module", params=[("normalize", 0.0), ("offset", 0.021)],
                ids=["normalize", "offset"])
def runs(request, film: Path, tmp_path_factory) -> dict[str, tuple[Path, list[dict]]]:
    """The remux of the film as before (no target) and with the loudness."""
    timeline, offset = request.param
    return {label: _prepare(film, tmp_path_factory.mktemp(label), target, timeline, offset)
            for label, target in (("before", None), ("after", -20.0))}


def _r128(path: Path, stream: str = "0:a:0") -> dict[str, float]:
    err = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-map", stream,
                          "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    summary = err.rpartition("Summary:")[2]
    return {"I": float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1)),
            "TP": float(re.search(r"True peak:\s+Peak:\s+(-?[\d.]+) dBFS", summary).group(1))}


def _timing(path: Path, stream: str) -> tuple[str, str, int]:
    """The stream's start and duration as its container has them, and how
    many samples it decodes to."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                          "stream=start_time,duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    start, duration = out.split(",")
    err = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-map",
                          f"0:{stream}", "-af", "astats=measure_perchannel=none", "-f", "null",
                          "-"], capture_output=True, text=True, check=True).stderr
    return start, duration, int(re.findall(r"Number of samples: (\d+)", err)[-1])


def _decoded_md5(path: Path, stream: str) -> str:
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", stream,
                           "-c:a", "pcm_f32le", "-f", "md5", "-"],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_a_quiet_mix_is_lifted_to_the_target_under_the_true_peak(runs) -> None:
    before, _ = runs["before"]
    _, meta = runs["after"]
    lifted = [m.get("_input") for m in meta]
    assert lifted[0] is not None and lifted[1] is not None and lifted[2] is None
    for i in (0, 1):
        # Quiet as the renditions were made before ...
        assert _r128(before, f"0:a:{i}")["I"] < -28
        # ... at the target now, its true peak under -2 dBTP, decoded.
        r = _r128(lifted[i])
        assert r["I"] == pytest.approx(-20.0, abs=1.0), r
        assert r["TP"] <= -2.0, r


def test_a_lifted_rendition_keeps_its_timing(runs) -> None:
    before, _ = runs["before"]
    _, meta = runs["after"]
    for i in (0, 1):
        # From the same first sample, as long, as many samples.
        assert _timing(meta[i]["_input"], "a:0") == _timing(before, f"a:{i}")


def test_the_companion_and_a_loud_tracks_rendition_are_as_before(runs) -> None:
    before, _ = runs["before"]
    after, meta = runs["after"]
    # The remux itself is as it was: every stereo rendition made as before
    # (the loud track's is the one packaged) and the 5.1 companion.
    for stream in ("0:a:0", "0:a:1", "0:a:2", "0:a:3"):
        assert _decoded_md5(after, stream) == _decoded_md5(before, stream), stream
    assert "_input" not in meta[2]


@pytest.mark.skipif(shutil.which("packager") is None, reason="needs shaka-packager on PATH")
def test_a_lifted_rendition_is_packaged_on_the_segments_it_had(
        film: Path, tmp_path: Path, monkeypatch) -> None:
    roots = {}
    for label, target in (("before", None), ("after", -20.0)):
        monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / label)
        pk.package_item(ITEM, str(film), "movie", title="clip", trickplay=False,
                        options=pk.PackageOptions(stereo_loudness=target))
        roots[label] = tmp_path / label / "movies" / ITEM[:2] / ITEM
    for a in ("a0", "a1", "a2"):
        playlists = [(roots[label] / "hls" / a / "playlist.m3u8").read_text()
                     for label in ("before", "after")]
        assert [re.findall(r"#EXTINF:([\d.]+)", p) for p in playlists[1:]] == [
            re.findall(r"#EXTINF:([\d.]+)", playlists[0])]
        starts = [_segment_starts(roots[label] / "hls" / a) for label in ("before", "after")]
        assert starts[1] == starts[0] and len(starts[0]) == 4
    # The lifted rendition is the one packaged.
    joined = tmp_path / "a1.mp4"
    joined.write_bytes(b"".join(p.read_bytes() for p in [
        roots["after"] / "hls" / "a1" / "init.mp4",
        *sorted((roots["after"] / "hls" / "a1").glob("seg-*.m4s"))]))
    assert _r128(joined)["I"] == pytest.approx(-20.0, abs=1.0)


def _segment_starts(rendition: Path) -> list[float]:
    """The first timestamp of every media segment of one rendition."""
    starts = []
    for seg in sorted(rendition.glob("seg-*.m4s")):
        joined = rendition / f".{seg.name}.mp4"
        joined.write_bytes((rendition / "init.mp4").read_bytes() + seg.read_bytes())
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=pts_time",
                              "-of", "csv=p=0", str(joined)], capture_output=True,
                             text=True).stdout
        joined.unlink()
        starts.append(min(float(t.split(",")[0]) for t in out.split()))
    return starts


def test_a_track_whose_timestamps_are_rounded_keeps_its_timing(tmp_path: Path,
                                                               monkeypatch) -> None:
    # A quiet stereo signal, every seventh frame of it 20 samples late (as a
    # container's rounding has it, not adding up), and its rendition as made
    # before: the format, no limiter.
    signal = tmp_path / "stereo0.nut"
    _ff("-filter_complex", _quiet(["FL", "FR"], 0.05) + ";[a]asetnsamples=n=1024:p=0,"
        "asetpts='N+20*eq(mod(N/1024,7),3)'[s]", "-map", "[s]", "-c:a", "pcm_f32le",
        "-f", "nut", str(signal))
    before = tmp_path / "before.mp4"
    _ff("-copyts", "-i", str(signal), "-af", pk._STEREO_FORMAT, "-c:a", "aac", "-b:a", "192k",
        str(before))
    stream = {"codec_name": "aac", "channels": 2, "channel_layout": "stereo",
              "tags": {"language": "eng"}}
    # Without the timestamps counted from the first sample: 10 ms late
    # after the first late frame, and not kept.
    monkeypatch.setattr(pk, "_LIFT_RETIME", "anull")
    assert pk._lift_one(0, stream, signal, before, -20.0) is None
    late = pk._audio_timing(tmp_path / "stereo0.mp4", "a:0")
    assert late is not None and int(late["duration_ts"]) - int(
        pk._audio_timing(before, "a:0")["duration_ts"]) > 400
    monkeypatch.undo()
    lifted = pk._lift_one(0, stream, signal, before, -20.0)
    assert lifted is not None
    start, duration, samples = _timing(lifted, "a:0")
    start_before, duration_before, samples_before = _timing(before, "a:0")
    assert (start, samples) == (start_before, samples_before)
    assert float(duration) == pytest.approx(float(duration_before), abs=0.002)
    assert _r128(lifted)["I"] == pytest.approx(-20.0, abs=1.0)
