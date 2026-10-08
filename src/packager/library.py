"""The library v2 tree (contract platform-library/1): where and how the
packager writes its records when a worker record carries a `library`
block (katalog.ItemLibrary, katalog.ExtraLibrary). Without one it
packages into the package store, as always (packager.package_item).

The packager writes three kinds of folder into a title's record, each
once, each built in the work tree's staging folder and renamed into
place in one step, so a reader sees all of it or nothing:

    <itemDir>/sources/<sourceId>/   source.json  ffprobe.json  <sidecar copies>
                                    checksums.sha256 (written last)
    <itemDir>/versions/<versionId>/ version.json  original.<ext>
                                    hls/  subs/  trickplay/
                                    checksums.sha256  package.json  .complete
    <itemDir>/extras/<extraId>/     extra.json  hls/  subs/
                                    checksums.sha256  package.json  .complete

A version folder is the one place its media lives: its original, the
package made from it, or both. What a run writes there is its worker
record's build mode (katalog.BUILD_MODES): `establish` a version with its
package and its original, which is renamed into the staged folder last,
under the name the record gives it, before that folder is renamed into
place; `takein` a version with its original and no package, and so no
chain (take_in); `repackage` — and a record without a mode — a version
with a package and no original, which stays where it is.

A package closes one chain, bottom up: checksums.sha256 lists the record
(version.json, extra.json) and every package file, package.json holds the
checksums file's hash, and .complete holds `sha256:<hex of package.json>`.
The chain never lists an original: its fixity is its source record's.
What the records say, and the chain's bytes, are the vendored record
logic's (records.py, libv2_records.py); this module writes them in that
order, and renames. Every path and name comes from the worker record: the
packager never works one out. The catalog writes everything else in the
title's folder (item.json, metadata.json, events/).

Staging: `<stagingDir>/` holds the run's sentinel `.packaging`
({startedAt, pid, host}) beside what it builds, `source/` and `version/`
for an item, `extra/` for an extra, so the sentinel never enters the
record. A run starts by removing what an earlier run of the same version
left there. The startup sweep removes entries of runs that started more
than a day ago (sweep_staging), and walks nothing else. Before a run
renames the original into `version/`, it notes where the original came
from (`.original`, beside the sentinel); a run that fails puts it back
there, and so does the next run of its version, or the sweep, for one
that died. A staging folder that holds an original is never removed.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from . import libv2_records as rec
from . import records
from .katalog import ESTABLISH, TAKEIN, ClaimedExtra, ClaimedItem, ExtraLibrary, ItemLibrary
from .packager import (
    _SUBTITLE_FILE_MAX_BYTES,
    SENTINEL,
    STALE_STAGING_SECONDS,
    PackageError,
    PackageOptions,
    _remove,
    _started_before,
    _subdirs,
    _subtitle_files,
    _verify_staged,
    build_package,
)
from .renditions import CONTRACT_FILE, PREPARED_FILE, PackageInputs, resolve_inputs

log = structlog.get_logger(__name__)

# The owner's decision for every package of the v2 tree: its video is
# HEVC, the original's copied or one HEVC encode of it. A v0 in another
# codec fails the run; a lower rung in another is left out.
HEVC_ONLY = ("hevc",)

SUMS = rec.SUMS
COMPLETE = ".complete"
PACKAGE_RECORD = "package.json"
VERSION_RECORD = "version.json"
EXTRA_RECORD = "extra.json"
# A package's chain, in the order it is written: .complete last.
CHAIN = (SUMS, PACKAGE_RECORD, COMPLETE)
# The note a run leaves in its staging folder before it renames the
# original into the staged version folder: {from, to}.
ORIGINAL_NOTE = ".original"
# The files a staged version folder holds beside its package's folders,
# but for an original.
_RECORDS = frozenset({VERSION_RECORD, *CHAIN})


def utc_now() -> str:
    """Now, as every record writes a moment: RFC 3339, upper-case T and Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def copy_new(src: Path, dst: Path) -> None:
    """Copy src's bytes to dst, a file of its own: never a link, never
    over another file, its mode the writer's (the umask), not src's."""
    with open(src, "rb") as fin, open(dst, "xb") as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)


# ---------------------------------------------------------------- staging

def open_staging(staging_dir: Path) -> None:
    """The run's staging folder, empty but for its sentinel: what an
    earlier run of the same version left there goes first (one run per
    version at a time, as an item's events share a Kafka partition), an
    original it left in it back where it came from before anything else
    (releasable). Raises PackageError, removing nothing, when it holds one
    that can't be put back."""
    if os.path.lexists(staging_dir):
        if not releasable(staging_dir):
            raise PackageError(kept(staging_dir))
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    (staging_dir / SENTINEL).write_bytes(rec.json_bytes({
        "startedAt": utc_now(), "pid": os.getpid(), "host": socket.gethostname(),
    }))


