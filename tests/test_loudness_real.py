"""Real ffmpeg runs of the stereo rendition's loudness (skipped unless ffmpeg
and ffprobe are on PATH, 5.1 or newer: the limiter's delay compensation;
the packaged check needs shaka-packager too).

A quiet film: a dialogue band at four syllables a second, under a burst of
low-passed pink noise every 8 s in every channel whose peaks are 30 dB over
the mix's loudness, as a 5.1(side) (FLAC, -29.8 LUFS as its stereo
rendition was made), as a stereo AAC track (-34.1 LUFS) and as a 5.1
ffprobe names no layout for (PCM in Matroska: ffmpeg's matrix normalised,
then the limiter), and a loud stereo AAC track (pink noise) beside them.
The packager's stereo renditions of the three quiet tracks are lifted to
-20 LUFS (+-1 LU), their true peaks under -2 dBTP as a player decodes them,
stereo, each as long as the rendition made before and from the same first
sample, on the MP4's timeline and on the remux's offset alike, from signals
of FLAC (24 bits, stereo, on the MP4's timeline); the 5.1 companions and
the loud track's rendition are what they were, sample for sample; a track
no client offers is made as before. Packaged, the lifted renditions'
segments are cut at the same instants as before. A track whose timestamps
are not contiguous (a container's rounding) is lifted with its timing: a
stereo one its first sample and its samples, its length to the rounding,
where the limiter, given those timestamps, puts the rest 10 ms late; a
downmixed one, whose rendition the limiter has put 10 ms late already, as
its rendition, packet for packet. Every rendition of a track that isn't
lifted, and the one in the remux of a track that is, is what it was
without the loudness, packet for packet: a loud mono AAC track, a hot
stereo AAC one at 44.1 kHz, a 2.1 and a stereo FLAC one at 44.1 kHz, a
quiet mono one. Two quiet tracks a client is offered neither of: the one
the master makes the default is lifted, and counted. A film whose title tag
is Latin-1 bytes is packaged, its quiet track lifted; a lift that runs over
its timeout is killed and leaves nothing behind.

The temp folder's free space is a node's as the guard sees it (this
machine's disk may be fuller than its floor)."""

from __future__ import annotations

import json
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


@pytest.fixture(scope="module", autouse=True)
def temp_space():
    """A node's 256 GB as the guard sees the temp folder, 200 GB of it free."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shutil, "disk_usage",
                   lambda path: shutil._ntuple_diskusage(256 * 10**9, 56 * 10**9, 200 * 10**9))
        yield


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
    _ff("-filter_complex", _quiet("FL FR FC LFE SL SR".split(), 0.15), "-map", "[a]",
        "-c:a", "pcm_s16le", str(d / "unnamed.mka"))
    _ff("-f", "lavfi", "-i", f"color=c=black:s=64x64:r=24:d={SECONDS}",
        "-i", str(d / "surround.mka"), "-i", str(d / "stereo.mka"), "-i", str(d / "loud.mka"),
        "-i", str(d / "unnamed.mka"),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a", "-map", "4:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-c:a", "copy",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=ger",
        "-metadata:s:a:2", "language=fre", "-metadata:s:a:3", "language=ita",
        str(d / "film.mkv"))
    return d / "film.mkv"


QUIET = (0, 1, 3)  # the loud track: 2
STEREO, COMPANIONS = ["0:a:0", "0:a:1", "0:a:2", "0:a:3"], ["0:a:4", "0:a:5"]


def _prepare(film: Path, d: Path, target: float | None, timeline: str = "normalize",
             ts_offset: float = 0.0, shown: set[int] | None = None) -> tuple[Path, list[dict]]:
    probe = pk._ffprobe(film)
    assert [(s["channels"], s.get("channel_layout")) for s in probe.audio] == [
        (6, "5.1(side)"), (2, "stereo"), (2, "stereo"), (6, None)]
    surround = pk._surround_plan(probe.audio, {0, 1, 2, 3}, pk.PackageOptions())
    assert [(s.source_index, s.mode) for s in surround] == [(0, "encode"), (3, "encode")]
    mp4, meta, _surround = pk._prepare_source(film, probe, d, surround=surround, timeline=timeline,
                                              ts_offset=ts_offset, stereo_loudness=target,
                                              audio_visible_indices=shown)
    return mp4, meta


def _streams(path: Path) -> list[tuple[str, int, str]]:
    """Every audio stream's codec, channels and layout, as ffprobe has them."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=codec_name,channels,channel_layout", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return [(c, int(n), layout) for c, n, layout in
            (line.split(",")[:3] for line in out.splitlines() if line)]


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
    assert [i for i, path in enumerate(lifted) if path is not None] == list(QUIET)
    # Every shown track says what became of it: the loud one is kept.
    assert [m.get("_lift") for m in meta] == ["lift", "lift", "keep", "lift"]
    for i in QUIET:
        # Quiet as the renditions were made before ...
        assert _r128(before, f"0:a:{i}")["I"] < -28
        # ... at the target now, its true peak under -2 dBTP, decoded, a stereo.
        r = _r128(lifted[i])
        assert r["I"] == pytest.approx(-20.0, abs=1.0), r
        assert r["TP"] <= -2.0, r
        assert _streams(lifted[i]) == [("aac", 2, "stereo")]


