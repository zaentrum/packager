"""Real ffmpeg + shaka-packager runs (skipped unless both are on PATH).

Builds a transcoder-style handoff from a generated lavfi clip — the
original (H.264, 5.1 AAC English, stereo AC-3 German, three SRT tracks
one of them forced) plus two lower rungs with the source's keyframes —
and packages it: N video variants x 2 audio groups, I-frame playlists,
the 5.1 companion, DEFAULT/FORCED flags, aligned segments, the manifest.
A clip with cues at known times checks that the WebVTT renditions put
every cue on its frame. Packaging a title again leaves its package as it
was until the new one is complete, then swaps the new one in; the
startup sweep clears what runs that died left. An extra of the title (a
trailer, with the transcoder's handoff on the extras' ladder, and one it
left as it was) goes through the extras handler into a folder of its
own, which the title's packaging again leaves alone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packager import extras, hls, worker
from packager import packager as pk
from packager.hls import parse_attributes
from packager.katalog import ClaimedExtra
from packager.renditions import resolve_inputs


def _ffmpeg_major() -> int:
    """'ffmpeg version 7.1.5-0+deb13u1' / 'n7.1.5-…' / '8.1.2' -> 7 / 7 / 8."""
    first = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.split("\n", 1)[0]
    m = re.search(r"version n?(\d+)\.", first)
    return int(m.group(1)) if m else 99  # git builds ("N-12345-g…") are new


# The handoff fixture encodes like the transcoder (ffmpeg 7.1:
# -enc_time_base demux); older distro builds skip.
pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe", "packager"))
    or _ffmpeg_major() < 7,
    reason="needs ffmpeg/ffprobe >= 7 and shaka-packager on PATH",
)

ITEM = "c0ffee00-0000-4000-8000-000000000007"


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _start_time(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return float(out.strip() or 0)


@pytest.fixture(scope="module")
def handoff(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("handoff")
    for name, body in {
        "en.srt": "1\n00:00:01,000 --> 00:00:03,500\nHello.\n\n"
                  "2\n00:00:05,000 --> 00:00:08,000\nAcross a segment boundary.\n",
        "forced.srt": "1\n00:00:02,000 --> 00:00:04,000\n[foreign words]\n",
        "de.srt": "1\n00:00:01,000 --> 00:00:03,500\nHallo.\n",
    }.items():
        (d / name).write_text(body)
    src = d / "movie.mkv"
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24000/1001:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=12",
        "-i", str(d / "en.srt"), "-i", str(d / "forced.srt"), "-i", str(d / "de.srt"),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3", "-map", "4", "-map", "5",
        # GOP of 100 frames (4.171 s) — not a multiple of the 6 s segments.
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "100",
        "-c:a:0", "aac", "-ac:a:0", "6", "-c:a:1", "ac3", "-ac:a:1", "2", "-c:s", "srt",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=ger",
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
        "-metadata:s:s:1", "title=Forced", "-disposition:s:1", "forced",
        "-metadata:s:s:2", "language=ger", str(src))

    # What the transcoder would write for LADDER=source,360p,240p on an
    # H.264 source: v0 a stream copy, v1/v2 with the source's keyframes.
    inbox = d / "_inbox" / ITEM
    inbox.mkdir(parents=True)
    common = ["-force_key_frames", "source", "-forced-idr", "1", "-sc_threshold", "0",
              "-g", "1439", "-enc_time_base:v", "demux", "-an", "-sn", "-dn", "-f", "matroska"]
    _ff("-i", str(src), "-filter_complex",
        "[0:0]split=2[a][b];[a]scale=640:360,setsar=1,format=yuv420p[o1];"
        "[b]scale=426:240,setsar=1,format=yuv420p[o2]",
        "-map", "[o1]", "-c:v", "libx264", "-preset", "ultrafast", *common, str(inbox / "v1.mkv"),
        "-map", "[o2]", "-c:v", "libx264", "-preset", "ultrafast", *common, str(inbox / "v2.mkv"))
    (inbox / "renditions.json").write_text(json.dumps({
        "version": 1, "itemId": ITEM, "segmentSeconds": 6, "keyframes": "source",
        "timestampOffset": -_start_time(src),
        "video": [
            {"id": "v0", "label": "source", "file": None, "mode": "copy", "encoder": "copy"},
            {"id": "v1", "label": "360p", "file": "v1.mkv", "mode": "encode",
             "encoder": "libx264"},
            {"id": "v2", "label": "240p", "file": "v2.mkv", "mode": "encode",
             "encoder": "libx264"},
        ],
    }))
    return src, inbox


def _package(tmp_path: Path, monkeypatch, src: Path, inbox: Path | None, *,
             item: dict | None = None, **options):
    """Package src (with the handoff in inbox); `item` holds what the
    worker passes from the item record and the settings."""
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    inputs = resolve_inputs(inbox, str(src)) if inbox else None
    manifest = pk.package_item(
        ITEM, str(src), "movie", title="clip", inputs=inputs,
        options=pk.PackageOptions(**{"preferred_languages": ("en",), **options}),
        **(item or {}),
    )
    return manifest, tmp_path / "packages" / "movies" / ITEM[:2] / ITEM


def _master(root: Path) -> list[tuple[str, dict[str, str], str]]:
    lines = (root / "hls" / "master.m3u8").read_text().splitlines()
    out = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-") and ":" in line:
            tag, attrs = line.split(":", 1)
            uri = lines[i + 1] if tag == "#EXT-X-STREAM-INF" else ""
            out.append((tag, parse_attributes(attrs), uri))
    return out


def _segment_starts(rendition: Path) -> list[float]:
    """First keyframe PTS of every media segment of one rendition."""
    starts = []
    for seg in sorted(rendition.glob("seg-*.m4s")):
        joined = rendition / f".{seg.name}.mp4"
        joined.write_bytes((rendition / "init.mp4").read_bytes() + seg.read_bytes())
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "packet=pts_time,flags",
             "-of", "csv=p=0", str(joined)], capture_output=True, text=True).stdout
        joined.unlink()
        keys = [float(t) for t, f in (ln.split(",")[:2] for ln in out.splitlines() if ln)
                if "K" in f]
        starts.append(round(min(keys), 3))
    return starts


def test_ladder_package(tmp_path: Path, monkeypatch, handoff) -> None:
    src, inbox = handoff
    manifest, root = _package(tmp_path, monkeypatch, src, inbox, hls_subtitles=True)
    entries = _master(root)

    variants = [(a, uri) for tag, a, uri in entries if tag == "#EXT-X-STREAM-INF"]
    assert [uri for _, uri in variants] == ["v0/playlist.m3u8", "v1/playlist.m3u8",
                                            "v2/playlist.m3u8"] * 2
    assert [a["RESOLUTION"] for a, _ in variants[:3]] == ["1280x720", "640x360", "426x240"]
    assert [a["AUDIO"] for a, _ in variants] == ["audio"] * 3 + ["audio-surround"] * 3
    for a, _ in variants:
        assert a["CODECS"].startswith("avc1.")
        assert int(a["BANDWIDTH"]) >= int(a["AVERAGE-BANDWIDTH"]) > 0
        assert a["FRAME-RATE"] == "23.976" and a["SUBTITLES"] == "subs"
    assert variants[0][0]["CODECS"].endswith(",mp4a.40.2")
    assert variants[3][0]["CODECS"].endswith(",ec-3")
    # Higher rung, higher bandwidth.
    bw = [int(a["BANDWIDTH"]) for a, _ in variants[:3]]
    assert bw == sorted(bw, reverse=True)

    iframes = [a for tag, a, _ in entries if tag == "#EXT-X-I-FRAME-STREAM-INF"]
    assert [a["URI"] for a in iframes] == [f"v{i}/iframes.m3u8" for i in range(3)]
    assert all((root / "hls" / f"v{i}" / "iframes.m3u8").exists() for i in range(3))

    media = [a for tag, a, _ in entries if tag == "#EXT-X-MEDIA"]
    stereo = [a for a in media if a["GROUP-ID"] == "audio"]
    surround = [a for a in media if a["GROUP-ID"] == "audio-surround"]
    subs = [a for a in media if a["TYPE"] == "SUBTITLES"]
    assert [(a["LANGUAGE"], a["NAME"], a["DEFAULT"], a["CHANNELS"]) for a in stereo] == [
        ("en", "English", "YES", "2"), ("de", "German", "NO", "2")]
    assert [(a["LANGUAGE"], a["NAME"], a["DEFAULT"], a["CHANNELS"]) for a in surround] == [
        ("en", "English 5.1", "YES", "6")]
    # The forced track's title, "Forced", says what it is.
    assert [(a["NAME"], a["DEFAULT"], a.get("FORCED")) for a in subs] == [
        ("English", "NO", None), ("English · Forced", "NO", "YES"), ("German", "NO", None)]

    # Every rung is cut at the same instants (the source's keyframes in
    # each 6 s window: 0, 8.342 for a 100-frame GOP).
    starts = [_segment_starts(root / "hls" / f"v{i}") for i in range(3)]
    assert len(starts[0]) == 2
    assert starts[1] == pytest.approx(starts[0], abs=0.002)
    assert starts[2] == pytest.approx(starts[0], abs=0.002)

    r = manifest["renditions"]
    assert [(v["id"], v["width"], v["height"], v["label"]) for v in r["video"]] == [
        ("v0", 1280, 720, "source"), ("v1", 640, 360, "360p"), ("v2", 426, 240, "240p")]
    assert [a["default"] for a in r["audio"]] == [True, False]
    assert [(a["id"], a["codec"], a["channels"]) for a in r["audioSurround"]] == [
        ("a2", "ec-3", 6)]
    assert [s.get("hls") for s in manifest["subtitles"]] == ["hls/s0", "hls/s1", "hls/s2"]
    # The forced English track shows by itself under the English audio.
    assert [s["default"] for s in manifest["subtitles"]] == [False, True, False]
    assert all((root / s["path"]).exists() for s in manifest["subtitles"])
    assert manifest["hls"] == {"master": "hls/master.m3u8", "segmentSeconds": 6,
                               "audioGroups": ["audio", "audio-surround"],
                               "subtitleGroup": "subs"}
    assert not list((root / "hls").rglob(".shaka-master.m3u8"))



def test_every_audio_group_has_exactly_one_default(tmp_path: Path, monkeypatch, handoff) -> None:
    # German preferred: the stereo default is the German track, which has
    # no 5.1. The 5.1 group's only rendition (English) used to get
    # DEFAULT=NO, leaving that group without a default.
    src, inbox = handoff
    manifest, root = _package(tmp_path, monkeypatch, src, inbox, preferred_languages=("de",))
    audio = [a for tag, a, _ in _master(root) if tag == "#EXT-X-MEDIA" and a["TYPE"] == "AUDIO"]
    assert {g: [a["LANGUAGE"] for a in audio if a["GROUP-ID"] == g and a["DEFAULT"] == "YES"]
            for g in ("audio", "audio-surround")} == {"audio": ["de"], "audio-surround": ["en"]}
    r = manifest["renditions"]
    assert [a["default"] for a in r["audio"]] == [False, True]
    assert [a["default"] for a in r["audioSurround"]] == [True]
    # The forced English subtitle is foreign to the German audio.
    assert [s["default"] for s in manifest["subtitles"]] == [False, False, False]


def test_a_subtitle_the_file_flags_default_does_not_show_by_itself(
    tmp_path: Path, monkeypatch,
) -> None:
    # Sintel's tracks: an English film, its audio titled after its codec,
    # German subtitles first and flagged default (the transcoder's remux
    # flags the first one when the file flags none).
    for name, text in (("de.srt", "Hallo."), ("en.srt", "Hello.")):
        (tmp_path / name).write_text(f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n")
    src = tmp_path / "sintel.mkv"
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-i", str(tmp_path / "de.srt"), "-i", str(tmp_path / "en.srt"),
        "-map", "0:v", "-map", "1:a", "-map", "2", "-map", "3",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=AC3 5.1 @ 640 Kbps",
        "-metadata:s:s:0", "language=ger", "-metadata:s:s:1", "language=eng",
        "-disposition:s:0", "default", "-disposition:s:1", "0", str(src))
    manifest, root = _package(tmp_path, monkeypatch, src, None, hls_subtitles=True,
                              surround_codec="off")
    assert [(s["language"], s["default"]) for s in manifest["subtitles"]] == [
        ("ger", False), ("eng", False)]
    [audio] = manifest["renditions"]["audio"]
    assert (audio["name"], audio["title"]) == ("English", "AC3 5.1 @ 640 Kbps")
    media = [a for tag, a, _ in _master(root) if tag == "#EXT-X-MEDIA"]
    assert [(a["TYPE"], a["LANGUAGE"], a["NAME"], a["DEFAULT"]) for a in media] == [
        ("AUDIO", "en", "English", "YES"),
        ("SUBTITLES", "de", "German", "NO"), ("SUBTITLES", "en", "English", "NO")]

def test_the_catalogs_track_languages_reach_the_manifest_and_the_master(
    tmp_path: Path, monkeypatch, handoff,
) -> None:
    # The catalog knows better than two of the file's tags: the second
    # audio track is French, not German, and the third subtitle Spanish.
    src, inbox = handoff
    manifest, root = _package(
        tmp_path, monkeypatch, src, inbox, hls_subtitles=True, surround_codec="off",
        item={"track_languages": [
            {"kind": "audio", "ordinal": 1, "language": "fre"},
            {"kind": "subtitle", "ordinal": 2, "language": "spa"},
            {"kind": "audio", "ordinal": 7, "language": "ita"},  # no such track
        ]})
    assert [(a["language"], a["name"]) for a in manifest["renditions"]["audio"]] == [
        ("eng", "English"), ("fre", "French")]
    assert [s["language"] for s in manifest["subtitles"]] == ["eng", "eng", "spa"]
    media = [a for tag, a, _ in _master(root) if tag == "#EXT-X-MEDIA"]
    assert [(a["LANGUAGE"], a["NAME"]) for a in media if a["TYPE"] == "AUDIO"] == [
        ("en", "English"), ("fr", "French")]
    assert [(a["LANGUAGE"], a["NAME"]) for a in media if a["TYPE"] == "SUBTITLES"] == [
        ("en", "English"), ("en", "English · Forced"), ("es", "Spanish")]


@pytest.mark.parametrize(("override", "language", "name"), [
    (None, "und", "Unknown"),             # the file's tag: none
    ("zxx", "zxx", "No dialogue"),        # what the catalog knows
])
def test_a_film_without_dialogue(tmp_path: Path, monkeypatch, override, language, name) -> None:
    # One untagged track, as most short films' MP4s have; a whitelist that
    # names neither und nor zxx, and no one-language fallback.
    src = tmp_path / "film.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src))
    tracks = [{"kind": "audio", "ordinal": 0, "language": override}] if override else []
    manifest, root = _package(
        tmp_path, monkeypatch, src, None, surround_codec="off",
        item={"language_whitelist": ["en", "de"], "keep_original_if_single": False,
              "track_languages": tracks})
    [audio] = manifest["renditions"]["audio"]
    assert (audio["language"], audio["name"], audio["visible"], audio["default"]) == (
        language, name, True, True)
    [media] = [a for tag, a, _ in _master(root) if tag == "#EXT-X-MEDIA"]
    assert (media.get("LANGUAGE"), media["NAME"], media["DEFAULT"]) == (
        override, name, "YES")


def test_subtitle_files_next_to_the_source(tmp_path: Path, monkeypatch, handoff) -> None:
    # The handoff's source with four files next to it: French in
    # Windows-1252, English forced with a byte order mark, German as ASS
    # (hidden by an en,fr whitelist), and one that isn't a subtitle file.
    src, inbox = handoff
    folder = tmp_path / "Movie (2010)"
    folder.mkdir()
    source = folder / "Movie.mkv"
    shutil.copy(src, source)
    files = {
        "Movie.fr.srt": "1\n00:00:01,000 --> 00:00:03,500\nÇa va, « Café » ?\n".encode("cp1252"),
        "Movie.en.forced.srt": b"\xef\xbb\xbf1\r\n00:00:02,000 --> 00:00:04,000\r\n[Signs]\r\n",
        "Movie.de.ass": (b"[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, "
                         b"End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                         b"Dialogue: 0,0:00:05.00,0:00:07.00,Default,,0,0,0,,Hallo\n"),
        "Movie.xx.srt": b"not a subtitle file\n",
    }
    for name, body in files.items():
        (folder / name).write_bytes(body)
    shutil.copytree(inbox, tmp_path / "inbox")
    manifest, root = _package(
        tmp_path, monkeypatch, source, tmp_path / "inbox", hls_subtitles=True,
        surround_codec="off",
        item={"language_whitelist": ["en", "fr"], "subtitle_files": [
            {"path": str(folder / "Movie.fr.srt"), "language": "fre", "label": "Français"},
            {"path": str(folder / "Movie.en.forced.srt"), "language": "eng", "forced": True},
            {"path": str(folder / "Movie.de.ass"), "language": "ger"},
            {"path": str(folder / "Movie.xx.srt"), "language": "eng"},
            {"path": str(tmp_path / "elsewhere.srt"), "language": "eng"},  # not next to it
        ]})
    subs = manifest["subtitles"]
    # The source's three tracks, then the files that converted.
    assert [(s["id"], s["language"], s.get("external", False), s["forced"], s["default"],
             s["visible"], s.get("hls")) for s in subs] == [
        ("sub0", "eng", False, False, False, True, "hls/s0"),
        ("sub1", "eng", False, True, True, True, "hls/s1"),
        ("sub2", "ger", False, False, False, False, None),
        ("sub3", "fre", True, False, False, True, "hls/s3"),
        ("sub4", "eng", True, True, False, True, "hls/s4"),
        ("sub5", "ger", True, False, False, False, None),
    ]
    assert subs[3]["title"] == "Français"
    vtt = (root / subs[3]["path"]).read_text(encoding="utf-8")
    assert "00:01.000 --> 00:03.500\nÇa va, « Café » ?" in vtt
    assert "00:05.000 --> 00:07.000\nHallo" in (root / subs[5]["path"]).read_text()
    media = [a for tag, a, _ in _master(root) if tag == "#EXT-X-MEDIA" and a["TYPE"] == "SUBTITLES"]
    assert [(a["URI"], a["LANGUAGE"], a["NAME"], a.get("FORCED")) for a in media] == [
        ("s0/playlist.m3u8", "en", "English", None),
        ("s1/playlist.m3u8", "en", "English · Forced", "YES"),
        ("s3/playlist.m3u8", "fr", "French", None),          # its label: "Français"
        ("s4/playlist.m3u8", "en", "English (forced)", "YES"),
    ]
    # ... and the rendition's cue is at its time (shaka adds cue settings).
    assert [line.split()[:3] for line in
            (root / "hls" / "s3" / "seg-00001.vtt").read_text().splitlines() if "-->" in line] == [
        ["00:00:01.000", "-->", "00:00:03.500"]]
    assert (root / ".complete").exists()


def test_subtitle_group_off_by_default(tmp_path: Path, monkeypatch, handoff) -> None:
    src, inbox = handoff
    manifest, root = _package(tmp_path, monkeypatch, src, inbox)
    text = (root / "hls" / "master.m3u8").read_text()
    assert "TYPE=SUBTITLES" not in text and "SUBTITLES=" not in text
    # ... but the renditions are on disk, ready for the day the master
    # references them (no re-encode or re-segmenting needed then).
    assert (root / "hls" / "s1" / "playlist.m3u8").exists()
    assert manifest["hls"]["subtitleGroup"] is None


def test_single_rendition_without_handoff(tmp_path: Path, monkeypatch, handoff) -> None:
    src, _inbox = handoff
    manifest, root = _package(tmp_path, monkeypatch, src, None, surround_codec="off")
    variants = [uri for tag, _a, uri in _master(root) if tag == "#EXT-X-STREAM-INF"]
    assert variants == ["v0/playlist.m3u8"]
    assert len(manifest["renditions"]["video"]) == 1
    assert manifest["renditions"]["audioSurround"] == []
    assert (root / ".complete").exists()


_CUE = re.compile(r"^(\d+):(\d\d):(\d\d)\.(\d{3}) --> ")
_MAP = re.compile(r"^X-TIMESTAMP-MAP=(.*)$")


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _mapped_cues(rendition: Path) -> list[tuple[int, float, str]]:
    """(segment number, start on the media timeline, text) of every cue of
    a WebVTT rendition, read as a player does: the cue time mapped through
    the segment's X-TIMESTAMP-MAP (MPEGTS at 90 kHz, LOCAL a cue time) —
    LOCAL 0 = MPEGTS 0 when a segment has none (RFC 8216 3.5)."""
    out = []
    for n, seg in enumerate(sorted(rendition.glob("seg-*.vtt")), start=1):
        lines = seg.read_text().splitlines()
        local, mpegts = 0.0, 0
        for line in lines:
            if m := _MAP.match(line):
                attrs = dict(kv.split(":", 1) for kv in m.group(1).split(","))
                local = _seconds(*re.match(r"(\d+):(\d\d):(\d\d)\.(\d{3})",
                                           attrs["LOCAL"]).groups())
                mpegts = int(attrs["MPEGTS"])
        for i, line in enumerate(lines):
            if m := _CUE.match(line):
                out.append((n, _seconds(*m.groups()) - local + mpegts / 90_000, lines[i + 1]))
    return out


@pytest.mark.parametrize("start", [0.0, 0.5])
def test_webvtt_cues_line_up_with_the_media(tmp_path: Path, monkeypatch, start: float) -> None:
    # Cues 2 s and 7.5 s after the first frame of a clip with no B-frames
    # (first PTS = first decode time, what hls.js anchors the media
    # timeline on) and a keyframe every 2 s. shaka's default wrote
    # MPEGTS:9000 into every segment — each cue 100 ms after its frame.
    # A clip starting at 0 is packaged as is; one starting later goes the
    # transcoder's way, a contract moving the original onto the timeline
    # that starts at 0 (timestampOffset).
    d = tmp_path / "clip"
    d.mkdir()
    (d / "cues.srt").write_text("1\n00:00:02,000 --> 00:00:03,000\nAt two.\n\n"
                                "2\n00:00:07,500 --> 00:00:08,500\nAt seven and a half.\n")
    src = d / "cues.mkv"
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
        "-i", str(d / "cues.srt"), "-map", "0:v", "-map", "1:a", "-map", "2",
        "-c:v", "libx264", "-preset", "ultrafast", "-bf", "0", "-g", "50",
        "-c:a", "aac", "-c:s", "srt", "-metadata:s:s:0", "language=eng",
        *(["-output_ts_offset", str(start)] if start else []), str(src))
    assert _start_time(src) == pytest.approx(start, abs=0.001)
    inbox = None
    if start:
        inbox = d / "_inbox" / ITEM
        inbox.mkdir(parents=True)
        (inbox / "renditions.json").write_text(json.dumps({
            "version": 1, "segmentSeconds": 6, "keyframes": "source",
            "timestampOffset": -start,
            "video": [{"id": "v0", "label": "source", "file": None, "mode": "copy"}],
        }))
    _manifest, root = _package(tmp_path, monkeypatch, src, inbox, hls_subtitles=True,
                               surround_codec="off")

    video = _segment_starts(root / "hls" / "v0")
    assert video == pytest.approx([0.0, 6.0], abs=0.001)
    # Every cue sits in the segment whose window holds it, at its source
    # time on the media's timeline, to the millisecond.
    assert [(n, round(t - video[0], 3), text) for n, t, text in
            _mapped_cues(root / "hls" / "s0")] == [
        (1, 2.0, "At two."), (2, 7.5, "At seven and a half.")]
    # ... and the subtitle playlist's segments span the video's.
    extinf = [float(line.split(":", 1)[1].rstrip(","))
              for line in (root / "hls" / "s0" / "playlist.m3u8").read_text().splitlines()
              if line.startswith("#EXTINF:")]
    assert [sum(extinf[:i]) for i in range(len(extinf))] == pytest.approx(video, abs=0.001)


def test_worker_consumes_the_handoff(tmp_path: Path, monkeypatch, handoff) -> None:
    src, inbox = handoff
    work = tmp_path / "inbox-copy" / ITEM
    shutil.copytree(inbox, work)
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    monkeypatch.setattr(worker, "_INBOX_ROOT", work.parent)

    class Client:
        def __init__(self) -> None:
            self.steps: list[tuple[str, dict]] = []
            self.manifests: list[dict] = []

        def settings(self) -> dict:
            return {"packager.language_whitelist": "en,de"}

        def upsert_step(self, _id: str, status: str, **kw: object) -> None:
            self.steps.append((status, kw))

        def packaging_complete(self, _id: str, manifest: dict) -> None:
            self.manifests.append(manifest)

    from packager.katalog import ClaimedItem
    client = Client()
    item = ClaimedItem(id=ITEM, type="movie", title="clip", year=None, duration_ms=None,
                       path=str(src))
    worker._process_one(item, client, pk.PackageOptions())  # type: ignore[arg-type]
    assert [s for s, _ in client.steps] == ["in_progress", "done"]
    assert "vr=3" in str(client.steps[-1][1]["details"])
    assert len(client.manifests[0]["renditions"]["video"]) == 3
    assert not work.exists()  # handoff cleaned up after a successful package

    # The catalog gets the source as probed (this handoff's contract has no
    # source block, so all of it comes from a probe of the original); the
    # manifest on disk keeps none (v2).
    fmt = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,bit_rate", "-of", "json",
         str(src)], capture_output=True, text=True, check=True).stdout)["format"]
    assert client.manifests[0]["source"] == {
        "codec": "h264", "width": 1280, "height": 720,
        "durationMs": int(float(fmt["duration"]) * 1000), "bitRate": int(fmt["bit_rate"]),
    }
    on_disk = json.loads((tmp_path / "packages" / "movies" / ITEM[:2] / ITEM
                          / "manifest.json").read_text())
    assert "source" not in on_disk


# ------------------------------------------------------- packaging again

def _tree(root: Path) -> dict[str, bytes]:
    """Every file under root, by its path relative to it."""
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _live(root: Path) -> dict[str, bytes]:
    """The item folder without a run's staging folder and the packages a
    swap replaced."""
    return {rel: body for rel, body in _tree(root).items()
            if rel.split("/", 1)[0] != pk.STAGING_DIR and ".old-" not in rel.split("/", 1)[0]}


def _served(root: Path) -> dict[str, bytes]:
    """What a player gets, by the paths the stream service reads: the
    manifest, the master, every playlist the master names and every init
    section and segment those name."""
    out = {pk.MANIFEST_FILE: (root / pk.MANIFEST_FILE).read_bytes()}
    master = (root / "hls" / "master.m3u8").read_text()
    out["hls/master.m3u8"] = master.encode()
    for uri in hls.playlist_uris(master):
        playlist = root / "hls" / uri
        out[f"hls/{uri}"] = playlist.read_bytes()
        for name in hls.playlist_uris(playlist.read_text()):
            out[(Path("hls") / uri).parent.joinpath(name).as_posix()] = (
                playlist.parent / name).read_bytes()
    return out


def _replaced(root: Path) -> dict[str, Path]:
    return {p.name.split(".old-")[0]: p for p in root.iterdir() if ".old-" in p.name}


def test_packaging_again_swaps_the_new_package_in(tmp_path: Path, monkeypatch, handoff) -> None:
    # A title packaged before it had a ladder (the original, one rendition)
    # is packaged again with three rungs and the 5.1 group, as a re-encode
    # does. The old package is served byte for byte while the new one is
    # written; once it is complete, the new one is what a player gets.
    src, inbox = handoff
    _first, root = _package(tmp_path, monkeypatch, src, None, surround_codec="off")
    old, old_served = _live(root), _served(root)
    real_shaka, real_swap = pk._run_shaka, pk._swap_in
    staged: dict[str, bytes] = {}

    def shaka(cmd: list[str], cwd: Path, label: str) -> None:
        assert cwd == root / pk.STAGING_DIR
        assert _live(root) == old and _served(root) == old_served
        real_shaka(cmd, cwd, label)

    def swap(out_root: Path, stage: Path) -> list[Path]:
        assert _live(out_root) == old
        staged.update(_tree(stage))
        return real_swap(out_root, stage)

    monkeypatch.setattr(pk, "_run_shaka", shaka)
    monkeypatch.setattr(pk, "_swap_in", swap)
    manifest, _root = _package(tmp_path, monkeypatch, src, inbox)

    # Every staged file is live, as it was staged, and is what is served.
    staged.pop(pk.SENTINEL)
    live = _live(root)
    assert set(live) == {*staged, ".complete"}
    assert all(live[rel] == body for rel, body in staged.items())
    served = _served(root)
    assert served == {rel: staged[rel] for rel in served}
    assert json.loads(served[pk.MANIFEST_FILE]) == manifest
    assert [uri for tag, _a, uri in _master(root) if tag == "#EXT-X-STREAM-INF"] == [
        f"v{i}/playlist.m3u8" for i in range(3)] * 2
    # Nothing points into the staging folder, which is gone.
    assert not (root / pk.STAGING_DIR).exists()
    assert not [rel for rel, body in served.items()
                if rel.endswith((".m3u8", ".json")) and pk.STAGING_DIR.encode() in body]
    # The old package waits beside the new one, as it was, for the
    # requests that started on it.
    replaced = _replaced(root)
    assert sorted(replaced) == ["hls", "subs", "trickplay"]
    for name, path in replaced.items():
        assert _tree(path) == {rel.split("/", 1)[1]: body for rel, body in old.items()
                               if rel.startswith(f"{name}/")}


def test_a_run_that_fails_leaves_the_live_package_as_it_was(
    tmp_path: Path, monkeypatch, handoff,
) -> None:
    src, inbox = handoff
    _first, root = _package(tmp_path, monkeypatch, src, None, surround_codec="off")
    old = _live(root)
    real_shaka = pk._run_shaka

    def shaka(cmd: list[str], cwd: Path, label: str) -> None:
        real_shaka(cmd, cwd, label)
        if label == "media":  # its segments written, then the disk is full
            raise pk.PackageError("shaka-packager (media) exited 1: No space left on device")

    monkeypatch.setattr(pk, "_run_shaka", shaka)
    with pytest.raises(pk.PackageError, match="No space left"):
        _package(tmp_path, monkeypatch, src, inbox)
    assert {rel: body for rel, body in _live(root).items() if rel != ".failed"} == old
    assert sorted(p.name for p in root.iterdir()) == [
        ".complete", ".failed", "hls", pk.MANIFEST_FILE, "subs", "trickplay"]
    assert "No space left" in json.loads((root / ".failed").read_text())["error"]

    # A run that succeeds clears .failed.
    monkeypatch.setattr(pk, "_run_shaka", real_shaka)
    _package(tmp_path, monkeypatch, src, inbox)
    assert not (root / ".failed").exists()


def test_a_package_with_a_segment_missing_is_not_swapped_in(
    tmp_path: Path, monkeypatch, handoff,
) -> None:
    src, inbox = handoff
    _first, root = _package(tmp_path, monkeypatch, src, None, surround_codec="off")
    old = _live(root)
    real = pk._run_shaka_packager

    def lose_a_segment(*args, **kwargs):
        out = real(*args, **kwargs)
        (root / pk.STAGING_DIR / "hls" / "v1" / "seg-00002.m4s").unlink()
        return out

    monkeypatch.setattr(pk, "_run_shaka_packager", lose_a_segment)
    with pytest.raises(pk.PackageError, match=r"not swapped in: 1 missing: hls/v1/seg-00002\.m4s"):
        _package(tmp_path, monkeypatch, src, inbox)
    assert {rel: body for rel, body in _live(root).items() if rel != ".failed"} == old
    assert not (root / pk.STAGING_DIR).exists() and not _replaced(root)


def test_the_replaced_package_goes_after_the_grace_period(
    tmp_path: Path, monkeypatch, handoff,
) -> None:
    src, inbox = handoff
    _package(tmp_path, monkeypatch, src, None, surround_codec="off")
    _manifest, root = _package(tmp_path, monkeypatch, src, inbox, old_package_grace_seconds=1.0)
    new = _live(root)
    replaced = list(_replaced(root).values())
    assert len(replaced) == 3  # the grace period has just begun
    deadline = time.monotonic() + 15
    while any(p.exists() for p in replaced) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not [p for p in replaced if p.exists()]
    assert _live(root) == new


class _Killed(BaseException):
    """The process dies mid-run (SIGKILL, the OOM killer): no handler runs."""


def test_the_next_run_clears_what_a_dead_run_left(tmp_path: Path, monkeypatch, handoff) -> None:
    src, inbox = handoff
    _first, root = _package(tmp_path, monkeypatch, src, None, surround_codec="off")
    old = _live(root)
    real_shaka = pk._run_shaka

    def killed(cmd: list[str], cwd: Path, label: str) -> None:
        real_shaka(cmd, cwd, label)
        raise _Killed

    monkeypatch.setattr(pk, "_run_shaka", killed)
    with pytest.raises(_Killed):
        _package(tmp_path, monkeypatch, src, inbox)
    assert (root / pk.STAGING_DIR / "hls" / "v0" / "init.mp4").exists()
    assert _live(root) == old
    # ... and a package replaced an hour ago whose timer died with its process.
    expired = root / f"hls.old-{(datetime.now(UTC) - timedelta(hours=1)).strftime(pk._STAMP)}"
    shutil.copytree(root / "hls", expired)

    monkeypatch.setattr(pk, "_run_shaka", real_shaka)
    _package(tmp_path, monkeypatch, src, inbox)
    assert not (root / pk.STAGING_DIR).exists() and not expired.exists()
    assert sorted(_replaced(root)) == ["hls", "subs", "trickplay"]  # this run's, in their grace


def test_a_startup_sweep_clears_what_dead_runs_left(tmp_path: Path, monkeypatch, handoff) -> None:
    src, inbox = handoff
    _package(tmp_path, monkeypatch, src, None, surround_codec="off")
    _manifest, root = _package(tmp_path, monkeypatch, src, inbox)
    real_shaka = pk._run_shaka

    def killed(cmd: list[str], cwd: Path, label: str) -> None:
        real_shaka(cmd, cwd, label)
        raise _Killed

    monkeypatch.setattr(pk, "_run_shaka", killed)
    with pytest.raises(_Killed):
        _package(tmp_path, monkeypatch, src, inbox)
    live = _live(root)
    # Days later: the process died with the replaced package's timer, an
    # hour past its grace period, and with the run it was in.
    hour_ago = (datetime.now(UTC) - timedelta(minutes=70)).strftime(pk._STAMP)
    expired = [path.rename(root / f"{name}.old-{hour_ago}")
               for name, path in _replaced(root).items()]
    two_days_ago = time.time() - 2 * 86400
    os.utime(root / pk.STAGING_DIR / pk.SENTINEL, (two_days_ago, two_days_ago))
    # Another replica packages another title right now.
    busy = root.parent / "c0ffee00-0000-4000-8000-0000000000aa" / pk.STAGING_DIR
    busy.mkdir(parents=True)
    (busy / pk.SENTINEL).write_text("{}")

    assert pk.sweep_leftovers(600) == len(expired) + 1
    assert len(expired) == 3 and not [p for p in expired if p.exists()]
    assert not (root / pk.STAGING_DIR).exists()
    assert _live(root) == live
    assert (busy / pk.SENTINEL).exists()


# ------------------------------------------------------------------ extras

EXTRA = "1b5c2a8e-0000-4000-8000-0000000000e1"


@pytest.fixture(scope="module")
def extra_handoff(tmp_path_factory) -> tuple[Path, Path]:
    """A 720p H.264 trailer of the title, outside the media root, and what
    the transcoder hands over for it on the extras' default ladder
    (720p:h264,480p:h264): v0 the trailer as it is ("file": null), v1 a
    480p encode on its keyframes."""
    d = tmp_path_factory.mktemp("extra")
    trailer = d / "extras" / "clip" / "trailer.mp4"
    trailer.parent.mkdir(parents=True)
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-c:a", "aac", "-ac", "2",
        str(trailer))
    inbox = d / "_inbox" / f"extra-{EXTRA}"
    inbox.mkdir(parents=True)
    _ff("-i", str(trailer), "-vf", "scale=854:480,setsar=1,format=yuv420p",
        "-c:v", "libx264", "-preset", "ultrafast", "-force_key_frames", "source",
        "-forced-idr", "1", "-sc_threshold", "0", "-g", "1439", "-enc_time_base:v", "demux",
        "-an", "-sn", "-dn", "-f", "matroska", str(inbox / "v1.mkv"))
    (inbox / "renditions.json").write_text(json.dumps({
        "version": 1, "itemId": EXTRA, "segmentSeconds": 6, "keyframes": "source",
        "timestampOffset": -_start_time(trailer),
        "video": [
            {"id": "v0", "label": "720p", "file": None, "mode": "copy", "encoder": "copy"},
            {"id": "v1", "label": "480p", "file": "v1.mkv", "mode": "encode",
             "encoder": "libx264"},
        ],
    }))
    return trailer, inbox


class _ExtrasCatalog:
    """The catalog's side of one extra: its record, the settings, and what
    the packager writes back."""

    def __init__(self, extra: ClaimedExtra) -> None:
        self.extra = extra
        self.steps: list[tuple[str, dict]] = []
        self.packages: list[dict] = []

    def get_extra(self, extra_id: str) -> ClaimedExtra | None:
        return self.extra if extra_id == self.extra.id else None

    def settings(self) -> dict:
        return {}

    def upsert_extra_step(self, _id: str, status: str, **kw: object) -> None:
        self.steps.append((status, kw))

    def extra_packaging_complete(self, _id: str, manifest: dict) -> dict:
        self.packages.append(manifest)
        return {"extraId": _id, "itemId": self.extra.parent_id, "packaged": True,
                "durationMs": manifest["durationMs"]}


def _package_extra(catalog: _ExtrasCatalog) -> Path:
    """One catalog.extra.transcoded through the extras handler; the
    extra's package folder."""
    extra = catalog.extra
    envelope = {"eventId": "9f2b", "extraId": extra.id, "parentId": extra.parent_id,
                "type": "extra", "kind": extra.kind, "step": "package", "status": "queued",
                "occurredAt": "2026-10-06T08:00:00Z", "source": "transcoder"}
    extras._handle_extra(extra.id, envelope, catalog,  # type: ignore[arg-type]
                         pk.PackageOptions(surround_codec="off"))
    return pk.PACKAGES_ROOT / "extras" / extra.id[:2] / extra.id


