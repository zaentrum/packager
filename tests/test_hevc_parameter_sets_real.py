"""Real ffmpeg + shaka-packager runs of an HEVC source whose parameter
sets are in its stream only (skipped unless both are on PATH, ffmpeg is
7 or newer and has libx265).

The source is made as some web releases come: libx265 writes the VPS,
SPS and PPS into the stream at every keyframe (repeat-headers), and the
Matroska CodecPrivate is then rewritten in place as the stream's decoder
configuration record without them (numOfArrays 0). FFmpeg's MP4 muxer
writes an empty hvcC for such a stream copied as hvc1, which
shaka-packager can't parse; the packager copies it through Annex B, and
the package is whole and decodes. A source whose CodecPrivate holds its
parameter sets is remuxed exactly as before."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from packager import packager as pk
from packager.hls import parse_attributes


def _ffmpeg_major() -> int:
    first = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.split("\n", 1)[0]
    m = re.search(r"version n?(\d+)\.", first)
    return int(m.group(1)) if m else 99


def _has_x265() -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL).stdout
    return "libx265" in out


pytestmark = pytest.mark.skipif(
    any(shutil.which(b) is None for b in ("ffmpeg", "ffprobe", "packager"))
    or _ffmpeg_major() < 7 or not _has_x265(),
    reason="needs ffmpeg/ffprobe >= 7 with libx265 and shaka-packager on PATH",
)

ITEM = "c0ffee00-0000-4000-8000-0000000000c5"
FRAMES = 96  # 4 s at 24 fps


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _clip(path: Path, *x265: str) -> Path:
    """A 4 s HEVC clip with a stereo AAC track, in Matroska."""
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-c:v", "libx265", "-preset", "ultrafast",
        "-x265-params", ":".join(["log-level=error", "keyint=48", *x265]),
        "-c:a", "aac", "-metadata:s:a:0", "language=eng", str(path))
    return path


def _vint(n: int) -> bytes:
    """The shortest EBML size that holds n (all ones is reserved)."""
    width = next(w for w in range(1, 9) if n < (1 << 7 * w) - 1)
    return ((1 << 7 * width) | n).to_bytes(width, "big")


def _drop_parameter_sets(mkv: Path) -> None:
    """Rewrite the clip's HEVC CodecPrivate in place as its decoder
    configuration record with no arrays (numOfArrays 0): its parameter
    sets are then in the stream only. What the record no longer holds
    becomes an EBML Void of the same size, so nothing else moves."""
    data = bytearray(mkv.read_bytes())
    at = data.find(b"\x63\xa2")                       # CodecPrivate
    width = 9 - data[at + 2].bit_length()
    size = int.from_bytes(data[at + 2:at + 2 + width], "big") & ((1 << 7 * width) - 1)
    record = bytes(data[at + 2 + width:at + 2 + width + size])
    assert record[0] == 1 and size > 23, "not an hvcC"
    total = 2 + width + size
    head = b"\x63\xa2" + _vint(23) + record[:22] + b"\x00"
    rest = total - len(head)
    w = next(w for w in range(1, 9) if len(_vint(rest - 1 - w)) == w)
    void = b"\xec" + _vint(rest - 1 - w) + bytes(rest - 1 - w)
    data[at:at + total] = head + void
    mkv.write_bytes(bytes(data))


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("hevc")
    whole = _clip(d / "whole.mkv")
    in_band = _clip(d / "in-band.mkv", "repeat-headers=1")
    _drop_parameter_sets(in_band)
    return whole, in_band


def _in_band(path: Path) -> dict[int, int]:
    """How many VPS, SPS and PPS the stream itself carries."""
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
                          "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
                         capture_output=True, check=True).stdout
    counts = {32: 0, 33: 0, 34: 0}
    for m in re.finditer(b"\x00\x00\x01", out):
        t = (out[m.end()] >> 1) & 0x3F
        if t in counts:
            counts[t] += 1
    return counts


def _package(tmp_path: Path, monkeypatch, src: Path) -> tuple[dict, Path, list[list[str]]]:
    monkeypatch.setattr(pk, "PACKAGES_ROOT", tmp_path / "packages")
    real = pk._run_ffmpeg_capturing
    calls: list[list[str]] = []

    def ffmpeg(label: str, args: list[str]) -> None:
        calls.append(args)
        real(label, args)

    monkeypatch.setattr(pk, "_run_ffmpeg_capturing", ffmpeg)
    manifest = pk.package_item(ITEM, str(src), "movie", title="clip",
                               options=pk.PackageOptions(surround_codec="off"))
    return manifest, tmp_path / "packages" / "movies" / ITEM[:2] / ITEM, calls


def _decoded_frames(rendition: Path) -> int:
    joined = rendition / ".joined.mp4"
    joined.write_bytes((rendition / "init.mp4").read_bytes() + b"".join(
        seg.read_bytes() for seg in sorted(rendition.glob("seg-*.m4s"))))
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                              "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
                              str(joined)], capture_output=True, text=True, check=True)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(joined),
                        "-f", "null", "-"], capture_output=True, check=True)
    finally:
        joined.unlink()
    return int(out.stdout.strip())


def test_the_sample_has_its_parameter_sets_in_band_only(clips) -> None:
    _whole, in_band = clips
    record = pk._codec_private(in_band, 0)
    assert record is not None and len(record) == 23 and record[22] == 0
    assert all(n > 0 for n in _in_band(in_band).values())
    assert pk._parameter_sets_in_band_only(in_band, 0) is True


def test_an_hevc_source_with_in_band_parameter_sets_only_packages(
    tmp_path: Path, monkeypatch, clips,
) -> None:
    _whole, in_band = clips
    manifest, root, calls = _package(tmp_path, monkeypatch, in_band)
    transmux = next(args for args in calls if args[-1].endswith("transmux.mp4"))
    copy = transmux[transmux.index("-c:v"):transmux.index("-filter_complex")]
    assert copy == ["-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-tag:v", "hvc1"]
    [video] = manifest["renditions"]["video"]
    assert video["codec"].startswith("hvc1.") and (video["width"], video["height"]) == (320, 240)
    master = (root / "hls" / "master.m3u8").read_text().splitlines()
    variants = [parse_attributes(line.split(":", 1)[1]) for line in master
                if line.startswith("#EXT-X-STREAM-INF:")]
    assert variants and all(a["CODECS"].startswith("hvc1.") for a in variants)
    # The init segment's decoder configuration is whole, and the picture
    # decodes, every frame of it.
    record = pk._codec_private(root / "hls" / "v0" / "init.mp4", 0)
    assert record is not None and not pk._hvcc_lacks_parameter_sets(record)
    assert _decoded_frames(root / "hls" / "v0") == FRAMES
    assert (root / ".complete").exists()


def test_an_hevc_source_with_its_parameter_sets_is_remuxed_as_before(
    tmp_path: Path, monkeypatch, clips,
) -> None:
    whole, _in_band = clips
    assert pk._parameter_sets_in_band_only(whole, 0) is False
    manifest, root, calls = _package(tmp_path, monkeypatch, whole)
    transmux = next(args for args in calls if args[-1].endswith("transmux.mp4"))
    copy = transmux[transmux.index("-c:v"):transmux.index("-filter_complex")]
    assert copy == ["-c:v", "copy", "-tag:v", "hvc1"]
    assert manifest["renditions"]["video"][0]["codec"].startswith("hvc1.")
    assert _decoded_frames(root / "hls" / "v0") == FRAMES
