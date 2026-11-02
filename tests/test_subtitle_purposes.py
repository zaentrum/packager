"""A subtitle is forced, or for the deaf and hard of hearing, in the
package's record as the source record reads the stream it was made from.
The record logic calls a stream forced by its disposition, by its title
("Forced"), or by its content: a track neither says anything of, with a
small fraction of the cues of the full track in its language, translates a
few lines, not the film. The package's subtitle was forced by its
disposition alone, so where the source record called a track forced by its
title or its content, the package's essence lacked its language, and the
deletion gate kept the original though the package carries the track: a
VobSub titled for the foreign parts only beside the full track, a Spanish
text track of under a hundred cues beside the full one, a German PGS of a
few pictures. Such a track is `forced` in package.json now, so the essences
name the same languages and a client shows it by itself; a track for the
deaf and hard of hearing is sdh whatever title the encode kept. A track
flagged before is as it was; the default and the built manifest are as
they were. The streams are made up; nothing runs ffmpeg or
shaka-packager."""

from __future__ import annotations

from pathlib import Path

import pytest

from packager import libv2_records as rec
from packager import packager as pk
from packager import records

NOW = "2026-11-02T10:00:00Z"
SOURCE = "0b6c3d2e-3333-4a2b-8c3d-4e5f60718293"
ORIGINAL = "Example Film (2024).mkv"
# What the essences count a forced subtitle's language for
# (libv2_records.track_essence).
FORCED = ("forced", "signs-songs")


def _raw(subtitles: list[tuple], audio: str = "eng") -> dict:
    """The probe of a file with a video, an audio track in `audio` and these
    subtitle streams, (codec, language, title, disposition, cues), as
    ffprobe reports it: the cues in the statistics tag mkvmerge writes
    (NUMBER_OF_FRAMES-eng), none where `cues` is None."""
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
         "pix_fmt": "yuv420p10le", "disposition": {"default": 1}, "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "disposition": {"default": 1}, "tags": {"language": audio}},
    ]
    for i, (codec, language, title, disposition, cues) in enumerate(subtitles):
        tags = {**({"language": language} if language else {}),
                **({"title": title} if title else {}),
                **({"NUMBER_OF_FRAMES-eng": str(cues)} if cues is not None else {})}
        streams.append({"index": 2 + i, "codec_type": "subtitle", "codec_name": codec,
                        "disposition": dict(disposition), "tags": tags})
    return {"streams": streams, "chapters": [],
            "format": {"format_name": "matroska,webm", "duration": "5400.000000"}}


def _probe(raw: dict) -> pk._Probe:
    """v0's probe, of the file `raw` is the probe of."""
    streams = raw["streams"]
    return pk._Probe(container="matroska,webm", duration_ms=5_400_000, video=streams[0],
                     audio=[s for s in streams if s["codec_type"] == "audio"],
                     subtitles=[s for s in streams if s["codec_type"] == "subtitle"],
                     video_index=0)


def _entries(monkeypatch: pytest.MonkeyPatch, subs: Path, probe: pk._Probe) -> list[dict]:
    """The manifest's entries of v0's subtitle tracks, as _extract_subtitles
    makes them: every track extracted, a text one as WebVTT, a PGS one as
    .sup, a VobSub one as its .idx and .sub."""
    def write(folder: Path, c) -> None:
        (folder / c.name).write_bytes(b"x")
        if c.format == "vobsub":
            (folder / c.name).with_suffix(".sub").write_bytes(b"x")

    def together(_src: Path, folder: Path, sidecars: list) -> set[int]:
        for c in sidecars:
            write(folder, c)
        return {c.index for c in sidecars}

    def alone(_src: Path, folder: Path, c, _codec: str) -> bool:
        write(folder, c)
        return True

    monkeypatch.setattr(pk, "_extract_together", together)
    monkeypatch.setattr(pk, "_extract_alone", alone)
    monkeypatch.setattr(pk, "_extract_vobsubs", lambda _src, folder, cs, _streams: (
        together(_src, folder, cs)))
    return pk._extract_subtitles(Path("/m/original.mkv"), probe, subs)


