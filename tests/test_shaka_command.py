"""Every shaka-packager run keeps the edit lists' timing (no binaries: the
runs are recorded): --mp4_reset_initial_composition_offset_to_zero=false, on
the media run and on each subtitle rendition's run. At its default shaka moved
a video that starts later than its decoding (a Matroska source with B-frames)
that much earlier (test_av_sync_real)."""

from __future__ import annotations

from pathlib import Path

import pytest

from packager import packager as pk

FLAG = "--mp4_reset_initial_composition_offset_to_zero=false"


def test_the_media_run_keeps_the_edit_lists_timing() -> None:
    assert FLAG in pk._shaka_command(["in=v0.mp4,stream=video"], 6, "hls/.shaka-master.m3u8")


def test_a_subtitle_renditions_run_keeps_it_too(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    runs: list[list[str]] = []
    monkeypatch.setattr(pk, "_run_shaka", lambda cmd, cwd, label: runs.append(cmd))
    pk._package_text_tracks([{"id": "sub0", "format": "webvtt", "visible": True,
                              "path": "subs/0.vtt", "language": "eng", "title": ""}],
                            tmp_path, 6)
    assert len(runs) == 1 and FLAG in runs[0]
