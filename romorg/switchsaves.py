"""The save data of the Switch emulators, found and matched by title ID.

Both emulators keep a game's save as a plain folder of the files the game wrote (no encryption, no container), and both know the
game by its title ID:

* **Eden / yuzu and its forks** (Sudachi, Suyu ...): ``<nand>/user/save/0000000000000000/<user ID, 32 hex>/<title ID, 16 hex>/...``.
  The user folder is the profile's UUID; all zeros is device save data.
* **Ryujinx**: ``<data folder>/bis/user/save/<save ID, 16 hex counter>/{0,1}/...`` where ``0`` is the committed copy and ``1`` the
  working copy (LibHac's directory save file system). The counter says nothing about the game, but ``ExtraData0`` next to them starts
  with the save's attribute: program (title) ID (8 bytes, little endian), user ID (16 bytes) and, at ``0x20``, the save type.

Matching a game's saves of the two emulators is then a lookup by title ID (and by user, see :func:`users_of`).
"""

from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = ["SwitchSave", "find_eden", "find_ryujinx", "eden_users", "ryujinx_users", "group_by_title", "describe_pair",
           "read_extra_data", "read_ini_value", "detect_eden", "detect_ryujinx", "eden_game_dirs", "SAVE_TYPES"]

_HEX16 = re.compile(r"^[0-9A-Fa-f]{16}$")
_HEX32 = re.compile(r"^[0-9A-Fa-f]{32}$")
SAVE_TYPES = {0: "system", 1: "account", 2: "bcat", 3: "device", 4: "temporary", 5: "cache", 6: "system bcat"}   # LibHac's SaveDataType


@dataclass
class SwitchSave:
    emulator: str                       # "eden" | "ryujinx"
    title_id: str                       # upper case
    user: str                           # the profile's ID as the emulator spells it ("" for device data)
    kind: str                           # account | device | cache | ...
    path: Path                          # the folder that holds the game's files
    files: int = 0
    bytes: int = 0
    mtime: float = 0.0                  # the newest file
    save_id: str = ""                   # Ryujinx: the counter folder
    listing: Tuple[Tuple[str, int], ...] = field(default_factory=tuple)      # (relative path, size) of every file

    def public(self) -> Dict[str, object]:
        return {"emulator": self.emulator, "title_id": self.title_id, "user": self.user, "kind": self.kind, "path": str(self.path),
                "files": self.files, "bytes": self.bytes, "mtime": self.mtime, "save_id": self.save_id}


def _measure(folder: Path) -> Tuple[int, int, float, Tuple[Tuple[str, int], ...]]:
    files = 0
    total = 0
    newest = 0.0
    listing = []
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            files += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
            listing.append((p.relative_to(folder).as_posix(), st.st_size))
    listing.sort()
    return files, total, newest, tuple(listing)


# --------------------------------------------------------------------------- Eden / yuzu
def find_eden(nand: Path) -> List[SwitchSave]:
    """Every game save under ``<nand>/user/save/0000000000000000``."""
    base = Path(nand) / "user" / "save" / "0000000000000000"
    out: List[SwitchSave] = []
    try:
        users = sorted(p for p in base.iterdir() if p.is_dir() and _HEX32.match(p.name))
    except OSError:
        return out
    for user in users:
        try:
            titles = sorted(p for p in user.iterdir() if p.is_dir() and _HEX16.match(p.name))
        except OSError:
            continue
        device = int(user.name, 16) == 0
        for title in titles:
            files, total, newest, listing = _measure(title)
            out.append(SwitchSave("eden", title.name.upper(), "" if device else user.name.upper(), "device" if device else "account",
                                  title, files, total, newest, listing=listing))
    return out


def eden_users(nand: Path) -> List[Dict[str, str]]:
    """The profiles that have save folders in an Eden / yuzu NAND (the name is not stored with the saves)."""
    base = Path(nand) / "user" / "save" / "0000000000000000"
    try:
        return [{"id": p.name.upper(), "name": ""} for p in sorted(base.iterdir()) if p.is_dir() and _HEX32.match(p.name)
                and int(p.name, 16) != 0]
    except OSError:
        return []


# --------------------------------------------------------------------------- Ryujinx
def read_extra_data(path: Path) -> Optional[Tuple[str, str, int]]:
    """``(title ID, user ID, save type)`` from the save attribute at the start of an ``ExtraData0`` file."""
    try:
        with open(path, "rb") as f:
            raw = f.read(0x28)
    except OSError:
        return None
    if len(raw) < 0x21:
        return None
    program = struct.unpack("<Q", raw[:8])[0]
    user_hi, user_lo = struct.unpack("<QQ", raw[8:24])
    return f"{program:016X}", f"{user_hi:016X}{user_lo:016X}", raw[0x20]


def find_ryujinx(data: Path) -> List[SwitchSave]:
    """Every game save under ``<data folder>/bis/user/save`` (the committed copy ``0`` of each)."""
    base = Path(data) / "bis" / "user" / "save"
    out: List[SwitchSave] = []
    try:
        ids = sorted(p for p in base.iterdir() if p.is_dir() and _HEX16.match(p.name))
    except OSError:
        return out
    for folder in ids:
        attr = read_extra_data(folder / "ExtraData0")
        committed = folder / "0"
        if attr is None or not committed.is_dir():
            continue
        title, user, kind = attr
        if int(title, 16) == 0:
            continue
        files, total, newest, listing = _measure(committed)
        zero = int(user, 16) == 0
        out.append(SwitchSave("ryujinx", title, "" if zero else user, SAVE_TYPES.get(kind, str(kind)), committed, files, total,
                              newest, save_id=folder.name, listing=listing))
    return out


