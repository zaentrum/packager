"""Entry point. One process runs:
  - the worker loop (thread)
  - a tiny FastAPI server for /healthz and /readyz, so kubelet probes work.

Same shape as katalog-analyzer's main — intentionally — so anyone
reading both can map them onto each other line-for-line."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

import structlog
import uvicorn
from fastapi import FastAPI

from .config import Config
from .katalog import KatalogClient
from .worker import run_worker

# Pods run with a random non-root UID in GID 0. Without this, mkdir creates
# 0750 shards and a packager pod running under a different UID can't write
# into directories created by another. 0002 → group rwx so peers can share.
os.umask(0o002)


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def main() -> int:
    _configure_logging()
    log = structlog.get_logger("packager.main")
    cfg = Config.from_env()
    log.info(
        "packager.start",
        katalog=cfg.katalog_api_url,
        batch_size=cfg.claim_batch_size,
        idle_sleep=cfg.idle_sleep_seconds,
    )

    client = KatalogClient(
        base_url=cfg.katalog_api_url,
        token_url=cfg.oidc_token_url,
        client_id=cfg.oidc_client_id,
        client_secret=cfg.oidc_client_secret,
    )

    stop = threading.Event()

    def _handle_sigterm(signum: int, _frame: object) -> None:
        log.info("packager.signal", signum=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    worker_thread = threading.Thread(
        target=run_worker,
        kwargs={
            "client": client,
            "batch_size": cfg.claim_batch_size,
            "idle_sleep": cfg.idle_sleep_seconds,
            "error_sleep": cfg.error_sleep_seconds,
            "stop": stop,
        },
        daemon=True,
        name="packager-worker",
    )
    worker_thread.start()

    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> dict:
        return {"ok": worker_thread.is_alive()}

    uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None)
    stop.set()
    client.close()
    worker_thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
