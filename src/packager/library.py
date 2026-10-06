"""The library v2 tree (contract platform-library/1): where and how the
packager writes its records when a worker record carries a `library`
block (katalog.ItemLibrary, katalog.ExtraLibrary). Without one it
packages into the package store, as always (packager.package_item).

The packager writes three kinds of folder into a title's record, each
once, each built in the work tree's staging folder and renamed into
place in one step, so a reader sees all of it or nothing:

    <itemDir>/sources/<sourceId>/   source.json  ffprobe.json  <sidecar copies>
                                    checksums.sha256 (written last)
    <itemDir>/versions/<versionId>/ version.json  hls/  subs/  trickplay/
                                    checksums.sha256  package.json  .complete
    <itemDir>/extras/<extraId>/     extra.json  hls/  subs/
                                    checksums.sha256  package.json  .complete

A version and an extra close one chain, bottom up: checksums.sha256 lists
the record (version.json, extra.json) and every package file,
package.json holds the checksums file's hash, and .complete holds
`sha256:<hex of package.json>`. Every path comes from the record; the
packager never works one out. The catalog writes everything else in the
title's folder (item.json, metadata.json, events/).

Staging: `<stagingDir>/` holds the run's sentinel `.packaging`
({startedAt, pid, host}) beside what it builds, `source/` and `version/`
for an item, `extra/` for an extra, so the sentinel never enters the
record. A run starts by removing what an earlier run of the same version
left there. The startup sweep removes entries of runs that started more
than a day ago (sweep_staging), and walks nothing else.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from .packager import (
    SENTINEL,
    STALE_STAGING_SECONDS,
    PackageError,
    _remove,
    _started_before,
    _subdirs,
)

log = structlog.get_logger(__name__)

SUMS = "checksums.sha256"
COMPLETE = ".complete"
PACKAGE_RECORD = "package.json"
# The package's folders in a version or an extra folder: what its
# checksums list beside the record, and what its size counts.
PACKAGE_DIRS = ("hls", "subs", "trickplay")


def utc_now() -> str:
    """Now, as every record writes a moment: RFC 3339, upper-case T and Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def json_bytes(doc: Any) -> bytes:
    """A record as it is written: two-space indented UTF-8, one trailing
    line break."""
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def qh1(path: Path) -> str:
    """sha256(first 64 KiB || last 64 KiB || uint64be(size)): the cheap
    fixity the catalog recorded the original by, two reads however large
    the file is."""
    size = os.path.getsize(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(65536))
        if size > 65536:
            f.seek(max(size - 65536, 0))
            h.update(f.read(65536))
    h.update(size.to_bytes(8, "big"))
    return "sha256:" + h.hexdigest()


