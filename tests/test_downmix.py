"""The filters that make a source track's stereo rendition
(packager._stereo_filter; no ffmpeg run): a mono or stereo track's are the
format they always were; a track of more than two channels in a layout the
downmix knows is downmixed by the usual matrix normalised, made up to 3 dB
under the plain matrix and limited under -3 dBFS; one in any other layout by
ffmpeg's own matrix, normalised. And those that make its encoded 5.1
companion (packager._surround_filter): a track of six channels or fewer is
made a 5.1 by the format it always was; one of more than six in a layout the
fold knows by the fold normalised, made up to 3 dB under the plain matrix and
limited under -3 dBFS; one in any other layout by ffmpeg's own matrix,
normalised."""

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


SURROUND_FORMAT = "aformat=sample_rates=48000:channel_layouts=5.1(side)|5.1"
SURROUND_NORMALISED = f"aresample=48000:rematrix_maxval=1,{SURROUND_FORMAT}"
SURROUND = ("FL", "FR", "FC", "LFE", "SL", "SR")


def _fold(chain: str) -> dict[str, dict[str, float]]:
    """The companion's pan gains: {output: {input: gain}}."""
    [spec] = re.findall(r"^pan=5\.1\(side\)\|([^,]+)", chain)
    out: dict[str, dict[str, float]] = {}
    for part in spec.split("|"):
        name, terms = part.split("=")
        out[name] = {i: float(g) for g, i in (t.split("*") for t in terms.split("+"))}
    return out


def _plain_fold(names: list[str]) -> dict[str, dict[str, float]]:
    """The plain matrix to a 5.1(side): its own channels as they are, a back
    pair into the surrounds as it is where there is no side pair, else at
    -3 dB; a back centre into both surrounds, a front of centre into its
    front and the centre, at -3 dB."""
    m: dict[str, dict[str, float]] = {out: {} for out in SURROUND}
    for name in names:
        if name in SURROUND:
            m[name][name] = 1
        elif name in ("BL", "BR"):
            m["S" + name[1]][name] = H if "S" + name[1] in names else 1
        elif name == "BC":
            m["SL"][name] = m["SR"][name] = H
        else:
            m["F" + name[1]][name] = m["FC"][name] = H
    return {out: row for out, row in m.items() if row}


@pytest.mark.parametrize(("channels", "layout"), [
    (6, "5.1"), (6, "5.1(side)"), (6, None), (6, "unknown"),
    (6, "6.0"), (6, "hexagonal"),   # six channels: what they always were
    (5, "5.0(side)"), (4, "quad"), (3, "3.0"), (3, "2.1"),
])
def test_a_track_of_six_channels_or_fewer_is_made_a_5_1_as_ever(channels: int,
                                                                 layout: str | None) -> None:
    assert pk._surround_filter(_s(channels, layout)) == SURROUND_FORMAT == pk._SURROUND_FORMAT


@pytest.mark.parametrize(("layout", "channels", "chain"), [
    ("7.1", 8, "pan=5.1(side)|FL=0.5858*FL|FR=0.5858*FR|FC=0.5858*FC|LFE=0.5858*LFE"
               "|SL=0.5858*SL+0.4142*BL|SR=0.5858*SR+0.4142*BR"
               f",aresample=48000,volume=1.63dB,{LIMITER},{SURROUND_FORMAT}"),
    ("6.1", 7, "pan=5.1(side)|FL=0.5858*FL|FR=0.5858*FR|FC=0.5858*FC|LFE=0.5858*LFE"
               "|SL=0.5858*SL+0.4142*BC|SR=0.5858*SR+0.4142*BC"
               f",aresample=48000,volume=1.63dB,{LIMITER},{SURROUND_FORMAT}"),
    # The back pair is the surrounds where there is no side pair.
    ("6.1(back)", 7, "pan=5.1(side)|FL=0.5858*FL|FR=0.5858*FR|FC=0.5858*FC|LFE=0.5858*LFE"
                     "|SL=0.5858*BL+0.4142*BC|SR=0.5858*BR+0.4142*BC"
                     f",aresample=48000,volume=1.63dB,{LIMITER},{SURROUND_FORMAT}"),
    # The fronts of centre into the fronts and the centre, which sums most.
    ("7.1(wide)", 8, "pan=5.1(side)|FL=0.4142*FL+0.2929*FLC|FR=0.4142*FR+0.2929*FRC"
                     "|FC=0.4142*FC+0.2929*FLC+0.2929*FRC|LFE=0.4142*LFE|SL=0.4142*BL|SR=0.4142*BR"
                     f",aresample=48000,volume=4.65dB,{LIMITER},{SURROUND_FORMAT}"),
    # No LFE: the companion's is silent.
    ("7.0", 7, "pan=5.1(side)|FL=0.5858*FL|FR=0.5858*FR|FC=0.5858*FC"
               "|SL=0.5858*SL+0.4142*BL|SR=0.5858*SR+0.4142*BR"
               f",aresample=48000,volume=1.63dB,{LIMITER},{SURROUND_FORMAT}"),
])
def test_the_fold_of_a_layout(layout: str, channels: int, chain: str) -> None:
    assert pk._surround_filter(_s(channels, layout)) == chain


@pytest.mark.parametrize("layout", sorted(pk._SURROUND_FOLD_LAYOUTS))
def test_every_fold_is_the_plain_matrix_3_db_down_and_limited(layout: str) -> None:
    names = pk._SURROUND_FOLD_LAYOUTS[layout].split("+")
    assert len(names) > 6
    chain = pk._surround_filter(_s(len(names), layout))
    fold = _fold(chain)
    plain = _plain_fold(names)
    [makeup] = [float(x) for x in re.findall(r",volume=([\d.]+)dB,", chain)]
    total = max(sum(row.values()) for row in plain.values())
    assert total > math.sqrt(2)  # every one folds a channel into one it has
    # Normalised: no channel of the matrix alone can exceed full scale ...
    assert max(sum(row.values()) for row in fold.values()) == pytest.approx(1, abs=1e-3)
    # ... and with its make-up, it is the plain matrix 3 dB down: every
    # channel of the source in the companion, the LFE in its own alone.
    gain = 10 ** (makeup / 20)
    assert {out: {n: g * gain for n, g in row.items()} for out, row in fold.items()} == {
        out: pytest.approx({n: g * H for n, g in row.items()}, abs=2e-3)
        for out, row in plain.items()}
    assert {n for row in fold.values() for n in row} == set(names)
    assert fold.get("LFE", {"LFE": 1}).keys() == {"LFE"}
    # The make-up after the resample to 48 kHz and before the limiter, the
    # format last.
    assert chain.endswith(f",aresample=48000,volume={makeup:.2f}dB,{LIMITER},{SURROUND_FORMAT}")


@pytest.mark.parametrize(("channels", "layout"), [
    (8, None),            # ffprobe names no layout: PCM in Matroska
    (8, "unknown"),
    (8, "5.1.2"),         # height channels
    (10, "5.1.4"),
    (12, "7.1.4"),
    (8, "cube"),
    (8, "6.1"),           # a name that isn't the track's channels
    (7, "7.1"),
    (24, "22.2"),
])
def test_any_other_layout_of_more_than_six_channels_gets_ffmpegs_own_matrix_normalised(
        channels: int, layout: str | None) -> None:
    assert pk._surround_filter(_s(channels, layout)) == SURROUND_NORMALISED
