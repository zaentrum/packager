"""Real ffmpeg runs of the stereo downmix and of the 5.1 companion's fold
(skipped unless ffmpeg and ffprobe are on PATH, 5.1 or newer: the limiter's
delay compensation).

A loud film: an explosion every 1.5 s in every channel at once, no channel
over -1 dBFS, and a dialogue band in the centre besides, as a 5.1(side), a
7.1 and a 5.1 (FLAC), as a 5.1 ffprobe names no layout for (PCM in
Matroska), a stereo AAC track, a 6.1 (FLAC) and a 7.1 ffprobe names no
layout for (PCM). The plain matrix the stereo rendition had goes over full
scale on it; the packager's stereo renditions of the surround tracks stay
under -1 dBFS as a player decodes them, those of the unnamed layouts too
(ffmpeg's matrix normalised, then the limiter), every one as long as the
plain matrix's, beside their 5.1 companions; the stereo track's rendition is
what the filter it always had makes of it, sample for sample. ffmpeg's own
fold of the 7.1 into its 5.1 companion goes over full scale too; the
packager's companions of the 7.1 and the 6.1 and the unnamed 7.1 stay under
-1 dBFS in every channel as the E-AC-3 is decoded, every companion as long
as before and starting where it did; those of the 5.1 tracks are what the format they
always had makes of them, sample for sample. The layouts the downmix and the
fold know by name are ffmpeg's, and ffmpeg runs every fold; the fallback is
ffmpeg's normalised fold where the limiter doesn't act, and each fold puts
every source channel where it belongs, on the sample. The stereo fallback,
too, is ffmpeg's normalised matrix where the limiter doesn't act, and a
stereo under -1 dBFS as the AAC of a full-scale track is decoded."""

from __future__ import annotations

import array
import json
import math
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

SECONDS = 6
H = math.sqrt(0.5)
CHANNELS = {"5.1(side)": "FL FR FC LFE SL SR", "7.1": "FL FR FC LFE BL BR SL SR",
            "5.1": "FL FR FC LFE BL BR", "6.1": "FL FR FC LFE BC SL SR"}


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _loud(layout: str) -> str:
    """The loud signal in `layout`, a lavfi graph to [a]: low-passed pink
    noise in bursts (one every 1.5 s, decaying over 0.25 s) in every channel
    at once, 300-3400 Hz noise at four syllables a second in the centre
    besides, held under -1 dBFS in every channel, as a mastered track is."""
    names = CHANNELS[layout].split()
    centre = names.index("FC")
    return ";".join([
        f"anoisesrc=color=pink:seed=3:duration={SECONDS},lowpass=f=5000[noise]",
        f"aevalsrc=exprs='exp(-mod(t,1.5)/0.25)':sample_rate=48000:duration={SECONDS}[decay]",
        f"[noise][decay]amultiply,volume=2,asplit={len(names)}"
        + "".join(f"[b{i}]" for i in range(len(names))),
        f"anoisesrc=color=white:seed=5:duration={SECONDS},highpass=f=300,lowpass=f=3400[band]",
        f"aevalsrc=exprs='0.25*(0.5+0.5*sin(2*PI*4*t))':sample_rate=48000"
        f":duration={SECONDS}[syllables]",
        "[band][syllables]amultiply[voice]",
        f"[b{centre}][voice]amix=inputs=2:normalize=0[c{centre}]",
        "".join(f"[c{i}]" if i == centre else f"[b{i}]" for i in range(len(names)))
        + f"join=inputs={len(names)}:channel_layout={layout}:map="
        + "|".join(f"{i}.0-{name}" for i, name in enumerate(names))
        + ",alimiter=limit=-1dB:level=disabled:latency=enabled[a]",
    ])


