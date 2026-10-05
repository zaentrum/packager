"""Subtitle tracks: the one that may be default, and the subtitle files
next to the source (the item record's subtitleFiles): which are taken,
how their text is decoded, their manifest entries, and their conversion
to WebVTT (the last with ffmpeg, skipped without it)."""

from __future__ import annotations

import codecs
import shutil
from pathlib import Path

import pytest

from packager import packager as pk

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg on PATH")


def _sub(lang: str, *, forced: bool = False, visible: bool = True) -> dict:
    return {"language": lang, "forced": forced, "visible": visible, "format": "webvtt"}


def test_a_full_subtitle_is_never_default() -> None:
    # Sintel: an English film whose subtitles the file flags none of. The
    # transcoder's remux flagged the first, German, default (ffmpeg does
    # that when a file has several and flags none), and it showed by itself.
    subs = [_sub("ger"), _sub("eng"), _sub("spa")]
    assert pk._pick_default_subtitle(subs, "eng") is None
    assert pk._pick_default_subtitle(subs, "ger") is None
    assert pk._pick_default_subtitle([], "eng") is None


def test_a_forced_subtitle_in_the_audios_language_is_default() -> None:
    subs = [_sub("eng"), _sub("eng", forced=True), _sub("ger", forced=True)]
    assert pk._pick_default_subtitle(subs, "eng") == 1
    assert pk._pick_default_subtitle(subs, "ger") == 2
    assert pk._pick_default_subtitle(subs, "deu") == 2  # one language, B or T code


def test_a_forced_subtitle_in_a_foreign_language_is_not_default() -> None:
    assert pk._pick_default_subtitle([_sub("ger", forced=True), _sub("eng")], "eng") is None


def test_a_forced_subtitle_of_no_known_language() -> None:
    # Not foreign to any audio, but one in the audio's own language first.
    assert pk._pick_default_subtitle([_sub("und", forced=True)], "eng") == 0
    assert pk._pick_default_subtitle(
        [_sub("und", forced=True), _sub("eng", forced=True)], "eng") == 1
    # Audio of no known language (und, zxx, none): the first forced track.
    for audio in ("und", "zxx", None):
        assert pk._pick_default_subtitle(
            [_sub("eng"), _sub("ger", forced=True), _sub("eng", forced=True)], audio) == 1


def test_a_hidden_forced_subtitle_is_not_default() -> None:
    subs = [_sub("eng", forced=True, visible=False), _sub("eng", forced=True)]
    assert pk._pick_default_subtitle(subs, "eng") == 1
    assert pk._pick_default_subtitle(subs[:1], "eng") is None


# ------------------------------------------------- subtitle files: which
def test_subtitle_files_next_to_the_source(tmp_path: Path) -> None:
    source = tmp_path / "Movie (2010)" / "Movie.mkv"
    folder = source.parent
    files = pk._subtitle_files([
        {"path": str(folder / "Movie.en.srt"), "language": "eng", "label": " English ",
         "forced": False},
        {"path": str(folder / "Subs" / "2_German.ASS"), "language": "GER", "forced": True},
        {"path": str(folder / "Movie.vtt"), "language": "en"},     # not a 639-2 code
        {"path": str(folder / "Movie.ssa"), "forced": "true"},     # forced only when true
    ], source)
    assert [(f.path, f.language, f.label, f.forced) for f in files] == [
        (folder / "Movie.en.srt", "eng", "English", False),
        (folder / "Subs" / "2_German.ASS", "ger", "", True),
        (folder / "Movie.vtt", "und", "", False),
        (folder / "Movie.ssa", "und", "", False),
    ]


@pytest.mark.parametrize("entry", [
    {"path": "Movie.en.srt", "language": "eng"},                    # relative
    {"path": "/media/Other (2011)/Other.en.srt", "language": "eng"},  # another folder
    {"path": "/media/Movie (2010)/../Other (2011)/x.srt"},          # climbs out
    {"path": "/media/Movie (2010)/Movie.en.sub"},                   # VobSub
    {"path": "/media/Movie (2010)/Movie.en.sup"},
    {"path": "/media/Movie (2010)/Movie.en.txt"},
    {"path": "/media/Movie (2010)/Movie.mkv"},
    {"path": ""},
    {"path": None},
    {"path": 7},
    {"language": "eng"},
    "/media/Movie (2010)/Movie.en.srt",
    None,
])
def test_subtitle_files_ignore_an_entry_they_cant_take(entry) -> None:
    source = Path("/media/Movie (2010)/Movie.mkv")
    ok = {"path": "/media/Movie (2010)/Movie.de.srt", "language": "ger"}
    assert [f.path for f in pk._subtitle_files([entry, ok], source)] == [
        Path("/media/Movie (2010)/Movie.de.srt")]
    assert pk._subtitle_files(None, source) == []


