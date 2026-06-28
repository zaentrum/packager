"""Runtime configuration. Everything from env vars; defaults are
sized for a single-replica deployment doing a serial packaging sweep.
Packaging is CPU + disk heavy, so the default `claim_batch_size` is 1 —
running two packages in parallel inside the same pod just doubles the
CPU pressure with no end-to-end benefit. Scale up via replicas, not
batch size."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    katalog_api_url: str
    oidc_token_url: str
    oidc_client_id: str
    oidc_client_secret: str
    # Worker tunables. The HTTP claim endpoint clamps to [1, 32]; the
    # per-cycle wait is what controls how often an idle worker re-polls.
    claim_batch_size: int = 1
    idle_sleep_seconds: float = 30.0
    error_sleep_seconds: float = 60.0
    # Output root for packaged items. Mounted from the katalog-packages
    # PVC in the deployment.
    packages_root: str = "/var/lib/katalog/packages"

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            katalog_api_url=_require("KATALOG_API_URL"),
            oidc_token_url=_require("OIDC_TOKEN_URL"),
            oidc_client_id=_require("OIDC_CLIENT_ID"),
            oidc_client_secret=_require("OIDC_CLIENT_SECRET"),
            claim_batch_size=int(os.environ.get("CLAIM_BATCH_SIZE", "1")),
            idle_sleep_seconds=float(os.environ.get("IDLE_SLEEP_SECONDS", "30")),
            error_sleep_seconds=float(os.environ.get("ERROR_SLEEP_SECONDS", "60")),
            packages_root=os.environ.get("PACKAGES_ROOT", "/var/lib/katalog/packages"),
        )


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"required env var {key} is empty/unset")
    return val
