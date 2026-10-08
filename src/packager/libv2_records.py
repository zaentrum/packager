"""The v2 library records as their writers write them — one implementation, in plain Python 3.11
with nothing but the standard library, of what a source record says about an original, what a
version record says about the cut it is, what a package record says the package carries and
lost, what an extra record says about bonus material, and the checksums and the .complete marker
that close a version's chain.

Two writers use it, and they must agree byte for byte, because the deletion gate — the essence of
a version's sources minus the essence of its package — is computed from what they write:

  * the packager, which records an original the first time it packages it (sources/<sourceId>/),
    and every package it makes (versions/<versionId>/, extras/<extraId>/). It vendors this file
    byte-identically, and a hash test in each repository guards the copy;
  * library-v2-from-catalog.py, which records what a catalog already holds, and imports it.

Nothing here reads a database, the network or a clock: every moment is passed in, every id is
derived (did) or given, and the same inputs always make the same bytes. Nothing here writes a file
either — the builders return records and bytes, and the writer puts them where its layout says,
through a temporary file and a rename. What a builder could not know stays empty rather than
guessed, and what it normalises it says, in the notes it returns.

No record names a file as it arrived, and no file in the library is named so: a name, a folder or a
container title can tell where a file came from. An original a version keeps, a source describes or
an extra was packaged from is named by library_original_name(), original.<ext>; the copy of a
subtitle file that came with it by subtitle_copy_name(), subtitle-<n>.<lang>[.forced][.sdh].<ext>;
and the probe a source keeps is the tool's output with the two edits scrub_probe() makes. The name a
file arrived under is read for what it claims — its labels and its numbering — and the record keeps
those values, never the name.

The order a version's folder is written in, which only the writer can keep:

  version.json        version_record(), then json_bytes()
  checksums.sha256    checksums(): version.json and every package file
  package.json        package_record(), holding the checksums file's hash
  .complete           complete(): sha256:<hex> of package.json, written last

The original a version is established with is no link of that chain — its source record's fixity
covers it, and it is deleted later — and goes in under library_original_name() once the rest is
built: renamed last into the staged folder, which then goes into place whole. A version taken in
before anything packaged it is version.json and its original alone; its package is added later, in
the same order.

A source folder: source.json from source_record() (with the probe it returns, written beside it as
ffprobe.json), a copy of each subtitle file that came with the original under the name
sidecar_entry() gives it, then the checksums.sha256 that lists exactly those files. Nothing else that
sat beside the original is copied: NFO text routinely names where a file came from. An extra's
folder: extra.json from extra_record(), the package, then the chain as a version's, its checksums
over extra.json and every package file. chain_problems() checks a folder written that way.

Piped into a pod behind a tool that uses it (cat libv2_records.py tool.py | python3 - …), this
file runs as part of that tool, so it defines no main and runs nothing when it is read.
"""
import copy, datetime, hashlib, json, os, re, shutil, subprocess, uuid

# The version of what this module writes. A change to the shape of a record it builds is a new
# value, and the copy a writer vendors names the one it was taken at. 2: no record names a file as
# it arrived, and a source copies only the subtitle files that came with its original. 3: a source
# covers the episodes its file holds as the database linked them, and its naming reads a file of
# several episodes as the catalog's scanner does, from the first to the last.
LIBV2_RECORDS = 3

NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://zaentrum.github.io/schemas/library")
PACKAGE_DIRS = ("hls", "subs", "trickplay", "trailers")
# An extra's package is a version's without trailers: a trailer of its own is an extra.
EXTRA_DIRS = ("hls", "subs", "trickplay")
SUMS = "checksums.sha256"
OS_ARTEFACTS = re.compile(r"^(\.DS_Store|\._.*|Thumbs\.db|desktop\.ini|@eaDir|\.@__thumb|#recycle|\.AppleDouble)$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$")
SEGMENT_KINDS = {"intro", "recap", "credits", "preview", "commercial", "other"}
# The command a source record's ffprobe.json is the output of, but for the edits scrub_probe() makes.
FFPROBE_ARGS = ("-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters")
# The names the library gives an original (library_original_name) and the copy of a subtitle file that
# came with it (subtitle_copy_name): nothing of the name either arrived under, and only [a-z0-9.-].
ORIGINAL_NAME_RE = re.compile(r"^original(-[0-9]+)?\.[a-z0-9]{1,8}$")
SUBTITLE_COPY_RE = re.compile(r"^subtitle-[0-9]+\.[a-z]{2,8}(\.forced)?(\.sdh)?\.[a-z0-9]{1,8}$")
# The values HLS gives VIDEO-RANGE, as a video rendition's videoRange keeps them.
VIDEO_RANGES = ("SDR", "PQ", "HLG")


# ---------------------------------------------------------------- small helpers
def did(*parts):
    """A generated id: the same inputs always give the same id, so a re-run writes the same tree."""
    return str(uuid.uuid5(NS, ":".join(str(p) for p in parts)))


def sha_bytes(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def json_bytes(doc):
    """A record as it is written: two-space indent, UTF-8, one trailing line break."""
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def qh1(p):
    """sha256(first 64 KiB || last 64 KiB || uint64be(size)): cheap proof a large file is the one
    recorded, two reads however big the file is."""
    size = os.path.getsize(p)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read(65536))
        if size > 65536:
            f.seek(max(size - 65536, 0))
            h.update(f.read(65536))
    h.update(size.to_bytes(8, "big"))
    return "sha256:" + h.hexdigest()


def ts(v):
    """RFC 3339 with an upper-case T and Z, or None."""
    if not v:
        return None
    s = str(v).strip().replace(" ", "T")
    if not re.search(r"(Z|[+-]\d\d:?\d\d)$", s):
        s += "Z"
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)
    return s.replace("+00:00", "Z")


def is_moment(s):
    """Whether s is a timestamp as a record holds one: RFC 3339 with an upper-case T and Z, and a
    real moment."""
    if not s or not TIMESTAMP_RE.match(s):
        return False
    try:
        datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def ts_of_mtime(p):
    t = datetime.datetime.fromtimestamp(os.path.getmtime(p), datetime.timezone.utc)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def text(v):
    """A one-line string, or None: a record's fields never carry a line break."""
    if v is None:
        return None
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    return s or None


def ratio(v):
    return v if v and v not in ("0:1", "N/A") else None


def listdir(d):
    return sorted(n for n in os.listdir(d) if not OS_ARTEFACTS.match(n))


def walk_files(root):
    """Every file under root, relative to it, in a stable order and without OS artefacts."""
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not OS_ARTEFACTS.match(x))
        out += [os.path.relpath(os.path.join(base, f), root) for f in sorted(files) if not OS_ARTEFACTS.match(f)]
    return sorted(out)


ISO639 = {
    "eng": "en", "ger": "de", "deu": "de", "fre": "fr", "fra": "fr", "ita": "it", "spa": "es", "jpn": "ja",
    "chi": "zh", "zho": "zh", "dut": "nl", "nld": "nl", "por": "pt", "rus": "ru", "pol": "pl", "cze": "cs",
    "ces": "cs", "ukr": "uk", "hin": "hi", "kor": "ko", "swe": "sv", "nor": "no", "nob": "nb", "dan": "da",
    "fin": "fi", "tur": "tr", "ara": "ar", "heb": "he", "hun": "hu", "gre": "el", "ell": "el", "rum": "ro",
    "ron": "ro", "tha": "th", "vie": "vi", "ind": "id", "may": "ms", "msa": "ms", "cat": "ca", "hrv": "hr",
    "slv": "sl", "slo": "sk", "slk": "sk", "srp": "sr", "bul": "bg", "est": "et", "lav": "lv", "lit": "lt",
    "ice": "is", "isl": "is", "fil": "fil", "tel": "te", "tam": "ta", "und": "und", "zxx": "zxx", "mul": "mul",
    "baq": "eu", "eus": "eu", "glg": "gl", "kan": "kn", "mal": "ml", "ben": "bn", "mar": "mr", "urd": "ur",
    "per": "fa", "fas": "fa", "wel": "cy", "cym": "cy", "arm": "hy", "hye": "hy", "geo": "ka", "kat": "ka",
    "mac": "mk", "mkd": "mk", "alb": "sq", "sqi": "sq", "tgl": "tl", "lao": "lo", "khm": "km", "bur": "my",
    "mya": "my", "sin": "si", "nep": "ne", "pan": "pa", "guj": "gu", "tib": "bo", "bod": "bo", "mon": "mn",
    "kaz": "kk", "uzb": "uz", "aze": "az", "bel": "be", "bos": "bs", "gle": "ga", "bre": "br", "ltz": "lb",
    "afr": "af", "swa": "sw", "zul": "zu", "xho": "xh", "amh": "am", "som": "so", "hau": "ha", "yor": "yo",
    "ibo": "ig", "epo": "eo", "lat": "la", "yid": "yi", "jav": "jv", "sun": "su", "mao": "mi", "mri": "mi",
    "grn": "gn", "que": "qu", "nno": "nn", "fao": "fo", "kal": "kl", "iku": "iu", "tat": "tt", "kur": "ku",
    "pus": "ps", "tuk": "tk", "kir": "ky", "tgk": "tg", "mlt": "mt", "roh": "rm", "sco": "sco", "haw": "haw",
}


