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
import struct
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


# ------------------------------------------- a subtitle before the first frame

EARLY_SRT = ("1\n00:00:00,100 --> 00:00:00,300\nBefore\n\n"
             "2\n00:00:00,200 --> 00:00:01,000\nAcross\n\n"
             "3\n00:00:02,000 --> 00:00:03,000\nAfter\n")
EARLY_EVENTS = [(0.1, 0.3, "Before"), (0.2, 1.0, "Across"), (2.0, 3.0, "After")]
# The PGS track's, one after the other: a display set shows until the next.
EARLY_PGS = [(0.1, 0.3), (0.4, 1.2), (2.0, 3.0)]


def _pgs_segment(seconds: float, kind: int, payload: bytes) -> bytes:
    t = round(seconds * 90_000)
    return b"PG" + struct.pack(">IIBH", t, t, kind, len(payload)) + payload


def _pgs_display(seconds: float, shown: bool) -> bytes:
    """A display set: a 16x8 box shown (an epoch start), or the screen cleared."""
    pcs = struct.pack(">HHBHBBBB", 320, 240, 0x10, 1, 0x80 if shown else 0x00, 0, 0,
                         int(shown)) + (struct.pack(">HBBHH", 0, 0, 0, 10, 10) if shown else b"")
    out = _pgs_segment(seconds, 0x16, pcs) + _pgs_segment(
        seconds, 0x17, struct.pack(">BBHHHH", 1, 0, 10, 10, 16, 8))
    if shown:
        rle = b"\x00\x90\x01\x00\x00" * 8
        out += _pgs_segment(seconds, 0x14, bytes([0, 0, 1, 235, 128, 128, 255]))
        out += _pgs_segment(seconds, 0x15, struct.pack(">HBB", 0, 0, 0xC0)
                            + (4 + len(rle)).to_bytes(3, "big") + struct.pack(">HH", 16, 8) + rle)
    return out + _pgs_segment(seconds, 0x80, b"")


def _signed(ticks: int) -> int:
    return ticks - (1 << 32) if ticks >= 1 << 31 else ticks


def _pgs_sets(path: Path) -> list[tuple[float, bool]]:
    """Every display set's time (its PCS's, signed) and whether it shows."""
    data, out, i = path.read_bytes(), [], 0
    while i < len(data):
        pts, _dts, kind, size = struct.unpack_from(">IIBH", data, i + 2)
        if kind == 0x16:
            out.append((round(_signed(pts) / 90_000, 3), data[i + 13 + 10] > 0))
        i += 13 + size
    return out


@pytest.fixture(scope="module", params=["mkv", "m2ts"])
def early(request, tmp_path_factory) -> Path:
    """A clip whose first frame is at 0.5 s (plus a transport stream's delay),
    its audio with it, and subtitle events before it: SRT and ASS (Matroska)
    and PGS (both) 'Before' it and gone by then, 'Across' it, 'After' it."""
    d = tmp_path_factory.mktemp(f"early-{request.param}")
    (d / "early.srt").write_text(EARLY_SRT)
    (d / "early.ass").write_text(SCRIPT.split("Dialogue:")[0] + "".join(
        f"Dialogue: 0,0:00:{a:05.2f},0:00:{b:05.2f},Default,,0,0,0,,{text}\n"
        for a, b, text in EARLY_EVENTS))
    (d / "early.sup").write_bytes(b"".join(
        _pgs_display(a, True) + _pgs_display(b, False) for a, b in EARLY_PGS))
    av = ["-itsoffset", "0.5", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=6",
          "-itsoffset", "0.5", "-f", "lavfi", "-i",
          "sine=frequency=440:sample_rate=48000:duration=6"]
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-bf", "0", "-g", "25", "-c:a", "aac"]
    src = d / f"early.{request.param}"
    if request.param == "mkv":
        _ff(*av, "-i", str(d / "early.srt"), "-i", str(d / "early.ass"), "-i", str(d / "early.sup"),
            "-map", "0:v", "-map", "1:a", "-map", "2", "-map", "3", "-map", "4", *enc,
            "-c:s:0", "srt", "-c:s:1", "copy", "-c:s:2", "copy", str(src))
    else:
        _ff(*av, "-i", str(d / "early.sup"), "-map", "0:v", "-map", "1:a", "-map", "2", *enc,
            "-c:s", "copy", "-f", "mpegts", "-mpegts_m2ts_mode", "1", str(src))
    return src


