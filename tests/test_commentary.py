"""A commentary and an audio description by their disposition alone: an
audio or a subtitle stream flagged comment, an audio stream flagged
visual_impaired or descriptions, with no title that says so. The original's
record counts them by those dispositions (commentaryTracks,
commentarySubtitles, audioDescriptionTracks), and the package's rendition
made from each was read by its title alone, so the deletion gate named
them as lost. Its manifest entry now says so (commentary, visualImpaired),
its NAME is the one a title that says so gets, and its record makes it a
commentary or an audio description by its disposition, so deleting the
original costs none of them. A title that says so is read as before, its
NAME saying what the record does where the title is a language's code, and
a forced subtitle stays forced. In the master, an audio description says it
describes the video and may be picked by itself; a commentary never is.
The streams are made up; nothing runs ffmpeg or shaka-packager."""

from __future__ import annotations

from pathlib import Path

import pytest

from packager import hls, records
from packager import libv2_records as rec
from packager import packager as pk

NOW = "2026-11-01T10:00:00Z"
SOURCE = "0b6c3d2e-2222-4a2b-8c3d-4e5f60718293"
ORIGINAL = "Example Film (2024).mkv"

# The original's audio streams after its video: (codec, channels, language,
# title, disposition), and what each becomes: its manifest marks, its NAME,
# what its record says it is for and from what.
AUDIO = [
    (("eac3", 6, "eng", None, {"default": 1}),
     ({}, "English", ("main", "assumed"))),
    (("aac", 2, "eng", "Director", {"comment": 1}),
     ({"commentary": True}, "English · Director · Commentary", ("commentary", "disposition"))),
    (("aac", 2, "eng", "Commentary", {}),
     ({}, "English · Commentary", ("commentary", "title"))),
    # Flagged a commentary and titled one: named so once.
    (("aac", 2, "eng", "Commentary", {"comment": 1}),
     ({"commentary": True}, "English · Commentary", ("commentary", "disposition"))),
    (("aac", 2, "ger", None, {"visual_impaired": 1}),
     ({"visualImpaired": True}, "German · Audio description", ("description", "disposition"))),
    (("aac", 2, "fre", "Audiodescription", {"descriptions": 1}),
     ({"visualImpaired": True}, "French · Audiodescription", ("description", "disposition"))),
    # An audio description by a title its NAME leaves out, read as a code of
    # the language: named as its record has it.
    (("aac", 2, "fre", "fr-audiodescription", {}),
     ({}, "French · Audio description", ("description", "title"))),
]
# Its subtitle streams after them: (codec, language, title, disposition),
# and the same of each.
SUBTITLES = [
    (("subrip", "eng", None, {}),
     ({}, "English", ("dialogue", "assumed"))),
    (("subrip", "eng", None, {"comment": 1}),
     ({"commentary": True}, "English · Commentary", ("commentary", "disposition"))),
    (("subrip", "eng", None, {"comment": 1, "forced": 1}),
     ({"commentary": True}, "English (forced)", ("forced", "disposition"))),
    (("subrip", "ger", "Kommentar", {"comment": 1}),
     ({"commentary": True}, "German · Kommentar", ("commentary", "disposition"))),
    (("hdmv_pgs_subtitle", "spa", "Comentario del director", {}),
     ({}, "Spanish · Comentario del director", ("commentary", "title"))),
    (("subrip", "fre", "fr-commentaire", {}),
     ({}, "French · Commentary", ("commentary", "title"))),
]


def _raw(audio: list[tuple], subtitles: list[tuple]) -> dict:
    """The probe of a file with a video and these audio and subtitle
    streams, as ffprobe reports it."""
    streams: list[dict] = [{"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920,
                            "height": 1080, "pix_fmt": "yuv420p10le", "disposition": {"default": 1},
                            "tags": {}}]
    for codec, channels, language, title, disposition in audio:
        streams.append({"index": len(streams), "codec_type": "audio", "codec_name": codec,
                        "channels": channels, "disposition": disposition,
                        "tags": {"language": language, **({"title": title} if title else {})}})
    for codec, language, title, disposition in subtitles:
        streams.append({"index": len(streams), "codec_type": "subtitle", "codec_name": codec,
                        "disposition": disposition,
                        "tags": {"language": language, **({"title": title} if title else {})}})
    return {"streams": streams, "chapters": [],
            "format": {"format_name": "matroska,webm", "duration": "60.000000"}}


def _probe(raw: dict) -> pk._Probe:
    streams = raw["streams"]
    return pk._Probe(container="matroska,webm", duration_ms=60_000, video=streams[0],
                     audio=[s for s in streams if s["codec_type"] == "audio"],
                     subtitles=[s for s in streams if s["codec_type"] == "subtitle"],
                     video_index=0)


