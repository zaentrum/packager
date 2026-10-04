"""Shape tests for the packager worker — exercises everything that
runs without ffmpeg / shaka-packager being installed.

The full end-to-end packaging path is covered live in the cluster (the
janitor cron + the per-item processing-step rows surface any issue).
These tests catch the easy stuff: API contract drift on the katalog
client, dataclass shape, config validation.
"""

from __future__ import annotations

import os

import pytest

from packager.config import Config
from packager.katalog import ClaimedItem


def test_config_from_env_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KATALOG_API_URL", raising=False)
    with pytest.raises(RuntimeError, match="KATALOG_API_URL"):
        Config.from_env()


def test_config_from_env_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KATALOG_API_URL", "http://katalog-app")
    monkeypatch.setenv("OIDC_TOKEN_URL", "https://sso.example/token")
    monkeypatch.setenv("OIDC_CLIENT_ID", "katalog")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "x" * 32)
    cfg = Config.from_env()
    assert cfg.katalog_api_url == "http://katalog-app"
    # Kafka defaults match the shared contract when env is not set.
    assert cfg.kafka_brokers == "kafka:9092"
    assert cfg.kafka_security_protocol == "PLAINTEXT"
    assert cfg.kafka_group_id == "packager-workers"
    assert cfg.consume_topic == "stube.catalog.item.transcoded"
    # Terminal stage — no downstream event.
    assert cfg.produce_topic == ""
    # Default value retained when env var is not set.
    assert cfg.packages_root == "/var/lib/katalog/packages"


def test_config_kafka_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KATALOG_API_URL", "http://katalog-app")
    monkeypatch.setenv("OIDC_TOKEN_URL", "https://sso.example/token")
    monkeypatch.setenv("OIDC_CLIENT_ID", "katalog")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "x")
    monkeypatch.setenv("KAFKA_BROKERS", "b1:9092,b2:9092")
    monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", "SSL")
    monkeypatch.setenv("KAFKA_GROUP_ID", "packager-alt")
    monkeypatch.setenv("CONSUME_TOPIC", "custom.topic")
    cfg = Config.from_env()
    assert cfg.kafka_brokers == "b1:9092,b2:9092"
    assert cfg.kafka_security_protocol == "SSL"
    assert cfg.kafka_group_id == "packager-alt"
    assert cfg.consume_topic == "custom.topic"


def test_config_packages_root_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KATALOG_API_URL", "http://katalog-app")
    monkeypatch.setenv("OIDC_TOKEN_URL", "https://sso.example/token")
    monkeypatch.setenv("OIDC_CLIENT_ID", "katalog")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "x")
    monkeypatch.setenv("PACKAGES_ROOT", "/mnt/test/packages")
    cfg = Config.from_env()
    assert cfg.packages_root == "/mnt/test/packages"


def test_config_surround_is_off_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off by default: a master with an audio-surround group reaches clients
    # that can't decode it until chino-stream filters it per client.
    monkeypatch.setenv("KATALOG_API_URL", "http://katalog-app")
    monkeypatch.setenv("OIDC_TOKEN_URL", "https://sso.example/token")
    monkeypatch.setenv("OIDC_CLIENT_ID", "katalog")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "x")
    monkeypatch.delenv("SURROUND_AUDIO", raising=False)
    assert Config.from_env().package_options().surround_codec == "off"
    monkeypatch.setenv("SURROUND_AUDIO", "EAC3")
    assert Config.from_env().package_options().surround_codec == "eac3"


def test_claimed_item_from_json_minimum() -> None:
    body = {
        "id": "00111617-5a35-4c0c-afaa-ff9aae094f86",
        "type": "movie",
        "title": "Behind the Mask",
        "year": 2006,
        "durationMs": 5460000,
        "path": "/var/lib/katalog/media/movies/Behind the Mask (2006)/Behind the Mask (2006).mkv",
    }
    item = ClaimedItem.from_json(body)
    assert item.id == body["id"]
    assert item.type == "movie"
    assert item.duration_ms == 5460000
    assert item.path.endswith(".mkv")


def test_claimed_item_from_json_handles_null_year() -> None:
    body = {
        "id": "00000000-0000-0000-0000-000000000000",
        "type": "episode",
        "title": None,
        "year": None,
        "durationMs": None,
        "path": "/var/lib/katalog/media/shows/Foo/Bar.mkv",
    }
    item = ClaimedItem.from_json(body)
    assert item.year is None
    assert item.duration_ms is None
    assert item.title == ""   # None → "" so log fields stay strings


def test_module_layout_importable() -> None:
    # Sanity: every module in the package can be imported without ffmpeg.
    # If any of them grew a hard import on shaka-packager / nvidia-cuda
    # we'd notice here.
    import packager
    import packager.config
    import packager.events
    import packager.katalog
    import packager.packager
    import packager.worker
    assert os.path.dirname(packager.__file__).endswith("packager")
