"""Real ffmpeg + shaka-packager runs into the library v2 tree (skipped
unless both are on PATH, and libx265 is there to make the HEVC clip).

A generated HEVC clip — two audio tracks, one of them 5.1, two subtitle
tracks, one forced, two chapters — with a subtitle file and an .nfo
beside it goes through the worker as a v2 worker record hands it over:
its source folder and its version folder land in the title's record,
whole (the chain, every file's digest), and the catalog gets the v2
handover. An extra of the title, with the transcoder's handoff on an
HEVC ladder, lands in the title's extras/. When a checkout of the
schemas repository is beside this one (or ZAENTRUM_SCHEMAS names one)
whose schemas have the platform's additive fields, and a Python with
jsonschema runs its validator (this one, or LIBRARY_V2_PYTHON), the
tree is validated with --check-checksums."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from packager import extras, library, worker
from packager import libv2_records as rec
from packager import packager as pk
from packager.katalog import ClaimedExtra, ClaimedItem, Handover


def _has_x265() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL).stdout
    return "libx265" in out


pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe", "packager")) or not _has_x265(),
    reason="needs ffmpeg/ffprobe with libx265 and shaka-packager on PATH",
)

ITEM = "c0ffee00-0000-4000-8000-0000000000a2"
SOURCE = "5011ce00-0000-4000-8000-0000000000a2"
VERSION = "7e510000-0000-4000-8000-0000000000a2"
EXTRA = "e8740000-0000-4000-8000-0000000000a2"
ASSET = "5ab70000-0000-4000-8000-0000000000a2"


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    """The original, in its arrival folder, with its subtitle file and its
    .nfo beside it."""
    d = tmp_path_factory.mktemp("clip")
    for name, body in {
        "en.srt": "1\n00:00:01,000 --> 00:00:03,000\nHello.\n",
        "forced.srt": "1\n00:00:02,000 --> 00:00:04,000\n[foreign words]\n",
    }.items():
        (d / name).write_text(body)
    (d / "chapters.txt").write_text(
        ";FFMETADATA1\ntitle=Clip\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=6000\ntitle=One\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=6000\nEND=12000\ntitle=Two\n")
    folder = d / "incoming" / "Clip (2024)"
    folder.mkdir(parents=True)
    src = folder / "Clip (2024).mkv"
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=12",
        "-i", str(d / "en.srt"), "-i", str(d / "forced.srt"), "-i", str(d / "chapters.txt"),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3", "-map", "4",
        "-map_metadata", "5", "-map_chapters", "5",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error:keyint=48",
        "-c:a:0", "aac", "-ac:a:0", "6", "-c:a:1", "ac3", "-ac:a:1", "2", "-c:s", "srt",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=ger",
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
        "-metadata:s:s:1", "title=Forced", "-disposition:s:1", "forced", str(src))
    (folder / "Clip (2024).de.srt").write_text("1\n00:00:05,000 --> 00:00:07,000\nHallo.\n")
    (folder / "Clip (2024).nfo").write_text("<movie><title>Clip</title></movie>\n")
    return src


class Catalog:
    def __init__(self) -> None:
        self.written: list[tuple[str, dict]] = []
        self.handovers: list[dict] = []

    def settings(self) -> dict:
        return {"packager.language_whitelist": "en,de"}

    def get_steps(self, _id: str) -> dict:
        return {"transcode": "not_applicable"}

    def upsert_step(self, _id: str, status: str, **kw: object) -> None:
        self.written.append((status, kw))

    upsert_extra_step = upsert_step

    def packaging_complete_v2(self, _id: str, payload: dict) -> Handover:
        self.handovers.append(payload)
        return Handover(True, 200, {"current": True}, None)

    extra_packaging_complete_v2 = packaging_complete_v2


def _share(tmp_path: Path, clip: Path) -> tuple[Path, Path, Path]:
    """A library root and, apart from it, a work tree (the validator from
    before the platform's changes takes no dot-entry at the root); the
    original copied into the work tree's arrivals."""
    lib, work = tmp_path / "library", tmp_path / "work"
    arrival = work / "incoming" / clip.parent.name
    shutil.copytree(clip.parent, arrival)
    return lib, work, arrival / clip.name


def _record(lib: Path, work: Path, original: Path) -> dict:
    item_dir = lib / "movies" / ITEM[:2] / ITEM
    return {
        "id": ITEM, "type": "movie", "title": "Clip", "year": 2024, "durationMs": 12_000,
        "path": str(original), "movieTmdbId": None,
        "subtitleFiles": [{"id": ASSET, "path": str(original.parent / "Clip (2024).de.srt"),
                           "language": "ger", "label": "Deutsch"}],
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
                      "segments": [{"kind": "credits", "startMs": 10_000, "endMs": 12_000,
                                    "detector": "chapter", "confidence": 0.8, "label": None}]},
            "current": None,
        },
    }