def test_an_extra_is_packaged_in_a_folder_of_its_own(
    tmp_path: Path, monkeypatch, handoff, extra_handoff,
) -> None:
    # The title, then its trailer, then the title again with its ladder
    # (a re-encode).
    src, inbox = handoff
    trailer, extra_inbox = extra_handoff
    _first, title_root = _package(tmp_path, monkeypatch, src, None, surround_codec="off")
    title_before = _tree(title_root)
    work = tmp_path / "packages" / "_inbox"
    shutil.copytree(extra_inbox, work / extra_inbox.name)
    monkeypatch.setattr(extras, "_INBOX_ROOT", work)
    catalog = _ExtrasCatalog(ClaimedExtra(
        id=EXTRA, parent_id=ITEM, kind="trailer", title="Trailer", path=str(trailer),
        state="transcoded", parent_title="clip"))
    root = _package_extra(catalog)

    assert [status for status, _ in catalog.steps] == ["in_progress", "done"]
    assert "vr=2" in str(catalog.steps[-1][1]["details"])
    assert root == tmp_path / "packages" / "extras" / "1b" / EXTRA
    assert sorted(p.name for p in root.iterdir()) == [".complete", "hls", pk.MANIFEST_FILE]
    manifest = json.loads((root / pk.MANIFEST_FILE).read_text())
    assert {k: manifest[k] for k in ("itemId", "type", "parentId", "extraKind", "title",
                                     "year", "tmdbId")} == {
        "itemId": EXTRA, "type": "extra", "parentId": ITEM, "extraKind": "trailer",
        "title": "Trailer", "year": None, "tmdbId": None}
    assert "trickplay" not in manifest
    r = manifest["renditions"]
    assert [(v["id"], v["width"], v["height"], v["label"]) for v in r["video"]] == [
        ("v0", 1280, 720, "720p"), ("v1", 854, 480, "480p")]
    assert [(a["id"], a["language"], a["name"], a["default"]) for a in r["audio"]] == [
        ("a0", "und", "Unknown", True)]
    assert r["audioSurround"] == [] and manifest["subtitles"] == []

    # Two H.264 variants, cut at the same instants.
    variants = [(a, uri) for tag, a, uri in _master(root) if tag == "#EXT-X-STREAM-INF"]
    assert [uri for _, uri in variants] == ["v0/playlist.m3u8", "v1/playlist.m3u8"]
    assert [a["RESOLUTION"] for a, _ in variants] == ["1280x720", "854x480"]
    assert all(a["CODECS"].startswith("avc1.") and a["CODECS"].endswith(",mp4a.40.2")
               for a, _ in variants)
    starts = [_segment_starts(root / "hls" / f"v{i}") for i in range(2)]
    assert len(starts[0]) == 2
    assert starts[1] == pytest.approx(starts[0], abs=0.002)

    # The catalog got the package as it is on disk, with the source block,
    # and the handoff went.
    [sent] = catalog.packages
    assert {k: v for k, v in sent.items() if k != "source"} == manifest
    assert {k: sent["source"][k] for k in ("codec", "width", "height")} == {
        "codec": "h264", "width": 1280, "height": 720}
    assert not (work / extra_inbox.name).exists()

    # The title's folder is as it was, and holds nothing of its trailer.
    assert _tree(title_root) == title_before
    # The title packaged again: its swap retires what its new manifest
    # doesn't name, in its own folder only. The trailer's package stays
    # byte for byte.
    extra_before = _tree(root)
    _package(tmp_path, monkeypatch, src, inbox)
    assert sorted(_replaced(title_root)) == ["hls", "subs", "trickplay"]
    assert _tree(root) == extra_before


