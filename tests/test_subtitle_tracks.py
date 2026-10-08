"""The source's own subtitle tracks, extracted as sidecars: the PGS and
text tracks in one ffmpeg run over the source, an output each, instead of
one run — one full read of the file — per track; the image tracks ffmpeg
can't write (VobSub, DVB, and the bitmaps WebVTT can't be made of) in a
run each, as before; and when the one run fails, every track alone, as
before. The command lines without binaries; with ffmpeg, a source with
text, ASS, PGS, VobSub and DVB tracks gives the same files and records
both ways."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from packager import packager as pk

SRC = Path("/m/src.mkv")
HEAD = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-i", str(SRC)]


def _probe(*codecs: str) -> pk._Probe:
    return pk._Probe(
        container="matroska,webm", duration_ms=60_000,
        video={"index": 0, "codec_name": "hevc"}, video_index=0, audio=[],
        subtitles=[{"index": 1 + i, "codec_name": c, "tags": {"language": "eng", "title": f"t{i}"},
                    "disposition": {"forced": int(i == 1)}} for i, c in enumerate(codecs)])


class Ffmpeg:
    """Stand-in for subprocess.run: records every ffmpeg run, writes each
    output it names, and fails the runs `fails` says to (the one pass:
    "pass"; a track alone: its index), leaving what it wrote."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fails=()) -> None:
        self.runs: list[list[str]] = []
        self.fails = set(fails)
        monkeypatch.setattr(pk.subprocess, "run", self)

    def __call__(self, args: list[str], **kw):
        self.runs.append(args)
        outputs = [args[i + 1:] for i, a in enumerate(args) if a == "-map"]
        for out in outputs:
            target = Path(out[next(k for k, x in enumerate(out) if x.startswith("/"))])
            target.write_text(f"{target.name}\n")
        alone = len(outputs) == 1
        index = int(args[args.index("-map") + 1].rsplit(":", 1)[1])
        if ("pass" in self.fails and not alone) or (alone and index in self.fails):
            if kw.get("check"):
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 1, "", "Error opening output files")
        return subprocess.CompletedProcess(args, 0, "", "")


def _out(subs: Path, i: int, *options: str, name: str) -> list[str]:
    return ["-map", f"0:s:{i}", *options, str(subs / name)]


def test_every_text_and_pgs_track_is_one_run_over_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ffmpeg = Ffmpeg(monkeypatch)
    subs = tmp_path / "subs"
    entries = pk._extract_subtitles(
        SRC, _probe("subrip", "ass", "hdmv_pgs_subtitle", "mov_text", "webvtt", "subrip"), subs,
        visible_indices={0, 2, 3, 4, 5})
    assert ffmpeg.runs == [[
        *HEAD,
        *_out(subs, 0, "-c:s", "webvtt", name="0.vtt"),
        *_out(subs, 1, "-c:s", "webvtt", name="1.vtt"),
        *_out(subs, 2, "-c:s", "copy", "-f", "sup", name="2.sup"),
        *_out(subs, 3, "-c:s", "webvtt", name="3.vtt"),
        *_out(subs, 4, "-c:s", "webvtt", name="4.vtt"),
        *_out(subs, 5, "-c:s", "webvtt", name="5.vtt"),
    ]]
    assert entries == [
        {"id": f"sub{i}", "language": "eng", "title": f"t{i}", "default": False,
         "forced": i == 1, "visible": i != 1, "path": f"subs/{name}", "format": fmt}
        for i, (name, fmt) in enumerate([("0.vtt", "webvtt"), ("1.vtt", "webvtt"),
                                         ("2.sup", "pgs"), ("3.vtt", "webvtt"),
                                         ("4.vtt", "webvtt"), ("5.vtt", "webvtt")])]


