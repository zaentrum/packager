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
    in_progress / done / failed as the worker progresses.
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

Token refresh on 401 is handled here so the worker loop stays
straightforward. Pattern is mirrored from katalog-analyzer; the two
clients are intentionally parallel so anyone reading both sees the same
shape.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

# Keycloak default for client_credentials is 300 s; refresh 30 s ahead
# so we never send a token within seconds of expiry.
TOKEN_REFRESH_LEAD_SECONDS = 30


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
    # "language": "eng", "label": "English", "forced": false}]. Passed on
    # as sent, like track_languages.
    subtitle_files: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tmdb_id(self) -> str | None:
        """The single TMDB ID applicable to this item's media type —
        the series ID for episodes, the movie ID for movies."""
        if self.type == "episode":
            return self.series_tmdb_id
        return self.movie_tmdb_id

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> ClaimedItem:
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
    # The catalog answers 404 for a removed extra; a record that says it
    # was removed all the same is treated as gone.
    removed: bool = False

    @classmethod
    def from_json(cls, extra_id: str, body: dict[str, Any]) -> ClaimedExtra:
        """The record of `extra_id`, the id the request named: it names
        the extra's inbox and package folders."""
        return cls(
            id=extra_id,
            parent_id=str(body.get("parentId") or ""),
            kind=str(body.get("kind") or ""),
            title=str(body.get("title") or ""),
            path=str(body.get("path") or ""),
            state=str(body.get("state") or "").lower(),
            parent_title=str(body.get("parentTitle") or ""),
            removed=bool(body.get("removedAt") or body.get("removed")),
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
    ) -> None:
        """Move the package step to `status`. Best-effort; failures
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
                f"/api/analyze/items/{item_id}/steps/package",
                json=body,
            )
            if resp.status_code >= 400:
                log.warning(
                    "package.step.upsert_failed",
                    item_id=item_id,
                    status=status,
                    http=resp.status_code,
                    body=resp.text[:300],
                )
        except Exception as e:
            log.warning(
                "package.step.upsert_exception",
                item_id=item_id,
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
        (an HTTP error, no answer): an extra has no repair path for a
        package the catalog doesn't know, so the extras loop then fails
        the step and the catalog's retry runs the chain again."""
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