def _record(raw: dict, probe: pk._Probe, entries: list[dict]) -> tuple[dict, dict]:
    """The source record of the original `raw` is the probe of, and the
    package record of a package made from it (v0's probe `probe`) whose
    subtitles are `entries`."""
    source, _ = rec.source_record(
        SOURCE, ORIGINAL, 1, taken_at=NOW, taken_by="packager",
        library_path=f"Example Film (2024)/{ORIGINAL}", qh1="sha256:" + "0" * 64,
        probe=raw, probe_version="ffprobe test")
    manifest = {
        "packagedAt": NOW, "packager": "packager-test", "durationMs": 5_400_000,
        "renditions": {
            "video": [{"id": "v0", "dir": "hls/v0", "codec": "hvc1.2.4.L120.B0", "width": 1920,
                       "height": 1080}],
            "audio": [{"idx": 0, "id": "a0", "dir": "hls/a0", "codec": "mp4a.40.2",
                       "language": probe.audio[0]["tags"]["language"], "channels": 2,
                       "default": True}],
            "audioSurround": []},
        "subtitles": entries,
    }
    package = records.package_record(
        "c0ffee00-0000-4000-8000-0000000000bb", pk.Built(manifest, probe, {}), [], source,
        records.OriginalProbe(raw, "ffprobe test"), {}, None, NOW)
    return source, package


def _packaged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: dict,
              v0: dict | None = None) -> tuple[dict, dict]:
    """The source record of `raw` and the package record of the package made
    from it, v0 the file `v0` is the probe of (the original itself when
    None)."""
    probe = _probe(v0 or raw)
    return _record(raw, probe, _entries(monkeypatch, tmp_path / "subs", probe))


def _as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """The packager as it was: a subtitle forced by its stream's disposition
    alone, sdh by that disposition or by the title v0 kept."""
    monkeypatch.setattr(records, "_subtitle_purposes", lambda _probe: {})


def _subtitles(record: dict) -> list[tuple]:
    """A package record's subtitles, by what they say they are for."""
    return [(s["id"], s["sourceStreamIndex"], s["format"], s["forced"], s["purpose"],
             s["purposeFrom"], s["default"]) for s in record["subtitles"]]


# ---------------------------------------------------------------- what the gate kept originals for

# An English VobSub titled for the foreign parts only, flagged default and
# not forced, of a few dozen pictures, beside the full English track in PGS.
FOREIGN_PARTS = [
    ("dvd_subtitle", "eng", "Foreign Parts Only", {"default": 1, "forced": 0}, 30),
    ("hdmv_pgs_subtitle", "eng", None, {}, 2000),
]
# A Spanish text track of under a hundred cues, no title, not flagged,
# beside the full Spanish text track and a Spanish PGS; a French track
# besides.
SPARSE_TEXT = [
    ("hdmv_pgs_subtitle", "spa", "Spanish", {}, 2900),
    ("subrip", "fre", None, {}, 1400),
    ("subrip", "spa", None, {}, 90),
    ("subrip", "spa", None, {}, 1400),
]
# A German PGS of six pictures, no title, after the full German tracks.
FEW_PICTURES = [
    ("hdmv_pgs_subtitle", "ger", None, {}, 3400),
    ("hdmv_pgs_subtitle", "eng", None, {}, 4200),
    ("subrip", "ger", None, {}, 1500),
    ("hdmv_pgs_subtitle", "ger", None, {}, 6),
]


@pytest.mark.parametrize(("streams", "audio", "becomes", "language"), [
    (FOREIGN_PARTS, "eng", [
        ("sub0", 2, "vobsub", True, "forced", "disposition", False),
        ("sub1", 3, "pgs", False, "dialogue", "assumed", False)], "en"),
    (SPARSE_TEXT, "eng", [
        ("sub0", 2, "pgs", False, "dialogue", "assumed", False),
        ("sub1", 3, "webvtt", False, "dialogue", "assumed", False),
        ("sub2", 4, "webvtt", True, "forced", "disposition", False),
        ("sub3", 5, "webvtt", False, "dialogue", "assumed", False)], "es"),
    (FEW_PICTURES, "ger", [
        ("sub0", 2, "pgs", False, "dialogue", "assumed", False),
        ("sub1", 3, "pgs", False, "dialogue", "assumed", False),
        ("sub2", 4, "webvtt", False, "dialogue", "assumed", False),
        ("sub3", 5, "pgs", True, "forced", "disposition", False)], "de"),
], ids=["foreign-parts-vobsub", "sparse-text", "few-pictures-pgs"])
def test_a_track_the_source_record_calls_forced_by_its_content_is_forced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    streams: list[tuple], audio: str, becomes: list[tuple], language: str,
) -> None:
    raw = _raw(streams, audio=audio)
    source, package = _packaged(monkeypatch, tmp_path, raw)
    # The source record calls the short track forced by its content, and
    # nothing else forced.
    assert [(s["index"], s["purpose"], s["purposeFrom"]) for s in source["streams"]
            if s["type"] == "subtitle" and s["purpose"] != "dialogue"] == [
        (index, "forced", "content") for _, index, _, forced, *_ in becomes if forced]
    assert source["essence"]["forcedSubtitleLanguages"] == [language]
    # The package carries every track, the short one forced, none default.
    assert _subtitles(package) == becomes
    assert package["essence"]["forcedSubtitleLanguages"] == [language]
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []


@pytest.mark.parametrize(("streams", "language"), [
    (FOREIGN_PARTS, "en"), (SPARSE_TEXT, "es"), (FEW_PICTURES, "de")])
def test_as_before_the_gate_named_the_forced_language_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, streams: list[tuple], language: str,
) -> None:
    _as_before(monkeypatch)
    source, package = _packaged(monkeypatch, tmp_path, _raw(streams))
    assert [s["forced"] for s in package["subtitles"]] == [False] * len(streams)
    assert package["essence"]["forcedSubtitleLanguages"] == []
    assert rec.deletion_gate([source["essence"]], package["essence"]) == [
        f"forcedSubtitleLanguages:{language}"]


# ---------------------------------------------------------------- what each kind of track becomes

# Every way the record logic reads a subtitle stream, in one file:
# (codec, language, title, disposition, cues).
EVERY_KIND = [
    # the dialogue; forced by its disposition, by its title; a title that
    # says it is none, but a short track: forced by its content
    ("subrip", "eng", None, {}, 1500),
    ("subrip", "eng", None, {"forced": 1}, 30),
    ("subrip", "eng", "Forced", {}, 40),
    ("subrip", "eng", "English (non-forced)", {}, 45),
    # sdh by its disposition, hearing_impaired or captions
    ("hdmv_pgs_subtitle", "eng", None, {"hearing_impaired": 1}, 1600),
    ("hdmv_pgs_subtitle", "ger", None, {"captions": 1}, 1700),
    ("hdmv_pgs_subtitle", "ger", None, {}, 1650),
    # sdh by its title
    ("subrip", "fre", "SDH", {}, 1500),
    ("subrip", "fre", None, {}, 1400),
    ("subrip", "fre", "CC", {}, 1450),
    # signs and songs, by its title
    ("subrip", "spa", "Signs & Songs", {}, 50),
    ("subrip", "spa", None, {}, 1300),
    # a commentary by its title, by its disposition: short, but no content
    # makes one forced
    ("subrip", "ita", "Commentary", {}, 20),
    ("subrip", "ita", None, {"comment": 1}, 25),
    ("subrip", "ita", None, {}, 1200),
    # lyrics by its disposition, by its title
    ("subrip", "jpn", None, {"lyrics": 1}, 15),
    ("subrip", "jpn", "Karaoke", {}, 12),
    ("subrip", "jpn", None, {}, 900),
    # forced by its content, a VobSub beside the full PGS
    ("dvd_subtitle", "dut", None, {}, 25),
    ("hdmv_pgs_subtitle", "dut", None, {}, 1800),
    # short, but no full track to be short of; no count of its cues
    ("subrip", "swe", None, {}, 20),
    ("subrip", "swe", None, {}, 250),
    ("subrip", "nor", None, {}, None),
    ("subrip", "nor", None, {}, 1100),
    # of no known language, short: forced by its content
    ("subrip", "und", None, {}, 10),
    ("subrip", "und", None, {}, 1000),
    # forced first, whatever else its disposition or title says
    ("subrip", "fin", None, {"forced": 1, "hearing_impaired": 1}, 35),
    ("subrip", "fin", "SDH", {"forced": 1}, 40),
    ("subrip", "fin", None, {}, 1350),
]


def test_the_essences_name_the_same_languages_for_the_tracks_a_package_carries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source, package = _packaged(monkeypatch, tmp_path, _raw(EVERY_KIND))
    streams = {s["index"]: s for s in source["streams"] if s["type"] == "subtitle"}
    assert len(package["subtitles"]) == len(streams)
    for key in ("forcedSubtitleLanguages", "sdhSubtitleLanguages", "subtitleLanguages"):
        assert package["essence"][key] == source["essence"][key], key
    assert package["essence"]["forcedSubtitleLanguages"] == ["en", "es", "fi", "nl", "und"]
    assert package["essence"]["sdhSubtitleLanguages"] == ["de", "en", "fr"]
    # Track by track: counted forced, and sdh, as its stream is.
    for s in package["subtitles"]:
        stream = streams[s["sourceStreamIndex"]]
        assert (s["purpose"] in FORCED, s["purpose"] == "sdh") == (
            stream["purpose"] in FORCED, stream["purpose"] == "sdh"), s["id"]
        # forced exactly where the record logic calls the stream so.
        assert s["forced"] == (stream["purpose"] == "forced"), s["id"]
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []


def test_a_track_flagged_before_is_as_it_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw = _raw(EVERY_KIND)
    _, package = _packaged(monkeypatch, tmp_path / "now", raw)
    with monkeypatch.context() as m:
        _as_before(m)
        _, before = _packaged(m, tmp_path / "before", raw)
    changed = {s["sourceStreamIndex"] - 2: (b, s) for s, b in
               zip(package["subtitles"], before["subtitles"], strict=True) if s != b}
    # Only the tracks the source record calls forced by their title or
    # their content are new: forced now, by the mark the record reads
    # first, and nothing else of them. One forced or sdh by its
    # disposition, sdh by its title, signs and songs, a commentary, lyrics
    # or the dialogue has the record it had.
    assert sorted(changed) == [2, 3, 18, 24]
    for b, s in changed.values():
        was = "forced" if b["title"] == "Forced" else "dialogue"
        assert (b["forced"], b["purpose"]) == (False, was)
        assert (s["forced"], s["purpose"], s["purposeFrom"]) == (True, "forced", "disposition")
        assert {k: v for k, v in s.items() if k not in ("forced", "purpose", "purposeFrom")} == {
            k: v for k, v in b.items() if k not in ("forced", "purpose", "purposeFrom")}


# ---------------------------------------------------------------- for the deaf and hard of hearing

def test_sdh_by_its_disposition_and_by_its_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw = _raw([
        ("hdmv_pgs_subtitle", "eng", None, {"hearing_impaired": 1}, 1600),
        ("mov_text", "eng", None, {"captions": 1}, 1550),
        ("subrip", "eng", "English SDH", {}, 1580),
        ("subrip", "eng", "Hearing impaired", {}, 1570),
        ("subrip", "eng", "Closed captions", {}, 1560),
        ("subrip", "eng", None, {}, 1500),
    ])
    source, package = _packaged(monkeypatch, tmp_path, raw)
    assert [(s["purpose"], s["purposeFrom"], s["forced"]) for s in package["subtitles"]] == [
        ("sdh", "disposition", False), ("sdh", "disposition", False), ("sdh", "title", False),
        ("sdh", "title", False), ("sdh", "title", False), ("dialogue", "assumed", False)]
    assert package["essence"]["sdhSubtitleLanguages"] == ["en"]
    assert source["essence"]["sdhSubtitleLanguages"] == ["en"]
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []


def test_an_encode_that_kept_no_title_and_no_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # v0, the transcoder's encode, kept neither the titles nor the
    # statistics tags of the original's tracks: the record reads each track
    # as the source record reads the original's stream it was made from.
    original = _raw([
        ("subrip", "eng", "English SDH", {}, 1580),
        ("subrip", "eng", "Forced", {}, 30),
        ("subrip", "eng", None, {}, 1500),
        ("subrip", "ger", None, {}, 12),
        ("subrip", "ger", None, {}, 1400),
    ])
    v0 = _raw([("subrip", "eng", None, {}, None)] * 3 + [("subrip", "ger", None, {}, None)] * 2)
    source, package = _packaged(monkeypatch, tmp_path, original, v0)
    assert [(s["title"], s["forced"], s["purpose"]) for s in package["subtitles"]] == [
        ("", False, "sdh"), ("", True, "forced"), ("", False, "dialogue"),
        ("", True, "forced"), ("", False, "dialogue")]
    assert package["essence"]["sdhSubtitleLanguages"] == ["en"]
    assert package["essence"]["forcedSubtitleLanguages"] == ["de", "en"]
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []
    # As before, the record read v0's tracks alone, and the gate kept the
    # original for each of them.
    with monkeypatch.context() as m:
        _as_before(m)
        source, package = _packaged(m, tmp_path / "before", original, v0)
    assert rec.deletion_gate([source["essence"]], package["essence"]) == [
        "forcedSubtitleLanguages:de", "forcedSubtitleLanguages:en", "sdhSubtitleLanguages:en"]