@pytest.fixture(scope="module")
def film(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("downmix")
    tracks = []
    for i, (layout, codec) in enumerate([("5.1(side)", "flac"), ("7.1", "flac"),
                                         ("5.1", "flac"), ("5.1", "pcm_s16le")]):
        tracks.append(d / f"a{i}.mka")
        _ff("-filter_complex", _loud(layout), "-map", "[a]", "-c:a", codec, str(tracks[-1]))
    tracks.append(d / "stereo.mka")
    _ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.5:seed=9:duration={SECONDS}",
        "-ac", "2", "-c:a", "aac", "-b:a", "256k", str(tracks[-1]))
    for i, (layout, codec) in enumerate([("6.1", "flac"), ("7.1", "pcm_s16le")]):
        tracks.append(d / f"b{i}.mka")
        _ff("-filter_complex", _loud(layout), "-map", "[a]", "-c:a", codec, str(tracks[-1]))
    args = ["-f", "lavfi", "-i", f"color=c=black:s=64x64:r=24:d={SECONDS}"]
    for t in tracks:
        args += ["-i", str(t)]
    args += ["-map", "0:v", *[a for i in range(len(tracks)) for a in ("-map", f"{i + 1}:a")],
             "-c:v", "mpeg4", "-c:a", "copy"]
    for i, language in enumerate(["eng", "ger", "fre", "ita", "spa", "por", "dut"]):
        args += [f"-metadata:s:a:{i}", f"language={language}"]
    _ff(*args, str(d / "film.mkv"))
    return d / "film.mkv"


@pytest.fixture(scope="module")
def prepared(film: Path, tmp_path_factory) -> Path:
    """The packager's remux of the film: the seven stereo renditions, then
    the six surround tracks' 5.1 companions, encoded beside them (asplit)."""
    probe = pk._ffprobe(film)
    assert [(s["channels"], s.get("channel_layout")) for s in probe.audio] == [
        (6, "5.1(side)"), (8, "7.1"), (6, "5.1"), (6, None), (2, "stereo"), (7, "6.1"), (8, None)]
    surround = pk._surround_plan(probe.audio, set(range(7)), pk.PackageOptions())
    assert [(s.source_index, s.mode) for s in surround] == [
        (i, "encode") for i in (0, 1, 2, 3, 5, 6)]
    mp4, _meta, _surround = pk._prepare_source(film, probe, tmp_path_factory.mktemp("prepared"),
                                               surround=surround)
    return mp4


def _stats(path: Path, stream: str) -> tuple[float, int]:
    """The highest sample of a stream as decoded (dBFS) and how many samples
    it has."""
    err = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-map", stream,
                          "-af", "astats=measure_perchannel=none", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    peak = re.findall(r"Peak level dB: (\S+)", err)[-1]
    samples = re.findall(r"Number of samples: (\d+)", err)[-1]
    return float(peak), int(samples)


def _encode(film: Path, stream: str, chain: str, out: Path) -> Path:
    """A stream of the film encoded as the stereo rendition is, through `chain`."""
    _ff("-i", str(film), "-filter_complex", f"[{stream}]{chain}[s]", "-map", "[s]",
        "-c:a", "aac", "-b:a", "192k", str(out))
    return out


def _decoded_md5(path: Path, stream: str) -> str:
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", stream,
                           "-c:a", "pcm_f32le", "-f", "md5", "-"],
                          capture_output=True, text=True, check=True).stdout.strip()


# The 5.1 companions in the remux, after the seven stereo renditions: the
# source track each is made of.
COMPANIONS = {0: "0:a:7", 1: "0:a:8", 2: "0:a:9", 3: "0:a:10", 5: "0:a:11", 6: "0:a:12"}


def _encode_surround(film: Path, track: int, chain: str, out: Path) -> Path:
    """A track of the film encoded as its 5.1 companion is, through `chain`."""
    _ff("-i", str(film), "-filter_complex", f"[0:a:{track}]{chain}[m]", "-map", "[m]",
        "-c:a", "eac3", "-b:a", "448k", "-movflags", "+faststart", str(out))
    return out


def _channel_peaks(path: Path, stream: str) -> list[float]:
    """Every channel's highest sample as decoded (dBFS), in the stream's order."""
    err = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-map", stream,
                          "-af", "astats=measure_overall=none", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    return [float(x) for x in re.findall(r"Peak level dB: (\S+)", err)]


def _timing(path: Path, stream: str) -> tuple[str, str, int]:
    """A stream's start and duration as the MP4 has them, and how many
    samples it decodes to."""
    index = int(stream.rsplit(":", 1)[1])
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"a:{index}",
                          "-show_entries", "stream=start_time,duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    [s] = json.loads(out)["streams"]
    return s["start_time"], s["duration"], _stats(path, stream)[1]


def _standard_layouts() -> dict[str, str]:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-layouts"], capture_output=True, text=True,
                         check=True).stdout
    return dict(line.split() for line in out.split("Standard channel layouts:")[1]
                .splitlines()[2:] if line.strip())