def lang(raw):
    """(BCP 47 code, the code the file carried) — 'eng' becomes 'en' and keeps 'eng' beside it."""
    if not raw:
        return None, None
    r = str(raw).strip().lower()
    if re.fullmatch(r"[a-z]{2}(-[a-z0-9]{2,8})?", r):
        return r, None
    if r in ISO639:
        return ISO639[r], r
    if re.fullmatch(r"[a-z]{3}", r):
        return r, None
    return "und", r


def primary_language(raw):
    code = (lang(raw)[0] or "und").split("-")[0]
    return {"nb": "no", "nn": "no"}.get(code, code)


LANGUAGE_RE = re.compile(r"^([a-z]{2,3}(-[A-Za-z0-9]{2,8})*|und|zxx|mul)$")


def rendition_language(raw):
    """A package rendition keeps the code the media file carried ('eng'), not a normalised one: it
    describes the playlist as it was written. Only a code the format cannot hold is normalised."""
    r = str(raw or "").strip()
    return r if LANGUAGE_RE.match(r) else (lang(r)[0] or "und")


# ---------------------------------------------------------------- what a file name claims
QUALITY = re.compile(r"\b(Remux|Blu-?ray|DVD|WEB[A-Za-z-]*|HDTV|SDTV)[- ](\d{3,4}p)\b|\b(\d{3,4}p)\b", re.I)
EDITION_WORDS = [
    ("directors-cut", r"director'?s?[ ._-]?cut"), ("extended", r"\bextended\b"), ("unrated", r"\bunrated\b"),
    ("theatrical", r"\btheatrical\b"), ("special-edition", r"special[ ._-]?edition"),
    ("remastered", r"\bremaster(ed)?\b"), ("restored", r"\brestor(ed|ation)\b"), ("imax", r"\bimax\b"),
    ("final-cut", r"final[ ._-]?cut"),
]
# How a name numbers the episodes its file holds, read as the catalog's scanner reads a name, so that a
# source's naming says the range the catalog links the file's episodes by: S05E15, letter case aside, standing
# apart from the words around it — no letter, digit or underscore just before or after — and not followed by
# a digit; for a file of several, the episodes after it, each where the one before ends — S05E15E16,
# S05E15-E16 (or .E16, _E16, " E16"), S05E15-16, S05E15-E17, S05E15E16E17 — each after the one before, and
# at most MAX_COVERED episodes from the first to the last. A number after a dash that a p follows is none
# (S05E15-720p is the fifteenth episode in 720p), and neither is one a digit follows (S05E15-1080), one no
# higher than the one before, or one that would cover more than MAX_COVERED (S05E15-264); the token ends
# where it still stands apart (S05E15-E16x numbers the fifteenth alone). '1x05', which the scanner does not
# read, numbers one episode and never a range.
EPISODE_HEAD = re.compile(r"\bS(\d{1,2})E(\d{1,3})", re.I | re.A)
EPISODE_MORE = re.compile(r"(?:[ ._-]?E|-)(\d{1,3})", re.I | re.A)
EPISODE_NXN = re.compile(r"(?<![0-9a-z])(\d{1,2})x(\d{2,3})(?![0-9])", re.I)
# The most episodes one file covers, the first and the last among them.
MAX_COVERED = 10
DIGITS = "0123456789"


def medium_of(token):
    t = token.lower()
    return "web" if t.startswith("web") else "disc" if t in ("remux", "bluray", "blu-ray", "dvd") else \
        "broadcast" if t in ("hdtv", "sdtv") else "unknown"


def edition_word(t):
    if not t:
        return None
    for kind, pat in EDITION_WORDS:
        if re.search(pat, t, re.I):
            return kind
    return None


def labels(name, folder):
    hits = list(QUALITY.finditer(os.path.splitext(name)[0]))
    m = hits[-1] if hits else None
    fold_ed = None
    fm = re.search(r" - ([^()]+?) \(\d{4}\)$", folder or "")
    if fm and edition_word(fm.group(1)):
        fold_ed = fm.group(1).strip()
    return {
        "quality": m.group(0) if m else None,
        "medium": medium_of(m.group(1)) if m and m.group(1) else None,
        "resolution": ((m.group(2) or m.group(3)) if m else None),
        "edition": edition_word(name),
        "folderEdition": fold_ed,
    }


def episode_range(stem):
    """The first token of stem that numbers episodes as the catalog's scanner reads one (EPISODE_HEAD
    above): (where it starts, where it ends, its season, the first episode, the last) — the last the first
    for a file of one episode — or None."""
    word = lambda c: c in DIGITS or c == "_" or "a" <= c <= "z" or "A" <= c <= "Z"
    for m in EPISODE_HEAD.finditer(stem):
        end = m.end()
        if end < len(stem) and stem[end] in DIGITS:
            continue  # E1500: no episode of a season numbers so
        first = last = int(m.group(2))
        after, at = [], end
        while True:  # the episodes after the first, each where the one before ends
            x = EPISODE_MORE.match(stem, at)
            if not x:
                break
            n, nxt = int(x.group(1)), x.end()
            dashed = stem[at] == "-" and stem[at + 1] in DIGITS
            if nxt < len(stem) and (stem[nxt] in DIGITS or (dashed and stem[nxt] in "pP")):
                break  # -720p, -1080: no episode
            if n <= last or n - first + 1 > MAX_COVERED:
                break
            after.append((nxt, n))
            at, last = nxt, n
        for tok_end, tok_last in reversed([(end, first)] + after):  # the longest of it that stands apart
            if tok_end == len(stem) or not word(stem[tok_end]):
                return m.start(), tok_end, int(m.group(1)), first, tok_last
    return None


def naming(name):
    """How the file's own name numbered what it holds, read as the catalog's scanner reads it: a file of
    several episodes from its first to its last, episodeEnd the last. The name never says which ordering it
    used, so the scheme stays 'unknown' and only the numbers and the token itself are recorded."""
    stem = os.path.splitext(name)[0]
    tok, nxn = episode_range(stem), EPISODE_NXN.search(stem)
    if tok and not (nxn and nxn.start() < tok[0]):
        start, end, season, first, last = tok
        return {"scheme": "unknown", "seasonNumber": season, "episodeNumber": first,
                "episodeEnd": last if last != first else None, "raw": stem[start:end]}
    if nxn:
        return {"scheme": "unknown", "seasonNumber": int(nxn.group(1)), "episodeNumber": int(nxn.group(2)),
                "episodeEnd": None, "raw": nxn.group(0)}
    return None


# ---------------------------------------------------------------- the names the library gives
def _extension(name):
    """A file name's extension as a name in the library keeps it: lower-cased, when it is one to eight
    letters and digits, and bin when it is anything else or nothing."""
    ext = os.path.splitext(str(name or "").replace("\\", "/").rsplit("/", 1)[-1])[1][1:].lower()
    return ext if re.fullmatch(r"[a-z0-9]{1,8}", ext) else "bin"


def library_original_name(arrival_name: str, part: int | None = None) -> str:
    """The name the library gives an original: original.<ext>, or original-<part>.<ext> for one part
    of a version split into several, in part order from 1 — <ext> the extension of the name it arrived
    under, lower-cased when that is one to eight letters and digits, and bin when it is not. Nothing
    else of that name is kept. The same name is the original's in its version folder, its source
    record's file.name, the version's originalFiles and an extra's packagedFrom."""
    if part is not None and (not isinstance(part, int) or isinstance(part, bool) or part < 1):
        raise ValueError(f"a part of a version is numbered from 1, not {part!r}")
    return f"original.{_extension(arrival_name)}" if part is None else f"original-{part}.{_extension(arrival_name)}"


