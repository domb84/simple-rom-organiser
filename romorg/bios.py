"""BIOS, firmware and key files: told by checksum, kept with the ROMs.

A file in a system's folder that matches no game may be something an emulator needs: a console's BIOS (``scph1001.bin``), a firmware
image, a Kickstart. Such a file is recognised by its checksum against the list libretro's cores use (``biosdata``, from
``System.dat``), whatever it is called, and treated like a ROM: it stays with the ROMs, under the name the emulators expect, and is
never moved to ``_unmatched`` or the archive. The key files of the Switch and Wii U emulators (``prod.keys``, ``keys.txt`` ...) have no
checksum to go by (they differ per console); they are told by their name and left as they are.

Only the checksum decides: a file that merely has a BIOS's name is an ordinary unmatched file.
"""

from __future__ import annotations

import hashlib
import os
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from . import biosdata

__all__ = ["Bios", "lookup", "identify", "is_key_file", "KEY_NAMES", "MAX_BYTES"]

KEY_NAMES = frozenset(("prod.keys", "title.keys", "dev.keys", "keys.txt", "console.keys"))
MAX_BYTES = 64 << 20                    # nothing on the list is larger; a bigger file is never read for this


@dataclass(frozen=True)
class Bios:
    system: str                         # "Sony - PlayStation"
    name: str                           # the name the emulators expect (no folder)
    names: FrozenSet[str]               # every name the list has for this content (folded): any of them is right


def _build() -> Tuple[Dict[str, Bios], Dict[Tuple[int, str], Bios], FrozenSet[int]]:
    groups: Dict[str, List[Tuple[str, str]]] = {}
    sizes: Dict[str, int] = {}
    crcs: Dict[str, str] = {}
    for system, name, size, crc, sha1 in biosdata.ENTRIES:
        groups.setdefault(sha1, []).append((system, name.replace("\\", "/").rsplit("/", 1)[-1]))
        sizes[sha1], crcs[sha1] = size, crc
    by_sha1: Dict[str, Bios] = {}
    by_crc: Dict[Tuple[int, str], Bios] = {}
    for sha1, items in groups.items():
        b = Bios(items[0][0], items[0][1], frozenset(n.casefold() for _s, n in items))
        by_sha1[sha1] = b
        by_crc.setdefault((sizes[sha1], crcs[sha1]), b)
    return by_sha1, by_crc, frozenset(sizes.values())


_BY_SHA1, _BY_CRC, _SIZES = _build()
_SEEN: Dict[Tuple[str, int, int], Optional[Bios]] = {}


def lookup(size: int, crc: str = "", sha1: str = "") -> Optional[Bios]:
    """The BIOS / firmware file with these checksums, or None. ``sha1`` decides when given, else size and CRC-32."""
    if sha1:
        return _BY_SHA1.get(sha1.lower())
    return _BY_CRC.get((int(size), crc.lower().zfill(8))) if crc else None


def identify(path: Path) -> Optional[Bios]:
    """The BIOS / firmware file at ``path`` (read only when its size is one the list has), or None. Never raises."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size not in _SIZES or st.st_size > MAX_BYTES:
        return None
    key = (os.fspath(path), st.st_size, st.st_mtime_ns)
    if key not in _SEEN:
        try:
            h, crc = hashlib.sha1(), 0
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    crc = zlib.crc32(chunk, crc)
            _SEEN[key] = lookup(st.st_size, sha1=h.hexdigest())
        except OSError:
            return None
        if len(_SEEN) > 4096:
            _SEEN.pop(next(iter(_SEEN)))
    return _SEEN[key]


def is_key_file(path: Path) -> bool:
    """True for the key files of the Switch and Wii U emulators (told by name: they have no common checksum)."""
    return Path(path).name.casefold() in KEY_NAMES
