"""Fonts for the tests, made here byte by byte: no font of anyone else's is
in this repository. They are the repository's code and output, under its
licence (MPL 2.0).

truetype() is a whole TrueType font: every table a renderer needs (cmap,
glyf, head, hhea, hmtx, loca, maxp, name, OS/2, post), the printable ASCII
characters each mapped to one glyph — a filled box, the space empty — and
the names given in its name table, on the Windows platform (and, with
mac_names, on the Macintosh platform alone). FreeType reads it, so libass
draws a styled subtitle with it. collection() puts several in one TrueType
collection (.ttc). opentype_header() is an OpenType font's first bytes and
a name table, and no outlines: a file that says it is an OTF, for what
reads only that."""

from __future__ import annotations

import struct

_FIRST, _LAST = 0x20, 0x7E


def _checksum(data: bytes) -> int:
    data += b"\0" * (-len(data) % 4)
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def name_table(names: dict[int, str], *, mac: bool = False) -> bytes:
    """A name table (format 0) of names by name id: on the Windows platform
    in UTF-16BE (US English), or with mac on the Macintosh one in Mac Roman."""
    records, strings = [], b""
    for name_id in sorted(names):
        raw = names[name_id].encode("mac_roman" if mac else "utf-16-be")
        platform, encoding, language = (1, 0, 0) if mac else (3, 1, 0x0409)
        records.append(struct.pack(">6H", platform, encoding, language, name_id, len(raw),
                                   len(strings)))
        strings += raw
    return struct.pack(">3H", 0, len(records), 6 + 12 * len(records)) + b"".join(records) + strings


def _names(family: str, style: str) -> dict[int, str]:
    return {1: family, 2: style, 3: f"{family} {style} 1.0", 4: f"{family} {style}",
            5: "Version 1.0", 6: f"{family}-{style}".replace(" ", "")}


def _tables(family: str, style: str, mac_names: bool) -> dict[bytes, bytes]:
    glyphs = 2 + (_LAST - _FIRST)  # .notdef, the space, then one box per character
    box = (struct.pack(">5h", 1, 100, 0, 600, 700) + struct.pack(">2H", 3, 0) + bytes([1] * 4)
           + struct.pack(">4h", 100, 0, 500, 0) + struct.pack(">4h", 0, 700, 0, -700))
    box += b"\0" * (-len(box) % 4)
    loca = struct.pack(f">{glyphs + 1}I", 0, 0, *(len(box) * i for i in range(glyphs - 1)))
    hmtx = struct.pack(">Hh", 500, 0) + struct.pack(">Hh", 300, 0) \
        + struct.pack(">Hh", 700, 100) * (glyphs - 2)
    cmap = struct.pack(">2H", 0, 1) + struct.pack(">2HI", 3, 1, 12) \
        + struct.pack(">7H", 4, 32, 0, 4, 4, 1, 0) + struct.pack(">2H", _LAST, 0xFFFF) \
        + struct.pack(">H", 0) + struct.pack(">2H", _FIRST, 0xFFFF) \
        + struct.pack(">2h", 1 - _FIRST, 1) + struct.pack(">2H", 0, 0)
    head = struct.pack(">4I2H2q4h2Hh2h", 0x00010000, 0x00010000, 0, 0x5F0F3CF5, 0x000B, 1000,
                       0, 0, 0, 0, 600, 700, 0, 8, 2, 1, 0)
    hhea = struct.pack(">I3hH3h3h4hhH", 0x00010000, 800, -200, 0, 700, 0, 0, 600, 1, 0, 0,
                       0, 0, 0, 0, 0, glyphs)
    maxp = struct.pack(">I14H", 0x00010000, glyphs, 4, 1, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0)
    os2 = struct.pack(">Hh2Hh10hh10s4I4s3H3h2H2I2h3H", 4, 600, 400, 5, 0,
                      650, 600, 0, 75, 650, 600, 0, 350, 50, 250, 0, bytes(10), 1, 0, 0, 0,
                      b"NONE", 0x40, _FIRST, _LAST, 800, -200, 0, 800, 200, 1, 0, 500, 700, 0,
                      32, 1)
    post = struct.pack(">Ii2h5I", 0x00030000, 0, -100, 50, 0, 0, 0, 0, 0)
    return {b"OS/2": os2, b"cmap": cmap, b"glyf": box * (glyphs - 2), b"head": head,
            b"hhea": hhea, b"hmtx": hmtx, b"loca": loca, b"maxp": maxp,
            b"name": name_table(_names(family, style), mac=mac_names), b"post": post}


def _directory(version: int, tables: dict[bytes, bytes], offsets: dict[bytes, int]) -> bytes:
    tags = sorted(tables)
    power = 1 << (len(tags).bit_length() - 1)
    out = struct.pack(">I4H", version, len(tags), power * 16, power.bit_length() - 1,
                      len(tags) * 16 - power * 16)
    for tag in tags:
        out += struct.pack(">4s3I", tag, _checksum(tables[tag]), offsets[tag], len(tables[tag]))
    return out


def _laid_out(tables: dict[bytes, bytes], start: int) -> tuple[dict[bytes, int], bytes]:
    offsets, body = {}, b""
    for tag in sorted(tables):
        offsets[tag] = start + len(body)
        body += tables[tag] + b"\0" * (-len(tables[tag]) % 4)
    return offsets, body


def truetype(family: str, style: str = "Regular", *, mac_names: bool = False) -> bytes:
    """A TrueType font of that family and style (module doc)."""
    tables = _tables(family, style, mac_names)
    offsets, body = _laid_out(tables, 12 + 16 * len(tables))
    font = _directory(0x00010000, tables, offsets) + body
    at = offsets[b"head"] + 8  # checkSumAdjustment: the whole font sums to 0xB1B0AFBA
    adjustment = (0xB1B0AFBA - _checksum(font)) & 0xFFFFFFFF
    return font[:at] + struct.pack(">I", adjustment) + font[at + 4:]


def collection(*faces: tuple[str, str]) -> bytes:
    """A TrueType collection of fonts of those (family, style), its faces'
    tables laid out after their directories."""
    each = [_tables(family, style, False) for family, style in faces]
    header = 12 + 4 * len(each)
    directories = [12 + 16 * len(t) for t in each]
    start = header + sum(directories)
    out_dirs, bodies, at = [], b"", start
    for tables in each:
        offsets, body = _laid_out(tables, at)
        out_dirs.append(_directory(0x00010000, tables, offsets))
        bodies += body
        at += len(body)
    starts, pos = [], header
    for d in out_dirs:
        starts.append(pos)
        pos += len(d)
    return (b"ttcf" + struct.pack(">HHI", 1, 0, len(each)) + struct.pack(f">{len(each)}I", *starts)
            + b"".join(out_dirs) + bodies)


def opentype_header(family: str, style: str = "Regular") -> bytes:
    """An OpenType (CFF) font's first bytes and its name table, nothing else."""
    tables = {b"name": name_table(_names(family, style))}
    offsets, body = _laid_out(tables, 12 + 16)
    return _directory(0x4F54544F, tables, offsets) + body
