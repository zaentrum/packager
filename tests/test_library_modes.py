"""The build modes of a library v2 run (the worker record's build.mode),
with the stand-ins for its binaries of test_library_flow.py: an establish
renames the original into the version folder it builds, last, and puts
it back when the run fails after that; a run that dies with the original
in its staging folder leaves it to the next run, or to the startup sweep,
which put it back; a handover that was lost is made again from the version
in the record, the original's new place in it; a repackage builds a new
version and leaves the original where it is. The real runs are in
test_library_modes_real.py."""

from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path

import pytest
from test_library_flow import (
    ASSET,
    NEXT_VERSION,
    SOURCE,
    SOURCE_BLOCK,
    VERSION,
    Binaries,
    Catalog,
    Share,
    _handoff,
    _Killed,
    run,
)

from packager import library, worker
from packager import libv2_records as rec
from packager import packager as pk
from packager.katalog import ClaimedItem
from packager.renditions import PackageInputs, VideoInput

NAME = "original.mkv"


@pytest.fixture
def binaries(monkeypatch: pytest.MonkeyPatch) -> Binaries:
    return Binaries(monkeypatch)


@pytest.fixture
def share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Share:
    return Share(tmp_path, monkeypatch)


def record(share: Share, mode: str | None, *, name: str | None = NAME, **fields) -> dict:
    """The worker record of a run of `mode`, with the name its original
    gets in the version folder."""
    body = share.record(**fields)
    build = body["library"]["build"]
    if mode is not None:
        build["mode"] = mode
    if name is not None:
        build["originalName"] = name
    return body


def go(body: dict, catalog: Catalog) -> None:
    """The job a record's mode is run as: a takein job, or a package job."""
    item = ClaimedItem.from_json(body)
    if body["library"]["build"].get("mode") == "takein":
        worker._process_takein(item, catalog)  # type: ignore[arg-type]
    else:
        run(body, catalog)


def _ino(path: Path) -> int:
    return path.stat().st_ino


def _left(share: Share) -> list[str]:
    """What of a run is left where nothing of one may stay."""
    return sorted(p.relative_to(share.lib).as_posix() for p in share.lib.rglob("*")
                  if p.name in (library.ORIGINAL_NOTE, pk.SENTINEL, pk.STAGING_DIR,
                                pk.MANIFEST_FILE, ".failed"))


# ------------------------------------------------------------- establish

def test_an_establish_renames_the_original_into_its_version_folder(
    binaries: Binaries, share: Share,
) -> None:
    ino, data = _ino(share.original), share.original.read_bytes()
    catalog = Catalog()
    run(record(share, "establish"), catalog)
    assert catalog.statuses == ["in_progress", "done"]

    vdir = share.version_dir
    assert sorted(p.name for p in vdir.iterdir()) == [
        ".complete", "checksums.sha256", "hls", NAME, "package.json", "subs", "trickplay",
        "version.json"]
    # Renamed, not copied: the same file, gone from its arrival.
    assert not share.original.exists()
    assert _ino(vdir / NAME) == ino and (vdir / NAME).read_bytes() == data
    # The chain holds and never lists the original.
    assert library.verify_chain(vdir, "version.json") is None
    assert NAME not in (vdir / "checksums.sha256").read_text()
    version = json.loads((vdir / "version.json").read_text())
    assert version["originalFiles"] == [NAME]
    source = json.loads((share.source_dir / "source.json").read_text())
    assert source["file"]["name"] == NAME
    assert source["file"]["fixity"] == {"qh1": rec.qh1(str(vdir / NAME))}
    package = json.loads((vdir / "package.json").read_text())
    assert package["role"] == "derived"           # the original is kept beside it

    [payload] = catalog.handovers
    assert payload == {
        "layout": "v2", "versionId": VERSION, "packageId": package["packageId"],
        "versionDir": str(vdir), "complete": (vdir / ".complete").read_text().strip(),
        "sourceId": SOURCE, "sourceRecorded": True, "package": package,
        "sidecars": [{"subtitleAssetId": ASSET, "rendition": "sub0", "path": "subs/0.vtt"}],
        "source": SOURCE_BLOCK,
        "original": {"path": str(vdir / NAME), "name": NAME},
    }
    assert not share.staging.exists() and not share.inbox.exists() and _left(share) == []


