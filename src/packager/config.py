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

from .packager import PackageOptions


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
    # --- packaging --------------------------------------------------------
    # HLS segment length when the transcoder's renditions.json doesn't say
    # (it records the keyframe interval it encoded with, which wins).
    segment_seconds: int = 6
    # 5.1 companion for >= 6-channel tracks: eac3 | ac3 | off. Off until
    # chino-stream drops the audio-surround group for clients that don't
    # advertise eac3/ac3: a player that can't decode it may still pick it.
    surround_audio: str = "off"
    surround_bitrate: str = "448k"
    # Reference the WebVTT HLS renditions from the master. Off by default:
    # the clients draw sidecar subtitles themselves, and the API routes
    # don't serve hls/sN/ yet (see README "Subtitles").
    hls_subtitles: bool = False
    # Language preference for DEFAULT=YES audio, comma-separated; empty =
    # the order of the packager.language_whitelist setting.
    preferred_languages: str = ""
    # Seconds a package replaced by a new one (a title packaged again)
    # stays on disk for the requests that started on it. Keep it well
    # above the NFS mounts' attribute cache time (acdirmax).
    old_package_grace_seconds: float = 600.0

    @classmethod
    def from_env(cls) -> Config:
        surround = os.environ.get("SURROUND_AUDIO", "off").strip().lower()
        if surround not in ("eac3", "ac3", "off"):
            raise RuntimeError(f"SURROUND_AUDIO must be eac3, ac3 or off (got {surround!r})")
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
            segment_seconds=int(os.environ.get("SEGMENT_SECONDS", "6")),
            surround_audio=surround,
            surround_bitrate=os.environ.get("SURROUND_BITRATE", "448k"),
            hls_subtitles=os.environ.get("HLS_SUBTITLES", "false").strip().lower()
            in ("1", "true", "yes"),
            preferred_languages=os.environ.get("PREFERRED_LANGUAGES", ""),
            old_package_grace_seconds=float(os.environ.get("OLD_PACKAGE_GRACE_SECONDS", "600")),
        )

    def package_options(self) -> PackageOptions:
        return PackageOptions(
            segment_seconds=self.segment_seconds,
            surround_codec=self.surround_audio,
            surround_bitrate=self.surround_bitrate,
            hls_subtitles=self.hls_subtitles,
            preferred_languages=tuple(
                t.strip().lower() for t in self.preferred_languages.split(",") if t.strip()
            ),
            old_package_grace_seconds=self.old_package_grace_seconds,
        )


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"required env var {key} is empty/unset")
    return val
