"""Runtime configuration. Everything from env vars; defaults are
sized for a single-replica deployment doing a serial packaging sweep.

Packaging is CPU + disk heavy, so the worker runs one item at a time.
The packager is a PURE Kafka event consumer: it subscribes to the
`stube.catalog.item.transcoded` topic and packages whatever item the
transcoder just finished. It is the TERMINAL stage of the pipeline —
it produces no downstream event. Scale up via replicas (the Kafka
consumer group rebalances partitions across them), not batch size."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    katalog_api_url: str
    oidc_token_url: str
    oidc_client_id: str
    oidc_client_secret: str
    # --- Kafka ------------------------------------------------------------
    # Comma-separated bootstrap broker list, e.g. "kafka:9092". The bundled
    # demo broker is PLAINTEXT (no TLS); security_protocol defaults to
    # PLAINTEXT and is overridable for a secured broker.
    kafka_brokers: str = "kafka:9092"
    kafka_security_protocol: str = "PLAINTEXT"
    kafka_group_id: str = "packager-workers"
    # Packager CONSUMES the transcoder's output and produces nothing
    # (terminal stage). produce_topic is retained for env symmetry with
    # the sibling workers but is unused.
    consume_topic: str = "stube.catalog.item.transcoded"
    produce_topic: str = ""
    # --- error backoff ----------------------------------------------------
    # Sleep after an unexpected consumer-loop error before re-polling, so a
    # broker outage doesn't turn into a tight crash loop.
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
            kafka_brokers=os.environ.get("KAFKA_BROKERS", "kafka:9092"),
            kafka_security_protocol=os.environ.get(
                "KAFKA_SECURITY_PROTOCOL", "PLAINTEXT"
            ),
            kafka_group_id=os.environ.get("KAFKA_GROUP_ID", "packager-workers"),
            consume_topic=os.environ.get(
                "CONSUME_TOPIC", "stube.catalog.item.transcoded"
            ),
            produce_topic=os.environ.get("PRODUCE_TOPIC", ""),
            error_sleep_seconds=float(os.environ.get("ERROR_SLEEP_SECONDS", "60")),
            packages_root=os.environ.get("PACKAGES_ROOT", "/var/lib/katalog/packages"),
        )


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"required env var {key} is empty/unset")
    return val
