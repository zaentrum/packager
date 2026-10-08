"""The worker records' library block (library v2, contract
platform-library/1) and the v2 packaging-complete calls, without
binaries: what the packager reads from the block, which blocks it refuses
(never falling back to the package store for them), and that only a 2xx
from the catalog counts as taken."""

from __future__ import annotations

import json

import httpx
import pytest

from packager.katalog import (
    ClaimedExtra,
    ClaimedItem,
    KatalogClient,
    LibraryRecordError,
    parse_extra_library,
    parse_item_library,
)

ITEM = "f001aeff-9c18-4183-b51b-51403af2515e"
SOURCE = "0b6c3d2e-1111-4a2b-8c3d-4e5f60718293"
VERSION = "9a2e4f6a-2222-4b3c-9d4e-5f6071829304"
EXTRA = "16aa63f3-3333-4c4d-8e5f-60718293a4b5"
TITLE = "ea886f9b-0d06-4f0f-babb-d2a1162f9b01"
LIB = "/var/lib/katalog"
ITEM_DIR = f"{LIB}/movies/f0/{ITEM}"
QH1 = "sha256:" + "ab" * 32
BASE = "http://catalog.test"


def item_library(**fields: object) -> dict:
    """An item record's library block, as the contract's example has it."""
    return {
        "contract": 1, "root": LIB, "itemDir": ITEM_DIR, "blocked": None,
        "source": {"sourceId": SOURCE, "recorded": False,
                   "recordDir": f"{ITEM_DIR}/sources/{SOURCE}",
                   "libraryPath": "Sintel (2010).mkv", "sizeBytes": 1234567890, "qh1": QH1},
        "inboxDir": f"{LIB}/.work/inbox/{ITEM}",
        "build": {"versionId": VERSION, "stagingDir": f"{LIB}/.work/staging/{VERSION}",
                  "versionDir": f"{ITEM_DIR}/versions/{VERSION}", "createdBy": "katalog-manager",
                  "chapters": [{"startMs": 0, "endMs": 61000, "title": "Opening"}],
                  "chaptersFrom": "original-file",
                  "segments": [{"kind": "credits", "startMs": 828000, "endMs": 888000,
                                "detector": "chapter", "confidence": 0.9, "label": None}]},
        "current": None,
        **fields,
    }


def extra_library(**fields: object) -> dict:
    title_dir = f"{LIB}/movies/ea/{TITLE}"
    return {
        "contract": 1, "itemDir": title_dir,
        "inboxDir": f"{LIB}/.work/inbox/extra-{EXTRA}",
        "stagingDir": f"{LIB}/.work/staging/extra-{EXTRA}",
        "extraDir": f"{title_dir}/extras/{EXTRA}",
        "recorded": False,
        "record": {"kind": "trailer", "title": "Trailer", "localizedTitles": {}, "language": "zxx",
                   "seasonNumber": None, "origin": None, "createdAt": "2026-10-06T08:00:00Z",
                   "createdBy": "katalog-manager/api"},
        "original": {"name": "trailer.mov", "sizeBytes": 123456789, "qh1": QH1},
        **fields,
    }


def item_record(**fields: object) -> dict:
    return {"id": ITEM, "type": "movie", "title": "Sintel", "year": 2010, "durationMs": 888000,
            "path": f"{LIB}/.work/incoming/Sintel (2010).mkv", **fields}


# -------------------------------------------------------------------- items

def test_an_item_record_without_a_library_block_is_legacy() -> None:
    for body in (item_record(), item_record(library=None)):
        item = ClaimedItem.from_json(body)
        assert item.library is None and item.library_error is None


