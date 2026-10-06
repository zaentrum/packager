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

def _version(folder: Path) -> None:
    """A small version folder: version.json and a package."""
    folder.mkdir(parents=True)
    (folder / "version.json").write_bytes(library.json_bytes({"versionId": VERSION}))
    for rel, body in {"hls/master.m3u8": "#EXTM3U\n", "hls/v0/playlist.m3u8": MEDIA,
                      "hls/v0/init.mp4": "init", "hls/v0/seg-00001.m4s": "segment",
                      "subs/0.vtt": "WEBVTT\n", "trickplay/sprite-0000.jpg": "jpg"}.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(body)


def _close(folder: Path, record: str = "version.json") -> tuple[bytes, list]:
    seen: list = []

    def package(checksums: dict, size: int) -> dict:
        seen.append((checksums, size))
        return {"packageId": "p", "sizeBytes": size, "checksums": checksums}

    return library.close_chain(folder, record, package), seen


def test_a_closed_chain(tmp_path: Path) -> None:
    folder = tmp_path / VERSION
    _version(folder)
    body, [(checksums, size)] = _close(folder)
    sums = (folder / library.SUMS).read_text().splitlines()
    listed = [line.split("  ", 1)[1] for line in sums]
    assert listed == sorted(["version.json", "hls/master.m3u8", "hls/v0/playlist.m3u8",
                             "hls/v0/init.mp4", "hls/v0/seg-00001.m4s", "subs/0.vtt",
                             "trickplay/sprite-0000.jpg"])
    for line in sums:
        digest, rel = line.split("  ", 1)
        assert digest == hashlib.sha256((folder / rel).read_bytes()).hexdigest()
    sums_bytes = (folder / library.SUMS).read_bytes()
    assert checksums == {"file": library.SUMS, "algorithm": "sha256",
                         "sha256": "sha256:" + hashlib.sha256(sums_bytes).hexdigest(),
                         "files": 7,
                         "bytes": sum((folder / rel).stat().st_size for rel in listed)}
    # The package's size is its files', not the records'.
    assert size == checksums["bytes"] - (folder / "version.json").stat().st_size
    assert (folder / library.PACKAGE_RECORD).read_bytes() == body
    assert body.endswith(b"}\n") and b'\n  "packageId": "p"' in body
    assert (folder / library.COMPLETE).read_text() == (
        f"sha256:{hashlib.sha256(body).hexdigest()}\n")
    assert library.verify_chain(folder, "version.json") is None


def test_an_extras_chain_is_over_its_extra_json(tmp_path: Path) -> None:
    folder = tmp_path / "extra"
    _version(folder)
    (folder / "version.json").rename(folder / "extra.json")
    _close(folder, "extra.json")
    assert library.verify_chain(folder, "extra.json") is None
    assert "extra.json" in (folder / library.SUMS).read_text()


@pytest.mark.parametrize(("change", "says"), [
    (lambda f: (f / ".complete").unlink(), "no .complete"),
    (lambda f: (f / "package.json").unlink(), "no package.json"),
    (lambda f: (f / "checksums.sha256").unlink(), "no checksums.sha256"),
    (lambda f: (f / ".complete").write_text("2026-10-06T08:00:00+00:00\n"),
     ".complete does not name this package.json"),
    (lambda f: (f / "package.json").write_text('{"checksums": {}}\n'),
     ".complete does not name this package.json"),
    (lambda f: (f / "checksums.sha256").write_text("x\n"),
     "package.json does not name this checksums.sha256"),
    (lambda f: (f / "hls" / "v0" / "seg-00002.m4s").write_text("more"),
     "checksums.sha256 does not list hls/v0/seg-00002.m4s"),
    (lambda f: (f / "subs" / "0.vtt").unlink(),
     "checksums.sha256 lists subs/0.vtt, which is not there"),
    (lambda f: (f / "hls" / "v0" / "seg-00001.m4s").write_text("a longer segment"),
     "the files checksums.sha256 lists total"),
    (lambda f: (f / "version.json").write_bytes(library.json_bytes({"versionId": "other"})),
     "the files checksums.sha256 lists total"),
    (lambda f: (f / "version.json").write_bytes(
        library.json_bytes({"versionId": VERSION[::-1]})), "version.json does not match"),
])
def test_a_broken_chain_says_what_broke_it(tmp_path: Path, change, says: str) -> None:
    folder = tmp_path / VERSION
    _version(folder)
    _close(folder)
    change(folder)
    problem = library.verify_chain(folder, "version.json")
    assert problem is not None and problem.startswith(says), problem


def test_a_chain_that_never_closed(tmp_path: Path) -> None:
    folder = tmp_path / VERSION
    _version(folder)
    assert library.verify_chain(folder, "version.json") == "no .complete"
    assert library.verify_chain(tmp_path / "nothing", "version.json") == "no .complete"


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
        size, digest = library.copy_hashed(src, tmp_path / "copy.srt")
    finally:
        os.umask(old)
    copy = tmp_path / "copy.srt"
    assert copy.read_bytes() == src.read_bytes()
    assert (size, digest) == (src.stat().st_size, hashlib.sha256(src.read_bytes()).hexdigest())
    assert stat.S_IMODE(copy.stat().st_mode) == 0o664
    assert copy.stat().st_ino != src.stat().st_ino and copy.stat().st_nlink == 1
    with pytest.raises(FileExistsError):
        library.copy_hashed(src, copy)


def test_a_name_another_file_has_gets_a_number() -> None:
    taken = {"source.json", "ffprobe.json", "checksums.sha256"}
    assert [library.free_name(n, taken) for n in (
        "Movie.en.srt", "source.json", "Movie.en.srt", "checksums.sha256", "source.json",
        "README")] == [
        "Movie.en.srt", "source-1.json", "Movie.en-1.srt", "checksums-1.sha256",
        "source-2.json", "README"]


def test_qh1_is_the_catalogs(tmp_path: Path) -> None:
    small, large = tmp_path / "small", tmp_path / "large"
    small.write_bytes(b"x" * 100)
    large.write_bytes(bytes(range(256)) * 1024)          # 256 KiB
    for f in (small, large):
        data = f.read_bytes()
        size = len(data)
        h = hashlib.sha256(data[:65536])
        if size > 65536:
            h.update(data[max(size - 65536, 0):][:65536])
        h.update(size.to_bytes(8, "big"))
        assert library.qh1(f) == "sha256:" + h.hexdigest()