def test_a_track_ffmpeg_cant_write_keeps_its_own_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # DVB (no format for .dvb), XSUB and teletext (bitmaps WebVTT can't be
    # made of) fail as ffmpeg sets its outputs up: in the one pass they'd
    # fail it for every track. VobSub is no ffmpeg run at all (mkvextract's,
    # not installed here).
    monkeypatch.setattr(pk.shutil, "which", lambda _name: None)
    ffmpeg = Ffmpeg(monkeypatch, fails={3, 4, 5})
    subs = tmp_path / "subs"
    entries = pk._extract_subtitles(
        SRC, _probe("subrip", "dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub",
                    "dvb_teletext"), subs)
    assert ffmpeg.runs == [
        HEAD + _out(subs, 0, "-c:s", "webvtt", name="0.vtt")
        + _out(subs, 2, "-c:s", "copy", "-f", "sup", name="2.sup"),
        # ... each exactly as the run it always had.
        HEAD + _out(subs, 3, "-c:s", "copy", name="3.dvb"),
        HEAD + _out(subs, 4, "-c:s", "webvtt", name="4.vtt"),
        HEAD + _out(subs, 5, "-c:s", "webvtt", name="5.vtt"),
    ]
    assert [(e["id"], e["path"], e["format"]) for e in entries] == [
        ("sub0", "subs/0.vtt", "webvtt"), ("sub2", "subs/2.sup", "pgs")]


# ------------------------------------------------------------- VobSub

def _ident(*codec_ids: str, container: str = "Matroska") -> dict:
    """mkvmerge -J of a source with a video track and these subtitle tracks."""
    tracks = [{"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
               "properties": {"codec_id": "V_MPEGH/ISO/HEVC"}}]
    tracks += [{"id": 1 + i, "type": "subtitles", "codec": c.split("/")[-1],
                "properties": {"codec_id": c}} for i, c in enumerate(codec_ids)]
    return {"container": {"recognized": True, "type": container}, "tracks": tracks}


