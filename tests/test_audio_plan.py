"""Language matching, rendition names, the DEFAULT track, the 5.1
companions and the remux command line (no ffmpeg run: the runner is
stubbed)."""

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


def test_whitelist_never_hides_a_track_without_dialogue() -> None:
    # zxx, no linguistic content: a dialogue-free film's track. Visible
    # whatever the whitelist, as und is. With a dubbed track beside it the
    # one-language fallback didn't apply, and nothing was visible.
    streams = [_a("zxx"), _a("ger"), _a("jpn")]
    assert pk._visible_indices(streams, ["en"], keep_original_if_single=True) == {0}
    assert pk._visible_indices([_a("zxx")], ["en"], keep_original_if_single=False) == {0}
    assert pk._visible_indices([_a("zxx"), _a("und"), _a("eng"), _a("fre")], ["en"],
                               keep_original_if_single=False) == {0, 1, 2}


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
        _a("eng", 8, "truehd"),           # 7.1: encoded, downmixed -> E-AC-3 5.1
        _a("eng", 6, "ac3"),              # second English surround: skipped
        _a("ger", 6, "eac3"),             # already an E-AC-3 5.1: copied
        _a("eng", 6, "dts", comment=True),
        _a("fre", 6, "dts"),              # hidden by the whitelist: a companion all the same
        _a("ita", 2, "aac"),              # stereo
    ]
    plan = pk._surround_plan(streams, {0, 1, 2, 3, 5}, pk.PackageOptions())
    # Every surround track's language keeps its 5.1: the package is what is
    # left of the original once it is deleted.
    assert [(s.source_index, s.mode, s.hls_codec) for s in plan] == [
        (0, "encode", "ec-3"), (2, "copy", "ec-3"), (4, "encode", "ec-3")]
    assert pk._surround_plan(streams, {0}, pk.PackageOptions(surround_codec="off")) == []
    ac3 = pk._surround_plan(streams[1:], {0}, pk.PackageOptions(surround_codec="ac3"))
    assert [(s.source_index, s.mode, s.hls_codec) for s in ac3] == [
        (0, "copy", "ac-3"), (1, "encode", "ac-3"), (3, "encode", "ac-3")]


@pytest.mark.parametrize(("channels", "codec", "mode"), [
    (6, "eac3", "copy"),        # an E-AC-3 5.1 (Atmos too) is kept as it is
    (8, "eac3", "encode"),      # an E-AC-3 7.1 is made a 5.1, as every companion is
    (5, "eac3", "encode"),      # 5.0: up to 5.1
    (3, "aac", "encode"),       # 3.0 is surround to a record's essence: > 2 channels
    (4, "flac", "encode"),
    (8, "pcm_s24le", "encode"),
])
def test_every_surround_track_gets_a_5_1_companion(channels: int, codec: str, mode: str) -> None:
    [s] = pk._surround_plan([_a("eng", channels, codec)], {0}, pk.PackageOptions())
    assert (s.source_index, s.mode, s.codec, s.hls_codec, s.bitrate) == (
        0, mode, "eac3", "ec-3", "448k")


def test_a_stereo_or_mono_source_gets_no_companion() -> None:
    streams = [_a("eng", 2, "ac3"), _a("ger", 1, "aac"), _a("fre", 2, "eac3")]
    assert pk._surround_plan(streams, {0, 1, 2}, pk.PackageOptions()) == []


def test_the_default_companion_is_a_shown_one() -> None:
    # English hidden by the whitelist (the default stereo is French, which
    # has no 5.1): the German 5.1 is the group's default, not the English.
    streams = [_a("eng", 6, "dts"), _a("ger", 6, "dts"), _a("fre", 2, "aac")]
    plan = pk._surround_plan(streams, {1, 2}, pk.PackageOptions())
    assert [s.source_index for s in plan] == [0, 1]
    assert pk._pick_default_surround(plan, streams, 2, ["en", "de"], {1, 2}) == 1
    # Without a shown one, a hidden one: the group still has its default.
    assert pk._pick_default_surround(plan, streams, 2, ["en"], {2}) == 0
    assert pk._pick_default_surround(plan, streams, 2, ["en"]) == 0



