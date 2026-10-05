"""The extras of a title (trailers, teasers, featurettes), without
binaries: where an extra's package goes, what the startup sweep clears
there, and how package_item packages one (no trickplay, a manifest
naming its title and kind), with stand-ins for its binaries (ffprobe,
the remux, shaka-packager, the trickplay sprites); the extras' topic,
envelope and catalog calls; and the extras loop against a fake broker
and the real KatalogClient talking to a fake catalog through an httpx
mock transport, so the guards read the extra's record exactly as in
production, with package_item replaced by a recorder. The real
packaging runs of extras are in test_package_real.py."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from packager import extras, worker
from packager import packager as pk
from packager.config import Config
from packager.events import is_retry, parse_envelope, parse_extra_id, parse_item_id
from packager.katalog import ClaimedExtra, KatalogClient

EXTRA = "1b5c2a8e-0000-4000-8000-0000000000e1"
PARENT = "c0ffee00-0000-4000-8000-000000000007"
BASE = "http://catalog.test"

MASTER = (
    "#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS\n"
    '#EXT-X-MEDIA:TYPE=AUDIO,URI="a0/playlist.m3u8",GROUP-ID="audio",NAME="English",DEFAULT=YES\n'
    '#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1.64001f,mp4a.40.2",AUDIO="audio"\n'
    "v0/playlist.m3u8\n"
    '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="v0/iframes.m3u8"\n'
)
MEDIA = (
    '#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MAP:URI="init.mp4"\n'
    "#EXTINF:6.000,\nseg-00001.m4s\n#EXTINF:2.000,\nseg-00002.m4s\n#EXT-X-ENDLIST\n"
)
IFRAMES = MEDIA.replace("#EXTINF:6.000,\n", "#EXTINF:6.000,\n#EXT-X-BYTERANGE:9@0\n")


def _write_hls(root: Path, tag: str) -> None:
    """A small whole HLS tree (one video and one audio rendition) under
    root/hls; every file says which run wrote it."""
    for rel, text in {"master.m3u8": MASTER, "v0/playlist.m3u8": MEDIA,
                      "v0/iframes.m3u8": IFRAMES, "a0/playlist.m3u8": MEDIA}.items():
        (root / "hls" / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "hls" / rel).write_text(f"{text}## {tag}\n")
    for folder in ("v0", "a0"):
        for name in ("init.mp4", "seg-00001.m4s", "seg-00002.m4s"):
            (root / "hls" / folder / name).write_text(tag)


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _stamp(ago: timedelta) -> str:
    return (datetime.now(UTC) - ago).strftime(pk._STAMP)


# --------------------------------------------------------- the package folder

def test_an_extra_has_a_package_category_of_its_own(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path)
    root = pk._item_root(EXTRA, "extra")
    assert root == tmp_path / "extras" / "1b" / EXTRA
    # No title's category: never inside a title's folder, even were the
    # extra's id ever its title's.
    titles = {pk._item_root(EXTRA, t).parent.parent
              for t in ("movie", "episode", "series", "season", "track", None)}
    assert root.parent.parent not in titles
    assert pk._item_root(EXTRA, "Extra") == root
    # The status probe, which knows only the id, finds it there.
    root.mkdir(parents=True)
    assert pk._find_existing_root(EXTRA) == root


def test_the_startup_sweep_covers_the_extras(tmp_path: Path, monkeypatch) -> None:
    # A run that died while packaging an extra, and the package a swap
    # replaced an hour ago, whose timer died with the process.
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path)
    root = tmp_path / "extras" / "1b" / EXTRA
    _write_hls(root, "live")
    (root / pk.MANIFEST_FILE).write_text("{}")
    (root / ".complete").write_text("x")
    (root / pk.STAGING_DIR / "hls").mkdir(parents=True)
    (root / pk.STAGING_DIR / pk.SENTINEL).write_text("{}")
    two_days_ago = time.time() - 2 * 86400
    os.utime(root / pk.STAGING_DIR / pk.SENTINEL, (two_days_ago, two_days_ago))
    (root / f"hls.old-{_stamp(timedelta(hours=1))}").mkdir()
    live = _tree(root / "hls")

    assert pk.sweep_leftovers(600) == 2
    assert sorted(p.name for p in root.iterdir()) == [".complete", "hls", pk.MANIFEST_FILE]
    assert _tree(root / "hls") == live


@pytest.mark.parametrize("item_type", ["movie", "episode", None])
def test_a_titles_category_is_as_it_was(item_type, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path)
    category = {"movie": "movies", "episode": "shows", None: "other"}[item_type]
    assert pk._item_root(PARENT, item_type) == tmp_path / category / "c0" / PARENT


# ------------------------------------------------- package_item, no binaries

class Binaries:
    """Stand-ins for what package_item runs: the probe of a 33 s 720p
    H.264 clip with one English AAC track, a remux that writes nothing, a
    shaka run that writes a small whole HLS tree, and a trickplay run
    that writes one sprite. `trickplay` lists the folders it ran for."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path / "packages"
        self.trickplay: list[Path] = []
        self.source = tmp_path / "extras" / "clip" / "trailer.mp4"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"x")
        monkeypatch.setattr(pk, "PACKAGES_ROOT", self.root)
        monkeypatch.setattr(pk, "_packager_version", lambda: "packager-test")
        monkeypatch.setattr(pk, "_ffprobe", lambda _path: pk._Probe(
            container="mov,mp4,m4a,3gp,3g2,mj2", duration_ms=33_000,
            video={"index": 0, "codec_name": "h264", "width": 1280, "height": 720},
            audio=[{"index": 1, "codec_name": "aac", "channels": 2,
                    "tags": {"language": "eng"}, "disposition": {"default": 1}}],
            subtitles=[], video_index=0))
        monkeypatch.setattr(pk, "_prepare_source", self._prepare)
        monkeypatch.setattr(pk, "_run_shaka_packager", self._shaka)
        monkeypatch.setattr(pk, "_generate_trickplay", self._sprites)

    @staticmethod
    def _prepare(_src, _probe, tmpdir: Path, **_kw):
        audio = [{"idx": 0, "codec": "aac", "language": "eng", "title": "", "channels": 2,
                  "default": True, "visible": True}]
        return tmpdir / "transmux.mp4", audio, []

    @staticmethod
    def _shaka(_primary, _videos, audio_meta, surround_meta, out_root: Path, **_kw):
        _write_hls(out_root, "new")
        video = [{"id": "v0", "dir": "hls/v0", "codec": "avc1.64001f", "width": 1280,
                  "height": 720, "label": "720p", "encoder": "copy"}]
        audio = [{**audio_meta[0], "id": "a0", "dir": "hls/a0", "codec": "mp4a.40.2",
                  "group": "audio", "name": "English"}]
        return video, audio, surround_meta

    def _sprites(self, _src, _probe, out_dir: Path):
        self.trickplay.append(out_dir)
        out_dir.mkdir(parents=True)
        (out_dir / "thumbnails.vtt").write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:10.000\nsprite-0000.jpg#xywh=0,0,320,180\n")
        (out_dir / "sprite-0000.jpg").write_text("x")
        return {"vttPath": "trickplay/thumbnails.vtt", "spritePattern": "trickplay/sprite-%04d.jpg",
                "intervalSec": 10, "thumbWidth": 320, "thumbHeight": 180,
                "gridCols": 10, "gridRows": 10}