def _audio(probe: pk._Probe) -> list[dict]:
    """The manifest's stereo entries, as _prepare_source makes them."""
    return [{**pk._audio_meta_from_stream(i, s, transcoded=True), "id": f"a{i}",
             "dir": f"hls/a{i}", "codec": "mp4a.40.2", "default": i == 0, "visible": True}
            for i, s in enumerate(probe.audio)]


def _subtitles(monkeypatch: pytest.MonkeyPatch, folder: Path, probe: pk._Probe) -> list[dict]:
    """The manifest's subtitle entries, as _extract_subtitles makes them."""
    def together(_src: Path, subs: Path, sidecars: list) -> set[int]:
        for c in sidecars:
            (subs / c.name).write_text("WEBVTT\n\n00:01.000 --> 00:02.000\nWell.\n")
        return {c.index for c in sidecars}

    monkeypatch.setattr(pk, "_extract_together", together)
    monkeypatch.setattr(pk, "_extract_alone", lambda src, subs, c, codec: (
        (subs / c.name).write_bytes(b"\x50\x47") and True))
    return pk._extract_subtitles(Path("/m/original.mkv"), probe, folder)


def _records(raw: dict, probe: pk._Probe, audio: list[dict], subtitles: list[dict]
             ) -> tuple[dict, dict]:
    source, _ = rec.source_record(
        SOURCE, ORIGINAL, 1, taken_at=NOW, taken_by="packager",
        library_path=f"Example Film (2024)/{ORIGINAL}", qh1="sha256:" + "0" * 64,
        probe=raw, probe_version="ffprobe test")
    manifest = {
        "packagedAt": NOW, "packager": "packager-test", "durationMs": 60_000,
        "renditions": {
            "video": [{"id": "v0", "dir": "hls/v0", "codec": "hvc1.2.4.L120.B0", "width": 1920,
                       "height": 1080}],
            "audio": audio, "audioSurround": []},
        "subtitles": subtitles,
    }
    package = records.package_record(
        "c0ffee00-0000-4000-8000-0000000000bb", pk.Built(manifest, probe, {}), [], source,
        records.OriginalProbe(raw, "ffprobe test"), {}, None, NOW)
    return source, package


