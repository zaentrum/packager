"""Real ffmpeg + shaka-packager runs of sources with surround tracks, with
SURROUND_AUDIO=eac3 (skipped unless both are on PATH, ffmpeg is 7 or
newer, and it has libx265 and the AC-3, E-AC-3 and FLAC encoders).

A film in four languages — an English AC-3 5.1, a German FLAC 7.1, a
French stereo AAC, a Spanish E-AC-3 5.1 and a second English 5.1 —, a
plain 5.1 film and a stereo one. Every track becomes a stereo AAC
rendition in group "audio"; every surround track's language gets one 5.1
E-AC-3 companion in group "audio-surround" (encoded, the 7.1 downmixed,
the E-AC-3 5.1 copied bit for bit), shown or hidden as its stereo
rendition, with exactly one default; a stereo film gets none. In both
layouts: the legacy manifest.json, and the v2 record, whose package.json
validates against the schemas (validate-library-v2.py --check-checksums,
where a schemas checkout and a Python with jsonschema are at hand) and
whose essence no longer loses "surround" — nor "maxAudioChannels", but
for the 7.1, whose two extra channels a 5.1 can't keep."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_library_real import Catalog, _catalogs_part, _validate

from packager import libv2_records as rec
from packager import packager as pk
from packager import worker
from packager.hls import parse_attributes
from packager.katalog import ClaimedItem


def _ffmpeg_major() -> int:
    first = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.split("\n", 1)[0]
    m = re.search(r"version n?(\d+)\.", first)
    return int(m.group(1)) if m else 99


def _encoders() -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout


pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe", "packager"))
    or _ffmpeg_major() < 7
    or not all(f" {e} " in _encoders() for e in ("libx265", "ac3", "eac3", "flac")),
    reason="needs ffmpeg/ffprobe >= 7 (libx265, ac3, eac3, flac) and shaka-packager on PATH",
)

ITEM = "c0ffee00-0000-4000-8000-0000000000d1"
SOURCE = "5011ce00-0000-4000-8000-0000000000d1"
VERSION = "7e510000-0000-4000-8000-0000000000d1"


def _tones(channels: int) -> str:
    """An aevalsrc expression list: a tone of its own on every channel."""
    return "|".join(f"0.1*sin(2*PI*{110 * (k + 1)}*t)" for k in range(channels))


def _film(path: Path, audio: list[tuple[str, int, str]]) -> Path:
    """An 8 s HEVC film with the given audio tracks: (codec, channels,
    language) each, every one of them its own tones."""
    path.parent.mkdir(parents=True)
    layouts = {2: "stereo", 6: "5.1", 8: "7.1"}
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=8"]
    for _codec, channels, _language in audio:
        args += ["-f", "lavfi", "-i", f"aevalsrc=exprs={_tones(channels)}:"
                 f"channel_layout={layouts[channels]}:sample_rate=48000:duration=8"]
    args += ["-map", "0:v", *[a for i in range(len(audio)) for a in ("-map", f"{i + 1}:a")],
             "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error:keyint=48"]
    for i, (codec, _channels, language) in enumerate(audio):
        args += [f"-c:a:{i}", codec, f"-metadata:s:a:{i}", f"language={language}"]
    subprocess.run([*args, str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def films(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("films")
    return {
        "multi": _film(d / "Multi (2024)" / "Multi (2024).mkv", [
            ("ac3", 6, "eng"), ("flac", 8, "ger"), ("aac", 2, "fre"), ("eac3", 6, "spa"),
            ("aac", 6, "eng")]),
        "six": _film(d / "Six (2024)" / "Six (2024).mkv", [("ac3", 6, "eng"), ("aac", 2, "ger")]),
        "stereo": _film(d / "Stereo (2024)" / "Stereo (2024).mkv",
                        [("aac", 2, "eng"), ("ac3", 2, "ger")]),
    }


def _media(root: Path) -> list[dict[str, str]]:
    return [parse_attributes(line.split(":", 1)[1])
            for line in (root / "hls" / "master.m3u8").read_text().splitlines()
            if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO")]


def _variants(root: Path) -> list[dict[str, str]]:
    return [parse_attributes(line.split(":", 1)[1])
            for line in (root / "hls" / "master.m3u8").read_text().splitlines()
            if line.startswith("#EXT-X-STREAM-INF:")]


def _decoded(rendition: Path) -> tuple[str, int, str]:
    """(codec, channels, layout) of a rendition, its init and segments read
    as one file."""
    joined = rendition / ".joined.mp4"
    joined.write_bytes((rendition / "init.mp4").read_bytes() + b"".join(
        s.read_bytes() for s in sorted(rendition.glob("seg-*.m4s"))))
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "stream=codec_name,channels,channel_layout", "-of", "json",
                              str(joined)], capture_output=True, text=True, check=True).stdout
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(joined),
                        "-f", "null", "-"], capture_output=True, check=True)
    finally:
        joined.unlink()
    [s] = json.loads(out)["streams"]
    return s["codec_name"], s["channels"], s.get("channel_layout", "")


def _raw_eac3(path: Path, stream: str) -> bytes:
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", stream,
                           "-c", "copy", "-f", "eac3", "-"], capture_output=True, check=True).stdout


def test_a_film_in_four_languages_keeps_a_5_1_per_surround_language(
    tmp_path: Path, monkeypatch, films,
) -> None:
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    manifest = pk.package_item(
        ITEM, str(films["multi"]), "movie", title="multi", language_whitelist=["en", "de", "fr"],
        options=pk.PackageOptions(surround_codec="eac3", preferred_languages=("en",)))
    root = tmp_path / "packages" / "movies" / ITEM[:2] / ITEM
    ren = manifest["renditions"]
    assert [(a["id"], a["language"], a["channels"], a["visible"], a["default"])
            for a in ren["audio"]] == [
        ("a0", "eng", 2, True, True), ("a1", "ger", 2, True, False),
        ("a2", "fre", 2, True, False), ("a3", "spa", 2, False, False),
        ("a4", "eng", 2, True, False)]
    # One 5.1 per surround language: the second English one has none, the
    # stereo French none; the Spanish, hidden by the whitelist, has its own,
    # hidden too.
    assert [(a["id"], a["idx"], a["language"], a["codec"], a["channels"], a["default"],
             a["visible"], a["mode"], a["group"], a["name"]) for a in ren["audioSurround"]] == [
        ("a5", 0, "eng", "ec-3", 6, True, True, "encode", "audio-surround", "English 5.1"),
        ("a6", 1, "ger", "ec-3", 6, False, True, "encode", "audio-surround", "German 5.1"),
        ("a7", 3, "spa", "ec-3", 6, False, False, "copy", "audio-surround", "Spanish 5.1")]
    assert manifest["hls"]["audioGroups"] == ["audio", "audio-surround"]

    surround = [m for m in _media(root) if m["GROUP-ID"] == "audio-surround"]
    assert [(m["URI"], m["LANGUAGE"], m["NAME"], m["DEFAULT"], m.get("AUTOSELECT"),
             m["CHANNELS"]) for m in surround] == [
        ("a5/playlist.m3u8", "en", "English 5.1", "YES", "YES", "6"),
        ("a6/playlist.m3u8", "de", "German 5.1", "NO", "YES", "6"),
        ("a7/playlist.m3u8", "es", "Spanish 5.1", "NO", None, "6")]
    stereo = [m for m in _media(root) if m["GROUP-ID"] == "audio"]
    assert sum(m["DEFAULT"] == "YES" for m in stereo) == 1
    variants = _variants(root)
    assert [(v["AUDIO"], v["CODECS"].split(",")[1]) for v in variants] == [
        ("audio", "mp4a.40.2"), ("audio-surround", "ec-3")]
    assert int(variants[1]["BANDWIDTH"]) > int(variants[0]["BANDWIDTH"])

    # Each companion plays as a 5.1: the AC-3 and the FLAC 7.1 encoded (the
    # 7.1 downmixed), the E-AC-3 5.1 copied bit for bit.
    for rid in ("a5", "a6", "a7"):
        assert _decoded(root / "hls" / rid) == ("eac3", 6, "5.1(side)")
    joined = root / "hls" / "a7" / ".copy.mp4"
    joined.write_bytes((root / "hls" / "a7" / "init.mp4").read_bytes() + b"".join(
        s.read_bytes() for s in sorted((root / "hls" / "a7").glob("seg-*.m4s"))))
    assert _raw_eac3(joined, "0:a:0") == _raw_eac3(films["multi"], "0:a:3")
    joined.unlink()
    # ... and every stereo rendition stays a stereo AAC.
    for rid in ("a0", "a1", "a2", "a3", "a4"):
        assert _decoded(root / "hls" / rid) == ("aac", 2, "stereo")


def test_a_stereo_film_gets_no_companion(tmp_path: Path, monkeypatch, films) -> None:
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    manifest = pk.package_item(ITEM, str(films["stereo"]), "movie", title="stereo",
                               options=pk.PackageOptions(surround_codec="eac3"))
    root = tmp_path / "packages" / "movies" / ITEM[:2] / ITEM
    assert manifest["renditions"]["audioSurround"] == []
    assert manifest["hls"]["audioGroups"] == ["audio"]
    assert {m["GROUP-ID"] for m in _media(root)} == {"audio"}
    assert [v["AUDIO"] for v in _variants(root)] == ["audio"]


# ------------------------------------------------------------- the v2 record

def _record(lib: Path, work: Path, original: Path) -> dict:
    item_dir = lib / "movies" / ITEM[:2] / ITEM
    return {
        "id": ITEM, "type": "movie", "title": original.stem, "year": 2024,
        "path": str(original),
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
                      "segments": None},
            "current": None,
        },
    }


def _v2(tmp_path: Path, film: Path, surround: str) -> tuple[Path, dict, dict]:
    """One v2 run of film through the worker: the library root, the
    package record and the source record."""
    lib, work = tmp_path / "library", tmp_path / "work"
    arrival = work / "incoming" / film.parent.name
    shutil.copytree(film.parent, arrival)
    record = _record(lib, work, arrival / film.name)
    catalog = Catalog()
    worker._process_one(ClaimedItem.from_json(record), catalog,  # type: ignore[arg-type]
                        pk.PackageOptions(surround_codec=surround))
    assert [s for s, _ in catalog.written] == ["in_progress", "done"], catalog.written
    item_dir = Path(record["library"]["itemDir"])
    package = json.loads((item_dir / "versions" / VERSION / "package.json").read_text())
    source = json.loads((item_dir / "sources" / SOURCE / "source.json").read_text())
    assert catalog.handovers[0]["package"] == package
    _catalogs_part(item_dir, title=film.stem)
    return lib, package, source


def _gate(source: dict, package: dict) -> list[str]:
    return rec.deletion_gate([source["essence"]], package["essence"])


def test_a_v2_record_carries_the_companions_and_validates(tmp_path: Path, films) -> None:
    lib, package, source = _v2(tmp_path, films["multi"], "eac3")
    ren = package["renditions"]
    assert [a["id"] for a in ren["audio"]] == ["a0", "a1", "a2", "a3", "a4"]
    # Each companion names the track of the original it was made from, and
    # how many channels that track has.
    assert [(a["id"], a["language"], a["codec"], a["channels"], a["sourceStreamIndex"],
             a["sourceChannels"], a["default"], a["group"], a["name"], a["purpose"])
            for a in ren["audioSurround"]] == [
        ("a5", "eng", "ec-3", 6, 1, 6, True, "audio-surround", "English 5.1", "main"),
        ("a6", "ger", "ec-3", 6, 2, 8, False, "audio-surround", "German 5.1", "main"),
        ("a7", "spa", "ec-3", 6, 4, 6, False, "audio-surround", "Spanish 5.1", "main")]
    assert package["hls"]["audioGroups"] == ["audio", "audio-surround"]
    # The package no longer loses surround. A 5.1 can't keep a 7.1's two
    # extra channels: those, and the FLAC's lossless coding, are the
    # losses the gate still names.
    assert source["essence"]["maxAudioChannels"] == 8
    assert (package["essence"]["surround"], package["essence"]["maxAudioChannels"]) == (True, 6)
    assert _gate(source, package) == ["losslessAudio", "maxAudioChannels"]
    assert [(x["kind"], x["detail"]) for x in package["fidelity"]["losses"]] == [
        ("audio-downmix", "8ch -> 6ch")]
    _validate(lib)


def test_a_5_1_source_loses_neither_surround_nor_channels(tmp_path: Path, films) -> None:
    lib, package, source = _v2(tmp_path / "on", films["six"], "eac3")
    assert [(a["language"], a["channels"]) for a in package["renditions"]["audioSurround"]] == [
        ("eng", 6)]
    assert _gate(source, package) == []
    _validate(lib)
    # Without the companion, both were lost.
    _lib, stereo_only, source = _v2(tmp_path / "off", films["six"], "off")
    assert "audioSurround" not in stereo_only["renditions"]
    assert stereo_only["hls"]["audioGroups"] == ["audio"]
    assert _gate(source, stereo_only) == ["maxAudioChannels", "surround"]


def test_a_stereo_sources_v2_record_has_no_surround_group(tmp_path: Path, films) -> None:
    lib, package, source = _v2(tmp_path, films["stereo"], "eac3")
    assert "audioSurround" not in package["renditions"]
    assert package["hls"]["audioGroups"] == ["audio"]
    assert _gate(source, package) == []
    _validate(lib)
