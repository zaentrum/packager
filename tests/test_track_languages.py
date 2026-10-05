"""The catalog's language for a track (the item record's trackLanguages):
which entries count, how an ordinal finds its track when v0 is the
transcoder's encode of the source, and that the remux, the manifest, the
whitelist and the DEFAULT pick all see the effective language (no
binaries: ffmpeg and ffprobe are stubbed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from packager import packager as pk


def _s(codec: str, lang: str | None = None, title: str = "") -> dict:
    tags = {**({"language": lang} if lang else {}), **({"title": title} if title else {})}
    return {"codec_name": codec, "channels": 2, "tags": tags, "disposition": {}}


def _probe(audio: list[dict], subtitles: list[dict] | None = None) -> pk._Probe:
    return pk._Probe(container="matroska,webm", duration_ms=60_000,
                     video={"codec_name": "hevc", "index": 0}, audio=audio,
                     subtitles=subtitles or [], video_index=0)


def _langs(streams: list[dict]) -> list[str]:
    return [pk._track_language(s) for s in streams]


def test_overrides_keep_the_well_formed_entries() -> None:
    assert pk._track_overrides([
        {"kind": "audio", "ordinal": 0, "language": "eng"},
        {"kind": "subtitle", "ordinal": 2, "language": " GER "},
        {"kind": "audio", "ordinal": 1, "language": "zxx"},
        {"kind": "audio", "ordinal": 0, "language": "fre"},  # named twice: the last wins
    ]) == {("audio", 0): "fre", ("subtitle", 2): "ger", ("audio", 1): "zxx"}
    assert pk._track_overrides(None) == {}
    assert pk._track_overrides([]) == {}


@pytest.mark.parametrize("entry", [
    {"kind": "video", "ordinal": 0, "language": "eng"},
    {"kind": "Audio", "ordinal": 0, "language": "eng"},
    {"ordinal": 0, "language": "eng"},
    {"kind": "audio", "language": "eng"},
    {"kind": "audio", "ordinal": -1, "language": "eng"},
    {"kind": "audio", "ordinal": "0", "language": "eng"},
    {"kind": "audio", "ordinal": 0.0, "language": "eng"},
    {"kind": "audio", "ordinal": True, "language": "eng"},
    {"kind": "audio", "ordinal": 0},
    {"kind": "audio", "ordinal": 0, "language": None},
    {"kind": "audio", "ordinal": 0, "language": "en"},
    {"kind": "audio", "ordinal": 0, "language": "english"},
    {"kind": "audio", "ordinal": 0, "language": "en-US"},
    "audio 0 eng",
    None,
])
def test_overrides_ignore_a_malformed_entry(entry) -> None:
    assert pk._track_overrides([entry, {"kind": "audio", "ordinal": 3, "language": "spa"}]) == {
        ("audio", 3): "spa"}


def test_an_override_replaces_the_tag_of_its_track_by_kind_and_ordinal() -> None:
    probe = _probe([_s("ac3", "und", title="AC3 5.1 @ 640 Kbps"), _s("aac")],
                   [_s("subrip", "ger"), _s("subrip", "eng")])
    out = pk._with_track_languages(
        probe, {("audio", 0): "eng", ("audio", 1): "zxx", ("subtitle", 1): "fre"}, None)
    assert _langs(out.audio) == ["eng", "zxx"]
    assert _langs(out.subtitles) == ["ger", "fre"]
    assert out.audio[0]["tags"] == {"language": "eng", "title": "AC3 5.1 @ 640 Kbps"}
    # The probe it was given is as it was.
    assert _langs(probe.audio) == ["und", "und"] and _langs(probe.subtitles) == ["ger", "eng"]


def test_an_override_for_no_track_is_ignored() -> None:
    probe = _probe([_s("aac", "eng")], [_s("subrip", "ger")])
    out = pk._with_track_languages(probe, {("audio", 1): "fre", ("subtitle", 5): "spa"}, None)
    assert out.audio == probe.audio and out.subtitles == probe.subtitles


def test_ordinals_count_the_source_tracks_when_v0_is_an_encode() -> None:
    # The transcoder's prepared.mkv carries every audio track but only the
    # subtitle tracks Matroska can stream-copy: the source's mov_text track
    # is not in it, so the encode's second subtitle is the source's third.
    source = _probe([_s("dts", "eng"), _s("ac3", "ger")],
                    [_s("subrip", "eng"), _s("mov_text", "fre"), _s("subrip", "spa"),
                     _s("hdmv_pgs_subtitle", "ita")])
    encode = _probe([_s("dts", "eng"), _s("ac3", "ger")],
                    [_s("subrip", "eng"), _s("subrip", "spa"), _s("hdmv_pgs_subtitle", "ita")])
    assert pk._source_ordinals(encode.audio, source.audio) == [0, 1]
    assert pk._source_ordinals(encode.subtitles, source.subtitles) == [0, 2, 3]
    out = pk._with_track_languages(
        encode, {("subtitle", 2): "por", ("subtitle", 1): "dut", ("audio", 1): "fre"}, source)
    assert _langs(out.subtitles) == ["eng", "por", "ita"]  # 1 is the mov_text track: not here
    assert _langs(out.audio) == ["eng", "fre"]


def test_ordinals_are_the_packaged_order_without_a_source_that_lines_up() -> None:
    packaged = [_s("subrip"), _s("ass")]
    assert pk._source_ordinals(packaged, None) == [0, 1]
    assert pk._source_ordinals(packaged, [_s("subrip"), _s("ass")]) == [0, 1]
    assert pk._source_ordinals(packaged, [_s("ass"), _s("subrip")]) == [0, 1]
    assert pk._source_ordinals(packaged, [_s("subrip")]) == [0, 1]


def test_the_source_is_probed_only_when_v0_is_an_encode_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[Path] = []
    monkeypatch.setattr(pk, "_ffprobe", lambda p: probed.append(p) or _probe([]))
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x")
    encode = tmp_path / "_inbox" / "prepared.mkv"
    assert pk._probe_of_source(source, source) is None
    assert pk._probe_of_source(encode, tmp_path / "gone.mkv") is None
    assert pk._probe_of_source(encode, source) is not None
    assert probed == [source]


def test_the_remux_and_the_manifest_carry_the_effective_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", lambda label, args: calls.append(args))
    audio = [_s("ac3", "und"), _s("eac3", "ger")]
    audio[1]["channels"] = 6
    probe = pk._with_track_languages(_probe(audio), {("audio", 0): "zxx", ("audio", 1): "fre"},
                                     None)
    surround = pk._surround_plan(probe.audio, {0, 1}, pk.PackageOptions())
    _mp4, meta, surround_meta = pk._prepare_source(
        Path("/m/src.mkv"), probe, tmp_path, audio_visible_indices={0, 1}, default_index=0,
        surround=surround, surround_default=1)
    [args] = calls
    assert [args[i + 1] for i, a in enumerate(args) if a.startswith("-metadata:s:a:")] == [
        "language=zxx", "language=fre", "language=fre"]
    assert [m["language"] for m in meta] == ["zxx", "fre"]
    assert [m["language"] for m in surround_meta] == ["fre"]


def test_the_whitelist_and_the_default_see_the_effective_language() -> None:
    # Two untagged tracks the catalog knows to be German and English.
    probe = pk._with_track_languages(_probe([_s("aac"), _s("aac")]),
                                     {("audio", 0): "ger", ("audio", 1): "eng"}, None)
    visible = pk._visible_indices(probe.audio, ["en"], keep_original_if_single=True)
    assert visible == {1}
    assert pk._pick_default_audio(probe.audio, visible, ["en"]) == 1