def test_the_manifest_entries_and_the_names(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    probe = _probe(_raw([a for a, _ in AUDIO], [s for s, _ in SUBTITLES]))
    audio = _audio(probe)
    assert [{k: e[k] for k in ("commentary", "visualImpaired") if k in e} for e in audio] == [
        marks for _, (marks, _, _) in AUDIO]
    assert [pk._audio_display_name(e) for e in audio] == [name for _, (_, name, _) in AUDIO]
    subs = _subtitles(monkeypatch, tmp_path / "subs", probe)
    assert [{k: e[k] for k in ("commentary",) if k in e} for e in subs] == [
        marks for _, (marks, _, _) in SUBTITLES]
    assert [pk._subtitle_display_name(e) for e in subs] == [
        name for _, (_, name, _) in SUBTITLES]
    # A track that says nothing of it is as it was: no mark.
    assert "commentary" not in audio[0] and "visualImpaired" not in audio[0]
    assert set(subs[0]) == {"id", "language", "title", "default", "forced", "visible", "path",
                            "format"}


def test_the_record_makes_them_so_and_deleting_the_original_costs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw = _raw([a for a, _ in AUDIO], [s for s, _ in SUBTITLES])
    probe = _probe(raw)
    source, package = _records(raw, probe, _audio(probe),
                               _subtitles(monkeypatch, tmp_path / "subs", probe))
    assert [(s["purpose"], s["purposeFrom"]) for s in source["streams"] if s["type"] == "audio"] \
        == [purpose for _, (_, _, purpose) in AUDIO]
    assert [(a["purpose"], a["purposeFrom"]) for a in package["renditions"]["audio"]] == [
        purpose for _, (_, _, purpose) in AUDIO]
    assert [(s["purpose"], s["purposeFrom"]) for s in package["subtitles"]] == [
        purpose for _, (_, _, purpose) in SUBTITLES]
    counts = ("commentaryTracks", "commentarySubtitles", "audioDescriptionTracks")
    assert {k: package["essence"][k] for k in counts} == {k: source["essence"][k] for k in counts} \
        == {"commentaryTracks": 3, "commentarySubtitles": 4, "audioDescriptionTracks": 3}
    assert [x for x in rec.deletion_gate([source["essence"]], package["essence"])
            if x in counts] == []


def test_without_the_marks_the_gate_names_them(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    # What a manifest written before said of the same tracks: titles alone.
    raw = _raw([a for a, _ in AUDIO], [s for s, _ in SUBTITLES])
    probe = _probe(raw)
    plain = lambda entries: [{k: v for k, v in e.items()  # noqa: E731
                              if k not in ("commentary", "visualImpaired")} for e in entries]
    audio = plain(_audio(probe))
    subs = plain(_subtitles(monkeypatch, tmp_path / "subs", probe))
    manifest = {"renditions": {"video": [{"id": "v0", "height": 1080}],
                               "audio": [rec.audio_rendition(a, i) for i, a in enumerate(audio)]},
                "subtitles": [rec.subtitle_rendition(s, i) for i, s in enumerate(subs)]}
    source, _ = rec.source_record(
        SOURCE, ORIGINAL, 1, taken_at=NOW, taken_by="packager",
        library_path=f"Example Film (2024)/{ORIGINAL}", qh1="sha256:" + "0" * 64,
        probe=raw, probe_version="ffprobe test")
    lost = rec.deletion_gate([source["essence"]], rec.package_essence(manifest, source["essence"]))
    assert [x for x in lost if x in ("commentaryTracks", "commentarySubtitles",
                                     "audioDescriptionTracks")] == [
        "audioDescriptionTracks", "commentarySubtitles", "commentaryTracks"]


def test_an_encode_that_lost_a_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The transcoder's prepared.mkv has no word for an MP4's descriptions,
    # and here kept no kind of its subtitle either: v0 says nothing of the
    # French audio description or of the English commentary. Their NAMEs
    # can't say so; their records, read from the original's own probe,
    # still do.
    original = _raw([("aac", 2, "eng", None, {"default": 1}),
                     ("aac", 2, "fre", None, {"descriptions": 1})],
                    [("mov_text", "eng", None, {"comment": 1})])
    v0 = _probe(_raw([("aac", 2, "eng", None, {"default": 1}), ("aac", 2, "fre", None, {})],
                     [("subrip", "eng", None, {})]))
    audio = _audio(v0)
    assert [pk._audio_display_name(a) for a in audio] == ["English", "French"]
    subs = _subtitles(monkeypatch, tmp_path / "subs", v0)
    assert [pk._subtitle_display_name(s) for s in subs] == ["English"]
    source, package = _records(original, v0, audio, subs)
    assert [(a["sourceStreamIndex"], a["purpose"], a["purposeFrom"])
            for a in package["renditions"]["audio"]] == [
        (1, "main", "assumed"), (2, "description", "disposition")]
    assert [(s["sourceStreamIndex"], s["purpose"], s["purposeFrom"])
            for s in package["subtitles"]] == [(3, "commentary", "disposition")]
    assert rec.deletion_gate([source["essence"]], package["essence"]) == []


def test_the_master_marks_an_audio_description_and_never_autoselects_a_commentary() -> None:
    # Apple's HLS authoring specification: an audio description says it
    # describes the video, and a player may pick it by itself (AUTOSELECT)
    # where its viewer asks for one; a commentary no player should pick by
    # itself. Neither takes the place of its language's main audio, the
    # first visible one of it; a hidden one is never picked.
    probe = _probe(_raw([a for a, _ in AUDIO], []))
    assert [pk._audio_characteristics(e) for e in _audio(probe)] == [
        hls.DESCRIBES_VIDEO if purpose == "description" else ""
        for _, (_, _, (purpose, _)) in AUDIO]
    metas = [
        {"language": "ger", "title": "", "visualImpaired": True, "visible": True},
        {"language": "ger", "title": "", "visible": True},
        {"language": "eng", "title": "", "commentary": True, "visible": True},
        {"language": "eng", "title": "Director's commentary", "visible": True},
        {"language": "eng", "title": "", "visible": True},
        {"language": "eng", "title": "", "visible": True},
        {"language": "fre", "title": "Audio description", "visible": False},
        {"language": "fre", "title": "", "visible": False},
    ]
    stats = hls.PlaylistStats(peak_bps=0, avg_bps=0, segments=1, target_duration=6, duration=6.0)
    renditions = [hls.AudioRendition(f"a{i}/playlist.m3u8", "audio", pk._lang_key(m["language"]),
                                     f"Track {i}", False, False, "2", "mp4a.40.2", stats,
                                     pk._audio_characteristics(m))
                  for i, m in enumerate(metas)]
    done = pk._finish_group(renditions, metas)
    assert [(r.autoselect, r.characteristics) for r in done] == [
        (True, hls.DESCRIBES_VIDEO), (True, ""), (False, ""), (False, ""), (True, ""), (False, ""),
        (False, hls.DESCRIBES_VIDEO), (False, "")]
    lines = [line for line in hls.build_master([], {"audio": done}, None, []).splitlines()
             if line.startswith("#EXT-X-MEDIA")]
    assert lines[0] == (
        '#EXT-X-MEDIA:TYPE=AUDIO,URI="a0/playlist.m3u8",GROUP-ID="audio",LANGUAGE="de",'
        'NAME="Track 0",DEFAULT=NO,AUTOSELECT=YES,CHANNELS="2",'
        'CHARACTERISTICS="public.accessibility.describes-video"')
    assert "AUTOSELECT" not in lines[2] and "CHARACTERISTICS" not in lines[2]