def remove_staging(staging_dir: Path) -> None:
    """Best effort: the startup sweep takes what this leaves. An original
    a run renamed into it goes back first; a folder that still holds one
    stays (releasable)."""
    if os.path.lexists(staging_dir) and releasable(staging_dir) and not _remove(staging_dir):
        log.warning("packager.staging.cleanup_failed", dir=str(staging_dir))


def sweep_staging(work_root: Path, stale_after_seconds: float = STALE_STAGING_SECONDS) -> int:
    """Remove what runs that ended uncleanly left in <work_root>/staging/:
    each entry whose run started more than stale_after_seconds ago, by its
    sentinel (the entry itself when the run died before it wrote one), once
    the original such a run renamed into it is back where it came from;
    one that still holds an original stays (releasable). A younger one may
    be another replica's, at work. Walks that folder only, never the
    library. Returns how many entries went."""
    t0 = time.monotonic()
    cutoff = time.time() - stale_after_seconds
    entries = _subdirs(work_root / "staging")
    removed = 0
    for entry in entries:
        if _started_before(entry, cutoff) and releasable(entry) and _remove(entry):
            log.info("packager.sweep.dead_staging", dir=str(entry))
            removed += 1
    log.info("packager.sweep.staging_done", entries=len(entries), removed=removed,
             elapsed_s=round(time.monotonic() - t0, 1))
    return removed


def releasable(staging_dir: Path) -> bool:
    """Whether a staging folder may be removed: the original a run renamed
    into it is put back where it came from first (restore_original); one
    still there — it can't be put back, or it is there without the run's
    note, a file of version/ that is no record — keeps the folder as it
    is, for an operator. Logged."""
    if not restore_original(staging_dir):
        return False
    try:
        with os.scandir(staging_dir / "version") as it:
            strays = sorted(e.name for e in it
                            if e.name not in _RECORDS and not e.is_dir(follow_symlinks=False))
    except (FileNotFoundError, NotADirectoryError):
        strays = []
    except OSError as e:
        strays = [f"(version/ can't be listed: {str(e)[:200]})"]
    if strays:
        log.error("packager.staging.holds_files", dir=str(staging_dir), files=strays)
        return False
    return True


def kept(staging_dir: Path) -> str:
    """Why a run can't start, or go on, from a staging folder that holds an
    original."""
    return (f"{staging_dir} holds an original that can't be put back where it came from: "
            f"the folder stays as it is, for an operator (the packager's log says more)")


def move_original_in(original: Path, staged: Path, staging_dir: Path) -> None:
    """Rename the original into the staged version folder, as `staged`:
    the last step before that folder is renamed into the record. The note
    of where it came from (ORIGINAL_NOTE) is written first, on disk before
    the rename, so a run that fails or dies from here on gives it back
    (restore_original). The file itself, never a link to it, never a
    copy, never over another file."""
    if original.is_symlink():
        raise PackageError(f"the original {original} is a link: a version folder keeps the "
                           f"file itself")
    if os.path.lexists(staged):
        raise PackageError(f"{staged} is there already")
    note = staging_dir / ORIGINAL_NOTE
    draft = staging_dir / f"{ORIGINAL_NOTE}.tmp"
    with open(draft, "wb") as f:
        f.write(rec.json_bytes({"from": str(original), "to": str(staged)}))
        f.flush()
        os.fsync(f.fileno())
    os.replace(draft, note)
    try:
        os.rename(original, staged)
    except OSError as e:
        note.unlink(missing_ok=True)          # nothing moved
        if e.errno == errno.EXDEV:
            raise PackageError(
                f"the original {original} is not on the library's file system, so it can't be "
                f"renamed into its version folder: the arrivals must be on the library's "
                f"share") from e
        raise
    log.info("packager.library.original_staged", original=str(original), staged=str(staged))


def restore_original(staging_dir: Path) -> bool:
    """Put back where it came from an original a run renamed into its
    staged version folder and never into the record (the run failed, or
    died), by the note it left (ORIGINAL_NOTE): never over another file.
    True when no original of a run is left in the staging folder — none
    was moved in, it is back, or it went into the record with its version
    — and the note is gone; False, logged, when one is there that can't
    be put back."""
    note = staging_dir / ORIGINAL_NOTE
    if not os.path.lexists(note):
        return True
    try:
        doc = json.loads(note.read_bytes())
        came_from, staged = Path(doc["from"]), Path(doc["to"])
    except (OSError, ValueError, TypeError, KeyError) as e:
        log.error("packager.library.original_note_unreadable", note=str(note), error=str(e)[:200])
        return False
    if not came_from.is_absolute() or staged.parent != staging_dir / "version":
        log.error("packager.library.original_note_unreadable", note=str(note),
                  error=f"from {came_from}, to {staged}")
        return False
    if os.path.lexists(staged):
        why = None
        if os.path.lexists(came_from):
            why = "a file is there"
        else:
            try:
                came_from.parent.mkdir(parents=True, exist_ok=True)
                os.rename(staged, came_from)
            except OSError as e:
                why = str(e)[:200]
        if why is not None:
            log.error("packager.library.original_not_restored", original=str(staged),
                      to=str(came_from), error=why)
            return False
        log.warning("packager.library.original_restored", original=str(came_from))
    try:
        note.unlink(missing_ok=True)
    except OSError as e:
        log.warning("packager.library.original_note_left", note=str(note), error=str(e)[:200])
    return True


