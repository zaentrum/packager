"""The package's origin with real ffmpeg (and shaka-packager where a test
packages; skipped where they are not on PATH): the first video frame at the
package's 0 on every ffmpeg version, whatever the version reads before it.

ffmpeg 7.1 reads a Matroska AAC track ffmpeg muxed from its codec delay
before 0 (the file from -0.021 s), 9.0 from 0; an MPEG-TS file's audio
priming is before its first frame on both. Packaged as is, B-frames and
all, such a clip's video starts at 0 and its cues at their times after the
first frame; on the contract's timeline its styled script's events and
cues are at their times after it, as its video is. A quiet track that
starts before the first frame is lifted with its rendition's timing. A
transport stream whose clock jumps is packaged as long as it plays."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_package_real import _mapped_cues, _segment_starts

from packager import packager as pk

pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe")),
    reason="needs ffmpeg and ffprobe on PATH",
)
needs_shaka = pytest.mark.skipif(shutil.which("packager") is None,
                                 reason="needs shaka-packager on PATH")

ITEM = "c0ffee00-0000-4000-8000-0000000000a0"
SCRIPT = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 240\n\n[V4+ Styles]\n"
          "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
          "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
          "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
          "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,"
          "0,0,1,1,0,2,10,10,10,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
          "MarginR, MarginV, Effect, Text\n"
          "Dialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,At two\n"
          "Dialogue: 0,0:00:07.50,0:00:08.50,Default,,0,0,0,,At seven and a half\n")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _starts(path: Path) -> dict[str, float]:
    """The file's start and its first video stream's, as ffprobe reads them."""
    out = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=start_time:stream=codec_type,start_time", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout)
    video = next(s for s in out["streams"] if s["codec_type"] == "video")
    return {"file": float(out["format"]["start_time"]), "video": float(video["start_time"])}


def _first_pts(path: Path, stream: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                          "packet=pts_time", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return min(float(t.strip(",")) for t in out.split())


def _clip(d: Path, container: str, *, seconds: int = 12, start: float = 0.0,
          quiet: bool = False) -> Path:
    """H.264 with B-frames and a keyframe every 2 s, stereo AAC (quiet pink
    noise, or a tone), and in a Matroska clip an ASS track: its first frame
    at `start`, every stream with it."""
    d.mkdir(parents=True, exist_ok=True)
    audio = (f"anoisesrc=color=pink:amplitude=0.02:seed=5:duration={seconds}:sample_rate=48000"
             if quiet else f"sine=frequency=440:sample_rate=48000:duration={seconds}")
    subs: list[str] = []
    if container == "mkv":
        (d / "script.ass").write_text(SCRIPT)
        subs = ["-i", str(d / "script.ass")]
    src = d / f"clip.{container}"
    _ff("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", audio, *subs, "-map", "0:v", "-map", "1:a",
        *(["-map", "2", "-c:s", "copy", "-metadata:s:s:0", "language=eng"] if subs else []),
        "-c:v", "libx264", "-preset", "ultrafast", "-bf", "2", "-g", "50",
        "-c:a", "aac", "-ac", "2", "-metadata:s:a:0", "language=eng",
        *(["-output_ts_offset", str(start)] if start else []), str(src))
    return src


@needs_shaka
@pytest.mark.parametrize("container", ["mkv", "ts"])
def test_a_package_made_as_is_starts_its_video_at_0(tmp_path: Path, monkeypatch,
                                                    container: str) -> None:
    src = _clip(tmp_path / "clip", container)
    starts = _starts(src)
    # The TS's muxer delay, its frames' reordering after it.
    assert starts["video"] > 1.0 if container == "ts" else starts["video"] == 0.0
    # The file starts before its first frame: the TS's audio priming, and
    # 7.1's reading of the Matroska AAC track's codec delay.
    assert starts["file"] < starts["video"] if container == "ts" else (
        starts["file"] <= starts["video"])
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    manifest = pk.package_item(ITEM, str(src), "movie", title="clip", trickplay=False,
                               options=pk.PackageOptions(hls_subtitles=True, surround_codec="off",
                                                         stereo_loudness=None))
    root = tmp_path / "packages" / "movies" / ITEM[:2] / ITEM
    assert _segment_starts(root / "hls" / "v0") == pytest.approx([0.0, 6.0], abs=0.001)
    if container == "mkv":
        # Its cues at their times after the first frame, and its script's.
        assert [(n, round(t, 3), text) for n, t, text in _mapped_cues(root / "hls" / "s0")] == [
            (1, 2.0, "At two"), (2, 7.5, "At seven and a half")]
        assert manifest["subtitles"][0]["styled"] == {"path": "subs/0.ass", "format": "ass"}
        assert _dialogue_times(root / "subs" / "0.ass") == [
            ("0:00:02.00", "0:00:03.00"), ("0:00:07.50", "0:00:08.50")]


def _dialogue_times(script: Path) -> list[tuple[str, str]]:
    return [tuple(line.split(",")[1:3])  # type: ignore[misc]
            for line in script.read_text().splitlines() if line.startswith("Dialogue:")]


def _cue_times(vtt: Path) -> list[str]:
    return [line.split(" --> ")[0] for line in vtt.read_text().splitlines() if " --> " in line]


def test_on_the_contracts_timeline_the_script_and_the_cues_keep_their_times(
    tmp_path: Path,
) -> None:
    # A Matroska original whose first frame is at 0.5 s, moved onto the
    # contract's timeline (timestampOffset -0.5): its video at 0, its
    # script's events and its cues at their times after the first frame,
    # whatever the file's start is read as.
    src = _clip(tmp_path / "clip", "mkv", start=0.5)
    assert _starts(src)["video"] == pytest.approx(0.5, abs=0.001)
    probe = pk._ffprobe(src)
    subs = tmp_path / "subs"
    [entry] = pk._extract_subtitles(src, probe, subs, timeline="offset", ts_offset=-0.5)
    assert entry["styled"] == {"path": "subs/0.ass", "format": "ass"}
    assert _dialogue_times(subs / "0.ass") == [
        ("0:00:02.00", "0:00:03.00"), ("0:00:07.50", "0:00:08.50")]
    assert _cue_times(subs / "0.vtt") == ["00:02.000", "00:07.500"]
    mp4, _meta, _surround = pk._prepare_source(src, probe, tmp_path, timeline="offset",
                                               ts_offset=-0.5)
    assert _first_pts(mp4, "v:0") == pytest.approx(0.0, abs=0.001)


@pytest.mark.parametrize("container", ["mkv", "ts"])
def test_a_quiet_track_that_starts_before_the_first_frame_is_lifted_on_its_timing(
    tmp_path: Path, monkeypatch, container: str,
) -> None:
    # Its rendition's priming before 0; its signal, which NUT can't hold
    # there, written that much later and the lift moved back: the lift is
    # kept, with its rendition's timing (_same_timing).
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda path: shutil._ntuple_diskusage(256 * 10**9, 56 * 10**9, 200 * 10**9))
    src = _clip(tmp_path / "clip", container, seconds=8, quiet=True)
    probe = pk._ffprobe(src)
    work = tmp_path / "work"
    work.mkdir()
    mp4, meta, _surround = pk._prepare_source(src, probe, work, default_index=0,
                                              stereo_loudness=-20.0)
    assert meta[0]["_lift"] == pk.LIFTED
    lifted = meta[0]["_input"]
    assert pk._same_timing(pk._audio_timing(lifted, "a:0"), pk._audio_timing(mp4, "a:0"))
    assert _first_pts(lifted, "a:0") == pytest.approx(_first_pts(mp4, "a:0"), abs=0.0001)
    assert _first_pts(mp4, "v:0") == pytest.approx(0.0, abs=0.001)


def test_a_transport_stream_whose_clock_jumps_is_packaged_as_long_as_it_plays(
    tmp_path: Path,
) -> None:
    # Two 4 s recordings, the second's clock 50 minutes on: ffmpeg's
    # normalisation makes it continuous (under -copyts it came out 50
    # minutes long).
    parts = []
    for n, clock in enumerate((10, 3000)):
        part = tmp_path / f"part{n}.ts"
        _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-bf", "0", "-g", "25", "-c:a", "aac",
            "-output_ts_offset", str(clock), "-f", "mpegts", str(part))
        parts.append(part.read_bytes())
    src = tmp_path / "jumps.ts"
    src.write_bytes(b"".join(parts))
    mp4, _meta, _surround = pk._prepare_source(src, pk._ffprobe(src), tmp_path)
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=duration", "-of", "csv=p=0", str(mp4)],
                         capture_output=True, text=True, check=True).stdout
    assert float(re.findall(r"[\d.]+", out)[0]) == pytest.approx(8.0, abs=0.1)
    assert _first_pts(mp4, "v:0") == pytest.approx(0.0, abs=0.001)
