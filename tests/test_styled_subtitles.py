"""An original whose subtitles are styled (ASS) and that carries the fonts
they are drawn with, packaged into the library v2 tree with real ffmpeg —
its probe, the remux, the audio, the subtitles, the fonts, the trickplay —
and a stand-in for shaka-packager alone, so it runs wherever ffmpeg with
libx265 is (skipped elsewhere).

The package keeps each styled track's script beside its WebVTT copy, as
the track holds it, and a styled file beside the original as its bytes
are; it takes every font the original carries out of it, byte for byte,
and no other attachment. package.json names them — a subtitle's `styled`,
the package's `fonts` — and its essence has styled subtitles and fonts, so
the deletion gate holds the original for neither; a package that kept a
styled track as WebVTT alone, or left a font out, would still be held. The
WebVTT entry a client reads is the one it was. The package store's
manifest.json says the same of a package made there. When a checkout of the
schemas repository with record logic 10's fields is at hand (see
test_library_real._validator), the tree is validated with
--check-checksums."""

from __future__ import annotations

import codecs
import json
import shutil
import subprocess
from pathlib import Path

import fontmaker
import pytest
from test_library_real import Catalog, _catalogs_part, _has_x265, _validator

from packager import libv2_records as rec
from packager import packager as pk
from packager import worker
from packager.katalog import ClaimedItem

pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe")) or not _has_x265(),
    reason="needs ffmpeg/ffprobe with libx265 on PATH",
)

ITEM = "c0ffee00-0000-4000-8000-0000000000f5"
SOURCE = "5011ce00-0000-4000-8000-0000000000f5"
VERSION = "7e510000-0000-4000-8000-0000000000f5"
ASSET = "5ab70000-0000-4000-8000-0000000000f5"

SANS = fontmaker.truetype("Example Sans")
SERIF = fontmaker.truetype("Example Serif", "Bold")
FAMILY = fontmaker.collection(("Example Display", "Regular"), ("Example Display", "Italic"))

STYLES = ("[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
          "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
          "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
          "Style: Default,Example Sans,28,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,"
          "100,100,0,0,1,2,1,2,20,20,20,1\n"
          "Style: Sign,Example Display,20,&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,"
          "100,100,0,0,1,1,0,8,10,10,10,1\n\n")
EVENTS = ("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, "
          "Text\n")
DIALOGUE = ("[Script Info]\nTitle: Dialogue\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n\n"
            + STYLES + EVENTS
            + "Dialogue: 0,0:00:00.50,0:00:01.50,Default,,0,0,0,,{\\i1}One{\\i0}\\Ntwo\n"
            "Dialogue: 0,0:00:02.00,0:00:03.20,Default,,0,0,0,,"
            "{\\fnExample Serif\\b1}Three{\\b0}\n")
SIGNS = ("[Script Info]\nTitle: Signs\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n\n"
         + STYLES + EVENTS
         + "Dialogue: 1,0:00:01.00,0:00:02.40,Sign,,0,0,0,,"
         "{\\an8\\pos(320,40)\\fad(100,100)}Sign\n")