# ---------------------------------------------------------------- the chain

def close_chain(
    folder: Path, record: str, record_bytes: bytes, dirs: tuple[str, ...],
    package: Callable[[list[tuple[str, str, int]]], dict[str, Any]],
) -> bytes:
    """Close the chain over a folder holding `record` (version.json,
    extra.json, written already as record_bytes) and its package under
    `dirs`, in the record logic's order: checksums.sha256 over the record
    and every package file; package.json, the document package(listed)
    makes of what the checksums list; .complete with package.json's hash,
    last. Returns package.json's bytes."""
    listed = [rec.record_entry(record, record_bytes), *rec.package_files(str(folder), dirs)]
    sums, _ = rec.checksums(listed)
    (folder / SUMS).write_bytes(sums)
    body = rec.json_bytes(package(listed))
    (folder / PACKAGE_RECORD).write_bytes(body)
    (folder / COMPLETE).write_bytes(rec.complete(body))
    return body


def verify_chain(folder: Path, record: str, dirs: tuple[str, ...] = rec.PACKAGE_DIRS) -> str | None:
    """None when the folder's chain holds (libv2_records.chain_problems: the
    catalog's chain level, the package files' own digests unread), else
    what is wrong with it."""
    problems = rec.chain_problems(str(folder), record, dirs)
    return "; ".join(problems) if problems else None


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


# ---------------------------------------------------------------- versions

@dataclass(frozen=True)
class Placed:
    """A version or an extra whole in the record: its folder, its
    package.json as written, and the handover's sidecars (a version's):
    [{subtitleAssetId, rendition, path}], each subtitle file the catalog
    named mapped to the rendition made from it. reported_again: the folder
    was in place already, from a run whose handover was lost. original:
    where the original is in the version folder, when the run renamed it
    there. A version taken in has no package: no body, no package."""
    folder: Path
    body: bytes | None
    package: dict[str, Any] | None
    sidecars: list[dict[str, Any]]
    reported_again: bool = False
    original: Path | None = None

    @property
    def complete(self) -> str | None:
        """What .complete holds: sha256:<hex of package.json>; None for a
        version taken in."""
        return rec.sha_bytes(self.body) if self.body is not None else None


@dataclass(frozen=True)
class SidecarCopy:
    """A file from beside the original, as its source folder keeps it."""
    name: str               # its name in sources/<sid>/
    original_name: str      # its name beside the original
    path: Path              # where it was copied from (a recorded one: where it is)
    kind: str               # subtitle | nfo | image | other


def package_version(
    item: ClaimedItem, inputs: PackageInputs, *, options: PackageOptions,
    language_whitelist: list[str] | None = None, keep_original_if_single: bool = True,
) -> Placed:
    """Package an item whose worker record carries a library block into
    its version folder (contract section 1.4, steps 1-9; the handover,
    step 10, is the worker's):

      1. the staging folder, empty but for its sentinel;
      2. the package, in <stagingDir>/version/;
      3. the source record in <stagingDir>/source/, unless the source is
         recorded already: ffprobe.json, the copies of the files beside
         the original, source.json, checksums.sha256;
      4. version.json; 5-7. the chain (checksums.sha256, package.json,
         .complete), then the package and the chain checked;
      8. the source renamed into <itemDir>/sources/<sourceId>/ (kept as it
         is when it is there with its checksums; an unfinished one there
         fails the run);
      9. for an `establish`, the original renamed into <stagingDir>/version/
         under the name the worker record gives it (move_original_in);
     10. the version renamed into <itemDir>/versions/<versionId>/.

    A version folder already there and whole is the work of a run whose
    handover was lost: it is reported again as it is, with the original
    an establish renamed into it; one that isn't whole fails the run.
    Neither is ever written over. A run that fails removes its staging
    folder, the original back where it came from first; one that dies
    leaves both to the next run of its version, or to the startup sweep,
    which put the original back the same way."""
    lib = _library(item)
    if lib.build.mode == TAKEIN:
        raise PackageError("the worker record's build mode is takein: a takein is taken in, "
                           "never packaged")
    vdir = Path(lib.build.version_dir)
    sdir = Path(lib.source.record_dir)
    staging = Path(lib.build.staging_dir)
    if not restore_original(staging):
        raise PackageError(kept(staging))
    if os.path.lexists(vdir):
        return _version_there(item)
    if os.path.lexists(sdir) and not (sdir / SUMS).is_file():
        raise PackageError(_unfinished(lib))
    original = Path(item.path)
    if lib.build.mode == ESTABLISH:
        _outside_the_record(original, lib)
    fixity = _check_original(original, lib.source.size_bytes, lib.source.qh1, "the original")
    open_staging(staging)
    try:
        return _stage_version(item, lib, inputs, staging, fixity, options=options,
                              language_whitelist=language_whitelist,
                              keep_original_if_single=keep_original_if_single)
    except Exception:
        remove_staging(staging)
        raise


