"""The consumer loop against a fake broker and a fake catalog API: which
`transcoded` events package the item, and which the worker only acks —
a redelivery, or a retry the catalog sent before the step finished.

The real KatalogClient talks to the fake catalog through an httpx mock
transport, so the guard reads the step statuses exactly as it does in
production; the packaging itself (`_process_one`) is replaced by a
recorder — the real packaging runs are in test_package_real.py.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest
from structlog.testing import capture_logs

from packager import events, worker
from packager.events import is_retry, parse_envelope
from packager.katalog import KatalogClient

ITEM = "7a1c0de0-0000-4000-8000-000000000002"
BASE = "http://catalog.test"


class Message:
    """A consumed record, as confluent-kafka hands it to the loop."""

    def __init__(self, value: dict, offset: int) -> None:
        self._value = json.dumps(value).encode()
        self._offset = offset

    def value(self) -> bytes:
        return self._value

    def error(self) -> None:
        return None

    def offset(self) -> int:
        return self._offset


class Broker:
    """One partition for the consumer. Sets `stop` once every message has
    been polled, which ends the loop."""

    def __init__(self, events: list[dict], stop: threading.Event) -> None:
        self.pending = [Message(e, i) for i, e in enumerate(events)]
        self.stop = stop
        self.committed: list[int] = []

    def subscribe(self, _topics: list[str]) -> None:
        pass

    def poll(self, _timeout: float) -> Message | None:
        if not self.pending:
            self.stop.set()
            return None
        return self.pending.pop(0)

    def commit(self, message: Message) -> None:
        self.committed.append(message.offset())

    def close(self) -> None:
        pass


class Catalog:
    """The catalog's worker protocol: item detail, step statuses, writes."""

    def __init__(self, steps: dict[str, str]) -> None:
        self.steps = steps
        self.writes: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        if request.method == "GET" and path == f"/api/analyze/items/{ITEM}":
            return httpx.Response(200, json={
                "id": ITEM, "type": "movie", "title": "Clip", "year": 2020,
                "durationMs": 60_000, "path": "/media/clip.mkv",
            })
        if request.method == "GET" and path == f"/api/analyze/items/{ITEM}/steps":
            return httpx.Response(200, json={"itemId": ITEM, "steps": self.steps})
        self.writes.append((request.method, path, json.loads(request.content or b"null")))
        return httpx.Response(200, json={})


def event(**fields: str) -> dict:
    """A `transcoded` envelope as the transcoder produces it."""
    return {"eventId": "e1", "itemId": ITEM, "type": "movie", "step": "package",
            "status": "done", "occurredAt": "2026-10-04T00:00:00Z", "source": "transcoder",
            **fields}


def retry() -> dict:
    """The same trigger as the catalog's retry sends it again."""
    return event(status="retry", source="retry")


def run(monkeypatch: pytest.MonkeyPatch, events: list[dict],
        steps: dict[str, str]) -> tuple[Broker, Catalog, list[str]]:
    stop = threading.Event()
    broker = Broker(events, stop)
    catalog = Catalog(steps)
    packaged: list[str] = []
    monkeypatch.setattr(worker, "build_consumer", lambda **_kw: broker)
    monkeypatch.setattr(worker, "_process_one",
                        lambda item, _client, _options=None: packaged.append(item.id))
    client = KatalogClient(BASE, f"{BASE}/token", "worker", "not-a-secret")
    client._http = httpx.Client(transport=httpx.MockTransport(catalog))
    worker.run_worker(client, "broker.test:9092", "packager-workers",
                      "stube.catalog.item.transcoded", "PLAINTEXT", 0.0, stop)
    return broker, catalog, packaged