def ryujinx_users(data: Path) -> List[Dict[str, str]]:
    """``[{id, name}]`` from Ryujinx's ``system/Profiles.json``."""
    try:
        doc = json.loads((Path(data) / "system" / "Profiles.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for p in doc.get("profiles", []) if isinstance(doc, dict) else []:
        if isinstance(p, dict) and isinstance(p.get("user_id"), str):
            out.append({"id": p["user_id"].upper(), "name": str(p.get("name") or "")})
    return out


# --------------------------------------------------------------------------- matching
def group_by_title(*lists: Iterable[SwitchSave]) -> Dict[str, List[SwitchSave]]:
    """All saves by title ID."""
    out: Dict[str, List[SwitchSave]] = {}
    for saves in lists:
        for s in saves:
            out.setdefault(s.title_id, []).append(s)
    return out


def describe_pair(eden: Optional[SwitchSave], ryu: Optional[SwitchSave]) -> str:
    """``eden only`` | ``ryujinx only`` | ``same`` (the same files at the same sizes) | ``eden newer`` | ``ryujinx newer``."""
    if eden and not ryu:
        return "eden only"
    if ryu and not eden:
        return "ryujinx only"
    if not eden or not ryu:
        return ""
    if eden.listing == ryu.listing:
        return "same"
    return "eden newer" if eden.mtime >= ryu.mtime else "ryujinx newer"


# --------------------------------------------------------------------------- where the emulators keep things
def read_ini_value(path: Path, key: str) -> str:
    """``key=value`` from a Qt ``.ini`` (the first line with exactly that key), or ""."""
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == key:
                return v.strip()
    except OSError:
        pass
    return ""


def eden_game_dirs(ini: Path) -> List[str]:
    """The game folders in Eden's ``qt-config.ini`` (``Paths\\gamedirs\\N\\path``; SDMC / UserNAND / SysNAND are not folders)."""
    out: List[str] = []
    try:
        for line in Path(ini).read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and key.startswith("Paths\\gamedirs\\") and key.endswith("\\path") and value.strip() not in ("SDMC", "UserNAND", "SysNAND", ""):
                out.append(value.strip())
    except OSError:
        pass
    return out


def detect_eden(home: Optional[Path] = None) -> Dict[str, object]:
    """Eden's NAND and SD card folders and game folders from its ``qt-config.ini`` (or its default places); {} when it is not installed."""
    home = Path(home) if home else Path.home()
    cands = []           # (name, config folder, data folder)
    for name in ("eden", "yuzu", "sudachi", "suyu"):
        cands.append((name, home / ".config" / name, home / ".local" / "share" / name))
    for flatpak, name in (("dev.eden_emu.eden", "eden"), ("org.yuzu_emu.yuzu", "yuzu")):            # (the flatpaks keep their own XDG folders)
        base = home / ".var" / "app" / flatpak
        cands.append((name, base / "config" / name, base / "data" / name))
    appdata = os.environ.get("APPDATA")
    if appdata:                                                                                       # Windows: one folder holds config and data
        for name in ("eden", "yuzu", "sudachi", "suyu"):
            cands.append((name, Path(appdata) / name / "config", Path(appdata) / name))
    for name, conf, share in cands:
        ini = conf / "qt-config.ini"
        if ini.is_file() or share.is_dir():
            nand = read_ini_value(ini, "nand_directory") or str(share / "nand")
            sdmc = read_ini_value(ini, "sdmc_directory") or str(share / "sdmc")
            return {"app": name, "nand": nand, "sdmc": sdmc, "config": str(ini) if ini.is_file() else "", "games": eden_game_dirs(ini)}
    return {}


def detect_ryujinx(home: Optional[Path] = None, near: Iterable[Path] = ()) -> Dict[str, object]:
    """Ryujinx's data folder and the game folders its ``Config.json`` lists.

    A **portable** Ryujinx keeps everything in one folder (``bis``, ``Config.json``, ``games`` ...) wherever the user put it, so the
    folders in ``near`` (the games folder, say) and their parents up to four levels are looked at first: the first one that holds a
    ``bis`` folder is the answer. Otherwise the standard place, ``~/.config/Ryujinx``."""
    def read(base: Path, portable: bool) -> Dict[str, object]:
        games: List[str] = []
        try:
            doc = json.loads((base / "Config.json").read_text(encoding="utf-8"))
            games = [g for g in doc.get("game_dirs", []) if isinstance(g, str)]
        except (OSError, ValueError):
            pass
        return {"data": str(base), "games": games, "portable": portable}

    for folder in near:
        folder = Path(folder)
        for up in [folder, *folder.parents][:5]:
            if (up / "bis" / "user").is_dir() or (up / "bis" / "system").is_dir():
                return read(up, True)
    home = Path(home) if home else Path.home()
    places = [home / ".config" / "Ryujinx"]
    if os.environ.get("APPDATA"):
        places.append(Path(os.environ["APPDATA"]) / "Ryujinx")                                        # Windows
    for base in places:
        if (base / "Config.json").is_file() or (base / "bis").is_dir():
            return read(base, False)
    return {}
