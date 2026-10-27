"""A subtitle track for the deaf and hard of hearing by its disposition
alone: flagged hearing_impaired, or captions, with no title that says so,
as an MP4's second English track often is. Its manifest entry says so
(hearingImpaired), its NAME is the one a title that says SDH gets
("English · SDH"), its HLS rendition carries the CHARACTERISTICS of one,
and its record makes it sdh by its disposition, so the package's essence
names its language as its source's does and deleting the original costs
nothing. A forced one stays forced, a title that says SDH already is not
said twice, and a plain track is as before. The streams are made up;
nothing runs ffmpeg or shaka-packager."""

from __future__ import annotations

from pathlib import Path

import pytest

from packager import hls, records
from packager import libv2_records as rec
from packager import packager as pk

NOW = "2026-10-27T10:00:00Z"
SOURCE = "0b6c3d2e-1111-4a2b-8c3d-4e5f60718293"
ORIGINAL = "Example Film (2024).mkv"

# The original's subtitle streams, after its video and its audio: (codec,
# language, title, disposition).
STREAMS = [
    ("subrip", "eng", None, {}),
    ("mov_text", "eng", None, {"hearing_impaired": 1, "captions": 1}),
    ("subrip", "ger", None, {"captions": 1}),
    ("subrip", "fre", "SDH", {"hearing_impaired": 1}),
    ("subrip", "spa", None, {"forced": 1, "hearing_impaired": 1}),
    ("subrip", "ita", "Italiano (SDH)", {}),
]
# What each becomes: whether its manifest entry is marked, its NAME, what
# its record says it is for and from what, and whether its HLS rendition
# says it is for the deaf and hard of hearing.
BECOMES = [
    # A plain track: as before.
    (False, "English", ("dialogue", "assumed"), False),
    # The second English track: flagged, no title.
    (True, "English · SDH", ("sdh", "disposition"), True),
    # Flagged captions only.
    (True, "German · SDH", ("sdh", "disposition"), True),
    # Flagged, and its title says so: said once.
    (True, "French · SDH", ("sdh", "disposition"), True),
    # Forced and flagged: forced, as its disposition says first.
    (True, "Spanish (forced)", ("forced", "disposition"), False),
    # Its title alone says so: by its title, as before.
    (False, "Italian · SDH", ("sdh", "title"), True),
]


def _raw(subtitles: list[tuple[str, str, str | None, dict]]) -> dict:
    """The probe of a file with a video, an English audio and these
    subtitle streams, as ffprobe reports it."""
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
         "pix_fmt": "yuv420p10le", "disposition": {"default": 1}, "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "disposition": {"default": 1}, "tags": {"language": "eng"}},
    ]
    for i, (codec, language, title, disposition) in enumerate(subtitles):
        streams.append({"index": 2 + i, "codec_type": "subtitle", "codec_name": codec,
                        "disposition": disposition,
                        "tags": {"language": language, **({"title": title} if title else {})}})
    return {"streams": streams, "chapters": [],
            "format": {"format_name": "matroska,webm", "duration": "60.000000"}}


def _probe(raw: dict) -> pk._Probe:
    """v0's probe, of the file `raw` is the probe of."""
    streams = raw["streams"]
    return pk._Probe(container="matroska,webm", duration_ms=60_000, video=streams[0],
                     audio=[s for s in streams if s["codec_type"] == "audio"],
                     subtitles=[s for s in streams if s["codec_type"] == "subtitle"],
                     video_index=0)


def _entries(monkeypatch: pytest.MonkeyPatch, subs: Path, probe: pk._Probe) -> list[dict]:
    """The manifest's entries of v0's subtitle tracks, as _extract_subtitles
    makes them: the one ffmpeg pass writes a cue file for each."""
    def together(_src: Path, folder: Path, sidecars: list) -> set[int]:
        for c in sidecars:
            (folder / c.name).write_text("WEBVTT\n\n00:01.000 --> 00:02.000\n[door creaks]\n")
        return {c.index for c in sidecars}

    monkeypatch.setattr(pk, "_extract_together", together)
    return pk._extract_subtitles(Path("/m/original.mkv"), probe, subs)


def _shaka(monkeypatch: pytest.MonkeyPatch) -> None:
    """shaka-packager for one text track: its media playlist, a segment,
    and the master beside them, which names the track's language."""
    def run(cmd: list[str], cwd: Path, _label: str) -> None:
        fields = dict(f.split("=", 1) for f in cmd[1].split(","))
        playlist = cwd / fields["playlist_name"]
        playlist.parent.mkdir(parents=True, exist_ok=True)
        (playlist.parent / "seg-00001.vtt").write_text("WEBVTT\n")
        playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.000,\nseg-00001.vtt\n"
                            "#EXT-X-ENDLIST\n")
        language = pk._lang_key(fields.get("language"))
        (cwd / cmd[cmd.index("--hls_master_playlist_output") + 1]).write_text(
            f'#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,URI="{playlist.parent.name}/playlist.m3u8",'
            f'GROUP-ID="subs",LANGUAGE="{language}",NAME="x"\n')

    monkeypatch.setattr(pk, "_run_shaka", run)


