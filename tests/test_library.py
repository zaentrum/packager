"""The library v2 tree's machinery, without binaries: the staging folder
and its sentinel, the startup sweep of the work tree's staging folder,
the chain a version or an extra folder closes (and what breaks it), and
the one-step rename into the record that never goes over anything."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import stat
import time
from pathlib import Path

import pytest

from packager import library, main
from packager import libv2_records as rec
from packager import packager as pk
from packager.config import Config

VERSION = "9a2e4f6a-2222-4b3c-9d4e-5f6071829304"
MEDIA = (
    '#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MAP:URI="init.mp4"\n'
    "#EXTINF:6.000,\nseg-00001.m4s\n#EXT-X-ENDLIST\n"
)


def _two_days_ago(path: Path) -> None:
    t = time.time() - 2 * 86400
    os.utime(path, (t, t))


# ---------------------------------------------------------------- staging

def test_a_run_opens_its_staging_folder_empty_but_for_its_sentinel(tmp_path: Path) -> None:
    staging = tmp_path / ".work" / "staging" / VERSION
    (staging / "version" / "hls").mkdir(parents=True)       # what a dead run left
    (staging / "version" / "hls" / "seg-00001.m4s").write_text("old")
    library.open_staging(staging)
    assert [p.name for p in staging.iterdir()] == [pk.SENTINEL]
    sentinel = json.loads((staging / pk.SENTINEL).read_text())
    assert set(sentinel) == {"startedAt", "pid", "host"}
    assert (sentinel["pid"], sentinel["host"]) == (os.getpid(), socket.gethostname())
    assert sentinel["startedAt"].endswith("Z") and "T" in sentinel["startedAt"]


def test_the_startup_sweep_walks_the_staging_folder_only(tmp_path: Path) -> None:
    work = tmp_path / ".work"
    staging = work / "staging"
    dead = staging / VERSION                                   # a run that died
    (dead / "version" / "hls").mkdir(parents=True)
    (dead / pk.SENTINEL).write_text("{}")
    _two_days_ago(dead / pk.SENTINEL)
    early = staging / "extra-16aa63f3-3333-4c4d-8e5f-60718293a4b5"  # died before its sentinel
    (early / "extra").mkdir(parents=True)
    _two_days_ago(early)
    busy = staging / "0b6c3d2e-1111-4a2b-8c3d-4e5f60718293"   # another replica, at work
    (busy / "version").mkdir(parents=True)
    (busy / pk.SENTINEL).write_text("{}")
    young = staging / "77c1aaaa-0000-4000-8000-000000000000"   # just started, no sentinel yet
    young.mkdir()
    (staging / "stray.txt").write_text("not a run's")
    # Nothing outside it is walked: not the inbox, not the library.
    inbox = work / "inbox" / VERSION
    inbox.mkdir(parents=True)
    _two_days_ago(inbox)
    record = tmp_path / "movies" / "f0" / "f001aeff" / "versions" / VERSION
    record.mkdir(parents=True)
    (record / pk.SENTINEL).write_text("{}")
    _two_days_ago(record / pk.SENTINEL)

    assert library.sweep_staging(work) == 2
    assert sorted(p.name for p in staging.iterdir()) == sorted([busy.name, young.name,
                                                                "stray.txt"])
    assert inbox.exists() and (record / pk.SENTINEL).exists()


def test_the_sweep_of_a_work_tree_that_isnt_there(tmp_path: Path) -> None:
    assert library.sweep_staging(tmp_path / "no-such-work-root") == 0


# ------------------------------------------------- an original in staging

def _staged_original(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A run's staging folder whose original has been renamed into its
    staged version folder, and the arrival it came from."""
    arrival = tmp_path / ".work" / "incoming" / "Clip (2024)" / "Clip (2024).mkv"
    arrival.parent.mkdir(parents=True)
    arrival.write_bytes(b"the original's bytes")
    staging = tmp_path / ".work" / "staging" / VERSION
    library.open_staging(staging)
    (staging / "version").mkdir()
    staged = staging / "version" / "original.mkv"
    library.move_original_in(arrival, staged, staging)
    return arrival, staging, staged