def test_an_items_library_block() -> None:
    item = ClaimedItem.from_json(item_record(
        library=item_library(current={"versionId": "x", "dir": "/d"}),
        subtitleFiles=[{"id": "a1b2", "path": f"{LIB}/.work/incoming/Sintel (2010).en.srt",
                        "language": "eng", "label": "English"}]))
    assert item.library_error is None
    lib = item.library
    assert (lib.root, lib.item_dir, lib.inbox_dir) == (LIB, ITEM_DIR, f"{LIB}/.work/inbox/{ITEM}")
    assert lib.source.source_id == SOURCE and lib.source.recorded is False
    assert lib.source.record_dir == f"{ITEM_DIR}/sources/{SOURCE}"
    assert (lib.source.library_path, lib.source.size_bytes, lib.source.qh1) == (
        "Sintel (2010).mkv", 1234567890, QH1)
    assert lib.build.version_id == VERSION
    assert lib.build.staging_dir == f"{LIB}/.work/staging/{VERSION}"
    assert lib.build.version_dir == f"{ITEM_DIR}/versions/{VERSION}"
    assert lib.build.created_by == "katalog-manager"
    assert lib.build.chapters == [{"startMs": 0, "endMs": 61000, "title": "Opening"}]
    assert lib.build.chapters_from == "original-file"
    assert [s["kind"] for s in lib.build.segments] == ["credits"]
    assert lib.current == {"versionId": "x", "dir": "/d"}
    # The subtitle files keep the catalog's id of each.
    assert item.subtitle_files[0]["id"] == "a1b2"


def test_a_block_without_marks_or_fixity() -> None:
    lib = parse_item_library(item_library(
        build={**item_library()["build"], "chapters": None, "chaptersFrom": None,
               "segments": None},
        source={**item_library()["source"], "sizeBytes": None, "qh1": None, "recorded": True}))
    assert (lib.build.chapters, lib.build.chapters_from, lib.build.segments) == ([], None, [])
    assert (lib.source.size_bytes, lib.source.qh1, lib.source.recorded) == (None, None, True)


def test_a_blocked_item_fails_with_the_catalogs_words() -> None:
    reason = "an episode needs its season and episode numbers before it is recorded"
    # Nothing else of the block is read: it may be incomplete.
    item = ClaimedItem.from_json(item_record(library={"contract": 1, "blocked": reason}))
    assert item.library is None and item.library_error == reason


@pytest.mark.parametrize(("change", "says"), [
    ({"contract": 2}, "library contract 2 is not supported"),
    ({"contract": True}, "library contract True is not supported"),
    ({"contract": None}, "library contract None"),
    ({"itemDir": "movies/f0/x"}, "library.itemDir is not an absolute path"),
    ({"itemDir": f"{ITEM_DIR}/"}, "library.itemDir is not an absolute path"),
    ({"itemDir": f"{LIB}/movies/../{ITEM}"}, "library.itemDir is not an absolute path"),
    ({"itemDir": f"/srv/movies/f0/{ITEM}"}, "is not under /var/lib/katalog"),
    ({"inboxDir": None}, "library.inboxDir is not an absolute path"),
    ({"source": None}, "library.source is not an object"),
    ({"build": []}, "library.build is not an object"),
    ({"blocked": ""}, "library.blocked is not a reason"),
    ({"blocked": 7}, "library.blocked is not a reason"),
])
def test_a_library_block_that_cant_be_worked_from(change: dict, says: str) -> None:
    item = ClaimedItem.from_json(item_record(library=item_library(**change)))
    assert item.library is None
    assert item.library_error is not None and says in item.library_error


def test_a_build_without_a_mode_is_one_from_before_the_modes() -> None:
    lib = parse_item_library(item_library())
    assert (lib.build.mode, lib.build.original_name) == (None, None)


@pytest.mark.parametrize(("mode", "name"), [
    ("establish", "original.mkv"), ("takein", "original.mkv"), ("takein", "original-2.m2ts"),
    ("add", None), ("repackage", None)])
def test_a_builds_mode_and_its_originals_name(mode: str, name: str | None) -> None:
    lib = parse_item_library(item_library(
        build={**item_library()["build"], "mode": mode, "originalName": name}))
    assert (lib.build.mode, lib.build.original_name) == (mode, name)


@pytest.mark.parametrize("mode", ["add", "repackage"])
def test_a_mode_that_moves_no_original_takes_no_name_for_it(mode: str) -> None:
    # The original stays where it is: a name sent all the same is not used.
    lib = parse_item_library(item_library(
        build={**item_library()["build"], "mode": mode, "originalName": "original.mkv"}))
    assert (lib.build.mode, lib.build.original_name) == (mode, None)


