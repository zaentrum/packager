"""HDR10 static metadata through the packager, with real ffmpeg and
shaka-packager runs (skipped unless both are on PATH, ffmpeg has libx265
and it makes the films below as they are meant to be).

shaka-packager writes every init segment's sample entry with its decoder
configuration and its colour alone, so the renditions of an HDR10 original
lost its mastering display colour volume and content light level. Here an
HDR10 original — the metadata in its container and in its bitstream — is
packaged through the library v2 path as a copy and on a transcoder's
ladder whose rungs carry none of it: the init segment of every HEVC rung
in PQ holds the original's mdcv and clli boxes, FFmpeg's byte for byte,
and nothing else of it moved; ffprobe reads them back from the rendition,
every frame decodes, and shaka-packager reads the rendition again.
package.json says so per rung, its essence has the metadata, and deleting
the original no longer costs it, where the encode's package lost it
before. An original with the metadata in its bitstream alone gives its
first frames'. The init segment of an SDR rung, of an HLG original and of
a PQ original without the metadata is shaka-packager's, byte for byte."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_hdr10 import CLLI, MDCV
from test_library_real import Catalog, _catalogs_part, _has_x265, _validate

from packager import hdr10, worker
from packager import libv2_records as rec
from packager import packager as pk
from packager.katalog import ClaimedItem

pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe", "packager")) or not _has_x265(),
    reason="needs ffmpeg/ffprobe with libx265 and shaka-packager on PATH",
)

ITEM = "c0ffee00-0000-4000-8000-0000000000e1"
SOURCE = "5011ce00-0000-4000-8000-0000000000e1"
VERSION = "7e510000-0000-4000-8000-0000000000e1"

X265 = "log-level=error:keyint=48:min-keyint=48:scenecut=0"
# The HDR10 static metadata the films are mastered with: BT.2020's
# primaries, D65, 0.0001-1000 cd/m², MaxCLL 1404, MaxFALL 1000 — the
# values of CLLI and MDCV.
SEI = ("master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):"
       "max-cll=1404,1000:hdr10=1")
PQ = "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv"
HLG = "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc:range=tv"
SDR = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
# A rung as a GPU encode leaves it: neither the container nor the
# bitstream says anything of the metadata.
BARE = ("sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA,"
        "sidedata=mode=delete:type=CONTENT_LIGHT_LEVEL")
NO_SEI = ["-bsf:v", "filter_units=remove_types=39"]
MASTERING, LIGHT = "Mastering display metadata", "Content light level metadata"


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _probe(path: Path, *args: str) -> dict:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-of", "json", *args, str(path)],
        capture_output=True, text=True, check=True).stdout)


def _stream(path: Path) -> dict:
    return _probe(path, "-show_streams")["streams"][0]


def _side_data(entries: list[dict] | None) -> list[str]:
    return sorted(x.get("side_data_type", "") for x in entries or []
                  if x.get("side_data_type") in (MASTERING, LIGHT))


def _in_frames(path: Path) -> list[str]:
    frames = _probe(path, "-read_intervals", "%+#5", "-show_frames", "-show_entries",
                    "frame=side_data_list")["frames"]
    return sorted({t for f in frames for t in _side_data(f.get("side_data_list"))})


@pytest.fixture(scope="module")
def films(tmp_path_factory) -> dict[str, Path]:
    """4 s 10-bit HEVC films with an English stereo track, each in its
    arrival folder: HDR10 (the metadata in the container and the
    bitstream), SEI (in the bitstream alone), BARE (PQ, without it), HLG
    and SDR. Skips where this ffmpeg makes them otherwise."""
    d = tmp_path_factory.mktemp("hdr10")
    out: dict[str, Path] = {}

    def film(name: str, *args: str) -> None:
        path = d / f"{name} (2024)" / f"{name} (2024).mkv"
        path.parent.mkdir()
        _ff(*args, "-c:a", "aac", "-ac", "2", "-metadata:s:a:0", "language=eng", str(path))
        out[name] = path

    picture = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=4",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
               "-map", "0:v", "-map", "1:a", "-c:v", "libx265", "-preset", "ultrafast"]
    film("SEI", *picture, "-vf", f"{PQ},format=yuv420p10le", "-x265-params", f"{X265}:{SEI}")
    # Encoded again from frames that carry it, the metadata goes into the
    # container too.
    film("HDR10", "-i", str(out["SEI"]), "-map", "0", "-c:v", "libx265", "-preset", "ultrafast",
         "-x265-params", X265)
    film("BARE", *picture, "-vf", f"{PQ},format=yuv420p10le", "-x265-params", X265)
    film("HLG", *picture, "-vf", f"{HLG},format=yuv420p10le", "-x265-params", X265)
    film("SDR", *picture, "-vf", f"{SDR},format=yuv420p10le", "-x265-params", X265)
    made = {name: (_stream(path).get("color_transfer"), _side_data(_stream(path).get(
        "side_data_list")), _in_frames(path)) for name, path in out.items()}
    want = {"HDR10": ("smpte2084", [LIGHT, MASTERING], [LIGHT, MASTERING]),
            "SEI": ("smpte2084", [], [LIGHT, MASTERING]), "BARE": ("smpte2084", [], []),
            "HLG": ("arib-std-b67", [], []), "SDR": ("bt709", [], [])}
    if made != want:
        pytest.skip(f"this ffmpeg makes other films than these tests need: {made}")
    return out


def _ladder(original: Path, inbox: Path) -> None:
    """A transcoder's handoff on a ladder of three encoded rungs, none of
    which carries the metadata: v0 at the original's size and v1 at half of
    it in PQ, v2 tone-mapped to SDR (its colour said so; the pixels are the
    test's least concern)."""
    inbox.mkdir(parents=True)
    _ff("-i", str(original), "-map", "0:v", "-map", "0:a", "-vf", BARE, "-c:v", "libx265",
        "-preset", "ultrafast", "-x265-params", X265, *NO_SEI, "-c:a", "copy",
        "-f", "matroska", str(inbox / "prepared.mkv"))
    _ff("-i", str(original), "-map", "0:v", "-vf", f"{BARE},scale=160:120,setsar=1",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", X265, *NO_SEI, "-an",
        "-f", "matroska", str(inbox / "v1.mkv"))
    _ff("-i", str(original), "-map", "0:v", "-vf", f"{BARE},{SDR},scale=160:120,setsar=1",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", X265, *NO_SEI, "-an",
        "-f", "matroska", str(inbox / "v2.mkv"))
    for name in ("prepared.mkv", "v1.mkv", "v2.mkv"):
        stream = _stream(inbox / name)
        assert _side_data(stream.get("side_data_list")) == _in_frames(inbox / name) == []
    (inbox / "renditions.json").write_text(json.dumps({
        "version": 1, "itemId": ITEM, "segmentSeconds": 6, "keyframes": "interval",
        "timestampOffset": 0.0,
        "video": [{"id": "v0", "label": "source", "file": "prepared.mkv", "mode": "encode",
                   "encoder": "hevc_nvenc"},
                  {"id": "v1", "label": "120p", "file": "v1.mkv", "mode": "encode",
                   "encoder": "hevc_nvenc"},
                  {"id": "v2", "label": "120p SDR", "file": "v2.mkv", "mode": "encode",
                   "encoder": "hevc_nvenc"}]}))


def _v2(tmp_path: Path, film: Path, *, ladder: bool = False) -> tuple[Path, Path, dict, dict]:
    """One v2 run of film through the worker, on the transcoder's ladder or
    from the original: the library root, the version folder, the package
    record and the source record."""
    lib, work = tmp_path / "library", tmp_path / "work"
    arrival = work / "incoming" / film.parent.name
    shutil.copytree(film.parent, arrival)
    original = arrival / film.name
    item_dir = lib / "movies" / ITEM[:2] / ITEM
    record = {
        "id": ITEM, "type": "movie", "title": film.stem, "year": 2024, "path": str(original),
        "library": {
            "contract": 1, "root": str(lib), "itemDir": str(item_dir), "blocked": None,
            "source": {"sourceId": SOURCE, "recorded": False,
                       "recordDir": str(item_dir / "sources" / SOURCE),
                       "libraryPath": f"{film.parent.name}/{film.name}",
                       "sizeBytes": original.stat().st_size, "qh1": rec.qh1(str(original))},
            "inboxDir": str(work / "inbox" / ITEM),
            "build": {"versionId": VERSION, "stagingDir": str(work / "staging" / VERSION),
                      "versionDir": str(item_dir / "versions" / VERSION),
                      "createdBy": "katalog-manager", "chapters": None, "chaptersFrom": None,
                      "segments": None},
            "current": None,
        },
    }
    if ladder:
        _ladder(original, work / "inbox" / ITEM)
    catalog = Catalog()
    worker._process_one(ClaimedItem.from_json(record), catalog,  # type: ignore[arg-type]
                        pk.PackageOptions(surround_codec="off", stereo_loudness=None))
    assert [s for s, _ in catalog.written] == ["in_progress", "done"], catalog.written
    vdir = item_dir / "versions" / VERSION
    package = json.loads((vdir / "package.json").read_text())
    source = json.loads((item_dir / "sources" / SOURCE / "source.json").read_text())
    assert catalog.handovers[0]["package"] == package
    assert rec.chain_problems(str(vdir), "version.json", rec.PACKAGE_DIRS, full=True) == []
    _catalogs_part(item_dir, title=film.stem)
    return lib, vdir, package, source


def _without_the_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """The packager as it was: no rung gets the boxes."""
    monkeypatch.setattr(pk, "_hdr10_metadata", lambda *a, **kw: None)


def _shakas(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """The init segment of every video rung as shaka-packager wrote it, by
    rung, from the run's media run on (it writes the time into each, so no
    other run's are these)."""
    written: dict[str, bytes] = {}
    run = pk._run_shaka

    def keeping(cmd: list[str], cwd: Path, label: str) -> None:
        run(cmd, cwd, label)
        if label == "media":
            written.update({p.parent.name: p.read_bytes()
                            for p in sorted(Path(cwd).glob("hls/v*/init.mp4"))})

    monkeypatch.setattr(pk, "_run_shaka", keeping)
    return written


def _children(init: bytes) -> list[tuple[bytes, bytes]]:
    """The boxes of an MP4's visual sample entry, (type, bytes) each."""
    start, end = 0, len(init)
    for kind in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"):
        [(pos, header, size)] = [(p, h, s) for p, h, s, k in hdr10._boxes(init, start, end)
                                 if k == kind]
        start, end = pos + header, pos + size
    [(pos, header, size, _kind)] = list(hdr10._boxes(init, start + 8, end))
    return [(k, init[p:p + s]) for p, _h, s, k in hdr10._boxes(init, pos + header + 78, pos + size)]


def _ffmpegs_boxes(original: Path, tmp_path: Path) -> bytes:
    """The clli and mdcv boxes FFmpeg's MP4 muxer writes for the original's
    video stream, in its order."""
    mp4 = tmp_path / "ffmpeg.mp4"
    _ff("-i", str(original), "-map", "0:v", "-c", "copy", "-tag:v", "hvc1", "-t", "1", str(mp4))
    return b"".join(b for k, b in _children(mp4.read_bytes()) if k in (b"clli", b"mdcv"))


def _played(rendition: Path, tmp_path: Path) -> tuple[bytes | None, int]:
    """The rendition, its init and segments as one file: the boxes of the
    metadata ffprobe reads from its stream (None: none), and the frames
    ffmpeg decodes of it, failing on any error; shaka-packager reads it
    again."""
    joined = tmp_path / f"{rendition.name}.mp4"
    joined.write_bytes((rendition / "init.mp4").read_bytes() + b"".join(
        s.read_bytes() for s in sorted(rendition.glob("seg-*.m4s"))))
    found = hdr10.from_side_data(_stream(joined).get("side_data_list"))
    frames = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(joined), "-map", "0:v",
         "-f", "framemd5", "-"], capture_output=True, text=True, check=True).stdout
    again = tmp_path / f"{rendition.name}-again"
    subprocess.run(["packager", f"in={joined},stream=video,output={again}.mp4"],
                   capture_output=True, check=True, cwd=tmp_path)
    return (found.boxes() if found else None,
            sum(1 for line in frames.splitlines() if line and not line.startswith("#")))


