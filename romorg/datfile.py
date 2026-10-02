"""Parse DAT files into a DatFile.

Two formats are supported: Logiqx XML (TOSEC, DAT-o-MATIC) and the clrmamepro
text format (libretro's No-Intro mirror). :func:`parse_dat` picks the parser.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from .tags import Tags

FORMAT_LOGIQX = "logiqx"
FORMAT_CLRMAMEPRO = "clrmamepro"

_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def strip_ext(name: str) -> str:
    """``"X (USA).nes"`` -> ``"X (USA)"`` (only a short alphanumeric last extension is removed)."""
    return _EXT_RE.sub("", name)


@dataclass(frozen=True)
class Rom:
    name: str
    size: int
    crc: str   # lowercase hex, zero-padded to 8 ("" if absent)
    md5: str   # lowercase hex ("" if absent)
    sha1: str  # lowercase hex ("" if absent)
    game: str  # name of the enclosing <game>
    dat: str = ""  # name of the DAT this rom comes from (header name, e.g. "Commodore Amiga - Firmware")
    # Logical game ("set"): No-Intro rom name without its extension, so alternates such as
    # NES ".nes"/".unh" or N64 ".z64"/".v64" share it. Empty for TOSEC (every rom counts).
    set_name: str = ""
    # Redump: the game's ``<category>`` ("Games", "Demos", "Coverdiscs", ...); "" for the other sources.
    category: str = ""

    @property
    def tags(self) -> "Tags":
        """Parsed name tags (regions, languages, version, flags...); cached in tags.py."""
        from .tags import of_rom
        return of_rom(self)


def unit_key(rom: Rom) -> tuple[str, str]:
    """What counts as one item for have/missing/duplicates: the set (No-Intro) or the rom (TOSEC)."""
    return (rom.dat, rom.set_name or rom.name)


def archive_stem(rom: Rom) -> str:
    """Stem an archive holding ``rom`` is named after (No-Intro: the set, TOSEC: the game)."""
    return rom.set_name or rom.game


@dataclass
class DatFile:
    name: str
    description: str
    version: str
    roms: list[Rom]
    format: str = FORMAT_LOGIQX    # "logiqx" | "clrmamepro"
    homepage: str = ""
    _by_sha1: dict[str, list[Rom]] | None = field(default=None, init=False, repr=False, compare=False)
    _by_crc_size: dict[tuple[str, int], list[Rom]] | None = field(
        default=None, init=False, repr=False, compare=False)
    _games: dict[str, list[Rom]] | None = field(default=None, init=False, repr=False, compare=False)
    _sets: dict[str, list[Rom]] | None = field(default=None, init=False, repr=False, compare=False)

    def by_sha1(self) -> dict[str, list[Rom]]:
        """sha1 -> roms (lists: TOSEC has identical dumps under several names)."""
        if self._by_sha1 is None:
            idx: dict[str, list[Rom]] = {}
            for rom in self.roms:
                if rom.sha1:
                    idx.setdefault(rom.sha1, []).append(rom)
            self._by_sha1 = idx
        return self._by_sha1

    def by_crc_size(self) -> dict[tuple[str, int], list[Rom]]:
        """(crc, size) -> roms."""
        if self._by_crc_size is None:
            idx: dict[tuple[str, int], list[Rom]] = {}
            for rom in self.roms:
                if rom.crc:
                    idx.setdefault((rom.crc, rom.size), []).append(rom)
            self._by_crc_size = idx
        return self._by_crc_size

    def games(self) -> dict[str, list[Rom]]:
        """game name -> roms, in DAT order."""
        if self._games is None:
            idx: dict[str, list[Rom]] = {}
            for rom in self.roms:
                idx.setdefault(rom.game, []).append(rom)
            self._games = idx
        return self._games

    def sets(self) -> dict[str, list[Rom]]:
        """set name (``set_name or name``) -> roms, in DAT order (No-Intro: alternates grouped)."""
        if self._sets is None:
            idx: dict[str, list[Rom]] = {}
            for rom in self.roms:
                idx.setdefault(rom.set_name or rom.name, []).append(rom)
            self._sets = idx
        return self._sets

    @property
    def count_by(self) -> str:
        """``"game"`` when completeness is counted per set (No-Intro), else ``"rom"``."""
        return "game" if any(r.set_name for r in self.roms) else "rom"


def _hex(value: str | None, width: int = 0) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    return value.zfill(width) if width else value


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


TOSEC_VERSION_MARK = " (TOSEC-v"


def dat_name_from_filename(path: Union[str, os.PathLike[str]]) -> str:
    """``"X - Y (TOSEC-v2025-01-30_CM).dat"`` -> ``"X - Y"`` (falls back to the stem)."""
    stem = Path(path).name
    if stem.lower().endswith(".dat"):
        stem = stem[:-4]
    cut = stem.find(TOSEC_VERSION_MARK)
    return (stem[:cut] if cut >= 0 else stem).strip()


def detect_format(path: Union[str, os.PathLike[str]]) -> str:
    """``"logiqx"`` or ``"clrmamepro"`` from the first 4 KiB; ``ValueError`` otherwise."""
    with open(path, "rb") as fh:
        head = fh.read(4096)
    text = head.decode("utf-8", errors="replace").lstrip("\ufeff \t\r\n")
    if text.startswith("<"):
        return FORMAT_LOGIQX
    m = re.match(r"[A-Za-z_]+", text)
    if m and m.group(0).lower() in ("clrmamepro", "emulator", "game", "machine"):
        return FORMAT_CLRMAMEPRO
    raise ValueError("unknown DAT format")


def parse_dat(path: Union[str, os.PathLike[str]], set_names: Optional[bool] = None) -> DatFile:
    """Parse a DAT in either format (see :func:`detect_format`).

    ``set_names`` fills ``Rom.set_name`` (rom name without extension); ``None`` means
    True for clrmamepro (No-Intro) and False for Logiqx (TOSEC).
    """
    fmt = detect_format(path)
    if fmt == FORMAT_CLRMAMEPRO:
        return parse_clrmamepro(path, set_names=True if set_names is None else set_names)
    dat = parse_logiqx(path)
    if set_names:
        dat.roms = [replace(r, set_name=strip_ext(r.name)) for r in dat.roms]
    return dat


def parse_logiqx(path: Union[str, os.PathLike[str]]) -> DatFile:
    """Parse a Logiqx XML DAT file.

    Uses iterparse and clears processed <game> elements so memory stays flat.
    The DOCTYPE is ignored (expat does not fetch external DTDs). Every rom's
    ``dat`` is the header ``<name>`` (or the filename part before `` (TOSEC-v``).
    """
    name = description = version = ""
    roms: list[Rom] = []
    in_header = False
    root: ET.Element | None = None
    dat_label = dat_name_from_filename(path)  # until/unless the header names it

    for event, elem in ET.iterparse(str(Path(path)), events=("start", "end")):
        tag = elem.tag
        if event == "start":
            if root is None:
                root = elem
            if tag == "header":
                in_header = True
            continue

        # event == "end"
        if in_header:
            if tag == "name":
                name = (elem.text or "").strip()
            elif tag == "description":
                description = (elem.text or "").strip()
            elif tag == "version":
                version = (elem.text or "").strip()
            elif tag == "header":
                in_header = False
                dat_label = name or dat_name_from_filename(path)
                if root is not None:
                    root.clear()
        elif tag in ("game", "machine"):
            game_name = elem.get("name", "")
            category = (elem.findtext("category") or "").strip()
            for rom_el in elem.iter("rom"):
                roms.append(Rom(
                    name=rom_el.get("name", ""),
                    size=_int(rom_el.get("size")),
                    crc=_hex(rom_el.get("crc"), 8),
                    md5=_hex(rom_el.get("md5")),
                    sha1=_hex(rom_el.get("sha1")),
                    game=game_name,
                    dat=dat_label,
                    category=category,
                ))
            # Drop finished games from the root to keep memory bounded.
            if root is not None:
                root.clear()

    return DatFile(name=name or dat_label, description=description, version=version, roms=roms)


def parse_redump(path: Union[str, os.PathLike[str]]) -> DatFile:
    """Parse a Redump Logiqx DAT: one *game* is one disc, so every rom of a game (its ``.cue`` and one
    ``(Track N).bin`` per track) gets ``set_name = game name`` (have / missing count games, not tracks)."""
    dat = parse_logiqx(path)
    dat.roms = [replace(r, set_name=r.game) for r in dat.roms]
    return dat


# --------------------------------------------------------------------------- clrmamepro

# One token: a quoted string (group 1, escapes kept), a paren, or a bare word; a lone '"'
# (string never closed) is matched last so it can be reported.
_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|[()]|[^\s()"]+|"')
_ESCAPE_RE = re.compile(r'\\(["\\])')
_QUOTED, _OPEN, _CLOSE, _WORD = 0, 1, 2, 3
_HEADER_BLOCKS = ("clrmamepro", "emulator")
_GAME_BLOCKS = ("game", "machine")


class _Tokens:
    """Token stream over a clrmamepro text; keeps positions for error messages."""

    __slots__ = ("text", "path", "items", "i")

    def __init__(self, text: str, path: str) -> None:
        self.text = text
        self.path = path
        items: list[tuple[int, str, int]] = []
        append = items.append
        for m in _TOKEN_RE.finditer(text):
            q = m.group(1)
            if q is not None:
                if "\\" in q:
                    q = _ESCAPE_RE.sub(r"\1", q)
                append((_QUOTED, q, m.start()))
            else:
                tok = m.group(0)
                if tok == "(":
                    append((_OPEN, tok, m.start()))
                elif tok == ")":
                    append((_CLOSE, tok, m.start()))
                elif tok == '"':
                    raise self.error("unterminated string", m.start())
                else:
                    append((_WORD, tok, m.start()))
        self.items = items
        self.i = 0

    def error(self, msg: str, pos: Optional[int] = None) -> ValueError:
        if pos is None:
            pos = self.items[self.i][2] if self.i < len(self.items) else len(self.text)
        line = self.text.count("\n", 0, pos) + 1
        return ValueError(f"{self.path}: line {line}: {msg}")


def _parse_block(tk: _Tokens) -> list[tuple[str, object]]:
    """Parse ``key value ...`` up to the matching ``)`` (already past the ``(``).

    Values are strings or nested lists of pairs.
    """
    items = tk.items
    n = len(items)
    out: list[tuple[str, object]] = []
    while True:
        if tk.i >= n:
            raise tk.error("unbalanced parentheses: missing ')'")
        kind, key, pos = items[tk.i]
        if kind == _CLOSE:
            tk.i += 1
            return out
        if kind == _OPEN:
            raise tk.error("unexpected '('")
        tk.i += 1
        if tk.i >= n:
            raise tk.error(f"missing value for {key!r}", pos)
        vkind, value, _vpos = items[tk.i]
        if vkind == _CLOSE:
            raise tk.error(f"missing value for {key!r}", pos)
        tk.i += 1
        if vkind == _OPEN:
            out.append((key, _parse_block(tk)))
        else:
            out.append((key, value))


def parse_clrmamepro(path: Union[str, os.PathLike[str]], set_names: bool = True) -> DatFile:
    """Parse a clrmamepro text DAT (``clrmamepro ( ... )`` header + ``game ( ... )`` blocks)."""
    p = Path(path)
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # some community DATs (the WHDLoad one) are Windows-1252, not UTF-8: "Alien\xb3", "D\xe9but"
        text = raw.decode("cp1252", errors="replace")
    tk = _Tokens(text, str(p))
    items = tk.items
    n = len(items)
    name = description = version = homepage = date = ""
    games: list[list[tuple[str, object]]] = []
    while tk.i < n:
        kind, word, pos = items[tk.i]
        if kind != _WORD and kind != _QUOTED:
            raise tk.error(f"unexpected {word!r}" if kind == _CLOSE else "unexpected '('")
        tk.i += 1
        if tk.i >= n or items[tk.i][0] != _OPEN:
            raise tk.error(f"expected '(' after {word!r}", pos)
        tk.i += 1
        block = _parse_block(tk)
        low = word.lower()
        if low in _HEADER_BLOCKS:
            for key, value in block:
                if isinstance(value, str):
                    if key == "name":
                        name = value
                    elif key == "description":
                        description = value
                    elif key == "version":
                        version = value
                    elif key == "homepage":
                        homepage = value
                    elif key == "date" and not date:
                        date = value   # e.g. the WHDLoad DAT: ``date "2026-07-05"`` (no ``version``)
        elif low in _GAME_BLOCKS:
            games.append(block)
        # other top-level blocks (resource, ...) are ignored

    version = version or date
    dat_label = name or dat_name_from_filename(p)
    roms: list[Rom] = []
    for block in games:
        game_name = ""
        rom_blocks: list[list[tuple[str, object]]] = []
        for key, value in block:
            if key == "name" and isinstance(value, str):
                game_name = value
            elif key == "rom" and isinstance(value, list):
                rom_blocks.append(value)
        for rb in rom_blocks:
            fields: dict[str, str] = {}
            for key, value in rb:
                if isinstance(value, str) and key not in fields:
                    fields[key] = value
            rom_name = fields.get("name", "")
            roms.append(Rom(
                name=rom_name,
                size=_int(fields.get("size")),
                crc=_hex(fields.get("crc"), 8),
                md5=_hex(fields.get("md5")),
                sha1=_hex(fields.get("sha1")),
                game=game_name,
                dat=dat_label,
                set_name=strip_ext(rom_name) if set_names else "",
            ))
    return DatFile(name=name or dat_label, description=description, version=version, roms=roms,
                   format=FORMAT_CLRMAMEPRO, homepage=homepage)

