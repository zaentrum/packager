"""The fonts an original carries as attachments, taken into its package as
their bytes are: what each is by its bytes (fonts.font_format), the names a
styled subtitle asks for it by (fonts.font_names), and the one ffmpeg run
that takes them out of the original (packager._extract_fonts) — its
command line and what it keeps, with stand-ins; with ffmpeg, a Matroska
file whose fonts come out byte for byte, its cover and its text file not at
all. The fonts are made here (fontmaker.py)."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import fontmaker
import pytest
from structlog.testing import capture_logs

from packager import fonts
from packager import packager as pk

SANS = fontmaker.truetype("Example Sans")
SERIF_BOLD = fontmaker.truetype("Example Serif", "Bold")


# ------------------------------------------------------------- the bytes

@pytest.mark.parametrize(("data", "fmt"), [
    (SANS, "ttf"),
    (b"true" + SANS[4:], "ttf"),
    (fontmaker.opentype_header("Example Display"), "otf"),
    (fontmaker.collection(("Example Sans", "Regular"), ("Example Sans", "Italic")), "ttc"),
    (b"wOFF\x00\x01\x00\x00" + bytes(40), "woff"),
    (b"wOF2" + bytes(44), "woff2"),
    (b"\xff\xd8\xff\xe0 a picture", None),
    (b"", None),
])
def test_a_font_is_what_its_first_bytes_say(data: bytes, fmt: str | None) -> None:
    assert fonts.font_format(data) == fmt


def test_a_font_answers_to_its_family_full_and_postscript_names() -> None:
    assert fonts.font_names(SANS) == ["Example Sans", "Example Sans Regular", "ExampleSans-Regular"]
    assert fonts.font_names(SERIF_BOLD) == [
        "Example Serif", "Example Serif Bold", "ExampleSerif-Bold"]
    assert fonts.font_names(fontmaker.opentype_header("Example Display")) == [
        "Example Display", "Example Display Regular", "ExampleDisplay-Regular"]


def test_a_collection_answers_to_every_faces_names_once() -> None:
    ttc = fontmaker.collection(("Example Sans", "Regular"), ("Example Sans", "Italic"))
    assert fonts.font_names(ttc) == [
        "Example Sans", "Example Sans Regular", "ExampleSans-Regular",
        "Example Sans Italic", "ExampleSans-Italic"]


def test_a_font_with_macintosh_names_alone_answers_to_its_family_and_postscript_name() -> None:
    # libass reads the Windows platform's family and full names, and
    # FreeType's family name where there is none: of a Macintosh one, the
    # family; the PostScript name from either.
    assert fonts.font_names(fontmaker.truetype("Example Mac", mac_names=True)) == [
        "Example Mac", "ExampleMac-Regular"]


def test_names_in_another_mac_script_or_of_no_text_are_none() -> None:
    table = struct.pack(">3H", 0, 3, 6 + 36)
    strings = b""
    for platform, encoding, name_id, raw in (
            (1, 1, 1, b"\x82\xa0"), (3, 1, 1, b"\x00 \x00\x00"),
            (3, 1, 4, "Example Sans  Regular\n".encode("utf-16-be"))):
        table += struct.pack(">6H", platform, encoding, 0, name_id, len(raw), len(strings))
        strings += raw
    font = struct.pack(">I4H", 0x00010000, 1, 16, 0, 0) + struct.pack(">4s3I", b"name", 0, 28,
                                                                         len(table + strings))
    assert fonts.font_names(font + table + strings) == ["Example Sans Regular"]


@pytest.mark.parametrize("data", [
    SANS[:20],                                         # its tables cut off
    SANS[:148] + b"\xff" * 8 + SANS[156:],            # its name table where no byte is
    b"\x00\x01\x00\x00" + struct.pack(">H", 0xFFFF),  # tables it doesn't hold
    b"ttcf\x00\x01\x00\x00\xff\xff\xff\xff",          # faces it doesn't hold
    b"wOFF" + bytes(60),
    b"not a font at all",
    b"",
])
def test_bytes_that_are_no_font_give_no_names_and_no_error(data: bytes) -> None:
    assert fonts.font_names(data) == []


# ------------------------------------------------------ taken out, with stand-ins

ORIGINAL = Path("/m/original.mkv")


def _attachment(index: int, filename: str | None, mimetype: str | None, size: int | None = 100,
                codec: str | None = None) -> dict:
    tags = {k: v for k, v in (("filename", filename), ("mimetype", mimetype)) if v}
    return {"index": index, "codec_type": "attachment", "tags": tags,
            **({"codec_name": codec} if codec else {}),
            **({"extradata_size": size} if size is not None else {})}


class Tools:
    """Stand-ins for ffprobe (it answers `streams`) and ffmpeg (it writes
    `data` for each -dump_attachment it is given, but those whose index is
    in `short`, of which it writes half, and exits `code`)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, streams: list[dict],
                 data: dict[int, bytes], *, short=(), code=0) -> None:
        self.streams, self.data, self.short, self.code = streams, data, set(short), code
        self.runs: list[list[str]] = []
        monkeypatch.setattr(pk.subprocess, "run", self)

    def __call__(self, args: list[str], **kw):
        self.runs.append(args)
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, json.dumps({"streams": self.streams}), "")
        for i, a in enumerate(args):
            if a.startswith("-dump_attachment:"):
                index = int(a.split(":")[1])
                body = self.data[index]
                half = body[:len(body) // 2]
                Path(args[i + 1]).write_bytes(half if index in self.short else body)
        return subprocess.CompletedProcess(args, self.code, "", "warning" if self.code else "")


def test_every_font_attachment_is_one_run_over_the_originals_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    otf = fontmaker.opentype_header("Example Display")
    streams = [
        _attachment(3, "Example Sans.ttf", "application/x-truetype-font", len(SANS), "ttf"),
        _attachment(4, "cover.jpg", "image/jpeg"),
        _attachment(5, "Example Serif Bold.TTF", "font/ttf", len(SERIF_BOLD)),
        _attachment(6, "notes.txt", "text/plain"),
        _attachment(7, "display.otf", "application/vnd.ms-opentype", len(otf), "otf"),
        # OpenType by a MIME type that says no font, and by no name: the
        # source record calls it none, and so it is taken as none
        _attachment(9, "display", "application/vnd.ms-opentype", len(otf)),
        _attachment(8, "packed.woff", "font/woff", 52),
    ]
    woff = b"wOFF" + bytes(48)
    tools = Tools(monkeypatch, streams, {3: SANS, 5: SERIF_BOLD, 7: otf, 8: woff})
    subs, work = tmp_path / "subs", tmp_path / "work"
    work.mkdir()
    out = pk._extract_fonts(ORIGINAL, subs, work)
    assert tools.runs == [
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
         "-select_streams", "t", str(ORIGINAL)],
        ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
         "-dump_attachment:3", str(work / "font-3"), "-dump_attachment:5", str(work / "font-5"),
         "-dump_attachment:7", str(work / "font-7"), "-dump_attachment:8", str(work / "font-8"),
         "-i", str(ORIGINAL), "-map", "0:3", "-c", "copy", "-t", "0", "-f", "null", "-"],
    ]
    # By the original's streams, a font by its MIME type or its name; each
    # named by its place and what its bytes are, never by its file name.
    assert out == [
        {"path": "subs/fonts/0.ttf", "format": "ttf",
         "names": ["Example Sans", "Example Sans Regular", "ExampleSans-Regular"],
         "sourceStreamIndex": 3},
        {"path": "subs/fonts/1.ttf", "format": "ttf",
         "names": ["Example Serif", "Example Serif Bold", "ExampleSerif-Bold"],
         "sourceStreamIndex": 5},
        {"path": "subs/fonts/2.otf", "format": "otf",
         "names": ["Example Display", "Example Display Regular", "ExampleDisplay-Regular"],
         "sourceStreamIndex": 7},
        {"path": "subs/fonts/3.woff", "format": "woff", "names": [], "sourceStreamIndex": 8},
    ]
    assert {p.name: p.read_bytes() for p in (subs / "fonts").iterdir()} == {
        "0.ttf": SANS, "1.ttf": SERIF_BOLD, "2.otf": otf, "3.woff": woff}


