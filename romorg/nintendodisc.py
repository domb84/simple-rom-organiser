"""Wii, GameCube and Wii U disc images: who they are, read from their headers (a few bytes), no checksum and no key.

* **Wii / GameCube** (``.iso`` ``.gcm`` ``.rvz`` ``.wia`` ``.wbfs`` ``.ciso``): the disc header starts with the 6-character game ID
  (``SB4E01``: game ``SB4``, region ``E``, maker ``01``), then the disc number, the revision and a 64-character title. A Wii disc has
  the magic ``0x5D1C9EA3`` at ``0x18``, a GameCube disc ``0xC2339F3D`` at ``0x1C``. Compressed formats keep a copy of the header
  where it is cheap to read: RVZ / WIA at ``0x58``, WBFS at ``0x200``, CISO at ``0x8000``.
* **Wii U** (``.wux`` ``.wud``): the first bytes of the disc are the product code, ``WUP-P-AFXE-00-551USA-0`` (game ``AFXE``,
  region ``USA``). A WUX is a deduplicated WUD: a sector table points at the sectors, the first of which holds that header.

Why no checksum against Redump: a Wii disc in RVZ / WIA / WBFS is not the original image (its encrypted partitions are stored
decrypted) and a WUX is not a WUD, so the original image's hash can only be had by rebuilding the whole disc (tens of gigabytes,
with encryption). The game ID is what Dolphin, Cemu and GameTDB use to tell games apart, and what the saves are named by.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

__all__ = ["DISC_EXTS", "WIIU_EXTS", "DiscInfo", "read_disc", "read_wiiu", "region_of", "REGIONS"]

DISC_EXTS = (".iso", ".gcm", ".rvz", ".wia", ".wbfs", ".ciso")
WIIU_EXTS = (".wux", ".wud")
WII_MAGIC, GC_MAGIC = 0x5D1C9EA3, 0xC2339F3D
# the 4th character of a game ID (and of a Wii U game code) is the region
REGIONS: Dict[str, str] = {"E": "USA", "P": "Europe", "J": "Japan", "K": "Korea", "D": "Germany", "F": "France", "I": "Italy",
                           "S": "Spain", "U": "Australia", "X": "Europe", "Y": "Europe", "Z": "Europe", "W": "Taiwan", "R": "Russia",
                           "L": "Japan (import)", "M": "Europe (import)", "N": "USA (import)", "H": "Netherlands", "Q": "Korea"}
_ID = re.compile(r"^[A-Z0-9]{6}$")
_CODE = re.compile(r"^WUP-[A-Z]-([A-Z0-9]{4})-(\d\d)-(\w+?)(USA|EUR|JPN|KOR|CHN|TWN|ALL)?-")


@dataclass
class DiscInfo:
    kind: str                   # "wii" | "gc" | "wiiu"
    game_id: str                # "SB4E01"; Wii U: the 4-character game code, "AFXE"
    name: str = ""              # the title in the disc header (Wii U: "")
    disc: int = 0
    revision: int = 0
    region: str = ""
    container: str = ""         # iso | rvz | wia | wbfs | ciso | wux | wud
    product: str = ""           # Wii U: the full product code

    @property
    def code4(self) -> str:
        """The first four characters of the ID: what the save folders / files of Dolphin are named by."""
        return self.game_id[:4]


def region_of(code: str) -> str:
    return REGIONS.get(code[3:4].upper(), "") if len(code) >= 4 else ""


def _parse_header(raw: bytes, container: str) -> Optional[DiscInfo]:
    if len(raw) < 0x40:
        return None
    game_id = raw[:6].decode("ascii", errors="replace")
    if not _ID.match(game_id):
        return None
    wii = len(raw) >= 0x1C and struct.unpack(">I", raw[0x18:0x1C])[0] == WII_MAGIC
    gc = len(raw) >= 0x20 and struct.unpack(">I", raw[0x1C:0x20])[0] == GC_MAGIC
    if not (wii or gc):
        return None
    name = raw[0x20:0x60].split(b"\0")[0].decode("shift_jis" if game_id[3] == "J" else "latin-1", errors="replace").strip()
    return DiscInfo("wii" if wii else "gc", game_id, name, raw[6], raw[7], region_of(game_id), container)


def read_disc(path: Path) -> Optional[DiscInfo]:
    """The header of a Wii / GameCube disc image, or None when the file is not one (or is a format whose header is compressed)."""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if ext in (".rvz", ".wia") or magic in (b"RVZ\x01", b"WIA\x01"):
                f.seek(0x58)
                return _parse_header(f.read(0x80), "rvz" if magic[:3] == b"RVZ" else "wia")
            if ext == ".wbfs" or magic == b"WBFS":
                f.seek(0x200)
                return _parse_header(f.read(0x80), "wbfs")
            if ext == ".ciso" or magic == b"CISO":
                f.seek(0x8000)
                return _parse_header(f.read(0x80), "ciso")
            if ext in (".iso", ".gcm"):
                f.seek(0)
                return _parse_header(f.read(0x80), "iso")
    except OSError:
        pass
    return None


def read_wiiu(path: Path) -> Optional[DiscInfo]:
    """The product code of a Wii U disc image (``.wux`` / ``.wud``), or None."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            head = f.read(0x20)
            if head[:4] == b"WUX0":
                sector, size = struct.unpack_from("<IQ", head, 8)[0], struct.unpack_from("<Q", head, 0x10)[0]
                if sector == 0 or size == 0:
                    return None
                count = size // sector
                f.seek(0x20)
                first = struct.unpack("<I", f.read(4))[0]
                data = (0x20 + 4 * count + sector - 1) // sector * sector
                f.seek(data + first * sector)
                raw, container = f.read(0x40), "wux"
            elif head[:4] == b"WUP-":
                raw, container = head + f.read(0x20), "wud"
            else:
                return None
    except (OSError, struct.error):
        return None
    product = raw.split(b"\0")[0].decode("ascii", errors="replace")
    m = _CODE.match(product + "-")
    if not product.startswith("WUP-") or m is None:
        return None
    code = m.group(1)
    return DiscInfo("wiiu", code, "", 0, int(m.group(2)), region_of(code), container, product)
