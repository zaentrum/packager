"""The master's order and AUTOSELECT of the renditions of a language, as
the record logic reads each (no binaries). Apple's HLS authoring
specification lists a language's main rendition first, and a player that
takes the first track in its viewer's language — a client of the
platform's does — takes it: so in each language the main audio comes
before an audio description or a commentary, and the dialogue's subtitles
before a forced track, subtitles for the deaf and hard of hearing or a
commentary's, whatever order the original has them in; each language keeps
the places it has, and the manifest and the records keep the original's
order. AUTOSELECT goes on the first track of a language for the dialogue,
on every forced track, every audio description and every subtitle for the
deaf and hard of hearing — none of which takes the dialogue's — and never
on a commentary."""

from __future__ import annotations

from packager import hls
from packager import packager as pk


def _stats() -> hls.PlaylistStats:
    return hls.PlaylistStats(peak_bps=200, avg_bps=100, segments=1, target_duration=6,
                             duration=6.0)


def test_main_first_keeps_each_languages_places() -> None:
    items = ["de AD", "en main", "de main", "en commentary", "fr main"]
    languages = ["de", "en", "de", "en", "fr"]
    mains = [False, True, True, False, True]
    assert pk._main_first(items, languages, mains) == [
        "de main", "en main", "de AD", "en commentary", "fr main"]
    # In order already, nothing moves; two main tracks keep theirs.
    assert pk._main_first(["a", "b", "c"], ["en"] * 3, [True, True, False]) == ["a", "b", "c"]
    assert pk._main_first(["x", "y"], ["en", "en"], [False, False]) == ["x", "y"]


def _subtitle(language: str, name: str, forced: bool = False) -> hls.SubtitleRendition:
    return hls.SubtitleRendition(f"{name}/playlist.m3u8", language, name, forced, False, _stats())


def test_subtitles_the_dialogue_first_and_autoselect_as_each_is_for() -> None:
    # An SDH track and a commentary's before the English dialogue, a forced
    # one after it; German the other way round.
    renditions = [_subtitle("en", "English · SDH"), _subtitle("en", "English · Commentary"),
                  _subtitle("en", "English"), _subtitle("en", "English (forced)", forced=True),
                  _subtitle("de", "German"), _subtitle("de", "German · Commentary")]
    purposes = ["sdh", "commentary", "dialogue", "forced", "dialogue", "commentary"]
    done = pk._finish_subtitles(renditions, purposes, ["en", "en", "en", "en", "de", "de"])
    assert [(r.name, r.autoselect) for r in done] == [
        ("English", True), ("English · SDH", True), ("English · Commentary", False),
        ("English (forced)", True), ("German", True), ("German · Commentary", False)]
    # Neither the SDH track nor the commentary's took the dialogue's
    # AUTOSELECT: a second English dialogue track gets none.
    again = pk._finish_subtitles(
        [_subtitle("en", "English · SDH"), _subtitle("en", "English · Commentary"),
         _subtitle("en", "English"), _subtitle("en", "English")],
        ["sdh", "commentary", "dialogue", "dialogue"], ["en"] * 4)
    assert [(r.name, r.autoselect) for r in again] == [
        ("English", True), ("English (2)", False), ("English · SDH", True),
        ("English · Commentary", False)]


def test_audio_the_main_track_first_and_autoselect_as_each_is_for() -> None:
    # German preferred: an audio description first in the file, the German
    # main track the default; an English commentary before its main track.
    metas = [
        {"language": "ger", "title": "", "visualImpaired": True, "visible": True},
        {"language": "ger", "title": "", "visible": True, "default": True},
        {"language": "eng", "title": "", "commentary": True, "visible": True},
        {"language": "eng", "title": "", "visible": True},
    ]
    names = ["German · Audio description", "German", "English · Commentary", "English"]
    renditions = [hls.AudioRendition(f"a{i}/playlist.m3u8", "audio", pk._lang_key(m["language"]),
                                     names[i], bool(m.get("default")), False, "2", "mp4a.40.2",
                                     _stats(), pk._audio_characteristics(m))
                  for i, m in enumerate(metas)]
    finished = pk._finish_group(renditions, metas)
    done = pk._main_first(finished, [r.language for r in finished],
                          [not (m.get("commentary") or m.get("visualImpaired")) for m in metas])
    assert [(r.name, r.default, r.autoselect, r.characteristics) for r in done] == [
        ("German", True, True, ""),
        ("German · Audio description", False, True, hls.DESCRIBES_VIDEO),
        ("English", False, True, ""), ("English · Commentary", False, False, "")]
