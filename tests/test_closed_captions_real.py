"""Closed captions through the packager, with real ffmpeg and
shaka-packager runs (skipped unless both are on PATH, ffmpeg has libx265
and it makes the films below as they are meant to be).

Closed captions (EIA/CEA-608/708) ride in the video stream itself: an A53
cc_data SEI with each picture, as broadcast and disc sources have them. A
copy of the stream keeps them, and so does an encode whose encoder writes
them (the transcoder's -a53cc 1); shaka-packager keeps the SEI in every
segment. The package's record said it carried none, whatever it carried,
so the deletion gate held every original recorded with them. Here films
with captions are packaged through the library v2 path, whose video is
HEVC, as a copy and on a transcoder's ladder encoded on the CPU (libx265
with -a53cc 1, and without it, which since FFmpeg 7.1 writes none), and
through the path that writes a manifest alone, which takes H.264 too (a
copy, libx264 with -a53cc 1 and with -a53cc 0): every rendition whose own
frames carry them says so, read from the rendition itself, the package's
essence has them when its top rendition has, and deleting an original
recorded with them costs nothing more. A rendition without them, and a
film without them, says none. A film whose captions start late — an
encoder may write them only where there is text — has none in its first
frames, so a rendition is read in windows across it."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_library_real import Catalog, _catalogs_part, _has_x265, _validate

from packager import libv2_records as rec
from packager import packager as pk
from packager import worker
from packager.katalog import ClaimedItem
from packager.renditions import resolve_inputs

pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe", "packager")) or not _has_x265(),
    reason="needs ffmpeg/ffprobe with libx265 and shaka-packager on PATH",
)

ITEM = "c0ffee00-0000-4000-8000-0000000000cc"
SOURCE = "5011ce00-0000-4000-8000-0000000000cc"
VERSION = "7e510000-0000-4000-8000-0000000000cc"
OPTIONS = pk.PackageOptions(surround_codec="off", stereo_loudness=None)

KEYS = "keyint=48:min-keyint=48:scenecut=0"
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-x264-params", KEYS]
X265 = ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", f"log-level=error:{KEYS}"]
FRAMES = 96  # 4 s at 24 fps
# cc_data as a broadcast carries it with each picture: process_cc_data_flag
# and two field-1 byte pairs, a caption's control codes in odd parity.
CC_DATA = bytes([0x40 | 2, 0xFF, 0xFC, 0x94, 0x2C, 0xFC, 0xC8, 0x49, 0xFF])
A53 = bytes([0xB5, 0x00, 0x31]) + b"GA94" + bytes([0x03]) + CC_DATA  # ITU-T T.35, ATSC


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _captioned(path: Path) -> tuple[int, int]:
    """(video frames, those whose SEI carries A53 closed captions) of a file
    or a media playlist, every frame of it decoded."""
    frames = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_entries",
         "frame=key_frame:frame_side_data=side_data_type", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout).get("frames") or []
    return len(frames), sum(1 for f in frames if any(
        x.get("side_data_type") == "ATSC A53 Part 4 Closed Captions"
        for x in f.get("side_data_list") or []))


def _with_captions(raw: Path, out: Path, first: int = 0) -> None:
    """An H.264 elementary stream at `raw` again at `out`, an A53 cc_data
    SEI (payload type 4) before the slice of each picture from the
    `first`-th on."""
    sei = b"\x00\x00\x00\x01" + bytes([0x06, 0x04, len(A53)]) + A53 + b"\x80"
    data, made, pos, picture = raw.read_bytes(), bytearray(), 0, 0
    for m in re.finditer(rb"\x00\x00\x01(.)", data, re.DOTALL):
        start = m.start() - 1 if m.start() and data[m.start() - 1] == 0 else m.start()
        if m.group(1)[0] & 0x1F in (1, 5):  # a slice: one per picture
            if picture >= first:
                made += data[pos:start] + sei
                pos = start
            picture += 1
    out.write_bytes(bytes(made + data[pos:]))


@pytest.fixture(scope="module")
def films(tmp_path_factory) -> dict[str, Path]:
    """4 s 24 fps films with an English stereo track, each in its arrival
    folder: H264 (H.264, captions in every frame's SEI), HEVC (encoded from
    it by libx265 with -a53cc 1, so in every frame too) and PLAIN (HEVC,
    none); LATE, 12 s of H.264 at 25 frames, a keyframe every 100, its
    captions from its 100th picture on; LATE_HEVC, encoded from it by
    libx265 with -a53cc 1 at the same keyframes, and LATE_LATER the same
    with its first picture at 10 s. Skips where this ffmpeg makes them
    otherwise."""
    d = tmp_path_factory.mktemp("captions")
    out: dict[str, Path] = {}
    picture = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=4"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4"]
    audio = ["-c:a", "aac", "-ac", "2", "-metadata:s:a:0", "language=eng"]

    def path(name: str) -> Path:
        p = d / f"{name} (2024)" / f"{name} (2024).mkv"
        p.parent.mkdir()
        out[name] = p
        return p

    raw = d / "plain.h264"
    _ff(*picture, *X264[:4], "-x264-params", f"{KEYS}:slices=1", "-f", "h264", str(raw))
    _with_captions(raw, d / "captioned.h264")
    _ff("-framerate", "24", "-i", str(d / "captioned.h264"), *tone, "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", *audio, str(path("H264")))
    _ff("-i", str(out["H264"]), "-map", "0", *X265, "-a53cc", "1", "-c:a", "copy",
        str(path("HEVC")))
    _ff(*picture, *tone, "-map", "0:v", "-map", "1:a", *X265, *audio, str(path("PLAIN")))
    late = d / "late.h264"
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=12", *X264[:4],
        "-x264-params", "slices=1:keyint=100:min-keyint=100:scenecut=0", "-f", "h264", str(late))
    _with_captions(late, d / "late-captioned.h264", first=100)
    _ff("-framerate", "25", "-i", str(d / "late-captioned.h264"), "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=12", "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", *audio, str(path("LATE")))
    late_x265 = ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
                 "log-level=error:keyint=100:min-keyint=100:scenecut=0:bframes=0", "-a53cc", "1"]
    _ff("-i", str(out["LATE"]), "-map", "0", *late_x265, "-c:a", "copy", str(path("LATE_HEVC")))
    _ff("-i", str(out["LATE"]), "-map", "0", *late_x265, "-c:a", "copy", "-output_ts_offset", "10",
        str(path("LATE_LATER")))
    made = {name: _captioned(p) for name, p in out.items()}
    want = {"H264": (FRAMES, FRAMES), "HEVC": (FRAMES, FRAMES), "PLAIN": (FRAMES, 0),
            "LATE": (300, 200), "LATE_HEVC": (300, 200), "LATE_LATER": (300, 200)}
    if made != want:
        pytest.skip(f"this ffmpeg makes other films than these tests need: {made}")
    return out


def _ladder(original: Path, inbox: Path, rungs: list[tuple[str, list[str]]]) -> None:
    """A transcoder's handoff of encoded rungs, each (encoder, its ffmpeg
    arguments): v0 at the original's size with its audio, the rest at 120p."""
    inbox.mkdir(parents=True)
    video = []
    for i, (encoder, args) in enumerate(rungs):
        name = "prepared.mkv" if i == 0 else f"v{i}.mkv"
        _ff("-i", str(original), "-map", "0:v", *(["-map", "0:a", "-c:a", "copy"] if i == 0
                                                   else ["-an", "-vf", "scale=160:120,setsar=1"]),
            *args, "-f", "matroska", str(inbox / name))
        video.append({"id": f"v{i}", "label": "source" if i == 0 else "120p", "file": name,
                      "mode": "encode", "encoder": encoder})
    (inbox / "renditions.json").write_text(json.dumps({
        "version": 1, "itemId": ITEM, "segmentSeconds": 6, "keyframes": "interval",
        "timestampOffset": 0.0, "video": video}))