def test_the_downmix_knows_ffmpegs_layouts() -> None:
    standard = _standard_layouts()
    assert {name: standard.get(name) for name in pk._DOWNMIX_LAYOUTS} == pk._DOWNMIX_LAYOUTS


def test_the_fold_knows_ffmpegs_layouts() -> None:
    standard = _standard_layouts()
    assert ({name: standard.get(name) for name in pk._SURROUND_FOLD_LAYOUTS}
            == pk._SURROUND_FOLD_LAYOUTS)


@pytest.mark.parametrize("layout", sorted(pk._SURROUND_FOLD_LAYOUTS))
def test_ffmpeg_runs_every_fold_under_the_ceiling(layout: str, tmp_path: Path) -> None:
    # A full-scale burst in every channel at once: ffmpeg's own fold goes
    # over full scale, the packager's stays at the limiter's -3 dBFS, a 5.1.
    names = pk._SURROUND_FOLD_LAYOUTS[layout].split("+")
    src = (f"aevalsrc=exprs='{'|'.join(['sin(2*PI*220*t)*lt(mod(t,0.5),0.2)'] * len(names))}'"
           f":channel_layout={layout}:sample_rate=48000:duration=1.5")
    peaks = {}
    for label, chain in [("ffmpeg", pk._SURROUND_FORMAT),
                         ("fold", pk._surround_filter({"channels": len(names),
                                                       "channel_layout": layout}))]:
        out = tmp_path / f"{label}.wav"
        _ff("-f", "lavfi", "-i", src, "-af", chain, "-c:a", "pcm_f32le", str(out))
        peaks[label] = _channel_peaks(out, "0:a:0")
        assert len(peaks[label]) == 6
    assert max(peaks["ffmpeg"]) > 0, peaks
    assert max(peaks["fold"]) <= -2.99, peaks


def test_a_loud_surround_track_stays_under_full_scale(film: Path, prepared: Path,
                                                      tmp_path: Path) -> None:
    # The plain matrix, which the stereo rendition had: over full scale.
    plain, plain_samples = _stats(
        _encode(film, "0:a:0", pk._STEREO_FORMAT, tmp_path / "plain.mp4"), "0:a:0")
    assert plain > 0
    # The 5.1(side), the 7.1, the 5.1 and the 6.1, and the 5.1 and the 7.1
    # ffprobe names no layout for (ffmpeg's matrix normalised, then the
    # limiter): under -1 dBFS.
    stats = [_stats(prepared, f"0:a:{i}") for i in (0, 1, 2, 5, 3, 6)]
    assert all(peak <= -1.0 for peak, _ in stats), stats
    # The limiter's delay is compensated: as many samples as the plain
    # matrix's, nothing lost at the end, nothing added at the start.
    assert {samples for _, samples in stats} == {plain_samples}
    # Each companion is a 5.1 still.
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=codec_name,channels", "-of", "json", str(prepared)],
                         capture_output=True, text=True, check=True).stdout
    assert [(s["codec_name"], s["channels"]) for s in json.loads(out)["streams"]] == (
        [("aac", 2)] * 7 + [("eac3", 6)] * 6)


def test_a_stereo_tracks_rendition_is_as_before(film: Path, prepared: Path,
                                                tmp_path: Path) -> None:
    before = _encode(film, "0:a:4", "aformat=sample_rates=48000:channel_layouts=stereo",
                     tmp_path / "before.mp4")
    assert _decoded_md5(prepared, "0:a:4") == _decoded_md5(before, "0:a:0")


def test_a_loud_tracks_5_1_companion_stays_under_full_scale(film: Path, prepared: Path,
                                                            tmp_path: Path) -> None:
    # ffmpeg's own fold of the 7.1, which the companion had: over full scale.
    plain = _channel_peaks(_encode_surround(film, 1, pk._SURROUND_FORMAT,
                                            tmp_path / "plain.mp4"), "0:a:0")
    assert max(plain) > 0, plain
    # The 7.1's and the 6.1's companions, and the unnamed 7.1's (ffmpeg's
    # matrix normalised, then the limiter): every channel under -1 dBFS as
    # the E-AC-3 is decoded. Six channels each, every one of them sounding.
    peaks = {track: _channel_peaks(prepared, COMPANIONS[track]) for track in (1, 5, 6)}
    assert all(len(p) == 6 and min(p) > -40 for p in peaks.values()), peaks
    assert all(peak <= -1.0 for p in peaks.values() for peak in p), peaks