def test_an_original_is_noted_before_it_is_renamed_into_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list = []
    real = os.rename

    def rename(src, dst):
        note = Path(dst).parent.parent / library.ORIGINAL_NOTE
        seen.append(json.loads(note.read_text()) if note.exists() else None)
        real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    arrival, staging, staged = _staged_original(tmp_path)
    assert seen == [{"from": str(arrival), "to": str(staged)}]
    assert not arrival.exists() and staged.read_bytes() == b"the original's bytes"
    assert sorted(p.name for p in staging.iterdir()) == [
        library.ORIGINAL_NOTE, pk.SENTINEL, "version"]


def test_an_original_left_in_staging_goes_back_where_it_came_from(tmp_path: Path) -> None:
    arrival, staging, staged = _staged_original(tmp_path)
    ino = staged.stat().st_ino
    assert library.restore_original(staging) is True
    assert arrival.read_bytes() == b"the original's bytes" and arrival.stat().st_ino == ino
    assert not staged.exists() and not (staging / library.ORIGINAL_NOTE).exists()
    assert library.restore_original(staging) is True                 # nothing left to do


def test_an_original_that_went_into_the_record_stays_there(tmp_path: Path) -> None:
    arrival, staging, staged = _staged_original(tmp_path)
    vdir = tmp_path / "movies" / "f0" / "f001" / "versions" / VERSION
    assert library.place(staging / "version", vdir)
    assert library.restore_original(staging) is True
    assert (vdir / "original.mkv").exists() and not arrival.exists()
    assert not (staging / library.ORIGINAL_NOTE).exists()


@pytest.mark.parametrize("remove", [library.remove_staging, lambda s: library.finish(str(s))])
def test_a_staging_folder_goes_only_once_its_original_is_back(tmp_path: Path, remove) -> None:
    arrival, staging, _staged = _staged_original(tmp_path)
    remove(staging)
    assert not staging.exists() and arrival.read_bytes() == b"the original's bytes"


def test_a_run_opens_its_staging_folder_with_the_original_put_back(tmp_path: Path) -> None:
    arrival, staging, _staged = _staged_original(tmp_path)
    library.open_staging(staging)
    assert [p.name for p in staging.iterdir()] == [pk.SENTINEL]
    assert arrival.read_bytes() == b"the original's bytes"


@pytest.mark.parametrize("why", ["taken", "note", "elsewhere", "no note"])
def test_a_staging_folder_whose_original_cant_be_put_back_stays(tmp_path: Path, why: str) -> None:
    arrival, staging, staged = _staged_original(tmp_path)
    note = staging / library.ORIGINAL_NOTE
    if why == "taken":                       # another file at the arrival path meanwhile
        arrival.write_bytes(b"another file")
    elif why == "note":
        note.write_text("{not json")
    elif why == "elsewhere":                 # a note that names a file outside the staged version
        note.write_bytes(rec.json_bytes({"from": str(arrival), "to": str(tmp_path / "x.mkv")}))
    else:                                    # an original there without its note
        note.unlink()
    before = sorted(p.relative_to(staging).as_posix() for p in staging.rglob("*"))
    assert library.releasable(staging) is False
    library.remove_staging(staging)
    with pytest.raises(pk.PackageError, match="holds an original that can't be put back"):
        library.open_staging(staging)
    _two_days_ago(staging / pk.SENTINEL)
    assert library.sweep_staging(tmp_path / ".work") == 0
    assert sorted(p.relative_to(staging).as_posix() for p in staging.rglob("*")) == before
    assert staged.read_bytes() == b"the original's bytes"
    if why == "taken":
        assert arrival.read_bytes() == b"another file"


def test_the_sweep_puts_a_dead_runs_original_back(tmp_path: Path) -> None:
    arrival, staging, _staged = _staged_original(tmp_path)
    arrival.parent.rmdir()                   # its arrival folder went meanwhile
    _two_days_ago(staging / pk.SENTINEL)
    assert library.sweep_staging(tmp_path / ".work") == 1
    assert not staging.exists() and arrival.read_bytes() == b"the original's bytes"