def test_a_lifted_rendition_keeps_its_timing(runs) -> None:
    before, _ = runs["before"]
    _, meta = runs["after"]
    for i in QUIET:
        # From the same first sample, as long, as many samples.
        assert _timing(meta[i]["_input"], "a:0") == _timing(before, f"a:{i}")


def test_the_companion_and_a_loud_tracks_rendition_are_as_before(runs) -> None:
    before, _ = runs["before"]
    after, meta = runs["after"]
    # The remux itself is as it was: every stereo rendition made as before
    # (the loud track's is the one packaged) and the 5.1 companions.
    for stream in STEREO + COMPANIONS:
        assert _decoded_md5(after, stream) == _decoded_md5(before, stream), stream
    assert "_input" not in meta[2]
    assert _streams(after) == _streams(before) == (
        [("aac", 2, "stereo")] * 4 + [("eac3", 6, "5.1(side)")] * 2)


def test_the_signals_are_flac_of_24_bits_on_the_mp4s_timeline(film: Path, tmp_path: Path,
                                                              monkeypatch) -> None:
    seen = {}
    lift = pk._lift_stereo

    def probe_then_lift(probe, signals, mp4, target):
        for i, signal in signals.items():
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                  "stream=codec_name,sample_fmt,bits_per_raw_sample,channels,"
                                  "channel_layout,start_pts,time_base", "-of", "json", str(signal)],
                                 capture_output=True, text=True, check=True).stdout
            seen[i] = json.loads(out)["streams"][0]
        return lift(probe, signals, mp4, target)

    monkeypatch.setattr(pk, "_lift_stereo", probe_then_lift)
    mp4, _meta = _prepare(film, tmp_path, -20.0, "offset", 0.021)
    assert sorted(seen) == [0, 1, 2, 3]
    for i, s in seen.items():
        assert (s["codec_name"], s["sample_fmt"], s["bits_per_raw_sample"], s["channels"],
                s["channel_layout"], s["time_base"]) == (
            "flac", "s32", "24", 2, "stereo", "1/48000"), s
        # From the rendition's first sample: its first packet's, 1024 samples
        # of AAC priming on (the remux's offset, 1008 samples).
        first = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"a:{i}",
                                "-show_entries", "packet=pts", "-read_intervals", "%+#1",
                                "-of", "csv=p=0", str(mp4)], capture_output=True, text=True,
                               check=True).stdout.split(",")[0]
        assert int(s["start_pts"]) == int(first) + 1024 == 1008
    assert not list(tmp_path.glob("*.nut"))


