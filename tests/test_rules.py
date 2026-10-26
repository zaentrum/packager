"""The report of the rules this packager runs with (rules.py): what it
says, as the catalog's compliance check reads it, and how it goes —
against a fake catalog API (an httpx mock transport) that takes it,
refuses it, or is older than the route and answers 404 or 405."""

from __future__ import annotations

import json
import threading

import httpx
import pytest
from structlog.testing import capture_logs

from packager import rules
from packager.config import Config
from packager.katalog import KatalogClient
from packager.rules import RulesReporter, image_subtitle_forms, image_version, rules_payload

BASE = "http://catalog.test"


def options(**kw: object):
    """The options of a deployment that sets nothing but its credentials and
    what kw names: every other value its default."""
    return Config(katalog_api_url=BASE, oidc_token_url=f"{BASE}/token", oidc_client_id="worker",
                  oidc_client_secret="not-a-secret", **kw).package_options()  # type: ignore[arg-type]


def test_the_payload_is_the_catalogs_contract() -> None:
    # a 5.1 companion in E-AC-3, everything else the defaults
    got = rules_payload(options(surround_audio="eac3"), "3f2a9c1",
                        image_subtitles=["pgs", "vobsub"])
    assert got == {"version": "3f2a9c1", "rules": {
        "surroundAudio": "eac3", "surroundLayout": "5.1", "surroundBitrate": "448k",
        "stereoBitrate": "192k", "imageSubtitles": ["pgs", "vobsub"]}}


def test_the_layout_is_the_one_the_companion_is_made_in() -> None:
    # the report names the layout the encode asks ffmpeg for, and a source
    # track of it in the target codec is copied as it is
    from packager import packager

    layout = rules_payload(options(surround_audio="eac3"), None)["rules"]["surroundLayout"]
    assert layout == packager.SURROUND_LAYOUT
    assert f"channel_layouts={layout}(side)|{layout}" in packager._SURROUND_FORMAT
    streams = [{"index": 1, "codec_name": "eac3", "channels": packager.SURROUND_CHANNELS},
               {"index": 2, "codec_name": "truehd", "channels": 8, "tags": {"language": "fre"}}]
    plan = packager._surround_plan(streams, {0, 1}, options(surround_audio="eac3"))
    assert [t.mode for t in plan] == ["copy", "encode"]


def test_the_payload_says_the_values_this_process_runs_with() -> None:
    # the platform's default is no companion; a bit rate set is the one sent
    got = rules_payload(options(surround_bitrate="640k"), None, image_subtitles=["pgs"])["rules"]
    assert got == {"surroundAudio": "off", "surroundLayout": "5.1", "surroundBitrate": "640k",
                   "stereoBitrate": "192k", "imageSubtitles": ["pgs"]}


@pytest.mark.parametrize(("installed", "forms"), [
    ({"mkvmerge", "mkvextract"}, ["pgs", "vobsub"]),
    ({"mkvmerge"}, ["pgs"]),
    (set(), ["pgs"]),
])
def test_the_image_subtitles_are_those_the_code_carries(monkeypatch: pytest.MonkeyPatch,
                                                        installed: set[str],
                                                        forms: list[str]) -> None:
    # PGS is a stream copy; VobSub is mkvextract's, which needs mkvmerge to
    # name the tracks; DVB and XSUB are never carried
    monkeypatch.setattr(rules.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in installed else None)
    assert image_subtitle_forms() == forms
    assert rules_payload(options(), None)["rules"]["imageSubtitles"] == forms


def test_the_version_is_the_images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_VERSION", " v1.4.0 ")
    assert image_version() == "v1.4.0"
    monkeypatch.delenv("IMAGE_VERSION")
    assert image_version() == "0.1.0"  # the installed package's


def rules_logs(logs: list[dict]) -> list[dict]:
    """The reporter's lines of logs (the client logs its token too)."""
    return [e for e in logs if e["event"].startswith("packager.rules.")]


def client_for(handler) -> tuple[KatalogClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        seen.append(request)
        return handler(request)

    client = KatalogClient(BASE, f"{BASE}/token", "worker", "not-a-secret")
    client._http = httpx.Client(transport=httpx.MockTransport(transport))
    return client, seen


def test_the_report_is_put_where_the_catalog_takes_it() -> None:
    body = rules_payload(options(surround_audio="eac3"), "3f2a9c1", image_subtitles=["pgs"])
    client, seen = client_for(lambda _r: httpx.Response(200, json={"worker": "packager"}))
    with capture_logs() as logs:
        assert RulesReporter(client, body).report_once()
    [request] = seen
    assert (request.method, request.url.path) == ("PUT", "/api/analyze/rules/packager")
    assert request.headers["authorization"] == "Bearer t"
    assert json.loads(request.content) == body
    assert [e["event"] for e in rules_logs(logs)] == ["packager.rules.reported"]


@pytest.mark.parametrize("status", [404, 405])
def test_an_older_catalog_is_said_once_and_asked_again(status: int) -> None:
    client, seen = client_for(lambda _r: httpx.Response(status, text="no route"))
    reporter = RulesReporter(client, rules_payload(options(), None, image_subtitles=["pgs"]))
    with capture_logs() as logs:
        assert not reporter.report_once()
        assert not reporter.report_once()
    assert len(seen) == 2  # asked again, every round
    [said] = rules_logs(logs)
    assert (said["event"], said["log_level"]) == ("packager.rules.unsupported", "info")
    assert said["status"] == status


@pytest.mark.parametrize("answer", [httpx.Response(400, text="surroundBitrate is no bit rate"),
                                    httpx.Response(503, text="migration 051 is not applied"),
                                    httpx.ConnectError("down")])
def test_a_failed_report_never_stops_the_worker(answer) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    client, _ = client_for(handler)
    reporter = RulesReporter(client, rules_payload(options(), None, image_subtitles=["pgs"]))
    with capture_logs() as logs:
        assert not reporter.report_once()  # logged, never raised
    assert [e["log_level"] for e in rules_logs(logs)] == ["warning"]


def test_the_report_goes_at_start_and_every_interval() -> None:
    stop = threading.Event()
    calls: list[str] = []

    class Catalog:
        def put_rules(self, worker: str, _payload: dict) -> httpx.Response:
            calls.append(worker)
            if len(calls) == 3:
                stop.set()
            if len(calls) == 2:
                raise RuntimeError("a hiccup")  # the next round goes all the same
            return httpx.Response(200)

    reporter = RulesReporter(Catalog(), rules_payload(options(), None, image_subtitles=["pgs"]),
                             interval=0.01)
    thread = threading.Thread(target=reporter.run, kwargs={"stop": stop})
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert calls == ["packager"] * 3
