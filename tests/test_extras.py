"""The extras of a title (trailers, teasers, featurettes), without
binaries: where an extra's package goes, and what the startup sweep
clears there. The real packaging runs of extras are in
test_package_real.py."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packager import packager as pk

EXTRA = "1b5c2a8e-0000-4000-8000-0000000000e1"
PARENT = "c0ffee00-0000-4000-8000-000000000007"

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
