"""Backfill missing subtitle sidecars on items packaged before the
bitmap-sidecar code went live (commit f92d4b0) AND items whose text
subs were dropped by older packager bugs (e.g. mov_text in .mp4
sources never extracted).

Runs inside the packager pod (mounts /var/lib/katalog/{media,packages}
RO/RW, has ffprobe + ffmpeg + OIDC env). The cheap version of a
re-packaging pass: skips shaka-packager + audio entirely and only
extracts whichever subtitle streams aren't already on disk. Image
codecs (PGS/VobSub/DVB) go through `ffmpeg -c:s copy` to their
native container; text codecs (SubRip/mov_text/SSA/ASS/WebVTT)
get transcoded to WebVTT via `ffmpeg -c:s webvtt`.

Driven by a per-line stdin protocol so a separate psql query feeds
the work list — keeps the script DB-agnostic. Each line:

    {itemId}\\t{sourcePath}\\t{packageRoot}

Per-item flow:
  1. Read existing manifest.json
  2. Parse the existing subs/N.<ext> paths to learn which source
     subtitle indices are already on disk
  3. ffprobe the source
  4. For each source subtitle stream whose index ISN'T already in
     the manifest, run the right ffmpeg invocation per its codec
     family (image: stream-copy; text: transcode to WebVTT)
  5. Append the new entries to manifest.subtitles[], atomic-write
  6. POST the updated manifest to katalog-api packaging-complete so
     SubtitleAssets rows in postgres mirror what's on disk."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

KATALOG = os.environ["KATALOG_API_URL"].rstrip("/")
TOKEN_URL = os.environ["OIDC_TOKEN_URL"]
CID = os.environ["OIDC_CLIENT_ID"]
CSEC = os.environ["OIDC_CLIENT_SECRET"]

# codec_name -> (manifest format tag, sidecar extension, ffmpeg -f or None)
IMAGE_CODECS: dict[str, tuple[str, str, str | None]] = {
    "hdmv_pgs_subtitle": ("pgs", "sup", "sup"),
    "dvd_subtitle":      ("vobsub", "idx", "vobsub"),
    "dvb_subtitle":      ("dvb", "dvb", None),
}

# Text-based subtitle codecs that ffmpeg can transcode to WebVTT
# via `-c:s webvtt`. Same path the original packager uses for
# SubRip/SSA/etc; adding `mov_text` here lets us recover WEB-DL
# .mp4 items whose subs were dropped by earlier packager bugs.
TEXT_CODECS: set[str] = {
    "subrip", "srt",
    "mov_text",
    "ass", "ssa",
    "webvtt",
    "text",
}

# Parses an existing manifest entry's path to learn which source
# subtitle stream index it represents. Packager naming convention
# is subs/<sourceIndex>.<ext>, so 'subs/3.sup' => 3.
SUBS_PATH_INDEX_RE = re.compile(r"^subs/(\d+)\.")


def get_token() -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": CID,
            "client_secret": CSEC,
            "scope": "openid",
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def post_packaging_complete(item_id: str, manifest: dict, token: str) -> int:
    body = json.dumps(manifest).encode()
    req = urllib.request.Request(
        f"{KATALOG}/api/items/{item_id}/packaging-complete",
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def ffprobe_subs(src: Path) -> list[dict]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "s",
            str(src),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(out.stdout).get("streams", [])


def extract_image(src: Path, sub_index: int, codec: str, dest: Path) -> bool:
    """Stream-copy an image-codec subtitle (PGS / VobSub / DVB) into
    its native container. Returns True on success."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(src),
        # `-map 0:s:N` indexes among SUBTITLE streams only, matching
        # the index space we recorded in the manifest. ffprobe's
        # selected-streams JSON above is sorted in that order.
        "-map", f"0:s:{sub_index}",
        "-c:s", "copy",
    ]
    _, _ext, ffmt = IMAGE_CODECS[codec]
    if ffmt is not None:
        cmd += ["-f", ffmt]
    cmd += [str(dest)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(
            f"extract_image failed src={src} idx={sub_index} codec={codec}: "
            f"{res.stderr[:300]}\n"
        )
        return False
    return True


def extract_text(src: Path, sub_index: int, dest: Path) -> bool:
    """Transcode a text-codec subtitle (SubRip / mov_text / SSA /
    ASS / WebVTT / plain text) to WebVTT. The original packager's
    text path uses the same `-c:s webvtt` muxer."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(src),
        "-map", f"0:s:{sub_index}",
        "-c:s", "webvtt",
        str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(
            f"extract_text failed src={src} idx={sub_index}: "
            f"{res.stderr[:300]}\n"
        )
        return False
    return True


def process_one(item_id: str, src_path: str, package_root: str, token: str) -> str:
    """Process a single item; return a one-word status code."""
    src = Path(src_path)
    pkg = Path(package_root)
    manifest_path = pkg / "manifest.json"
    if not manifest_path.is_file():
        return "no_manifest"
    if not src.is_file():
        return "no_source"
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as e:
        sys.stderr.write(f"manifest read failed for {item_id}: {e}\n")
        return "manifest_read_err"

    existing_subs = manifest.get("subtitles") or []
    # Index-aware "already done" check: any source subtitle stream
    # whose ordinal is reflected in manifest.subtitles[].path is
    # considered already extracted, regardless of format. That lets
    # the same script handle both "no PGS in manifest" and "no
    # mov_text in manifest" cases without re-extracting tracks that
    # were already written.
    existing_indices: set[int] = set()
    for s in existing_subs:
        path = s.get("path") or ""
        m = SUBS_PATH_INDEX_RE.match(path)
        if m:
            existing_indices.add(int(m.group(1)))

    try:
        streams = ffprobe_subs(src)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"ffprobe failed for {item_id}: {e.stderr[:300]}\n")
        return "ffprobe_err"

    # The source's i-th subtitle stream maps to subs/<i>.<ext>. We
    # want every source stream whose index isn't already covered.
    missing: list[tuple[int, dict]] = []
    for i, s in enumerate(streams):
        if i in existing_indices:
            continue
        codec = s.get("codec_name", "")
        if codec in IMAGE_CODECS or codec in TEXT_CODECS:
            missing.append((i, s))
    if not missing:
        return "already_done"

    subs_dir = pkg / "subs"
    subs_dir.mkdir(parents=True, exist_ok=True)

    new_entries: list[dict] = []
    for i, s in missing:
        codec = s["codec_name"]
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        if codec in IMAGE_CODECS:
            fmt, ext, _ = IMAGE_CODECS[codec]
            dest = subs_dir / f"{i}.{ext}"
            if not extract_image(src, i, codec, dest):
                continue
        else:
            ext = "vtt"
            fmt = "webvtt"
            dest = subs_dir / f"{i}.{ext}"
            if not extract_text(src, i, dest):
                continue
        new_entries.append(
            {
                "id": f"sub{i}",
                "path": f"subs/{i}.{ext}",
                "language": (tags.get("language") or "und"),
                "title": tags.get("title") or "",
                "default": bool(disp.get("default")),
                "forced": bool(disp.get("forced")),
                "format": fmt,
                "visible": True,
            }
        )

    if not new_entries:
        return "extract_failed"

    # Merge by id so we don't duplicate any track that somehow already
    # carried an image entry. The packaging-complete endpoint deletes
    # then re-inserts SubtitleAssets, so it's safe to send the whole
    # list — but keep the existing webvtt entries intact.
    by_id = {s.get("id"): s for s in existing_subs if s.get("id")}
    for e in new_entries:
        by_id[e["id"]] = e
    manifest["subtitles"] = list(by_id.values())

    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(manifest_path)

    try:
        post_packaging_complete(item_id, manifest, token)
    except Exception as e:
        sys.stderr.write(f"packaging-complete POST failed for {item_id}: {e}\n")
        return "post_failed"

    return f"ok({len(new_entries)})"


def main() -> int:
    token = get_token()
    # Keycloak hands out 5-15 min tokens; refresh every 4 min to keep
    # a comfortable safety margin around the lower bound. The token
    # endpoint costs nothing and gating every POST through it adds
    # ~50 ms which is negligible against the ffmpeg work.
    token_refresh_at = time.time() + 60 * 4

    n_ok = 0
    n_skip = 0
    n_err = 0
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            item_id, src_path, package_root = line.split("\t")
        except ValueError:
            sys.stderr.write(f"bad line: {raw!r}\n")
            n_err += 1
            continue
        if time.time() > token_refresh_at:
            token = get_token()
            token_refresh_at = time.time() + 60 * 4
        status = process_one(item_id, src_path, package_root, token)
        print(f"{item_id}\t{status}", flush=True)
        if status.startswith("ok("):
            n_ok += 1
        elif status in ("already_done", "no_image_subs"):
            n_skip += 1
        else:
            n_err += 1
    sys.stderr.write(f"DONE ok={n_ok} skip={n_skip} err={n_err}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
