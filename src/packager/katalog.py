"""HTTP client for the katalog Spring app.

Calls in scope for the packager (a PURE Kafka consumer — it no longer
polls a claim endpoint; the itemId arrives on the
`stube.catalog.item.transcoded` topic):
  * `GET  /api/analyze/items/{id}` — fetch the full item detail from
    the itemId carried on the Kafka event (the event carries only the
    id; the client never trusts a payload-supplied path).
  * `GET  /api/analyze/items/{id}/steps` — read the current step
    statuses for the idempotency guard (skip work if package has
    already finished).
  * `PUT  /api/analyze/items/{id}/steps/package` — flip the step to
    in_progress / done / failed as the worker progresses
    (`steps/takein` for a takein job of the library v2 layout).
  * `POST /api/items/{id}/packaging-complete` — mirror the on-disk
    manifest into the catalog DB after a successful package, with a
    `source` block (codec, width, height, durationMs, bitRate) the
    catalog keeps as the title's source asset.
  * `POST /api/analyze/items/{id}/fail` — last-resort hard fail when
    the worker can't even attribute the error to the package step
    (e.g. the source file vanished from NFS).

The extras mode (extras.py) makes the same kinds of call for an extra
of a title — a trailer, a featurette — which the catalog keeps and the
packager packages apart from its title:
  * `GET  /api/analyze/extras/{id}` — the extra's worker record (its
    title, parent, kind, source path and state). 404 when the catalog
    doesn't know it or has removed it.
  * `PUT  /api/analyze/extras/{id}/steps/package` — the extra's
    package step, with the body an item's step takes.
  * `POST /api/extras/{id}/packaging-complete` — the manifest (and the
    source block) of the extra's package, which makes it playable.

When the catalog runs the library v2 layout, both worker records carry a
`library` block (the title's folders in the library tree, below) and the
two packaging-complete calls take the v2 payload instead, which the
catalog must take (a 2xx) before the step is done (library.py).

Token refresh on 401 is handled here so the worker loop stays
straightforward. Pattern is mirrored from katalog-analyzer; the two
clients are intentionally parallel so anyone reading both sees the same
shape.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

# Keycloak default for client_credentials is 300 s; refresh 30 s ahead
# so we never send a token within seconds of expiry.
TOKEN_REFRESH_LEAD_SECONDS = 30

# ---------------------------------------------------------- the library block
#
# A worker record carries a `library` block when the catalog's setting
# library.layout is v2 (contract platform-library/1): the folders of the
# title's record in the library tree, which the packager writes into
# (library.py), and the version it builds, how (its mode) and under which
# name its original goes into it. Without the block the packager works as
# it always has, into the package store. Every path and name in it is the
# catalog's: the packager never works one out itself.

LIBRARY_CONTRACT = 1

# What a run of a version does (library.build.mode), as the catalog decides
# it: `establish` builds a source's first version, its package and its
# original; `takein` a version that keeps its original and has no package;
# `add` builds the package of a version that holds only its original;
# `repackage` a new version of a source whose version has a package, with
# no original. A record without a mode is a run as before the catalog knew
# them, which builds as `repackage` does.
ESTABLISH, TAKEIN, ADD, REPACKAGE = "establish", "takein", "add", "repackage"
BUILD_MODES = (ESTABLISH, TAKEIN, ADD, REPACKAGE)
# The modes that rename the original into the version folder they build.
MOVES_ORIGINAL = frozenset({ESTABLISH, TAKEIN})

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_QH1 = re.compile(r"sha256:[0-9a-f]{64}")
# The name an original gets in its version folder: original.<ext>, or
# original-<n>.<ext> for a part of a version split into several.
_ORIGINAL_NAME = re.compile(r"original(-[1-9][0-9]*)?\.[a-z0-9]{1,8}")


class LibraryRecordError(ValueError):
    """A library block the packager can't work from: the step fails with
    this message. A record that carries a library block is never packaged
    into the package store instead."""


@dataclass(frozen=True)
class LibrarySource:
    """library.source: the original this run packages, as the catalog
    recorded it at its arrival."""
    source_id: str
    # Whether sources/<sourceId>/ is in the record already (an earlier
    # version of the same original wrote it).
    recorded: bool
    record_dir: str           # <itemDir>/sources/<sourceId>
    library_path: str         # relative to the arrivals root
    size_bytes: int | None
    qh1: str | None


@dataclass(frozen=True)
class LibraryBuild:
    """library.build: the version this run builds. Its id stays the same
    across retries until a package for it is recorded complete. For an
    `add`, the version folder is the one there already, which holds the
    original."""
    version_id: str
    staging_dir: str          # <workRoot>/staging/<versionId>
    version_dir: str          # <itemDir>/versions/<versionId>
    created_by: str
    chapters: list[dict[str, Any]]
    chapters_from: str | None
    segments: list[dict[str, Any]]
    # One of BUILD_MODES; None in a record from before the catalog named one.
    mode: str | None = None
    # The name the original gets in the version folder (original.mkv), for
    # a mode that renames it there (MOVES_ORIGINAL); None for the others.
    original_name: str | None = None


@dataclass(frozen=True)
class ItemLibrary:
    """An item's library block (GET /api/analyze/items/{id})."""
    root: str
    item_dir: str
    inbox_dir: str            # the transcoder's handoff
    source: LibrarySource
    build: LibraryBuild
    # The item's complete version, {"versionId", "dir"}, or None.
    current: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExtraLibrary:
    """An extra's library block (GET /api/analyze/extras/{id})."""
    item_dir: str             # its title's folder
    inbox_dir: str
    staging_dir: str          # <workRoot>/staging/extra-<extraId>
    extra_dir: str            # <itemDir>/extras/<extraId>
    recorded: bool
    # What extra.json takes from the catalog: kind, title, localizedTitles,
    # language, seasonNumber, origin, createdAt, createdBy.
    record: dict[str, Any]
    # The file the package is made from, {name, sizeBytes, qh1}, as the
    # catalog took it in; None when the record names none.
    original: dict[str, Any] | None = None