def test_a_track_no_client_offers_is_made_as_before(film: Path, tmp_path: Path) -> None:
    (tmp_path / "before").mkdir()
    before, _ = _prepare(film, tmp_path / "before", None)
    mp4, meta = _prepare(film, tmp_path, -20.0, shown={0, 2})
    assert [m.get("_input") is not None for m in meta] == [True, False, False, False]
    for stream in STEREO[1:]:
        assert _decoded_md5(mp4, stream) == _decoded_md5(before, stream), stream


@pytest.mark.skipif(shutil.which("packager") is None, reason="needs shaka-packager on PATH")
def test_a_lifted_rendition_is_packaged_on_the_segments_it_had(
        film: Path, tmp_path: Path, monkeypatch) -> None:
    roots, stereo = {}, {}
    for label, target in (("before", None), ("after", -20.0)):
        monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / label)
        stereo[label] = pk.StereoLoudness()
        manifest = pk.package_item(ITEM, str(film), "movie", title="clip", trickplay=False,
                                   options=pk.PackageOptions(stereo_loudness=target),
                                   stereo=stereo[label])
        roots[label] = tmp_path / label / "movies" / ITEM[:2] / ITEM
        assert not any(k.startswith("_") for a in manifest["renditions"]["audio"] for k in a)
    # What the done step's details say of it: three lifted, the loud one kept.
    assert stereo == {"before": pk.StereoLoudness(known=True, off=True),
                      "after": pk.StereoLoudness(known=True, lifted=3, kept=1, skipped=0)}
    for a in ("a0", "a1", "a2", "a3", "a4", "a5"):
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


@pytest.mark.parametrize("layout", ["stereo", "5.1(side)"])
def test_a_track_whose_timestamps_are_rounded_keeps_its_timing(tmp_path: Path, monkeypatch,
                                                               layout: str) -> None:
    # A quiet track, every seventh frame of it 20 samples late (as a
    # container's rounding has it, not adding up), its rendition and its
    # signal made by the remux's split.
    names = {"stereo": ["FL", "FR"], "5.1(side)": "FL FR FC LFE SL SR".split()}[layout]
    src = tmp_path / "src.nut"
    _ff("-filter_complex", _quiet(names, 0.05 if layout == "stereo" else 0.15)
        + ";[a]asetnsamples=n=1024:p=0,asetpts='N+20*eq(mod(N/1024,7),3)'[s]", "-map", "[s]",
        "-c:a", "pcm_f32le", "-f", "nut", str(src))
    stream = {"codec_name": "pcm_f32le", "channels": len(names), "channel_layout": layout,
              "tags": {"language": "eng"}}
    before, signal = tmp_path / "before.mp4", tmp_path / "stereo0.nut"

    def remux() -> None:
        split = pk._stereo_split(stream, 0)
        _ff("-copyts", "-i", str(src), "-filter_complex", f"[0:a:0]{split}[s0]",
            "-map", "[s0]", "-c:a", "aac", "-b:a", "192k", str(before),
            "-map", "[l0]", *pk._SIGNAL_CODEC, "-f", "nut", str(signal))

    remux()
    if layout == "stereo":
        # Its rendition made without a limiter: lifted without the timestamps
        # counted from the first sample, 10 ms late after the first late
        # frame, and not kept.
        with monkeypatch.context() as mp:
            mp.setattr(pk, "_LIFT_RETIME", "anull")
            assert pk._lift_one(0, stream, signal, before, -20.0) == (pk.SKIPPED, None)
            late = pk._audio_timing(tmp_path / "stereo0.mp4", "a:0")
            assert late is not None and int(late["duration_ts"]) - int(
                pk._audio_timing(before, "a:0")["duration_ts"]) > 400
    kind, lifted = pk._lift_one(0, stream, signal, before, -20.0)
    assert kind == pk.LIFTED and _streams(lifted) == [("aac", 2, "stereo")]
    start, duration, samples = _timing(lifted, "a:0")
    start_before, duration_before, samples_before = _timing(before, "a:0")
    assert (start, samples) == (start_before, samples_before)
    assert float(duration) == pytest.approx(float(duration_before), abs=0.002)
    assert _r128(lifted)["I"] == pytest.approx(-20.0, abs=1.0)
    if layout != "stereo":
        # The downmix's limiter put the rendition 10 ms late at the first late
        # frame; its lift is, packet for packet.
        assert _packets(lifted) == _packets(before)


