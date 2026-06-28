"""HTTP client for the katalog Spring app.

Three calls in scope for the packager:
  * `POST /api/analyze/claim?pass=packager` — claim items whose
    package step is pending (set by the transcoder after it
    finishes or skips).
  * `PUT  /api/analyze/items/{id}/steps/package` — flip the step to
    in_progress / done / failed as the worker progresses.
  * `POST /api/analyze/items/{id}/fail` — last-resort hard fail when
    the worker can't even attribute the error to the package step
    (e.g. the source file vanished from NFS).

Token refresh on 401 is handled here so the worker loop stays
straightforward. Pattern is mirrored from katalog-analyzer; the two
clients are intentionally parallel so anyone reading both sees the same
shape.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
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

    # ------------------------------------------------------------- claims
    def claim(self, limit: int = 1) -> list[ClaimedItem]:
        """Claim up to `limit` items in package=pending state. The
        Java side flips package=in_progress as part of the same
        transaction so a sibling packager pod won't grab the same
        item between dequeue and the first heartbeat below."""
        resp = self._request(
            "POST",
            f"/api/analyze/claim?pass=packager&limit={limit}",
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        return [ClaimedItem.from_json(it) for it in items]

    # ------------------------------------------------------------- steps
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