def test_a_mark_is_never_taken_away(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A track v0 flags forced, or for the deaf and hard of hearing, keeps
    # its mark though the original's stream has none (the transcoder sets
    # none): the record says what it said before.
    original = _raw([("subrip", "eng", None, {}, 1500), ("subrip", "eng", None, {}, 1400)])
    v0 = _raw([("subrip", "eng", None, {"forced": 1}, None),
               ("subrip", "eng", None, {"hearing_impaired": 1}, None)])
    _, package = _packaged(monkeypatch, tmp_path, original, v0)
    with monkeypatch.context() as m:
        _as_before(m)
        _, before = _packaged(m, tmp_path / "before", original, v0)
    assert package["subtitles"] == before["subtitles"]
    assert [(s["forced"], s["purpose"]) for s in package["subtitles"]] == [(True, "forced"),
                                                                          (False, "sdh")]


# ---------------------------------------------------------------- never the default

def test_a_forced_track_is_never_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The manifest's default is a track forced by its stream's disposition,
    # in the audio's language, as before (_pick_default_subtitle): the
    # VobSub the source record calls forced by its content is not one, and
    # the record makes no subtitle the default.
    raw = _raw(FOREIGN_PARTS)
    probe = _probe(raw)
    entries = _entries(monkeypatch, tmp_path / "subs", probe)
    assert pk._pick_default_subtitle(entries, "eng") is None
    _, package = _record(raw, probe, entries)
    assert [s["default"] for s in package["subtitles"]] == [False, False]
    # A manifest that flags it default anyway: the record takes it back, as
    # it does any forced track's.
    entries = _entries(monkeypatch, tmp_path / "again", probe)
    entries[0]["default"] = True
    _, package = _record(raw, probe, entries)
    assert [(s["forced"], s["default"]) for s in package["subtitles"]] == [(True, False),
                                                                           (False, False)]
    # One forced by its disposition, in the audio's language, is the
    # manifest's default, and in the record no default, as before.
    raw = _raw([("subrip", "eng", None, {"forced": 1}, 30), ("subrip", "eng", None, {}, 1500)])
    probe = _probe(raw)
    entries = _entries(monkeypatch, tmp_path / "flagged", probe)
    picked = pk._pick_default_subtitle(entries, "eng")
    assert picked == 0
    entries[picked]["default"] = True
    _, package = _record(raw, probe, entries)
    assert [(s["forced"], s["default"]) for s in package["subtitles"]] == [(True, False),
                                                                           (False, False)]


def test_the_built_manifest_is_as_it_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The record is made of a copy: the manifest the master, its names and
    # its order were made of keeps each stream's own disposition.
    raw = _raw(EVERY_KIND)
    probe = _probe(raw)
    entries = _entries(monkeypatch, tmp_path / "subs", probe)
    kept = [dict(e) for e in entries]
    built = pk.Built({"subtitles": entries, "renditions": {}}, probe, {})
    for_record = records.for_record(built, records.OriginalProbe(raw, "ffprobe test"), NOW)
    assert entries == kept
    assert [e["forced"] for e in for_record["subtitles"]] != [e["forced"] for e in kept]


def test_a_subtitle_file_beside_the_original_is_as_the_catalog_says(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # A file that came with the original is no stream of it: no record
    # calls it anything, and its entry says what the catalog does.
    raw = _raw([("subrip", "eng", None, {}, 1500)])
    probe = _probe(raw)
    entries = [*_entries(monkeypatch, tmp_path / "subs", probe),
               {"id": "sub1", "language": "eng", "title": "", "default": False, "forced": False,
                "visible": True, "path": "subs/1.vtt", "format": "webvtt", "external": True}]
    _, package = _record(raw, probe, entries)
    assert [(s["id"], s.get("sourceStreamIndex"), s["forced"], s["purpose"])
            for s in package["subtitles"]] == [("sub0", 2, False, "dialogue"),
                                               ("sub1", None, False, "dialogue")]