# --------------------------------------------- subtitle files: their text
@pytest.mark.parametrize(("raw", "text"), [
    ("Café\n".encode(), "Café\n"),
    (codecs.BOM_UTF8 + "Café\n".encode(), "Café\n"),
    (codecs.BOM_UTF16_LE + "Café\n".encode("utf-16-le"), "Café\n"),
    (codecs.BOM_UTF16_BE + "Café\n".encode("utf-16-be"), "Café\n"),
    (codecs.BOM_UTF32_LE + "Café\n".encode("utf-32-le"), "Café\n"),
    # Windows-1252: its quotes and dash aren't Latin-1's.
    ("“Café” — crème\n".encode("cp1252"), "“Café” — crème\n"),
    # A byte Windows-1252 doesn't define: read as Latin-1.
    (b"caf\xe9 \x81\n", "café \x81\n"),
    (b"1\r\n00:00:01,000 --> 00:00:02,000\r\nA\rB\r\n", "1\n00:00:01,000 --> 00:00:02,000\nA\nB\n"),
])
def test_subtitle_text_is_decoded(raw: bytes, text: str) -> None:
    assert pk._subtitle_text(raw) == text


@pytest.mark.parametrize(("language", "text", "codec"), [
    ("rus", "Привет, мир", "cp1251"),
    ("pol", "Zażółć gęślą jaźń", "cp1250"),
    ("gre", "Καλημέρα", "cp1253"),
    ("tur", "Günaydın, şimdi", "cp1254"),  # noqa: RUF001 (Turkish dotless i)
])
def test_subtitle_text_in_its_languages_legacy_code_page(language, text, codec) -> None:
    assert pk._subtitle_text(text.encode(codec), language) == text
    assert pk._subtitle_text(text.encode("utf-8"), language) == text  # UTF-8 first


# ----------------------------------------- subtitle files: their entries
def test_subtitle_file_entries_follow_the_sources_own_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ffmpeg(_label: str, args: list[str]) -> None:
        source, target = Path(args[args.index("-i") + 1]), Path(args[-1])
        if "broken" in source.read_text():
            target.write_text("half")
            raise pk.PackageError("ffmpeg subtitle file exited 183: Invalid data")
        target.write_text("WEBVTT\n\n" + source.read_text())

    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", ffmpeg)
    folder = tmp_path / "media"
    folder.mkdir()
    for name, body in (("a.fr.srt", "Café".encode("cp1252")), ("a.en.srt", b"Hello"),
                       ("a.xx.srt", b"broken"), ("a.de.ass", b"Hallo")):
        (folder / name).write_bytes(body)
    files = pk._subtitle_files([
        {"path": str(folder / "a.fr.srt"), "language": "fre", "label": "Français"},
        {"path": str(folder / "a.en.srt"), "language": "eng", "forced": True},
        {"path": str(folder / "a.gone.srt"), "language": "spa"},
        {"path": str(folder / "a.xx.srt")},
        {"path": str(folder / "a.de.ass"), "language": "ger"},
    ], folder / "a.mkv")
    subs = tmp_path / "stage" / "subs"
    work = tmp_path / "work"
    work.mkdir()
    entries = pk._convert_subtitle_files(files, subs, work, first=2, visible_indices={2, 3, 6})
    # After the source's two tracks (sub0, sub1); the missing and the
    # broken file are left out, their numbers with them.
    assert entries == [
        {"id": "sub2", "language": "fre", "title": "Français", "default": False,
         "forced": False, "visible": True, "path": "subs/2.vtt", "format": "webvtt",
         "external": True},
        {"id": "sub3", "language": "eng", "title": "", "default": False, "forced": True,
         "visible": True, "path": "subs/3.vtt", "format": "webvtt", "external": True},
        {"id": "sub6", "language": "ger", "title": "", "default": False, "forced": False,
         "visible": True, "path": "subs/6.vtt", "format": "webvtt", "external": True},
    ]
    assert sorted(p.name for p in subs.iterdir()) == ["2.vtt", "3.vtt", "6.vtt"]
    assert (subs / "2.vtt").read_text(encoding="utf-8") == "WEBVTT\n\nCafé"  # as UTF-8
    assert pk._convert_subtitle_files([], tmp_path / "none", work, first=0) == []
    assert not (tmp_path / "none").exists()
    # A file far larger than any subtitle file isn't read.
    monkeypatch.setattr(pk, "_SUBTITLE_FILE_MAX_BYTES", 3)  # "Café" in Windows-1252: 4
    assert pk._convert_subtitle_files(files[:1], subs, work, first=2) == []