@pytest.fixture
def binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Binaries:
    return Binaries(tmp_path, monkeypatch)


def _on_disk(root: Path) -> dict:
    return json.loads((root / pk.MANIFEST_FILE).read_text())


def test_an_extra_is_packaged_without_trickplay(binaries: Binaries) -> None:
    manifest = pk.package_item(EXTRA, str(binaries.source), "extra", title="Trailer",
                               trickplay=False)
    root = binaries.root / "extras" / "1b" / EXTRA
    assert binaries.trickplay == []
    assert "trickplay" not in manifest
    assert _on_disk(root) == manifest
    assert sorted(p.name for p in root.iterdir()) == [".complete", "hls", pk.MANIFEST_FILE]


def test_a_title_has_its_trickplay_as_before(binaries: Binaries) -> None:
    # The default: the sprites, run against the source, into the staging
    # folder, and swapped in with the package.
    manifest = pk.package_item(PARENT, str(binaries.source), "movie", title="Clip")
    root = binaries.root / "movies" / "c0" / PARENT
    assert binaries.trickplay == [root / pk.STAGING_DIR / "trickplay"]
    assert manifest["trickplay"]["vttPath"] == "trickplay/thumbnails.vtt"
    assert (root / "trickplay" / "sprite-0000.jpg").exists()
    assert _on_disk(root) == manifest


def _package_extra(binaries: Binaries, **kw) -> dict:
    return pk.package_item(
        EXTRA, str(binaries.source), "extra", title="Trailer", trickplay=False,
        manifest_extra={"parentId": PARENT, "extraKind": "trailer"}, **kw)