def subtitle_copy_name(n: int, language: str | None, forced: bool, sdh: bool, ext: str) -> str:
    """The name of the copy a source folder keeps of the nth subtitle file that came with its original
    (n from 1, in the order the catalog lists them): subtitle-<n>.<lang>[.forced][.sdh].<ext>, lang
    the BCP 47 primary language subtag of language, lower-cased ('ger' and 'de-CH' are de), or und when
    it names none, and ext the file's own extension, lower-cased, or bin when it is not one to eight
    letters and digits. It says only what the record says of the file, never the name it came with."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"the subtitle files of a source are numbered from 1, not {n!r}")
    primary = str(language or "").strip().replace("_", "-").split("-")[0]
    code = (lang(primary)[0] or "und").lower()
    if not re.fullmatch(r"[a-z]{2,8}", code):
        code = "und"
    return f"subtitle-{n}.{code}" + (".forced" if forced else "") + (".sdh" if sdh else "") + \
        f".{_extension('x.' + str(ext or '').lstrip('.'))}"


# ---------------------------------------------------------------- streams, as ffprobe reported them
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "arib_caption"}
IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
LOSSLESS_AUDIO = {"truehd", "mlp", "flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_bluray",
                  "pcm_dvd", "wavpack", "ape", "tta"}
CLAIM_CODECS = [("truehd", r"truehd|true-hd|true hd"), ("dts-hd ma", r"dts[- .]?hd[- .]?(ma|master)"),
                ("dts", r"\bdts\b"), ("e-ac-3", r"e-?ac-?3|dd\+|ddp|dolby digital plus"),
                ("ac-3", r"\bac-?3\b|dolby digital(?! plus)"), ("flac", r"\bflac\b"), ("opus", r"\bopus\b"),
                ("aac", r"\baac\b")]
VARIANT_WORDS = re.compile(r"\b(Latin[ -]?American|LatAm|European|Castilian|Canadian|Brazilian|Portugal|"
                           r"Simplified|Traditional|Cantonese|Mandarin|Flemish|Swiss|Austrian|Mexican|"
                           r"Hong Kong|Taiwanese|Belgian|Qu[eé]b[eé]cois)\b", re.I)
RE_ENCODE = re.compile(r"handbrake|x264|x265|lavf|lavc|ffmpeg|nvenc|vaapi|qsv|mencoder|staxrip|shutter", re.I)


def title_variant(t):
    m = VARIANT_WORDS.search(t or "")
    return m.group(1) if m else None


def claim(title):
    if not title:
        return None
    t = title.lower()
    codec = next((c for c, pat in CLAIM_CODECS if re.search(pat, t)), None)
    m = re.search(r"\b([1-9])[.,]([0-2])\b", t)
    channels = f"{m.group(1)}.{m.group(2)}" if m else None
    return None if not codec and not channels else {"codec": codec, "channels": channels}


def claim_contradicts(c, codec, channels):
    if not c:
        return False
    actual = (codec or "").lower()
    family = {"truehd": ("truehd", "mlp"), "dts-hd ma": ("dts",), "dts": ("dts",), "e-ac-3": ("eac3",),
              "ac-3": ("ac3",), "flac": ("flac",), "opus": ("opus",), "aac": ("aac",)}
    bad = bool(c["codec"] and not any(actual.startswith(x) for x in family.get(c["codec"], ())))
    if c["channels"]:
        a, b = c["channels"].split(".")
        bad = bad or int(a) + int(b) != channels
    return bad


def dispositions(s):
    d = s.get("disposition") or {}
    out = {"default": bool(d.get("default")), "forced": bool(d.get("forced")), "original": bool(d.get("original")),
           "dub": bool(d.get("dub")), "commentary": bool(d.get("comment")),
           "hearingImpaired": bool(d.get("hearing_impaired")), "visualImpaired": bool(d.get("visual_impaired"))}
    if d.get("attached_pic"):
        out["attachedPic"] = True
    for raw, key in (("lyrics", "lyrics"), ("captions", "captions"), ("descriptions", "descriptions")):
        if d.get(raw):
            out[key] = True
    return out


def subtitle_purpose(s):
    d = s.get("disposition") or {}
    t = ((s.get("tags") or {}).get("title") or "").lower()
    if d.get("forced"):
        return "forced", "disposition"
    if d.get("hearing_impaired") or d.get("captions"):
        return "sdh", "disposition"
    if d.get("comment"):
        return "commentary", "disposition"
    if d.get("lyrics"):
        return "lyrics", "disposition"
    if re.search(r"\bforced\b", t) and not re.search(r"\b(non|not|no|un)[- ]?forced\b", t):
        return "forced", "title"
    if re.search(r"\bsigns?\b.{0,12}\bsongs?\b|\bsigns only\b", t):
        return "signs-songs", "title"
    if re.search(r"\bsdh\b|\bcc\b|hearing|closed caption", t):
        return "sdh", "title"
    if re.search(r"commentary|kommentar|commentaire|comentario|commento|commentaar", t):
        return "commentary", "title"
    if re.search(r"\blyrics\b|karaoke", t):
        return "lyrics", "title"
    return "dialogue", "assumed"


def audio_purpose(s):
    d = s.get("disposition") or {}
    t = ((s.get("tags") or {}).get("title") or "").lower()
    if d.get("comment"):
        return "commentary", "disposition"
    if d.get("visual_impaired") or d.get("descriptions"):
        return "description", "disposition"
    if re.search(r"commentary|kommentar|commentaire|comentario|commento|commentaar", t):
        return "commentary", "title"
    if re.search(r"audio description|descriptive (video|audio)|audiodeskription|audiodescription", t):
        return "description", "title"
    return "main", "assumed"


def hdr_info(s):
    trc = s.get("color_transfer")
    side = s.get("side_data_list") or []
    dovi = next((x for x in side if "DOVI" in (x.get("side_data_type") or "")), None)
    mastering = next((x for x in side if "Mastering display" in (x.get("side_data_type") or "")), None)
    cll = next((x for x in side if "Content light" in (x.get("side_data_type") or "")), None)
    fmt = "dolby-vision" if dovi else "hdr10" if trc == "smpte2084" else "hlg" if trc == "arib-std-b67" else "sdr"
    out = {"format": fmt, "masteringDisplay": None, "contentLightLevel": None, "dolbyVision": None}
    if mastering:
        out["masteringDisplay"] = {k: v for k, v in mastering.items() if k != "side_data_type"}
    if cll:
        out["contentLightLevel"] = {k: v for k, v in cll.items() if k != "side_data_type"}
    if dovi:
        out["dolbyVision"] = {"profile": num(dovi.get("dv_profile")) or 0, "level": num(dovi.get("dv_level")) or 0,
                              "rpuPresent": bool(dovi.get("rpu_present_flag")),
                              "elPresent": bool(dovi.get("el_present_flag")),
                              "blPresent": bool(dovi.get("bl_present_flag")),
                              "blCompatibilityId": num(dovi.get("dv_bl_signal_compatibility_id")) or 0}
    return out


def stereo3d(s):
    tags = s.get("tags") or {}
    mode = (tags.get("stereo_mode") or tags.get("STEREO_MODE") or "").lower()
    for x in s.get("side_data_list") or []:
        if "Stereo 3D" in (x.get("side_data_type") or ""):
            mode = (x.get("type") or mode or "").lower()
    return None if not mode or mode in ("mono", "2d") else mode


def bit_depth(s):
    b = num(s.get("bits_per_raw_sample"))
    if b:
        return b
    pf = s.get("pix_fmt") or ""
    m = re.search(r"p(\d{2})(le|be)?$", pf)
    return int(m.group(1)) if m else (8 if pf else None)


def norm_stream(s):
    t = s.get("codec_type")
    tags = s.get("tags") or {}
    language, raw = lang(tags.get("language"))
    base = {"index": s["index"], "codec": s.get("codec_name"), "language": language, "languageRaw": raw,
            "title": text(tags.get("title")), "dispositions": dispositions(s)}
    if t == "video":
        base.update({
            "type": "video", "profile": s.get("profile"),
            "level": s.get("level") if isinstance(s.get("level"), (int, float)) and s.get("level") >= 0 else None,
            "bitDepth": bit_depth(s), "pixelFormat": s.get("pix_fmt"), "width": s.get("width"),
            "height": s.get("height"), "sampleAspectRatio": ratio(s.get("sample_aspect_ratio")),
            "displayAspectRatio": ratio(s.get("display_aspect_ratio")),
            "frameRate": s.get("avg_frame_rate") if s.get("avg_frame_rate") not in (None, "0/0") else s.get("r_frame_rate"),
            "fieldOrder": s.get("field_order"), "bitrate": num(s.get("bit_rate")),
            "colour": {"primaries": s.get("color_primaries"), "transfer": s.get("color_transfer"),
                       "matrix": s.get("color_space"), "range": s.get("color_range")},
            "hdr": hdr_info(s), "stereo3d": stereo3d(s), "closedCaptions": bool(s.get("closed_captions")),
            "encoder": text(tags.get("ENCODER") or tags.get("encoder")),
        })
        return base
    if t == "audio":
        codec = (s.get("codec_name") or "").lower()
        profile = s.get("profile") or ""
        channels = num(s.get("channels")) or 1
        c = claim(tags.get("title"))
        if c is not None:
            c["contradictsActual"] = claim_contradicts(c, codec, channels)
        base.update({
            "type": "audio", "profile": profile or None, "channels": channels,
            "channelLayout": s.get("channel_layout"), "sampleRate": num(s.get("sample_rate")),
            "bitDepth": num(s.get("bits_per_raw_sample")) or None,
            "bitrate": num(s.get("bit_rate")) or num(tags.get("BPS")),
            "lossless": codec in LOSSLESS_AUDIO or (codec == "dts" and "MA" in profile),
            "objectAudio": "atmos" if "atmos" in profile.lower() else "dts-x" if "dts:x" in profile.lower() else "none",
            "titleClaim": c, "encoder": text(tags.get("ENCODER") or tags.get("encoder")),
        })
        base["purpose"], base["purposeFrom"] = audio_purpose(s)
        base["variant"] = title_variant(tags.get("title"))
        return base
    if t == "subtitle":
        codec = (s.get("codec_name") or "").lower()
        events = next((num(v) for k, v in tags.items() if k.upper().startswith("NUMBER_OF_FRAMES")), None)
        base.update({"type": "subtitle", "form": "image" if codec in IMAGE_SUBS else "text",
                     "styled": codec in ("ass", "ssa"), "variant": title_variant(tags.get("title")),
                     "events": events})
        base["purpose"], base["purposeFrom"] = subtitle_purpose(s)
        return base
    if t == "attachment":
        fname, mime = tags.get("filename"), tags.get("mimetype")
        role = "font" if (mime and "font" in mime) or (fname and re.search(r"\.(ttf|otf|ttc)$", fname, re.I)) \
            else "cover" if (mime and mime.startswith("image/")) else "other"
        base.update({"type": "attachment", "filename": text(fname), "mimetype": text(mime), "role": role})
        return base
    base.update({"type": "data"})
    return base


def infer_forced_by_size(streams):
    """An untitled, unflagged subtitle with a small fraction of the events of the full track in its
    language translates a few lines, not the film: it is a forced track."""
    subs = [x for x in streams if x["type"] == "subtitle"]
    for x in subs:
        if x["purposeFrom"] != "assumed" or not x.get("events"):
            continue
        peers = [y["events"] for y in subs if y is not x and y.get("events")
                 and primary_language(y.get("language")) == primary_language(x.get("language"))]
        if peers and max(peers) >= 300 and x["events"] < 0.1 * max(peers):
            x["purpose"], x["purposeFrom"] = "forced", "content"


# ---------------------------------------------------------------- essence
def track_essence(audio, subs, closed_captions):
    def langs(items, purposes):
        return sorted({lang(x.get("language"))[0] for x in items if x.get("purpose") in purposes and x.get("language")})
    return {"commentarySubtitles": sum(1 for x in subs if x.get("purpose") == "commentary"),
            "audioDescriptionTracks": sum(1 for x in audio if x.get("purpose") == "description"),
            "sdhSubtitleLanguages": langs(subs, ("sdh",)),
            "forcedSubtitleLanguages": langs(subs, ("forced", "signs-songs")),
            "closedCaptions": bool(closed_captions)}


def source_essence(streams, chapters):
    v = [x for x in streams if x["type"] == "video" and not x["dispositions"].get("attachedPic")]
    a = [x for x in streams if x["type"] == "audio"]
    sub = [x for x in streams if x["type"] == "subtitle"]
    v0 = v[0] if v else {}
    hdr = v0.get("hdr") or {}
    return {
        "maxAudioChannels": max([x["channels"] for x in a], default=0),
        "surround": any(x["channels"] > 2 for x in a),
        "losslessAudio": any(x.get("lossless") for x in a),
        "objectAudio": any(x.get("objectAudio") in ("atmos", "dts-x") for x in a),
        "maxVideoHeight": v0.get("height"), "videoBitDepth": v0.get("bitDepth"),
        "hdr10Metadata": bool(hdr.get("masteringDisplay") or hdr.get("contentLightLevel")),
        "dolbyVision": hdr.get("format") == "dolby-vision", "stereo3d": bool(v0.get("stereo3d")),
        "interlaced": (v0.get("fieldOrder") or "progressive") not in ("progressive", "unknown", None),
        "audioLanguages": sorted({x["language"] for x in a if x.get("language")}),
        "subtitleLanguages": sorted({x["language"] for x in sub if x.get("language")}),
        "subtitleTracks": len(sub), "imageSubtitles": any(x.get("form") == "image" for x in sub),
        "styledSubtitles": any(x.get("styled") for x in sub),
        "fonts": any(x["type"] == "attachment" and x.get("role") == "font" for x in streams),
        "chapters": bool(chapters),
        "commentaryTracks": sum(1 for x in a if x.get("purpose") == "commentary"),
        **track_essence(a, sub, v0.get("closedCaptions", False)),
    }


def package_essence(man, source=None):
    """What a package carries, in the terms of a source's essence. The 5.1 companions under
    renditions.audioSurround count for the channels and the languages it carries; a track is counted
    once, by its stereo rendition, for what a track is for. A top video rendition that is the
    original's own stream, copied (encoder 'copy'), in PQ, carries the HDR10 metadata in its bitstream
    when the original — source, its essence — had it; anything re-encoded is taken to have lost it."""
    ren = man.get("renditions") or {}
    vids, auds = ren.get("video") or [], ren.get("audio") or []
    every = auds + (ren.get("audioSurround") or [])
    subs = man.get("subtitles") or []
    v0 = vids[0] if vids else {}
    codec = v0.get("codec") or ""
    copied_pq = v0.get("encoder") == "copy" and v0.get("videoRange") == "PQ"
    return {
        "maxAudioChannels": max([x.get("channels") or 0 for x in every], default=0),
        "surround": any((x.get("channels") or 0) > 2 for x in every),
        "losslessAudio": False, "objectAudio": False, "maxVideoHeight": v0.get("height"),
        "videoBitDepth": 10 if codec.startswith(("hev1.2", "hvc1.2")) else (8 if codec else None),
        "hdr10Metadata": bool(copied_pq and (source or {}).get("hdr10Metadata")), "dolbyVision": False,
        "stereo3d": False, "interlaced": False,
        "audioLanguages": sorted({lang(x.get("language"))[0] for x in every if x.get("language")}),
        "subtitleLanguages": sorted({lang(x.get("language"))[0] for x in subs if x.get("language")}),
        "subtitleTracks": len(subs),
        "imageSubtitles": any((x.get("format") or "") in ("pgs", "sup", "vobsub", "dvb") for x in subs),
        "styledSubtitles": False, "fonts": False, "chapters": False,
        "commentaryTracks": sum(1 for x in auds if x.get("purpose") == "commentary"),
        **track_essence(auds, subs, False),
    }


CODEC_FAMILY = [("aac", r"^(mp4a\.40|aac)"), ("ac3", r"^(ac-3|ac3)"), ("eac3", r"^(ec-3|eac3)"),
                ("opus", r"^opus"), ("flac", r"^(fLaC|flac)"), ("alac", r"^alac"), ("truehd", r"^(truehd|mlp)"),
                ("dts", r"^dts"), ("hevc", r"^(hev1|hvc1|hevc)"), ("h264", r"^(avc1|avc3|h264)"),
                ("av1", r"^av0?1"), ("vp9", r"^vp0?9")]


def codec_family(codec):
    """'mp4a.40.2' and 'aac' are the same codec under two names; a package advertises the MP4 name
    and a probe reports the library name, so they are compared as families."""
    c = (codec or "").strip().lower()
    for family, pat in CODEC_FAMILY:
        if re.match(pat, c):
            return family
    return c


def losses_against(src, pkg_essence, streams, renditions, subs):
    """Every way the package is poorer than the original, in the package record's own terms. A source
    track is packaged once, as its stereo rendition, so only those count for a track that was dropped;
    its 5.1 companion counts for the codecs the package carries."""
    out = []
    a = [x for x in streams if x["type"] == "audio"]
    v = [x for x in streams if x["type"] == "video" and not x["dispositions"].get("attachedPic")]
    sub = [x for x in streams if x["type"] == "subtitle"]
    auds = renditions.get("audio") or []
    vids = renditions.get("video") or []
    if src.get("maxAudioChannels", 0) > pkg_essence.get("maxAudioChannels", 0):
        out.append({"kind": "audio-downmix",
                    "detail": f"{src['maxAudioChannels']}ch -> {pkg_essence.get('maxAudioChannels', 0)}ch",
                    "sourceStreamIndex": a[0]["index"] if a else None})
    codecs = sorted({codec_family(x.get("codec")) for x in a})
    packaged = sorted({codec_family(x.get("codec")) for x in auds + (renditions.get("audioSurround") or [])})
    if codecs and any(c not in codecs for c in packaged):
        out.append({"kind": "audio-codec", "detail": f"{'/'.join(codecs)} -> {'/'.join(packaged)}",
                    "sourceStreamIndex": a[0]["index"] if a else None})
    if len(a) > len(auds):
        out.append({"kind": "audio-dropped", "detail": f"{len(a) - len(auds)} of {len(a)} audio track(s) not packaged",
                    "sourceStreamIndex": None})
    if v and vids and (v[0].get("height") or 0) > (vids[0].get("height") or 0):
        out.append({"kind": "video-resolution", "detail": f"{v[0]['height']}p -> {vids[0]['height']}p",
                    "sourceStreamIndex": v[0]["index"]})
    if (src.get("videoBitDepth") or 0) > (pkg_essence.get("videoBitDepth") or 0):
        out.append({"kind": "video-bitdepth",
                    "detail": f"{src['videoBitDepth']}bit -> {pkg_essence.get('videoBitDepth')}bit",
                    "sourceStreamIndex": v[0]["index"] if v else None})
    if src.get("hdr10Metadata") and not pkg_essence.get("hdr10Metadata"):
        out.append({"kind": "dynamic-range", "detail": "HDR10 metadata not carried into the package",
                    "sourceStreamIndex": v[0]["index"] if v else None})
    if src.get("dolbyVision") and not pkg_essence.get("dolbyVision"):
        out.append({"kind": "dolby-vision", "detail": "Dolby Vision layer not carried into the package",
                    "sourceStreamIndex": v[0]["index"] if v else None})
    if src.get("stereo3d") and not pkg_essence.get("stereo3d"):
        out.append({"kind": "stereo3d", "detail": "3D presentation not carried into the package",
                    "sourceStreamIndex": v[0]["index"] if v else None})
    if len(sub) > len(subs):
        out.append({"kind": "subtitle-dropped",
                    "detail": f"{len(sub) - len(subs)} of {len(sub)} source subtitle track(s) not packaged",
                    "sourceStreamIndex": None})
    if src.get("styledSubtitles") and not pkg_essence.get("styledSubtitles"):
        out.append({"kind": "subtitle-styling", "detail": "ASS/SSA typesetting flattened", "sourceStreamIndex": None})
    if src.get("closedCaptions") and not pkg_essence.get("closedCaptions"):
        out.append({"kind": "closed-captions-dropped", "detail": "embedded CEA-608/708 captions not carried",
                    "sourceStreamIndex": v[0]["index"] if v else None})
    if src.get("fonts") and not pkg_essence.get("fonts"):
        out.append({"kind": "attachments-dropped", "detail": "attached fonts not carried", "sourceStreamIndex": None})
    return out


# What an original carries that a package can fail to carry, in the terms an original-deleted event
# accepts. 'chapters' is not here (the version keeps the marks, not the file) and neither is
# 'interlaced' (a deinterlaced picture is not a poorer one).
LOSS_FLAGS = ("surround", "losslessAudio", "objectAudio", "hdr10Metadata", "dolbyVision", "stereo3d",
              "imageSubtitles", "styledSubtitles", "fonts", "closedCaptions")
LOSS_COUNTS = ("maxAudioChannels", "maxVideoHeight", "videoBitDepth", "subtitleTracks",
               "commentaryTracks", "commentarySubtitles", "audioDescriptionTracks")
LOSS_LANGUAGES = ("audioLanguages", "subtitleLanguages", "sdhSubtitleLanguages", "forcedSubtitleLanguages")


def deletion_gate(sources, package):
    """What deleting a version's originals costs: the essence of its sources minus that of its
    package, sorted. Nothing records it, so every reader computes it the same way, and an
    original-deleted event accepts no more than it."""
    lost = set()
    for key in LOSS_FLAGS:
        if any(s.get(key) for s in sources) and not package.get(key):
            lost.add(key)
    for key in LOSS_COUNTS:
        had = [s[key] for s in sources if s.get(key) is not None]
        kept = package.get(key)
        if had and (kept is None or kept < max(had)):
            lost.add(key)
    for key in LOSS_LANGUAGES:
        kept = set(package.get(key) or [])
        lost |= {f"{key}:{x}" for s in sources for x in s.get(key) or [] if x not in kept}
    return sorted(lost)


# ---------------------------------------------------------------- ffprobe
def have_ffprobe():
    return shutil.which("ffprobe") is not None


def ffprobe(path):
    """The verbatim probe of a file — ffprobe -show_format -show_streams -show_chapters, as JSON — or
    None. Reads headers only, so it is cheap even over NFS."""
    try:
        r = subprocess.run(["ffprobe", *FFPROBE_ARGS, path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=300)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ffprobe_version():
    try:
        r = subprocess.run(["ffprobe", "-version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        return text(r.stdout.decode("utf-8", "replace").splitlines()[0]) if r.returncode == 0 else None
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def scrub_probe(probe: dict, library_name: str) -> dict:
    """The probe as a source folder keeps it, ffprobe.json: a copy of the tool's output with two edits
    and nothing else changed — format.filename is library_name, the name the library gives the
    original, not the path the tool read, and every key of format.tags that is title, in any case, is
    gone. Both could tell where the file came from. The probe passed in is left as it was."""
    out = copy.deepcopy(probe)
    fmt = out.get("format") if isinstance(out, dict) else None
    if isinstance(fmt, dict):
        fmt["filename"] = library_name
        tags = fmt.get("tags")
        if isinstance(tags, dict):
            for key in [k for k in tags if str(k).lower() == "title"]:
                del tags[key]
    return out


def fidelity(streams, container):
    """Whether a file was untouched or already a re-encode, with what says so."""
    v = next((x for x in streams if x["type"] == "video" and not x["dispositions"].get("attachedPic")), None)
    evidence = []
    if v and v.get("encoder"):
        evidence.append({"signal": "encoder-tag", "value": v["encoder"], "weight": 0.9})
    for key in ("writingApp", "muxingApp"):
        if container.get(key) and RE_ENCODE.search(container[key]):
            evidence.append({"signal": "container-title", "value": container[key], "weight": 0.5,
                             "note": key})
    for a in [x for x in streams if x["type"] == "audio"]:
        if (a.get("titleClaim") or {}).get("contradictsActual"):
            evidence.append({"signal": "codec-vs-title", "value": a.get("title"), "weight": 0.8})
    fingerprint = None
    if v:
        hdr = (v.get("hdr") or {}).get("format") or "sdr"
        fingerprint = "/".join(str(x) for x in [v.get("codec") or "?", (v.get("profile") or "?").lower(),
                                                f"{v.get('bitDepth') or '?'}bit", hdr,
                                                f"{v.get('width') or '?'}x{v.get('height') or '?'}"])
    return {"class": "derivative" if evidence else "unknown", "fingerprint": fingerprint, "evidence": evidence}


def probed(probe):
    """What a probe found in a file, in the terms a source record and an extra share: its container,
    every stream, whether it was already a re-encode, and its essence. The container's title tag is
    not among them, in any case: it routinely names where the file came from."""
    fmt = probe.get("format") or {}
    tags = {k: text(v) for k, v in (fmt.get("tags") or {}).items() if text(v) and str(k).lower() != "title"}
    streams = [norm_stream(s) for s in probe.get("streams") or []]
    infer_forced_by_size(streams)
    duration = fmt.get("duration")
    container = {"format": fmt.get("format_name") or "",
                 "durationMs": int(float(duration) * 1000) if duration else None,
                 "bitrate": num(fmt.get("bit_rate")), "title": None,
                 "muxingApp": tags.get("muxing_application") or tags.get("encoder"),
                 "writingApp": tags.get("writing_application"),
                 "creationTime": tags.get("creation_time"), "tags": tags}
    return {"container": container, "streams": streams, "fidelity": fidelity(streams, container),
            "essence": source_essence(streams, probe.get("chapters") or [])}


def probe_chapters(probe):
    """The chapter marks a probe found in the file itself, on its timeline."""
    return [{"startMs": int(float(c.get("start_time", 0)) * 1000), "endMs": int(float(c.get("end_time", 0)) * 1000),
             "title": text((c.get("tags") or {}).get("title"))} for c in (probe or {}).get("chapters") or []]


# ---------------------------------------------------------------- the catalog's own words
def external_ids(entries, item_type):
    """The reference ids of an item from the catalog's [{source, externalId}], keyed as item.json and
    metadata.json key them. Returns (ids, notes): a source the format has no field for, and a TMDB id
    that is not a number, are dropped with a note, never renamed or guessed."""
    ids, notes = {}, []
    for e in entries or []:
        src, val = (e.get("source") or "").lower(), text(e.get("externalId"))
        if not val:
            continue
        if src in ("tmdb", "themoviedb"):
            ids["tmdbMovie" if item_type == "movie" else "tmdbTv"] = val
        elif src in ("tmdb-episode", "tmdbepisode"):
            if item_type == "episode":
                ids["tmdbEpisode"] = val
        elif src in ("tmdb-season", "tmdbseason"):
            if item_type != "movie":
                ids["tmdbSeason"] = val
        elif src == "imdb" and re.fullmatch(r"tt[0-9]+", val):
            ids["imdb"] = val
        elif src == "tvdb" and re.fullmatch(r"[0-9]+", val):
            ids["tvdb"] = val
        else:
            notes.append(f"external id source {src!r} has no v2 field, dropped")
    for key in ("tmdbMovie", "tmdbTv", "tmdbSeason", "tmdbEpisode"):
        if key in ids and not re.fullmatch(r"[0-9]+", ids[key]):
            notes.append(f"{key} {ids[key]!r} is not a number, dropped")
            del ids[key]
    return ids, notes


def chapter_marks(rows):
    """The catalog's chapter rows ({startMs, endMs, title, ordinal}) as marks on the version's
    timeline, in their order; a row without a start is no mark."""
    chapters = [c for c in (rows or []) if c.get("startMs") is not None]
    return [{"startMs": int(c["startMs"]), "endMs": int(c.get("endMs") or c["startMs"]), "title": text(c.get("title"))}
            for c in sorted(chapters, key=lambda c: c.get("ordinal") or 0)]


def segments(rows):
    """The catalog's detected ranges ({kind, startMs, endMs, source, confidence, label}) as a version
    keeps them: the detector that found each, and a kind the format does not know as 'other', labelled
    with the catalog's own kind."""
    out = []
    for s in rows or []:
        kind = (s.get("kind") or "other").lower()
        known = kind in SEGMENT_KINDS
        out.append({"kind": kind if known else "other", "startMs": int(s["startMs"]),
                    "endMs": int(s.get("endMs") or s["startMs"]), "detector": text(s.get("source")),
                    "confidence": s["confidence"] if isinstance(s.get("confidence"), (int, float)) else None,
                    "label": text(s.get("label")) if known else (text(kind) or text(s.get("label")))})
    return out