def copy_hashed(src: Path, dst: Path) -> tuple[int, str]:
    """Copy src's bytes to dst, a new file (mode by the umask, not src's),
    hashing them on the way: (size, sha256 hex)."""
    h = hashlib.sha256()
    size = 0
    with open(src, "rb") as fin, open(dst, "xb") as fout:
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            h.update(chunk)
            fout.write(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def free_name(name: str, taken: set[str]) -> str:
    """name, or name with -1, -2, … before its extension when another file
    of the folder has it ("source.json" -> "source-1.json"). Adds it to
    taken."""
    out, n = name, 0
    stem, ext = os.path.splitext(name)
    while out in taken:
        n += 1
        out = f"{stem}-{n}{ext}"
    taken.add(out)
    return out


# ---------------------------------------------------------------- staging

def open_staging(staging_dir: Path) -> None:
    """The run's staging folder, empty but for its sentinel: what an
    earlier run of the same version left there goes first (one run per
    version at a time, as an item's events share a Kafka partition)."""
    if os.path.lexists(staging_dir):
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    (staging_dir / SENTINEL).write_bytes(json_bytes({
        "startedAt": utc_now(), "pid": os.getpid(), "host": socket.gethostname(),
    }))


def remove_staging(staging_dir: Path) -> None:
    """Best effort: the startup sweep takes what this leaves."""
    if os.path.lexists(staging_dir) and not _remove(staging_dir):
        log.warning("packager.staging.cleanup_failed", dir=str(staging_dir))


def sweep_staging(work_root: Path, stale_after_seconds: float = STALE_STAGING_SECONDS) -> int:
    """Remove what runs that ended uncleanly left in <work_root>/staging/:
    each entry whose run started more than stale_after_seconds ago, by its
    sentinel (the entry itself when the run died before it wrote one). A
    younger one may be another replica's, at work. Walks that folder only,
    never the library. Returns how many entries went."""
    t0 = time.monotonic()
    cutoff = time.time() - stale_after_seconds
    entries = _subdirs(work_root / "staging")
    removed = 0
    for entry in entries:
        if _started_before(entry, cutoff) and _remove(entry):
            log.info("packager.sweep.dead_staging", dir=str(entry))
            removed += 1
    log.info("packager.sweep.staging_done", entries=len(entries), removed=removed,
             elapsed_s=round(time.monotonic() - t0, 1))
    return removed


# ---------------------------------------------------------------- the chain

def package_files(folder: Path) -> list[str]:
    """Every file of the package in a version or extra folder, relative to
    it, sorted: hls/, subs/ and trickplay/."""
    out: list[str] = []
    for d in PACKAGE_DIRS:
        for root, dirs, files in os.walk(folder / d):
            dirs.sort()
            out += [os.path.relpath(os.path.join(root, f), folder) for f in files]
    return sorted(Path(rel).as_posix() for rel in out)


def close_chain(
    folder: Path, record: str,
    package: Callable[[dict[str, Any], int], dict[str, Any]],
) -> bytes:
    """Write the chain over a folder holding `record` (version.json,
    extra.json) and its package, bottom up: checksums.sha256 over the
    record and every package file; package.json, the document
    package(checksums block, size of the package files) returns; .complete
    with package.json's hash. Returns package.json's bytes."""
    listed = sorted([record, *package_files(folder)])
    lines: list[str] = []
    total = package_bytes = 0
    for rel in listed:
        path = folder / rel
        size = path.stat().st_size
        total += size
        if rel != record:
            package_bytes += size
        lines.append(f"{sha256_hex(path)}  {rel}\n")
    sums = "".join(lines).encode()
    (folder / SUMS).write_bytes(sums)
    checksums = {"file": SUMS, "algorithm": "sha256",
                 "sha256": "sha256:" + hashlib.sha256(sums).hexdigest(),
                 "files": len(listed), "bytes": total}
    body = json_bytes(package(checksums, package_bytes))
    (folder / PACKAGE_RECORD).write_bytes(body)
    (folder / COMPLETE).write_bytes(f"sha256:{hashlib.sha256(body).hexdigest()}\n".encode())
    return body


def verify_chain(folder: Path, record: str) -> str | None:
    """None when the folder's chain holds — .complete names package.json's
    hash, package.json the checksums file's, which lists exactly `record`
    and the package files there, as many and as large as package.json
    says, and `record` has its listed digest — else what is wrong. The
    chain level of the catalog's verification: the package files' own
    digests are not read."""
    try:
        mark = (folder / COMPLETE).read_bytes().decode("utf-8", "replace").strip()
        body = (folder / PACKAGE_RECORD).read_bytes()
        sums = (folder / SUMS).read_bytes()
    except FileNotFoundError as e:
        return f"no {Path(e.filename).name}"
    except OSError as e:
        return f"unreadable: {e}"
    if mark != "sha256:" + hashlib.sha256(body).hexdigest():
        return f"{COMPLETE} does not name this {PACKAGE_RECORD}"
    try:
        checksums = json.loads(body).get("checksums") or {}
    except (ValueError, AttributeError):
        return f"{PACKAGE_RECORD} is not a JSON object"
    if checksums.get("sha256") != "sha256:" + hashlib.sha256(sums).hexdigest():
        return f"{PACKAGE_RECORD} does not name this {SUMS}"
    listed: dict[str, str] = {}
    for line in sums.decode("utf-8", "replace").splitlines():
        digest, sep, rel = line.partition("  ")
        if not sep or len(digest) != 64 or not rel:
            return f"{SUMS} holds a line that is not '<sha256>  <path>'"
        listed[rel] = digest
    present = {record} if (folder / record).is_file() else set()
    present |= set(package_files(folder))
    if set(listed) != present:
        extra, gone = sorted(present - set(listed)), sorted(set(listed) - present)
        return f"{SUMS} " + "; ".join(
            ([f"does not list {', '.join(extra[:3])}"] if extra else [])
            + ([f"lists {', '.join(gone[:3])}, which is not there"] if gone else []))
    if checksums.get("files") != len(listed):
        return f"{SUMS} lists {len(listed)} files, {PACKAGE_RECORD} says {checksums.get('files')}"
    total = sum((folder / rel).stat().st_size for rel in listed)
    if checksums.get("bytes") != total:
        return f"the files {SUMS} lists total {total} bytes, {PACKAGE_RECORD} says " \
               f"{checksums.get('bytes')}"
    if sha256_hex(folder / record) != listed[record]:
        return f"{record} does not match its checksum"
    return None


# ---------------------------------------------------------------- placing

def place(staged: Path, target: Path) -> bool:
    """Rename a staged folder into the record, in one step, never over
    anything: False when the target is there already, whole or not (the
    caller says which it may be). Its parent is made as needed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target):
        return False
    try:
        os.rename(staged, target)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise PackageError(
                f"{staged} is not on the library's file system, so it can't be renamed into "
                f"{target}: the work root must be on the library's share") from e
        raise
    log.info("packager.library.placed", dir=str(target))
    return True
