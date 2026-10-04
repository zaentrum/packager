"""Master playlist assembly: attribute parsing, RFC 8216 bit rates, the
shaka-master reader and a golden master (no binaries needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from packager import hls

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


def test_parse_attributes_keeps_quoted_commas() -> None:
    attrs = hls.parse_attributes(
        'BANDWIDTH=1200,CODECS="hvc1.1.6.L120.90,mp4a.40.2",RESOLUTION=1920x1080,'
        'NAME="A, B",FRAME-RATE=23.976'
    )
    assert attrs == {
        "BANDWIDTH": "1200",
        "CODECS": "hvc1.1.6.L120.90,mp4a.40.2",
        "RESOLUTION": "1920x1080",
        "NAME": "A, B",
        "FRAME-RATE": "23.976",
    }


def test_quoted_strips_what_hls_forbids() -> None:
    assert hls.quoted('Director "cut"\n') == "\"Director 'cut' \""


def test_unique_names() -> None:
    assert hls.unique_names(["English", "German", "English", "English"]) == [
        "English", "German", "English (2)", "English (3)",
    ]


def test_playlist_uris() -> None:
    master = (FIXTURES / "shaka-master.m3u8").read_text()
    assert hls.playlist_uris(master) == [
        "a0/playlist.m3u8", "a1/playlist.m3u8", "a2/playlist.m3u8",
        "s1/playlist.m3u8", "s2/playlist.m3u8", "s0/playlist.m3u8",
        *[f"v{i}/playlist.m3u8" for i in range(3)] * 2,
        *[f"v{i}/iframes.m3u8" for i in range(3)],
    ]
    media = ('#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MAP:URI="init.mp4"\n'
             "#EXTINF:6.000,\n#EXT-X-BYTERANGE:17748@84\nseg-00001.m4s\n"
             "#EXTINF:2.000,\n\nseg-00002.m4s\n#EXT-X-ENDLIST\n")
    assert hls.playlist_uris(media) == ["init.mp4", "seg-00001.m4s", "seg-00002.m4s"]


def _playlist(tmp: Path, segs: list[tuple[float, int]], target: int, byterange=False) -> Path:
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-TARGETDURATION:{target}",
             "#EXT-X-PLAYLIST-TYPE:VOD", '#EXT-X-MAP:URI="init.mp4"']
    (tmp / "init.mp4").write_bytes(b"x" * 999)  # never counted
    for n, (dur, size) in enumerate(segs, 1):
        lines.append(f"#EXTINF:{dur:.3f},")
        if byterange:
            lines.append(f"#EXT-X-BYTERANGE:{size}@0")
            lines.append("all.m4s")
        else:
            name = f"seg-{n:05d}.m4s"
            (tmp / name).write_bytes(b"\0" * size)
            lines.append(name)
    lines.append("#EXT-X-ENDLIST")
    path = tmp / "playlist.m3u8"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_playlist_stats_peak_is_rfc_windowed(tmp_path: Path) -> None:
    # 6 s target: windows must last 3..9 s. The 2 s tail spike (8 Mbit/s
    # alone) only ever counts together with the segment before it.
    segs = [(6.0, 750_000), (6.0, 1_500_000), (6.0, 750_000), (2.0, 2_000_000)]
    stats = hls.playlist_stats(_playlist(tmp_path, segs, 6))
    assert stats.segments == 4
    assert stats.target_duration == 6
    assert stats.duration == pytest.approx(20.0)
    assert stats.avg_bps == round(5_000_000 * 8 / 20)        # 2.0 Mbit/s
    assert stats.peak_bps == round((750_000 + 2_000_000) * 8 / 8)  # 2.75 > 2.0


def test_playlist_stats_byterange(tmp_path: Path) -> None:
    stats = hls.playlist_stats(_playlist(tmp_path, [(6.0, 600_000), (6.0, 300_000)], 6,
                                         byterange=True))
    assert stats.peak_bps == 800_000
    assert stats.avg_bps == 600_000


def test_single_short_segment_falls_back_to_its_own_rate(tmp_path: Path) -> None:
    stats = hls.playlist_stats(_playlist(tmp_path, [(1.0, 1000)], 6))
    assert stats.peak_bps == 8000


def test_read_shaka_master_fixture() -> None:
    """A real shaka-packager v3.4.2 master: 3 video rungs, a stereo and an
    E-AC-3 group, three text tracks."""
    m = hls.read_shaka_master(FIXTURES / "shaka-master.m3u8")
    assert list(m.video) == ["v0/playlist.m3u8", "v1/playlist.m3u8", "v2/playlist.m3u8"]
    assert m.video["v0/playlist.m3u8"]["CODECS"] == "hvc1.1.6.L120.90,mp4a.40.2"
    assert m.video["v1/playlist.m3u8"]["RESOLUTION"] == "1280x720"
    assert m.group_codecs == {"audio": ["mp4a.40.2"], "audio-eac3": ["ec-3"]}
    assert m.media["a1/playlist.m3u8"]["LANGUAGE"] == "de"
    assert m.media["a2/playlist.m3u8"]["CHANNELS"] == "6"
    assert m.media["s1/playlist.m3u8"]["FORCED"] == "YES"
    assert len(m.iframes) == 3 and 'URI="v2/iframes.m3u8"' in m.iframes[2]


def _stats(peak: int, avg: int) -> hls.PlaylistStats:
    return hls.PlaylistStats(peak_bps=peak, avg_bps=avg, segments=10, target_duration=6,
                             duration=60.0)


def _golden_inputs():
    videos = [
        hls.VideoVariant("v0/playlist.m3u8", "hvc1.2.4.L150.90", 3840, 1606, 23.976, "PQ",
                         _stats(9_000_000, 7_000_000)),
        hls.VideoVariant("v1/playlist.m3u8", "avc1.64001f", 1280, 536, 23.976, "SDR",
                         _stats(2_800_000, 1_900_000)),
    ]
    audio = {
        "audio": [
            hls.AudioRendition("a0/playlist.m3u8", "audio", "en", "English", False, True, "2",
                               "mp4a.40.2", _stats(200_000, 195_000)),
            hls.AudioRendition("a1/playlist.m3u8", "audio", "de", "German", True, True, "2",
                               "mp4a.40.2", _stats(198_000, 194_000)),
            hls.AudioRendition("a2/playlist.m3u8", "audio", "en", "Commentary", False, False, "2",
                               "mp4a.40.2", _stats(190_000, 180_000)),
        ],
        "audio-surround": [
            hls.AudioRendition("a3/playlist.m3u8", "audio-surround", "en", "English 5.1", False,
                               True, "6", "ec-3", _stats(450_000, 448_000)),
        ],
    }
    subs = [
        hls.SubtitleRendition("s0/playlist.m3u8", "en", "English", False, True, _stats(400, 300)),
        hls.SubtitleRendition("s1/playlist.m3u8", "en", "English (Forced)", True, True,
                              _stats(200, 100)),
    ]
    iframes = [
        'BANDWIDTH=90000,AVERAGE-BANDWIDTH=54000,CODECS="hvc1.2.4.L150.90",'
        'RESOLUTION=3840x1606,CLOSED-CAPTIONS=NONE,URI="v0/iframes.m3u8"',
        'BANDWIDTH=56000,AVERAGE-BANDWIDTH=33000,CODECS="avc1.64001f",'
        'RESOLUTION=1280x536,CLOSED-CAPTIONS=NONE,URI="v1/iframes.m3u8"',
    ]
    return videos, audio, subs, iframes


def test_golden_master() -> None:
    videos, audio, subs, iframes = _golden_inputs()
    text = hls.build_master(videos, audio, subs, iframes, header_comment="## golden")
    assert text == (GOLDEN / "master.m3u8").read_text()


def test_variant_bandwidth_is_video_plus_largest_audio_plus_subs() -> None:
    videos, audio, subs, iframes = _golden_inputs()
    text = hls.build_master(videos, audio, subs, iframes)
    first = next(line for line in text.splitlines() if line.startswith("#EXT-X-STREAM-INF"))
    attrs = hls.parse_attributes(first.split(":", 1)[1])
    assert int(attrs["BANDWIDTH"]) == 9_000_000 + 200_000 + 400
    assert int(attrs["AVERAGE-BANDWIDTH"]) == 7_000_000 + 195_000 + 300


def test_master_without_subtitles_has_no_subtitle_group() -> None:
    videos, audio, _subs, iframes = _golden_inputs()
    text = hls.build_master(videos, audio, None, iframes)
    assert "TYPE=SUBTITLES" not in text and "SUBTITLES=" not in text
    # Stereo variants first, top rung first: what players start on.
    uris = [line for line in text.splitlines() if line.endswith("playlist.m3u8")
            and not line.startswith("#")]
    assert uris == ["v0/playlist.m3u8", "v1/playlist.m3u8"] * 2


def test_master_without_audio() -> None:
    videos, _audio, _subs, _iframes = _golden_inputs()
    text = hls.build_master(videos, {}, None, [])
    assert "AUDIO=" not in text
    assert 'CODECS="hvc1.2.4.L150.90"' in text