# ---------------------------------------------------------------- sources/<sourceId>/
def sidecar_language(raw):
    """A sidecar's language as the catalog's row has it, when the format can hold that code, and its
    BCP 47 code otherwise; None when the row names none."""
    r = str(raw or "").strip().lower()
    if not r:
        return None
    return r if LANGUAGE_RE.match(r) else lang(r)[0]


def sidecar_entry(source_id, n, path, arrival_name, language=None, forced=False):
    """One sidecars[] entry of a source record: the copy of the nth subtitle file that came with the
    original (n from 1, in the order the catalog lists them), kept as sources/<sourceId>/<the name
    subtitle_copy_name() gives it> — path is that copy, or the file it is taken from, the same bytes —
    with its size and hash, its language as the catalog's row has it, whether it is forced or for the
    hard of hearing, as the name it arrived under or the catalog says (".forced." / ".sdh.", ".cc."
    in the name), and so what it is for. That name is read for what it says, and kept nowhere: the
    entry's file is where the copy goes."""
    low = arrival_name.lower()
    forced = bool(forced) or ".forced." in low
    hearing = bool(re.search(r"\.(sdh|cc)\.", low))
    ext = os.path.splitext(arrival_name)[1]
    return {"file": f"sources/{source_id}/{subtitle_copy_name(n, language, forced, hearing, ext)}",
            "kind": "subtitle", "format": ext.lower().lstrip(".") or None, "language": sidecar_language(language),
            "forced": forced, "hearingImpaired": hearing, "purpose": "forced" if forced else "sdh" if hearing else "dialogue",
            "sizeBytes": os.path.getsize(path), "sha256": sha_file(path)}


