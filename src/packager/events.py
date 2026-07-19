"""Kafka wiring for the packager event consumer.

The packager is the TERMINAL stage of the catalog pipeline. It CONSUMES
`stube.catalog.item.transcoded` and produces nothing, so this module
only builds a consumer — no producer.

Shared contract (must match the analyzer + transcoder workers and the
Go hub exactly):

  * Broker list from KAFKA_BROKERS (comma-separated bootstrap servers).
  * security.protocol defaults to PLAINTEXT (the bundled demo broker has
    no TLS); overridable via KAFKA_SECURITY_PROTOCOL.
  * Consumer: group.id per worker, enable.auto.commit=false,
    auto.offset.reset=earliest. The offset is committed by the caller
    ONLY after the item is fully processed, so a crash mid-work
    reprocesses the message (idempotent by the katalog (item_id, step)
    unique index + the pre-work step-status guard).

  * Envelope (JSON value):
        {"eventId": <uuid4 hex>, "itemId": <str>, "type": <str>,
         "step": <str>, "status": <str>, "occurredAt": <RFC3339>,
         "source": <str>}
    Consumers REQUIRE only `itemId`; every other field is tolerated /
    ignored so the schema can grow without a lock-step deploy.
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
from confluent_kafka import Consumer

log = structlog.get_logger(__name__)


def _security_conf(security_protocol: str) -> dict[str, str]:
    """Kafka security settings. When KAFKA_CERT_DIR points at a mounted
    mTLS secret (user.crt/user.key + the CLUSTER CA's ca.crt — the shared
    Strimzi profile), it wins over `security_protocol`: a mounted cert dir
    IS the operator's way of saying "this broker speaks mTLS"."""
    cert_dir = os.environ.get("KAFKA_CERT_DIR", "").strip()
    if cert_dir and os.path.isdir(cert_dir):
        return {
            "security.protocol": "SSL",
            "ssl.ca.location": os.path.join(cert_dir, "ca.crt"),
            "ssl.certificate.location": os.path.join(cert_dir, "user.crt"),
            "ssl.key.location": os.path.join(cert_dir, "user.key"),
        }
    return {"security.protocol": security_protocol}


def build_consumer(
    *,
    brokers: str,
    group_id: str,
    security_protocol: str = "PLAINTEXT",
) -> Consumer:
    """Construct a manual-commit consumer. The caller subscribes and
    drives the poll loop; offsets are committed explicitly only after an
    item is fully processed."""
    return Consumer(
        {
            "bootstrap.servers": brokers,
            "group.id": group_id,
            **_security_conf(security_protocol),
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )


def parse_item_id(raw_value: bytes | str | None) -> str | None:
    """Parse an event envelope and return its itemId, or None when the
    message is malformed / missing itemId. The caller logs a warning and
    commits+skips a None so a poison message can't wedge the partition."""
    if raw_value is None:
        return None
    try:
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8")
        envelope: dict[str, Any] = json.loads(raw_value)
    except (ValueError, UnicodeDecodeError) as e:
        log.warning("event.parse_failed", error=str(e)[:200])
        return None
    if not isinstance(envelope, dict):
        log.warning("event.not_object", value=str(raw_value)[:200])
        return None
    item_id = envelope.get("itemId")
    if not item_id or not isinstance(item_id, str):
        log.warning("event.missing_item_id", envelope=str(envelope)[:200])
        return None
    return item_id