def test_an_original_is_renamed_never_linked_copied_or_written_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging" / VERSION
    library.open_staging(staging)
    (staging / "version").mkdir()
    target = tmp_path / "Clip.mkv"
    target.write_bytes(b"x")
    link = tmp_path / "link.mkv"
    link.symlink_to(target)
    with pytest.raises(pk.PackageError, match="is a link: a version folder keeps the file itself"):
        library.move_original_in(link, staging / "version" / "original.mkv", staging)
    (staging / "version" / "original.mkv").write_bytes(b"y")
    with pytest.raises(pk.PackageError, match="is there already"):
        library.move_original_in(target, staging / "version" / "original.mkv", staging)
    (staging / "version" / "original.mkv").unlink()

    def rename(_src, _dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(pk.PackageError, match="the arrivals must be on the library's share"):
        library.move_original_in(target, staging / "version" / "original.mkv", staging)
    assert target.read_bytes() == b"x" and not (staging / library.ORIGINAL_NOTE).exists()


def test_the_work_root(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {"KATALOG_API_URL": "http://katalog-app",
                       "OIDC_TOKEN_URL": "https://sso.example/token",
                       "OIDC_CLIENT_ID": "katalog", "OIDC_CLIENT_SECRET": "x"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("WORK_ROOT", raising=False)
    assert Config.from_env().work_root == "/var/lib/katalog/.work"
    monkeypatch.setenv("WORK_ROOT", " ")
    assert Config.from_env().work_root == "/var/lib/katalog/.work"
    monkeypatch.setenv("WORK_ROOT", "/srv/share/.work")
    assert Config.from_env().work_root == "/srv/share/.work"


def test_main_sweeps_the_package_store_and_the_staging_folder(monkeypatch) -> None:
    calls: list[tuple] = []

    def leftovers(grace: float) -> int:
        calls.append(("leftovers", grace))
        raise OSError("the package store's share is gone")

    monkeypatch.setattr(main, "sweep_leftovers", leftovers)
    monkeypatch.setattr(main, "sweep_staging", lambda root: calls.append(("staging", root)))
    main._sweep(600.0, "/var/lib/katalog/.work")
    # One that fails doesn't keep the other from running.
    assert calls == [("leftovers", 600.0), ("staging", Path("/var/lib/katalog/.work"))]


# ---------------------------------------------------------------- the chain

def _version(folder: Path) -> bytes:
    """A small version folder: version.json (returned) and a package."""
    folder.mkdir(parents=True)
    record = rec.json_bytes({"versionId": VERSION})
    (folder / "version.json").write_bytes(record)
    for rel, body in {"hls/master.m3u8": "#EXTM3U\n", "hls/v0/playlist.m3u8": MEDIA,
                      "hls/v0/init.mp4": "init", "hls/v0/seg-00001.m4s": "segment",
                      "subs/0.vtt": "WEBVTT\n", "trickplay/sprite-0000.jpg": "jpg"}.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(body)
    return record


def _close(folder: Path, record: str = "version.json", record_bytes: bytes | None = None,
           dirs: tuple[str, ...] = rec.PACKAGE_DIRS) -> tuple[bytes, list]:
    seen: list = []

    def package(listed: list) -> dict:
        seen.append(listed)
        return {"packageId": "p", "checksums": rec.checksums(listed)[1]}

    body = library.close_chain(folder, record, record_bytes or (folder / record).read_bytes(),
                               dirs, package)
    return body, seen


def test_a_closed_chain(tmp_path: Path) -> None:
    folder = tmp_path / VERSION
    _version(folder)
    body, [listed] = _close(folder)
    sums = (folder / library.SUMS).read_text().splitlines()
    names = [line.split("  ", 1)[1] for line in sums]
    assert names == sorted(["version.json", "hls/master.m3u8", "hls/v0/playlist.m3u8",
                            "hls/v0/init.mp4", "hls/v0/seg-00001.m4s", "subs/0.vtt",
                            "trickplay/sprite-0000.jpg"])
    for line in sums:
        digest, rel = line.split("  ", 1)
        assert digest == hashlib.sha256((folder / rel).read_bytes()).hexdigest()
    # The package record is made of what the checksums list.
    assert sorted(rel for rel, _digest, _size in listed) == names
    assert json.loads(body)["checksums"]["sha256"] == rec.sha_file(str(folder / library.SUMS))
    assert (folder / library.PACKAGE_RECORD).read_bytes() == body
    assert body.endswith(b"}\n") and b'\n  "packageId": "p"' in body
    assert (folder / library.COMPLETE).read_text() == (
        f"sha256:{hashlib.sha256(body).hexdigest()}\n")
    assert library.verify_chain(folder, "version.json") is None


def test_an_extras_chain_is_over_its_extra_json(tmp_path: Path) -> None:
    folder = tmp_path / "extra"
    _version(folder)
    (folder / "version.json").rename(folder / "extra.json")
    _close(folder, "extra.json", dirs=rec.EXTRA_DIRS)
    assert library.verify_chain(folder, "extra.json", rec.EXTRA_DIRS) is None
    assert "extra.json" in (folder / library.SUMS).read_text()


@pytest.mark.parametrize(("change", "says"), [
    (lambda f: (f / ".complete").unlink(), "the package never finished"),
    (lambda f: (f / "package.json").unlink(), "the package never finished"),
    (lambda f: (f / "checksums.sha256").unlink(), "checksums.sha256 is missing"),
    (lambda f: (f / ".complete").write_text("2026-10-06T08:00:00+00:00\n"),
     ".complete does not name this package.json"),
    (lambda f: (f / "package.json").write_text('{"checksums": {}}\n'),
     ".complete does not name this package.json"),
    (lambda f: (f / "checksums.sha256").write_text("x\n"),
     "checksums.sha256 is not the one package.json names"),
    (lambda f: (f / "hls" / "v0" / "seg-00002.m4s").write_text("more"),
     "hls/v0/seg-00002.m4s is not listed in checksums.sha256"),
    (lambda f: (f / "subs" / "0.vtt").unlink(),
     "checksums.sha256 lists subs/0.vtt, which is not here"),
    (lambda f: (f / "hls" / "v0" / "seg-00001.m4s").write_text("a longer segment"),
     "the files checksums.sha256 lists total"),
    (lambda f: (f / "version.json").write_bytes(
        rec.json_bytes({"versionId": VERSION[::-1]})),
     "version.json does not match the digest checksums.sha256 lists for it"),
])
def test_a_broken_chain_says_what_broke_it(tmp_path: Path, change, says: str) -> None:
    folder = tmp_path / VERSION
    _version(folder)
    _close(folder)
    change(folder)
    problem = library.verify_chain(folder, "version.json")
    assert problem is not None and says in problem, problem


def test_a_chain_that_never_closed(tmp_path: Path) -> None:
    folder = tmp_path / VERSION
    _version(folder)
    assert "the package never finished" in library.verify_chain(folder, "version.json")
    assert "the package never finished" in library.verify_chain(tmp_path / "nothing",
                                                                "version.json")


# ---------------------------------------------------------------- placing

def test_a_staged_folder_is_renamed_into_place(tmp_path: Path) -> None:
    staged = tmp_path / ".work" / "staging" / VERSION / "version"
    _version(staged)
    target = tmp_path / "movies" / "f0" / "f001" / "versions" / VERSION
    before = {p.relative_to(staged): p.read_bytes() for p in staged.rglob("*") if p.is_file()}
    ino = staged.stat().st_ino
    assert library.place(staged, target) is True
    assert not staged.exists() and target.stat().st_ino == ino     # one rename, no copy
    assert {p.relative_to(target): p.read_bytes()
            for p in target.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("there", ["empty", "whole", "file"])
def test_a_rename_never_goes_over_anything(tmp_path: Path, there: str) -> None:
    staged = tmp_path / "staged"
    _version(staged)
    target = tmp_path / "versions" / VERSION
    if there == "file":
        target.parent.mkdir(parents=True)
        target.write_text("x")
    elif there == "empty":
        target.mkdir(parents=True)          # rename(2) would replace an empty folder
    else:
        _version(target)
    before = sorted(p.relative_to(target).as_posix() for p in target.rglob("*")) \
        if target.is_dir() else None
    assert library.place(staged, target) is False
    assert staged.exists()
    if before is not None:
        assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*")) == before


def test_a_work_root_on_another_file_system(tmp_path: Path, monkeypatch) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()

    def rename(_src, _dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(pk.PackageError, match="the work root must be on the library's share"):
        library.place(staged, tmp_path / "versions" / VERSION)


# ---------------------------------------------------------------- copies

def test_a_copy_is_new_bytes_with_the_writers_mode(tmp_path: Path) -> None:
    src = tmp_path / "Movie.en.srt"
    src.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nHello.\n")
    os.chmod(src, 0o600)
    old = os.umask(0o002)
    try:
        library.copy_new(src, tmp_path / "copy.srt")
    finally:
        os.umask(old)
    copy = tmp_path / "copy.srt"
    assert copy.read_bytes() == src.read_bytes()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o664
    assert copy.stat().st_ino != src.stat().st_ino and copy.stat().st_nlink == 1
    with pytest.raises(FileExistsError):
        library.copy_new(src, copy)