def _library(item: ClaimedItem) -> ItemLibrary:
    if item.library is None:
        raise PackageError("the worker record has no library block")
    return item.library


def _unfinished(lib: ItemLibrary) -> str:
    return (f"sources/{lib.source.source_id} exists unfinished (no {SUMS}): a source folder "
            f"is written once, and one another writer left half-written is never completed")


def _outside_the_record(original: Path, lib: ItemLibrary) -> None:
    """An original a run renames into its version folder comes from its
    arrival: one in the title's record already is another version's."""
    if Path(os.path.normpath(original)).is_relative_to(lib.item_dir):
        raise PackageError(f"the original {original} is in the title's record already: a run "
                           f"takes an original into its version folder from its arrival")


def _names_its_original(source: dict[str, Any], lib: ItemLibrary) -> None:
    """A source recorded already names its original as the version will
    keep it: a version's originals are its sources' files."""
    name = (source.get("file") or {}).get("name")
    if name != lib.build.original_name:
        raise PackageError(
            f"sources/{lib.source.source_id} names its original {name!r}, and the version "
            f"would keep it as {lib.build.original_name!r}: a version names its originals as "
            f"its sources do")


def _check_original(path: Path, size: int | None, recorded: str | None, what: str) -> str:
    """The original's qh1, once it is the file the catalog recorded at its
    arrival: its size and qh1, where the record names them. A record
    describes the bytes it was made from."""
    actual = path.stat().st_size
    if size is not None and actual != size:
        raise PackageError(f"{what} {path.name} is {actual} bytes, the catalog recorded {size}: "
                           f"it changed since it arrived")
    fixity = rec.qh1(str(path))
    if recorded is not None and fixity != recorded:
        raise PackageError(f"{what} {path.name} does not match the qh1 the catalog recorded: "
                           f"it changed since it arrived")
    return fixity


def _stage_version(
    item: ClaimedItem, lib: ItemLibrary, inputs: PackageInputs, staging: Path, fixity: str, *,
    options: PackageOptions, language_whitelist: list[str] | None,
    keep_original_if_single: bool,
) -> Placed:
    sid, vid = lib.source.source_id, lib.build.version_id
    vstage = staging / "version"
    vstage.mkdir()
    built = build_package(
        item.id, item.path, item.type, vstage, inputs=inputs, options=options,
        language_whitelist=language_whitelist, keep_original_if_single=keep_original_if_single,
        title=item.title, year=item.year, series_title=item.series_title,
        season_number=item.season_number, episode_number=item.episode_number,
        tmdb_id=item.tmdb_id, track_languages=item.track_languages,
        subtitle_files=item.subtitle_files, codecs=HEVC_ONLY,
    )
    probe = records.probe_original(Path(item.path))
    now = utc_now()
    source, copies, staged_source = _source_of(item, lib, staging, probe, fixity, now)
    sidecars = {sub_id: f"sources/{sid}/{c.name}"
                for sub_id, c in _copies_of(built.from_files, copies).items()}

    version = rec.json_bytes(records.version_record(lib, source, probe, now))
    (vstage / VERSION_RECORD).write_bytes(version)
    peak = rec.peak_bandwidth(str(vstage), built.manifest["hls"]["master"])
    package_id = str(uuid.uuid4())
    body = close_chain(vstage, VERSION_RECORD, version, rec.PACKAGE_DIRS, lambda listed:
                       records.package_record(
                           package_id, built, listed, source, probe, sidecars, peak, now,
                           beside_original=lib.build.original_name is not None))
    package = json.loads(body)
    _verify_staged(vstage, package)
    problem = verify_chain(vstage, VERSION_RECORD)
    if problem is not None:
        raise PackageError(f"the staged version is not whole: {problem}")

    if not _into_the_record(item, lib, staging, staged_source):
        return _version_there(item)
    log.info("packager.library.version", item_id=item.id, version_id=vid, package_id=package_id,
             source_recorded_now=staged_source, files=package["checksums"]["files"],
             bytes=package["checksums"]["bytes"], original=lib.build.original_name)
    vdir = Path(lib.build.version_dir)
    return Placed(vdir, body, package, _handover(item, package, copies, sid),
                  original=vdir / lib.build.original_name if lib.build.original_name else None)


def _source_of(
    item: ClaimedItem, lib: ItemLibrary, staging: Path, probe: records.OriginalProbe,
    fixity: str, now: str,
) -> tuple[dict[str, Any], list[SidecarCopy], bool]:
    """The version's source record and its copies: staged in
    <stagingDir>/source/ unless the source is recorded already (True when
    it is staged now), else read — and, for a run that renames the
    original into the version folder, one that names its original as the
    version keeps it."""
    sdir = Path(lib.source.record_dir)
    if not (sdir / SUMS).is_file():
        source, copies = _stage_source(staging / "source", item, lib, probe, fixity, now)
        return source, copies, True
    source, copies = _recorded_source(sdir, lib.source.source_id)
    if lib.build.original_name is not None:
        _names_its_original(source, lib)
    return source, copies, False


