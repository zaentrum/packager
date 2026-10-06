"""A run whose worker record carries a library block (library v2), with
stand-ins for its binaries (ffprobe, the remux, shaka-packager, the
trickplay sprites, the subtitle conversion): what goes into the record
and the work tree and in which steps, the handover the catalog gets and
what makes the step done, a run that dies before, between or after the
two renames, folders that are already there, the inputs a run takes, and
the extras. The real runs are in test_library_real.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from packager import extras, library, records, worker
from packager import libv2_records as rec
from packager import packager as pk
from packager.katalog import ClaimedExtra, ClaimedItem, Handover

ITEM = "f001aeff-9c18-4183-b51b-51403af2515e"
SOURCE = "0b6c3d2e-1111-4a2b-8c3d-4e5f60718293"
VERSION = "9a2e4f6a-2222-4b3c-9d4e-5f6071829304"
NEXT_VERSION = "77c1aaaa-3333-4c3d-9e4f-506172839405"
ASSET = "a1b2c3d4-0000-4000-8000-00000000a5e7"
EXTRA = "16aa63f3-3333-4c4d-8e5f-60718293a4b5"
SOURCE_BLOCK = {"codec": "hevc", "width": 1920, "height": 1080, "durationMs": 33_000,
                "bitRate": 5_000_000}
MEDIA = ('#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MAP:URI="init.mp4"\n'
         "#EXTINF:6.000,\nseg-00001.m4s\n#EXTINF:2.000,\nseg-00002.m4s\n#EXT-X-ENDLIST\n")


class _Killed(BaseException):
    """The process dies (SIGKILL, the OOM killer): no handler runs."""


def _raw_probe(codec: str = "hevc") -> dict:
    """ffprobe -show_format -show_streams -show_chapters of the original."""
    return {
        "streams": [
            {"index": 0, "codec_name": codec, "codec_type": "video", "profile": "Main 10",
             "width": 1920, "height": 1080, "pix_fmt": "yuv420p10le", "avg_frame_rate": "24/1",
             "display_aspect_ratio": "16:9", "disposition": {"default": 1}, "tags": {}},
            {"index": 1, "codec_name": "aac", "codec_type": "audio", "channels": 6,
             "channel_layout": "5.1", "sample_rate": "48000", "disposition": {"default": 1},
             "tags": {"language": "eng"}},
        ],
        "chapters": [{"id": 0, "start_time": "0.000000", "end_time": "33.000000",
                      "tags": {"title": "Chapter 1"}}],
        "format": {"format_name": "matroska,webm", "duration": "33.000000",
                   "bit_rate": "5000000", "tags": {"ENCODER": "Lavf61.7.100"}},
    }


class Binaries:
    """Stand-ins for what a run executes. `builds` counts the shaka runs;
    `hooks` run inside one (a run that dies there, a look at the tree)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.codec = "hevc"
        self.rung_codec = "hevc"
        self.builds = 0
        self.hooks: list = []
        monkeypatch.setattr(pk, "_packager_version", lambda: "packager-test")
        monkeypatch.setattr(pk, "_ffprobe", self._probe)
        monkeypatch.setattr(pk, "_prepare_source", self._prepare)
        monkeypatch.setattr(pk, "_run_shaka_packager", self._shaka)
        monkeypatch.setattr(pk, "_generate_trickplay", self._sprites)
        monkeypatch.setattr(pk, "_run_ffmpeg_capturing", self._ffmpeg)
        monkeypatch.setattr(records, "probe_original",
                            lambda path: records.OriginalProbe(_raw_probe(self.codec),
                                                               "ffprobe version test"))
        monkeypatch.setattr(worker, "probe_source", lambda _path: dict(SOURCE_BLOCK))

    def _probe(self, path: Path) -> pk._Probe:
        codec = self.rung_codec if Path(path).name.startswith("v1") else self.codec
        return pk._Probe(container="matroska,webm", duration_ms=33_000,
                         video={"index": 0, "codec_name": codec, "width": 1920, "height": 1080},
                         audio=[{"index": 1, "codec_name": "aac", "channels": 6,
                                 "tags": {"language": "eng"}, "disposition": {"default": 1}}],
                         subtitles=[], video_index=0)

    @staticmethod
    def _prepare(_src, _probe, tmpdir: Path, **_kw):
        audio = [{"idx": 0, "codec": "aac", "language": "eng", "title": "", "channels": 2,
                  "default": True, "visible": True}]
        return tmpdir / "transmux.mp4", audio, []

    def _shaka(self, _primary, videos, audio_meta, surround_meta, out_root: Path, **_kw):
        self.builds += 1
        for hook in self.hooks:
            hook(out_root)
        master = ["#EXTM3U", "#EXT-X-INDEPENDENT-SEGMENTS",
                  '#EXT-X-MEDIA:TYPE=AUDIO,URI="a0/playlist.m3u8",GROUP-ID="audio",NAME="English",'
                  "DEFAULT=YES,CHANNELS=\"2\""]
        video = []
        for v in videos:
            rid = v.rung.id
            master += [f'#EXT-X-STREAM-INF:BANDWIDTH={4_000_000 // (len(video) + 1)},'
                       f'CODECS="hvc1.2.4.L120.B0,mp4a.40.2",AUDIO="audio"', f"{rid}/playlist.m3u8"]
            video.append({"id": rid, "dir": f"hls/{rid}", "codec": "hvc1.2.4.L120.B0",
                          "width": 1920, "height": 1080, "bitrateBps": 3_000_000,
                          "peakBitrateBps": 3_500_000, "hdr": False, "videoRange": "SDR",
                          "frameRate": "24/1", "segments": 2, "targetDuration": 6,
                          "label": v.rung.label, "encoder": v.rung.encoder})
        for rid in [v.rung.id for v in videos] + ["a0"]:
            d = out_root / "hls" / rid
            d.mkdir(parents=True, exist_ok=True)
            (d / "playlist.m3u8").write_text(MEDIA)
            for name in ("init.mp4", "seg-00001.m4s", "seg-00002.m4s"):
                (d / name).write_bytes(f"{rid}/{name}".encode())
        (out_root / "hls" / "master.m3u8").write_text("\n".join(master) + "\n")
        audio = [{**audio_meta[0], "id": "a0", "dir": "hls/a0", "codec": "mp4a.40.2",
                  "bitrateBps": 192_000, "segments": 2, "group": "audio", "name": "English"}]
        return video, audio, surround_meta

    @staticmethod
    def _sprites(_src, _probe, out_dir: Path):
        out_dir.mkdir(parents=True)
        cues = "".join(f"00:00:{i * 10:02d}.000 --> 00:00:{i * 10 + 10:02d}.000\n"
                       f"sprite-0000.jpg#xywh=0,0,320,180\n\n" for i in range(3))
        (out_dir / "thumbnails.vtt").write_text("WEBVTT\n\n" + cues)
        (out_dir / "sprite-0000.jpg").write_bytes(b"\xff\xd8jpg")
        return {"vttPath": "trickplay/thumbnails.vtt", "spritePattern": "trickplay/sprite-%04d.jpg",
                "intervalSec": 10, "thumbWidth": 320, "thumbHeight": 180, "gridCols": 10,
                "gridRows": 10}

    @staticmethod
    def _ffmpeg(label: str, args: list[str]) -> None:
        if label.startswith("subtitle file"):
            source = Path(args[args.index("-i") + 1])
            Path(args[-1]).write_text("WEBVTT\n\n" + source.read_text())