def test_a_copy_carries_the_originals_metadata_in_its_init_segment(
    tmp_path: Path, films: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffmpegs = _ffmpegs_boxes(films["HDR10"], tmp_path)
    assert ffmpegs == CLLI + MDCV
    shakas = _shakas(monkeypatch)
    lib, vdir, package, source = _v2(tmp_path, films["HDR10"])
    [v0] = package["renditions"]["video"]
    assert (v0["encoder"], v0["videoRange"], v0.get("hdr10Metadata")) == ("copy", "PQ", True)
    init = (vdir / "hls" / "v0" / "init.mp4").read_bytes()
    # FFmpeg's boxes, where FFmpeg puts them: after colr; nothing else of
    # shaka-packager's init segment moved.
    assert [k for k, _ in _children(shakas["v0"])] == [b"hvcC", b"colr"]
    assert [k for k, _ in _children(init)] == [b"hvcC", b"colr", b"clli", b"mdcv"]
    assert hdr10.insert_boxes(shakas["v0"], ffmpegs) == init
    assert _played(vdir / "hls" / "v0", tmp_path) == (CLLI + MDCV, 96)
    assert source["essence"]["hdr10Metadata"] is package["essence"]["hdr10Metadata"] is True
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []
    _validate(lib, hdr10=True)


def test_every_pq_rung_of_an_encode_carries_the_originals_metadata(
    tmp_path: Path, films: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    shakas = _shakas(monkeypatch)
    lib, vdir, package, source = _v2(tmp_path / "now", films["HDR10"], ladder=True)
    assert [(v["id"], v["encoder"], v["videoRange"], v.get("hdr10Metadata"))
            for v in package["renditions"]["video"]] == [
        ("v0", "hevc_nvenc", "PQ", True), ("v1", "hevc_nvenc", "PQ", True),
        ("v2", "hevc_nvenc", "SDR", None)]
    for rung in ("v0", "v1"):
        init = (vdir / "hls" / rung / "init.mp4").read_bytes()
        assert hdr10.insert_boxes(shakas[rung], CLLI + MDCV) == init, rung
        assert [k for k, _ in _children(init)] == [b"hvcC", b"colr", b"clli", b"mdcv"], rung
        assert _played(vdir / "hls" / rung, tmp_path)[0] == CLLI + MDCV, rung
    # The SDR rung's init segment is shaka-packager's, byte for byte.
    assert (vdir / "hls" / "v2" / "init.mp4").read_bytes() == shakas["v2"]
    assert _played(vdir / "hls" / "v2", tmp_path)[0] is None
    # The package keeps the metadata its original had: deleting it costs
    # nothing.
    assert package["essence"]["hdr10Metadata"] is True
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []
    assert "dynamic-range" not in [x["kind"] for x in package["fidelity"]["losses"]]
    _validate(lib, hdr10=True)
    # As it was, the encode's package lost it.
    _without_the_metadata(monkeypatch)
    _lib, _before, old, _source = _v2(tmp_path / "before", films["HDR10"], ladder=True)
    assert [v.get("hdr10Metadata") for v in old["renditions"]["video"]] == [None, None, None]
    assert rec.deletion_gate([source["essence"]], old["essence"]) == ["hdr10Metadata"]


def test_metadata_in_the_bitstream_alone_is_read_from_the_first_frames(
    tmp_path: Path, films: dict[str, Path],
) -> None:
    lib, vdir, package, source = _v2(tmp_path, films["SEI"])
    assert package["renditions"]["video"][0].get("hdr10Metadata") is True
    assert _played(vdir / "hls" / "v0", tmp_path)[0] == CLLI + MDCV
    # The source record reads a stream's metadata from its container: this
    # one has none to lose, and the package's essence claims none.
    assert source["essence"]["hdr10Metadata"] is package["essence"]["hdr10Metadata"] is False
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []
    _validate(lib, hdr10=True)


@pytest.mark.parametrize("name", ["SDR", "HLG", "BARE"])
def test_without_hdr10_metadata_the_init_segment_is_shakas(
    tmp_path: Path, films: dict[str, Path], monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    shakas = _shakas(monkeypatch)
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path)
    manifest = pk.package_item(ITEM, str(films[name]), "movie", title=name, trickplay=False,
                               options=pk.PackageOptions(surround_codec="off",
                                                         stereo_loudness=None))
    init = (tmp_path / "movies" / ITEM[:2] / ITEM / "hls" / "v0" / "init.mp4").read_bytes()
    assert init == shakas["v0"]
    assert [k for k, _ in _children(init)] == [b"hvcC", b"colr"]
    assert "hdr10Metadata" not in manifest["renditions"]["video"][0]