def _packets(path: Path) -> list[str]:
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                           "packet=pts,duration", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, check=True).stdout.split()


def _framemd5(path: Path, stream: str) -> list[str]:
    """A stream's packets, as they are (framemd5 of its copy)."""
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", stream,
                          "-c", "copy", "-f", "framemd5", "-"], capture_output=True, text=True,
                         check=True).stdout
    return [line for line in out.splitlines() if not line.startswith("#")]


@pytest.fixture(scope="module")
def loud_film(tmp_path_factory) -> Path:
    """Loud tracks kept as they are (a mono AAC, a hot stereo AAC at 44.1 kHz,
    a 2.1 FLAC, a stereo FLAC at 44.1 kHz) and a quiet mono AAC, lifted."""
    d = tmp_path_factory.mktemp("identity")
    hot = ("anoisesrc=color=pink:amplitude=0.9:seed={s}:duration=8:sample_rate={r},"
           "alimiter=limit=0.99:level=disabled,volume=1.5")
    tracks = [(hot.format(s=1, r=48000), ["-ac", "1", "-c:a", "aac", "-b:a", "128k"]),
              (hot.format(s=2, r=44100), ["-ac", "2", "-c:a", "aac", "-b:a", "256k"]),
              ("anoisesrc=color=pink:amplitude=0.5:seed=3:duration=8:sample_rate=48000,"
               "pan=2.1|FL=c0|FR=c0|LFE=c0", ["-c:a", "flac"]),
              ("anoisesrc=color=pink:amplitude=0.5:seed=4:duration=8:sample_rate=44100",
               ["-ac", "2", "-c:a", "flac"]),
              ("anoisesrc=color=pink:amplitude=0.02:seed=5:duration=8:sample_rate=48000",
               ["-ac", "1", "-c:a", "aac", "-b:a", "128k"])]
    args = ["-f", "lavfi", "-i", "color=c=black:s=64x64:r=24:d=8"]
    for n, (graph, codec) in enumerate(tracks):
        _ff("-f", "lavfi", "-i", graph, *codec, str(d / f"t{n}.mka"))
        args += ["-i", str(d / f"t{n}.mka")]
    _ff(*args, "-map", "0:v", *[a for n in range(len(tracks)) for a in ("-map", f"{n + 1}:a")],
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy",
        *[a for n, lang in enumerate(["eng", "ger", "fre", "ita", "spa"])
          for a in (f"-metadata:s:a:{n}", f"language={lang}")], str(d / "loud.mkv"))
    return d / "loud.mkv"


def test_every_rendition_is_what_it_was_without_the_loudness(loud_film: Path,
                                                             tmp_path: Path) -> None:
    probe = pk._ffprobe(loud_film)
    assert [(s["codec_name"], s["channels"], int(s["sample_rate"])) for s in probe.audio] == [
        ("aac", 1, 48000), ("aac", 2, 44100), ("flac", 3, 48000), ("flac", 2, 44100),
        ("aac", 1, 48000)]
    runs = {}
    for label, target in (("before", None), ("after", -20.0)):
        (tmp_path / label).mkdir()
        runs[label] = pk._prepare_source(loud_film, probe, tmp_path / label,
                                         audio_visible_indices=None, default_index=0,
                                         stereo_loudness=target)
    (before, _, _), (after, meta, _) = runs["before"], runs["after"]
    # The loud ones kept, the quiet mono lifted ...
    assert [m.get("_lift") for m in meta] == ["keep", "keep", "keep", "keep", "lift"]
    assert _streams(meta[4]["_input"]) == [("aac", 2, "stereo")]
    # ... and every rendition of the remux as it was, packet for packet.
    assert _streams(after) == _streams(before) == [("aac", 2, "stereo")] * 5
    for stream in [f"0:a:{n}" for n in range(5)]:
        assert _framemd5(after, stream) == _framemd5(before, stream), stream