def source_record(source_id, name, size_bytes, *, taken_at, taken_by, library_path, qh1=None, mtime=None,
                  probe=None, probe_version=None, sidecars=(), note=None, covers=()):
    """sources/<sourceId>/source.json for one original, and the bytes of the ffprobe.json written
    beside it (None without a probe). Returns (record, probe bytes).

    name is the name the original arrived under, and library_path where it sat, relative to the
    arrivals it came through: the record reads what they claim — its labels, its numbering — and
    keeps neither, nor where the file came from; file.name is the name the library gives it,
    library_original_name(name). probe is the tool's output (ffprobe()), whose facts — container,
    streams, fidelity, essence — the record carries, and which is kept beside it with the two edits
    scrub_probe() makes. Without one the record carries the file's size and fixity alone, and
    probe.note says why. Without qh1 — an original already gone, so nothing could fingerprint it —
    the record has no fixity, which only a note that says why may excuse.

    covers, for a file that holds several episodes, is the item ids of the episodes it holds in
    episode order, the holder first — the episode whose folder keeps this source — as the database
    linked them; empty, as it is by default, for a file of one episode. It is written as given: the
    record logic does not read it from the name, whose numbering is only what the release claimed."""
    if qh1 is None and not text(note):
        raise ValueError("a source record without the original's fixity needs a note that says why it has none")
    covers = [str(c) for c in covers or ()]
    if covers and (len(set(covers)) != len(covers) or not all(UUID_RE.match(c) for c in covers)):
        raise ValueError(f"a file covers episodes by their item ids, each once: {covers!r}")
    folder = os.path.dirname(library_path.replace("\\", "/"))
    file = {"name": library_original_name(name), "kind": "stream-container", "sizeBytes": size_bytes}
    if mtime:
        file["mtime"] = mtime
    if qh1:
        file["fixity"] = {"qh1": qh1}
    rec = {"schema": "zaentrum.library.source/2", "sourceId": source_id, "takenAt": taken_at, "takenBy": taken_by,
           "file": file}
    named = naming(name)
    if named:
        rec["naming"] = named
    rec["labels"] = labels(name, folder)
    raw = None
    if probe:
        facts = probed(probe)
        raw = json_bytes(scrub_probe(probe, file["name"]))
        rec.update(container=facts["container"], fidelity=facts["fidelity"], streams=facts["streams"],
                   sidecars=list(sidecars), covers=covers, essence=facts["essence"],
                   probe={"tool": "ffprobe", "version": probe_version, "at": taken_at,
                          "file": f"sources/{source_id}/ffprobe.json", "sha256": sha_bytes(raw), "note": text(note)})
    else:
        rec.update(container={"format": "", "durationMs": None, "bitrate": None, "title": None, "muxingApp": None,
                              "writingApp": None, "creationTime": None, "tags": {}},
                   fidelity={"class": "unknown", "fingerprint": None, "evidence": []}, streams=[],
                   sidecars=list(sidecars), covers=covers, essence={},
                   probe={"tool": "ffprobe", "version": None, "at": None, "file": None, "sha256": None,
                          "note": text(note) or "ffprobe was not available where this record was written; the "
                                                "original's streams, fidelity and essence are unknown"})
    return rec, raw