def _vtt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def _ass_time(seconds: float) -> str:
    cs = round(seconds * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


@pytest.mark.parametrize("timeline", ["normalize", "offset"])
def test_no_subtitle_event_is_before_0(early: Path, tmp_path: Path, timeline: str) -> None:
    # Packaged as is or moved onto the contract's timeline (the first frame
    # at 0): an event gone before the first frame is taken out, one across
    # it starts at 0 and keeps its end, the rest keep their times after it —
    # every WebVTT cue, every event of the script, every PGS display set;
    # no time before 0, no track moved.
    probe = pk._ffprobe(early)
    first = float(probe.video["start_time"])
    assert first > 0.5
    ts_offset = -first if timeline == "offset" else 0.0
    raw = tmp_path / "raw.sup"  # the PGS track as the source has it
    pgs = next(i for i, s in enumerate(probe.subtitles) if s["codec_name"] == "hdmv_pgs_subtitle")
    _ff("-copyts", "-i", str(early), "-map", f"0:s:{pgs}", "-c:s", "copy", "-f", "sup", str(raw))
    in_source = _pgs_sets(raw)
    assert sum(t < first for t, _ in in_source) == 3  # shown, cleared, shown: before it

    subs = tmp_path / "subs"
    entries = pk._extract_subtitles(early, probe, subs, timeline=timeline, ts_offset=ts_offset)
    assert _pgs_sets(subs / entries[pgs]["path"].removeprefix("subs/")) == [(0.0, True)] + [
        (round(t - first, 3), shown) for t, shown in in_source if t > first]
    kept = [(0.0, 1.0 - first, "Across"), (2.0 - first, 3.0 - first, "After")]
    for entry in entries:
        if entry["format"] == "webvtt":
            lines = (tmp_path / entry["path"]).read_text().splitlines()
            assert [(line, lines[n + 1]) for n, line in enumerate(lines) if " --> " in line] == [
                (f"{_vtt_time(a)} --> {_vtt_time(b)}", text) for a, b, text in kept]
        if entry.get("styled"):
            assert _dialogue_times(tmp_path / entry["styled"]["path"]) == [
                (_ass_time(a), _ass_time(b)) for a, b, _ in kept]
    assert [e["format"] for e in entries] == (
        ["webvtt", "webvtt", "pgs"] if early.suffix == ".mkv" else ["pgs"])
    mp4, _meta, _surround = pk._prepare_source(early, probe, tmp_path, timeline=timeline,
                                               ts_offset=ts_offset)
    assert _first_pts(mp4, "v:0") == pytest.approx(0.0, abs=0.001)


@needs_shaka
@pytest.mark.parametrize("early", ["mkv"], indirect=True)  # its cues are the Matroska clip's
def test_a_cue_kept_from_0_is_packaged_from_0(early: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    pk.package_item(ITEM, str(early), "movie", title="clip", trickplay=False,
                    options=pk.PackageOptions(hls_subtitles=True, surround_codec="off",
                                              stereo_loudness=None))
    root = tmp_path / "packages" / "movies" / ITEM[:2] / ITEM
    first = float(pk._ffprobe(early).video["start_time"])
    assert [(round(t, 3), text) for _n, t, text in _mapped_cues(root / "hls" / "s0")] == [
        (0.0, "Across"), (round(2.0 - first, 3), "After")]