def test_surround_group_gets_exactly_one_default() -> None:
    # The stereo default's own companion when it has one ...
    streams = [_a("eng", 6, "dts"), _a("ger", 6, "eac3", default=True), _a("fre", 2)]
    plan = pk._surround_plan(streams, {0, 1, 2}, pk.PackageOptions())
    assert pk._pick_default_surround(plan, streams, 1, ["de", "en"]) == 1
    # ... else the 5.1 of the default's language (another track of it) ...
    streams = [_a("ger", 2, "aac"), _a("eng", 6, "dts"), _a("ger", 6, "dts")]
    plan = pk._surround_plan(streams, {0, 1, 2}, pk.PackageOptions())
    assert pk._pick_default_surround(plan, streams, 0, ["de"]) == 2
    # ... else the first preferred language with one: a German default
    # that has no 5.1 left the group without any DEFAULT=YES.
    streams = [_a("ger", 2, "ac3"), _a("fre", 6, "dts"), _a("eng", 6, "dts")]
    plan = pk._surround_plan(streams, {0, 1, 2}, pk.PackageOptions())
    assert pk._pick_default_surround(plan, streams, 0, ["de", "en", "fr"]) == 2
    # ... else the first.
    assert pk._pick_default_surround(plan, streams, 0, []) == 1
    assert pk._pick_default_surround([], streams, 0, ["de"]) is None


def test_prepare_source_marks_the_picked_surround_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: None)
    audio = [_a("ger", 2, "ac3"), _a("eng", 6, "dts")]
    surround = pk._surround_plan(audio, {0, 1}, pk.PackageOptions())
    _mp4, meta, surround_meta = pk._prepare_source(
        Path("/m/src.mkv"), _probe(audio), tmp_path, audio_visible_indices={0, 1},
        default_index=0, surround=surround, surround_default=1,
    )
    assert [m["default"] for m in meta] == [True, False]
    assert [(m["idx"], m["default"]) for m in surround_meta] == [(1, True)]

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
        default_index=1, surround=surround,
        surround_default=pk._pick_default_surround(surround, audio, 1, []),
        timeline="offset", ts_offset=0.021,
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