def test_a_5_1_tracks_companion_is_as_before(film: Path, prepared: Path, tmp_path: Path) -> None:
    # The 5.1(side), the 5.1 and the unnamed 5.1: the format they always had,
    # sample for sample.
    for track in (0, 2, 3):
        before = _encode_surround(film, track, "aformat=sample_rates=48000:channel_layouts="
                                  "5.1(side)|5.1", tmp_path / f"before{track}.mp4")
        assert _decoded_md5(prepared, COMPANIONS[track]) == _decoded_md5(before, "0:a:0"), track


def test_every_companions_timing_is_as_before(film: Path, prepared: Path,
                                              tmp_path: Path) -> None:
    # The fold's limiter delay is compensated: every companion starts where
    # the format alone has it start, lasts as long and has as many samples.
    for track, stream in COMPANIONS.items():
        before = _encode_surround(film, track, pk._SURROUND_FORMAT, tmp_path / f"t{track}.mp4")
        assert _timing(prepared, stream) == _timing(before, "0:a:0"), track


def test_the_fallback_folds_as_ffmpeg_does_and_stays_under_full_scale(tmp_path: Path) -> None:
    # A 7.1 ffprobe names no layout for (PCM in Matroska), the same noise in
    # every channel at once: quiet, the fallback is ffmpeg's matrix normalised
    # sample for sample (the limiter doesn't act); at full scale that matrix
    # goes over full scale as the E-AC-3 is decoded, the fallback stays under
    # -1 dBFS; a 5.1(side) either way.
    plain = "aresample=48000:rematrix_maxval=1," + pk._SURROUND_FORMAT
    fallback = pk._surround_filter({"channels": 8})
    for level, quiet in ((0.05, True), (0.99, False)):
        src = tmp_path / f"src{level}.mka"
        _ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:seed=3:duration=3,lowpass=f=5000,"
            f"alimiter=limit=0.0625:level=disabled,volume={level / 0.0625},"
            "pan=7.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0|SL=c0|SR=c0",
            "-c:a", "pcm_s16le", str(src))
        assert pk._ffprobe(src).audio[0].get("channel_layout") is None
        encoded = {}
        for label, chain in (("plain", plain), ("fallback", fallback)):
            if quiet:
                wav = _ffwav(src, chain, tmp_path / f"{label}.wav")
                encoded[label] = _decoded_md5(wav, "0:a:0")
                continue
            out = tmp_path / f"{label}.mp4"
            _ff("-i", str(src), "-af", chain, "-c:a", "eac3", "-b:a", "448k", str(out))
            encoded[label] = _channel_peaks(out, "0:a:0")
        if quiet:
            assert encoded["plain"] == encoded["fallback"]
        else:
            assert len(encoded["fallback"]) == 6, encoded
            assert max(encoded["plain"]) > 0, encoded
            assert max(encoded["fallback"]) <= -1.0, encoded


def _ffwav(src: Path, chain: str, out: Path) -> Path:
    _ff("-i", str(src), "-af", chain, "-c:a", "pcm_f32le", str(out))
    return out


# What one sample of a source channel becomes in each channel of the 5.1(side)
# companion (FL FR FC LFE SL SR): a channel the 5.1 has, and a back pair where
# there is no side pair, at 3 dB under full; a back pair beside the side pair,
# a back centre and a front of centre at 6 dB under, into the channels it is
# folded into; nothing anywhere else, and nothing early or late.
ROUTES = {
    "7.1": {"FL": {"FL": H}, "FR": {"FR": H}, "FC": {"FC": H}, "LFE": {"LFE": H},
            "BL": {"SL": 0.5}, "BR": {"SR": 0.5}, "SL": {"SL": H}, "SR": {"SR": H}},
    "6.1": {"FL": {"FL": H}, "FR": {"FR": H}, "FC": {"FC": H}, "LFE": {"LFE": H},
            "BC": {"SL": 0.5, "SR": 0.5}, "SL": {"SL": H}, "SR": {"SR": H}},
    "7.1(wide)": {"FL": {"FL": H}, "FR": {"FR": H}, "FC": {"FC": H}, "LFE": {"LFE": H},
                  "BL": {"SL": H}, "BR": {"SR": H}, "FLC": {"FL": 0.5, "FC": 0.5},
                  "FRC": {"FR": 0.5, "FC": 0.5}},
}


