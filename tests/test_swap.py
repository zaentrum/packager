"""Swapping a staged package into its item folder, without binaries: the
check a staged package passes first, a swap that fails half-way, what a
swap retires, and which replaced packages a run clears."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packager import packager as pk

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
THUMBS = "WEBVTT\n\n00:00:00.000 --> 00:00:10.000\nsprite-0000.jpg#xywh=0,0,320,180\n"


def _write_package(root: Path, tag: str) -> None:
    """A small whole package under root; every file says which one it is."""
    files = {
        "hls/master.m3u8": MASTER, "hls/v0/playlist.m3u8": MEDIA,
        "hls/v0/iframes.m3u8": IFRAMES, "hls/a0/playlist.m3u8": MEDIA,
        "trickplay/thumbnails.vtt": THUMBS,
    }
    body = {rel: f"{text}## {tag}\n" for rel, text in files.items()}
    for folder in ("hls/v0", "hls/a0"):
        for name in ("init.mp4", "seg-00001.m4s", "seg-00002.m4s"):
            body[f"{folder}/{name}"] = tag
    body["subs/0.vtt"] = f"WEBVTT\n\nNOTE {tag}\n"
    body["trickplay/sprite-0000.jpg"] = tag
    body[pk.MANIFEST_FILE] = json.dumps({
        "tag": tag,
        "hls": {"master": "hls/master.m3u8"},
        "renditions": {"video": [{"id": "v0", "dir": "hls/v0"}],
                       "audio": [{"id": "a0", "dir": "hls/a0"}]},
        "subtitles": [{"id": "sub0", "path": "subs/0.vtt", "format": "webvtt"}],
        "trickplay": {"vttPath": "trickplay/thumbnails.vtt"},
    })
    for rel, text in body.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def item(tmp_path: Path) -> tuple[Path, Path]:
    """An item folder with a live package ("old") and a staged one ("new")."""
    root = tmp_path / "movies" / "c0" / "c0ffee00-0000-4000-8000-000000000001"
    _write_package(root, "old")
    (root / ".complete").write_text("2026-10-01T00:00:00+00:00\n")
    stage = root / pk.STAGING_DIR
    _write_package(stage, "new")
    (stage / pk.SENTINEL).write_text("{}")
    return root, stage


def test_a_whole_package_passes(item) -> None:
    _root, stage = item
    pk._verify_staged(stage)


def test_an_empty_sidecar_passes(item) -> None:
    # A subtitle track without a cue (a forced track with nothing to force)
    # extracts to an empty file: the package is whole all the same.
    _root, stage = item
    (stage / "subs" / "0.vtt").write_bytes(b"")
    pk._verify_staged(stage)


@pytest.mark.parametrize(("change", "named"), [
    (lambda s: (s / "hls/v0/seg-00002.m4s").unlink(), "hls/v0/seg-00002.m4s"),
    (lambda s: (s / "hls/a0/init.mp4").write_bytes(b""), "hls/a0/init.mp4"),
    (lambda s: (s / "hls/v0/iframes.m3u8").unlink(), "hls/v0/iframes.m3u8"),
    (lambda s: (s / "hls/a0/playlist.m3u8").write_text(MEDIA.replace("#EXT-X-ENDLIST\n", "")),
     "hls/a0/playlist.m3u8 (no #EXT-X-ENDLIST)"),
    (lambda s: (s / "hls/v0/playlist.m3u8").write_text(
        MEDIA.replace("seg-00001.m4s", "../../../.complete")), "outside the package"),
    (lambda s: (s / "hls/master.m3u8").write_text(
        MASTER.replace("v0/playlist.m3u8\n", "/srv/v0/playlist.m3u8\n")), "outside the package"),
    (lambda s: (s / "subs/0.vtt").unlink(), "subs/0.vtt"),
    (lambda s: (s / "trickplay/sprite-0000.jpg").unlink(), "trickplay/sprite-0000.jpg"),
])
def test_an_incomplete_package_is_refused(item, change, named: str) -> None:
    _root, stage = item
    change(stage)
    with pytest.raises(pk.PackageError, match="not swapped in") as e:
        pk._verify_staged(stage)
    assert named in str(e.value)


def test_a_swap_puts_the_staged_package_in_place(item) -> None:
    root, stage = item
    old, new = _tree(root / "hls"), _tree(stage)
    (root / ".failed").write_text("{}")             # an earlier run that failed
    (root / "trailers").mkdir()                     # what the new package doesn't have
    (root / "trailers" / "0.mp4").write_text("old")
    replaced = pk._swap_in(root, stage)

    live = {rel: text for rel, text in _tree(root).items() if ".old-" not in rel.split("/")[0]}
    assert {rel: text for rel, text in live.items() if rel != ".complete"} == {
        rel: text for rel, text in new.items() if rel != pk.SENTINEL}
    assert live[".complete"] != "2026-10-01T00:00:00+00:00\n"
    assert not (root / ".failed").exists() and not stage.exists()
    # The old package and what it had beyond the new one, beside it.
    assert sorted(p.name.split(".old-")[0] for p in replaced) == [
        "hls", "subs", "trailers", "trickplay"]
    assert all(p.parent == root and p.exists() for p in replaced)
    assert _tree(next(p for p in replaced if p.name.startswith("hls."))) == old


def test_a_swap_that_fails_moves_everything_back(item, monkeypatch) -> None:
    # The manifest's rename is the last step of the swap; when it fails,
    # the package directories already moved go back where they were.
    root, stage = item
    live, staged = _tree(root), _tree(stage)
    real = os.replace

    def replace(src, dst):
        if Path(dst).name == pk.MANIFEST_FILE:
            raise OSError(5, "Input/output error")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError, match="Input/output"):
        pk._swap_in(root, stage)
    assert _tree(root) == live          # stage included, as it was
    assert _tree(stage) == staged
    assert not [p for p in root.iterdir() if ".old-" in p.name]


def _stamp(ago: timedelta) -> str:
    return (datetime.now(UTC) - ago).strftime(pk._STAMP)


def test_a_run_clears_the_replaced_packages_past_their_grace(tmp_path: Path) -> None:
    expired = [f"hls.old-{_stamp(timedelta(hours=1))}", f"subs.old-{_stamp(timedelta(hours=1))}",
               f"manifest.json.tmp.old-{_stamp(timedelta(minutes=11))}"]
    recent = [f"hls.old-{_stamp(timedelta(minutes=9))}"]
    other = ["hls", "hls.old", "hls.old-yesterday", ".complete"]
    for name in expired + recent + other:
        if name.startswith("manifest") or name == ".complete":
            (tmp_path / name).write_text("x")
        else:
            (tmp_path / name).mkdir()
            (tmp_path / name / "seg-00001.m4s").write_text("x")
    assert pk._remove_replaced(tmp_path, 600) == len(expired)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(recent + other)
