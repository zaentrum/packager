"""Language matching, the DEFAULT track, the 5.1 companions and the
remux command line (no ffmpeg run: the runner is stubbed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from packager import packager as pk


def _a(lang: str, ch: int = 2, codec: str = "aac", title: str = "", default: bool = False,
       comment: bool = False) -> dict:
    return {"codec_name": codec, "channels": ch,
            "tags": {"language": lang, **({"title": title} if title else {})},
            "disposition": {"default": int(default), "comment": int(comment)}}


@pytest.mark.parametrize(("tag", "key"), [
    ("eng", "en"), ("ger", "de"), ("deu", "de"), ("de", "de"), ("de-CH", "de"),
    ("fre", "fr"), ("chi", "zh"), ("gsw", "gsw"), (None, "und"), ("", "und"),
])
def test_lang_key(tag, key) -> None:
    assert pk._lang_key(tag) == key


def test_whitelist_matches_bibliographic_codes() -> None:
    # "ger"[:2] is "ge" — the old prefix match hid German tracks from a
    # "de" whitelist.
    streams = [_a("eng"), _a("ger"), _a("fre"), _a("und")]
    assert pk._visible_indices(streams, ["de", "en"], keep_original_if_single=True) == {0, 1, 3}


def test_default_follows_preference_then_source_flag() -> None:
    streams = [_a("eng", default=True), _a("ger"), _a("ger", title="Kommentar")]
    visible = {0, 1, 2}
    assert pk._pick_default_audio(streams, visible, ["de", "en"]) == 1
    assert pk._pick_default_audio(streams, visible, ["fr", "en"]) == 0
    assert pk._pick_default_audio(streams, visible, []) == 0      # source default flag
    assert pk._pick_default_audio([], visible, ["de"]) is None


def test_default_skips_commentary_and_hidden_tracks() -> None:
    streams = [_a("eng", comment=True, default=True), _a("eng"), _a("jpn")]
    assert pk._pick_default_audio(streams, {0, 1, 2}, ["en"]) == 1
    assert pk._pick_default_audio(streams, {2}, []) == 2


def test_surround_plan_first_per_language_copy_or_encode() -> None:
    streams = [
        _a("eng", 8, "truehd"),           # encode -> E-AC-3 5.1
        _a("eng", 6, "ac3"),              # second English surround: skipped
        _a("ger", 6, "eac3"),             # already E-AC-3: copied
        _a("eng", 6, "dts", comment=True),
        _a("fre", 6, "dts"),              # hidden by the whitelist
        _a("ita", 2, "aac"),              # stereo
    ]
    plan = pk._surround_plan(streams, {0, 1, 2, 3, 5}, pk.PackageOptions())
    assert [(s.source_index, s.mode, s.hls_codec) for s in plan] == [
        (0, "encode", "ec-3"), (2, "copy", "ec-3")]
    assert pk._surround_plan(streams, {0}, pk.PackageOptions(surround_codec="off")) == []
    ac3 = pk._surround_plan(streams, {1}, pk.PackageOptions(surround_codec="ac3"))
    assert [(s.mode, s.hls_codec) for s in ac3] == [("copy", "ac-3")]


def _probe(audio: list[dict], codec: str = "hevc") -> pk._Probe:
    return pk._Probe(container="matroska,webm", duration_ms=60_000,
                     video={"codec_name": codec, "index": 0}, audio=audio, subtitles=[],
                     video_index=0)


def test_prepare_source_command(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    audio = [_a("eng", 6, "dts"), _a("ger", 6, "eac3"), _a("eng", 2, "aac", title="Commentary")]
    probe = _probe(audio)
    surround = pk._surround_plan(audio, {0, 1, 2}, pk.PackageOptions())
    mp4, meta, surround_meta = pk._prepare_source(
        Path("/m/src.mkv"), probe, tmp_path, audio_visible_indices={0, 1, 2},
        default_index=1, surround=surround, timeline="offset", ts_offset=0.021,
    )
    [args] = calls
    assert args[:args.index("-i")] == [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-copyts"]
    graph = args[args.index("-filter_complex") + 1]
    assert graph == ";".join([
        "[0:a:0]asplit=2[as0][am0]",
        "[as0]aformat=sample_rates=48000:channel_layouts=stereo[s0]",
        "[am0]aformat=sample_rates=48000:channel_layouts=5.1(side)|5.1[m0]",
        "[0:a:1]aformat=sample_rates=48000:channel_layouts=stereo[s1]",
        "[0:a:2]aformat=sample_rates=48000:channel_layouts=stereo[s2]",
    ])
    head = args[:args.index("-filter_complex")]
    assert head[-8:] == ["-i", "/m/src.mkv", "-map", "0:0", "-c:v", "copy", "-tag:v", "hvc1"]
    tail = args[args.index("-filter_complex") + 2:]
    assert tail == [
        "-map", "[s0]", "-c:a:0", "aac", "-b:a:0", "192k", "-metadata:s:a:0", "language=eng",
        "-map", "[s1]", "-c:a:1", "aac", "-b:a:1", "192k", "-metadata:s:a:1", "language=ger",
        "-map", "[s2]", "-c:a:2", "aac", "-b:a:2", "192k", "-metadata:s:a:2", "language=eng",
        "-map", "[m0]", "-c:a:3", "eac3", "-b:a:3", "448k", "-metadata:s:a:3", "language=eng",
        "-map", "0:a:1", "-c:a:4", "copy", "-metadata:s:a:4", "language=ger",
        "-sn", "-dn", "-output_ts_offset", "0.021000",
        "-movflags", "+faststart", str(tmp_path / "transmux.mp4"),
    ]
    assert mp4 == tmp_path / "transmux.mp4"
    assert [m["default"] for m in meta] == [False, True, False]
    assert [(m["idx"], m["codec"], m["channels"], m["default"], m["mode"])
            for m in surround_meta] == [(0, "ec-3", 6, False, "encode"),
                                        (1, "ec-3", 6, True, "copy")]


def test_prepare_source_legacy_timeline_has_no_copyts(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    pk._prepare_source(Path("/m/src.mkv"), _probe([_a("eng")], codec="h264"), tmp_path)
    [args] = calls
    assert "-copyts" not in args and "-output_ts_offset" not in args
    assert "-tag:v" not in args  # hvc1 only for HEVC


@pytest.mark.parametrize(("entry", "name"), [
    ({"language": "eng", "title": "", "forced": False}, "English"),
    ({"language": "eng", "title": "Forced", "forced": True}, "English (Forced)"),
    ({"language": "ger", "title": "", "forced": True}, "German (forced)"),
    ({"language": "eng", "title": "SDH", "forced": False}, "English (SDH)"),
    ({"language": "eng", "title": "Director's Commentary", "forced": False},
     "Director's Commentary"),
    ({"language": "und", "title": "", "forced": False}, "Subtitles 4"),
])
def test_subtitle_display_name(entry, name) -> None:
    assert pk._subtitle_display_name(entry, "4") == name
