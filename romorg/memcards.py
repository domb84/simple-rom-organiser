"""Memory cards: which games' saves a card image holds.

A console memory card is a small file system. A RetroArch core keeps the whole card of a game in one file named after the game
(``Game.srm``, ``Game.A1.bin``), and an emulator's card image can hold the saves of many games. Reading the card's directory says
which games those are, whatever the file is called. Each reader is written from the public format descriptions (stdlib only; the
Python libraries that read PS2 cards, ``ps2mc`` and ``mymc+``, are GPL / third party and the app takes no dependencies):

* **PlayStation** (128 KB: RetroArch's ``.srm`` of PCSX-ReARMed / Beetle PSX, ``.mcr`` ``.mcd`` ``.bin``; with the DexDrive ``.gme``
  or PSP ``.vmp`` header too): 16 blocks of 8 KB; the first holds 15 directory frames (128 bytes: state, size, a 20-byte file name
  ``BASLUS-01234NAME`` = region, the game's product code, the game's own name for the save) and each save starts with a header
  frame (``SC`` and the title in Shift-JIS).
* **Dreamcast VMU** (128 KB: Flycast's ``.A1.bin``): 256 blocks of 512 bytes; the root block (255) says where the directory is, each
  32-byte entry names a file (12 characters) and the file's header block carries a description of 16 and of 32 characters.
* **GameCube** (``.raw`` ``.gcp``, 0.5 to 16 MB): 8 KB blocks, a directory in block 1 (and 2) of 127 entries of 64 bytes, each with
  the 4-character game code and the save's file name. Dolphin's ``.gci`` files are single saves and are read elsewhere.
* **PlayStation 2** (8 MB ``.ps2``): read by :mod:`romorg.pcsx2` (its directories are named ``BASLUS-20946GTA50000``).

Nothing here writes; a file that is not a card is ``None``.
"""

from __future__ import annotations

import os
import re
import struct
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__all__ = ["Card", "CardEntry", "read", "sniff_size", "PS1", "DREAMCAST", "GAMECUBE", "PS2"]

PS1, DREAMCAST, GAMECUBE, PS2 = "ps1", "dreamcast", "gamecube", "ps2"
LABEL = {PS1: "PlayStation card", DREAMCAST: "Dreamcast VMU", GAMECUBE: "GameCube card", PS2: "PlayStation 2 card"}
_PS1_SIZE = 131072
_PS1_HEADERS = {0: 0, 3904: 3904, 128: 128}      # extra bytes before the card: none, DexDrive (.gme), PSP (.vmp)
_GC_SIZES = {0x80000 << n for n in range(6)}       # 4 Mbit ... 128 Mbit (59 to 2043 blocks of 8 KB)
_PS1_CODE = re.compile(r"^B[AEISC]([A-Z]{4}-\d{5})")


@dataclass
class CardEntry:
    """One save on a card."""
    title: str                  # what the card says the save is (the game's own title for it, tidied), may be ""
    code: str = ""              # the game's product code / game code ("SLUS-01234", "GALE") when the card carries it
    name: str = ""              # the save's file name on the card
    blocks: int = 0


@dataclass
class Card:
    kind: str
    entries: List[CardEntry] = field(default_factory=list)
    free_blocks: int = 0

    @property
    def label(self) -> str:
        return LABEL.get(self.kind, "memory card")

    def games(self) -> List[str]:
        """One line per save for the screen: ``Title (code)``."""
        out = []
        for e in self.entries:
            text = e.title or e.name
            out.append(f"{text} ({e.code})" if e.code and e.code not in text else text)
        return out


