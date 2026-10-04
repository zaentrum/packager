"""Reading the transcoder handoff (renditions.json / prepared.mkv / none),
and the source block the catalog gets from it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packager import worker
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


# ------------------------------------------------- the catalog's source block
PROBED = {"codec": "h264", "width": 1280, "height": 720, "durationMs": 12_000,
          "bitRate": 2_000_000}


def _probes(monkeypatch: pytest.MonkeyPatch, result: dict) -> list[Path]:
    seen: list[Path] = []
    monkeypatch.setattr(worker, "probe_source", lambda p: seen.append(p) or dict(result))
    return seen


def test_source_block_forwards_the_contract_without_a_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox, original = tmp_path / "inbox", tmp_path / "movie.mkv"
    original.write_bytes(b"x")
    source = {"codec": "mpeg2video", "width": 720, "height": 576, "frameRate": "25/1",
              "hdr": False, "durationMs": 5_400_000, "bitRate": 6_500_000}
    _contract(inbox, [{"id": "v0", "file": "prepared.mkv"}], source=source)
    (inbox / "prepared.mkv").write_bytes(b"x")
    probes = _probes(monkeypatch, PROBED)
    assert worker._source_block(resolve_inputs(inbox, str(original)), str(original)) == source
    assert probes == []


def test_source_block_probes_the_original_for_what_the_contract_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transcoder from before durationMs/bitRate: its codec and size win
    # (it probed the same file), the probe of the ORIGINAL — not of the
    # encoded prepared.mkv — adds the rest.
    inbox, original = tmp_path / "inbox", tmp_path / "movie.mkv"
    original.write_bytes(b"x")
    _contract(inbox, [{"id": "v0", "file": "prepared.mkv"}],
              source={"codec": "vc1", "width": 1920, "height": 1080, "hdr": False})
    (inbox / "prepared.mkv").write_bytes(b"x")
    probes = _probes(monkeypatch, PROBED)
    block = worker._source_block(resolve_inputs(inbox, str(original)), str(original))
    assert block == {"codec": "vc1", "width": 1920, "height": 1080, "hdr": False,
                     "durationMs": 12_000, "bitRate": 2_000_000}
    assert probes == [original]


def test_source_block_takes_a_zero_size_for_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox, original = tmp_path / "inbox", tmp_path / "movie.mkv"
    original.write_bytes(b"x")
    _contract(inbox, [{"id": "v0", "file": "prepared.mkv"}],
              source={"codec": "vc1", "width": 0, "height": 0, "hdr": False,
                      "durationMs": 5_400_000, "bitRate": None})
    (inbox / "prepared.mkv").write_bytes(b"x")
    _probes(monkeypatch, PROBED)
    block = worker._source_block(resolve_inputs(inbox, str(original)), str(original))
    assert block == {"codec": "vc1", "width": 1280, "height": 720, "hdr": False,
                     "durationMs": 5_400_000, "bitRate": 2_000_000}


def test_source_block_without_a_handoff_is_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"x")
    _probes(monkeypatch, PROBED)
    inputs = resolve_inputs(tmp_path / "inbox", str(original))
    assert worker._source_block(inputs, str(original)) == PROBED


def test_source_block_is_empty_when_nothing_is_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gone = str(tmp_path / "gone.mkv")
    probes = _probes(monkeypatch, PROBED)
    assert worker._source_block(resolve_inputs(tmp_path / "inbox", gone), gone) == {}
    assert probes == []