def _into_the_record(
    item: ClaimedItem, lib: ItemLibrary, staging: Path, staged_source: bool,
) -> bool:
    """A staged version into the record: the source renamed into
    sources/<sourceId>/ when it is staged (one there with its checksums
    is kept as it is; an unfinished one fails the run), then, for a run
    that takes the original into the version folder, the original renamed
    into the staged one, last, and that renamed into versions/<versionId>/.
    False when a version folder is there already: the original is back
    where it came from, and the caller says what is there."""
    sdir = Path(lib.source.record_dir)
    if staged_source and not place(staging / "source", sdir):
        if not (sdir / SUMS).is_file():
            raise PackageError(_unfinished(lib))
        log.info("packager.library.source_there", item_id=item.id,
                 source_id=lib.source.source_id)
    vstage = staging / "version"
    if lib.build.original_name is not None:
        move_original_in(Path(item.path), vstage / lib.build.original_name, staging)
    if place(vstage, Path(lib.build.version_dir)):
        return True
    if not restore_original(staging):
        raise PackageError(kept(staging))
    return False


def _stage_source(
    folder: Path, item: ClaimedItem, lib: ItemLibrary, probe: records.OriginalProbe,
    fixity: str, now: str,
) -> tuple[dict[str, Any], list[SidecarCopy]]:
    """sources/<sid>/ in staging (contract sections 3.2, 3.3), in the
    record logic's order: a copy of every subtitle file the catalog named
    and of the original's <stem>.nfo/.jpg/.png/.txt, each under the name
    libv2_records.sidecar_names gives it; source.json and its ffprobe.json;
    the checksums over all of them, last. Returns the record and the
    copies."""
    folder.mkdir()
    sid = lib.source.source_id
    original = Path(item.path)
    files: list[tuple[Path, str, str | None, bool]] = []      # (path, kind, language, forced)
    for f in _subtitle_files(item.subtitle_files, original):
        try:
            if f.path.stat().st_size > _SUBTITLE_FILE_MAX_BYTES:
                log.warning("packager.library.sidecar_too_large", path=str(f.path))
                continue
        except OSError as e:
            log.warning("packager.library.sidecar_missing", path=str(f.path), error=str(e)[:200])
            continue
        # The record keeps the catalog's language as BCP 47 ("ger" -> "de"):
        # the worker record hands the packager its ISO 639-2 code.
        language = rec.lang(f.language)[0] if f.language != "und" else None
        files.append((f.path, "subtitle", language, f.forced))
    files += [(Path(p), kind, None, False) for p, kind in rec.companion_files(str(original))]
    copies, entries = [], []
    for (path, kind, language, forced), name in zip(
            files, rec.sidecar_names([p.name for p, *_ in files]), strict=True):
        copy_new(path, folder / name)
        copies.append(SidecarCopy(name=name, original_name=path.name, path=path, kind=kind))
        entries.append(rec.sidecar_entry(sid, name, str(folder / name), path.name, kind,
                                         language=language, forced=forced))
    doc, probe_bytes = records.source_record(lib, original, fixity, probe, entries, now)
    record = rec.json_bytes(doc)
    (folder / records.PROBE_FILE).write_bytes(probe_bytes)
    (folder / records.SOURCE_RECORD).write_bytes(record)
    sums, _ = rec.checksums([
        rec.record_entry(records.SOURCE_RECORD, record),
        rec.record_entry(records.PROBE_FILE, probe_bytes),
        *((c.name, e["sha256"].removeprefix("sha256:"), e["sizeBytes"])
          for c, e in zip(copies, entries, strict=True)),
    ])
    (folder / SUMS).write_bytes(sums)
    return doc, copies


def _recorded_source(sdir: Path, source_id: str) -> tuple[dict[str, Any], list[SidecarCopy]]:
    """A source record in place already (an earlier version of the same
    original wrote it): its source.json, and the copies it holds."""
    try:
        doc = json.loads((sdir / records.SOURCE_RECORD).read_text())
    except (OSError, ValueError) as e:
        raise PackageError(f"sources/{source_id} is recorded, but its source.json can't be "
                           f"read: {e}") from e
    if not isinstance(doc, dict) or doc.get("sourceId") != source_id:
        raise PackageError(f"sources/{source_id} holds the record of another source")
    prefix = f"sources/{source_id}/"
    copies = []
    for e in doc.get("sidecars") or []:
        file = e.get("file") if isinstance(e, dict) else None
        if isinstance(file, str) and file.startswith(prefix):
            name = file[len(prefix):]
            copies.append(SidecarCopy(name=name, original_name=str(e.get("originalName") or name),
                                      path=sdir / name, kind=str(e.get("kind") or "other")))
    return doc, copies