def _record(raw: dict, probe: pk._Probe, entries: list[dict]) -> tuple[dict, dict]:
    """The source record of the original `raw` is the probe of, and the
    package record of a package made from it (v0's probe `probe`) whose
    subtitles are `entries`."""
    source, _ = rec.source_record(
        SOURCE, ORIGINAL, 1, taken_at=NOW, taken_by="packager",
        library_path=f"Example Film (2024)/{ORIGINAL}", qh1="sha256:" + "0" * 64,
        probe=raw, probe_version="ffprobe test")
    manifest = {
        "packagedAt": NOW, "packager": "packager-test", "durationMs": 60_000,
        "renditions": {
            "video": [{"id": "v0", "dir": "hls/v0", "codec": "hvc1.2.4.L120.B0", "width": 1920,
                       "height": 1080}],
            "audio": [{"idx": 0, "id": "a0", "dir": "hls/a0", "codec": "mp4a.40.2",
                       "language": "eng", "channels": 2, "default": True}],
            "audioSurround": []},
        "subtitles": entries,
    }
    package = records.package_record(
        "c0ffee00-0000-4000-8000-0000000000aa", pk.Built(manifest, probe, {}), [], source,
        records.OriginalProbe(raw, "ffprobe test"), {}, None, NOW)
    return source, package


def test_the_manifest_entry_and_the_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entries = _entries(monkeypatch, tmp_path / "subs", _probe(_raw(STREAMS)))
    assert [("hearingImpaired" in e, e.get("hearingImpaired")) for e in entries] == [
        (marked, True if marked else None) for marked, _, _, _ in BECOMES]
    assert [pk._subtitle_display_name(e) for e in entries] == [name for _, name, _, _ in BECOMES]
    # Nothing else of an entry changes.
    assert [{k: v for k, v in e.items() if k != "hearingImpaired"} for e in entries] == [
        {"id": f"sub{i}", "language": language, "title": title or "", "default": False,
         "forced": bool(disposition.get("forced")), "visible": True, "path": f"subs/{i}.vtt",
         "format": "webvtt"}
        for i, (_, language, title, disposition) in enumerate(STREAMS)]


def test_the_hls_rendition_says_what_it_is_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    entries = _entries(monkeypatch, tmp_path / "subs", _probe(_raw(STREAMS)))
    _shaka(monkeypatch)
    renditions = pk._package_text_tracks(entries, tmp_path, 6)
    assert [e["hls"] for e in entries] == [f"hls/s{i}" for i in range(len(STREAMS))]
    master = hls.build_master([], {}, renditions, [])
    media = [hls.parse_attributes(line.split(":", 1)[1]) for line in master.splitlines()
             if line.startswith("#EXT-X-MEDIA:")]
    assert [(m["NAME"], m.get("CHARACTERISTICS"), m.get("FORCED")) for m in media] == [
        (name, hls.SDH_CHARACTERISTICS if sdh else None, "YES" if purpose[0] == "forced" else None)
        for _, name, purpose, sdh in BECOMES]


def test_the_record_makes_it_sdh_and_deleting_the_original_costs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw = _raw(STREAMS)
    probe = _probe(raw)
    source, package = _record(raw, probe, _entries(monkeypatch, tmp_path / "subs", probe))
    # The original's streams are for what their dispositions and titles
    # say; the package's subtitles made from them are for the same.
    assert [(s["purpose"], s["purposeFrom"]) for s in source["streams"]
            if s["type"] == "subtitle"] == [purpose for _, _, purpose, _ in BECOMES]
    assert [(s["sourceStreamIndex"], s["purpose"], s["purposeFrom"])
            for s in package["subtitles"]] == [
        (2 + i, *purpose) for i, (_, _, purpose, _) in enumerate(BECOMES)]
    assert package["essence"]["sdhSubtitleLanguages"] == ["de", "en", "fr", "it"]
    assert package["essence"]["sdhSubtitleLanguages"] == source["essence"]["sdhSubtitleLanguages"]
    assert package["essence"]["forcedSubtitleLanguages"] == ["es"]
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []


def test_an_encode_that_lost_the_captions_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The transcoder writes an MP4's text tracks into prepared.mkv as
    # SubRip, and Matroska keeps hearing_impaired but has no word for
    # captions: v0 says nothing of the German track the original flags
    # captions. Its NAME can't say so; its record, read from the original's
    # own probe, still does, as its source's essence does.
    original = _raw([("mov_text", "eng", None, {}),
                     ("mov_text", "eng", None, {"hearing_impaired": 1, "captions": 1}),
                     ("mov_text", "ger", None, {"captions": 1})])
    v0 = _probe(_raw([("subrip", "eng", None, {}),
                      ("subrip", "eng", None, {"hearing_impaired": 1}),
                      ("subrip", "ger", None, {})]))
    entries = _entries(monkeypatch, tmp_path / "subs", v0)
    assert [(e.get("hearingImpaired", False), pk._subtitle_display_name(e)) for e in entries] == [
        (False, "English"), (True, "English · SDH"), (False, "German")]
    source, package = _record(original, v0, entries)
    assert [(s["sourceStreamIndex"], s["purpose"], s["purposeFrom"])
            for s in package["subtitles"]] == [
        (2, "dialogue", "assumed"), (3, "sdh", "disposition"), (4, "sdh", "disposition")]
    assert package["essence"]["sdhSubtitleLanguages"] == ["de", "en"]
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []
