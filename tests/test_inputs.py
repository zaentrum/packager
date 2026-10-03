"""Reading the transcoder handoff (renditions.json / prepared.mkv / none)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packager.renditions import ContractError, resolve_inputs


def _contract(inbox: Path, video: list[dict], **extra: object) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    body = {"version": 1, "segmentSeconds": 6, "keyframes": "source",
            "timestampOffset": 0.021, "video": video, **extra}
    (inbox / "renditions.json").write_text(json.dumps(body))


def test_no_handoff_packages_the_original(tmp_path: Path) -> None:
    inputs = resolve_inputs(tmp_path / "inbox", "/media/movie.mkv")
    assert inputs.kind == "original"
    assert [(v.id, str(v.path), v.timeline) for v in inputs.video] == [
        ("v0", "/media/movie.mkv", "normalize")]
    assert inputs.segment_seconds is None


def test_legacy_prepared_mkv(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "prepared.mkv").write_bytes(b"x")
    inputs = resolve_inputs(inbox, "/media/movie.mkv")
    assert inputs.kind == "prepared"
    assert inputs.primary.path == inbox / "prepared.mkv"
    assert inputs.primary.timeline == "normalize"  # exactly the old behaviour


def test_contract_with_copied_top_rung(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"x")
    _contract(inbox, [
        {"id": "v0", "file": None, "label": "source", "encoder": "copy"},
        {"id": "v1", "file": "v1.mkv", "label": "720p", "encoder": "h264_nvenc"},
    ])
    (inbox / "v1.mkv").write_bytes(b"x")
    inputs = resolve_inputs(inbox, str(original))
    assert inputs.kind == "contract"
    assert inputs.segment_seconds == 6
    assert inputs.timestamp_offset == pytest.approx(0.021)
    assert [(v.id, v.path.name, v.timeline, v.label) for v in inputs.video] == [
        ("v0", "movie.mkv", "offset", "source"),
        ("v1", "v1.mkv", "keep", "720p"),
    ]


def test_contract_with_encoded_top_rung(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _contract(inbox, [{"id": "v0", "file": "prepared.mkv"}])
    (inbox / "prepared.mkv").write_bytes(b"x")
    inputs = resolve_inputs(inbox, str(tmp_path / "gone.mkv"))
    assert [(v.path.name, v.timeline) for v in inputs.video] == [("prepared.mkv", "keep")]


def test_missing_lower_rung_is_skipped_and_ids_stay_contiguous(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _contract(inbox, [
        {"id": "v0", "file": "prepared.mkv"},
        {"id": "v1", "file": "v1.mkv"},
        {"id": "v2", "file": "v2.mkv"},
    ])
    for name in ("prepared.mkv", "v2.mkv"):
        (inbox / name).write_bytes(b"x")
    inputs = resolve_inputs(inbox, "/media/movie.mkv")
    assert [(v.id, v.path.name) for v in inputs.video] == [("v0", "prepared.mkv"),
                                                            ("v1", "v2.mkv")]


def test_missing_top_rung_is_an_error(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _contract(inbox, [{"id": "v0", "file": "prepared.mkv"}])
    with pytest.raises(ContractError, match="top rendition missing"):
        resolve_inputs(inbox, "/media/movie.mkv")


@pytest.mark.parametrize("body", ["not json", json.dumps({"version": 9, "video": [{}]}),
                                  json.dumps({"version": 1, "video": []})])
def test_unusable_contract_is_an_error(tmp_path: Path, body: str) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "renditions.json").write_text(body)
    with pytest.raises(ContractError):
        resolve_inputs(inbox, "/media/movie.mkv")


def test_contract_file_names_cannot_escape_the_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _contract(inbox, [{"id": "v0", "file": "../../etc/prepared.mkv"}])
    (inbox / "prepared.mkv").write_bytes(b"x")
    assert resolve_inputs(inbox, "/m.mkv").primary.path == inbox / "prepared.mkv"