@pytest.mark.parametrize(("change", "says"), [
    ({"mode": "move"}, "library.build.mode is not one of establish, takein, add, repackage: "
                       "'move'"),
    ({"mode": "Establish"}, "library.build.mode is not one of"),
    ({"mode": "establish"}, "library.build.originalName is not the name of an original in its "
                            "version folder (original.<ext>): None"),
    ({"mode": "takein", "originalName": "Sintel (2010).mkv"}, "library.build.originalName"),
    ({"mode": "establish", "originalName": "original.MKV"}, "library.build.originalName"),
    ({"mode": "establish", "originalName": "../original.mkv"}, "library.build.originalName"),
    ({"mode": "establish", "originalName": "original-0.mkv"}, "library.build.originalName"),
    ({"mode": "establish", "originalName": "original"}, "library.build.originalName"),
    ({"mode": "takein", "originalName": "version.json"}, "library.build.originalName"),
])
def test_a_mode_or_an_originals_name_that_cant_be_worked_from(change: dict, says: str) -> None:
    item = ClaimedItem.from_json(item_record(library=item_library(
        build={**item_library()["build"], **change})))
    assert item.library is None
    assert item.library_error is not None and says in item.library_error


@pytest.mark.parametrize(("part", "change", "says"), [
    ("source", {"sourceId": SOURCE.upper()}, "library.source.sourceId is not a lower-case UUID"),
    ("source", {"recordDir": f"{ITEM_DIR}/sources/other"}, "library.source.recordDir"),
    ("source", {"recordDir": f"{LIB}/movies/aa/x/sources/{SOURCE}"}, "library.source.recordDir"),
    ("source", {"sizeBytes": -1}, "library.source.sizeBytes is not a size"),
    ("source", {"sizeBytes": "12"}, "library.source.sizeBytes is not a size"),
    ("source", {"qh1": "ab" * 32}, "library.source.qh1 is not sha256:<hex>"),
    ("build", {"versionId": "../x"}, "library.build.versionId is not a lower-case UUID"),
    ("build", {"versionDir": f"{ITEM_DIR}/versions/{SOURCE}"}, "library.build.versionDir"),
    ("build", {"versionDir": f"{ITEM_DIR}/{VERSION}"}, "library.build.versionDir"),
    ("build", {"stagingDir": f"{LIB}/.work/staging/{SOURCE}"}, "library.build.stagingDir"),
    ("build", {"chapters": "0-61000"}, "library.build.chapters is not a list"),
])
def test_paths_the_contracts_rule_does_not_give_are_refused(
    part: str, change: dict, says: str,
) -> None:
    block = item_library()
    block[part] = {**block[part], **change}
    with pytest.raises(LibraryRecordError, match="^worker record: ") as e:
        parse_item_library(block)
    assert says in str(e.value)


# ------------------------------------------------------------------- extras

def extra_record(**fields: object) -> dict:
    return {"id": EXTRA, "type": "extra", "parentId": TITLE, "parentType": "movie",
            "parentTitle": "Sintel", "kind": "trailer", "title": "Trailer", "language": "zxx",
            "seasonNumber": None, "path": f"{LIB}/.work/extras/sintel/trailer.mov",
            "state": "transcoded", "removedAt": None, **fields}


def test_an_extra_record_without_a_library_block_is_legacy() -> None:
    extra = ClaimedExtra.from_json(EXTRA, extra_record())
    assert extra.library is None and extra.library_error is None


def test_an_extras_library_block() -> None:
    extra = ClaimedExtra.from_json(EXTRA, extra_record(library=extra_library()))
    lib = extra.library
    assert extra.library_error is None
    assert lib.item_dir == f"{LIB}/movies/ea/{TITLE}"
    assert lib.extra_dir == f"{LIB}/movies/ea/{TITLE}/extras/{EXTRA}"
    assert lib.staging_dir == f"{LIB}/.work/staging/extra-{EXTRA}"
    assert lib.inbox_dir == f"{LIB}/.work/inbox/extra-{EXTRA}"
    assert lib.recorded is False
    assert lib.record["kind"] == "trailer" and lib.record["createdBy"] == "katalog-manager/api"
    assert lib.original == {"name": "trailer.mov", "sizeBytes": 123456789, "qh1": QH1}
    assert parse_extra_library(extra_library(original=None), EXTRA).original is None