def _v2(tmp_path: Path, film: Path,
        rungs: list[tuple[str, list[str]]] | None = None) -> tuple[Path, Path, dict, dict]:
    """One v2 run of film through the worker, from the original or on a
    transcoder's ladder of `rungs`, that is done: the library root, the
    version folder, the package record and the source record."""
    lib, work = tmp_path / "library", tmp_path / "work"
    arrival = work / "incoming" / film.parent.name
    shutil.copytree(film.parent, arrival)
    original = arrival / film.name
    item_dir = lib / "movies" / ITEM[:2] / ITEM
    record = {
        "id": ITEM, "type": "movie", "title": film.stem, "year": 2024, "path": str(original),
        "library": {
            "contract": 1, "root": str(lib), "itemDir": str(item_dir), "blocked": None,
            "source": {"sourceId": SOURCE, "recorded": False,
                       "recordDir": str(item_dir / "sources" / SOURCE),
                       "libraryPath": f"{film.parent.name}/{film.name}",
                       "sizeBytes": original.stat().st_size, "qh1": rec.qh1(str(original))},
            "inboxDir": str(work / "inbox" / ITEM),
            "build": {"versionId": VERSION, "stagingDir": str(work / "staging" / VERSION),
                      "versionDir": str(item_dir / "versions" / VERSION),
                      "createdBy": "katalog-manager", "chapters": None, "chaptersFrom": None,
                      "segments": None},
            "current": None,
        },
    }
    if rungs:
        _ladder(original, work / "inbox" / ITEM, rungs)
    catalog = Catalog()
    worker._process_one(ClaimedItem.from_json(record), catalog,  # type: ignore[arg-type]
                        OPTIONS)
    assert [s for s, _ in catalog.written] == ["in_progress", "done"], catalog.written
    vdir = item_dir / "versions" / VERSION
    package = json.loads((vdir / "package.json").read_text())
    source = json.loads((item_dir / "sources" / SOURCE / "source.json").read_text())
    assert catalog.handovers[0]["package"] == package
    assert rec.chain_problems(str(vdir), "version.json", rec.PACKAGE_DIRS, full=True) == []
    _catalogs_part(item_dir, title=film.stem)
    return lib, vdir, package, source


