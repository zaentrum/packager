"""Reading the transcoder's handoff (`_inbox/{itemId}/`).

Three shapes, newest first:

  * `renditions.json` (contract v1, written last by the transcoder): N
    video rungs. v0 carries every audio + subtitle track — from its
    `file` or, when `"file": null`, from the item's original source. All
    inbox files share one timeline (the source shifted by
    `timestampOffset`); `segmentSeconds` is the keyframe interval the
    rungs were encoded with.
  * `prepared.mkv` alone (an older transcoder): one rung.
  * nothing: package the original source (an HEVC source the
    transcoder had no reason to touch).

See the transcoder's README "Rendition contract" for the full schema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

CONTRACT_FILE = "renditions.json"
PREPARED_FILE = "prepared.mkv"
SUPPORTED_VERSIONS = {1}


class ContractError(RuntimeError):
    """renditions.json exists but can't be used (bad JSON, unknown
    version, v0 missing). The package step fails with this message."""


@dataclass(frozen=True)
class VideoInput:
    """One video rung to package."""
    id: str               # v0, v1, ... (the HLS rendition dir name)
    path: Path
    label: str = "source"
    encoder: str = "copy"
    # How the packager's remux treats this file's timestamps:
    #   "normalize" — ffmpeg default (shift to start at 0): the legacy path
    #   "keep"      — -copyts: already on the contract's shared timeline
    #   "offset"    — -copyts + -output_ts_offset: the ORIGINAL source,
    #                 moved onto the shared timeline
    timeline: str = "normalize"


@dataclass(frozen=True)
class PackageInputs:
    video: list[VideoInput]
    kind: str                        # "contract" | "prepared" | "original"
    segment_seconds: int | None = None
    timestamp_offset: float = 0.0
    contract: dict[str, Any] = field(default_factory=dict)

    @property
    def primary(self) -> VideoInput:
        """v0: the file that carries the audio and subtitle tracks."""
        return self.video[0]


def resolve_inputs(inbox: Path, original: str) -> PackageInputs:
    contract_path = inbox / CONTRACT_FILE
    if contract_path.exists():
        return _from_contract(contract_path, inbox, Path(original))
    prepared = inbox / PREPARED_FILE
    if prepared.exists():
        return PackageInputs(video=[VideoInput("v0", prepared, encoder="transcoder")],
                             kind="prepared")
    return PackageInputs(video=[VideoInput("v0", Path(original))], kind="original")


def _from_contract(path: Path, inbox: Path, original: Path) -> PackageInputs:
    try:
        contract = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ContractError(f"unreadable {CONTRACT_FILE}: {e}") from e
    version = contract.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise ContractError(f"{CONTRACT_FILE} version {version!r} not supported")
    rungs = contract.get("video") or []
    if not rungs:
        raise ContractError(f"{CONTRACT_FILE} lists no video renditions")

    offset = float(contract.get("timestampOffset") or 0.0)
    inputs: list[VideoInput] = []
    for i, rung in enumerate(rungs):
        name = rung.get("file")
        if name:
            file = inbox / Path(str(name)).name  # never escape the inbox
            timeline = "keep"
        else:
            file, timeline = original, "offset"
        if not file.exists():
            if i == 0:
                raise ContractError(f"top rendition missing: {file}")
            # A lower rung is a nice-to-have; package what is there.
            log.warning("packager.contract.rung_missing", rung=rung.get("id"), path=str(file))
            continue
        inputs.append(VideoInput(
            id=f"v{len(inputs)}",
            path=file,
            label=str(rung.get("label") or ("source" if i == 0 else f"v{i}")),
            encoder=str(rung.get("encoder") or "copy"),
            timeline=timeline,
        ))
    seg = contract.get("segmentSeconds")
    return PackageInputs(
        video=inputs,
        kind="contract",
        segment_seconds=int(seg) if seg else None,
        timestamp_offset=offset,
        contract=contract,
    )