def _catalogs_part(item_dir: Path, item_type: str = "movie", title: str = "Clip") -> None:
    """What katalog-manager writes in the item folder: item.json with its
    checksums, metadata.json."""
    item_id = item_dir.name
    body = rec.json_bytes({"schema": "zaentrum.library.item/2", "itemId": item_id,
                           "type": item_type, "title": title, "externalIds": {},
                           "createdAt": "2026-10-06T08:00:00Z"})
    (item_dir / "item.json").write_bytes(body)
    sums, _ = rec.checksums([rec.record_entry("item.json", body)])
    (item_dir / "checksums.sha256").write_bytes(sums)
    (item_dir / "metadata.json").write_bytes(rec.json_bytes({
        "schema": "zaentrum.library.metadata/2", "itemId": item_id, "type": item_type,
        "asOf": "2026-10-06T08:00:00Z", "projectedBy": "katalog-manager",
        "titles": {"primary": title}, "images": []}))


def _validator(extras: bool) -> list[str] | None:
    """The validator and the Python to run it with, when there is a schemas
    checkout whose package schema has the platform's additive fields (S1)
    — and, for a tree with an extra, whose extra schema has packagedFrom
    (S2) — and a Python with jsonschema and referencing."""
    root = Path(os.environ.get("ZAENTRUM_SCHEMAS")
                or Path(__file__).resolve().parents[2] / "schemas")
    tool = root / "tools" / "validate-library-v2.py"
    schemas = root / "library" / "v2"
    if not tool.is_file():
        return None
    try:
        package = json.loads((schemas / "package.schema.json").read_text())
        extra = json.loads((schemas / "extra.schema.json").read_text())
    except (OSError, ValueError):
        return None
    if "audioSurround" not in package["properties"]["renditions"]["properties"] \
            or (extras and "packagedFrom" not in extra["properties"]):
        return None
    python = os.environ.get("LIBRARY_V2_PYTHON") or sys.executable
    ok = subprocess.run([python, "-c", "import jsonschema, referencing"], capture_output=True)
    if ok.returncode != 0:
        return None
    return [python, str(tool), "--schemas", str(schemas)]


