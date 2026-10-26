"""The rules this packager runs with, as it reports them to the catalog.

The catalog holds every movie and episode against the rules its workers
run with now, to find a package made before a rule came: a source's
surround left out before the 5.1 companion, its image subtitles before
they were carried (katalog-manager's README, "Library compliance"). It
knows the rules only as a worker reports them, and assumes none:
`PUT /api/analyze/rules/packager` with

    {"version": "<image commit or release>", "rules": {
      "surroundAudio": "eac3", "surroundLayout": "5.1",
      "surroundBitrate": "448k", "stereoAudio": "aac", "stereoBitrate": "192k",
      "imageSubtitles": ["pgs", "vobsub"]}}

Every value is the one this process runs with (README "Output", "Audio",
"Configuration"): the 5.1 companion's codec (`off`: none), the layout it
is made in (SURROUND_LAYOUT, which the encode asks ffmpeg for), the bit
rate of an encoded companion, the codec and the bit rate of the stereo
rendition (STEREO_CODEC, STEREO_BITRATE, which its encode is made with), and the forms of image
subtitles a package carries as they are — what the code really extracts:
PGS (a `.sup` stream copy) always, VobSub (an `.idx` and `.sub` pair)
only where mkvextract is installed, and from a Matroska file alone. DVB is
not carried (ffmpeg cannot write it), nor is XSUB.

The report goes at startup and every ten minutes, on a thread and a client
of its own. A catalog that does not take it (404 or 405: older than the
check) is said once, in `packager.rules.unsupported`, and asked again on
the next round; any other failure is logged and never stops the worker.
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import threading
from typing import Any, Protocol

import httpx
import structlog

from .packager import STEREO_BITRATE, STEREO_CODEC, SURROUND_LAYOUT, PackageOptions

log = structlog.get_logger(__name__)

WORKER = "packager"

# How often the report goes, seconds.
REPORT_INTERVAL_SECONDS = 600.0

# What an older catalog answers a route it does not have.
UNSUPPORTED = frozenset({404, 405})


def image_version() -> str | None:
    """This image's version: its commit or release tag, as the image build
    bakes it in (IMAGE_VERSION), else the installed package's."""
    version = (os.environ.get("IMAGE_VERSION") or "").strip()
    if version:
        return version
    try:
        return importlib.metadata.version("packager")
    except importlib.metadata.PackageNotFoundError:
        return None


def image_subtitle_forms() -> list[str]:
    """The forms of image subtitles a package carries as they are, as the
    subtitle extraction writes them: PGS always, VobSub where mkvextract
    (with mkvmerge, which names its tracks) is installed."""
    forms = ["pgs"]
    if shutil.which("mkvmerge") and shutil.which("mkvextract"):
        forms.append("vobsub")
    return forms


def rules_payload(options: PackageOptions, version: str | None,
                  image_subtitles: list[str] | None = None) -> dict[str, Any]:
    """The report of the rules this packager runs with (module doc)."""
    surround = (options.surround_codec or "off").strip().lower()
    forms = image_subtitle_forms() if image_subtitles is None else image_subtitles
    return {
        "version": version,
        "rules": {
            "surroundAudio": surround,
            "surroundLayout": SURROUND_LAYOUT,
            "surroundBitrate": options.surround_bitrate,
            "stereoAudio": STEREO_CODEC,
            "stereoBitrate": STEREO_BITRATE,
            "imageSubtitles": forms,
        },
    }


class RulesClient(Protocol):
    def put_rules(self, worker: str, payload: dict[str, Any]) -> httpx.Response: ...


class RulesReporter:
    """Reports the rules to the catalog, at once and then every interval
    until stop is set. Nothing it meets stops the worker: a catalog that
    does not take the report is said once, any other failure logged."""

    def __init__(
        self,
        client: RulesClient,
        payload: dict[str, Any],
        interval: float = REPORT_INTERVAL_SECONDS,
    ) -> None:
        self._client = client
        self._payload = payload
        self._interval = interval
        self._unsupported_said = False
        self._reported = False

    def report_once(self) -> bool:
        """One report; True when the catalog took it."""
        try:
            resp = self._client.put_rules(WORKER, self._payload)
        except Exception as e:
            log.warning("packager.rules.report_failed", error=str(e)[:200])
            return False
        if resp.status_code in UNSUPPORTED:
            if not self._unsupported_said:
                log.info("packager.rules.unsupported", status=resp.status_code,
                         note="the catalog takes no report of rules; asked again every round")
                self._unsupported_said = True
            return False
        if resp.status_code >= 400:
            log.warning("packager.rules.refused", status=resp.status_code, body=resp.text[:300])
            return False
        if not self._reported:
            log.info("packager.rules.reported", version=self._payload.get("version"))
            self._reported = True
        return True

    def run(self, stop: threading.Event) -> None:
        """Report now and every interval until stop is set."""
        while not stop.is_set():
            try:
                self.report_once()
            except Exception:  # never let the thread die of a report
                log.exception("packager.rules.report_failed")
            if stop.wait(self._interval):
                return