@pytest.mark.parametrize("layout", sorted(ROUTES))
def test_the_fold_puts_every_channel_where_it_belongs(layout: str, tmp_path: Path) -> None:
    # One sample of 0.25 in each source channel in turn, a quarter of a
    # second apart (under the limiter's ceiling: it doesn't act).
    names = pk._SURROUND_FOLD_LAYOUTS[layout].split("+")
    at = [48000 + 12000 * i for i in range(len(names))]
    src = tmp_path / "impulses.mka"
    _ff("-f", "lavfi", "-i", f"aevalsrc=exprs='{'|'.join(f'0.25*eq(n,{n})' for n in at)}'"
        f":channel_layout={layout}:sample_rate=48000:duration=3", "-c:a", "flac", str(src))
    raw = tmp_path / "out.f32"
    chain = pk._surround_filter({"channels": len(names), "channel_layout": layout})
    _ff("-i", str(src), "-af", chain, "-c:a", "pcm_f32le", "-f", "f32le", str(raw))
    samples = array.array("f")
    samples.frombytes(raw.read_bytes())
    out = ("FL", "FR", "FC", "LFE", "SL", "SR")
    for name, n in zip(names, at, strict=True):
        for j, channel in enumerate(out):
            want = 0.25 * ROUTES[layout][name].get(channel, 0.0)
            window = [samples[(n + k) * 6 + j] for k in range(-600, 601)]
            assert abs(window[600] - want) < 0.002, (name, channel, window[600], want)
            # nothing but the one sample: the limiter's delay is compensated
            assert max(abs(v) for k, v in enumerate(window) if k != 600) < 0.002, (name, channel)


def _layout(path: Path) -> list[tuple[str, int, str]]:
    """Every audio stream's codec, channels and layout, as ffprobe has them."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=codec_name,channels,channel_layout", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return [(s["codec_name"], s["channels"], s.get("channel_layout", ""))
            for s in json.loads(out)["streams"]]


def test_the_stereo_fallback_downmixes_as_ffmpeg_does_and_stays_under_full_scale(
        tmp_path: Path) -> None:
    # A 5.1 ffprobe names no layout for (PCM in Matroska), the same noise in
    # every channel: quiet, the fallback is ffmpeg's matrix normalised sample
    # for sample (the limiter doesn't act); at full scale that matrix reaches
    # full scale as the AAC is decoded (over it, by the noise: +0.35 dBFS on
    # this one, 8.1), the fallback stays under -1 dBFS; a stereo either way,
    # as long as the matrix alone.
    plain = f"aresample=48000:rematrix_maxval=1,{pk._STEREO_FORMAT}"
    fallback = pk._stereo_filter({"channels": 6})
    assert fallback == pk._stereo_filter({"channels": 6, "channel_layout": "unknown"}) != plain
    for level, quiet in ((0.05, True), (1.0, False)):
        src = tmp_path / f"src{level}.mka"
        _ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:seed=3:duration=3,lowpass=f=5000,"
            f"alimiter=limit=0.0625:level=disabled,volume={level / 0.0625},"
            "pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0", "-c:a", "pcm_s16le", str(src))
        assert [(s["channels"], s.get("channel_layout")) for s in pk._ffprobe(src).audio] == [
            (6, None)]
        if quiet:
            wavs = [_ffwav(src, chain, tmp_path / f"{label}.wav")
                    for label, chain in (("plain", plain), ("fallback", fallback))]
            assert _decoded_md5(wavs[0], "0:a:0") == _decoded_md5(wavs[1], "0:a:0")
            assert [s[:2] for s in _layout(wavs[1])] == [("pcm_f32le", 2)]  # WAV: no mask
            continue
        encoded = {label: _encode(src, "0:a:0", chain, tmp_path / f"{label}.mp4")
                   for label, chain in (("plain", plain), ("fallback", fallback))}
        assert _layout(encoded["fallback"]) == [("aac", 2, "stereo")]
        (plain_peak, plain_samples), (peak, samples) = (_stats(encoded[k], "0:a:0")
                                                        for k in ("plain", "fallback"))
        assert plain_peak > -1.0 >= peak, (plain_peak, peak)
        assert samples == plain_samples
