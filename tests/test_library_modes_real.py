"""Real ffmpeg (+ shaka-packager) runs of each build mode into the library
v2 tree (skipped unless ffmpeg and ffprobe with libx265 are on PATH, and
shaka-packager's `packager` for the modes that package).

A generated HEVC clip whose container carries a title and a comment of
its own — two audio tracks, an embedded subtitle, two chapters — with a
subtitle file and an .nfo beside it in its arrival folder goes through the
worker as a v2 worker record of each mode hands it over: an establish
renames it into the version folder it packages, a takein into one with no
package, an add puts a package beside it there, and a repackage builds a
new version and leaves it where it is. Every package's chain holds, every
file's digest included, and it plays; nothing in the title's folder but
the originals its versions keep — no record, no copy, no name — and
nothing in a handover carries the arrival's name or its container's
title. Each run, run again as after a lost handover, hands the same
version over again and writes nothing. When the schemas repository's
validator can run (test_library_real._validator), each tree validates;
without it, the rest of a test stands."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_library_real import Catalog, _catalogs_part, _share, _validator

from packager import libv2_records as rec
from packager import packager as pk
from packager import worker
from packager.katalog import ClaimedItem


def _has_x265() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL).stdout
    return "libx265" in out


pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe")) or not _has_x265(),
    reason="needs ffmpeg/ffprobe with libx265 on PATH",
)
packages = pytest.mark.skipif(shutil.which("packager") is None,
                              reason="needs shaka-packager on PATH")

ITEM = "c0ffee00-0000-4000-8000-0000000000b3"
SOURCE = "5011ce00-0000-4000-8000-0000000000b3"
VERSION = "7e510000-0000-4000-8000-0000000000b3"
NEXT_VERSION = "7e510000-1111-4000-8000-0000000000b3"
ASSET = "5ab70000-0000-4000-8000-0000000000b3"
NAME = "original.mkv"
# What only the arrival's name and the original's container title say: in
# no record, no copy, no name and no handover.
LEAKS = (b"Clip (2024)", b"Container Title Of The Arrival")
# The container's other global tags: a source record keeps them as the probe
# found them, and no package carries them.
GLOBAL_TAGS = (b"Container Comment Of The Arrival",)


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    """The original, in its arrival folder, with its subtitle file and its
    .nfo beside it."""
    d = tmp_path_factory.mktemp("clip")
    (d / "en.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nHello.\n")
    (d / "metadata.txt").write_text(
        ";FFMETADATA1\ntitle=Container Title Of The Arrival\n"
        "comment=Container Comment Of The Arrival\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=6000\ntitle=One\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=6000\nEND=12000\ntitle=Two\n")
    folder = d / "incoming" / "Clip (2024)"
    folder.mkdir(parents=True)
    src = folder / "Clip (2024).mkv"
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=12",
        "-i", str(d / "en.srt"), "-i", str(d / "metadata.txt"),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3",
        "-map_metadata", "4", "-map_chapters", "4",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error:keyint=48",
        "-c:a:0", "aac", "-ac:a:0", "6", "-c:a:1", "ac3", "-ac:a:1", "2", "-c:s", "srt",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=ger",
        "-metadata:s:s:0", "language=eng", str(src))
    (folder / "Clip (2024).de.srt").write_text("1\n00:00:05,000 --> 00:00:07,000\nHallo.\n")
    (folder / "Clip (2024).nfo").write_text("<movie><title>Clip</title></movie>\n")
    return src


class Tree:
    """A library root, its work tree and the arrival of one original."""

    def __init__(self, tmp_path: Path, clip: Path) -> None:
        self.lib, self.work, self.original = _share(tmp_path, clip)
        self.item_dir = self.lib / "movies" / ITEM[:2] / ITEM
        self.source_dir = self.item_dir / "sources" / SOURCE
        # The catalog's record of the original, made at its arrival.
        self.size, self.qh1 = self.original.stat().st_size, rec.qh1(str(self.original))

    def version_dir(self, version: str = VERSION) -> Path:
        return self.item_dir / "versions" / version

    def record(self, mode: str, *, version: str = VERSION, path: Path | None = None,
               copies: bool = False) -> dict:
        """The worker record of a run of `mode`; `copies`: its subtitle file
        named by its copy in the source record, as the catalog names it once
        the version is established."""
        sub = (self.source_dir / "subtitle-1.de.srt" if copies
               else self.original.parent / "Clip (2024).de.srt")
        return {
            "id": ITEM, "type": "movie", "title": "Clip", "year": 2024, "durationMs": 12_000,
            "path": str(path or self.original), "movieTmdbId": None,
            "subtitleFiles": [{"id": ASSET, "path": str(sub), "language": "ger",
                               "label": "Deutsch"}],
            "library": {
                "contract": 1, "root": str(self.lib), "itemDir": str(self.item_dir),
                "blocked": None,
                "source": {"sourceId": SOURCE, "recorded": copies,
                           "recordDir": str(self.source_dir),
                           "libraryPath": "Clip (2024)/Clip (2024).mkv",
                           "sizeBytes": self.size, "qh1": self.qh1},
                "inboxDir": str(self.work / "inbox" / ITEM),
                "build": {"versionId": version, "stagingDir": str(self.work / "staging" / version),
                          "versionDir": str(self.version_dir(version)),
                          "createdBy": "katalog-manager", "chapters": None, "chaptersFrom": None,
                          "segments": [], "mode": mode,
                          "originalName": NAME if mode in ("establish", "takein") else None},
                "current": None,
            },
        }


def _go(body: dict, catalog: Catalog) -> None:
    item = ClaimedItem.from_json(body)
    assert item.library is not None, item.library_error
    if item.library.build.mode == "takein":
        worker._process_takein(item, catalog)  # type: ignore[arg-type]
    else:
        worker._process_one(item, catalog,  # type: ignore[arg-type]
                            pk.PackageOptions(surround_codec="off", hls_subtitles=True,
                                              preferred_languages=("en",)))


def _again(body: dict, first: Catalog, vdir: Path) -> None:
    """The run again, as after a lost handover: the same version handed
    over in the same words, nothing written."""
    before = {p: p.stat().st_mtime_ns for p in vdir.rglob("*")}
    again = Catalog()
    _go(body, again)
    assert [s for s, _ in again.written] == ["in_progress", "done"], again.written
    assert again.handovers == first.handovers
    assert {p: p.stat().st_mtime_ns for p in vdir.rglob("*")} == before


def _leaks(t: Tree, *handovers: dict) -> list[tuple[str, bytes]]:
    """What in the title's folder — a file's name or its bytes, but for
    the originals its versions keep — or in a handover says what only the
    arrival's name and the original's container title say, and what in a
    package says what the container's other tags do."""
    out = []
    for p in sorted(t.item_dir.rglob("*")):
        rel = p.relative_to(t.item_dir).as_posix()
        if p.parent.parent.name == "versions" and rec.ORIGINAL_NAME_RE.fullmatch(p.name):
            continue
        data = rel.encode() + (p.read_bytes() if p.is_file() else b"")
        said = LEAKS + (GLOBAL_TAGS if rel.startswith("versions/") else ())
        out += [(rel, s) for s in said if s in data]
    for payload in handovers:
        data = json.dumps(payload, ensure_ascii=False).encode()
        out += [("the handover", s) for s in LEAKS if s in data]
    return out