def _quiet_stereo(path: Path, seed: int) -> None:
    _ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.15:seed={seed}:duration=12",
        "-ac", "2", "-c:a", "aac", "-b:a", "192k", str(path))


@pytest.mark.skipif(shutil.which("packager") is None, reason="needs shaka-packager on PATH")
def test_the_default_track_is_lifted_though_no_client_is_offered_it(tmp_path: Path,
                                                                    monkeypatch) -> None:
    # French and Italian, a whitelist of English and German: no track shown,
    # and the master's default plays all the same.
    for n in (1, 2):
        _quiet_stereo(tmp_path / f"q{n}.mka", n)
    film = tmp_path / "film.mkv"
    _ff("-f", "lavfi", "-i", "color=c=black:s=64x64:r=24:d=12", "-i", str(tmp_path / "q1.mka"),
        "-i", str(tmp_path / "q2.mka"), "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy",
        "-metadata:s:a:0", "language=fre", "-metadata:s:a:1", "language=ita", str(film))
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    stereo = pk.StereoLoudness()
    manifest = pk.package_item(ITEM, str(film), "movie", title="clip", trickplay=False,
                               language_whitelist=["en", "de"], keep_original_if_single=True,
                               options=pk.PackageOptions(stereo_loudness=-20.0), stereo=stereo)
    audio = manifest["renditions"]["audio"]
    assert [(a["language"], a["visible"], a["default"]) for a in audio] == [
        ("fre", False, True), ("ita", False, False)]
    assert stereo.details == " st_lift=1 st_keep=0 st_skip=0"
    root = tmp_path / "packages" / "movies" / ITEM[:2] / ITEM
    master = (root / "hls" / "master.m3u8").read_text()
    assert 'LANGUAGE="fr"' in master and "DEFAULT=YES,AUTOSELECT=YES" in master
    joined = tmp_path / "a0.mp4"
    joined.write_bytes(b"".join(p.read_bytes() for p in [
        root / "hls" / "a0" / "init.mp4", *sorted((root / "hls" / "a0").glob("seg-*.m4s"))]))
    assert _r128(joined)["I"] == pytest.approx(-20.0, abs=1.0)


def test_a_title_tag_of_latin_1_bytes_is_packaged_and_lifted(tmp_path: Path) -> None:
    # The title as an old muxer wrote it: Latin-1 bytes, not UTF-8.
    _quiet_stereo(tmp_path / "q.mka", 1)
    utf8 = tmp_path / "utf8.mkv"
    _ff("-f", "lavfi", "-i", "color=c=black:s=64x64:r=24:d=12", "-i", str(tmp_path / "q.mka"),
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy",
        "-metadata", "title=Caf\u00e9X", "-metadata:s:a:0", "language=eng", str(utf8))
    data = utf8.read_bytes()
    film = tmp_path / "latin1.mkv"
    film.write_bytes(data.replace(b"Caf\xc3\xa9X", b"Caf\xe9XX", 1))
    assert b"Caf\xe9XX" in film.read_bytes()
    # ffmpeg's log of the film itself holds those bytes: read, all the same.
    assert pk._measure_loudness(film) is not None
    probe = pk._ffprobe(film)
    mp4, meta, _ = pk._prepare_source(film, probe, tmp_path, default_index=0,
                                      stereo_loudness=-20.0)
    assert meta[0].get("_lift") == "lift"
    assert _r128(meta[0]["_input"])["I"] == pytest.approx(-20.0, abs=1.0)


def test_a_lift_that_runs_over_its_timeout_is_killed_and_leaves_nothing(
        film: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pk, "_LIFT_TIMEOUT", 0.0001)
    monkeypatch.setattr(pk, "_LIFT_TIMEOUT_PER_SECOND", 0.0)
    _mp4, meta = _prepare(film, tmp_path, -20.0)
    # Every quiet track's lift ran over: made as before, counted skipped.
    assert [m.get("_lift") for m in meta] == ["skip", "skip", "skip", "skip"]
    assert not any("_input" in m for m in meta)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["transmux.mp4"]