def test_an_extra_the_transcoder_left_as_it_was_is_packaged_from_its_file(
    tmp_path: Path, monkeypatch,
) -> None:
    # A small H.264 teaser: nothing to encode, no handoff (the transcoder's
    # step was not_applicable). Its file is packaged as it is.
    teaser = tmp_path / "extras" / "clip" / "teaser.mp4"
    teaser.parent.mkdir(parents=True)
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        "-metadata:s:a:0", "language=eng", str(teaser))
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    monkeypatch.setattr(extras, "_INBOX_ROOT", tmp_path / "packages" / "_inbox")
    catalog = _ExtrasCatalog(ClaimedExtra(
        id=EXTRA, parent_id=ITEM, kind="teaser", title="Teaser", path=str(teaser),
        state="transcoded"))
    root = _package_extra(catalog)

    assert [status for status, _ in catalog.steps] == ["in_progress", "done"]
    manifest = json.loads((root / pk.MANIFEST_FILE).read_text())
    assert (manifest["type"], manifest["extraKind"], manifest["title"]) == (
        "extra", "teaser", "Teaser")
    assert [(v["width"], v["height"]) for v in manifest["renditions"]["video"]] == [(640, 360)]
    assert [(a["language"], a["name"]) for a in manifest["renditions"]["audio"]] == [
        ("eng", "English")]
    assert [uri for tag, _a, uri in _master(root) if tag == "#EXT-X-STREAM-INF"] == [
        "v0/playlist.m3u8"]
    # No handoff, so the source block is a probe of the file.
    assert {k: catalog.packages[0]["source"][k] for k in ("codec", "width", "height")} == {
        "codec": "h264", "width": 640, "height": 360}
    assert (root / ".complete").exists() and not (root / "trickplay").exists()