def test_an_extras_manifest_names_its_parent_and_its_kind(binaries: Binaries) -> None:
    manifest = _package_extra(binaries)
    assert _on_disk(binaries.root / "extras" / "1b" / EXTRA) == manifest
    assert {k: manifest[k] for k in ("version", "itemId", "type", "parentId", "extraKind",
                                     "title", "year", "tmdbId", "durationMs")} == {
        "version": 2, "itemId": EXTRA, "type": "extra", "parentId": PARENT,
        "extraKind": "trailer", "title": "Trailer", "year": None, "tmdbId": None,
        "durationMs": 33_000,
    }
    # A title's keys, with parentId and extraKind and without trickplay.
    title = pk.package_item(PARENT, str(binaries.source), "movie", title="Clip")
    assert set(manifest) == set(title) - {"trickplay"} | {"parentId", "extraKind"}
    assert manifest["hls"] == {"master": "hls/master.m3u8", "segmentSeconds": 6,
                               "audioGroups": ["audio"], "subtitleGroup": None}
    assert [v["id"] for v in manifest["renditions"]["video"]] == ["v0"]
    assert manifest["renditions"]["audioSurround"] == [] and manifest["subtitles"] == []


@pytest.mark.parametrize("key", ["title", "type", "renditions", "hls"])
def test_manifest_extra_never_replaces_what_the_packager_writes(
    binaries: Binaries, key: str,
) -> None:
    # The run fails as any other does: the live package stays as it was.
    _package_extra(binaries)
    root = binaries.root / "extras" / "1b" / EXTRA
    live = _tree(root)
    with pytest.raises(pk.PackageError, match=f"manifest_extra would replace {key}"):
        pk.package_item(EXTRA, str(binaries.source), "extra", trickplay=False,
                        manifest_extra={"parentId": PARENT, key: "x"})
    assert {rel: text for rel, text in _tree(root).items() if rel != ".failed"} == live
    assert "manifest_extra" in json.loads((root / ".failed").read_text())["error"]
    assert not (root / pk.STAGING_DIR).exists()


def test_a_title_packaged_again_leaves_its_extras_alone(binaries: Binaries) -> None:
    # The title, then its trailer, then the title again (a re-encode): the
    # swap retires what the title's new manifest doesn't name, in the
    # title's folder only. The trailer is in none of it.
    title_root = binaries.root / "movies" / "c0" / PARENT
    extra_root = binaries.root / "extras" / "1b" / EXTRA
    pk.package_item(PARENT, str(binaries.source), "movie", title="Clip")
    title_before = _tree(title_root)
    _package_extra(binaries)
    assert _tree(title_root) == title_before
    extra_before = _tree(extra_root)

    pk.package_item(PARENT, str(binaries.source), "movie", title="Clip",
                    options=pk.PackageOptions(old_package_grace_seconds=600))
    assert _tree(extra_root) == extra_before
    replaced = sorted(p.name.split(".old-")[0] for p in title_root.iterdir() if ".old-" in p.name)
    assert replaced == ["hls", "trickplay"]
    assert sorted(p.relative_to(binaries.root).as_posix()
                  for p in binaries.root.glob("*/*/*")) == [
        f"extras/1b/{EXTRA}", f"movies/c0/{PARENT}"]


# ------------------------------------------------------------------- config

def _env(monkeypatch: pytest.MonkeyPatch, **env: str) -> Config:
    for key, value in {"KATALOG_API_URL": "http://katalog-app",
                       "OIDC_TOKEN_URL": "https://sso.example/token",
                       "OIDC_CLIENT_ID": "katalog", "OIDC_CLIENT_SECRET": "x", **env}.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


def test_the_extras_consumer_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAFKA_TOPIC_PREFIX", raising=False)
    monkeypatch.delenv("EXTRAS_GROUP_ID", raising=False)
    cfg = _env(monkeypatch)
    assert cfg.extras_consume_topic == "stube.catalog.extra.transcoded"
    assert cfg.extras_group_id == "packager-extras"
    # The item consumer is as it was.
    assert (cfg.consume_topic, cfg.kafka_group_id) == (
        "stube.catalog.item.transcoded", "packager-workers")


@pytest.mark.parametrize(("prefix", "topic"), [
    ("zaentrum-demo.", "zaentrum-demo.catalog.extra.transcoded"),
    ("tenant", "tenant.catalog.extra.transcoded"),       # the dot added, as the catalog does
    ("  ", "stube.catalog.extra.transcoded"),             # blank: the default
])
def test_the_extras_topic_is_the_tenants(
    monkeypatch: pytest.MonkeyPatch, prefix: str, topic: str,
) -> None:
    cfg = _env(monkeypatch, KAFKA_TOPIC_PREFIX=prefix, EXTRAS_GROUP_ID="packager-extras-b")
    assert cfg.extras_consume_topic == topic
    assert cfg.extras_group_id == "packager-extras-b"


