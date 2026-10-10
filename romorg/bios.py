"""BIOS, firmware and key files: told by checksum, kept with the ROMs.

A file in a system's folder that matches no game may be something an emulator needs: a console's BIOS (``scph1001.bin``), a firmware
image, a Kickstart. Such a file is recognised by its checksum, whatever it is called, against two lists:

* the one libretro's cores use (``biosdata``, from ``System.dat``): it also says the name the emulators expect;
* the **TOSEC "- Firmware" DATs** (``Sega 32X - Firmware``: about 140 systems), read from the installed TOSEC pack: for the consoles
  whose cores need no BIOS and for the machines libretro has no core for. A Library build names such a file as the list names it, as it names a ROM after its DAT.

A file that matches is treated like a ROM: it stays with the ROMs and is never moved to ``_unmatched`` or the archive (when the
library rule *Keep BIOS, firmware and key files* is on). The key files of the Switch and Wii U emulators (``prod.keys``,
``keys.txt`` ...) have no checksum to go by (they differ per console); they are told by their name and left as they are.

Only the checksum decides: a file that merely has a BIOS's name is an ordinary unmatched file. A zip whose every file is one of
them (``[BIOS] Nintendo Game Boy Advance Boot ROM (World).zip``) is kept the same way, told by the checksums in its directory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from . import biosdata

__all__ = ["Bios", "Found", "lookup", "identify", "identify_archive", "find_in_scan", "find_in_files", "is_database_name", "is_firmware_dat", "count_firmware_matches", "refresh", "signature", "sources",
           "is_key_file", "KEY_NAMES", "MAX_BYTES"]

KEY_NAMES = frozenset(("prod.keys", "title.keys", "dev.keys", "keys.txt", "console.keys"))
MAX_BYTES = 64 << 20                    # nothing on the list is larger; a bigger file is never read for this


@dataclass(frozen=True)
class Bios:
    system: str                         # "Sony - PlayStation"
    name: str                           # the name the emulators expect (no folder)
    names: FrozenSet[str]               # every name the list has for this content (folded): any of them is right
    source: str = "libretro"            # "libretro" (names are what the emulators expect) | "tosec" (the list's own name for it)
    game: str = ""                      # tosec: the name of the game entry, flags and all: the library rules read it like a ROM's name


@dataclass(frozen=True)
class Found:
    """A BIOS / firmware file found in a folder: what it is, and whether it is a zip of such files."""
    bios: Bios
    in_zip: bool = False


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
_SEEN_ZIPS: Dict[Tuple[str, int, int], Optional[Bios]] = {}


# ---- the TOSEC "- Firmware" DATs of the installed pack (read once; again when the pack changes)
_FIRMWARE = re.compile(r" - Firmware(?: - .*)?$")
_T_SHA1: Dict[str, Bios] = {}
_T_CRC: Dict[Tuple[int, str], Bios] = {}
_T_SIZES: FrozenSet[int] = frozenset()
_T_NAMES: FrozenSet[str] = frozenset()           # the names the TOSEC lists give (folded): what a Library build names a BIOS file
_T_SIG: Optional[tuple] = None
_T_ENTRIES = 0


def _firmware_dats() -> List[Any]:
    from . import tosec
    try:
        return [d for d in tosec.list_dats() if _FIRMWARE.search(d.name)]
    except Exception:  # noqa: BLE001 - no pack, no list
        return []


def signature() -> tuple:
    """What the TOSEC firmware lists are made of (the DATs and their versions): a scan made with others is not the same scan."""
    refresh()
    return _T_SIG or ()


def refresh() -> bool:
    """Read the installed TOSEC "- Firmware" DATs again when the pack changed (a check costs one folder listing). True when read."""
    global _T_SHA1, _T_CRC, _T_SIZES, _T_NAMES, _T_SIG, _T_ENTRIES
    dats = _firmware_dats()
    sig = tuple((d.name, d.version) for d in dats)
    if sig == _T_SIG:
        return False
    from . import datfile
    by_sha1: Dict[str, Bios] = {}
    by_crc: Dict[Tuple[int, str], Bios] = {}
    sizes = set()
    names = set()
    count = 0
    for d in dats:
        try:
            roms = datfile.parse_dat(d.path).roms
        except Exception:  # noqa: BLE001 - one damaged DAT does not take the rest with it
            continue
        system = _FIRMWARE.sub("", d.name)
        for r in roms:
            if not r.sha1 or not r.crc:
                continue
            b = Bios(system, r.name, frozenset(), "tosec", r.game or "")
            by_sha1.setdefault(r.sha1.lower(), b)
            by_crc.setdefault((int(r.size), r.crc.lower().zfill(8)), b)
            sizes.add(int(r.size))
            names.add(r.name.casefold())
            count += 1
    _T_SHA1, _T_CRC, _T_SIZES, _T_NAMES, _T_SIG, _T_ENTRIES = by_sha1, by_crc, frozenset(sizes), frozenset(names), sig, count
    return True


def is_database_name(name: str) -> bool:
    """True when ``name`` is one the TOSEC firmware lists give a file (what a Library build names a BIOS file: it can be found by it)."""
    if _T_SIG is None:
        refresh()
    return name.casefold() in _T_NAMES


def sources() -> List[Dict[str, Any]]:
    """What the lists are made of, for the BIOS page: ``[{name, version, entries, installed}]``."""
    refresh()
    dats = _firmware_dats()
    return [{"name": "libretro System.dat", "version": biosdata.VERSION, "entries": len(biosdata.ENTRIES), "installed": True,
             "note": "what libretro's cores ask for, with the names the emulators expect"},
            {"name": "TOSEC firmware DATs", "version": f"{len(dats)} DATs", "entries": _T_ENTRIES, "installed": bool(dats),
             "note": "the TOSEC pack's \"- Firmware\" lists (for consoles whose cores need no BIOS, and older machines)"}]


def _both(libretro: Optional[Bios], tosec: Optional[Bios]) -> Optional[Bios]:
    """What the lists say of one file: when TOSEC knows it, its name for it (the database's), with the names the emulators look for
    from libretro's list besides; else what libretro says. The emulators' name is for the copy that goes to their folder."""
    if tosec is None:
        return libretro
    return tosec if libretro is None else dataclasses.replace(tosec, names=libretro.names)


def lookup(size: int, crc: str = "", sha1: str = "") -> Optional[Bios]:
    """The BIOS / firmware file with these checksums, or None. ``sha1`` decides when given, else size and CRC-32."""
    if _T_SIG is None:
        refresh()
    if sha1:
        return _both(_BY_SHA1.get(sha1.lower()), _T_SHA1.get(sha1.lower()))
    if not crc:
        return None
    key = (int(size), crc.lower().zfill(8))
    return _both(_BY_CRC.get(key), _T_CRC.get(key))


def identify(path: Path) -> Optional[Bios]:
    """The BIOS / firmware file at ``path`` (read only when its size is one the list has), or None. Never raises."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if _T_SIG is None:
        refresh()
    if (st.st_size not in _SIZES and st.st_size not in _T_SIZES) or st.st_size > MAX_BYTES:
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


def identify_archive(path: Path) -> Optional[Bios]:
    """The BIOS / firmware file a zip holds, when EVERY file in it is one (told by the size and CRC-32 of its directory entry, so
    nothing is read or unpacked); the first of them. None for any other zip. Never raises."""
    path = Path(path)
    if path.suffix.lower() != ".zip":
        return None
    try:
        st = os.stat(path)
        if st.st_size > MAX_BYTES:
            return None
        key = (os.fspath(path), st.st_size, st.st_mtime_ns)
        if key not in _SEEN_ZIPS:
            with zipfile.ZipFile(path) as zf:
                members = [i for i in zf.infolist() if not i.is_dir()]
                found = [lookup(i.file_size, f"{i.CRC:08x}") for i in members] if 0 < len(members) <= 64 else [None]
            _SEEN_ZIPS[key] = found[0] if all(found) else None
            if len(_SEEN_ZIPS) > 4096:
                _SEEN_ZIPS.pop(next(iter(_SEEN_ZIPS)))
    except (OSError, zipfile.BadZipFile, ValueError, NotImplementedError):
        return None
    return _SEEN_ZIPS[key]


def find_in_scan(result: Any) -> Dict[Path, Found]:
    """The BIOS / firmware files among the files of a scan that no game matched, by the checksums the scan already has (nothing is
    read again): ``{path: Found}``. A loose file by its checksums; a zip when every file in it is one."""
    refresh()
    root = Path(getattr(result, "root", "") or ".")

    def absolute(p: Any) -> Path:
        p = Path(p)
        return p if p.is_absolute() else root / p

    out: Dict[Path, Found] = {}
    members: Dict[Path, List[Optional[Bios]]] = {}
    for e in getattr(result, "unmatched", ()):
        if getattr(e, "member", None) is None:
            found = lookup(e.size, e.crc or "", getattr(e, "sha1", "") or "")
            if found is not None:
                out[absolute(e.path)] = Found(found)
        else:
            members.setdefault(absolute(e.path), []).append(lookup(e.size, e.crc or ""))
    matched = {absolute(m.entry.path) for m in getattr(result, "matched", ()) if getattr(m.entry, "member", None) is not None}
    for path, found in members.items():
        if path not in matched and found and all(found):
            out[path] = Found(found[0], True)
    return out


def find_in_files(files: Any) -> Dict[Path, Found]:
    """The BIOS / firmware files among ``files`` (a disc system's folder holds loose files, not entries with checksums: each is looked
    at, and one of a size no list has is not read)."""
    refresh()
    out: Dict[Path, Found] = {}
    for f in files:
        f = Path(f)
        inside = identify_archive(f)
        if inside is not None:
            out[f] = Found(inside, True)
            continue
        found = identify(f)
        if found is not None:
            out[f] = Found(found)
    return out


def is_firmware_dat(name: str) -> bool:
    """True for a TOSEC "- Firmware" DAT (``Commodore Amiga - Firmware``): a file that matches a game of it is a BIOS / firmware file
    (the Amiga's Kickstarts are such games of their system)."""
    return bool(_FIRMWARE.search(name or ""))


def count_firmware_matches(result: Any) -> int:
    """How many files of a scan matched a game of a "- Firmware" DAT (one per file, however many members it has)."""
    seen = set()
    for m in getattr(result, "matched", ()):
        roms = getattr(m, "roms", None) or []
        if roms and is_firmware_dat(getattr(roms[0], "dat", "")):
            seen.add((str(m.entry.path), m.entry.member))
    return len(seen)


def is_key_file(path: Path) -> bool:
    """True for the key files of the Switch and Wii U emulators (told by name: they have no common checksum)."""
    return Path(path).name.casefold() in KEY_NAMES