def _manifest(tmp_path: Path, film: Path,
              rungs: list[tuple[str, list[str]]] | None = None) -> tuple[Path, dict]:
    """One build of film as the path without a library writes it, its video
    H.264 or HEVC, from the original or on a ladder of `rungs`: the package
    folder and its manifest."""
    inbox, stage = tmp_path / "inbox", tmp_path / "stage"
    stage.mkdir(parents=True)
    if rungs:
        _ladder(film, inbox, rungs)
    built = pk.build_package(ITEM, str(film), "movie", stage,
                             inputs=resolve_inputs(inbox, str(film)), options=OPTIONS,
                             trickplay=False)
    return stage, built.manifest


def _picture(source: dict) -> dict:
    return next(x for x in source["streams"] if x["type"] == "video"
                and not x["dispositions"].get("attachedPic"))


def _shown(windows: list[dict]) -> list[tuple[float, bool, bool]]:
    """(at, read, captions) of each window."""
    return [(w["at"], w["frames"] > 0, w["captions"]) for w in windows]


def _gate(source: dict, package: dict) -> list[str]:
    """The deletion gate, as a reader computes it from the records."""
    return rec.deletion_gate([source["essence"]], package["essence"],
                             copied=rec.top_copied(package),
                             carried=rec.captions_carried([source], package))


ALL = [(at, True, True) for at in rec.CAPTION_WINDOWS]
NONE = [(at, True, False) for at in rec.CAPTION_WINDOWS]
LATE = [(0.1, True, False), (0.3, True, False), (0.5, True, True), (0.7, True, True),
        (0.9, True, True)]


def _says(renditions: dict) -> list[tuple[str, str, bool | None]]:
    return [(v["id"], v.get("encoder"), v.get("closedCaptions")) for v in renditions["video"]]