# ---------------------------------------------------------------- versions/<versionId>/version.json
def version_record(version_id, source, *, created_at, created_by, chapters=(), chapters_from=None, segments=(),
                   original_files=()):
    """versions/<versionId>/version.json for a version made from source, a source record: its edition as
    the name the file arrived under and its folder claimed — the source's labels say what they did —
    its presentation and runtime as the probe found them, the marks it keeps, and the source it was
    made from. original_files names the originals the version keeps in its folder: the source's
    file.name when the version is established with its original, which goes in beside the package, or
    taken in before anything packaged it; none for a re-package, whose original stays in the version
    that holds it."""
    streams = source.get("streams") or []
    v0 = next((x for x in streams if x["type"] == "video" and not x["dispositions"].get("attachedPic")), None)
    claims = source.get("labels") or {}
    folder_edition = claims.get("folderEdition")
    # what the name the file arrived under claimed, read when its source was recorded; a source record
    # from before 2026-10-08 still names the file as it arrived, and that name claims the same
    named = edition_word(claims.get("edition")) or edition_word(source["file"]["name"])
    kind = named or edition_word(folder_edition)
    evidence = [{"signal": "filename", "value": named, "weight": 0.6}] if named else \
        [{"signal": "folder-name", "value": text(folder_edition), "weight": 0.6}] if kind else []
    edition = {"kind": kind or "unknown", "label": text(folder_edition), "decidedBy": "inferred",
               "decidedAt": created_at, "evidence": evidence}
    dynamic = (v0.get("hdr") or {}).get("format") if v0 else None
    three_d = (v0 or {}).get("stereo3d")
    presentation = {
        "colour": "unknown",
        "dynamicRange": dynamic if dynamic in ("sdr", "hdr10", "hdr10plus", "hlg", "dolby-vision") else "unknown",
        "stereo3d": "none" if v0 is not None and not three_d else
                    {"side_by_side": "side-by-side", "sbs": "side-by-side", "top_and_bottom": "top-and-bottom",
                     "tb": "top-and-bottom", "mvc": "mvc"}.get(three_d or "", "unknown"),
        "aspectRatio": (v0 or {}).get("displayAspectRatio"),
    }
    marks = list(chapters)
    return {"schema": "zaentrum.library.version/2", "versionId": version_id, "createdAt": created_at,
            "createdBy": created_by, "edition": edition, "presentation": presentation,
            "runtimeMs": (source.get("container") or {}).get("durationMs"), "chapters": marks,
            "chaptersFrom": chapters_from if marks else None, "segments": list(segments),
            "sourceIds": [source["sourceId"]], "originalFiles": list(original_files)}


