"""Real ffmpeg runs of the stereo downmix (skipped unless ffmpeg and ffprobe
are on PATH, 5.1 or newer: the limiter's delay compensation).

A loud film: an explosion every 1.5 s in every channel at once, no channel
over -1 dBFS, and a dialogue band in the centre besides, as a 5.1(side), a
7.1 and a 5.1 (FLAC), as a 5.1 ffprobe names no layout for (PCM in
Matroska), and a stereo AAC track. The plain matrix the stereo rendition had
goes over full scale on it; the packager's stereo renditions of the surround
tracks stay under -1 dBFS as a player decodes them, the one of the unnamed
layout under full scale, every one as long as the plain matrix's, beside
their 5.1 companions; the stereo track's rendition is what the filter it
always had makes of it, sample for sample. The layouts the downmix knows by
name are ffmpeg's."""

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

SECONDS = 6
CHANNELS = {"5.1(side)": "FL FR FC LFE SL SR", "7.1": "FL FR FC LFE BL BR SL SR",
            "5.1": "FL FR FC LFE BL BR"}


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
    args = ["-f", "lavfi", "-i", f"color=c=black:s=64x64:r=24:d={SECONDS}"]
    for t in tracks:
        args += ["-i", str(t)]
    args += ["-map", "0:v", *[a for i in range(len(tracks)) for a in ("-map", f"{i + 1}:a")],
             "-c:v", "mpeg4", "-c:a", "copy"]
    for i, language in enumerate(["eng", "ger", "fre", "ita", "spa"]):
        args += [f"-metadata:s:a:{i}", f"language={language}"]
    _ff(*args, str(d / "film.mkv"))
    return d / "film.mkv"


@pytest.fixture(scope="module")
def prepared(film: Path, tmp_path_factory) -> Path:
    """The packager's remux of the film: the five stereo renditions, then the
    four surround tracks' 5.1 companions, encoded beside them (asplit)."""
    probe = pk._ffprobe(film)
    assert [(s["channels"], s.get("channel_layout")) for s in probe.audio] == [
        (6, "5.1(side)"), (8, "7.1"), (6, "5.1"), (6, None), (2, "stereo")]
    surround = pk._surround_plan(probe.audio, {0, 1, 2, 3, 4}, pk.PackageOptions())
    assert [(s.source_index, s.mode) for s in surround] == [
        (0, "encode"), (1, "encode"), (2, "encode"), (3, "encode")]
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


def test_the_downmix_knows_ffmpegs_layouts() -> None:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-layouts"], capture_output=True, text=True,
                         check=True).stdout
    standard = dict(line.split() for line in out.split("Standard channel layouts:")[1]
                    .splitlines()[2:] if line.strip())
    assert {name: standard.get(name) for name in pk._DOWNMIX_LAYOUTS} == pk._DOWNMIX_LAYOUTS


def test_a_loud_surround_track_stays_under_full_scale(film: Path, prepared: Path,
                                                      tmp_path: Path) -> None:
    # The plain matrix, which the stereo rendition had: over full scale.
    plain, plain_samples = _stats(
        _encode(film, "0:a:0", pk._STEREO_FORMAT, tmp_path / "plain.mp4"), "0:a:0")
    assert plain > 0
    # The 5.1(side), the 7.1 and the 5.1: under -1 dBFS; the 5.1 ffprobe
    # names no layout for (ffmpeg's matrix normalised): under full scale.
    stats = [_stats(prepared, f"0:a:{i}") for i in range(4)]
    assert all(peak <= -1.0 for peak, _ in stats[:3]), stats
    assert stats[3][0] <= 0, stats
    # The limiter's delay is compensated: as many samples as the plain
    # matrix's, nothing lost at the end, nothing added at the start.
    assert {samples for _, samples in stats} == {plain_samples}
    # Each companion is a 5.1 still.
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=codec_name,channels", "-of", "json", str(prepared)],
                         capture_output=True, text=True, check=True).stdout
    assert [(s["codec_name"], s["channels"]) for s in json.loads(out)["streams"]] == (
        [("aac", 2)] * 5 + [("eac3", 6)] * 4)


def test_a_stereo_tracks_rendition_is_as_before(film: Path, prepared: Path,
                                                tmp_path: Path) -> None:
    before = _encode(film, "0:a:4", "aformat=sample_rates=48000:channel_layouts=stereo",
                     tmp_path / "before.mp4")
    assert _decoded_md5(prepared, "0:a:4") == _decoded_md5(before, "0:a:0")
