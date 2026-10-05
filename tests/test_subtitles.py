"""Subtitle tracks: the one that may be default (no binaries)."""

from __future__ import annotations

from packager import packager as pk


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