@pytest.mark.parametrize(("change", "says"), [
    ({"contract": 0}, "library contract 0 is not supported"),
    ({"extraDir": f"{LIB}/movies/ea/{TITLE}/extras/{TITLE}"}, "library.extraDir"),
    ({"extraDir": f"{LIB}/extras/{EXTRA}"}, "library.extraDir"),
    ({"stagingDir": f"{LIB}/.work/staging/{EXTRA}"}, "library.stagingDir"),
    ({"record": None}, "library.record is not an object"),
    ({"original": "trailer.mov"}, "library.original is not an object"),
    ({"original": {"name": "t.mov", "sizeBytes": 1, "qh1": "x"}}, "library.original.qh1"),
])
def test_an_extras_block_that_cant_be_worked_from(change: dict, says: str) -> None:
    extra = ClaimedExtra.from_json(EXTRA, extra_record(library=extra_library(**change)))
    assert extra.library is None
    assert extra.library_error is not None and says in extra.library_error


# ---------------------------------------------------------- the handovers

def _client(handler) -> KatalogClient:
    def with_token(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        return handler(request)

    client = KatalogClient(BASE, f"{BASE}/token", "worker", "not-a-secret")
    client._http = httpx.Client(transport=httpx.MockTransport(with_token))
    return client


@pytest.mark.parametrize(("call", "path", "key"), [
    ("packaging_complete_v2", f"/api/items/{ITEM}/packaging-complete", ITEM),
    ("extra_packaging_complete_v2", f"/api/extras/{EXTRA}/packaging-complete", EXTRA),
])
def test_a_handover_the_catalog_takes(call: str, path: str, key: str) -> None:
    sent: list[tuple[str, str, object]] = []
    answer = {"itemId": ITEM, "versionId": VERSION, "current": True, "superseded": None}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=answer)

    payload = {"layout": "v2", "versionId": VERSION}
    got = getattr(_client(handler), call)(key, payload)
    assert sent == [("POST", path, payload)]
    assert (got.taken, got.status, got.answer, got.error) == (True, 200, answer, None)


@pytest.mark.parametrize(("respond", "status", "says"), [
    (lambda _r: httpx.Response(409, text="stale version: a re-encode started"), 409,
     "the catalog answered 409: stale version: a re-encode started"),
    (lambda _r: httpx.Response(422, json={"error": "broken chain"}), 422,
     'the catalog answered 422: {"error":"broken chain"}'),
    (lambda _r: httpx.Response(404), 404, "the catalog answered 404"),
    (lambda _r: httpx.Response(500, text="boom"), 500, "the catalog answered 500: boom"),
])
def test_a_handover_the_catalog_refuses(respond, status: int, says: str) -> None:
    got = _client(respond).packaging_complete_v2(ITEM, {})
    assert (got.taken, got.status) == (False, status)
    assert got.error is not None and got.error.startswith(says)


@pytest.mark.parametrize(("step", "path"), [
    (None, f"/api/analyze/items/{ITEM}/steps/package"),
    ("takein", f"/api/analyze/items/{ITEM}/steps/takein")])
def test_a_step_is_written_to_its_own_path(step: str | None, path: str) -> None:
    sent: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    client = _client(handler)
    if step is None:
        client.upsert_step(ITEM, "done", details="v=hvc1")
    else:
        client.upsert_step(ITEM, "done", details="v=hvc1", step=step)
    assert sent == [("PUT", path, {"status": "done", "details": "v=hvc1"})]


def test_a_handover_without_an_answer() -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    got = _client(down).extra_packaging_complete_v2(EXTRA, {})
    assert (got.taken, got.status) == (False, None)
    assert got.error == "no answer: no route"