def test_a_hidden_tracks_companion_is_hidden_too(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    audio = [_a("eng", 6, "dts"), _a("fre", 8, "truehd"), _a("ger", 2, "aac")]
    visible = {0, 2}
    surround = pk._surround_plan(audio, visible, pk.PackageOptions())
    _mp4, meta, surround_meta = pk._prepare_source(
        Path("/m/src.mkv"), _probe(audio), tmp_path, audio_visible_indices=visible,
        default_index=0, surround=surround,
        surround_default=pk._pick_default_surround(surround, audio, 0, ["en"], visible))
    assert [(m["idx"], m["visible"]) for m in meta] == [(0, True), (1, False), (2, True)]
    assert [(m["idx"], m["channels"], m["default"], m["visible"], m["mode"])
            for m in surround_meta] == [(0, 6, True, True, "encode"),
                                        (1, 6, False, False, "encode")]
    # The 7.1 is made a 5.1 like the others: the same filter, a 5.1 layout.
    graph = calls[0][calls[0].index("-filter_complex") + 1]
    assert "[am1]aformat=sample_rates=48000:channel_layouts=5.1(side)|5.1[m1]" in graph


def test_prepare_source_legacy_timeline_has_no_copyts(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    pk._prepare_source(Path("/m/src.mkv"), _probe([_a("eng")], codec="h264"), tmp_path)
    [args] = calls
    assert "-copyts" not in args and "-output_ts_offset" not in args
    assert "-tag:v" not in args  # hvc1 only for HEVC


@pytest.mark.parametrize(("tag", "name"), [
    ("eng", "English"), ("ger", "German"), ("deu", "German"), ("de", "German"),
    ("de-CH", "German"), ("DUT", "Dutch"), ("nld", "Dutch"), ("pol", "Polish"),
    ("vie", "Vietnamese"), ("nob", "Norwegian Bokmål"), ("gsw", "Swiss German"),
    ("zxx", "No dialogue"), ("und", "Unknown"), ("", "Unknown"), (None, "Unknown"),
    ("qaa", "qaa"),  # a code it doesn't know is named by itself
])
def test_language_name(tag, name) -> None:
    assert pk._language_name(tag) == name


@pytest.mark.parametrize(("lang", "title", "name"), [
    # A title that says what the track is stays, after its language.
    ("eng", "Commentary", "English · Commentary"),
    ("eng", "Director's commentary", "English · Director's commentary"),
    ("eng", "Audio description", "English · Audio description"),
    ("eng", "English - Commentary", "English · Commentary"),
    ("eng", f"English {chr(0x2013)} Commentary", "English · Commentary"),  # an en dash
    ("eng", "Commentary 5.1", "English · Commentary"),      # the layout goes
    ("ger", "Deutsch (Kommentar)", "German · Kommentar"),
    ("zxx", "Music & Effects", "No dialogue · Music & Effects"),
    # Noise goes: Sintel's codec descriptor, formats, numbers, the language.
    ("eng", "AC3 5.1 @ 640 Kbps", "English"),
    ("eng", "DTS-HD MA", "English"),
    ("eng", "AAC 2.0", "English"),
    ("eng", "Commentary (AC3)", "English"),                  # as the clients have it
    ("eng", "Stereo", "English"),
    ("eng", "Track 0", "English"),
    ("eng", "#2", "English"),
    ("eng", "English", "English"),
    ("eng", "eng", "English"),
    ("ger", "Deutsch", "German"),
    ("zxx", "No dialogue", "No dialogue"),
    ("eng", "", "English"),
    # No known language: what the title says, else "Unknown" (was "Track 0").
    ("und", "", "Unknown"),
    ("und", "und", "Unknown"),
    ("und", "Commentary", "Commentary"),
    ("und", "English", "English"),
    ("zxx", "", "No dialogue"),
])
def test_audio_display_name(lang, title, name) -> None:
    assert pk._audio_display_name({"language": lang, "title": title}) == name


def test_audio_display_name_in_the_51_group_and_a_long_title() -> None:
    assert pk._audio_display_name({"language": "eng", "title": "Original"},
                                  surround=True) == "English 5.1 · Original"
    assert pk._audio_display_name({"language": "eng", "title": "AC3 5.1 @ 640 Kbps"},
                                  surround=True) == "English 5.1"
    # What a title adds is cut at a word, ellipsis included, at 40.
    long = "Commentary by the director and the writer, recorded in 2011"
    name = pk._audio_display_name({"language": "eng", "title": long})
    assert name == "English · Commentary by the director and the…"
    assert len(name.removeprefix("English · ")) <= 40
    word = pk._audio_display_name({"language": "eng", "title": "x" * 60})
    assert word == "English · " + "x" * 39 + "…"


@pytest.mark.parametrize(("entry", "name"), [
    ({"language": "eng", "title": "", "forced": False}, "English"),
    ({"language": "eng", "title": "SDH", "forced": False}, "English · SDH"),
    ({"language": "eng", "title": "English (SDH)", "forced": False}, "English · SDH"),
    ({"language": "eng", "title": "Signs & Songs", "forced": False}, "English · Signs & Songs"),
    ({"language": "eng", "title": "Forced", "forced": True}, "English · Forced"),
    ({"language": "eng", "title": "English (Forced)", "forced": True}, "English · Forced"),
    ({"language": "ger", "title": "", "forced": True}, "German (forced)"),
    ({"language": "eng", "title": "Track 3", "forced": True}, "English (forced)"),
    ({"language": "fre", "title": "Français", "forced": False}, "French"),
    ({"language": "zxx", "title": "", "forced": False}, "No dialogue"),
    ({"language": "und", "title": "Signs & Songs", "forced": False}, "Signs & Songs"),
    ({"language": "und", "title": "", "forced": False}, "Unknown"),  # was "Subtitles 4"
])
def test_subtitle_display_name(entry, name) -> None:
    assert pk._subtitle_display_name(entry) == name