def _copies_of(files: dict[str, Any], copies: list[SidecarCopy]) -> dict[str, SidecarCopy]:
    """The copy of the subtitle file each subtitle made from one (by its id;
    `files` is Built.from_files) is: by its path for the copies this run
    made, else by its name, in order, among those a recorded source
    holds."""
    subtitles = [c for c in copies if c.kind == "subtitle"]
    by_path = {c.path: c for c in subtitles}
    by_name: dict[str, list[SidecarCopy]] = {}
    for c in subtitles:
        by_name.setdefault(c.original_name, []).append(c)
    out: dict[str, SidecarCopy] = {}
    for sub_id, f in sorted(files.items(), key=lambda kv: int(kv[0].removeprefix("sub"))):
        c = by_path.get(f.path)
        if c is None and by_name.get(f.path.name):
            c = by_name[f.path.name].pop(0)
        if c is not None:
            out[sub_id] = c
    return out


def _handover(
    item: ClaimedItem, package: dict[str, Any], copies: list[SidecarCopy], source_id: str,
) -> list[dict[str, Any]]:
    """The handover's sidecars: each subtitle file the catalog named (by
    its id) mapped to the rendition made from it, by the package's
    fromSidecar and the copy it names (contract section 2.5)."""
    copy_of = {f"sources/{source_id}/{c.name}": c for c in copies}
    by_name: dict[str, list[Any]] = {}
    for f in _subtitle_files(item.subtitle_files, Path(item.path)):
        by_name.setdefault(f.path.name, []).append(f)
    out = []
    for s in package.get("subtitles") or []:
        c = copy_of.get(s.get("fromSidecar"))
        if c is None or not by_name.get(c.original_name):
            continue
        f = by_name[c.original_name].pop(0)
        if f.asset_id:
            out.append({"subtitleAssetId": f.asset_id, "rendition": s["id"], "path": s["path"]})
    return out


def _version_there(item: ClaimedItem) -> Placed:
    """The version folder in place already: reported again as it is when
    it is whole and this version's, with the original a run renamed into
    it (_original_in), else the run fails. Never written over."""
    lib = _library(item)
    vid, sid = lib.build.version_id, lib.source.source_id
    vdir = Path(lib.build.version_dir)
    problem = verify_chain(vdir, VERSION_RECORD)
    if problem is not None:
        raise PackageError(f"versions/{vid} is there but not a whole version ({problem}): "
                           f"a version folder is never written over")
    try:
        version = json.loads((vdir / VERSION_RECORD).read_text())
        body = (vdir / PACKAGE_RECORD).read_bytes()
        package = json.loads(body)
    except (OSError, ValueError) as e:
        raise PackageError(f"versions/{vid} can't be read: {e}") from e
    if version.get("versionId") != vid or sid not in (version.get("sourceIds") or []):
        raise PackageError(f"versions/{vid} holds the record of another version or source")
    original = _original_in(vdir, version, lib)
    sdir = Path(lib.source.record_dir)
    if not (sdir / SUMS).is_file():
        raise PackageError(f"versions/{vid} is there, but its source sources/{sid} is not")
    _source, copies = _recorded_source(sdir, sid)
    log.info("packager.library.reported_again", item_id=item.id, version_id=vid,
             package_id=package.get("packageId"))
    return Placed(vdir, body, package, _handover(item, package, copies, sid), reported_again=True,
                  original=original)


def _original_in(vdir: Path, version: dict[str, Any], lib: ItemLibrary) -> Path | None:
    """The original a run renamed into a version folder in place: None
    for a mode that renames none, and for a version whose record keeps no
    original by the name the worker record gives it (one placed before the
    modes, whose original stayed at its arrival); else there, and the file
    the catalog recorded, or the run fails."""
    name = lib.build.original_name
    keeps = version.get("originalFiles")
    if name is None or not isinstance(keeps, list) or name not in keeps:
        return None
    path = vdir / name
    if path.is_symlink() or not path.is_file():
        raise PackageError(f"versions/{lib.build.version_id} keeps its original {name}, "
                           f"which is not there")
    _check_original(path, lib.source.size_bytes, lib.source.qh1, "the original")
    return path


def original_at(item: ClaimedItem) -> str:
    """Where a run finds the item's original: where the worker record says
    (its path) — or, for a run whose mode renames it into the version
    folder, there, once that folder is in the record (a run whose handover
    was lost)."""
    lib = _library(item)
    if lib.build.original_name is not None:
        moved = Path(lib.build.version_dir) / lib.build.original_name
        if moved.is_file():
            return str(moved)
    return item.path