# ----------------------------------------------------------------- envelope

def transcoded(**fields: object) -> dict:
    """A `catalog.extra.transcoded` envelope as the transcoder sends it."""
    return {"eventId": "9f2b", "extraId": EXTRA, "parentId": PARENT, "type": "extra",
            "kind": "trailer", "step": "package", "status": "queued",
            "occurredAt": "2026-10-06T08:00:00Z", "source": "transcoder", **fields}


def test_the_extra_id_is_read_from_the_event() -> None:
    assert parse_extra_id(json.dumps(transcoded()).encode()) == EXTRA
    assert parse_extra_id(json.dumps(transcoded())) == EXTRA


@pytest.mark.parametrize("raw", [
    None, b"", b"not json", b"[]", json.dumps({"itemId": PARENT}).encode(),
    json.dumps(transcoded(extraId=None)).encode(),
    # It names folders and a URL path: a lower-case UUID, whole, or nothing.
    json.dumps(transcoded(extraId="../../etc")).encode(),
    json.dumps(transcoded(extraId=EXTRA.upper())).encode(),
    json.dumps(transcoded(extraId="{" + EXTRA + "}")).encode(),
    json.dumps(transcoded(extraId=EXTRA + "/x")).encode(),
    json.dumps(transcoded(extraId=EXTRA + "\n")).encode(),
    json.dumps(transcoded(extraId=7)).encode(),
])
def test_a_malformed_extras_event_has_no_extra_id(raw) -> None:
    assert parse_extra_id(raw) is None


def test_the_item_loop_skips_an_extras_event() -> None:
    # No itemId, on purpose: an item consumer pointed at the extras topic
    # by mistake finds nothing to run.
    assert parse_item_id(json.dumps(transcoded()).encode()) is None


def test_an_extras_retry_is_marked() -> None:
    assert is_retry(parse_envelope(json.dumps(transcoded(status="retry", source="retry"))))
    assert not is_retry(parse_envelope(json.dumps(transcoded())))


# ------------------------------------------------------------ catalog client

def record(state: str = "transcoded", **fields: object) -> dict:
    """An extra's worker record, as GET /api/analyze/extras/{id} answers."""
    return {"id": EXTRA, "type": "extra", "parentId": PARENT, "parentType": "movie",
            "parentTitle": "Clip", "kind": "trailer", "title": "Trailer", "language": "en",
            "seasonNumber": None, "path": "/var/lib/katalog/extras/clip/trailer.mov",
            "state": state, **fields}


def _client(handler) -> KatalogClient:
    def with_token(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        return handler(request)

    client = KatalogClient(BASE, f"{BASE}/token", "worker", "not-a-secret")
    client._http = httpx.Client(transport=httpx.MockTransport(with_token))
    return client


def test_an_extras_record() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json=record("READY", id="someone-else"))

    extra = _client(handler).get_extra(EXTRA)
    assert seen == [f"GET /api/analyze/extras/{EXTRA}"]
    # The id the request named, whatever the body says: it names folders.
    assert extra == ClaimedExtra(id=EXTRA, parent_id=PARENT, kind="trailer", title="Trailer",
                                 path="/var/lib/katalog/extras/clip/trailer.mov", state="ready",
                                 parent_title="Clip", removed=False)


def test_an_extra_that_is_unknown_or_removed() -> None:
    assert _client(lambda _r: httpx.Response(404)).get_extra(EXTRA) is None
    removed = _client(lambda _r: httpx.Response(
        200, json=record(removedAt="2026-10-05T08:00:00Z"))).get_extra(EXTRA)
    assert removed is not None and removed.removed
    with pytest.raises(httpx.HTTPStatusError):
        _client(lambda _r: httpx.Response(503)).get_extra(EXTRA)


def test_an_extras_package_step() -> None:
    writes: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        writes.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(500 if len(writes) == 2 else 200, json={})

    client = _client(handler)
    client.upsert_extra_step(EXTRA, "in_progress")
    client.upsert_extra_step(EXTRA, "failed", error="x" * 600)   # a 500, swallowed
    client.upsert_extra_step(EXTRA, "done", details="v=avc1.64001f a=1 subs=0 dur_s=1.2")
    step = f"/api/analyze/extras/{EXTRA}/steps/package"
    assert writes == [
        ("PUT", step, {"status": "in_progress"}),
        ("PUT", step, {"status": "failed", "error": "x" * 500}),
        ("PUT", step, {"status": "done", "details": "v=avc1.64001f a=1 subs=0 dur_s=1.2"}),
    ]

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    _client(down).upsert_extra_step(EXTRA, "done")   # logged, not raised