def test_a_copy_keeps_the_originals_captions_and_says_so(
    tmp_path: Path, films: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib, vdir, package, source = _v2(tmp_path / "now", films["HEVC"])
    # the original's record reads its picture's windows, and knows them
    assert _shown(_picture(source)["captionWindows"]) == ALL
    assert source["essence"]["closedCaptions"] is source["essence"]["closedCaptionsKnown"] is True
    # the package is read at the times of the original's windows
    [v0] = package["renditions"]["video"]
    assert _says(package["renditions"]) == [("v0", "copy", True)]
    assert _shown(v0["captionWindows"]) == ALL
    assert [(w["start"], w["end"]) for w in v0["captionWindows"]] == [
        (pytest.approx(w["start"], abs=0.01), pytest.approx(w["end"], abs=0.01))
        for w in _picture(source)["captionWindows"]]
    assert v0["timestampOffset"] == 0.0
    assert _captioned(vdir / "hls" / "v0" / "playlist.m3u8") == (FRAMES, FRAMES)
    assert package["essence"]["closedCaptions"] is True
    assert rec.captions_carried([source], package) is True
    assert _gate(source, package) == []
    assert "closed-captions-dropped" not in [x["kind"] for x in package["fidelity"]["losses"]]
    # As it was, the package said none: its encode would have kept the
    # original, and only a copy goes, which carries them whatever it says.
    with monkeypatch.context() as m:
        m.setattr(pk, "_read_captions", lambda *a, **k: None)
        _lib, _before, old, _source = _v2(tmp_path / "before", films["HEVC"])
    assert _says(old["renditions"]) == [("v0", "copy", None)]
    assert "captionWindows" not in old["renditions"]["video"][0]
    assert rec.captions_carried([source], old) is False
    assert rec.deletion_gate([source["essence"]], old["essence"],
                             carried=rec.captions_carried([source], old)) == ["closedCaptions"]
    assert _gate(source, old) == []
    _validate(lib, closed_captions=True)


def test_every_rung_whose_encoder_wrote_the_captions_says_so(
    tmp_path: Path, films: dict[str, Path],
) -> None:
    # On the CPU, as the transcoder encodes without a GPU: libx265 with
    # -a53cc 1 writes them, without it none.
    lib, vdir, package, source = _v2(tmp_path, films["H264"], [
        ("libx265", [*X265, "-a53cc", "1"]), ("libx265", [*X265, "-a53cc", "1"]),
        ("libx265", X265)])
    assert _says(package["renditions"]) == [
        ("v0", "libx265", True), ("v1", "libx265", True), ("v2", "libx265", None)]
    assert [_shown(v["captionWindows"]) for v in package["renditions"]["video"]] == [ALL, ALL, NONE]
    assert [_captioned(vdir / "hls" / rung / "playlist.m3u8") for rung in ("v0", "v1", "v2")] \
        == [(FRAMES, FRAMES), (FRAMES, FRAMES), (FRAMES, 0)]
    assert package["essence"]["closedCaptions"] is True
    assert rec.captions_carried([source], package) is True
    assert _gate(source, package) == []
    _validate(lib, closed_captions=True)


def test_an_encode_whose_top_rung_lost_the_captions_says_none(
    tmp_path: Path, films: dict[str, Path],
) -> None:
    # The full picture without them: a lower rung's are no package's.
    _lib, _vdir, package, source = _v2(tmp_path, films["H264"], [
        ("libx265", X265), ("libx265", [*X265, "-a53cc", "1"])])
    assert _says(package["renditions"]) == [("v0", "libx265", None), ("v1", "libx265", True)]
    assert package["essence"]["closedCaptions"] is False
    assert rec.captions_carried([source], package) is False
    assert _gate(source, package) == ["closedCaptions"]


def test_a_film_without_captions_says_none(tmp_path: Path, films: dict[str, Path]) -> None:
    _lib, vdir, package, source = _v2(tmp_path, films["PLAIN"])
    assert _says(package["renditions"]) == [("v0", "copy", None)]
    assert _shown(package["renditions"]["video"][0]["captionWindows"]) == NONE
    assert _captioned(vdir / "hls" / "v0" / "playlist.m3u8") == (FRAMES, 0)
    assert package["essence"]["closedCaptions"] is source["essence"]["closedCaptions"] is False
    # every window of the original read, none showing any: it knows it has none
    assert source["essence"]["closedCaptionsKnown"] is True
    assert rec.captions_carried([source], package) is None
    assert _gate(source, package) == []


def test_h264_as_a_copy_and_an_encode_says_its_captions(
    tmp_path: Path, films: dict[str, Path],
) -> None:
    # The path that writes a manifest alone takes H.264: the original's own
    # stream copied, and libx264's encodes, whose -a53cc is on unless it is
    # turned off. No source record: the windows are at the title's fractions.
    stage, manifest = _manifest(tmp_path / "copy", films["H264"])
    assert _says(manifest["renditions"]) == [("v0", "copy", True)]
    assert _shown(manifest["renditions"]["video"][0]["captionWindows"]) == ALL
    assert _captioned(stage / "hls" / "v0" / "playlist.m3u8") == (FRAMES, FRAMES)
    stage, manifest = _manifest(tmp_path / "encode", films["H264"], [
        ("libx264", [*X264, "-a53cc", "1"]), ("libx264", [*X264, "-a53cc", "0"])])
    assert _says(manifest["renditions"]) == [("v0", "libx264", True), ("v1", "libx264", None)]
    assert [_captioned(stage / "hls" / rung / "playlist.m3u8") for rung in ("v0", "v1")] \
        == [(FRAMES, FRAMES), (FRAMES, 0)]
    stage, manifest = _manifest(tmp_path / "plain", films["PLAIN"])
    assert _says(manifest["renditions"]) == [("v0", "copy", None)]


def test_captions_that_start_late_are_found_in_a_window_past_the_first_frames(
    tmp_path: Path, films: dict[str, Path],
) -> None:
    # Copied on the library v2 path, its picture read in five windows, those
    # from 50 % on past its 100th frame; its package read at their times.
    lib, vdir, package, source = _v2(tmp_path / "copy", films["LATE_HEVC"])
    playlist = vdir / "hls" / "v0" / "playlist.m3u8"
    [v0] = package["renditions"]["video"]
    assert _shown(_picture(source)["captionWindows"]) == LATE
    assert _says(package["renditions"]) == [("v0", "copy", True)]
    assert _shown(v0["captionWindows"]) == LATE
    assert rec.captions_carried([source], package) is True
    # Its first frames alone, as it was read, carry none.
    first = v0["captionWindows"][0]
    assert [w["captions"] for w in pk._caption_windows_of(
        playlist, [(0.1, first["start"], first["start"] + 2.0)])] == [False]
    # Encoded by libx265 with -a53cc 1, which writes them only for the
    # frames that have them.
    _lib, vdir, package, source = _v2(tmp_path / "encode", films["LATE"],
                                      [("libx265", [*X265, "-a53cc", "1"])])
    assert _says(package["renditions"]) == [("v0", "libx265", True)]
    assert _shown(package["renditions"]["video"][0]["captionWindows"]) == LATE
    assert _captioned(vdir / "hls" / "v0" / "playlist.m3u8") == (300, 200)
    assert rec.captions_carried([source], package) is True
    assert _gate(source, package) == []
    _validate(lib, closed_captions=True)


def test_a_package_on_a_timeline_behind_its_originals_is_read_at_the_same_times(
    tmp_path: Path, films: dict[str, Path],
) -> None:
    # An original whose first picture is at 10 s, packaged from 0: its
    # package's times run 10 s behind, and its windows are read there.
    # (Matroska counts this file's duration from 0, so its windows from 50 %
    # on fall on its last keyframe: they are read where they fall, the
    # package's at the same times.)
    lib, _vdir, package, source = _v2(tmp_path, films["LATE_LATER"])
    windows = _picture(source)["captionWindows"]
    [v0] = package["renditions"]["video"]
    assert v0["timestampOffset"] == pytest.approx(-10.0, abs=0.1)
    assert windows[0]["start"] == pytest.approx(10.0, abs=0.1)
    assert _shown(windows) == _shown(v0["captionWindows"])
    assert [c for _, _, c in _shown(windows)] == [False, True, True, True, True]
    assert [round(p["start"] - v0["timestampOffset"] - s["start"], 1)
            for p, s in zip(v0["captionWindows"], windows, strict=True)] == [0.0] * 5
    assert rec.captions_carried([source], package) is True
    # the same windows, their offset left out, fall elsewhere
    stripped = {"renditions": {"video": [{k: v for k, v in v0.items() if k != "timestampOffset"}]}}
    assert rec.captions_carried([source], stripped) is False
    _validate(lib, closed_captions=True)


def test_the_captions_are_read_from_the_segments_as_the_init_has_no_frame(
    tmp_path: Path, films: dict[str, Path],
) -> None:
    stage, _manifest_ = _manifest(tmp_path, films["HEVC"])
    rendition = stage / "hls" / "v0"
    assert _captioned(rendition / "init.mp4") == (0, 0)
    targets = pk._caption_targets(None, None, 0.0, 4.0)
    assert [w["captions"] for w in pk._caption_windows_of(rendition / "playlist.m3u8", targets)] \
        == [True] * 5
    for seg in rendition.glob("seg-*.m4s"):
        seg.unlink()
    assert [(w["frames"], w["captions"]) for w in pk._caption_windows_of(
        rendition / "playlist.m3u8", targets)] == [(0, False)] * 5