def version_payload(lib: ItemLibrary, placed: Placed, source: dict[str, Any]) -> dict[str, Any]:
    """POST /api/items/{id}/packaging-complete, v2 (contract section 2.5),
    with the original's place in the version folder ({path, name}) when the
    run renamed it there: the catalog moves the item's path to it as it
    records the version. A version taken in (takenIn) names no package:
    the catalog records it without one, and the item plays from its
    original."""
    if placed.package is None:
        payload: dict[str, Any] = {
            "layout": "v2",
            "versionId": lib.build.version_id, "versionDir": lib.build.version_dir,
            "sourceId": lib.source.source_id, "sourceRecorded": True,
            "takenIn": True,
            "sidecars": [],
            "source": source,
        }
    else:
        payload = {
            "layout": "v2",
            "versionId": lib.build.version_id, "packageId": placed.package.get("packageId"),
            "versionDir": lib.build.version_dir,
            "complete": placed.complete,
            "sourceId": lib.source.source_id, "sourceRecorded": True,
            "package": placed.package,
            "sidecars": placed.sidecars,
            "source": source,
        }
    if placed.original is not None:
        payload["original"] = {"path": str(placed.original), "name": placed.original.name}
    return payload


# ---------------------------------------------------------------- takein

def take_in(item: ClaimedItem) -> Placed:
    """Take an item's original into a version folder of its own, with no
    package (build mode takein), in package_version's steps but those of
    the package: the staging folder; the source record in
    <stagingDir>/source/ unless the source is recorded already;
    version.json in <stagingDir>/version/; the source renamed into the
    record, the original into the staged version folder, last, under the
    name the worker record gives it, and that folder into the record.

    A version taken in has no chain — no checksums.sha256, no
    package.json, no .complete: nothing covers its version.json until a
    package is added to it, and the original's fixity is its source
    record's. A version folder already there as a takein leaves it is the
    work of a run whose handover was lost, and is reported again; any
    other fails the run, and is never written over."""
    lib = _library(item)
    if lib.build.mode != TAKEIN:
        raise PackageError(f"the worker record's build mode is {lib.build.mode!r}, not takein")
    vdir = Path(lib.build.version_dir)
    sdir = Path(lib.source.record_dir)
    staging = Path(lib.build.staging_dir)
    if not restore_original(staging):
        raise PackageError(kept(staging))
    if os.path.lexists(vdir):
        return _taken_in_there(item)
    if os.path.lexists(sdir) and not (sdir / SUMS).is_file():
        raise PackageError(_unfinished(lib))
    original = Path(item.path)
    _outside_the_record(original, lib)
    fixity = _check_original(original, lib.source.size_bytes, lib.source.qh1, "the original")
    open_staging(staging)
    try:
        (staging / "version").mkdir()
        probe = records.probe_original(original)
        now = utc_now()
        source, _copies, staged_source = _source_of(item, lib, staging, probe, fixity, now)
        (staging / "version" / VERSION_RECORD).write_bytes(
            rec.json_bytes(records.version_record(lib, source, probe, now)))
        if not _into_the_record(item, lib, staging, staged_source):
            return _taken_in_there(item)
    except Exception:
        remove_staging(staging)
        raise
    log.info("packager.library.taken_in", item_id=item.id, version_id=lib.build.version_id,
             source_recorded_now=staged_source, original=lib.build.original_name)
    return Placed(vdir, None, None, [], original=vdir / str(lib.build.original_name))


def _taken_in_there(item: ClaimedItem) -> Placed:
    """A version folder in place already, for a takein: reported again as
    it is when it holds this version's record and the original it keeps,
    the file the catalog recorded, and nothing else; else the run fails.
    Never written over."""
    lib = _library(item)
    vid, sid = lib.build.version_id, lib.source.source_id
    vdir = Path(lib.build.version_dir)
    try:
        version = json.loads((vdir / VERSION_RECORD).read_text())
        entries = sorted(os.listdir(vdir))
    except (OSError, ValueError) as e:
        raise PackageError(f"versions/{vid} is there but can't be read: {e}") from e
    if not isinstance(version, dict) or version.get("versionId") != vid \
            or sid not in (version.get("sourceIds") or []):
        raise PackageError(f"versions/{vid} holds the record of another version or source")
    if entries != sorted([VERSION_RECORD, str(lib.build.original_name)]):
        raise PackageError(f"versions/{vid} is there but not the version a takein leaves "
                           f"(it holds {', '.join(entries) or 'nothing'}): a version folder "
                           f"is never written over")
    original = _original_in(vdir, version, lib)
    if original is None:
        raise PackageError(f"versions/{vid} keeps no original {lib.build.original_name}: a "
                           f"version folder is never written over")
    if not (Path(lib.source.record_dir) / SUMS).is_file():
        raise PackageError(f"versions/{vid} is there, but its source sources/{sid} is not")
    log.info("packager.library.reported_again", item_id=item.id, version_id=vid, taken_in=True)
    return Placed(vdir, None, None, [], reported_again=True, original=original)


# ---------------------------------------------------------------- extras

def package_extra(
    extra: ClaimedExtra, inputs: PackageInputs, *, options: PackageOptions,
    language_whitelist: list[str] | None = None, keep_original_if_single: bool = True,
    track_languages: list[dict[str, Any]] | None = None,
) -> Placed:
    """Package an extra whose worker record carries a library block into
    its title's extras/<extraId>/ (contract section 6), as a version is:
    built in <stagingDir>/extra/ — extra.json, the package, the chain —
    and renamed into place in one step. It keeps no original: extra.json
    names the file it was made from in packagedFrom. A folder there already
    and whole is reported again; one that isn't fails the run."""
    lib = _extra_library(extra)
    xdir, staging = Path(lib.extra_dir), Path(lib.staging_dir)
    if os.path.lexists(xdir):
        return _extra_there(extra)
    original = Path(extra.path)
    named = lib.original or {}
    fixity = _check_original(original, named.get("sizeBytes"), named.get("qh1"),
                             "the extra's file")
    open_staging(staging)
    try:
        xstage = staging / "extra"
        xstage.mkdir()
        built = build_package(
            extra.id, extra.path, "extra", xstage, inputs=inputs, options=options,
            language_whitelist=language_whitelist,
            keep_original_if_single=keep_original_if_single, title=extra.title,
            track_languages=track_languages, trickplay=False, codecs=HEVC_ONLY)
        probe = records.probe_original(original)
        now = utc_now()
        doc = records.extra_record(extra.id, lib, original, str(named.get("name") or original.name),
                                   fixity, probe, now)
        record = rec.json_bytes(doc)
        (xstage / EXTRA_RECORD).write_bytes(record)
        peak = rec.peak_bandwidth(str(xstage), built.manifest["hls"]["master"])
        package_id = str(uuid.uuid4())
        body = close_chain(xstage, EXTRA_RECORD, record, rec.EXTRA_DIRS, lambda listed:
                           records.package_record(package_id, built, listed, doc, probe, {},
                                                  peak, now))
        package = json.loads(body)
        _verify_staged(xstage, package)
        problem = verify_chain(xstage, EXTRA_RECORD, rec.EXTRA_DIRS)
        if problem is not None:
            raise PackageError(f"the staged extra is not whole: {problem}")
        if not place(xstage, xdir):
            return _extra_there(extra)
    except Exception:
        remove_staging(staging)
        raise
    log.info("packager.library.extra", extra_id=extra.id, package_id=package_id,
             files=package["checksums"]["files"], bytes=package["checksums"]["bytes"])
    return Placed(xdir, body, package, [])


def _extra_library(extra: ClaimedExtra) -> ExtraLibrary:
    if extra.library is None:
        raise PackageError("the worker record has no library block")
    return extra.library


def _extra_there(extra: ClaimedExtra) -> Placed:
    lib = _extra_library(extra)
    xdir = Path(lib.extra_dir)
    problem = verify_chain(xdir, EXTRA_RECORD, rec.EXTRA_DIRS)
    if problem is not None:
        raise PackageError(f"extras/{extra.id} is there but not a whole extra ({problem}): "
                           f"an extra's folder is never written over")
    try:
        doc = json.loads((xdir / EXTRA_RECORD).read_text())
        body = (xdir / PACKAGE_RECORD).read_bytes()
        package = json.loads(body)
    except (OSError, ValueError) as e:
        raise PackageError(f"extras/{extra.id} can't be read: {e}") from e
    if doc.get("extraId") != extra.id:
        raise PackageError(f"extras/{extra.id} holds the record of another extra")
    log.info("packager.library.extra_reported_again", extra_id=extra.id,
             package_id=package.get("packageId"))
    return Placed(xdir, body, package, [], reported_again=True)


def extra_payload(
    extra_id: str, lib: ExtraLibrary, placed: Placed, source: dict[str, Any],
) -> dict[str, Any]:
    """POST /api/extras/{id}/packaging-complete, v2 (contract section 2.6)."""
    return {
        "layout": "v2", "extraId": extra_id, "extraDir": lib.extra_dir,
        "packageId": placed.package.get("packageId"), "complete": placed.complete,
        "package": placed.package, "source": source,
    }


def finish(staging_dir: str, *inboxes: Path | None) -> None:
    """Once the catalog took the handover: the run's staging folder (its
    sentinel is all that is left there) and the transcoder's handoff go.
    Best effort."""
    remove_staging(Path(staging_dir))
    for inbox in inboxes:
        if inbox is not None and os.path.lexists(inbox) and not _remove(inbox):
            log.warning("packager.library.inbox_cleanup_failed", dir=str(inbox))


# ---------------------------------------------------------------- inputs

def has_handoff(inbox: Path) -> bool:
    return (inbox / CONTRACT_FILE).is_file() or (inbox / PREPARED_FILE).is_file()


def handoff(inbox_dir: str, legacy_inbox: Path, original: str) -> tuple[PackageInputs, Path | None]:
    """The inputs of a v2 run and the inbox they came from: the
    transcoder's handoff in the record's inbox; else one it left in the
    package store's inbox (legacy_inbox) for a transcode that finished
    before the layout switched; else the original (None). Raises
    renditions.ContractError for a handoff that can't be read."""
    for inbox in (Path(inbox_dir), legacy_inbox):
        if has_handoff(inbox):
            if inbox == legacy_inbox:
                log.info("packager.library.legacy_handoff", inbox=str(inbox))
            return resolve_inputs(inbox, original), inbox
    return resolve_inputs(Path(inbox_dir), original), None