def test_an_extras_packaging_complete() -> None:
    sent: list[tuple[str, str, object]] = []
    answer = {"extraId": EXTRA, "itemId": PARENT, "packaged": True, "durationMs": 33_000}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=answer)

    manifest = {"version": 2, "itemId": EXTRA, "type": "extra", "parentId": PARENT,
                "source": {"codec": "h264"}}
    assert _client(handler).extra_packaging_complete(EXTRA, manifest) == answer
    assert sent == [("POST", f"/api/extras/{EXTRA}/packaging-complete", manifest)]


@pytest.mark.parametrize(("respond", "expected"), [
    (lambda _r: httpx.Response(500, text="boom"), None),
    (lambda _r: httpx.Response(404), None),           # removed while it packaged
    (lambda _r: httpx.Response(200, text="ok"), {}),  # taken, without a JSON answer
])
def test_an_extras_packaging_complete_the_catalog_did_not_answer(respond, expected) -> None:
    assert _client(respond).extra_packaging_complete(EXTRA, {}) == expected


def test_an_extras_packaging_complete_without_a_catalog() -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    assert _client(down).extra_packaging_complete(EXTRA, {}) is None


# ----------------------------------------------------------------- the loop

TOPIC = "stube.catalog.extra.transcoded"
STEP = f"/api/analyze/extras/{EXTRA}/steps/package"
COMPLETE = f"/api/extras/{EXTRA}/packaging-complete"
SOURCE = {"codec": "h264", "width": 1280, "height": 720, "durationMs": 33_000,
          "bitRate": 2_400_000}
MANIFEST = {"version": 2, "itemId": EXTRA, "type": "extra", "parentId": PARENT,
            "extraKind": "trailer", "title": "Trailer", "durationMs": 33_000,
            "renditions": {"video": [{"id": "v0", "codec": "avc1.64001f"},
                                     {"id": "v1", "codec": "avc1.64001e"}],
                           "audio": [{"id": "a0"}], "audioSurround": []},
            "subtitles": []}


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
        self.built: dict = {}
        self.topics: list[str] = []
        self.committed: list[int] = []

    def subscribe(self, topics: list[str]) -> None:
        self.topics = topics

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
    """The catalog's extras worker protocol: the record, the settings, the
    step and packaging-complete. `state` None: the catalog answers 404."""

    def __init__(self, state: str | None, *, complete: int = 200, **fields: object) -> None:
        self.record = None if state is None else record(state, **fields)
        self.complete = complete
        self.fail_reads = False
        self.reads: list[str] = []
        self.writes: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            self.reads.append(path)
            if path == "/api/settings":
                return httpx.Response(200, json={
                    "packager.language_whitelist": {"valueText": "en,de"}})
            if self.fail_reads:
                return httpx.Response(503)
            if path == f"/api/analyze/extras/{EXTRA}" and self.record is not None:
                return httpx.Response(200, json=self.record)
            return httpx.Response(404)
        self.writes.append((request.method, path, json.loads(request.content or b"null")))
        if path == COMPLETE:
            return httpx.Response(self.complete, json={
                "extraId": EXTRA, "itemId": PARENT, "packaged": True, "durationMs": 33_000})
        return httpx.Response(200, json={})


def retry(**fields: object) -> dict:
    """The event as the catalog's retry chain sends it again."""
    return transcoded(status="retry", source="retry", **fields)


@pytest.fixture
def extra_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """The extra's source, outside the media root, and the transcoder's
    handoff for it: v0 the source as it is ("file": null), v1 a 480p
    encode."""
    source = tmp_path / "extras" / "clip" / "trailer.mov"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x")
    inbox = tmp_path / "_inbox" / f"extra-{EXTRA}"
    inbox.mkdir(parents=True)
    (inbox / "v1.mkv").write_bytes(b"x")
    (inbox / "renditions.json").write_text(json.dumps({
        "version": 1, "itemId": EXTRA, "segmentSeconds": 6, "keyframes": "source",
        "timestampOffset": 0.0, "source": SOURCE,
        "video": [{"id": "v0", "label": "720p", "file": None, "mode": "copy"},
                  {"id": "v1", "label": "480p", "file": "v1.mkv", "mode": "encode",
                   "encoder": "libx264"}],
    }))
    monkeypatch.setattr(extras, "_INBOX_ROOT", tmp_path / "_inbox")
    # The handoff says all the source block needs; nothing is probed.
    monkeypatch.setattr(worker, "probe_source", lambda _path: pytest.fail("probed"))
    return source, inbox