@pytest.mark.parametrize("status", ["done", "not_applicable", "skipped"])
def test_finished_package_is_not_packaged_again(
    monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    broker, catalog, packaged = run(monkeypatch, [event()], {"package": status})
    assert packaged == []
    assert catalog.writes == []
    assert broker.committed == [0]


@pytest.mark.parametrize("status", ["done", "not_applicable", "skipped"])
def test_retry_of_a_finished_package_is_only_acked(
    monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    # The reaper took a long run for dead and the catalog sent the trigger
    # again; the run reported its end before the retry was consumed. The
    # retry packages nothing and writes nothing: one log line.
    with capture_logs() as logs:
        broker, catalog, packaged = run(monkeypatch, [retry()], {"package": status})
    assert packaged == []
    assert catalog.writes == []
    assert broker.committed == [0]
    said = [e for e in logs if e.get("item_id") == ITEM]
    assert [(e["event"], e["status"]) for e in said] == [
        ("packager.retry.already_finished", status)]


@pytest.mark.parametrize("steps", [{}, {"package": "pending"}, {"package": "failed"},
                                   {"package": "in_progress"},
                                   {"transcode": "done", "package": "pending"},
                                   {"transcode": "not_applicable", "package": "pending"},
                                   {"transcode": "skipped", "package": "failed"}])
@pytest.mark.parametrize("make", [event, retry])
def test_unfinished_package_is_packaged(
    monkeypatch: pytest.MonkeyPatch, steps: dict[str, str], make,
) -> None:
    broker, _catalog, packaged = run(monkeypatch, [make()], steps)
    assert packaged == [ITEM]
    assert broker.committed == [0]


@pytest.mark.parametrize("transcode", ["pending", "in_progress", "failed"])
@pytest.mark.parametrize("make", [event, retry])
def test_a_handover_sent_before_its_transcode_was_reset_is_only_acked(
    monkeypatch: pytest.MonkeyPatch, transcode: str, make,
) -> None:
    # A pause left the transcoder's handover unconsumed, and a re-encode
    # reset both steps since: packaging it now would package the old
    # handoff and finish the package step, so the new encode's handover
    # would find it done. It packages nothing and writes nothing.
    with capture_logs() as logs:
        broker, catalog, packaged = run(
            monkeypatch, [make()], {"transcode": transcode, "package": "pending"})
    assert packaged == []
    assert catalog.writes == []
    assert broker.committed == [0]
    said = [e for e in logs if e.get("item_id") == ITEM]
    assert [(e["event"], e["transcode"]) for e in said] == [
        ("packager.item.stale_handover", transcode)]


def test_the_handover_of_the_run_that_finished_the_transcode_is_packaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stale handover first, while the re-encode runs; then the new
    # run's own, once its transcode is done: only that one packages.
    stop = threading.Event()
    broker = Broker([event(), event(eventId="e2")], stop)
    catalog = Catalog({"transcode": "in_progress", "package": "pending"})
    packaged: list[str] = []

    def poll(_timeout: float) -> Message | None:
        if broker.committed == [0]:
            catalog.steps["transcode"] = "done"
        return Broker.poll(broker, _timeout)

    broker.poll = poll  # type: ignore[method-assign]
    monkeypatch.setattr(worker, "build_consumer", lambda **_kw: broker)
    monkeypatch.setattr(worker, "_process_one",
                        lambda item, _client, _options=None: packaged.append(item.id))
    client = KatalogClient(BASE, f"{BASE}/token", "worker", "not-a-secret")
    client._http = httpx.Client(transport=httpx.MockTransport(catalog))
    worker.run_worker(client, "broker.test:9092", "packager-workers",
                      "stube.catalog.item.transcoded", "PLAINTEXT", 0.0, stop)
    assert packaged == [ITEM]
    assert broker.committed == [0, 1]


def test_consumer_keeps_its_partition_through_a_long_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # librdkafka's default (5 min) is shorter than packaging a long film:
    # the broker would hand the uncommitted item to the other replica,
    # which packages it again into the same directory. The interval must
    # outlast the catalog's package timeout (2 h), up to librdkafka's
    # ceiling (24 h).
    seen: dict = {}
    monkeypatch.setattr(events, "Consumer", lambda conf: seen.update(conf))
    events.build_consumer(brokers="broker.test:9092", group_id="packager-workers")
    assert 2 * 3600 * 1000 < seen["max.poll.interval.ms"] <= 86_400_000
    assert seen["enable.auto.commit"] is False


def test_retry_marker() -> None:
    assert is_retry(parse_envelope(json.dumps(retry()).encode()))
    assert not is_retry(parse_envelope(json.dumps(event()).encode()))
    assert parse_envelope(b"not json") == {}
    assert parse_envelope(b"[1, 2]") == {}
    assert parse_envelope(None) == {}