# ---------------------------------------------------------------- package.json
def video_rendition(v):
    out = {"id": text(v.get("id")) or "v0", "dir": v.get("dir") or "hls/v0", "codec": v.get("codec") or "",
           "width": num(v.get("width")) or 0, "height": num(v.get("height")) or 0,
           "bitrateBps": num(v.get("bitrateBps")) or 0, "hdr": bool(v.get("hdr")),
           "frameRate": str(v.get("frameRate") or ""), "segments": num(v.get("segments")) or 0,
           "targetDuration": num(v.get("targetDuration")) or 0}
    if v.get("dynamicRange"):
        out["dynamicRange"] = v["dynamicRange"]
    if v.get("sourceStreamIndex") is not None:
        out["sourceStreamIndex"] = num(v["sourceStreamIndex"])
    if num(v.get("peakBitrateBps")) is not None and num(v["peakBitrateBps"]) >= 0:
        out["peakBitrateBps"] = num(v["peakBitrateBps"])
    if v.get("videoRange") in VIDEO_RANGES:
        out["videoRange"] = v["videoRange"]
    for key in ("label", "encoder"):
        if text(v.get(key)):
            out[key] = text(v[key])
    return out


def audio_rendition(a, i):
    title = text(a.get("title")) or ""
    purpose, purpose_from = audio_purpose({"disposition": {}, "tags": {"title": title}})
    out = {"id": text(a.get("id")) or f"a{i}", "dir": a.get("dir") or f"hls/a{i}", "codec": a.get("codec") or "",
           "language": rendition_language(a.get("language")), "title": title, "default": bool(a.get("default")),
           "channels": num(a.get("channels")) or 0, "bitrateBps": num(a.get("bitrateBps")) or 0,
           "segments": num(a.get("segments")) or 0, "visible": bool(a.get("visible", True)),
           "purpose": purpose, "purposeFrom": purpose_from, "variant": title_variant(title)}
    for key in ("sourceStreamIndex", "sourceChannels"):
        if a.get(key) is not None:
            out[key] = num(a[key])
    if a.get("original") is not None:
        out["original"] = bool(a["original"])
    for key in ("group", "name"):
        if text(a.get(key)):
            out[key] = text(a[key])
    return out


def subtitle_rendition(s, i, from_sidecar=None):
    title = text(s.get("title")) or ""
    purpose, purpose_from = subtitle_purpose({"disposition": {"forced": bool(s.get("forced"))},
                                              "tags": {"title": title}})
    out = {"id": text(s.get("id")) or f"sub{i}", "path": s.get("path") or f"subs/{i}.vtt",
           "language": rendition_language(s.get("language")), "title": title, "default": bool(s.get("default")),
           "forced": bool(s.get("forced")), "format": s.get("format") or "webvtt",
           "visible": bool(s.get("visible", True)), "purpose": purpose, "purposeFrom": purpose_from,
           "variant": title_variant(title)}
    if s.get("sourceStreamIndex") is not None:
        out["sourceStreamIndex"] = num(s["sourceStreamIndex"])
    if text(s.get("name")):
        out["name"] = text(s["name"])
    if text(s.get("hls")):
        out["hls"] = text(s["hls"])
    if from_sidecar:
        out["fromSidecar"] = from_sidecar
    return out


def trickplay(tp):
    if not tp:
        return None
    return {"vttPath": tp.get("vttPath") or "trickplay/thumbnails.vtt",
            "spritePattern": tp.get("spritePattern") or "trickplay/sprite-%04d.jpg",
            "intervalSec": num(tp.get("intervalSec")) or 10, "thumbWidth": num(tp.get("thumbWidth")) or 0,
            "thumbHeight": num(tp.get("thumbHeight")) or 0, "gridCols": num(tp.get("gridCols")) or 1,
            "gridRows": num(tp.get("gridRows")) or 1}


def hls_block(h):
    """The package's HLS layout as the packager wrote it: the master playlist, the segment length,
    the audio groups and the subtitle group, or None for a package that does not say."""
    if not isinstance(h, dict) or not text(h.get("master")) or not num(h.get("segmentSeconds")) \
            or num(h["segmentSeconds"]) < 1:
        return None
    return {"master": text(h["master"]), "segmentSeconds": num(h["segmentSeconds"]),
            "audioGroups": [g for g in (text(x) for x in h.get("audioGroups") or []) if g],
            "subtitleGroup": text(h.get("subtitleGroup"))}


def peak_bandwidth(folder, master="hls/master.m3u8"):
    """The highest BANDWIDTH the package's master playlist advertises, or None without one."""
    p = os.path.join(folder, master)
    if os.path.isfile(p):
        with open(p, encoding="utf-8", errors="replace") as f:
            peaks = [int(m) for m in re.findall(r"BANDWIDTH=(\d+)", f.read())]
        if peaks:
            return max(peaks)
    return None


def package_files(folder, dirs=PACKAGE_DIRS):
    """Every file of the package in folder, as [(path relative to folder, hex sha256, size)]: the
    rendition, subtitle, trickplay and trailer folders, never the records beside them."""
    out = []
    for sub in dirs:
        base = os.path.join(folder, sub)
        if not os.path.isdir(base):
            continue
        for rel in walk_files(base):
            p = os.path.join(base, rel)
            out.append((os.path.join(sub, rel).replace(os.sep, "/"), sha_file(p).split(":", 1)[1], os.path.getsize(p)))
    return sorted(out)


def record_entry(name, data):
    """A record about to be written, as checksums() lists it: (name, hex sha256, size)."""
    return name, hashlib.sha256(data).hexdigest(), len(data)


def checksums(listed):
    """checksums.sha256 over listed — [(relative path, hex sha256, size)] — and the object a package
    record keeps of it: '<hex>  <path>' lines, sorted by path. Returns (bytes, object)."""
    listed = sorted(listed)
    data = "".join(f"{digest}  {rel}\n" for rel, digest, _ in listed).encode("utf-8")
    return data, {"file": SUMS, "algorithm": "sha256", "sha256": sha_bytes(data), "files": len(listed),
                  "bytes": sum(size for _, _, size in listed)}


def complete(package_bytes):
    """The .complete marker, written last: sha256:<hex> of package.json and a line break."""
    return (sha_bytes(package_bytes) + "\n").encode("utf-8")