def run(monkeypatch: pytest.MonkeyPatch, events: list[dict], catalog: Catalog,
        package=None) -> tuple[Broker, list[tuple[tuple, dict]]]:
    stop = threading.Event()
    broker = Broker(events, stop)
    calls: list[tuple[tuple, dict]] = []

    def build_consumer(**kw: object) -> Broker:
        broker.built = kw
        return broker

    def package_item(*args, **kwargs) -> dict:
        calls.append((args, kwargs))
        return package(*args, **kwargs) if package else MANIFEST

    monkeypatch.setattr(extras, "build_consumer", build_consumer)
    monkeypatch.setattr(extras, "package_item", package_item)
    options = pk.PackageOptions(segment_seconds=4)
    extras.run_extras_worker(_client(catalog), "broker.test:9092", "packager-extras", TOPIC,
                             "PLAINTEXT", 0.0, stop, options)
    for _args, kwargs in calls:
        assert kwargs["options"] is options
    return broker, calls


def test_an_extra_is_packaged_and_handed_to_the_catalog(
    monkeypatch: pytest.MonkeyPatch, extra_files,
) -> None:
    source, inbox = extra_files
    catalog = Catalog("transcoded", path=str(source))
    broker, calls = run(monkeypatch, [transcoded()], catalog)
    assert broker.built == {"brokers": "broker.test:9092", "group_id": "packager-extras",
                            "security_protocol": "PLAINTEXT"}
    assert broker.topics == [TOPIC]

    [(args, kwargs)] = calls
    assert args == (EXTRA, str(source))
    assert {k: kwargs[k] for k in ("item_type", "title", "trickplay", "manifest_extra",
                                   "language_whitelist", "keep_original_if_single")} == {
        "item_type": "extra", "title": "Trailer", "trickplay": False,
        "manifest_extra": {"parentId": PARENT, "extraKind": "trailer"},
        "language_whitelist": ["en", "de"], "keep_original_if_single": True,
    }
    assert [(v.id, v.path) for v in kwargs["inputs"].video] == [
        ("v0", source), ("v1", inbox / "v1.mkv")]

    # The extra's endpoints only, never an item's; the step is done only
    # once the catalog has the package.
    assert catalog.reads == [f"/api/analyze/extras/{EXTRA}", "/api/settings"]
    assert [(method, path) for method, path, _body in catalog.writes] == [
        ("PUT", STEP), ("POST", COMPLETE), ("PUT", STEP)]
    assert catalog.writes[0][2] == {"status": "in_progress"}
    assert catalog.writes[1][2] == {**MANIFEST, "source": SOURCE}
    done = catalog.writes[2][2]
    assert done["status"] == "done"
    assert re.fullmatch(r"v=avc1\.64001f a=1 subs=0 dur_s=[0-9.]+ vr=2", done["details"])
    assert not inbox.exists()  # the handoff goes once the catalog has the package
    assert broker.committed == [0]


def test_an_extra_the_transcoder_left_as_it_was_is_packaged_from_its_file(
    monkeypatch: pytest.MonkeyPatch, extra_files,
) -> None:
    # A small H.264 trailer: the transcoder's step was not_applicable and
    # it left no handoff. Its file is packaged as it is.
    source, inbox = extra_files
    for f in inbox.iterdir():
        f.unlink()
    inbox.rmdir()
    monkeypatch.setattr(worker, "probe_source", lambda _path: SOURCE)
    catalog = Catalog("transcoded", path=str(source))
    _broker, [(_args, kwargs)] = run(monkeypatch, [transcoded()], catalog)
    assert (kwargs["inputs"].kind, kwargs["inputs"].primary.path) == ("original", source)
    assert catalog.writes[1] == ("POST", COMPLETE, {**MANIFEST, "source": SOURCE})
    assert catalog.writes[-1][2]["status"] == "done"


@pytest.mark.parametrize("state", ["transcoded", "packaging", "pending", "queued",
                                   "transcoding", "failed"])
@pytest.mark.parametrize("make", [transcoded, retry])
def test_an_extra_not_ready_is_packaged(
    monkeypatch: pytest.MonkeyPatch, extra_files, state: str, make,
) -> None:
    # As for an unfinished item: packaging is a run that died (its events
    # share a partition, so no other run is at work), a retry is what a
    # retry is for.
    source, _inbox = extra_files
    catalog = Catalog(state, path=str(source))
    broker, calls = run(monkeypatch, [make()], catalog)
    assert len(calls) == 1
    assert [(path, body["status"]) for _m, path, body in catalog.writes if path == STEP] == [
        (STEP, "in_progress"), (STEP, "done")]
    assert broker.committed == [0]