# A styled file beside the original, as an editor saves one: UTF-8 with a
# byte order mark, CRLF line ends.
BESIDE = codecs.BOM_UTF8 + DIALOGUE.replace("Title: Dialogue", "Title: Beside").replace(
    "\n", "\r\n").encode("utf-8")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    """The original, in its arrival folder, with a styled file beside it:
    HEVC, a stereo AAC track, two ASS tracks and an SRT one, three fonts
    attached (by the MIME types Matroska files carry them under, one of
    them a collection) and a cover image."""
    d = tmp_path_factory.mktemp("styled")
    (d / "dialogue.ass").write_text(DIALOGUE)
    (d / "signs.ass").write_text(SIGNS)
    (d / "de.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHallo.\n")
    attached = {"sans.ttf": (SANS, "application/x-truetype-font", "Example Sans.ttf"),
                "serif.ttf": (SERIF, "font/ttf", "Example Serif Bold.ttf"),
                "cover.jpg": (b"\xff\xd8\xff\xe0 a cover", "image/jpeg", "cover.jpg"),
                "display.ttc": (FAMILY, "font/collection", "Example Display.ttc")}
    args = ["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
            "-i", str(d / "dialogue.ass"), "-i", str(d / "signs.ass"), "-i", str(d / "de.srt"),
            "-map", "0:v", "-map", "1:a", "-map", "2", "-map", "3", "-map", "4",
            "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error:keyint=48",
            "-c:a", "aac", "-c:s:0", "copy", "-c:s:1", "copy", "-c:s:2", "srt",
            "-metadata:s:a:0", "language=jpn", "-metadata:s:s:0", "language=eng",
            "-metadata:s:s:1", "language=eng", "-metadata:s:s:1", "title=Signs",
            "-metadata:s:s:2", "language=ger"]
    for i, (name, (body, mimetype, filename)) in enumerate(attached.items()):
        (d / name).write_bytes(body)
        args += ["-attach", str(d / name), f"-metadata:s:t:{i}", f"mimetype={mimetype}",
                 f"-metadata:s:t:{i}", f"filename={filename}"]
    folder = d / "incoming" / "Clip (2024)"
    folder.mkdir(parents=True)
    src = folder / "Clip (2024).mkv"
    _ff(*args, str(src))
    (folder / "Clip (2024).fr.ass").write_bytes(BESIDE)
    return src


def _shaka(_primary, videos, audio_meta, surround_meta, out_root: Path, **_kw):
    """shaka-packager's part, stood in for: a master, a media playlist and
    two segments for each rendition, the renditions' entries."""
    media = ('#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MAP:URI="init.mp4"\n'
             "#EXTINF:2.000,\nseg-00001.m4s\n#EXTINF:2.000,\nseg-00002.m4s\n#EXT-X-ENDLIST\n")
    master = ["#EXTM3U", '#EXT-X-MEDIA:TYPE=AUDIO,URI="a0/playlist.m3u8",GROUP-ID="audio",'
              'NAME="Japanese",DEFAULT=YES,CHANNELS="2"']
    video = []
    for v in videos:
        master += ['#EXT-X-STREAM-INF:BANDWIDTH=900000,CODECS="hvc1.1.6.L60.90,mp4a.40.2",'
                   'AUDIO="audio"', f"{v.rung.id}/playlist.m3u8"]
        video.append({"id": v.rung.id, "dir": f"hls/{v.rung.id}", "codec": "hvc1.1.6.L60.90",
                      "width": 320, "height": 180, "bitrateBps": 700_000,
                      "peakBitrateBps": 800_000, "hdr": False, "videoRange": "SDR",
                      "frameRate": "24/1", "segments": 2, "targetDuration": 2,
                      "label": v.rung.label, "encoder": v.rung.encoder})
    for rid in [v.rung.id for v in videos] + [f"a{i}" for i in range(len(audio_meta))]:
        d = out_root / "hls" / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "playlist.m3u8").write_text(media)
        for name in ("init.mp4", "seg-00001.m4s", "seg-00002.m4s"):
            (d / name).write_bytes(f"{rid}/{name}".encode())
    (out_root / "hls" / "master.m3u8").write_text("\n".join(master) + "\n")
    audio = [{**a, "id": f"a{i}", "dir": f"hls/a{i}", "codec": "mp4a.40.2",
              "bitrateBps": 192_000, "segments": 2, "group": "audio", "name": "Japanese"}
             for i, a in enumerate(audio_meta)]
    return video, audio, surround_meta


def _record(lib: Path, work: Path, original: Path) -> dict:
    item_dir = lib / "movies" / ITEM[:2] / ITEM
    return {
        "id": ITEM, "type": "movie", "title": "Clip", "year": 2024, "durationMs": 4_000,
        "path": str(original), "movieTmdbId": None,
        "subtitleFiles": [{"id": ASSET, "path": str(original.parent / "Clip (2024).fr.ass"),
                           "language": "fre", "label": "Français"}],
        "library": {
            "contract": 1, "root": str(lib), "itemDir": str(item_dir), "blocked": None,
            "source": {"sourceId": SOURCE, "recorded": False,
                       "recordDir": str(item_dir / "sources" / SOURCE),
                       "libraryPath": f"{original.parent.name}/{original.name}",
                       "sizeBytes": original.stat().st_size, "qh1": rec.qh1(str(original))},
            "inboxDir": str(work / "inbox" / ITEM),
            "build": {"versionId": VERSION, "stagingDir": str(work / "staging" / VERSION),
                      "versionDir": str(item_dir / "versions" / VERSION),
                      "createdBy": "katalog-manager", "chapters": None, "chaptersFrom": None,
                      "segments": []},
            "current": None,
        },
    }


