"""The filters that make a source track's stereo rendition
(packager._stereo_filter; no ffmpeg run): a mono or stereo track's are the
format they always were; a track of more than two channels in a layout the
downmix knows is downmixed by the usual matrix normalised, made up to 3 dB
under the plain matrix and limited under -3 dBFS; one in any other layout by
ffmpeg's own matrix, normalised."""

from __future__ import annotations

import math
import re

import pytest

from packager import packager as pk

FORMAT = "aformat=sample_rates=48000:channel_layouts=stereo"
LIMITER = "alimiter=limit=-3dB:attack=10:release=100:level=disabled:latency=enabled"
NORMALISED = f"aresample=48000:rematrix_maxval=1,{FORMAT}"

# The plain matrix, (left, right) per channel: the fronts as they are, the
# centre and the surrounds at -3 dB, a back centre at -6 dB, no LFE.
H = math.sqrt(0.5)
PLAIN = {"FL": (1, 0), "FR": (0, 1), "FC": (H, H), "LFE": (0, 0), "BL": (H, 0), "BR": (0, H),
         "SL": (H, 0), "SR": (0, H), "BC": (0.5, 0.5)}


def _s(channels: int, layout: str | None) -> dict:
    return {"codec_name": "eac3", "channels": channels,
            **({"channel_layout": layout} if layout is not None else {})}


def _pan(chain: str) -> dict[str, dict[str, float]]:
    """The pan filter's gains: {output: {input: gain}}."""
    [spec] = re.findall(r"^pan=stereo\|([^,]+)", chain)
    out: dict[str, dict[str, float]] = {}
    for part in spec.split("|"):
        name, terms = part.split("=")
        out[name] = {i: float(g) for g, i in (t.split("*") for t in terms.split("+"))}
    return out


@pytest.mark.parametrize(("channels", "layout"), [
    (1, "mono"), (1, None), (2, "stereo"), (2, None), (2, "downmix"),
])
def test_a_mono_or_stereo_track_is_converted_as_ever(channels: int, layout: str | None) -> None:
    assert pk._stereo_filter(_s(channels, layout)) == FORMAT == pk._STEREO_FORMAT


@pytest.mark.parametrize(("layout", "channels", "chain"), [
    ("5.1(side)", 6, "pan=stereo|FL=0.4142*FL+0.2929*FC+0.2929*SL|FR=0.4142*FR+0.2929*FC+0.2929*SR"
                     f",aresample=48000,volume=4.65dB,{LIMITER},{FORMAT}"),
    ("5.1", 6, "pan=stereo|FL=0.4142*FL+0.2929*FC+0.2929*BL|FR=0.4142*FR+0.2929*FC+0.2929*BR"
               f",aresample=48000,volume=4.65dB,{LIMITER},{FORMAT}"),
    ("7.1", 8, "pan=stereo|FL=0.3204*FL+0.2265*FC+0.2265*BL+0.2265*SL"
               "|FR=0.3204*FR+0.2265*FC+0.2265*BR+0.2265*SR"
               f",aresample=48000,volume=6.88dB,{LIMITER},{FORMAT}"),
    ("6.1", 7, "pan=stereo|FL=0.3431*FL+0.2426*FC+0.1716*BC+0.2426*SL"
               "|FR=0.3431*FR+0.2426*FC+0.1716*BC+0.2426*SR"
               f",aresample=48000,volume=6.28dB,{LIMITER},{FORMAT}"),
    ("quad", 4, "pan=stereo|FL=0.5858*FL+0.4142*BL|FR=0.5858*FR+0.4142*BR"
                f",aresample=48000,volume=1.63dB,{LIMITER},{FORMAT}"),
    ("3.0", 3, "pan=stereo|FL=0.5858*FL+0.4142*FC|FR=0.5858*FR+0.4142*FC"
               f",aresample=48000,volume=1.63dB,{LIMITER},{FORMAT}"),
    # A 2.1 sums nothing: its LFE left out, as before, and nothing to make up.
    ("2.1", 3, f"pan=stereo|FL=1*FL|FR=1*FR,{FORMAT}"),
])
def test_the_downmix_of_a_layout(layout: str, channels: int, chain: str) -> None:
    assert pk._stereo_filter(_s(channels, layout)) == chain


@pytest.mark.parametrize("layout", sorted(pk._DOWNMIX_LAYOUTS))
def test_every_layout_is_the_plain_matrix_3_db_down_and_limited(layout: str) -> None:
    names = pk._DOWNMIX_LAYOUTS[layout].split("+")
    chain = pk._stereo_filter(_s(len(names), layout))
    pan = _pan(chain)
    makeup = [float(x) for x in re.findall(r",volume=([\d.]+)dB,", chain)]
    total = max(sum(PLAIN[n][side] for n in names) for side in (0, 1))
    for side, out in enumerate(("FL", "FR")):
        # Normalised: the matrix alone cannot exceed full scale ...
        assert sum(pan[out].values()) == pytest.approx(1, abs=1e-3)
        # ... and with its make-up, it is the plain matrix 3 dB down, or
        # left normalised where that is louder (a 2.1). No LFE.
        gain = 10 ** (makeup[0] / 20) if makeup else 1
        level = H if total > math.sqrt(2) else 1 / total
        assert {n: g * gain for n, g in pan[out].items()} == pytest.approx(
            {n: PLAIN[n][side] * level for n in names if PLAIN[n][side]}, abs=2e-3)
        assert "LFE" not in pan[out]
    # A make-up comes after the resample to 48 kHz and before the limiter,
    # the format last; without one there's nothing to limit.
    assert chain.endswith(f",{FORMAT}")
    if makeup:
        assert f",aresample=48000,volume={makeup[0]:.2f}dB,{LIMITER},{FORMAT}" in chain
    else:
        assert "alimiter" not in chain and "volume" not in chain


@pytest.mark.parametrize(("channels", "layout"), [
    (6, None),          # ffprobe names no layout: a PCM track in Matroska, a WAV without a mask
    (6, "unknown"),
    (8, "5.1.2"),       # height channels
    (8, "7.1(wide)"),   # front left and right of centre
    (8, "5.1(side)"),   # a name that isn't the track's channels
])
def test_any_other_layout_gets_ffmpegs_own_matrix_normalised(channels: int,
                                                             layout: str | None) -> None:
    assert pk._stereo_filter(_s(channels, layout)) == NORMALISED