def package_record(package_id, manifest, listed, *, source=None, role="canonical", created_at=None,
                   packaged_by=None, duration_ms=None, peak_bandwidth_bps=None, sidecars=None):
    """package.json for the package a packager manifest describes — the manifest the packager builds,
    or one a legacy package folder holds — and listed, every file the checksums written below it list
    ([(path, hex sha256, size)], the record they cover among them, the package files under hls/ subs/
    trickplay/ trailers/ the rest). Returns (record, notes).

    source is the record the package was made from — a source record, or an extra record that was
    probed — whose streams and essence say what the package failed to carry; without them that is said
    to be unmeasured. role is derived while the folder keeps the original beside the package, and
    canonical when it keeps none. sidecars maps a subtitle rendition's id to the copy of the subtitle
    file it was made from, as its source's sidecars[] names it (sources/<sourceId>/subtitle-<n>…),
    relative to the item folder. The playlist flags a reader could
    not follow are normalised, each with a note: a second default audio rendition is not one, a
    package with none has its first, a forced subtitle is never the default, and of the rest only
    the first default is one."""
    notes = []
    sidecars = sidecars or {}
    ren = manifest.get("renditions") or {}
    video = [video_rendition(v) for v in ren.get("video") or []]
    audio = [audio_rendition(a, i) for i, a in enumerate(ren.get("audio") or [])]
    surround = [audio_rendition(a, len(audio) + i) for i, a in enumerate(ren.get("audioSurround") or [])]
    subs = []
    for i, s in enumerate(manifest.get("subtitles") or []):
        sid = text(s.get("id")) or f"sub{i}"
        subs.append(subtitle_rendition(s, i, sidecars.get(sid)))
    defaults = [a for a in audio if a["default"]]
    for a in defaults[1:]:
        a["default"] = False
        notes.append(f"audio rendition {a['id']} was a second default in the package manifest; cleared")
    if audio and not defaults:
        audio[0]["default"] = True
        notes.append("no audio rendition was default in the package manifest; the first one is")
    for a in [a for a in surround if a["default"]][1:]:
        a["default"] = False
        notes.append(f"surround rendition {a['id']} was a second default in the package manifest; cleared")
    for s in subs:
        if s["default"] and (s["forced"] or s["purpose"] in ("forced", "signs-songs")):
            s["default"] = False
            notes.append(f"subtitle {s['id']} was a forced track flagged default; cleared")
    for s in [s for s in subs if s["default"] and not s["forced"]][1:]:
        s["default"] = False
        notes.append(f"subtitle {s['id']} was a second default; cleared")
    renditions = {"video": video, "audio": audio}
    if surround:
        renditions["audioSurround"] = surround
    pkg_essence = package_essence({"renditions": renditions, "subtitles": subs}, (source or {}).get("essence"))
    streams = (source or {}).get("streams") or []
    if streams:
        losses = losses_against(source.get("essence") or {}, pkg_essence, streams, renditions, subs)
    else:
        losses = [{"kind": "other", "detail": "the original was not probed, so what the package failed to "
                                              "carry was never measured", "sourceStreamIndex": None}]
    data, sums = checksums(listed)
    doc = {"schema": "zaentrum.library.package/2", "packageId": package_id,
           "createdAt": ts(manifest.get("packagedAt")) or created_at,
           "packagedBy": text(manifest.get("packager")) or packaged_by or "unknown", "state": "complete",
           "role": role, "durationMs": num(manifest.get("durationMs")) or num(duration_ms) or 0,
           "renditions": renditions, "subtitles": subs, "trickplay": trickplay(manifest.get("trickplay"))}
    hls = hls_block(manifest.get("hls"))
    if hls:
        doc["hls"] = hls
    doc.update(trailers=[],
               sizeBytes=sum(size for rel, _, size in listed if rel.split("/", 1)[0] in PACKAGE_DIRS),
               peakBandwidthBps=peak_bandwidth_bps,
               fidelity={"lossless": not losses, "losses": losses, "droppedSourceStreams": []},
               essence=pkg_essence, checksums=sums)
    return doc, notes


# ---------------------------------------------------------------- extras/<extraId>/extra.json
def extra_record(extra_id, *, created_at, created_by, kind, title, localized_titles=None, language=None,
                 season_number=None, origin=None, probe=None, probe_version=None, probed_at=None,
                 original_files=(), originals=(), packaged_from=()):
    """extras/<extraId>/extra.json: what the extra is, as the catalog took it in, and what a probe of
    its original found. An extra of the platform keeps no original — it is deleted once the package is
    recorded — so original_files and originals stay empty, and packaged_from describes the original the
    package was made from: [{name, sizeBytes, fixity: {qh1}}], each named here as the library names an
    original, library_original_name() of the name it arrived under — the nth of several original-<n> —
    whatever name it is given. Without a language, the language spoken in its first audio track is the
    extra's."""
    doc = {"schema": "zaentrum.library.extra/2", "extraId": extra_id, "createdAt": created_at,
           "createdBy": created_by, "kind": kind, "title": title, "localizedTitles": dict(localized_titles or {}),
           "language": language, "runtimeMs": None}
    if season_number is not None:
        doc["seasonNumber"] = season_number
    doc.update(originalFiles=list(original_files), originals=list(originals))
    if packaged_from:
        parts = len(packaged_from) > 1
        doc["packagedFrom"] = [dict(o, name=library_original_name(o.get("name"), i + 1 if parts else None))
                               for i, o in enumerate(packaged_from)]
    if origin:
        doc["origin"] = origin
    if probe:
        facts = probed(probe)
        spoken = [x["language"] for x in facts["streams"] if x["type"] == "audio" and x.get("language")]
        doc.update(language=language or (spoken[0] if spoken else None), runtimeMs=facts["container"]["durationMs"],
                   **facts, probe={"tool": "ffprobe", "version": probe_version, "at": probed_at or created_at,
                                   "note": None})
    return doc


# ---------------------------------------------------------------- reading a chain back
def read_sums(path):
    """A sha256sum file as ({path: hex digest}, [lines that are not '<64 hex>  <path>'])."""
    entries, bad = {}, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = re.match(r"^([0-9a-f]{64})  (.+)$", line)
            if m:
                entries[m.group(2)] = m.group(1)
            elif line:
                bad.append(line)
    return entries, bad


def chain_problems(folder, record="version.json", dirs=PACKAGE_DIRS, kept=(), full=False):
    """What is wrong with the chain of a version or an extra folder: .complete names package.json,
    package.json names checksums.sha256, which lists exactly the record, the originals kept beside it
    (kept) and every package file, of the count and total size package.json records, and the
    record's digest is the one listed. With full, every listed file is hashed too. An empty list is a
    chain that holds."""
    out = []
    mark, pkg_path, sums_path = (os.path.join(folder, n) for n in (".complete", "package.json", SUMS))
    if not os.path.isfile(mark) or not os.path.isfile(pkg_path):
        return ["no package.json and .complete: the package never finished"]
    with open(mark, "rb") as f:
        head = f.read().decode("utf-8", "replace").strip()
    if head != sha_file(pkg_path):
        out.append(".complete does not name this package.json")
    try:
        with open(pkg_path, encoding="utf-8") as f:
            cs = (json.load(f) or {}).get("checksums") or {}
    except (OSError, ValueError) as e:
        return out + [f"package.json cannot be read: {e}"]
    if not os.path.isfile(sums_path):
        return out + [f"{SUMS} is missing"]
    if sha_file(sums_path) != cs.get("sha256"):
        out.append(f"{SUMS} is not the one package.json names")
    listed, bad = read_sums(sums_path)
    out += [f"{SUMS} holds a line that is not one: {line[:80]!r}" for line in bad[:3]]
    present = sorted({record} | {n for n in kept if os.path.isfile(os.path.join(folder, n))}
                     | {rel for rel, _, _ in _listing(folder, dirs)})
    for rel in sorted(set(present) - set(listed)):
        out.append(f"{rel} is not listed in {SUMS}")
    for rel in sorted(set(listed) - set(present)):
        out.append(f"{SUMS} lists {rel}, which is not here")
    if len(listed) != cs.get("files"):
        out.append(f"{SUMS} lists {len(listed)} files, package.json says {cs.get('files')}")
    total = sum(os.path.getsize(os.path.join(folder, rel)) for rel in listed
                if os.path.isfile(os.path.join(folder, rel)))
    if total != cs.get("bytes"):
        out.append(f"the files {SUMS} lists total {total} bytes, package.json says {cs.get('bytes')}")
    rp = os.path.join(folder, record)
    if os.path.isfile(rp) and sha_file(rp).split(":", 1)[1] != listed.get(record):
        out.append(f"{record} does not match the digest {SUMS} lists for it")
    if full:
        for rel in sorted(set(listed) & set(present) - {record}):
            if sha_file(os.path.join(folder, rel)).split(":", 1)[1] != listed[rel]:
                out.append(f"{rel} does not match its checksum")
    return out


def _listing(folder, dirs):
    """The package files under folder, by path, with nothing hashed."""
    out = []
    for sub in dirs:
        base = os.path.join(folder, sub)
        if os.path.isdir(base):
            out += [(os.path.join(sub, rel).replace(os.sep, "/"), None, None) for rel in walk_files(base)]
    return out
