"""The extras of a title (trailers, teasers, featurettes), without
binaries: where an extra's package goes, what the startup sweep clears
there, and how package_item packages one: no trickplay. package_item
runs for real but for its binaries (ffprobe, the remux, shaka-packager,
the trickplay sprites), which stand-ins replace. The real packaging
runs of extras are in test_package_real.py."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packager import packager as pk
from packager.config import Config

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
