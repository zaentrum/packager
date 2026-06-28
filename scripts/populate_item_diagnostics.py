"""Per-item source-side snapshot populator.

For each item on stdin (one per line as `<itemId>\\t<sourcePath>`),
runs ffprobe over the primary source file, lists the containing
folder, and emits SQL to UPSERT the row into
`com_nalet_katalog_itemdiagnostics`. The driver pipes the SQL into
psql so we don't need a new manager-api endpoint for the bulk pass.

Folder listing heuristic: each entry gets a `role` tag derived from
the extension + filename. Recognises the source MKV/MP4 itself
(`source`), Bluray PGS / VobSub sidecars (`external_sub_pgs`/`vobsub`),
SubRip sidecars (`external_sub_srt`), trailers, samples, NFOs,
artwork. Anything that doesn't match gets `other`.

Idempotent: the INSERT is wrapped in a DELETE so re-running on the
same itemId overwrites the prior snapshot."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROLE_BY_EXT: dict[str, str] = {
    ".mkv": "video",
    ".mp4": "video",
    ".m4v": "video",
    ".avi": "video",
    ".mov": "video",
    ".webm": "video",
    ".ts": "video",
    ".sup": "external_sub_pgs",
    ".idx": "external_sub_vobsub",
    ".sub": "external_sub_vobsub",
    ".srt": "external_sub_srt",
    ".vtt": "external_sub_vtt",
    ".ass": "external_sub_ass",
    ".ssa": "external_sub_ssa",
    ".nfo": "nfo",
    ".jpg": "artwork",
    ".jpeg": "artwork",
    ".png": "artwork",
    ".webp": "artwork",
    ".txt": "text",
    ".pdf": "doc",
}

TRAILER_RE = re.compile(r"-trailer\.|trailer\.[a-z0-9]+$", re.IGNORECASE)
SAMPLE_RE = re.compile(r"-sample\.|sample\.[a-z0-9]+$", re.IGNORECASE)


def classify(name: str, source_basename: str) -> str:
    """Pick a role for a single filename. Special-cases the actual
    source video (so it stands out from secondary videos in the same
    folder) and the trailer/sample naming convention before falling
    back to the extension map."""
    if name == source_basename:
        return "source"
    if TRAILER_RE.search(name):
        return "trailer"
    if SAMPLE_RE.search(name):
        return "sample"
    ext = os.path.splitext(name)[1].lower()
    return ROLE_BY_EXT.get(ext, "other")


def ffprobe_json(path: Path) -> str:
    out = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        # Don't die — leave a stub so we can still see the folder
        # listing and the failure reason on the Fiori page.
        return json.dumps({"error": (out.stderr or "")[:1000].strip()})
    return out.stdout


def folder_listing(folder: Path, source_basename: str) -> str:
    entries: list[dict] = []
    if not folder.is_dir():
        return json.dumps([])
    try:
        names = sorted(os.listdir(folder))
    except PermissionError:
        return json.dumps([{"error": "permission_denied"}])
    for name in names:
        full = folder / name
        try:
            st = full.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
                "role": classify(name, source_basename),
            }
        )
    return json.dumps(entries)


def sql_quote(s: str | None) -> str:
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def sql_quote_bytes(s: str) -> str:
    """For LargeString columns that hold JSON. SQL escapes only
    single quotes; the JSON serialiser already handles the rest."""
    return "'" + s.replace("'", "''") + "'"


def process_one(item_id: str, source_path: str) -> str | None:
    src = Path(source_path)
    folder = src.parent
    source_basename = src.name
    try:
        st = src.stat()
        size = st.st_size
        mtime_iso = datetime.fromtimestamp(st.st_mtime, UTC).isoformat(
            sep=" ", timespec="seconds"
        ).replace("+00:00", "")
    except OSError:
        size = None
        mtime_iso = None
    ffprobe_data = ffprobe_json(src)
    listing = folder_listing(folder, source_basename)
    row_id = str(uuid.uuid4())
    generated_at = datetime.now(UTC).isoformat(sep=" ", timespec="seconds").replace("+00:00", "")
    parts = [
        sql_quote(row_id),
        sql_quote(item_id),
        sql_quote(generated_at),
        sql_quote(source_path),
        "NULL" if size is None else str(size),
        sql_quote(mtime_iso),
        sql_quote_bytes(ffprobe_data),
        sql_quote_bytes(listing),
        "NULL",  # notes
    ]
    sql = (
        "DELETE FROM com_nalet_katalog_itemdiagnostics WHERE item_id="
        + sql_quote(item_id)
        + "; INSERT INTO com_nalet_katalog_itemdiagnostics "
        "(id, item_id, generatedat, sourcepath, sourcesize, sourcemtime, "
        "ffprobedata, folderlisting, notes) VALUES ("
        + ", ".join(parts) + ");"
    )
    return sql


def main() -> int:
    n_ok = 0
    n_err = 0
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line:
            continue
        try:
            item_id, source_path = line.split("\t")
        except ValueError:
            sys.stderr.write(f"bad line: {raw!r}\n")
            n_err += 1
            continue
        try:
            sql = process_one(item_id, source_path)
            if sql is None:
                n_err += 1
                continue
            print(sql, flush=True)
            n_ok += 1
        except Exception as e:
            sys.stderr.write(f"process_one({item_id}) raised: {e!r}\n")
            n_err += 1
    sys.stderr.write(f"DONE ok={n_ok} err={n_err}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
