"""A package's record says it carries styled subtitles and fonts only when
it carries them for every stream of its original that has them: the
script of each ASS track, each font attachment, by the stream each was
made from. The manifest is made as the packager makes it
(_extract_subtitles, the fonts _extract_fonts lists), and the record
through records.package_record, as package.json is written; a script or a
font the manifest names that is none of the package's files counts for
nothing. The streams are made up; nothing runs ffmpeg."""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from packager import libv2_records as rec
from packager import packager as pk
from packager import records

NOW = "2026-11-03T10:00:00Z"
SOURCE = "0b6c3d2e-2222-4a2b-8c3d-4e5f60718293"
ORIGINAL = "Example Film (2024).mkv"


def _font(index: int, filename: str, mimetype: str) -> dict:
    return {"index": index, "codec_type": "attachment",
            "tags": {"filename": filename, "mimetype": mimetype}}


# The original: a video, an audio, two ASS tracks, a cover and two fonts.
RAW = {
    "format": {"format_name": "matroska,webm", "duration": "60.000000"}, "chapters": [],
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
         "pix_fmt": "yuv420p10le", "disposition": {"default": 1}, "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "disposition": {"default": 1}, "tags": {"language": "jpn"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "ass", "disposition": {},
         "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "ass", "disposition": {},
         "tags": {"language": "eng", "title": "Signs"}},
        _font(4, "cover.jpg", "image/jpeg"),
        _font(5, "Example Sans.ttf", "font/ttf"),
        _font(6, "Example Serif.otf", "application/vnd.ms-opentype"),
    ],
}
FONTS = [
    {"path": "subs/fonts/0.ttf", "format": "ttf", "names": ["Example Sans"],
     "sourceStreamIndex": 5},
    {"path": "subs/fonts/1.otf", "format": "otf", "names": ["Example Serif"],
     "sourceStreamIndex": 6},
]
ASS = "[Script Info]\nScriptType: v4.00+\n"


def _probe() -> pk._Probe:
    streams = RAW["streams"]
    return pk._Probe(container="matroska,webm", duration_ms=60_000, video=streams[0],
                     audio=[s for s in streams if s["codec_type"] == "audio"],
                     subtitles=[s for s in streams if s["codec_type"] == "subtitle"],
                     video_index=0)


def _subtitles(monkeypatch: pytest.MonkeyPatch, subs: Path, scripts: set[int]) -> list[dict]:
    """The manifest's subtitle entries as _extract_subtitles makes them, the
    one pass writing each track's WebVTT copy, and the script of the tracks
    `scripts` names — the other's empty, a script ffmpeg didn't write."""
    def together(_src: Path, folder: Path, sidecars: list, _timeout: float) -> set[int]:
        for c in sidecars:
            (folder / c.name).write_text("WEBVTT\n\n00:01.000 --> 00:02.000\nA line.\n")
            if c.styled:
                (folder / c.styled).write_text(ASS if c.index in scripts else "")
        return {c.index for c in sidecars}

    monkeypatch.setattr(pk, "_extract_together", together)
    return pk._extract_subtitles(Path("/m/original.mkv"), _probe(), subs)


def _record(entries: list[dict], fonts: list[dict], listed: list[str] | None = None) -> dict:
    """The package record of a package of these subtitles and fonts, its
    checksums listing every file the manifest names unless `listed` says
    which."""
    source, _ = rec.source_record(
        SOURCE, ORIGINAL, 1, taken_at=NOW, taken_by="packager",
        library_path=f"Example Film (2024)/{ORIGINAL}", qh1="sha256:" + "0" * 64,
        probe=RAW, probe_version="ffprobe test")
    manifest = {
        "packagedAt": NOW, "packager": "packager-test", "durationMs": 60_000,
        "renditions": {
            "video": [{"id": "v0", "dir": "hls/v0", "codec": "hvc1.2.4.L120.B0", "width": 1920,
                       "height": 1080}],
            "audio": [{"idx": 0, "id": "a0", "dir": "hls/a0", "codec": "mp4a.40.2",
                       "language": "jpn", "channels": 2, "default": True}],
            "audioSurround": []},
        "subtitles": entries, **({"fonts": fonts} if fonts else {}),
    }
    if listed is None:
        listed = ([e["path"] for e in entries]
                  + [e["styled"]["path"] for e in entries if e.get("styled")]
                  + [f["path"] for f in fonts])
    return records.package_record(
        "c0ffee00-0000-4000-8000-0000000000ab", pk.Built(manifest, _probe(), {}),
        [(rel, "0" * 64, 1) for rel in listed], source,
        records.OriginalProbe(RAW, "ffprobe test"), {}, None, NOW)


def _carried(package: dict) -> tuple[bool, bool]:
    return package["essence"]["styledSubtitles"], package["essence"]["fonts"]


def test_both_tracks_scripts_and_both_fonts_are_carried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package = _record(_subtitles(monkeypatch, tmp_path / "subs", {0, 1}), FONTS)
    assert _carried(package) == (True, True)
    assert [s.get("styled") for s in package["subtitles"]] == [
        {"path": "subs/0.ass", "format": "ass"}, {"path": "subs/1.ass", "format": "ass"}]
    assert [f["sourceStreamIndex"] for f in package["fonts"]] == [5, 6]
    assert not [x for x in package["fidelity"]["losses"]
                if x["kind"] in ("subtitle-styling", "attachments-dropped")]


def test_one_of_two_scripts_is_no_styled_subtitles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package = _record(_subtitles(monkeypatch, tmp_path / "subs", {0}), FONTS)
    assert [s.get("styled") for s in package["subtitles"]] == [
        {"path": "subs/0.ass", "format": "ass"}, None]
    assert _carried(package) == (False, True)


def test_one_of_two_fonts_is_no_fonts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entries = _subtitles(monkeypatch, tmp_path / "subs", {0, 1})
    assert _carried(_record(entries, FONTS[:1])) == (True, False)
    assert _carried(_record(entries, FONTS[1:])) == (True, False)
    assert _carried(_record(entries, [])) == (True, False)


def test_a_script_or_a_font_that_is_no_file_of_the_package_counts_for_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    entries = _subtitles(monkeypatch, tmp_path / "subs", {0, 1})
    with capture_logs() as logs:
        package = _record(entries, FONTS,
                          listed=["subs/0.vtt", "subs/0.ass", "subs/1.vtt", "subs/fonts/0.ttf"])
    assert [s.get("styled") for s in package["subtitles"]] == [
        {"path": "subs/0.ass", "format": "ass"}, None]
    assert [f["path"] for f in package["fonts"]] == ["subs/fonts/0.ttf"]
    assert _carried(package) == (False, False)
    assert [e["note"] for e in logs if e["event"] == "packager.library.package_note"] == [
        "subtitle sub1's styled original subs/1.ass is no file of the package; left out",
        "font file subs/fonts/1.otf is no file of the package; left out"]


def test_an_original_whose_attachments_cant_be_read_in_time_gets_no_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # What an attachment ffmpeg gave no bytes of is can't be told: a record
    # that counted a font as none would let it go with the original.
    def no_heads(path: str, _probe: dict) -> dict[int, bytes]:
        raise rec.AttachmentTimeout(
            f"ffmpeg gave no first bytes of attachment 4 of {path} within 120 s")

    monkeypatch.setattr(records.rec, "ffprobe", lambda _path: RAW)
    monkeypatch.setattr(records.rec, "attachment_heads", no_heads)
    with pytest.raises(pk.PackageError, match="gets no record this time: ffmpeg gave no first"):
        records.probe_original(tmp_path / "original.mkv")
