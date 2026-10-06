"""Entry point. One process runs:
  - the worker loop (thread)
  - the extras loop (a second thread: trailers and other bonus material,
    extras.py)
  - at startup, a sweep of what runs that ended uncleanly left in the
    package store's item folders and in the library v2 work tree's
    staging folder (thread)
  - a tiny FastAPI server for /healthz and /readyz, so kubelet probes work.

Same shape as katalog-analyzer's main — intentionally — so anyone
reading both can map them onto each other line-for-line."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI

from .config import Config
from .extras import run_extras_worker
from .katalog import KatalogClient
from .library import sweep_staging
from .packager import sweep_leftovers
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


def _sweep(grace_seconds: float, work_root: str) -> None:
    """The startup sweep: what runs that ended uncleanly left in the
    package store's item folders (sweep_leftovers, a walk of every one of
    them) and in the library v2 work tree's staging folder (sweep_staging,
    that folder only; the library itself is never walked). Either finds
    nothing where its tree isn't there. On its own thread; the worker
    doesn't wait for it."""
    for sweep, args in ((sweep_leftovers, (grace_seconds,)), (sweep_staging, (Path(work_root),))):
        try:
            sweep(*args)
        except Exception:
            structlog.get_logger("packager.main").exception("packager.sweep.failed",
                                                            sweep=sweep.__name__)


def main() -> int:
    _configure_logging()
    log = structlog.get_logger("packager.main")
    cfg = Config.from_env()
    log.info(
        "packager.start",
        katalog=cfg.katalog_api_url,
        brokers=cfg.kafka_brokers,
        group_id=cfg.kafka_group_id,
        consume_topic=cfg.consume_topic,
        extras_group_id=cfg.extras_group_id,
        extras_consume_topic=cfg.extras_consume_topic,
        segment_seconds=cfg.segment_seconds,
        surround_audio=cfg.surround_audio,
        surround_bitrate=cfg.surround_bitrate,
        hls_subtitles=cfg.hls_subtitles,
        preferred_languages=cfg.preferred_languages or None,
        old_package_grace_seconds=cfg.old_package_grace_seconds,
        work_root=cfg.work_root,
    )

    def katalog_client() -> KatalogClient:
        return KatalogClient(
            base_url=cfg.katalog_api_url,
            token_url=cfg.oidc_token_url,
            client_id=cfg.oidc_client_id,
            client_secret=cfg.oidc_client_secret,
        )

    # One client per loop: a client keeps one token and one connection
    # pool, and was written for one thread.
    client = katalog_client()
    extras_client = katalog_client()

    stop = threading.Event()

    def _handle_sigterm(signum: int, _frame: object) -> None:
        log.info("packager.signal", signum=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    threading.Thread(target=_sweep, args=(cfg.old_package_grace_seconds, cfg.work_root),
                     daemon=True, name="packager-sweep").start()

    worker_thread = threading.Thread(
        target=run_worker,
        kwargs={
            "client": client,
            "brokers": cfg.kafka_brokers,
            "group_id": cfg.kafka_group_id,
            "consume_topic": cfg.consume_topic,
            "security_protocol": cfg.kafka_security_protocol,
            "error_sleep": cfg.error_sleep_seconds,
            "stop": stop,
            "options": cfg.package_options(),
        },
        daemon=True,
        name="packager-worker",
    )
    worker_thread.start()

    # The extras' consumer, on its own thread and in its own group, so a
    # long film's run never holds a trailer up: at most one item run and
    # one extra run at a time per pod.
    extras_thread = threading.Thread(
        target=run_extras_worker,
        kwargs={
            "client": extras_client,
            "brokers": cfg.kafka_brokers,
            "group_id": cfg.extras_group_id,
            "consume_topic": cfg.extras_consume_topic,
            "security_protocol": cfg.kafka_security_protocol,
            "error_sleep": cfg.error_sleep_seconds,
            "stop": stop,
            "options": cfg.package_options(),
        },
        daemon=True,
        name="packager-extras",
    )
    extras_thread.start()

    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> dict:
        return {"ok": worker_thread.is_alive() and extras_thread.is_alive(),
                "extras": extras_thread.is_alive()}

    uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None)
    stop.set()
    client.close()
    extras_client.close()
    worker_thread.join(timeout=10)
    extras_thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