class Mkvtoolnix:
    """Stand-in for subprocess.run and shutil.which with mkvtoolnix on PATH
    (none when not `installed`): mkvmerge -J answers `ident`; mkvextract
    writes the .idx/.sub pair of every track it is asked for but those whose
    sidecar index is in `missing`, and exits `code`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, ident: dict, *, missing=(), code=0,
                 installed: bool = True) -> None:
        self.ident, self.missing, self.code = ident, set(missing), code
        self.runs: list[list[str]] = []
        monkeypatch.setattr(pk.shutil, "which",
                            lambda name: f"/usr/bin/{name}" if installed else None)
        monkeypatch.setattr(pk.subprocess, "run", self)

    def __call__(self, args: list[str], **kw):
        self.runs.append(args)
        if args[0] == "mkvmerge":
            return subprocess.CompletedProcess(args, 0, json.dumps(self.ident), "")
        assert args[0] == "mkvextract", args
        for spec in args[3:]:
            dest = Path(spec.split(":", 1)[1])
            if int(dest.stem) not in self.missing:
                dest.write_bytes(b"\x00\x00\x01\xba")
                dest.with_suffix(".idx").write_text(
                    "# VobSub index file, v7 (do not modify this line!)\n")
        return subprocess.CompletedProcess(args, self.code, "", "warning" if self.code else "")


def test_vobsub_tracks_are_one_mkvextract_run_over_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The sidecars' indices are the tracks' places among the subtitle
    # tracks; mkvextract takes Matroska track ids (here one more, after
    # the video).
    tools = Mkvtoolnix(monkeypatch, _ident("S_VOBSUB", "S_VOBSUB", "S_VOBSUB"))
    subs = tmp_path / "subs"
    entries = pk._extract_subtitles(
        SRC, _probe("dvd_subtitle", "dvd_subtitle", "dvd_subtitle"), subs)
    assert tools.runs == [
        ["mkvmerge", "-J", str(SRC)],
        ["mkvextract", str(SRC), "tracks",
         f"1:{subs / '0.sub'}", f"2:{subs / '1.sub'}", f"3:{subs / '2.sub'}"],
    ]
    assert [(e["id"], e["path"], e["format"], e["forced"]) for e in entries] == [
        ("sub0", "subs/0.idx", "vobsub", False), ("sub1", "subs/1.idx", "vobsub", True),
        ("sub2", "subs/2.idx", "vobsub", False)]
    assert sorted(p.name for p in subs.iterdir()) == [
        "0.idx", "0.sub", "1.idx", "1.sub", "2.idx", "2.sub"]


@pytest.mark.parametrize(("ident", "said"), [
    (_ident("S_VOBSUB", container="MPEG transport stream"),
     "VobSub is extracted from a Matroska source only, not MPEG transport stream"),
    ({"container": {"recognized": True, "type": "Matroska"}, "tracks": []},
     "mkvmerge lists no track 1"),
    (_ident("S_HDMV/PGS"), "track 1 is S_HDMV/PGS to mkvmerge, no VobSub"),
    ({**_ident(), "tracks": [*_ident()["tracks"], {"id": 1, "type": "subtitles", "codec": "VobSub",
                                                   "properties": {"codec_id": "S_VOBSUB",
                                                                  "language": "ger"}}]},
     "track 1 is ger to mkvmerge, eng to ffprobe"),
])
def test_a_vobsub_track_mkvextract_cant_be_matched_to_is_not_extracted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ident: dict, said: str,
) -> None:
    tools = Mkvtoolnix(monkeypatch, ident)
    subs = tmp_path / "subs"
    with capture_logs() as logs:
        entries = pk._extract_subtitles(SRC, _probe("dvd_subtitle"), subs)
    assert entries == []
    assert tools.runs == [["mkvmerge", "-J", str(SRC)]]
    assert [(e["event"], e["idx"], e["error"]) for e in logs
            if e["event"] == "packager.subs.failed"] == [("packager.subs.failed", 0, said)]


def test_a_vobsub_track_mkvextract_did_not_write_costs_that_track_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    tools = Mkvtoolnix(monkeypatch, _ident("S_VOBSUB", "S_VOBSUB"), missing={1}, code=1)
    subs = tmp_path / "subs"
    with capture_logs() as logs:
        entries = pk._extract_subtitles(SRC, _probe("dvd_subtitle", "dvd_subtitle"), subs)
    assert len(tools.runs) == 2
    assert [(e["id"], e["path"]) for e in entries] == [("sub0", "subs/0.idx")]
    assert sorted(p.name for p in subs.iterdir()) == ["0.idx", "0.sub"]
    assert [e["idx"] for e in logs if e["event"] == "packager.subs.failed"] == [1]


def test_without_mkvtoolnix_a_vobsub_track_is_said_to_be_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    tools = Mkvtoolnix(monkeypatch, _ident("S_VOBSUB"), installed=False)
    with capture_logs() as logs:
        assert pk._extract_subtitles(SRC, _probe("dvd_subtitle"), tmp_path / "subs") == []
    assert tools.runs == []
    assert [e["error"] for e in logs if e["event"] == "packager.subs.failed"] == [
        "mkvextract is not installed"]


def test_a_one_pass_that_fails_extracts_every_track_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # A track ffmpeg can't convert fails the pass for all of them: then
    # each is extracted alone, as before, and only that track is lost. What
    # the failed pass wrote goes first.
    ffmpeg = Ffmpeg(monkeypatch, fails={"pass", 1})
    subs = tmp_path / "subs"
    seen: list[list[str]] = []
    real = ffmpeg.__call__

    def watch(args, **kw):
        seen.append(sorted(p.name for p in subs.iterdir()))
        return real(args, **kw)

    monkeypatch.setattr(pk.subprocess, "run", watch)
    entries = pk._extract_subtitles(SRC, _probe("subrip", "arib_caption", "hdmv_pgs_subtitle"),
                                    subs)
    assert ffmpeg.runs == [
        HEAD + _out(subs, 0, "-c:s", "webvtt", name="0.vtt")
        + _out(subs, 1, "-c:s", "webvtt", name="1.vtt")
        + _out(subs, 2, "-c:s", "copy", "-f", "sup", name="2.sup"),
        HEAD + _out(subs, 0, "-c:s", "webvtt", name="0.vtt"),
        HEAD + _out(subs, 1, "-c:s", "webvtt", name="1.vtt"),
        HEAD + _out(subs, 2, "-c:s", "copy", "-f", "sup", name="2.sup"),
    ]
    assert seen[1] == []        # the failed pass's files are gone before the first run alone
    assert [e["id"] for e in entries] == ["sub0", "sub2"]


def test_one_track_is_extracted_as_it_always_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ffmpeg = Ffmpeg(monkeypatch)
    subs = tmp_path / "subs"
    [entry] = pk._extract_subtitles(SRC, _probe("hdmv_pgs_subtitle"), subs)
    assert ffmpeg.runs == [HEAD + _out(subs, 0, "-c:s", "copy", "-f", "sup", name="0.sup")]
    assert (entry["path"], entry["format"]) == ("subs/0.sup", "pgs")
    assert pk._extract_subtitles(SRC, _probe(), subs) == [] and len(ffmpeg.runs) == 1


def test_a_pass_that_left_an_output_unwritten_counts_as_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    subs = tmp_path / "subs"
    subs.mkdir()
    runs: list[list[str]] = []

    def run(args, **_kw):
        runs.append(args)
        if args.count("-map") == 1:
            target = Path(args[-1])
            target.write_text("x")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(pk.subprocess, "run", run)
    entries = pk._extract_subtitles(SRC, _probe("subrip", "subrip"), subs)
    assert len(runs) == 3 and [e["id"] for e in entries] == ["sub0", "sub1"]


# ------------------------------------------------------------- with ffmpeg

def _encoders() -> str:
    if shutil.which("ffmpeg") is None:
        return ""
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffprobe") is None
    or not all(e in _encoders() for e in ("libx264", "dvdsub", "dvbsub")),
    reason="needs ffmpeg (libx264, dvdsub, dvbsub) and ffprobe on PATH")


def _pgs(path: Path) -> Path:
    """A tiny PGS stream (.sup): a 16x8 white box shown at 1 s and
    cleared at 3 s — one display set of PCS, WDS, PDS, ODS and END, then
    one of PCS, WDS and END."""

    def segment(seconds: float, kind: int, payload: bytes) -> bytes:
        t = int(seconds * 90_000)
        return b"PG" + struct.pack(">IIBH", t, t, kind, len(payload)) + payload

    def display(seconds: float, shown: bool) -> bytes:
        objects = struct.pack(">HBBHH", 0, 0, 0, 10, 10) if shown else b""
        pcs = struct.pack(">HHBHBBBB", 320, 240, 0x10, 1 if shown else 2,
                          0x80 if shown else 0x00, 0, 0, int(shown)) + objects
        out = segment(seconds, 0x16, pcs) + segment(seconds, 0x17,
                                                    struct.pack(">BBHHHH", 1, 0, 10, 10, 16, 8))
        if shown:
            rle = b"\x00\x90\x01\x00\x00" * 8          # 8 lines of 16 pixels of colour 1
            out += segment(seconds, 0x14, bytes([0, 0, 1, 235, 128, 128, 255]))
            out += segment(seconds, 0x15, struct.pack(">HBB", 0, 0, 0xC0)
                           + (4 + len(rle)).to_bytes(3, "big") + struct.pack(">HH", 16, 8) + rle)
        return out + segment(seconds, 0x80, b"")

    path.write_bytes(display(1.0, True) + display(3.0, False))
    return path


@pytest.fixture(scope="module")
def tracks(tmp_path_factory) -> Path:
    """A source with SRT, ASS, PGS, VobSub, DVB and a forced SRT track."""
    d = tmp_path_factory.mktemp("tracks")
    (d / "en.srt").write_text("1\n00:00:01,000 --> 00:00:02,500\nHello.\n\n"
                              "2\n00:00:03,000 --> 00:00:04,000\nAgain.\n")
    (d / "de.ass").write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
        "Style: Default,Arial,20\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
        "MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,"
        "Hallo {\\i1}Welt{\\i0}\n")
    pgs = _pgs(d / "box.sup")
    src = d / "tracks.mkv"
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=5",
        "-i", str(d / "en.srt"), "-i", str(d / "de.ass"), "-i", str(pgs), "-i", str(pgs),
        "-i", str(pgs),
        "-map", "0:v", "-map", "1", "-map", "2", "-map", "3", "-map", "4", "-map", "5", "-map", "1",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:s:0", "srt", "-c:s:1", "ass", "-c:s:2", "copy", "-c:s:3", "dvdsub", "-c:s:4", "dvbsub",
        "-c:s:5", "srt", "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=ger",
        "-metadata:s:s:2", "language=eng", "-metadata:s:s:3", "language=fre",
        "-metadata:s:s:4", "language=ita", "-metadata:s:s:5", "language=eng",
        "-metadata:s:s:5", "title=Forced", "-disposition:s:5", "forced", str(src)], check=True)
    return src


def _files(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())}


@needs_ffmpeg
def test_one_pass_writes_what_a_run_per_track_wrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tracks: Path,
) -> None:
    probe = pk._ffprobe(tracks)
    assert [s["codec_name"] for s in probe.subtitles] == [
        "subrip", "ass", "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "subrip"]
    real = subprocess.run
    runs: list[int] = []

    def run(args, **kw):
        if args[0] == "ffmpeg":
            runs.append(args.count("-map"))
        return real(args, **kw)

    monkeypatch.setattr(pk.subprocess, "run", run)
    # VobSub is mkvextract's (test_mkvextract_writes_the_vobsub_pair).
    monkeypatch.setattr(pk.shutil, "which", lambda _name: None)
    one = pk._extract_subtitles(tracks, probe, tmp_path / "one")
    # The source is read once for its PGS and text tracks; DVB keeps its
    # attempt, which fails as ffmpeg sets it up, as always.
    assert runs == [4, 1]

    # As before the one pass: every track in a run of its own.
    monkeypatch.setattr(pk, "_extract_together", lambda *_a: None)
    runs.clear()
    alone = pk._extract_subtitles(tracks, probe, tmp_path / "alone")
    assert runs == [1] * 5
    assert one == alone
    assert [(e["path"], e["format"], e["forced"]) for e in one] == [
        ("subs/0.vtt", "webvtt", False), ("subs/1.vtt", "webvtt", False),
        ("subs/2.sup", "pgs", False), ("subs/5.vtt", "webvtt", True)]
    assert _files(tmp_path / "one") == _files(tmp_path / "alone")
    assert sorted(_files(tmp_path / "one")) == ["0.vtt", "1.vtt", "2.sup", "5.vtt"]
    files = _files(tmp_path / "one")
    assert b"Hello." in files["0.vtt"] and b"Welt" in files["1.vtt"]
    assert files["2.sup"].startswith(b"PG") and files["2.sup"].count(b"PG") >= 8


needs_mkvtoolnix = pytest.mark.skipif(
    shutil.which("mkvmerge") is None or shutil.which("mkvextract") is None,
    reason="needs mkvmerge and mkvextract on PATH")


@needs_ffmpeg
@needs_mkvtoolnix
def test_mkvextract_writes_the_vobsub_pair(tmp_path: Path, tracks: Path) -> None:
    probe = pk._ffprobe(tracks)
    entries = pk._extract_subtitles(tracks, probe, tmp_path / "subs")
    assert [(e["id"], e["path"], e["format"], e["language"]) for e in entries
            if e["format"] == "vobsub"] == [("sub3", "subs/3.idx", "vobsub", "fre")]
    idx = (tmp_path / "subs" / "3.idx").read_text()
    assert idx.startswith("# VobSub index file") and "timestamp: " in idx
    # .sub is an MPEG program stream: a pack header first
    assert (tmp_path / "subs" / "3.sub").read_bytes()[:4] == b"\x00\x00\x01\xba"
    assert sorted(p.name for p in (tmp_path / "subs").iterdir()) == [
        "0.vtt", "1.vtt", "2.sup", "3.idx", "3.sub", "5.vtt"]