def test_the_original_is_renamed_in_last_and_the_record_sees_it_with_its_version(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list = []
    real = library.place

    def place(staged: Path, target: Path) -> bool:
        seen.append((staged.name, sorted(p.name for p in staged.iterdir()),
                     share.original.exists()))
        return real(staged, target)

    monkeypatch.setattr(library, "place", place)
    run(record(share, "establish"), Catalog())
    assert seen == [
        ("source", ["Sintel (2010).en.srt", "Sintel (2010).nfo", "checksums.sha256",
                    "ffprobe.json", "source.json"], True),
        ("version", [".complete", "checksums.sha256", "hls", NAME, "package.json", "subs",
                     "trickplay", "version.json"], False),
    ]


@pytest.mark.parametrize("fails", ["the version's rename", "the catalog's folder"])
def test_an_establish_that_fails_after_its_original_is_staged_puts_it_back(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch, fails: str,
) -> None:
    ino = _ino(share.original)
    real = library.place

    def place(staged: Path, target: Path) -> bool:
        if staged.name == "version":
            assert not share.original.exists()          # it is in the staged folder now
            if fails == "the version's rename":
                raise OSError(errno.EIO, "Input/output error")
            target.mkdir(parents=True)                  # a folder there meanwhile, not whole
        return real(staged, target)

    monkeypatch.setattr(library, "place", place)
    catalog = Catalog()
    run(record(share, "establish"), catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert ("Input/output error" if fails == "the version's rename"
            else "is there but not a whole version") in catalog.error
    assert _ino(share.original) == ino                  # back where it came from
    assert not share.staging.exists() and _left(share) == []
    assert catalog.handovers == []

    if fails == "the catalog's folder":
        share.version_dir.rmdir()
    monkeypatch.setattr(library, "place", real)
    catalog = Catalog()
    run(record(share, "establish"), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert _ino(share.version_dir / NAME) == ino


def test_an_establish_whose_original_cant_be_renamed_leaves_it_where_it_is(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = os.rename

    def rename(src, dst):
        if Path(src) == share.original:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    catalog = Catalog()
    run(record(share, "establish"), catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert "the arrivals must be on the library's share" in catalog.error
    assert share.original.exists() and not share.version_dir.exists()
    assert not share.staging.exists() and _left(share) == []


def _dies_with_the_original_staged(
    share: Share, monkeypatch: pytest.MonkeyPatch, body: dict | None = None,
) -> None:
    real = library.place

    def place(staged: Path, target: Path) -> bool:
        if staged.name == "version":
            raise _Killed
        return real(staged, target)

    monkeypatch.setattr(library, "place", place)
    catalog = Catalog()
    with pytest.raises(_Killed):
        go(body or record(share, "establish"), catalog)
    monkeypatch.setattr(library, "place", real)
    assert catalog.statuses == ["in_progress"] and catalog.handovers == []
    assert not share.original.exists() and not share.version_dir.exists()
    assert (share.staging / "version" / NAME).exists()
    assert json.loads((share.staging / library.ORIGINAL_NOTE).read_text()) == {
        "from": str(share.original), "to": str(share.staging / "version" / NAME)}


@pytest.mark.parametrize("mode", ["establish", "takein"])
def test_the_next_run_puts_back_an_original_a_dead_run_left_in_staging(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    ino = _ino(share.original)
    body = record(share, mode)
    _handoff(share.inbox)          # its v0 is the original: the inputs need it where it was
    _dies_with_the_original_staged(share, monkeypatch, body)
    catalog = Catalog(steps={"transcode": "done"})
    go(body, catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert binaries.builds == (2 if mode == "establish" else 0)
    assert _ino(share.version_dir / NAME) == ino
    assert catalog.handovers[0]["original"] == {"path": str(share.version_dir / NAME),
                                                "name": NAME}
    assert not share.staging.exists() and _left(share) == []


@pytest.mark.parametrize("mode", ["establish", "takein"])
def test_the_startup_sweep_puts_back_an_original_a_dead_run_left_in_staging(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    ino = _ino(share.original)
    _dies_with_the_original_staged(share, monkeypatch, record(share, mode))
    t = time.time() - 2 * 86400
    os.utime(share.staging / pk.SENTINEL, (t, t))
    assert library.sweep_staging(share.work) == 1
    assert _ino(share.original) == ino and not share.staging.exists()


@pytest.mark.parametrize("mode", ["establish", "takein"])
def test_a_run_never_starts_over_an_original_it_cant_put_back(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    body = record(share, mode)
    _dies_with_the_original_staged(share, monkeypatch, body)
    share.original.write_bytes(b"another file, by the original's old name")
    before = share.tree(share.staging)
    catalog = Catalog()
    go(body, catalog)
    assert catalog.statuses == ["failed"]
    assert "holds an original that can't be put back where it came from" in catalog.error
    assert share.tree(share.staging) == before
    assert binaries.builds == (1 if mode == "establish" else 0)


@pytest.mark.parametrize("handoff", [False, True])
def test_an_establish_whose_handover_was_lost_is_reported_again(
    binaries: Binaries, share: Share, handoff: bool,
) -> None:
    body = record(share, "establish")     # the catalog's path stays the arrival's
    if handoff:                    # its v0 is the original, which moved
        _handoff(share.inbox)
    steps = {"transcode": "done"} if handoff else {}
    catalog = Catalog(steps=steps)

    def dies(_payload: dict) -> None:
        raise _Killed

    catalog.on_handover = dies
    with pytest.raises(_Killed):
        run(body, catalog)
    first = catalog.handovers[0]
    placed = share.tree(share.item_dir)
    assert first["original"] == {"path": str(share.version_dir / NAME), "name": NAME}
    assert not share.original.exists()

    catalog = Catalog(steps=steps)
    run(body, catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert binaries.builds == 1                      # nothing built again
    assert catalog.handovers == [first]              # the same version, the same words
    assert share.tree(share.item_dir) == placed
    assert not share.staging.exists() and not share.inbox.exists() and _left(share) == []


def test_a_refused_establish_keeps_the_original_in_its_version_and_reports_it_again(
    binaries: Binaries, share: Share,
) -> None:
    body = record(share, "establish")
    catalog = Catalog(status=409)
    run(body, catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert (share.version_dir / NAME).exists() and not share.original.exists()
    catalog = Catalog()
    run(body, catalog)
    assert catalog.statuses == ["in_progress", "done"] and binaries.builds == 1
    assert catalog.handovers[0]["original"]["path"] == str(share.version_dir / NAME)


@pytest.mark.parametrize("change", ["gone", "other bytes"])
def test_a_version_in_place_whose_original_is_not_the_catalogs_is_never_reported(
    binaries: Binaries, share: Share, change: str,
) -> None:
    body = record(share, "establish")
    run(body, Catalog(status=500))
    kept_at = share.version_dir / NAME
    if change == "gone":
        kept_at.unlink()
    else:
        data = bytearray(kept_at.read_bytes())
        data[0] ^= 0xFF
        kept_at.write_bytes(bytes(data))
    catalog = Catalog()
    run(body, catalog)
    assert catalog.handovers == [] and catalog.statuses[-1] == "failed"
    if change == "gone":
        # Neither in its version folder nor at its arrival.
        assert catalog.error == f"source file missing: {share.original}"
        item = ClaimedItem.from_json(body)
        with pytest.raises(pk.PackageError, match=f"versions/{VERSION} keeps its original "
                                                  f"{NAME}, which is not there"):
            library.package_version(item, PackageInputs([VideoInput("v0", kept_at)], "original"),
                                    options=pk.PackageOptions())
    else:
        assert "does not match the qh1 the catalog recorded" in catalog.error


def test_a_version_placed_before_the_modes_is_reported_as_it_is(
    binaries: Binaries, share: Share,
) -> None:
    # Its run kept the original at its arrival and lost its handover; the
    # catalog now names the run an establish. Nothing is renamed into a
    # version folder that is written already.
    catalog = Catalog(status=500)
    run(record(share, None, name=None), catalog)
    placed = share.tree(share.version_dir)
    catalog = Catalog()
    run(record(share, "establish"), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert "original" not in catalog.handovers[0]
    assert share.original.exists() and share.tree(share.version_dir) == placed


def test_an_establish_names_its_original_as_its_recorded_source_does(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A run from before the modes recorded the source under the original's
    # arrival name and died before its version: the version would keep a
    # file its source doesn't name.
    real = library.place

    def place(staged: Path, target: Path) -> bool:
        if staged.name == "version":
            raise _Killed
        return real(staged, target)

    monkeypatch.setattr(library, "place", place)
    with pytest.raises(_Killed):
        run(record(share, None, name=None), Catalog())
    monkeypatch.setattr(library, "place", real)
    catalog = Catalog()
    run(record(share, "establish"), catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert (f"sources/{SOURCE} names its original 'Sintel (2010).mkv', and the version would "
            f"keep it as 'original.mkv'") in catalog.error
    assert share.original.exists() and not share.version_dir.exists()
    assert not share.staging.exists() and _left(share) == []


def test_an_establish_takes_its_original_from_its_arrival_only(
    binaries: Binaries, share: Share,
) -> None:
    body = record(share, "establish")
    inside = share.item_dir / "versions" / NEXT_VERSION / NAME
    inside.parent.mkdir(parents=True)
    share.original.rename(inside)
    body["path"] = str(inside)
    catalog = Catalog()
    run(body, catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert "is in the title's record already" in catalog.error
    assert inside.exists() and binaries.builds == 0


# ------------------------------------------------------------- takein

def test_a_takein_renames_the_original_into_a_version_with_no_package(
    binaries: Binaries, share: Share,
) -> None:
    ino = _ino(share.original)
    catalog = Catalog()
    go(record(share, "takein"), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert {kw.get("step") for _status, kw in catalog.written} == {"takein"}
    assert catalog.written[-1][1]["details"].startswith(f"original={NAME} dur_s=")
    assert binaries.builds == 0

    vdir = share.version_dir
    # No package, and so no chain: version.json and the original only.
    assert sorted(p.name for p in vdir.iterdir()) == [NAME, "version.json"]
    assert _ino(vdir / NAME) == ino and not share.original.exists()
    version = json.loads((vdir / "version.json").read_text())
    assert (version["versionId"], version["sourceIds"], version["originalFiles"]) == (
        VERSION, [SOURCE], [NAME])
    assert [(c["startMs"], c["title"]) for c in version["chapters"]] == [(0, "Opening")]
    source = json.loads((share.source_dir / "source.json").read_text())
    assert source["file"]["name"] == NAME
    assert sorted(p.name for p in share.source_dir.iterdir()) == [
        "Sintel (2010).en.srt", "Sintel (2010).nfo", "checksums.sha256", "ffprobe.json",
        "source.json"]

    [payload] = catalog.handovers
    assert payload == {
        "layout": "v2", "versionId": VERSION, "versionDir": str(vdir),
        "sourceId": SOURCE, "sourceRecorded": True, "takenIn": True, "sidecars": [],
        "source": SOURCE_BLOCK, "original": {"path": str(vdir / NAME), "name": NAME},
    }
    assert not share.staging.exists() and _left(share) == []


def test_a_takein_whose_handover_was_lost_is_reported_again(
    binaries: Binaries, share: Share,
) -> None:
    body = record(share, "takein")
    catalog = Catalog()

    def dies(_payload: dict) -> None:
        raise _Killed

    catalog.on_handover = dies
    with pytest.raises(_Killed):
        go(body, catalog)
    first = catalog.handovers[0]
    placed = share.tree(share.item_dir)
    catalog = Catalog()
    go(body, catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert catalog.handovers == [first]
    assert share.tree(share.item_dir) == placed
    assert not share.staging.exists() and _left(share) == []


@pytest.mark.parametrize("status", [409, 500])
def test_a_refused_takein_keeps_its_version_and_reports_it_again(
    binaries: Binaries, share: Share, status: int,
) -> None:
    body = record(share, "takein")
    catalog = Catalog(status=status)
    go(body, catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert catalog.error.startswith(f"packaging-complete: the catalog answered {status}")
    assert (share.version_dir / NAME).exists()
    catalog = Catalog()
    go(body, catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert catalog.handovers[0]["takenIn"] is True


@pytest.mark.parametrize("left", ["a package", "another file", "no original"])
def test_a_version_folder_that_is_not_a_takeins_is_never_written_over(
    binaries: Binaries, share: Share, left: str,
) -> None:
    body = record(share, "takein")
    go(body, Catalog(status=500))
    vdir = share.version_dir
    if left == "a package":
        (vdir / "hls").mkdir()
    elif left == "another file":
        (vdir / "notes.txt").write_text("x")
    else:
        (vdir / NAME).rename(share.original)      # back at its arrival
    before = share.tree(vdir)
    catalog = Catalog()
    go(body, catalog)
    assert catalog.statuses[-1] == "failed" and catalog.handovers == []
    says = f"versions/{VERSION} is there but not the version a takein leaves (it holds "
    assert says in catalog.error
    if left == "no original":
        assert f"{says}version.json)" in catalog.error
    assert share.tree(vdir) == before


def test_a_takein_record_on_a_package_job_is_only_acked(
    binaries: Binaries, share: Share,
) -> None:
    catalog = Catalog()
    run(record(share, "takein"), catalog)
    assert catalog.written == [] and catalog.handovers == []
    assert share.original.exists() and not share.item_dir.exists()


@pytest.mark.parametrize("mode", ["establish", "add", "repackage", None])
def test_a_takein_job_whose_record_is_another_jobs_is_only_acked(
    binaries: Binaries, share: Share, mode: str | None,
) -> None:
    body = record(share, mode)
    catalog = Catalog()
    worker._process_takein(ClaimedItem.from_json(body), catalog)  # type: ignore[arg-type]
    del body["library"]
    worker._process_takein(ClaimedItem.from_json(body), catalog)  # type: ignore[arg-type]
    assert catalog.written == [] and catalog.handovers == [] and binaries.builds == 0
    assert share.original.exists() and not share.item_dir.exists()


def test_a_takein_job_whose_block_cant_be_worked_from_fails_its_step(
    binaries: Binaries, share: Share,
) -> None:
    body = record(share, "takein", name="Sintel (2010).mkv")
    catalog = Catalog()
    worker._process_takein(ClaimedItem.from_json(body), catalog)  # type: ignore[arg-type]
    assert [(s, kw.get("step")) for s, kw in catalog.written] == [("failed", "takein")]
    assert "library.build.originalName" in catalog.error
    assert share.original.exists()


def test_a_takein_is_never_packaged(binaries: Binaries, share: Share) -> None:
    item = ClaimedItem.from_json(record(share, "takein"))
    with pytest.raises(pk.PackageError, match="a takein is taken in, never packaged"):
        library.package_version(item, PackageInputs([VideoInput("v0", share.original)],
                                                    "original"), options=pk.PackageOptions())
    with pytest.raises(pk.PackageError, match="build mode is 'establish', not takein"):
        library.take_in(ClaimedItem.from_json(record(share, "establish")))


# ------------------------------------------------------------- repackage

def test_a_repackage_is_a_new_version_and_the_original_stays_where_it_is(
    binaries: Binaries, share: Share,
) -> None:
    repackage = record(share, "repackage", name=None, version=NEXT_VERSION, recorded=True)
    run(record(share, "establish"), Catalog())
    first = share.tree(share.version_dir)
    kept_at = share.version_dir / NAME
    ino = _ino(kept_at)
    repackage["path"] = str(kept_at)            # the catalog's path, once it took the version
    catalog = Catalog()
    run(repackage, catalog)
    assert catalog.statuses == ["in_progress", "done"]
    second = share.item_dir / "versions" / NEXT_VERSION
    assert sorted(p.name for p in second.iterdir()) == [
        ".complete", "checksums.sha256", "hls", "package.json", "trickplay", "version.json"]
    assert json.loads((second / "version.json").read_text())["originalFiles"] == []
    assert json.loads((second / "package.json").read_text())["role"] == "canonical"
    assert library.verify_chain(second, "version.json") is None
    # The first version, its original in it, as it was.
    assert share.tree(share.version_dir) == first and _ino(kept_at) == ino
    [payload] = catalog.handovers
    assert payload["versionId"] == NEXT_VERSION and "original" not in payload
    assert not (share.work / "staging" / NEXT_VERSION).exists() and _left(share) == []


def test_a_record_without_a_mode_builds_as_before(binaries: Binaries, share: Share) -> None:
    catalog = Catalog()
    run(record(share, None, name=None), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert share.original.exists() and not (share.version_dir / NAME).exists()
    assert json.loads((share.version_dir / "version.json").read_text())["originalFiles"] == []
    assert json.loads((share.version_dir / "package.json").read_text())["role"] == "canonical"
    assert "original" not in catalog.handovers[0]