# ------------------------------------- subtitle files: WebVTT, by ffmpeg
def _convert(tmp_path: Path, name: str, raw: bytes, language: str = "und") -> str:
    (tmp_path / name).write_bytes(raw)
    [sub] = pk._subtitle_files([{"path": str(tmp_path / name), "language": language}],
                               tmp_path / "movie.mkv")
    target = tmp_path / "out.vtt"
    pk._convert_subtitle_file(sub, target, tmp_path / f"utf8-{name}")
    return target.read_text(encoding="utf-8")


SRT = ("1\n00:00:01,000 --> 00:00:03,500\n<i>Café</i> “crème” — brûlée\n\n"
       "2\n01:02:03,456 --> 01:02:04,000\nAn hour in.\n")


@needs_ffmpeg
@pytest.mark.parametrize("raw", [
    SRT.encode("utf-8"),
    codecs.BOM_UTF8 + SRT.replace("\n", "\r\n").encode("utf-8"),
    codecs.BOM_UTF16_LE + SRT.encode("utf-16-le"),
    SRT.replace("\n", "\r\n").encode("cp1252"),
], ids=["utf-8", "utf-8-bom-crlf", "utf-16", "cp1252-crlf"])
def test_srt_to_webvtt(tmp_path: Path, raw: bytes) -> None:
    vtt = _convert(tmp_path, "movie.en.srt", raw)
    assert vtt.startswith("WEBVTT\n")
    cues = [line for line in vtt.splitlines() if "-->" in line]
    assert cues == ["00:01.000 --> 00:03.500", "01:02:03.456 --> 01:02:04.000"]
    assert "<i>Café</i> “crème” — brûlée" in vtt
    assert "An hour in." in vtt


@needs_ffmpeg
def test_srt_in_a_legacy_code_page_to_webvtt(tmp_path: Path) -> None:
    srt = "1\n00:00:01,000 --> 00:00:02,000\nПривет, мир\n"  # noqa: RUF001 (Cyrillic)
    vtt = _convert(tmp_path, "movie.ru.srt", srt.encode("cp1251"), language="rus")
    assert "Привет, мир" in vtt


@needs_ffmpeg
def test_ass_and_webvtt_to_webvtt(tmp_path: Path) -> None:
    ass = ("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, "
           "Fontsize, PrimaryColour, Bold, Italic, Alignment\nStyle: Default,Arial,20,"
           "&H00FFFFFF,0,0,2\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
           "MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,"
           "{\\i1}Hello{\\i0} there\\Nsecond line\n")
    vtt = _convert(tmp_path, "movie.en.ass", ass.encode())
    assert [line for line in vtt.splitlines() if "-->" in line] == ["00:01.000 --> 00:02.500"]
    assert "<i>Hello</i> there\nsecond line" in vtt
    vtt = _convert(tmp_path, "movie.en.vtt", b"WEBVTT\n\n00:01.000 --> 00:02.000\nShort\n")
    assert [line for line in vtt.splitlines() if "-->" in line] == ["00:01.000 --> 00:02.000"]


@needs_ffmpeg
def test_a_file_that_is_no_subtitle_fails(tmp_path: Path) -> None:
    with pytest.raises(pk.PackageError):
        _convert(tmp_path, "movie.en.srt", b"not a subtitle file at all\n")