def _text(raw: bytes, encoding: str = "shift_jis") -> str:
    """A card's text field: up to the first NUL, decoded, full-width letters made plain (``ＳＰＩＤＥＲ`` -> ``SPIDER``), spaces tidied."""
    raw = raw.split(b"\0")[0]
    for enc in (encoding, "ascii", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("ascii", errors="replace")
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", "".join(c for c in text if c.isprintable())).strip()


# --------------------------------------------------------------------------- PlayStation

def _ps1(data: bytes) -> Optional[Card]:
    if len(data) != _PS1_SIZE or data[:2] != b"MC":
        return None
    card = Card(PS1)
    for i in range(1, 16):
        frame = data[i * 128:(i + 1) * 128]
        state = frame[0]
        if state == 0xA0:
            card.free_blocks += 1
        elif state == 0x51:                                  # the first block of a save
            size = struct.unpack_from("<I", frame, 4)[0]
            name = frame[10:30].split(b"\0")[0].decode("ascii", errors="replace").strip()
            head = data[i * 8192:i * 8192 + 128]
            title = _text(head[4:68]) if head[:2] == b"SC" else ""
            m = _PS1_CODE.match(name)
            card.entries.append(CardEntry(title, m.group(1) if m else "", name, max(1, size // 8192)))
    return card


# --------------------------------------------------------------------------- Dreamcast VMU

def _vmu(data: bytes) -> Optional[Card]:
    if len(data) != _PS1_SIZE or data[0xFF * 512:0xFF * 512 + 16] != b"\x55" * 16:
        return None
    root = data[0xFF * 512:0x100 * 512]
    dir_loc, dir_size = struct.unpack_from("<HH", root, 0x4A)[0], struct.unpack_from("<H", root, 0x4C)[0]
    if not 0 < dir_size <= 64 or dir_loc >= 256:
        return None
    card = Card(DREAMCAST)
    for n in range(dir_size):                                # the directory runs backwards from its first block
        block = dir_loc - n
        if block < 0:
            break
        raw = data[block * 512:(block + 1) * 512]
        for k in range(16):
            e = raw[k * 32:(k + 1) * 32]
            if e[0] != 0x33:                                 # 0x33 = a data file (0xCC = a mini game, 0 = nothing)
                continue
            first, = struct.unpack_from("<H", e, 2)
            size, offset = struct.unpack_from("<HH", e, 0x18)
            name = e[4:16].decode("ascii", errors="replace").strip()
            head_at = (first + offset) * 512
            head = data[head_at:head_at + 64] if head_at + 64 <= len(data) else b""
            long_text, short_text = _text(head[16:48]), _text(head[0:16])
            card.entries.append(CardEntry(long_text or short_text, "", name, size))
    return card


# --------------------------------------------------------------------------- GameCube

def _gc_checksum(block: bytes, length: int) -> Tuple[int, int]:
    """The two checksums of a card block: the sum of the 16-bit words of its first ``length`` bytes, and the sum of their inverses."""
    a = b = 0
    for (word,) in struct.iter_unpack(">H", block[:length]):
        a = (a + word) & 0xFFFF
        b = (b + (word ^ 0xFFFF)) & 0xFFFF
    if a == 0xFFFF:
        a = 0
    if b == 0xFFFF:
        b = 0
    return a, b


def _gc(data: bytes) -> Optional[Card]:
    if len(data) not in _GC_SIZES and (len(data) - 0x110) not in _GC_SIZES:
        return None
    if len(data) not in _GC_SIZES:
        data = data[0x110:]                                   # a .gcp file starts with a 0x110-byte header of its own
    best: Optional[Tuple[int, bytes]] = None
    for at in (0x2000, 0x4000):                               # the directory and its backup: the valid one with the higher update counter
        block = data[at:at + 0x2000]
        if len(block) < 0x2000:
            return None
        a, b = _gc_checksum(block, 0x1FFC)
        if (a, b) != struct.unpack_from(">HH", block, 0x1FFC):
            continue
        counter, = struct.unpack_from(">H", block, 0x1FFA)
        if best is None or counter > best[0]:
            best = (counter, block)
    if best is None:
        return None
    card = Card(GAMECUBE)
    sjis = struct.unpack_from(">H", data, 0x24)[0] == 1
    for k in range(127):
        e = best[1][k * 64:(k + 1) * 64]
        if e[:4] == b"\xff\xff\xff\xff":
            continue
        code = e[:4].decode("ascii", errors="replace")
        name = _text(e[8:40], "shift_jis" if sjis else "latin-1")
        blocks, = struct.unpack_from(">H", e, 0x38)
        card.entries.append(CardEntry(name, code, name, blocks))
    return card


# --------------------------------------------------------------------------- the front door

def sniff_size(size: int) -> bool:
    """True when a file of this size can be a card at all (a cartridge save is far smaller or an odd size): the cheap test before reading."""
    return size - _PS1_SIZE in _PS1_HEADERS or size in _GC_SIZES or size - 0x110 in _GC_SIZES or size in (8388608, 8650752)


_CACHE: Dict[Tuple[str, int, int], Optional[Card]] = {}


def read(path: Path) -> Optional[Card]:
    """The card in ``path``, or None when the file is not one (or cannot be read). Remembered per file, size and time."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return None
    if not sniff_size(st.st_size):
        return None
    key = (os.fspath(path), st.st_size, st.st_mtime_ns)
    if key in _CACHE:
        return _CACHE[key]
    card: Optional[Card] = None
    try:
        if st.st_size in (8388608, 8650752):
            from . import pcsx2
            saves = [s for s in pcsx2.read_card_image(path) if s.kind == "save" and s.serial]       # (BADATA-SYSTEM is the console's own)
            if saves:
                card = Card(PS2, [CardEntry("", s.serial, s.name, 0) for s in saves])
            elif path.read_bytes()[:28] == b"Sony PS2 Memory Card Format ":
                card = Card(PS2)
        else:
            data = path.read_bytes()
            extra = st.st_size - _PS1_SIZE
            if extra in (3904, 128):
                data = data[extra:]
            card = _ps1(data) or _vmu(data) or _gc(data)
    except (OSError, struct.error, ValueError, IndexError):
        card = None
    if len(_CACHE) > 4096:
        _CACHE.clear()
    _CACHE[key] = card
    return card