def test_an_original_without_fonts_is_read_once_and_gives_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    tools = Tools(monkeypatch, [_attachment(2, "cover.png", "image/png")], {})
    assert pk._extract_fonts(ORIGINAL, tmp_path / "subs", tmp_path) == []
    assert [args[0] for args in tools.runs] == ["ffprobe"]
    assert not (tmp_path / "subs").exists()


def test_a_font_not_written_whole_or_of_no_bytes_is_left_out_and_said(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    streams = [_attachment(3, "a.ttf", "font/ttf", len(SANS)),
               _attachment(4, "b.ttf", "font/ttf", 0),
               _attachment(5, "c.otf", "font/otf", len(SERIF_BOLD)),
               _attachment(6, "d.ttf", "font/ttf", None)]
    tools = Tools(monkeypatch, streams, {3: SANS, 5: SERIF_BOLD, 6: b"x" * 9}, short={5})
    with capture_logs() as logs:
        out = pk._extract_fonts(ORIGINAL, tmp_path / "subs", tmp_path)
    # A size ffprobe doesn't say is taken as written when ffmpeg says so;
    # bytes that are no font the format knows keep them all, as bin.
    assert [(f["path"], f["format"], f["sourceStreamIndex"]) for f in out] == [
        ("subs/fonts/0.ttf", "ttf", 3), ("subs/fonts/1.bin", None, 6)]
    assert "-dump_attachment:4" not in tools.runs[1]
    assert [(e["index"], e["error"]) for e in logs if e["event"] == "packager.fonts.failed"] == [
        (4, "the attachment holds no bytes"), (5, f"{len(SERIF_BOLD) // 2} bytes written of "
                                                  f"{len(SERIF_BOLD)}")]


def test_a_failed_run_keeps_only_the_fonts_it_wrote_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    streams = [_attachment(3, "a.ttf", "font/ttf", len(SANS)),
               _attachment(4, "b.ttf", "font/ttf", None)]
    Tools(monkeypatch, streams, {3: SANS, 4: SERIF_BOLD}, code=1)
    out = pk._extract_fonts(ORIGINAL, tmp_path / "subs", tmp_path)
    assert [f["sourceStreamIndex"] for f in out] == [3]


def test_an_original_ffprobe_cant_read_has_no_fonts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def fail(args, **_kw):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(pk.subprocess, "run", fail)
    assert pk._extract_fonts(ORIGINAL, tmp_path / "subs", tmp_path) == []


# ------------------------------------------------------------- with ffmpeg

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs ffmpeg and ffprobe on PATH")


@needs_ffmpeg
def test_ffmpeg_takes_the_fonts_out_byte_for_byte(tmp_path: Path) -> None:
    ttc = fontmaker.collection(("Example Sans", "Regular"), ("Example Sans", "Italic"))
    otf = fontmaker.opentype_header("Example Display")
    files = {"sans.ttf": (SANS, "application/x-truetype-font", "Example Sans.ttf"),
             "cover.jpg": (b"\xff\xd8\xff\xe0 a picture", "image/jpeg", "cover.jpg"),
             "serif.ttf": (SERIF_BOLD, "font/ttf", "Example Serif Bold.ttf"),
             "family.ttc": (ttc, "font/collection", "family.ttc"),
             "notes.txt": (b"some notes\n", "text/plain", "notes.txt"),
             "display.otf": (otf, "application/vnd.ms-opentype", "Example Display.otf")}
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=1",
            "-c:v", "libx264" if "libx264" in _encoders() else "mpeg4"]
    for i, (name, (body, mimetype, filename)) in enumerate(files.items()):
        (tmp_path / name).write_bytes(body)
        args += ["-attach", str(tmp_path / name), f"-metadata:s:t:{i}", f"mimetype={mimetype}",
                 f"-metadata:s:t:{i}", f"filename={filename}"]
    original = tmp_path / "original.mkv"
    subprocess.run([*args, str(original)], check=True)
    out = pk._extract_fonts(original, tmp_path / "subs", tmp_path)
    assert [(f["path"], f["format"], f["sourceStreamIndex"]) for f in out] == [
        ("subs/fonts/0.ttf", "ttf", 1), ("subs/fonts/1.ttf", "ttf", 3),
        ("subs/fonts/2.ttc", "ttc", 4), ("subs/fonts/3.otf", "otf", 6)]
    assert [(tmp_path / f["path"]).read_bytes() for f in out] == [SANS, SERIF_BOLD, ttc, otf]
    assert out[2]["names"] == fonts.font_names(ttc)
    assert sorted(p.name for p in (tmp_path / "subs" / "fonts").iterdir()) == [
        "0.ttf", "1.ttf", "2.ttc", "3.otf"]


def _encoders() -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout
