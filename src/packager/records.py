"""What the packager's library v2 records say: sources/<sid>/source.json
(with its ffprobe.json and the sidecar copies), versions/<vid>/version.json,
the package.json of a version or an extra, and extras/<xid>/extra.json.

The records are built by the schemas repository's record logic,
libv2_records.py, vendored byte for byte (tests/test_libv2_records.py
guards the copy): the same code the migration writes its records with,
so the two agree on every byte the deletion gate reads. What only the
packager knows it adds before handing over: which stream of the original
each rendition was made from. Where the records are written, and in
which order, is library.py's business."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from . import libv2_records as rec
from .katalog import ExtraLibrary, ItemLibrary
from .packager import Built, PackageError, _source_ordinals

log = structlog.get_logger(__name__)

PROBE_FILE = "ffprobe.json"
SOURCE_RECORD = "source.json"
CHAPTERS_FROM = frozenset({"original-file", "legacy-catalog", "human"})
EXTRA_KINDS = frozenset({"featurette", "behind-the-scenes", "making-of", "deleted-scene",
                         "interview", "trailer", "teaser", "gag-reel", "short", "other"})


@dataclass(frozen=True)
class OriginalProbe:
    """The verbatim probe of an original (libv2_records.FFPROBE_ARGS), and
    the ffprobe that made it."""
    raw: dict[str, Any]
    version: str | None


def probe_original(path: Path) -> OriginalProbe:
    """Probe an original for its record. Raises PackageError when ffprobe
    can't read it: a record written once is never written without the
    probe that describes its file."""
    raw = rec.ffprobe(str(path))
    if not isinstance(raw, dict):
        raise PackageError(f"ffprobe can't read the original {path.name}, so it gets no record")
    return OriginalProbe(raw=raw, version=rec.ffprobe_version())


# ---------------------------------------------------------------- sources

def source_record(
    lib: ItemLibrary, original: Path, fixity_qh1: str, probe: OriginalProbe,
    sidecars: list[dict[str, Any]], now: str,
) -> tuple[dict[str, Any], bytes]:
    """sources/<sid>/source.json and the bytes of its ffprobe.json
    (contract section 3.3): taken in by the packager, with the fixity the
    catalog recorded. The record logic reads the name the original arrived
    under, and where it sat among the arrivals, for what they claim (its
    labels, its numbering) and keeps neither: it names the file as the
    library does (file.name, original.<ext>), and the probe it returns for
    ffprobe.json names it so too, with no container title."""
    doc, probe_bytes = rec.source_record(
        lib.source.source_id, original.name, original.stat().st_size, taken_at=now,
        taken_by="packager", library_path=lib.source.library_path or original.name,
        qh1=fixity_qh1, mtime=rec.ts_of_mtime(str(original)),
        probe=probe.raw, probe_version=probe.version, sidecars=sidecars)
    return doc, probe_bytes


# ---------------------------------------------------------------- versions

def version_record(
    lib: ItemLibrary, source: dict[str, Any], probe: OriginalProbe, now: str,
) -> dict[str, Any]:
    """versions/<vid>/version.json (contract section 3.3): the catalog's
    marks for the version, the original's own chapters when the catalog
    has none (a version keeps the chapters its original carries), and the
    original it keeps, by the name the worker record gives it there, for a
    run that renames it into the version folder (none for any other)."""
    build = lib.build
    chapters = rec.chapter_marks(build.chapters)
    chapters_from = (build.chapters_from if build.chapters_from in CHAPTERS_FROM
                     else "original-file") if chapters else None
    if not chapters:
        chapters = rec.probe_chapters(probe.raw)
        chapters_from = "original-file" if chapters else None
    segments = rec.segments([
        # The worker record names a range's detector as version.json does;
        # the record logic reads the catalog row's name for it.
        {**s, "source": s.get("detector", s.get("source"))} for s in build.segments
        if isinstance(rec.num(s.get("startMs")), int)])
    return rec.version_record(
        build.version_id, source, created_at=now, created_by=build.created_by or "packager",
        chapters=chapters, chapters_from=chapters_from, segments=segments,
        original_files=[build.original_name] if build.original_name else [])


# ---------------------------------------------------------------- packages

def _of_kind(probe: OriginalProbe, kind: str) -> list[dict[str, Any]]:
    return [s for s in probe.raw.get("streams") or [] if s.get("codec_type") == kind]


def for_record(built: Built, probe: OriginalProbe, now: str) -> dict[str, Any]:
    """The built package's manifest as its record is made of it: completed
    at `now`, the moment its chain closes (the version record's too), and
    each rendition naming the stream of the original it was made from
    (sourceStreamIndex) and an audio track's channels there
    (sourceChannels). The transcoder's v0 carries the original's tracks in
    order, so a track is found by its place among those of its codec
    (packager._source_ordinals)."""
    man = copy.deepcopy(built.manifest)
    man["packagedAt"] = now
    ren = man.get("renditions") or {}
    video = next((s for s in _of_kind(probe, "video")
                  if not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is not None:
        for v in ren.get("video") or []:
            v["sourceStreamIndex"] = video["index"]
    audio = _of_kind(probe, "audio")
    at = _source_ordinals(built.probe.audio, audio)
    for a in [*(ren.get("audio") or []), *(ren.get("audioSurround") or [])]:
        idx = a.get("idx")
        if isinstance(idx, int) and 0 <= idx < len(at) and at[idx] < len(audio):
            a["sourceStreamIndex"] = audio[at[idx]]["index"]
            if rec.num(audio[at[idx]].get("channels")):
                a["sourceChannels"] = rec.num(audio[at[idx]]["channels"])
    subtitles = _of_kind(probe, "subtitle")
    st = _source_ordinals(built.probe.subtitles, subtitles)
    for s in man.get("subtitles") or []:
        if s.get("external"):
            continue
        n = rec.num(str(s.get("id", "")).removeprefix("sub"))
        if n is not None and 0 <= n < len(st) and st[n] < len(subtitles):
            s["sourceStreamIndex"] = subtitles[st[n]]["index"]
    return man


def package_record(
    package_id: str, built: Built, listed: list[tuple[str, str, int]], source: dict[str, Any],
    probe: OriginalProbe, sidecars: dict[str, str], peak_bandwidth_bps: int | None, now: str,
    *, beside_original: bool = False,
) -> dict[str, Any]:
    """package.json of a version or an extra: the package `built` as the
    record logic describes it, its losses measured against `source` (the
    source record, or the probed extra record), `listed` what the
    checksums written below it list, `sidecars` the copy in the source
    folder each subtitle made from a file next to the original was made
    from. Its role is what it is as it is written: `derived` beside the
    original its version folder keeps (beside_original), else
    `canonical`, the only copy. What the record logic normalised (a forced
    subtitle is never the default) is logged."""
    doc, notes = rec.package_record(
        package_id, for_record(built, probe, now), listed, source=source,
        role="derived" if beside_original else "canonical",
        created_at=now, peak_bandwidth_bps=peak_bandwidth_bps, sidecars=sidecars)
    for note in notes:
        log.info("packager.library.package_note", package_id=package_id, note=note)
    return doc


# ---------------------------------------------------------------- extras

def _kind_title(kind: str) -> str:
    """The title of an extra the catalog gives none: what kind of extra it
    is, in a word ("Behind the scenes", "Trailer"); "Extra" for other."""
    return "Extra" if kind == "other" else kind.replace("-", " ").capitalize()


def extra_record(
    extra_id: str, lib: ExtraLibrary, original: Path, original_name: str, fixity_qh1: str,
    probe: OriginalProbe, now: str,
) -> dict[str, Any]:
    """extras/<xid>/extra.json (contract section 6): what the catalog took
    the extra in as (library.record), what the packager's probe of its
    original says, and that original — never kept in the folder — in
    packagedFrom, which the record logic names as the library names an
    original, whatever `original_name` it is given. A value the record
    can't hold is left out (a kind it doesn't know is 'other'), never
    written as it came; a title the catalog gives none of is its kind's
    word, never a file's name."""
    record = lib.record
    kind = str(record.get("kind") or "").strip().lower()
    kind = kind if kind in EXTRA_KINDS else "other"
    titles = record.get("localizedTitles")
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "link":
        fetched = rec.ts(origin.get("fetchedAt"))
        origin = {"kind": "link", **{k: v for k, v in (
            ("site", rec.text(origin.get("site"))),
            ("externalId", rec.text(origin.get("externalId"))),
            ("url", rec.text(origin.get("url"))),
            ("fetchedAt", fetched if rec.is_moment(fetched) else None)) if v}}
    else:
        origin = None
    season = record.get("seasonNumber")
    language = rec.text(record.get("language"))
    created_at = rec.ts(record.get("createdAt"))
    return rec.extra_record(
        extra_id,
        created_at=created_at if rec.is_moment(created_at) else now,
        created_by=rec.text(record.get("createdBy")) or "packager",
        kind=kind,
        title=rec.text(record.get("title")) or _kind_title(kind),
        localized_titles={k: rec.text(v) for k, v in titles.items()
                          if isinstance(k, str) and rec.LANGUAGE_RE.match(k) and rec.text(v)}
        if isinstance(titles, dict) else {},
        language=language if language and rec.LANGUAGE_RE.match(language) else None,
        season_number=season if isinstance(season, int) and not isinstance(season, bool)
        and season >= 0 else None,
        origin=origin, probe=probe.raw, probe_version=probe.version, probed_at=now,
        packaged_from=[{"name": original_name, "sizeBytes": original.stat().st_size,
                        "fixity": {"qh1": fixity_qh1}}])