@pytest.fixture
def binaries(monkeypatch: pytest.MonkeyPatch) -> Binaries:
    return Binaries(monkeypatch)


class Share:
    """The share: the library root, its work tree and one arrival, with the
    worker record the catalog hands out for it."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lib = tmp_path / "katalog"
        self.work = self.lib / ".work"
        self.item_dir = self.lib / "movies" / "f0" / ITEM
        folder = self.work / "incoming" / "Sintel (2010)"
        folder.mkdir(parents=True)
        self.original = folder / "Sintel (2010).mkv"
        self.original.write_bytes(bytes(range(256)) * 800)     # 200 KiB: both ends of qh1
        self.srt = folder / "Sintel (2010).en.srt"
        self.srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n")
        (folder / "Sintel (2010).nfo").write_text("<movie><title>Sintel</title></movie>\n")
        self.inbox = self.work / "inbox" / ITEM
        self.staging = self.work / "staging" / VERSION
        self.version_dir = self.item_dir / "versions" / VERSION
        self.source_dir = self.item_dir / "sources" / SOURCE
        self.legacy_inbox = self.lib / "packages" / "_inbox"
        monkeypatch.setattr(worker, "_INBOX_ROOT", self.legacy_inbox)
        monkeypatch.setattr(extras, "_INBOX_ROOT", self.legacy_inbox)

    def record(self, *, version: str = VERSION, recorded: bool = False, **fields) -> dict:
        staging = self.work / "staging" / version
        return {
            "id": ITEM, "type": "movie", "title": "Sintel", "year": 2010, "durationMs": 33_000,
            "path": str(self.original), "movieTmdbId": "45745",
            "subtitleFiles": [{"id": ASSET, "path": str(self.srt), "language": "eng",
                               "label": "English"}],
            "library": {
                "contract": 1, "root": str(self.lib), "itemDir": str(self.item_dir),
                "blocked": None,
                "source": {"sourceId": SOURCE, "recorded": recorded,
                           "recordDir": str(self.source_dir),
                           "libraryPath": "Sintel (2010)/Sintel (2010).mkv",
                           "sizeBytes": self.original.stat().st_size,
                           "qh1": rec.qh1(str(self.original))},
                "inboxDir": str(self.inbox),
                "build": {"versionId": version, "stagingDir": str(staging),
                          "versionDir": str(self.item_dir / "versions" / version),
                          "createdBy": "katalog-manager",
                          "chapters": [{"startMs": 0, "endMs": 33000, "title": "Opening"}],
                          "chaptersFrom": "original-file",
                          "segments": [{"kind": "credits", "startMs": 28000, "endMs": 33000,
                                        "detector": "chapter", "confidence": 0.9,
                                        "label": None}]},
                "current": None,
            },
            **fields,
        }

    def tree(self, root: Path | None = None) -> dict[str, bytes]:
        root = root or self.lib
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Share:
    return Share(tmp_path, monkeypatch)


class Catalog:
    """The catalog's side of a run: the steps it records, the handovers it
    gets, and its answer to them (`status`; a callable to do more)."""

    def __init__(self, status: int = 200, steps: dict | None = None) -> None:
        self.status = status
        self.steps = steps or {}
        self.written: list[tuple[str, dict]] = []
        self.handovers: list[dict] = []
        self.on_handover = None

    def settings(self) -> dict:
        return {}

    def get_steps(self, _id: str) -> dict:
        return dict(self.steps)

    def upsert_step(self, _id: str, status: str, **kw: object) -> None:
        self.written.append((status, kw))

    upsert_extra_step = upsert_step

    def packaging_complete(self, *_a) -> None:
        raise AssertionError("a v2 record is never handed over as a legacy manifest")

    extra_packaging_complete = packaging_complete

    def packaging_complete_v2(self, _id: str, payload: dict) -> Handover:
        self.handovers.append(json.loads(json.dumps(payload)))
        if self.on_handover:
            self.on_handover(payload)
        if 200 <= self.status < 300:
            return Handover(True, self.status, {"itemId": ITEM, "current": True,
                                                "superseded": None}, None)
        return Handover(False, self.status, {},
                        f"the catalog answered {self.status}: stale version")

    extra_packaging_complete_v2 = packaging_complete_v2

    @property
    def statuses(self) -> list[str]:
        return [s for s, _ in self.written]

    @property
    def error(self) -> str:
        return str(self.written[-1][1].get("error"))


def run(record: dict, catalog: Catalog) -> None:
    worker._process_one(ClaimedItem.from_json(record), catalog,  # type: ignore[arg-type]
                        pk.PackageOptions())


# ------------------------------------------------------------- the version

def _whole(share: Share) -> None:
    """The record holds the source and the version, whole; nothing else of
    the run is left anywhere."""
    assert library.verify_chain(share.version_dir, "version.json") is None
    assert sorted(p.name for p in share.item_dir.iterdir()) == ["sources", "versions"]
    assert sorted(p.name for p in share.version_dir.iterdir()) == [
        ".complete", "checksums.sha256", "hls", "package.json", "subs", "trickplay",
        "version.json"]
    assert sorted(p.name for p in share.source_dir.iterdir()) == [
        "Sintel (2010).en.srt", "Sintel (2010).nfo", "checksums.sha256", "ffprobe.json",
        "source.json"]
    sums = dict(reversed(line.split("  ", 1))
                for line in (share.source_dir / "checksums.sha256").read_text().splitlines())
    assert sorted(sums) == sorted(p.name for p in share.source_dir.iterdir()
                                  if p.name != "checksums.sha256")
    for name, digest in sums.items():
        assert rec.sha_file(str(share.source_dir / name)) == f"sha256:{digest}"
    assert not [p for p in share.lib.rglob("*") if p.name in (
        pk.MANIFEST_FILE, ".failed", pk.STAGING_DIR, pk.SENTINEL)]


def test_a_version_goes_into_the_record(binaries: Binaries, share: Share) -> None:
    catalog = Catalog()
    run(share.record(), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    _whole(share)
    assert not share.staging.exists() and not share.inbox.exists()

    package_bytes = (share.version_dir / "package.json").read_bytes()
    package = json.loads(package_bytes)
    [payload] = catalog.handovers
    assert payload == {
        "layout": "v2", "versionId": VERSION, "packageId": package["packageId"],
        "versionDir": str(share.version_dir),
        "complete": (share.version_dir / ".complete").read_text().strip(),
        "sourceId": SOURCE, "sourceRecorded": True, "package": package,
        "sidecars": [{"subtitleAssetId": ASSET, "rendition": "sub0", "path": "subs/0.vtt"}],
        "source": SOURCE_BLOCK,
    }
    assert payload["complete"] == rec.sha_bytes(package_bytes)

    source = json.loads((share.source_dir / "source.json").read_text())
    assert (source["sourceId"], source["takenBy"]) == (SOURCE, "packager")
    assert source["origin"] == {"libraryPath": "Sintel (2010)/Sintel (2010).mkv",
                                "takenBy": "import", "folder": "Sintel (2010)"}
    assert source["file"]["fixity"] == {"qh1": rec.qh1(str(share.original))}
    assert source["probe"]["file"] == f"sources/{SOURCE}/ffprobe.json"
    assert source["probe"]["sha256"] == rec.sha_file(str(share.source_dir / "ffprobe.json"))
    assert json.loads((share.source_dir / "ffprobe.json").read_text()) == _raw_probe()
    assert [(s["file"], s["originalName"], s["kind"]) for s in source["sidecars"]] == [
        (f"sources/{SOURCE}/Sintel (2010).en.srt", "Sintel (2010).en.srt", "subtitle"),
        (f"sources/{SOURCE}/Sintel (2010).nfo", "Sintel (2010).nfo", "nfo")]
    assert (share.source_dir / "Sintel (2010).en.srt").read_bytes() == share.srt.read_bytes()

    version = json.loads((share.version_dir / "version.json").read_text())
    assert (version["versionId"], version["sourceIds"], version["originalFiles"]) == (
        VERSION, [SOURCE], [])
    assert (version["chapters"], version["chaptersFrom"]) == (
        [{"startMs": 0, "endMs": 33000, "title": "Opening"}], "original-file")
    assert version["segments"] == [{"kind": "credits", "startMs": 28000, "endMs": 33000,
                                    "detector": "chapter", "confidence": 0.9, "label": None}]
    assert (version["createdBy"], version["runtimeMs"]) == ("katalog-manager", 33000)

    assert (package["role"], package["state"]) == ("canonical", "complete")
    # One moment for both: the version is established when its package completes.
    assert package["createdAt"] == version["createdAt"]
    [sub] = package["subtitles"]
    assert (sub["id"], sub["path"], sub["fromSidecar"], sub["default"]) == (
        "sub0", "subs/0.vtt", f"sources/{SOURCE}/Sintel (2010).en.srt", False)
    assert [(v["id"], v["sourceStreamIndex"]) for v in package["renditions"]["video"]] == [
        ("v0", 0)]
    [audio] = package["renditions"]["audio"]
    assert (audio["sourceStreamIndex"], audio["sourceChannels"], audio["channels"]) == (1, 6, 2)
    assert package["hls"]["master"] == "hls/master.m3u8"
    assert package["peakBandwidthBps"] == 4_000_000
    # Its 5.1 is gone: the package says so, as the deletion gate will.
    assert {x["kind"] for x in package["fidelity"]["losses"]} >= {"audio-downmix"}


def test_the_run_builds_in_staging_and_the_record_sees_nothing_until_the_renames(
    binaries: Binaries, share: Share,
) -> None:
    seen: list = []

    def look(out_root: Path) -> None:
        seen.append((out_root, sorted(p.name for p in share.staging.iterdir()),
                     share.item_dir.exists()))
        sentinel = json.loads((share.staging / pk.SENTINEL).read_text())
        assert set(sentinel) == {"startedAt", "pid", "host"}

    binaries.hooks.append(look)
    run(share.record(), Catalog())
    assert seen == [(share.staging / "version", [pk.SENTINEL, "version"], False)]


@pytest.mark.parametrize("dies_at", ["source", "version"])
def test_a_run_that_dies_before_a_rename_is_built_again(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch, dies_at: str,
) -> None:
    # It dies as it renames the source (before step 8) or the version
    # (between 8 and 9): no handler runs.
    real = library.place

    def place(staged: Path, target: Path) -> bool:
        if staged.name == dies_at:
            raise _Killed
        return real(staged, target)

    monkeypatch.setattr(library, "place", place)
    catalog = Catalog()
    with pytest.raises(_Killed):
        run(share.record(), catalog)
    assert catalog.statuses == ["in_progress"] and catalog.handovers == []
    assert (share.staging / pk.SENTINEL).exists()
    assert not share.version_dir.exists()
    source_before = share.tree(share.source_dir) if dies_at == "version" else None
    if dies_at == "version":
        # The source is in the record, whole; the catalog doesn't know.
        assert sorted(source_before) == [
            "Sintel (2010).en.srt", "Sintel (2010).nfo", "checksums.sha256", "ffprobe.json",
            "source.json"]
        inode = (share.source_dir / "source.json").stat().st_ino
    else:
        assert not share.item_dir.exists()

    monkeypatch.setattr(library, "place", real)
    catalog = Catalog()
    run(share.record(), catalog)            # the record says recorded: false, as before
    assert catalog.statuses == ["in_progress", "done"]
    assert binaries.builds == 2
    _whole(share)
    if dies_at == "version":
        # Kept as it was, not written again.
        assert share.tree(share.source_dir) == source_before
        assert (share.source_dir / "source.json").stat().st_ino == inode
    # The copy the first run made is what the version names.
    [payload] = catalog.handovers
    assert payload["sidecars"] == [{"subtitleAssetId": ASSET, "rendition": "sub0",
                                    "path": "subs/0.vtt"}]
    assert payload["package"]["subtitles"][0]["fromSidecar"] == (
        f"sources/{SOURCE}/Sintel (2010).en.srt")


def test_a_run_that_dies_after_the_renames_is_reported_again(
    binaries: Binaries, share: Share,
) -> None:
    catalog = Catalog()

    def dies(_payload: dict) -> None:
        raise _Killed

    catalog.on_handover = dies
    with pytest.raises(_Killed):
        run(share.record(), catalog)
    placed = share.tree(share.item_dir)
    first = catalog.handovers[0]

    catalog = Catalog()
    run(share.record(), catalog)
    assert binaries.builds == 1                      # nothing built again
    assert catalog.statuses == ["in_progress", "done"]
    assert catalog.handovers == [first]              # the same version, the same words
    assert share.tree(share.item_dir) == placed
    assert not share.staging.exists() and not share.inbox.exists()


@pytest.mark.parametrize("status", [409, 422, 500])
def test_a_handover_the_catalog_refuses_fails_the_step_and_keeps_the_version(
    binaries: Binaries, share: Share, status: int,
) -> None:
    share.inbox.mkdir(parents=True)
    (share.inbox / "renditions.json").write_text(json.dumps({
        "version": 1, "segmentSeconds": 6, "source": SOURCE_BLOCK,
        "video": [{"id": "v0", "label": "source", "file": None, "mode": "copy"}]}))
    catalog = Catalog(status=status)
    run(share.record(), catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert catalog.error.startswith(f"packaging-complete: the catalog answered {status}")
    placed = share.tree(share.item_dir)
    assert library.verify_chain(share.version_dir, "version.json") is None
    assert share.inbox.exists() and (share.staging / pk.SENTINEL).exists()

    # Its retry reports the version again, as it is.
    catalog = Catalog()
    run(share.record(), catalog)
    assert binaries.builds == 1
    assert catalog.statuses == ["in_progress", "done"]
    assert share.tree(share.item_dir) == placed
    assert not share.inbox.exists() and not share.staging.exists()


def test_a_second_version_of_a_recorded_source(binaries: Binaries, share: Share) -> None:
    # The first version recorded the source; a re-encode builds another
    # version of the same original, and the source stays as it is.
    run(share.record(), Catalog())
    source_before = share.tree(share.source_dir)
    catalog = Catalog()
    run(share.record(version=NEXT_VERSION, recorded=True), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert share.tree(share.source_dir) == source_before
    second = share.item_dir / "versions" / NEXT_VERSION
    assert library.verify_chain(second, "version.json") is None
    assert library.verify_chain(share.version_dir, "version.json") is None   # the first, as it was
    [payload] = catalog.handovers
    assert payload["versionId"] == NEXT_VERSION and payload["sourceRecorded"] is True
    assert payload["sidecars"] == [{"subtitleAssetId": ASSET, "rendition": "sub0",
                                    "path": "subs/0.vtt"}]
    assert json.loads((second / "version.json").read_text())["sourceIds"] == [SOURCE]


def test_a_source_folder_another_writer_left_unfinished(binaries: Binaries, share: Share) -> None:
    share.source_dir.mkdir(parents=True)
    (share.source_dir / "source.json").write_text("{}")
    catalog = Catalog()
    run(share.record(), catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert f"sources/{SOURCE} exists unfinished" in catalog.error
    assert binaries.builds == 0 and not share.staging.exists()
    assert sorted(p.name for p in share.source_dir.iterdir()) == ["source.json"]


@pytest.mark.parametrize("left", ["empty", "half"])
def test_a_version_folder_that_is_not_whole_is_never_written_over(
    binaries: Binaries, share: Share, left: str,
) -> None:
    share.version_dir.mkdir(parents=True)
    if left == "half":
        (share.version_dir / "hls").mkdir()
        (share.version_dir / "version.json").write_text("{}")
    before = share.tree(share.version_dir)
    catalog = Catalog()
    run(share.record(), catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert f"versions/{VERSION} is there but not a whole version" in catalog.error
    assert binaries.builds == 0 and catalog.handovers == []
    assert share.tree(share.version_dir) == before


@pytest.mark.parametrize("change", ["size", "bytes"])
def test_an_original_that_changed_since_it_arrived(
    binaries: Binaries, share: Share, change: str,
) -> None:
    record = share.record()
    if change == "size":
        with open(share.original, "ab") as f:
            f.write(b"more")
    else:
        data = bytearray(share.original.read_bytes())
        data[0] ^= 0xFF
        share.original.write_bytes(bytes(data))
    catalog = Catalog()
    run(record, catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert "changed since it arrived" in catalog.error
    assert binaries.builds == 0 and not share.item_dir.exists() and not share.staging.exists()


def test_a_run_that_fails_leaves_no_staging_and_nothing_in_the_record(
    binaries: Binaries, share: Share,
) -> None:
    binaries.codec = "h264"               # the owner's decision: packages are HEVC only
    catalog = Catalog()
    run(share.record(), catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert "'h264' not supported (passthrough only; HEVC is the allowed input codec)" in (
        catalog.error)
    assert not share.item_dir.exists() and not share.staging.exists()


def test_a_lower_rung_that_is_not_hevc_is_left_out(
    binaries: Binaries, share: Share,
) -> None:
    binaries.rung_codec = "h264"
    share.inbox.mkdir(parents=True)
    (share.inbox / "v1.mkv").write_bytes(b"x")
    (share.inbox / "renditions.json").write_text(json.dumps({
        "version": 1, "segmentSeconds": 6, "source": SOURCE_BLOCK,
        "video": [{"id": "v0", "label": "source", "file": None, "mode": "copy"},
                  {"id": "v1", "label": "720p", "file": "v1.mkv", "mode": "encode",
                   "encoder": "libx264"}]}))
    catalog = Catalog()
    run(share.record(), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    package = catalog.handovers[0]["package"]
    assert [v["id"] for v in package["renditions"]["video"]] == ["v0"]


def test_a_blocked_item_fails_with_the_catalogs_words(binaries: Binaries, share: Share) -> None:
    reason = "an episode needs its season and episode numbers before it is recorded"
    record = share.record()
    record["library"] = {"contract": 1, "blocked": reason}
    catalog = Catalog()
    run(record, catalog)
    assert catalog.written == [("failed", {"error": reason})]
    assert binaries.builds == 0 and not share.lib.joinpath("movies").exists()


def test_a_block_that_cant_be_worked_from_never_packages_into_the_store(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "package_item", lambda *_a, **_k: pytest.fail("legacy run"))
    record = share.record()
    record["library"]["contract"] = 2
    catalog = Catalog()
    run(record, catalog)
    assert catalog.statuses == ["failed"]
    assert catalog.error.startswith("worker record: library contract 2 is not supported")


def test_a_record_without_a_library_block_packages_as_before(
    binaries: Binaries, share: Share, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pk, "PACKAGES_ROOT", share.lib / "packages")
    legacy: list[dict] = []
    catalog = Catalog()
    catalog.packaging_complete = lambda _id, manifest: legacy.append(manifest)  # type: ignore
    record = share.record()
    del record["library"]
    run(record, catalog)
    assert catalog.statuses == ["in_progress", "done"] and catalog.handovers == []
    root = share.lib / "packages" / "movies" / "f0" / ITEM
    assert json.loads((root / pk.MANIFEST_FILE).read_text())["itemId"] == ITEM
    assert legacy[0]["source"] == SOURCE_BLOCK
    assert not share.item_dir.exists() and not share.work.joinpath("staging").exists()


# ------------------------------------------------------------- the inputs

def _handoff(inbox: Path) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "v1.mkv").write_bytes(b"x")
    (inbox / "renditions.json").write_text(json.dumps({
        "version": 1, "segmentSeconds": 6, "source": SOURCE_BLOCK,
        "video": [{"id": "v0", "label": "source", "file": None, "mode": "copy"},
                  {"id": "v1", "label": "1080p", "file": "v1.mkv", "mode": "encode",
                   "encoder": "libx265"}]}))


def test_the_records_inbox_is_the_handoff(binaries: Binaries, share: Share) -> None:
    _handoff(share.inbox)
    catalog = Catalog(steps={"transcode": "done"})
    run(share.record(), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert [v["id"] for v in catalog.handovers[0]["package"]["renditions"]["video"]] == [
        "v0", "v1"]
    assert not share.inbox.exists()


def test_a_handoff_left_in_the_package_store_before_the_switch(
    binaries: Binaries, share: Share,
) -> None:
    # Transcoded while the layout was legacy, packaged once it is v2.
    legacy = share.legacy_inbox / ITEM
    _handoff(legacy)
    catalog = Catalog(steps={"transcode": "done"})
    run(share.record(), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert [v["id"] for v in catalog.handovers[0]["package"]["renditions"]["video"]] == [
        "v0", "v1"]
    assert not legacy.exists()


def test_a_done_transcode_whose_handoff_is_gone_is_not_packaged_from_the_original(
    binaries: Binaries, share: Share,
) -> None:
    catalog = Catalog(steps={"transcode": "done"})
    run(share.record(), catalog)
    assert catalog.statuses == ["failed"]
    assert catalog.error.startswith("the transcode step is done, but its handoff is in neither ")
    assert binaries.builds == 0


@pytest.mark.parametrize("transcode", ["not_applicable", "skipped", None])
def test_an_original_the_transcoder_left_as_it_was(
    binaries: Binaries, share: Share, transcode: str | None,
) -> None:
    catalog = Catalog(steps={"transcode": transcode} if transcode else {})
    run(share.record(), catalog)
    assert catalog.statuses == ["in_progress", "done"]


# ------------------------------------------------------------- sidecars

def test_every_file_beside_the_original_is_copied_once_under_a_name_of_its_own(
    binaries: Binaries, share: Share,
) -> None:
    folder = share.original.parent
    (folder / "Subs").mkdir()
    other = folder / "Subs" / "Sintel (2010).en.srt"          # the same name, another folder
    other.write_text("1\n00:00:03,000 --> 00:00:04,000\nAgain.\n")
    forced = folder / "Sintel (2010).de.forced.srt"
    forced.write_text("1\n00:00:01,000 --> 00:00:02,000\n[Zeichen]\n")
    (folder / "Sintel (2010).jpg").write_bytes(b"\xff\xd8jpg")
    (folder / "Sintel (2010).txt").write_bytes(b"x" * (10 * 1024 * 1024 + 1))  # too large
    (folder / "Other.nfo").write_text("not this film's")
    record = share.record(subtitleFiles=[
        {"id": ASSET, "path": str(share.srt), "language": "eng", "label": "English"},
        {"id": "b2", "path": str(other), "language": "eng"},
        {"id": "c3", "path": str(forced), "language": "ger", "forced": True},
        {"id": "d4", "path": str(folder / "gone.srt"), "language": "fre"},
    ])
    catalog = Catalog()
    run(record, catalog)
    assert catalog.statuses == ["in_progress", "done"]
    source = json.loads((share.source_dir / "source.json").read_text())
    assert [(Path(s["file"]).name, s["originalName"], s["kind"]) for s in source["sidecars"]] == [
        ("Sintel (2010).en.srt", "Sintel (2010).en.srt", "subtitle"),
        ("Sintel (2010).en-1.srt", "Sintel (2010).en.srt", "subtitle"),
        ("Sintel (2010).de.forced.srt", "Sintel (2010).de.forced.srt", "subtitle"),
        ("Sintel (2010).jpg", "Sintel (2010).jpg", "image"),
        ("Sintel (2010).nfo", "Sintel (2010).nfo", "nfo")]
    assert (share.source_dir / "Sintel (2010).en-1.srt").read_bytes() == other.read_bytes()
    forced_entry = source["sidecars"][2]
    assert (forced_entry["forced"], forced_entry["purpose"]) == (True, "forced")
    package = catalog.handovers[0]["package"]
    assert [(s["id"], s["fromSidecar"]) for s in package["subtitles"]] == [
        ("sub0", f"sources/{SOURCE}/Sintel (2010).en.srt"),
        ("sub1", f"sources/{SOURCE}/Sintel (2010).en-1.srt"),
        ("sub2", f"sources/{SOURCE}/Sintel (2010).de.forced.srt")]
    assert package["subtitles"][2]["purpose"] == "forced"
    assert catalog.handovers[0]["sidecars"] == [
        {"subtitleAssetId": ASSET, "rendition": "sub0", "path": "subs/0.vtt"},
        {"subtitleAssetId": "b2", "rendition": "sub1", "path": "subs/1.vtt"},
        {"subtitleAssetId": "c3", "rendition": "sub2", "path": "subs/2.vtt"}]
    # Copies, never links, in the writer's mode.
    for name in ("Sintel (2010).en.srt", "Sintel (2010).jpg"):
        st = (share.source_dir / name).stat()
        assert st.st_nlink == 1 and st.st_ino != (folder / name).stat().st_ino


# ------------------------------------------------------------- extras

class ExtraShare:
    def __init__(self, share: Share) -> None:
        self.share = share
        self.title_dir = share.item_dir
        folder = share.work / "extras" / "sintel"
        folder.mkdir(parents=True)
        self.file = folder / "trailer.mov"
        self.file.write_bytes(bytes(range(256)) * 400)
        self.extra_dir = self.title_dir / "extras" / EXTRA
        self.staging = share.work / "staging" / f"extra-{EXTRA}"
        self.inbox = share.work / "inbox" / f"extra-{EXTRA}"

    def record(self, **library) -> dict:
        return {
            "id": EXTRA, "type": "extra", "parentId": ITEM, "parentType": "movie",
            "parentTitle": "Sintel", "kind": "trailer", "title": "Trailer", "language": "en",
            "seasonNumber": None, "path": str(self.file), "state": "transcoded",
            "removedAt": None,
            "library": {
                "contract": 1, "itemDir": str(self.title_dir), "inboxDir": str(self.inbox),
                "stagingDir": str(self.staging), "extraDir": str(self.extra_dir),
                "recorded": False,
                "record": {"kind": "trailer", "title": "Trailer",
                           "localizedTitles": {"de": "Vorschau"},
                           "language": "en", "seasonNumber": None,
                           "origin": {"kind": "link", "site": "video.example",
                                      "externalId": "t-0001", "url": None,
                                      "fetchedAt": "2026-10-05T08:00:00Z"},
                           "createdAt": "2026-10-06T08:00:00Z",
                           "createdBy": "katalog-manager/api"},
                "original": {"name": "trailer.mov", "sizeBytes": self.file.stat().st_size,
                             "qh1": rec.qh1(str(self.file))},
                **library,
            },
        }


def run_extra(record: dict, catalog: Catalog) -> None:
    extra = ClaimedExtra.from_json(EXTRA, record)
    extras._process_extra(extra, {"extraId": EXTRA}, catalog,  # type: ignore[arg-type]
                          pk.PackageOptions())


def test_an_extra_goes_into_its_titles_folder(binaries: Binaries, share: Share) -> None:
    x = ExtraShare(share)
    catalog = Catalog()
    run_extra(x.record(), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert sorted(p.name for p in x.extra_dir.iterdir()) == [
        ".complete", "checksums.sha256", "extra.json", "hls", "package.json"]
    assert library.verify_chain(x.extra_dir, "extra.json", rec.EXTRA_DIRS) is None
    assert not x.staging.exists() and not x.inbox.exists()
    assert sorted(p.name for p in share.item_dir.iterdir()) == ["extras"]

    doc = json.loads((x.extra_dir / "extra.json").read_text())
    assert {k: doc[k] for k in ("extraId", "kind", "title", "localizedTitles", "language",
                                "createdAt", "createdBy", "originalFiles", "originals")} == {
        "extraId": EXTRA, "kind": "trailer", "title": "Trailer",
        "localizedTitles": {"de": "Vorschau"}, "language": "en",
        "createdAt": "2026-10-06T08:00:00Z", "createdBy": "katalog-manager/api",
        "originalFiles": [], "originals": []}
    assert doc["packagedFrom"] == [{"name": "trailer.mov", "sizeBytes": x.file.stat().st_size,
                                    "fixity": {"qh1": rec.qh1(str(x.file))}}]
    assert doc["origin"] == {"kind": "link", "site": "video.example", "externalId": "t-0001",
                             "fetchedAt": "2026-10-05T08:00:00Z"}
    assert "seasonNumber" not in doc and doc["runtimeMs"] == 33000

    package_bytes = (x.extra_dir / "package.json").read_bytes()
    package = json.loads(package_bytes)
    assert package["trickplay"] is None and not package.get("trailers")
    [payload] = catalog.handovers
    assert payload == {"layout": "v2", "extraId": EXTRA, "extraDir": str(x.extra_dir),
                       "packageId": package["packageId"], "complete": rec.sha_bytes(package_bytes),
                       "package": package, "source": SOURCE_BLOCK}


def test_an_extra_in_place_already_is_reported_again(binaries: Binaries, share: Share) -> None:
    x = ExtraShare(share)
    catalog = Catalog(status=500)
    run_extra(x.record(), catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    placed = share.tree(x.extra_dir)
    catalog = Catalog()
    run_extra(x.record(), catalog)
    assert catalog.statuses == ["in_progress", "done"] and binaries.builds == 1
    assert share.tree(x.extra_dir) == placed


def test_an_extras_folder_that_is_not_whole(binaries: Binaries, share: Share) -> None:
    x = ExtraShare(share)
    (x.extra_dir / "hls").mkdir(parents=True)
    catalog = Catalog()
    run_extra(x.record(), catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert f"extras/{EXTRA} is there but not a whole extra" in catalog.error
    assert binaries.builds == 0


def test_an_extras_file_that_changed(binaries: Binaries, share: Share) -> None:
    x = ExtraShare(share)
    record = x.record()
    with open(x.file, "ab") as f:
        f.write(b"more")
    catalog = Catalog()
    run_extra(record, catalog)
    assert catalog.statuses == ["in_progress", "failed"]
    assert "the extra's file trailer.mov is" in catalog.error
    assert not x.extra_dir.exists()


def test_an_extras_handoff_left_in_the_package_store(binaries: Binaries, share: Share) -> None:
    x = ExtraShare(share)
    legacy = share.legacy_inbox / f"extra-{EXTRA}"
    _handoff(legacy)
    catalog = Catalog()
    run_extra(x.record(), catalog)
    assert catalog.statuses == ["in_progress", "done"]
    assert [v["id"] for v in catalog.handovers[0]["package"]["renditions"]["video"]] == [
        "v0", "v1"]
    assert not legacy.exists()


def test_an_extra_block_that_cant_be_worked_from(binaries: Binaries, share: Share) -> None:
    x = ExtraShare(share)
    record = x.record(extraDir=str(share.lib / "extras" / EXTRA))
    catalog = Catalog()
    run_extra(record, catalog)
    assert catalog.statuses == ["failed"]
    assert catalog.error.startswith("worker record: library.extraDir")
    assert binaries.builds == 0


def test_the_umask_writes_what_the_catalog_can_read(binaries: Binaries, share: Share) -> None:
    os.chmod(share.srt, 0o600)
    old = os.umask(0o002)
    try:
        run(share.record(), Catalog())
    finally:
        os.umask(old)
    assert (share.source_dir / "Sintel (2010).en.srt").stat().st_mode & 0o777 == 0o664