@dataclass(frozen=True)
class Handover:
    """The catalog's answer to a v2 packaging-complete: `taken` only for a
    2xx; `status` is None when it didn't answer at all, and `error` says
    why it wasn't taken."""
    taken: bool
    status: int | None
    answer: dict[str, Any]
    error: str | None


def _block(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LibraryRecordError(f"worker record: {where} is not an object")
    return raw


def _contract(lib: dict[str, Any]) -> None:
    contract = lib.get("contract")
    if contract != LIBRARY_CONTRACT or isinstance(contract, bool):
        raise LibraryRecordError(
            f"worker record: library contract {contract!r} is not supported "
            f"(this packager writes contract {LIBRARY_CONTRACT})")


def _abs_path(obj: dict[str, Any], key: str, where: str) -> str:
    """An absolute, normalised path: no '..', no '//', no trailing '/'."""
    value = obj.get(key)
    if not isinstance(value, str) or not os.path.isabs(value) or os.path.normpath(value) != value:
        raise LibraryRecordError(f"worker record: {where}.{key} is not an absolute path: {value!r}")
    return value


def _uuid(obj: dict[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise LibraryRecordError(
            f"worker record: {where}.{key} is not a lower-case UUID: {value!r}")
    return value


def _size(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LibraryRecordError(f"worker record: {where} is not a size: {value!r}")
    return value


def _qh1(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _QH1.fullmatch(value):
        raise LibraryRecordError(f"worker record: {where} is not sha256:<hex>: {value!r}")
    return value


def _in(path: str, folder: str, name: str, where: str) -> None:
    """The record's own rule for a folder (contract section 2.1), checked,
    never applied: the packager renames into these folders, so one the rule
    doesn't give is refused rather than written."""
    want = os.path.join(folder, name)
    if path != want:
        raise LibraryRecordError(f"worker record: {where} {path} is not {want}")


def _marks(value: Any, where: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LibraryRecordError(f"worker record: {where} is not a list")
    return [m for m in value if isinstance(m, dict)]


def _mode(value: Any) -> str | None:
    if value is None or value in BUILD_MODES:
        return value
    raise LibraryRecordError(f"worker record: library.build.mode is not one of "
                             f"{', '.join(BUILD_MODES)}: {value!r}")


def _original_name(value: Any) -> str:
    """The name the catalog gives the original in its version folder,
    checked, never made up: the packager renames the original to it."""
    if not isinstance(value, str) or not _ORIGINAL_NAME.fullmatch(value):
        raise LibraryRecordError(f"worker record: library.build.originalName is not the name "
                                 f"of an original in its version folder (original.<ext>): "
                                 f"{value!r}")
    return value


def parse_item_library(raw: Any) -> ItemLibrary:
    """An item record's library block. Raises LibraryRecordError when it
    can't be worked from — and with the catalog's own words when it says
    the item can't be recorded yet (`blocked`)."""
    lib = _block(raw, "library")
    _contract(lib)
    blocked = lib.get("blocked")
    if blocked is not None:
        if not isinstance(blocked, str) or not blocked.strip():
            raise LibraryRecordError(f"worker record: library.blocked is not a reason: {blocked!r}")
        raise LibraryRecordError(blocked.strip())
    root = _abs_path(lib, "root", "library")
    item_dir = _abs_path(lib, "itemDir", "library")
    if not item_dir.startswith(root.rstrip("/") + "/"):
        raise LibraryRecordError(f"worker record: library.itemDir {item_dir} is not under {root}")
    src = _block(lib.get("source"), "library.source")
    source = LibrarySource(
        source_id=_uuid(src, "sourceId", "library.source"),
        recorded=src.get("recorded") is True,
        record_dir=_abs_path(src, "recordDir", "library.source"),
        library_path=src.get("libraryPath") if isinstance(src.get("libraryPath"), str) else "",
        size_bytes=_size(src.get("sizeBytes"), "library.source.sizeBytes"),
        qh1=_qh1(src.get("qh1"), "library.source.qh1"),
    )
    _in(source.record_dir, os.path.join(item_dir, "sources"), source.source_id,
        "library.source.recordDir")
    b = _block(lib.get("build"), "library.build")
    chapters_from = b.get("chaptersFrom")
    mode = _mode(b.get("mode"))
    build = LibraryBuild(
        version_id=_uuid(b, "versionId", "library.build"),
        staging_dir=_abs_path(b, "stagingDir", "library.build"),
        version_dir=_abs_path(b, "versionDir", "library.build"),
        created_by=b["createdBy"] if isinstance(b.get("createdBy"), str) else "",
        chapters=_marks(b.get("chapters"), "library.build.chapters"),
        chapters_from=chapters_from if isinstance(chapters_from, str) else None,
        segments=_marks(b.get("segments"), "library.build.segments"),
        mode=mode,
        original_name=_original_name(b.get("originalName")) if mode in MOVES_ORIGINAL else None,
    )
    _in(build.version_dir, os.path.join(item_dir, "versions"), build.version_id,
        "library.build.versionDir")
    _in(build.staging_dir, os.path.dirname(build.staging_dir), build.version_id,
        "library.build.stagingDir")
    current = lib.get("current")
    return ItemLibrary(
        root=root, item_dir=item_dir,
        inbox_dir=_abs_path(lib, "inboxDir", "library"),
        source=source, build=build,
        current=current if isinstance(current, dict) else None,
    )


def parse_extra_library(raw: Any, extra_id: str) -> ExtraLibrary:
    """An extra record's library block, for the extra `extra_id`. Raises
    LibraryRecordError when it can't be worked from."""
    lib = _block(raw, "library")
    _contract(lib)
    item_dir = _abs_path(lib, "itemDir", "library")
    staging_dir = _abs_path(lib, "stagingDir", "library")
    extra_dir = _abs_path(lib, "extraDir", "library")
    _in(extra_dir, os.path.join(item_dir, "extras"), extra_id, "library.extraDir")
    _in(staging_dir, os.path.dirname(staging_dir), f"extra-{extra_id}", "library.stagingDir")
    original = lib.get("original")
    if original is not None:
        original = _block(original, "library.original")
        _size(original.get("sizeBytes"), "library.original.sizeBytes")
        _qh1(original.get("qh1"), "library.original.qh1")
    return ExtraLibrary(
        item_dir=item_dir,
        inbox_dir=_abs_path(lib, "inboxDir", "library"),
        staging_dir=staging_dir,
        extra_dir=extra_dir,
        recorded=lib.get("recorded") is True,
        record=_block(lib.get("record"), "library.record"),
        original=original,
    )


@dataclass
class ClaimedItem:
    id: str
    type: str
    title: str
    year: int | None
    duration_ms: int | None
    path: str
    # Episode coordinates + parent series title — only populated when
    # type=='episode'. The packager writes them into the manifest so
    # an episode package is self-describing ("Ghosts S01E03 Spies")
    # even if the catalog DB is lost. Series TMDB ID is also surfaced
    # so the operator can re-fetch all metadata from TMDB later.
    season_number: int | None = None
    episode_number: int | None = None
    series_title: str | None = None
    series_tmdb_id: str | None = None
    movie_tmdb_id: str | None = None
    # The catalog's language for a track where it knows better than the
    # file's tag: [{"kind": "audio" | "subtitle", "ordinal": 0,
    # "language": "eng"}], `ordinal` counting the source's tracks of that
    # kind in ffprobe order. Passed on as sent; package_item checks every
    # entry. Empty when the record has none.
    track_languages: list[dict[str, Any]] = field(default_factory=list)
    # Subtitle files next to the source: [{"path": "/abs/movie.en.srt",
    # "language": "eng", "label": "English", "forced": false}], with the
    # catalog's id of each ("id") in a library v2 record. Passed on as
    # sent, like track_languages.
    subtitle_files: list[dict[str, Any]] = field(default_factory=list)
    # The library block (v2): None in a record without one, which packages
    # into the package store as always. library_error says why a record's
    # block can't be worked from (or the catalog's `blocked`); the step then
    # fails with it.
    library: ItemLibrary | None = None
    library_error: str | None = None

    @property
    def tmdb_id(self) -> str | None:
        """The single TMDB ID applicable to this item's media type —
        the series ID for episodes, the movie ID for movies."""
        if self.type == "episode":
            return self.series_tmdb_id
        return self.movie_tmdb_id

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> ClaimedItem:
        library, library_error = None, None
        if body.get("library") is not None:
            try:
                library = parse_item_library(body["library"])
            except LibraryRecordError as e:
                library_error = str(e)
        return cls(
            id=body["id"],
            type=body["type"],
            title=body.get("title") or "",
            year=body.get("year"),
            duration_ms=body.get("durationMs"),
            path=body["path"],
            season_number=body.get("seasonNumber"),
            episode_number=body.get("episodeNumber"),
            series_title=body.get("seriesTitle") or None,
            series_tmdb_id=body.get("seriesTmdbId"),
            movie_tmdb_id=body.get("movieTmdbId"),
            track_languages=_objects(body.get("trackLanguages")),
            subtitle_files=_objects(body.get("subtitleFiles")),
            library=library,
            library_error=library_error,
        )


def _objects(value: Any) -> list[dict[str, Any]]:
    """The JSON objects of an optional list field; [] when it is absent,
    null or not a list."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


@dataclass
class ClaimedExtra:
    """One extra of a title (a trailer, a featurette), as its worker
    record names it: its own source file, packaged on its own, never
    inside its title's package."""
    id: str
    parent_id: str
    kind: str
    title: str
    path: str
    state: str
    parent_title: str = ""
    # What the extra speaks, as the catalog names it (BCP 47 or ISO
    # 639-2, "en", "zxx"); empty when it names none.
    language: str = ""
    # The catalog answers 404 for a removed extra; a record that says it
    # was removed all the same is treated as gone.
    removed: bool = False
    # The library block (v2), as an item's: None without one; why one
    # can't be worked from in library_error.
    library: ExtraLibrary | None = None
    library_error: str | None = None

    @classmethod
    def from_json(cls, extra_id: str, body: dict[str, Any]) -> ClaimedExtra:
        """The record of `extra_id`, the id the request named: it names
        the extra's inbox and package folders."""
        library, library_error = None, None
        if body.get("library") is not None:
            try:
                library = parse_extra_library(body["library"], extra_id)
            except LibraryRecordError as e:
                library_error = str(e)
        return cls(
            id=extra_id,
            parent_id=str(body.get("parentId") or ""),
            kind=str(body.get("kind") or ""),
            title=str(body.get("title") or ""),
            path=str(body.get("path") or ""),
            state=str(body.get("state") or "").lower(),
            parent_title=str(body.get("parentTitle") or ""),
            language=str(body.get("language") or ""),
            removed=bool(body.get("removedAt") or body.get("removed")),
            library=library,
            library_error=library_error,
        )


class KatalogClient:
    def __init__(
        self,
        base_url: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(timeout=timeout_seconds)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def close(self) -> None:
        self._http.close()

    # ---------------------------------------------------------------- auth
    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        resp = self._http.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        ttl = int(body.get("expires_in", 60))
        self._token_expires_at = time.time() + ttl - TOKEN_REFRESH_LEAD_SECONDS
        log.debug("oidc.token_refreshed", expires_in=ttl)
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self._base}{path}"
        for attempt in range(2):
            resp = self._http.request(method, url, headers=self._headers(), **kwargs)
            if resp.status_code == 401 and attempt == 0:
                # Token revoked or rotated; refresh and retry once.
                self._token = None
                self._token_expires_at = 0
                continue
            return resp
        return resp  # type: ignore[return-value]

    # ------------------------------------------------------------ settings
    def settings(self) -> dict[str, str]:
        """Fetch the global key/value settings as a flat {key: valueText}
        map. Empty dict on any error — callers fall back to their own
        compile-time defaults. We don't raise here because settings
        are advisory: a packager that can't reach katalog-app for the
        language list should still package, just without the language
        filter."""
        try:
            resp = self._request("GET", "/api/settings")
            resp.raise_for_status()
            raw = resp.json() or {}
            return {k: (v.get("valueText") or "") for k, v in raw.items()
                    if isinstance(v, dict)}
        except Exception as e:
            log.warning("settings.fetch_failed", error=str(e)[:200])
            return {}

    # -------------------------------------------------------------- items
    def get_item(self, item_id: str) -> ClaimedItem | None:
        """Fetch one item's full detail from the itemId carried on the
        Kafka event. The katalog endpoint returns the full shape
        {id,type,title,year,durationMs,path,seasonNumber,episodeNumber,
        seriesTitle,seriesTmdbId,movieTmdbId} — everything the packager
        writes into a self-describing manifest — and, optionally,
        trackLanguages and subtitleFiles. Returns None on 404 (the
        item was deleted between the transcoder producing the event and
        us consuming it) so the caller can commit + skip the message."""
        resp = self._request("GET", f"/api/analyze/items/{item_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return ClaimedItem.from_json(resp.json())

    # ------------------------------------------------------------- steps
    def get_steps(self, item_id: str) -> dict[str, str]:
        """Return the current status of every analyze step on `item_id`
        as a flat {step: status} map. Used by the consumer's idempotency
        guard: if `package` has already finished (done, not_applicable,
        skipped) we skip the (expensive) packaging work on a redelivered
        or retried event. Empty dict on any error —
        the caller then treats the step as not-done and re-packages,
        which is safe (packaging is idempotent on disk)."""
        try:
            resp = self._request(
                "GET",
                f"/api/analyze/items/{item_id}/steps",
            )
            if resp.status_code >= 400:
                log.warning(
                    "steps.get_failed",
                    item_id=item_id,
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                return {}
            body = resp.json()
            steps = body.get("steps") or {}
            return {str(k): str(v) for k, v in steps.items()}
        except Exception as e:
            log.warning("steps.get_exception", item_id=item_id, error=str(e)[:200])
            return {}
    def upsert_step(
        self,
        item_id: str,
        status: str,
        *,
        error: str | None = None,
        details: str | None = None,
        step: str = "package",
    ) -> None:
        """Move the package step — or `step`, the takein step of a takein
        job — to `status`. Best-effort; failures
        are logged and swallowed so a flaky bookkeeping call doesn't
        crash an otherwise-successful packaging job. The endpoint is
        idempotent via ON CONFLICT (item_id, step)."""
        body: dict[str, Any] = {"status": status}
        if error is not None:
            body["error"] = error[:500]
        if details is not None:
            body["details"] = details
        try:
            resp = self._request(
                "PUT",
                f"/api/analyze/items/{item_id}/steps/{step}",
                json=body,
            )
            if resp.status_code >= 400:
                log.warning(
                    "package.step.upsert_failed",
                    item_id=item_id,
                    step=step,
                    status=status,
                    http=resp.status_code,
                    body=resp.text[:300],
                )
        except Exception as e:
            log.warning(
                "package.step.upsert_exception",
                item_id=item_id,
                step=step,
                status=status,
                error=str(e)[:200],
            )

    def packaging_complete(self, item_id: str, manifest: dict[str, Any]) -> None:
        """Tell katalog-app a package landed on disk so it can mirror
        the manifest into the catalog DB (PlaybackAssets codec/res/
        bitrate, packaged PlaybackAsset row, SubtitleAssets rows).
        Best-effort — the packaging itself already succeeded, this
        callback just updates the UI-visible state.

        Idempotent on the server side: the endpoint replaces the
        kind='packaged' row and the SubtitleAssets set, so retries
        from a re-deploy or pod restart converge to the same shape."""
        try:
            resp = self._request(
                "POST",
                f"/api/items/{item_id}/packaging-complete",
                json=manifest,
            )
            if resp.status_code >= 400:
                log.warning(
                    "packaging_complete.upload_failed",
                    item_id=item_id,
                    status=resp.status_code,
                    body=resp.text[:300],
                )
        except Exception as e:
            log.warning(
                "packaging_complete.exception",
                item_id=item_id,
                error=str(e)[:200],
            )

    def packaging_complete_v2(self, item_id: str, payload: dict[str, Any]) -> Handover:
        """Hand the catalog a version the packager put in the library tree
        (contract section 2.5): the step is done only when this is taken,
        a 2xx. A 409 (a stale version: a re-encode started meanwhile), a
        422 (a broken chain) or no answer at all is not."""
        return self._hand_over(f"/api/items/{item_id}/packaging-complete", payload,
                               item_id=item_id)

    def _hand_over(self, path: str, payload: dict[str, Any], **ids: str) -> Handover:
        try:
            resp = self._request("POST", path, json=payload)
        except Exception as e:
            log.warning("packaging_complete.exception", **ids, error=str(e)[:200])
            return Handover(False, None, {}, f"no answer: {str(e)[:200]}")
        try:
            body = resp.json()
        except ValueError:
            body = None
        answer = body if isinstance(body, dict) else {}
        if not 200 <= resp.status_code < 300:
            log.warning("packaging_complete.refused", **ids, status=resp.status_code,
                        body=resp.text[:300])
            return Handover(False, resp.status_code, answer,
                            f"the catalog answered {resp.status_code}: {resp.text.strip()[:300]}")
        return Handover(True, resp.status_code, answer, None)

    # ------------------------------------------------------------- extras
    def get_extra(self, extra_id: str) -> ClaimedExtra | None:
        """Fetch one extra's worker record, from the extraId on a consumed
        `catalog.extra.transcoded` event: {id, type, parentId, parentType,
        parentTitle, kind, title, language, seasonNumber, path, state}.
        None when the catalog doesn't know the extra or has removed it
        (404): the extras loop then logs, commits and skips. Any other
        error raises, as get_item does."""
        resp = self._request("GET", f"/api/analyze/extras/{extra_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return ClaimedExtra.from_json(extra_id, resp.json())

    def upsert_extra_step(
        self,
        extra_id: str,
        status: str,
        *,
        error: str | None = None,
        details: str | None = None,
    ) -> None:
        """Move an extra's package step to `status` (in_progress, done or
        failed), with the body an item's step takes; the catalog moves the
        extra's state with it. Best-effort, as upsert_step."""
        body: dict[str, Any] = {"status": status}
        if error is not None:
            body["error"] = error[:500]
        if details is not None:
            body["details"] = details
        try:
            resp = self._request(
                "PUT",
                f"/api/analyze/extras/{extra_id}/steps/package",
                json=body,
            )
            if resp.status_code >= 400:
                log.warning(
                    "package.extra_step.upsert_failed",
                    extra_id=extra_id,
                    status=status,
                    http=resp.status_code,
                    body=resp.text[:300],
                )
        except Exception as e:
            log.warning(
                "package.extra_step.upsert_exception",
                extra_id=extra_id,
                status=status,
                error=str(e)[:200],
            )

    def extra_packaging_complete(
        self, extra_id: str, manifest: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Hand the catalog an extra's package: its manifest, with the
        source block. The catalog records it (codec, size, peak bit rate,
        package size) and makes the extra playable, and answers {extraId,
        itemId, packaged, durationMs}. Returns that answer ({} when it has
        no JSON object), or None when the catalog didn't take the package
        (an HTTP error, no answer): only the catalog's word makes an extra
        playable, so the extras loop then fails the step and the
        catalog's retry runs the chain again."""
        try:
            resp = self._request(
                "POST",
                f"/api/extras/{extra_id}/packaging-complete",
                json=manifest,
            )
        except Exception as e:
            log.warning("extra_packaging_complete.exception", extra_id=extra_id,
                        error=str(e)[:200])
            return None
        if resp.status_code >= 400:
            log.warning("extra_packaging_complete.refused", extra_id=extra_id,
                        status=resp.status_code, body=resp.text[:300])
            return None
        try:
            answer = resp.json()
        except ValueError:
            return {}
        return answer if isinstance(answer, dict) else {}

    def extra_packaging_complete_v2(self, extra_id: str, payload: dict[str, Any]) -> Handover:
        """Hand the catalog an extra the packager put in its title's
        extras/<extraId>/ (contract section 2.6). Taken only with a 2xx."""
        return self._hand_over(f"/api/extras/{extra_id}/packaging-complete", payload,
                               extra_id=extra_id)

    def fail(self, item_id: str, reason: str) -> None:
        """Catastrophic-failure fallback (source file missing, etc.).
        Sets transcode=failed via the same global handler the analyzer
        uses; logs are captured in the audit row."""
        try:
            resp = self._request(
                "POST",
                f"/api/analyze/items/{item_id}/fail",
                json={"reason": reason},
            )
            if resp.status_code >= 400:
                log.warning(
                    "transcode.fail.report_failed",
                    item_id=item_id,
                    status=resp.status_code,
                    body=resp.text[:300],
                )
        except Exception as e:
            log.warning("transcode.fail.exception", item_id=item_id, error=str(e)[:200])