@pytest.fixture(scope="module")
def packaged(tmp_path_factory, clip: Path):
    """One v2 run of the clip, through the worker."""
    tmp = tmp_path_factory.mktemp("v2")
    lib, work = tmp / "library", tmp / "work"
    arrival = work / "incoming" / clip.parent.name
    shutil.copytree(clip.parent, arrival)
    catalog = Catalog()
    record = _record(lib, work, arrival / clip.name)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pk, "_run_shaka_packager", _shaka)
        worker._process_one(ClaimedItem.from_json(record), catalog,  # type: ignore[arg-type]
                            pk.PackageOptions(surround_codec="off"))
    item_dir = Path(record["library"]["itemDir"])
    return lib, item_dir, catalog


def test_a_styled_track_keeps_its_script_beside_its_webvtt_copy(packaged) -> None:
    lib, item_dir, catalog = packaged
    assert [s for s, _ in catalog.written] == ["in_progress", "done"], catalog.written
    vdir = item_dir / "versions" / VERSION
    package = json.loads((vdir / "package.json").read_text())
    # The entries a client reads are the WebVTT ones, as they were; a
    # styled one names its script beside it.
    assert [(s["id"], s["path"], s["format"], s["language"], s.get("styled"))
            for s in package["subtitles"]] == [
        ("sub0", "subs/0.vtt", "webvtt", "eng", {"path": "subs/0.ass", "format": "ass"}),
        ("sub1", "subs/1.vtt", "webvtt", "eng", {"path": "subs/1.ass", "format": "ass"}),
        ("sub2", "subs/2.vtt", "webvtt", "ger", None),
        ("sub3", "subs/3.vtt", "webvtt", "fre", {"path": "subs/3.ass", "format": "ass"}),
    ]
    subs = vdir / "subs"
    # The tracks' scripts as the tracks hold them — every style, every
    # event, every override tag —, the file beside the original as its
    # bytes are.
    assert (subs / "0.ass").read_text() == DIALOGUE
    assert (subs / "1.ass").read_text() == SIGNS
    assert (subs / "3.ass").read_bytes() == BESIDE
    assert "<i>One</i>\ntwo" in (subs / "0.vtt").read_text()
    assert [s.get("sourceStreamIndex") for s in package["subtitles"]] == [2, 3, 4, None]


def test_the_fonts_the_original_carries_are_taken_byte_for_byte_and_nothing_else(
    packaged,
) -> None:
    lib, item_dir, _catalog = packaged
    vdir = item_dir / "versions" / VERSION
    package = json.loads((vdir / "package.json").read_text())
    assert package["fonts"] == [
        {"path": "subs/fonts/0.ttf", "format": "ttf",
         "names": ["Example Sans", "Example Sans Regular", "ExampleSans-Regular"],
         "sourceStreamIndex": 5},
        {"path": "subs/fonts/1.ttf", "format": "ttf",
         "names": ["Example Serif", "Example Serif Bold", "ExampleSerif-Bold"],
         "sourceStreamIndex": 6},
        {"path": "subs/fonts/2.ttc", "format": "ttc",
         "names": ["Example Display", "Example Display Regular", "ExampleDisplay-Regular",
                   "Example Display Italic", "ExampleDisplay-Italic"],
         "sourceStreamIndex": 8},
    ]
    assert {p.name: p.read_bytes() for p in (vdir / "subs" / "fonts").iterdir()} == {
        "0.ttf": SANS, "1.ttf": SERIF, "2.ttc": FAMILY}
    # The fonts are files of the package: the chain lists them, every
    # digest holds.
    sums = (vdir / "checksums.sha256").read_text()
    assert all(f"  {f['path']}\n" in sums for f in package["fonts"])
    assert rec.chain_problems(str(vdir), "version.json", rec.PACKAGE_DIRS, full=True) == []


