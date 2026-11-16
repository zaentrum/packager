"""The picture and the sound of a package together, with real ffmpeg and
shaka-packager (skipped where they are not on PATH).

A clip shows a white frame and sounds a beep at the same instants, every
2.002 s. In a Matroska file with B-frames (libx264 -bf 3) its video starts at
the B-frames' delay (83 ms) after its first decode and its audio, which is at
the file's start: Matroska keeps presentation times alone, to the
millisecond. The packager's remux of it is an MP4 with an empty edit of that
delay and an edit from media_time = that delay; shaka-packager, at its
default, adds the two, takes the 0 for a missing edit list and moved the
video that much earlier (the picture 83 ms before the sound). Packaged as is
and on the transcoder's contract, from that Matroska file, from the MP4 it was
copied from, and from a clip without B-frames, every flash is within one
frame of its beep, as ffmpeg presents the package (its edit lists applied)."""

from __future__ import annotations

import array
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from packager import packager as pk
from packager.renditions import resolve_inputs

pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe", "packager")),
    reason="needs ffmpeg, ffprobe and shaka-packager on PATH",
)

ITEM = "c0ffee00-0000-4000-8000-0000000000b1"
RATE = 24000 / 1001
FRAME = 1 / RATE
FLASHES = 4  # one every 48 frames (2.002 s) from the 48th


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _beep(shift: int) -> str:
    """Stereo 48 kHz: a 40 ms 1 kHz beep from sample 96096k - `shift`, k = 1 to
    FLASHES."""
    on = (f"gte(n+{shift}\\,96096)*lt(n+{shift}\\,{96096 * (FLASHES + 1)})"
          f"*lt(mod(n+{shift}\\,96096)\\,1920)")
    tone = f"0.5*sin(2*PI*1000*mod(n+{shift}\\,96096)/48000)"
    return f"aevalsrc=exprs='if({on}\\,{tone}\\,0)|if({on}\\,{tone}\\,0)':s=48000:d=10"


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, Path]:
    """The clips, every flash at PTS 2.002k s and its beep at the same time:
    `mp4-bf3`, one encode, its video presented from the B-frames' delay (two
    frames, -copyts) after its first decode at 0, the AAC's priming cut by its
    edit list; `mkv-bf3`, that video copied into Matroska with the AAC encoded
    again 1024 samples earlier and muxed without a codec delay (decoded, its
    beep is where the MP4's is); `mkv-bf0`, no B-frames, the video at 0."""
    d = tmp_path_factory.mktemp("avsync")
    out = {}
    for bf, delay in ((3, 2), (0, 0)):
        n = f"n+{delay}"
        flash = f"eq(mod({n}\\,48)\\,0)*gte({n}\\,48)*lt({n}\\,{48 * FLASHES + 1})"
        mp4 = d / f"bf{bf}.mp4"
        _ff("-copyts", "-f", "lavfi", "-i",
            f"color=c=black:s=320x240:r=24000/1001:d=10,format=yuv420p,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='{flash}',setpts=PTS+{delay}",
            "-f", "lavfi", "-i", _beep(0), "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-bf", str(bf), "-g", "48",
            "-keyint_min", "48", "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k",
            "-movie_timescale", "48000", str(mp4))
        _ff("-f", "lavfi", "-i", _beep(1024), "-c:a", "aac", "-b:a", "128k", str(d / "a.m4a"))
        mkv = d / f"bf{bf}.mkv"
        _ff("-copyts", "-i", str(mp4), "-ignore_editlist", "1", "-i", str(d / "a.m4a"),
            "-map", "0:v", "-map", "1:a", "-c", "copy", str(mkv))
        out[f"mp4-bf{bf}"], out[f"mkv-bf{bf}"] = mp4, mkv
    return out


def _package(src: Path, tmp_path: Path, monkeypatch, contract: bool) -> Path:
    """The clip packaged as is, or on the transcoder's contract (v0 the
    original's own stream, its timestampOffset the file's start as the
    transcoder writes it)."""
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    inputs = None
    if contract:
        inbox = tmp_path / "_inbox" / ITEM
        inbox.mkdir(parents=True)
        start = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0",
             str(src)], capture_output=True, text=True, check=True).stdout.strip() or 0)
        (inbox / "renditions.json").write_text(json.dumps({
            "version": 1, "segmentSeconds": 6, "keyframes": "source", "timestampOffset": -start,
            "video": [{"id": "v0", "label": "source", "file": None, "mode": "copy"}]}))
        inputs = resolve_inputs(inbox, str(src))
    pk.package_item(ITEM, str(src), "movie", title="clip", inputs=inputs, trickplay=False,
                    options=pk.PackageOptions(surround_codec="off", stereo_loudness=None))
    return tmp_path / "packages" / "movies" / ITEM[:2] / ITEM / "hls"


def _joined(rendition: Path) -> Path:
    joined = rendition.parent / f"{rendition.name}.mp4"
    joined.write_bytes(b"".join(p.read_bytes() for p in [
        rendition / "init.mp4", *sorted(rendition.glob("seg-*.m4s"))]))
    return joined


def _flashes(path: Path) -> list[float]:
    """When each white frame is presented: the first frame brighter than mid
    grey after one that isn't, its timestamp as ffmpeg presents the file."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-copyts", "-i", str(path),
         "-map", "0:v:0", "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
         "-fps_mode", "passthrough", "-f", "null", "-"],
        capture_output=True, text=True, check=True).stdout
    frames, pts = [], None
    for line in out.splitlines():
        if m := re.search(r"pts_time:([-\d.]+)", line):
            pts = float(m.group(1))
        if (m := re.search(r"YAVG=([\d.]+)", line)) and pts is not None:
            frames.append((pts, float(m.group(1))))
    return [t for (t, y), (_, before) in zip(frames[1:], frames, strict=False)
            if y > 128 >= before]


def _beeps(path: Path) -> list[float]:
    """When each beep starts: its first sample over a fifth of full scale after
    half a second under it, counted from the first decoded sample's time."""
    first = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "frame=pts_time",
         "-read_intervals", "%+#64", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.split()[0]
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-copyts", "-i", str(path),
         "-map", "0:a:0", "-af", "pan=mono|c0=c0", "-ar", "48000", "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    samples = array.array("f", raw)
    out, last = [], -48000
    for i, x in enumerate(samples):
        if abs(x) > 0.1:
            if i - last > 24000:
                out.append(float(first.strip(",")) + i / 48000)
            last = i
    return out


@pytest.mark.parametrize("contract", [False, True], ids=["as-is", "contract"])
@pytest.mark.parametrize("clip", ["mkv-bf3", "mp4-bf3", "mkv-bf0"])
def test_every_flash_is_within_a_frame_of_its_beep(clips: dict[str, Path], tmp_path: Path,
                                                   monkeypatch, clip: str, contract: bool) -> None:
    src = clips[clip]
    if clip == "mkv-bf3":
        # The shape: the first frame presented two frames after the first
        # decode and the audio, both at the file's start.
        video = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=start_time", "-of", "json", str(src)],
            capture_output=True, text=True, check=True).stdout)["streams"][0]
        assert float(video["start_time"]) == pytest.approx(2 * FRAME, abs=0.001)
    hls = _package(src, tmp_path, monkeypatch, contract)
    flashes, beeps = _flashes(_joined(hls / "v0")), _beeps(_joined(hls / "a0"))
    assert len(flashes) == len(beeps) == FLASHES, (flashes, beeps)
    apart = [round((f - b) * 1000, 1) for f, b in zip(flashes, beeps, strict=True)]
    assert all(abs(ms) <= FRAME * 1000 for ms in apart), apart