def _validate(lib: Path, *, extras: bool = False) -> None:
    cmd = _validator(extras)
    if cmd is None:
        pytest.skip("no schemas checkout with the platform's fields, or no Python with "
                    "jsonschema, to validate with")
    out = subprocess.run([*cmd, "--check-checksums", str(lib)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.rstrip().endswith("OK"), out.stdout


@pytest.fixture(scope="module")
def packaged(tmp_path_factory, clip: Path):
    """One v2 run of the clip, through the worker."""
    tmp = tmp_path_factory.mktemp("v2")
    lib, work, original = _share(tmp, clip)
    catalog = Catalog()
    record = _record(lib, work, original)
    worker._process_one(ClaimedItem.from_json(record), catalog,  # type: ignore[arg-type]
                        pk.PackageOptions(surround_codec="eac3", hls_subtitles=True,
                                          preferred_languages=("en",)))
    return lib, work, original, record, catalog


def test_a_v2_run_puts_the_source_and_the_version_into_the_record(packaged) -> None:
    lib, work, original, record, catalog = packaged
    assert [s for s, _ in catalog.written] == ["in_progress", "done"], catalog.written
    item_dir = Path(record["library"]["itemDir"])
    vdir = Path(record["library"]["build"]["versionDir"])
    sdir = Path(record["library"]["source"]["recordDir"])
    assert sorted(p.name for p in item_dir.iterdir()) == ["sources", "versions"]
    assert sorted(p.name for p in vdir.iterdir()) == [
        ".complete", "checksums.sha256", "hls", "package.json", "subs", "trickplay",
        "version.json"]
    assert sorted(p.name for p in sdir.iterdir()) == [
        "Clip (2024).de.srt", "Clip (2024).nfo", "checksums.sha256", "ffprobe.json",
        "source.json"]
    # The chain holds, every file's digest included; the package plays.
    assert rec.chain_problems(str(vdir), "version.json", rec.PACKAGE_DIRS, full=True) == []
    package = json.loads((vdir / "package.json").read_text())
    pk._verify_staged(vdir, package)
    # Nothing of the run is left: no staging, no inbox, no legacy files.
    assert not (work / "staging" / VERSION).exists()
    assert not [p for p in lib.rglob("*") if p.name in (pk.MANIFEST_FILE, ".failed", ".next")]

    source = json.loads((sdir / "source.json").read_text())
    assert [s["type"] for s in source["streams"]] == [
        "video", "audio", "audio", "subtitle", "subtitle"]
    assert source["streams"][0]["codec"] == "hevc"
    assert source["essence"]["chapters"] is True and source["essence"]["surround"] is True
    assert [x["originalName"] for x in source["sidecars"]] == [
        "Clip (2024).de.srt", "Clip (2024).nfo"]

    version = json.loads((vdir / "version.json").read_text())
    # The catalog has no chapters: the original's own are kept.
    assert [(c["startMs"], c["endMs"], c["title"]) for c in version["chapters"]] == [
        (0, 6000, "One"), (6000, 12000, "Two")]
    assert version["chaptersFrom"] == "original-file"
    assert version["sourceIds"] == [SOURCE] and version["originalFiles"] == []

    assert package["role"] == "canonical"
    assert [v["codec"][:4] for v in package["renditions"]["video"]] == ["hvc1"]
    assert [(a["id"], a["language"], a["sourceStreamIndex"]) for a in
            package["renditions"]["audio"]] == [("a0", "eng", 1), ("a1", "ger", 2)]
    assert [(a["id"], a["channels"]) for a in package["renditions"]["audioSurround"]] == [
        ("a2", 6)]
    subs = package["subtitles"]
    assert [(s["id"], s.get("sourceStreamIndex"), s.get("fromSidecar"), s["default"]) for s in
            subs] == [("sub0", 3, None, False), ("sub1", 4, None, False),
                      ("sub2", None, f"sources/{SOURCE}/Clip (2024).de.srt", False)]
    assert subs[1]["forced"] is True and subs[1]["purpose"] == "forced"

    [payload] = catalog.handovers
    assert payload["layout"] == "v2" and payload["versionDir"] == str(vdir)
    assert payload["complete"] == (vdir / ".complete").read_text().strip()
    assert payload["package"] == package
    assert payload["sidecars"] == [{"subtitleAssetId": ASSET, "rendition": "sub2",
                                    "path": "subs/2.vtt"}]
    assert payload["source"]["codec"] == "hevc"


def test_the_tree_validates(packaged) -> None:
    lib, _work, _original, record, _catalog = packaged
    _catalogs_part(Path(record["library"]["itemDir"]))
    _validate(lib)


def test_an_extra_on_an_hevc_ladder_lands_in_its_titles_extras(
    tmp_path: Path, clip: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib, work = tmp_path / "library", tmp_path / "work"
    title_dir = lib / "movies" / ITEM[:2] / ITEM
    trailer = work / "extras" / "clip" / "trailer.mkv"
    trailer.parent.mkdir(parents=True)
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error:keyint=48",
        "-c:a", "aac", "-ac", "2", str(trailer))
    inbox = work / "inbox" / f"extra-{EXTRA}"
    inbox.mkdir(parents=True)
    _ff("-i", str(trailer), "-vf", "scale=426:240,setsar=1,format=yuv420p",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
        "log-level=error:keyint=48:min-keyint=48:scenecut=0", "-an", "-sn", "-dn",
        "-f", "matroska", str(inbox / "v1.mkv"))
    (inbox / "renditions.json").write_text(json.dumps({
        "version": 1, "itemId": EXTRA, "segmentSeconds": 6, "keyframes": "source",
        "timestampOffset": 0.0,
        "video": [{"id": "v0", "label": "source", "file": None, "mode": "copy", "encoder": "copy"},
                  {"id": "v1", "label": "240p", "file": "v1.mkv", "mode": "encode",
                   "encoder": "libx265"}]}))
    monkeypatch.setattr(extras, "_INBOX_ROOT", tmp_path / "packages" / "_inbox")
    record = {
        "id": EXTRA, "type": "extra", "parentId": ITEM, "parentType": "movie",
        "parentTitle": "Clip", "kind": "trailer", "title": "Trailer", "language": "zxx",
        "seasonNumber": None, "path": str(trailer), "state": "transcoded", "removedAt": None,
        "library": {
            "contract": 1, "itemDir": str(title_dir), "inboxDir": str(inbox),
            "stagingDir": str(work / "staging" / f"extra-{EXTRA}"),
            "extraDir": str(title_dir / "extras" / EXTRA), "recorded": False,
            "record": {"kind": "trailer", "title": "Trailer", "localizedTitles": {},
                       "language": "zxx", "seasonNumber": None, "origin": None,
                       "createdAt": "2026-10-06T08:00:00Z", "createdBy": "katalog-manager/api"},
            "original": {"name": "trailer.mkv", "sizeBytes": trailer.stat().st_size,
                         "qh1": rec.qh1(str(trailer))},
        },
    }
    catalog = Catalog()
    extras._process_extra(ClaimedExtra.from_json(EXTRA, record), {"extraId": EXTRA},
                          catalog, pk.PackageOptions(surround_codec="off"))  # type: ignore[arg-type]
    assert [s for s, _ in catalog.written] == ["in_progress", "done"], catalog.written
    xdir = title_dir / "extras" / EXTRA
    assert sorted(p.name for p in xdir.iterdir()) == [
        ".complete", "checksums.sha256", "extra.json", "hls", "package.json"]
    assert rec.chain_problems(str(xdir), "extra.json", rec.EXTRA_DIRS, full=True) == []
    package = json.loads((xdir / "package.json").read_text())
    assert [(v["id"], v["height"], v["codec"][:4]) for v in package["renditions"]["video"]] == [
        ("v0", 360, "hvc1"), ("v1", 240, "hvc1")]
    doc = json.loads((xdir / "extra.json").read_text())
    assert doc["language"] == "zxx" and doc["originalFiles"] == []
    assert doc["packagedFrom"][0]["fixity"] == {"qh1": rec.qh1(str(trailer))}
    assert not inbox.exists() and not (work / "staging" / f"extra-{EXTRA}").exists()
    assert catalog.handovers[0]["extraDir"] == str(xdir)

    _catalogs_part(title_dir)
    _validate(lib, extras=True)


def test_a_v2_run_that_meets_its_version_in_place_reports_it_again(
    tmp_path: Path, clip: Path,
) -> None:
    # The run renamed its version into place and died before the catalog
    # took it: the retry finds the version whole and hands it over as it is.
    lib, work, original = _share(tmp_path, clip)
    record = _record(lib, work, original)
    first = Catalog()
    worker._process_one(ClaimedItem.from_json(record), first,  # type: ignore[arg-type]
                        pk.PackageOptions(surround_codec="off"))
    vdir = Path(record["library"]["build"]["versionDir"])
    before = {p: p.stat().st_mtime_ns for p in vdir.rglob("*")}
    again = Catalog()
    worker._process_one(ClaimedItem.from_json(record), again,  # type: ignore[arg-type]
                        pk.PackageOptions(surround_codec="off"))
    assert [s for s, _ in again.written] == ["in_progress", "done"]
    assert again.handovers == first.handovers
    assert {p: p.stat().st_mtime_ns for p in vdir.rglob("*")} == before
    assert library.verify_chain(vdir, "version.json") is None