@pytest.mark.parametrize(("make", "event"), [
    (transcoded, "packager.extra.already_done"),
    (retry, "packager.extra.retry.already_finished"),
])
def test_a_ready_extra_is_not_packaged_again(
    monkeypatch: pytest.MonkeyPatch, extra_files, make, event: str,
) -> None:
    # The transcoder sends transcoded again for an extra past its
    # transcode whenever a trigger reaches it again: a duplicate. The
    # package plays; nothing runs, nothing is written.
    source, inbox = extra_files
    catalog = Catalog("ready", path=str(source))
    with capture_logs() as logs:
        broker, calls = run(monkeypatch, [make()], catalog)
    assert calls == [] and catalog.writes == []
    assert broker.committed == [0]
    assert [(e["event"], e["state"]) for e in logs if e.get("extra_id") == EXTRA] == [
        (event, "ready")]
    assert inbox.exists()


@pytest.mark.parametrize(("state", "fields", "event"), [
    (None, {}, "packager.extra.unresolved"),           # 404: unknown, or removed
    ("transcoded", {"removedAt": "2026-10-05T08:00:00Z"}, "packager.extra.removed_skip"),
    ("missing", {}, "packager.extra.missing_skip"),   # its file is gone
])
@pytest.mark.parametrize("make", [transcoded, retry])
def test_an_extra_that_is_gone_is_skipped(
    monkeypatch: pytest.MonkeyPatch, extra_files, state: str | None, fields: dict,
    event: str, make,
) -> None:
    source, inbox = extra_files
    catalog = Catalog(state, path=str(source), **fields)
    with capture_logs() as logs:
        broker, calls = run(monkeypatch, [make()], catalog)
    assert calls == [] and catalog.writes == []
    assert catalog.reads == [f"/api/analyze/extras/{EXTRA}"]
    assert broker.committed == [0]
    assert event in [e["event"] for e in logs if e.get("extra_id") == EXTRA]
    assert inbox.exists()


def test_a_malformed_event_is_committed_and_skipped(
    monkeypatch: pytest.MonkeyPatch, extra_files,
) -> None:
    source, _inbox = extra_files
    catalog = Catalog("transcoded", path=str(source))
    item_event = {"eventId": "e1", "itemId": PARENT, "type": "movie", "step": "package",
                  "status": "done", "source": "transcoder"}
    broker, calls = run(monkeypatch, [item_event, transcoded(extraId=EXTRA.upper()),
                                      transcoded(extraId=None)], catalog)
    assert calls == [] and catalog.reads == [] and catalog.writes == []
    assert broker.committed == [0, 1, 2]


def test_a_run_that_fails_fails_the_step_and_keeps_the_handoff(
    monkeypatch: pytest.MonkeyPatch, extra_files,
) -> None:
    source, inbox = extra_files
    catalog = Catalog("transcoded", path=str(source))

    def fails(*_args, **_kwargs) -> dict:
        raise pk.PackageError("shaka-packager (media) exited 1: No space left on device")

    broker, _calls = run(monkeypatch, [transcoded()], catalog, package=fails)
    assert catalog.writes == [
        ("PUT", STEP, {"status": "in_progress"}),
        ("PUT", STEP, {"status": "failed",
                       "error": "shaka-packager (media) exited 1: No space left on device"}),
    ]
    assert inbox.exists()
    assert broker.committed == [0]


@pytest.mark.parametrize("status", [500, 404])
def test_a_package_the_catalog_did_not_take_fails_the_step(
    monkeypatch: pytest.MonkeyPatch, extra_files, status: int,
) -> None:
    # On disk, but unknown to the catalog: nothing would play it, nothing
    # would repair it. Failed, so the catalog's retry runs the chain again.
    source, inbox = extra_files
    catalog = Catalog("transcoded", path=str(source), complete=status)
    run(monkeypatch, [transcoded()], catalog)
    assert [(m, p, b.get("status")) for m, p, b in catalog.writes] == [
        ("PUT", STEP, "in_progress"), ("POST", COMPLETE, None), ("PUT", STEP, "failed")]
    assert "packaging-complete" in catalog.writes[-1][2]["error"]
    assert inbox.exists()


@pytest.mark.parametrize(("handoff", "error"), [
    # The handoff's v0 is the extra's file ("file": null), which is gone.
    (True, "transcoder handoff: top rendition missing: "),
    # No handoff: the file itself is what is packaged.
    (False, "source file missing: "),
])
def test_an_extra_whose_file_is_gone_fails_without_a_run(
    monkeypatch: pytest.MonkeyPatch, extra_files, handoff: bool, error: str,
) -> None:
    _source, inbox = extra_files
    if not handoff:
        for f in inbox.iterdir():
            f.unlink()
        inbox.rmdir()
    gone = "/var/lib/katalog/extras/gone/trailer.mov"
    catalog = Catalog("transcoded", path=gone)
    _broker, calls = run(monkeypatch, [transcoded()], catalog)
    assert calls == []
    assert catalog.writes == [("PUT", STEP, {"status": "failed", "error": error + gone})]