def _valid(t: Tree) -> None:
    """The library tree validates, the catalog's part of the title's
    folder added, when the schemas repository's validator can run."""
    cmd = _validator(extras=False)
    if cmd is None:
        return
    _catalogs_part(t.item_dir)
    out = subprocess.run([*cmd, "--check-checksums", str(t.lib)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.rstrip().endswith("OK"), out.stdout


def _whole(vdir: Path) -> dict:
    """The package of a version folder, once its chain holds, every file's
    digest included, and it plays."""
    assert rec.chain_problems(str(vdir), "version.json", rec.PACKAGE_DIRS, full=True) == []
    package = json.loads((vdir / "package.json").read_text())
    pk._verify_staged(vdir, package)
    assert NAME not in (vdir / "checksums.sha256").read_text()
    return package


@packages
def test_an_establish_renames_the_original_into_the_version_it_packages(
    tmp_path: Path, clip: Path,
) -> None:
    t = Tree(tmp_path, clip)
    ino = t.original.stat().st_ino
    body = t.record("establish")
    catalog = Catalog()
    _go(body, catalog)
    assert [s for s, _ in catalog.written] == ["in_progress", "done"], catalog.written
    vdir = t.version_dir()
    assert sorted(p.name for p in vdir.iterdir()) == [
        ".complete", "checksums.sha256", "hls", NAME, "package.json", "subs", "trickplay",
        "version.json"]
    assert not t.original.exists() and (vdir / NAME).stat().st_ino == ino
    package = _whole(vdir)
    assert package["role"] == "derived"
    version = json.loads((vdir / "version.json").read_text())
    assert version["originalFiles"] == [NAME]
    assert [(c["startMs"], c["endMs"], c["title"]) for c in version["chapters"]] == [
        (0, 6000, "One"), (6000, 12000, "Two")]
    source = json.loads((t.source_dir / "source.json").read_text())
    assert source["file"]["name"] == NAME
    assert source["file"]["fixity"] == {"qh1": rec.qh1(str(vdir / NAME))}
    [payload] = catalog.handovers
    assert payload["original"] == {"path": str(vdir / NAME), "name": NAME}
    assert "takenIn" not in payload and payload["source"]["codec"] == "hevc"
    assert payload["sidecars"] == [{"subtitleAssetId": ASSET, "rendition": "sub1",
                                    "path": "subs/1.vtt"}]
    assert [x.get("fromSidecar") for x in package["subtitles"]] == [
        None, f"sources/{SOURCE}/subtitle-1.de.srt"]
    assert _leaks(t, payload) == []
    assert not (t.work / "staging" / VERSION).exists()
    _again(body, catalog, vdir)
    _valid(t)


def test_a_takein_renames_the_original_into_a_version_with_no_package(
    tmp_path: Path, clip: Path,
) -> None:
    t = Tree(tmp_path, clip)
    ino = t.original.stat().st_ino
    body = t.record("takein")
    catalog = Catalog()
    _go(body, catalog)
    assert [(s, kw.get("step")) for s, kw in catalog.written] == [
        ("in_progress", "takein"), ("done", "takein")], catalog.written
    vdir = t.version_dir()
    # No package anywhere, and so no chain.
    assert sorted(p.name for p in vdir.iterdir()) == [NAME, "version.json"]
    assert sorted(p.name for p in t.item_dir.iterdir()) == ["sources", "versions"]
    assert not t.original.exists() and (vdir / NAME).stat().st_ino == ino
    assert sorted(p.name for p in t.source_dir.iterdir()) == [
        "checksums.sha256", "ffprobe.json", "source.json", "subtitle-1.de.srt"]
    version = json.loads((vdir / "version.json").read_text())
    source = json.loads((t.source_dir / "source.json").read_text())
    assert (version["originalFiles"], version["sourceIds"], version["runtimeMs"]) == (
        [NAME], [SOURCE], source["container"]["durationMs"])
    assert [c["title"] for c in version["chapters"]] == ["One", "Two"]
    assert source["file"]["name"] == NAME
    [payload] = catalog.handovers
    assert {k: payload[k] for k in ("layout", "versionId", "versionDir", "sourceId",
                                    "sourceRecorded", "takenIn", "sidecars", "original")} == {
        "layout": "v2", "versionId": VERSION, "versionDir": str(vdir), "sourceId": SOURCE,
        "sourceRecorded": True, "takenIn": True, "sidecars": [],
        "original": {"path": str(vdir / NAME), "name": NAME}}
    assert not {"packageId", "complete", "package"} & set(payload)
    assert (payload["source"]["codec"], payload["source"]["width"],
            payload["source"]["height"]) == ("hevc", 640, 360)
    assert _leaks(t, payload) == []
    assert not (t.work / "staging" / VERSION).exists()
    _again(body, catalog, vdir)
    _valid(t)


@packages
def test_an_add_puts_a_package_beside_the_original_in_its_version(
    tmp_path: Path, clip: Path,
) -> None:
    t = Tree(tmp_path, clip)
    vdir = t.version_dir()
    _go(t.record("takein"), Catalog())
    version = (vdir / "version.json").read_bytes()
    body = t.record("add", path=vdir / NAME, copies=True)
    catalog = Catalog()
    _go(body, catalog)
    assert [s for s, _ in catalog.written] == ["in_progress", "done"], catalog.written
    assert sorted(p.name for p in vdir.iterdir()) == [
        ".complete", "checksums.sha256", "hls", NAME, "package.json", "subs", "trickplay",
        "version.json"]
    assert (vdir / "version.json").read_bytes() == version
    package = _whole(vdir)
    assert package["role"] == "derived"
    subs = package["subtitles"]
    assert [(s["id"], s.get("fromSidecar")) for s in subs] == [
        ("sub0", None), ("sub1", f"sources/{SOURCE}/subtitle-1.de.srt")]
    [payload] = catalog.handovers
    assert "original" not in payload and "takenIn" not in payload
    assert payload["package"] == package and payload["versionDir"] == str(vdir)
    assert payload["sidecars"] == [{"subtitleAssetId": ASSET, "rendition": "sub1",
                                    "path": "subs/1.vtt"}]
    assert _leaks(t, payload) == []
    assert not (t.work / "staging" / VERSION).exists()
    _again(body, catalog, vdir)
    _valid(t)


@packages
def test_a_repackage_builds_a_new_version_and_leaves_the_original_where_it_is(
    tmp_path: Path, clip: Path,
) -> None:
    t = Tree(tmp_path, clip)
    first = t.version_dir()
    established = Catalog()
    _go(t.record("establish"), established)
    kept = {p: p.stat().st_mtime_ns for p in first.rglob("*")}
    body = t.record("repackage", version=NEXT_VERSION, path=first / NAME, copies=True)
    catalog = Catalog()
    _go(body, catalog)
    assert [s for s, _ in catalog.written] == ["in_progress", "done"], catalog.written
    second = t.version_dir(NEXT_VERSION)
    assert sorted(p.name for p in second.iterdir()) == [
        ".complete", "checksums.sha256", "hls", "package.json", "subs", "trickplay",
        "version.json"]
    package = _whole(second)
    assert package["role"] == "canonical"
    assert json.loads((second / "version.json").read_text())["originalFiles"] == []
    assert {p: p.stat().st_mtime_ns for p in first.rglob("*")} == kept
    [payload] = catalog.handovers
    assert payload["versionId"] == NEXT_VERSION and "original" not in payload
    assert payload["sidecars"] == [{"subtitleAssetId": ASSET, "rendition": "sub1",
                                    "path": "subs/1.vtt"}]
    assert _leaks(t, *established.handovers, payload) == []
    _again(body, catalog, second)
    _valid(t)
