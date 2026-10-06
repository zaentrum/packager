"""libv2_records.py is the schemas repository's record logic
(tools/libv2_records.py), vendored byte for byte: the packager and the
migration must write the same records for the same facts, because the
deletion gate is computed from them. A copy that drifts fails here. To
take a new one, copy the file from the schemas repository at the commit
that has it, with the golden records in tests/fixtures/libv2_records/
from the same commit, and set the values below.

The golden records are the record logic's reference build of what a
packager writes for an original (golden_tree in the schemas repository's
tools tests): the same probe, the same files beside the original and the
same moments and ids must give the same bytes through the packager's own
writer — its source folder (source.json, ffprobe.json, the sidecar
copies, the checksums) and its version.json."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from packager import library, records
from packager import libv2_records as rec
from packager.katalog import ClaimedItem

# zaentrum/schemas 92effa9e6103735cf175ddf1c239758c9aaeae06, tools/libv2_records.py
SHA256 = "3954f76ae4b8f709e1edc0524a13dde477b5a5a32aa4775a7d8922a33b6326e8"
LIBV2_RECORDS = 1

GOLDEN = Path(__file__).parent / "fixtures" / "libv2_records"
ITEM = "f0f0f0f0-1111-4222-8333-444444444444"
AT = "2026-10-06T10:00:00Z"
ORIGINAL = "Example Film (2024) - 2160p.mkv"
SIDECAR = "Example Film (2024) - 2160p.de.srt"
COMPANION = "Example Film (2024) - 2160p.nfo"


def test_the_record_logic_is_the_schemas_repositorys_byte_for_byte() -> None:
    assert hashlib.sha256(Path(rec.__file__).read_bytes()).hexdigest() == SHA256
    assert rec.LIBV2_RECORDS == LIBV2_RECORDS


def test_the_packager_writes_the_reference_records(tmp_path: Path) -> None:
    arrivals = tmp_path / "arrivals" / "Example Film (2024)"
    arrivals.mkdir(parents=True)
    original = arrivals / ORIGINAL
    original.write_bytes(bytes((i * 7 + 3) % 251 for i in range(200000)))
    (arrivals / SIDECAR).write_text("1\n00:00:01,000 --> 00:00:02,000\nHallo\n")
    (arrivals / COMPANION).write_text("<movie><title>Example Film</title></movie>\n")
    moment = datetime(2026, 10, 6, 9, 0, tzinfo=UTC).timestamp()
    os.utime(original, (moment, moment))
    sid, vid = rec.did(ITEM, "source", ORIGINAL), rec.did(ITEM, "version", ORIGINAL)
    item_dir = tmp_path / "library" / "movies" / ITEM[:2] / ITEM
    work = tmp_path / "library" / ".work"
    item = ClaimedItem.from_json({
        "id": ITEM, "type": "movie", "title": "Example Film", "path": str(original),
        # The worker record names a sidecar's language in ISO 639-2.
        "subtitleFiles": [{"id": "a1", "path": str(arrivals / SIDECAR), "language": "ger",
                           "label": "Deutsch"}],
        "library": {
            "contract": 1, "root": str(tmp_path / "library"), "itemDir": str(item_dir),
            "blocked": None, "inboxDir": str(work / "inbox" / ITEM), "current": None,
            "source": {"sourceId": sid, "recorded": False,
                       "recordDir": str(item_dir / "sources" / sid),
                       "libraryPath": f"Example Film (2024)/{ORIGINAL}",
                       "sizeBytes": original.stat().st_size, "qh1": rec.qh1(str(original))},
            "build": {"versionId": vid, "stagingDir": str(work / "staging" / vid),
                      "versionDir": str(item_dir / "versions" / vid),
                      "createdBy": "katalog-manager", "chapters": None, "chaptersFrom": None,
                      # As the catalog hands them over: a kind the record has no word
                      # for is "other", labelled with the catalog's own.
                      "segments": [{"kind": "credits", "startMs": 50000, "endMs": 60000,
                                    "detector": "chapter", "confidence": 0.9, "label": None},
                                   {"kind": "other", "startMs": 55000, "endMs": 60000,
                                    "detector": "blackframe", "confidence": None,
                                    "label": "outro"}]},
        },
    })
    assert item.library is not None, item.library_error
    probe = records.OriginalProbe(json.loads((GOLDEN / "probe.json").read_text()),
                                  "ffprobe version 7.1")
    folder = tmp_path / "staged-source"
    source, copies = library._stage_source(folder, item, item.library, probe,
                                           rec.qh1(str(original)), AT)
    expected = GOLDEN / "expected"
    for name, golden in (("source.json", "source.json"), ("ffprobe.json", "ffprobe.json"),
                         ("checksums.sha256", "source.checksums.sha256")):
        assert (folder / name).read_bytes() == (expected / golden).read_bytes(), name
    assert [c.name for c in copies] == [SIDECAR, COMPANION]
    version = records.version_record(item.library, source, probe, AT)
    assert rec.json_bytes(version) == (expected / "version.json").read_bytes()