def test_the_gate_holds_the_original_for_no_style_and_no_font(packaged) -> None:
    lib, item_dir, _catalog = packaged
    vdir = item_dir / "versions" / VERSION
    source = json.loads((item_dir / "sources" / SOURCE / "source.json").read_text())
    package = json.loads((vdir / "package.json").read_text())
    # The three fonts; the cover is an attached picture to ffprobe, a video
    # stream no package takes for the film's.
    assert [(s["index"], s.get("role")) for s in source["streams"]
            if s["type"] == "attachment"] == [(5, "font"), (6, "font"), (8, "font")]
    assert [s["dispositions"].get("attachedPic", False) for s in source["streams"]
            if s["type"] == "video"] == [False, True]
    assert {k: source["essence"][k] for k in ("styledSubtitles", "fonts")} == {
        "styledSubtitles": True, "fonts": True}
    assert {k: package["essence"][k] for k in ("styledSubtitles", "fonts")} == {
        "styledSubtitles": True, "fonts": True}
    assert not {x["kind"] for x in package["fidelity"]["losses"]} & {
        "subtitle-styling", "attachments-dropped"}
    gate = rec.deletion_gate([source["essence"]], package["essence"])
    assert "styledSubtitles" not in gate and "fonts" not in gate, gate
    # Had it kept the Signs track as WebVTT alone, or left a font out, the
    # gate would hold the original for it.
    plain = [{k: v for k, v in s.items() if k != "styled" or s["id"] != "sub1"}
             for s in package["subtitles"]]
    essence = lambda subs, fonts: rec.package_essence(  # noqa: E731
        {"renditions": package["renditions"], "subtitles": subs, "fonts": fonts},
        source["essence"], source["streams"])
    assert [k for k in rec.deletion_gate([source["essence"]], essence(plain, package["fonts"]))
            if k in ("styledSubtitles", "fonts")] == ["styledSubtitles"]
    assert [k for k in rec.deletion_gate([source["essence"]],
                                         essence(package["subtitles"], package["fonts"][:2]))
            if k in ("styledSubtitles", "fonts")] == ["fonts"]


def test_the_tree_validates(packaged) -> None:
    lib, item_dir, _catalog = packaged
    _catalogs_part(item_dir)
    cmd = _validator(extras=False)
    package = Path(cmd[-1]) / "package.schema.json" if cmd else None
    if cmd is None or "fonts" not in json.loads(package.read_text())["properties"]:
        pytest.skip("no schemas checkout with record logic 10's fields, or no Python with "
                    "jsonschema, to validate with")
    out = subprocess.run([*cmd, "--check-checksums", str(lib)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.rstrip().endswith("OK"), out.stdout


def test_the_package_stores_manifest_says_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip: Path,
) -> None:
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    monkeypatch.setattr(pk, "_run_shaka_packager", _shaka)
    manifest = pk.package_item(
        ITEM, str(clip), "movie", subtitle_files=[
            {"path": str(clip.parent / "Clip (2024).fr.ass"), "language": "fre"}],
        trickplay=False, options=pk.PackageOptions(surround_codec="off"))
    root = tmp_path / "packages" / "movies" / ITEM[:2] / ITEM
    assert json.loads((root / pk.MANIFEST_FILE).read_text()) == manifest
    assert [s.get("styled", {}).get("path") for s in manifest["subtitles"]] == [
        "subs/0.ass", "subs/1.ass", None, "subs/3.ass"]
    assert [(f["path"], f["sourceStreamIndex"]) for f in manifest["fonts"]] == [
        ("subs/fonts/0.ttf", 5), ("subs/fonts/1.ttf", 6), ("subs/fonts/2.ttc", 8)]
    assert (root / "subs" / "fonts" / "2.ttc").read_bytes() == FAMILY
    assert (root / ".complete").exists()