def test_a_handoff_that_cannot_be_read_fails_without_a_run(
    monkeypatch: pytest.MonkeyPatch, extra_files,
) -> None:
    source, inbox = extra_files
    (inbox / "renditions.json").write_text(json.dumps({"version": 9, "video": []}))
    catalog = Catalog("transcoded", path=str(source))
    _broker, calls = run(monkeypatch, [transcoded()], catalog)
    assert calls == []
    [(_m, _p, body)] = catalog.writes
    assert body["status"] == "failed" and body["error"].startswith("transcoder handoff: ")


@pytest.mark.parametrize(("fields", "event", "manifest_extra", "error"), [
    # The record names the title and the kind; the event's are the fallback.
    ({"parentId": None, "kind": None}, {}, {"parentId": PARENT, "extraKind": "trailer"}, None),
    ({"kind": "teaser"}, {}, {"parentId": PARENT, "extraKind": "teaser"}, None),
    ({"parentId": None}, {"parentId": None}, None, "the extra's record has no parentId"),
    ({"path": None}, {}, None, "the extra's record has no path"),
])
def test_what_an_extras_manifest_names_it_after(
    monkeypatch: pytest.MonkeyPatch, extra_files, fields: dict, event: dict,
    manifest_extra: dict | None, error: str | None,
) -> None:
    source, _inbox = extra_files
    catalog = Catalog("transcoded", **{"path": str(source), **fields})
    _broker, calls = run(monkeypatch, [transcoded(**event)], catalog)
    if error is None:
        [(_args, kwargs)] = calls
        assert kwargs["manifest_extra"] == manifest_extra
    else:
        assert calls == []
        assert catalog.writes == [("PUT", STEP, {"status": "failed", "error": error})]


def test_a_catalog_that_does_not_answer_fails_the_step_and_commits(
    monkeypatch: pytest.MonkeyPatch, extra_files,
) -> None:
    source, _inbox = extra_files
    catalog = Catalog("transcoded", path=str(source))
    catalog.fail_reads = True
    broker, calls = run(monkeypatch, [transcoded()], catalog)
    assert calls == []
    [(_m, path, body)] = catalog.writes
    assert path == STEP and body["status"] == "failed"
    assert body["error"].startswith("worker bug: ")
    assert broker.committed == [0]


# --------------------------------------------------------------------- main

def test_main_runs_the_extras_loop_beside_the_items(monkeypatch: pytest.MonkeyPatch) -> None:
    from packager import main

    _env(monkeypatch, KAFKA_TOPIC_PREFIX="zaentrum-demo.",
         CONSUME_TOPIC="zaentrum-demo.catalog.item.transcoded")
    loops: dict[str, dict] = {}

    def loop(name: str):
        def run(**kw: object) -> None:
            loops[name] = kw
            kw["stop"].wait(5)  # type: ignore[attr-defined]
        return run

    ready: list[dict] = []

    def serve(app, **_kw: object) -> None:
        # Both loops at work: the probe says so.
        deadline = time.monotonic() + 5
        while len(loops) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        [readyz] = [r.endpoint for r in app.routes if getattr(r, "path", "") == "/readyz"]
        ready.append(readyz())

    monkeypatch.setattr(main, "_configure_logging", lambda: None)
    monkeypatch.setattr(main.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(main, "sweep_leftovers", lambda *_a, **_kw: 0)
    monkeypatch.setattr(main, "run_worker", loop("items"))
    monkeypatch.setattr(main, "run_extras_worker", loop("extras"))
    monkeypatch.setattr(main.uvicorn, "run", serve)
    assert main.main() == 0

    assert ready == [{"ok": True, "extras": True}]
    items, extra = loops["items"], loops["extras"]
    assert (items["group_id"], items["consume_topic"]) == (
        "packager-workers", "zaentrum-demo.catalog.item.transcoded")
    assert (extra["group_id"], extra["consume_topic"]) == (
        "packager-extras", "zaentrum-demo.catalog.extra.transcoded")
    # A client of its own, the same broker, the same packaging options.
    assert extra["client"] is not items["client"]
    assert (extra["brokers"], extra["options"]) == (items["brokers"], items["options"])
    assert extra["stop"] is items["stop"] and extra["stop"].is_set()
