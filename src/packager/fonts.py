"""What a font file is, and the names it answers to: read from its bytes,
for the font attachments a package takes out of an original
(packager._extract_fonts).

A styled subtitle (ASS/SSA) names its fonts in its styles and override
tags, and a player that draws it (libass, JASSUB in a browser) loads the
font a name asks for. libass matches the name against each font's family
and full names on the Windows platform of its name table, then its
PostScript name, and takes the family name FreeType gives where a font has
no Windows family name; a player that loads fonts as their names are asked
for needs the same names. font_names reads them from TrueType and OpenType
fonts and from collections of either; WOFF and WOFF2 are named by nothing
here (their tables are compressed), and their bytes are kept as they are.
Nothing here writes or changes a font, and bytes that are no font give no
format and no names, never an error."""

from __future__ import annotations

import struct

# What a font file is by its first four bytes: its sfnt version, a
# collection's tag, or WOFF's signature.
_MAGIC = {
    b"\x00\x01\x00\x00": "ttf",
    b"true": "ttf",
    b"OTTO": "otf",
    b"ttcf": "ttc",
    b"wOFF": "woff",
    b"wOF2": "woff2",
}

# The name table's names a script asks for a font by: its family (1), its
# full name (4) and its PostScript name (6).
_FAMILY, _FULL, _POSTSCRIPT = 1, 4, 6
_WINDOWS, _MACINTOSH, _UNICODE = 3, 1, 0

# The most faces of a collection, tables of a face and records of a name
# table read: far above any real font's, so bytes that only look like one
# can't make the reading long.
_MAX_FACES = 256
_MAX_TABLES = 512


def font_format(data: bytes) -> str | None:
    """The format a font file's bytes are: ttf, otf, ttc, woff or woff2;
    None for bytes that are none of them."""
    return _MAGIC.get(bytes(data[:4]))


def font_names(data: bytes) -> list[str]:
    """The names a script can ask for the font by, each once, in this order:
    the family names, then the full names, of the Windows platform of its
    name table, in the order the table lists them — the family name of
    the Macintosh or the Unicode platform where it has no Windows one, as
    FreeType falls back to it — then its PostScript name. For a collection,
    every face's, face by face. [] for WOFF, WOFF2 and bytes that are no
    font, or whose name table can't be read."""
    fmt = font_format(data)
    if fmt in ("ttf", "otf"):
        faces = [0]
    elif fmt == "ttc":
        try:
            count = struct.unpack_from(">I", data, 8)[0]
            faces = list(struct.unpack_from(f">{min(count, _MAX_FACES)}I", data, 12))
        except struct.error:
            return []
    else:
        return []
    out: dict[str, None] = {}
    for offset in faces:
        for name in _face_names(data, offset):
            out.setdefault(name, None)
    return list(out)


def _face_names(data: bytes, offset: int) -> list[str]:
    """The names of the face whose offset table is at offset (font_names)."""
    try:
        tables = struct.unpack_from(">H", data, offset + 4)[0]
        name_at = None
        for i in range(min(tables, _MAX_TABLES)):
            tag, _, at, length = struct.unpack_from(">4sIII", data, offset + 12 + 16 * i)
            if tag == b"name":
                name_at = (at, length)
                break
        if name_at is None:
            return []
        at, length = name_at
        table = data[at:at + length]
        _, count, strings = struct.unpack_from(">HHH", table, 0)
        records = [struct.unpack_from(">6H", table, 6 + 12 * i) for i in range(count)]
    except struct.error:
        return []

    def named(name_id: int, platforms: tuple[int, ...]) -> list[str]:
        found = []
        for platform, encoding, _, nid, size, start in records:
            if nid != name_id or platform not in platforms:
                continue
            raw = table[strings + start:strings + start + size]
            if platform == _MACINTOSH and encoding != 0:
                continue  # only Mac Roman is read: another script's encoding is no text here
            codec = "mac_roman" if platform == _MACINTOSH else "utf-16-be"
            name = " ".join(raw.decode(codec, errors="replace").replace("\x00", "").split())
            if name:
                found.append(name)
        return found

    families = named(_FAMILY, (_WINDOWS,)) or named(_FAMILY, (_MACINTOSH, _UNICODE))[:1]
    postscript = (named(_POSTSCRIPT, (_WINDOWS,)) or named(_POSTSCRIPT, (_MACINTOSH, _UNICODE)))[:1]
    return [*families, *named(_FULL, (_WINDOWS,)), *postscript]
