"""Real ffmpeg + shaka-packager runs (skipped unless both are on PATH).

Builds a transcoder-style handoff from a generated lavfi clip — the
original (H.264, 5.1 AAC English, stereo AC-3 German, three SRT tracks
one of them forced) plus two lower rungs with the source's keyframes —
and packages it: N video variants x 2 audio groups, I-frame playlists,
the 5.1 companion, DEFAULT/FORCED flags, aligned segments, the manifest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from packager import packager as pk
from packager import worker
from packager.hls import parse_attributes
from packager.renditions import resolve_inputs

pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe", "packager")),
    reason="needs ffmpeg, ffprobe and shaka-packager on PATH",
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


def _package(tmp_path: Path, monkeypatch, src: Path, inbox: Path | None, **options):
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    inputs = resolve_inputs(inbox, str(src)) if inbox else None
    manifest = pk.package_item(
        ITEM, str(src), "movie", title="clip", inputs=inputs,
        options=pk.PackageOptions(preferred_languages=("en",), **options),
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
    assert [(a["NAME"], a["DEFAULT"], a.get("FORCED")) for a in subs] == [
        ("English", "NO", None), ("English (Forced)", "NO", "YES"), ("German", "NO", None)]

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
    assert all((root / s["path"]).exists() for s in manifest["subtitles"])
    assert manifest["hls"] == {"master": "hls/master.m3u8", "segmentSeconds": 6,
                               "audioGroups": ["audio", "audio-surround"],
                               "subtitleGroup": "subs"}
    assert not list((root / "hls").rglob(".shaka-master.m3u8"))


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
