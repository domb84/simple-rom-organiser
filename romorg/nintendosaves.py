"""The save data of Dolphin (Wii and GameCube) and Cemu (Wii U), found and keyed by game.

* **Dolphin, Wii**: ``<user>/Wii/title/00010000/<hex of the 4-character game ID>/data/`` (``RMGE`` is ``524d4745``). One save per game.
* **Dolphin, GameCube**: ``<user>/GC/<region>/Card A/*.gci`` (and ``Card B``). A ``.gci`` is one save file; its header starts with the
  4-character game code and the 2-character maker code. ``*.gci.deleted`` is Dolphin's trash; a raw memory card image (``*.raw``,
  ``*.mcd``) is reported as one card, it holds the saves of many games.
* **Cemu, Wii U**: ``<mlc01>/usr/save/00050000/<low title ID>/user/<80000001 | common>/``. ``80000001`` is the first account's save,
  ``common`` is shared by all accounts; each non-empty one is a save game. The full title ID is ``00050000`` + the folder name.

The game ID / title ID joins them with the game files (``nintendodisc``, Cemu's ``title_list_cache.xml``).
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import winproc

__all__ = ["NinSave", "detect_dolphin", "detect_cemu", "find_dolphin_wii", "find_dolphin_gc", "find_cemu", "read_title_list",
           "read_gci_header", "dolphin_game_dirs", "cemu_game_dirs"]

_HEX8 = re.compile(r"^[0-9a-fA-F]{8}$")


@dataclass
class NinSave:
    emulator: str                       # dolphin | cemu
    console: str                        # wii | gc | wiiu
    key: str                            # Wii / GC: the 4-character game code; Wii U: the title ID (16 hex)
    kind: str                           # save | gci | memory card | profile | common
    path: Path
    files: int = 0
    bytes: int = 0
    mtime: float = 0.0
    note: str = ""                      # a GCI's file name, a card's region ...
    region: str = ""

    def public(self) -> Dict[str, object]:
        return {"emulator": self.emulator, "console": self.console, "key": self.key, "kind": self.kind, "path": str(self.path),
                "files": self.files, "bytes": self.bytes, "mtime": self.mtime, "note": self.note, "region": self.region}


def _measure(folder: Path) -> tuple[int, int, float]:
    files = total = 0
    newest = 0.0
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            try:
                st = (Path(dirpath) / name).stat()
            except OSError:
                continue
            files += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return files, total, newest


# --------------------------------------------------------------------------- Dolphin
def find_dolphin_wii(user: Path) -> List[NinSave]:
    """The Wii saves under ``<user>/Wii/title/00010000``: one per game ID that has files in ``data``."""
    base = Path(user) / "Wii" / "title" / "00010000"
    out: List[NinSave] = []
    try:
        folders = sorted(p for p in base.iterdir() if p.is_dir() and _HEX8.match(p.name))
    except OSError:
        return out
    for folder in folders:
        try:
            code = bytes.fromhex(folder.name).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            continue
        if not code.isprintable() or not code.strip():
            continue
        files, total, newest = _measure(folder / "data")
        if files:
            out.append(NinSave("dolphin", "wii", code.upper(), "save", folder / "data", files, total, newest))
    return out


def read_gci_header(path: Path) -> Optional[tuple[str, str, str]]:
    """``(game code, maker code, save file name)`` from the header of a ``.gci`` (None when it is no GCI)."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x28)
    except OSError:
        return None
    if len(head) < 0x28:
        return None
    code = head[:4].decode("ascii", errors="replace")
    maker = head[4:6].decode("ascii", errors="replace")
    name = head[8:0x28].split(b"\0")[0].decode("shift_jis", errors="replace")
    return (code, maker, name) if code.isprintable() and code.strip() else None


def find_dolphin_gc(user: Path) -> List[NinSave]:
    """The GameCube saves under ``<user>/GC/<region>/Card A|B``: one entry per ``.gci``, one per raw memory card image."""
    base = Path(user) / "GC"
    out: List[NinSave] = []
    try:
        regions = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return out
    for region in regions:
        try:
            cards = sorted(p for p in region.iterdir() if p.is_dir() and p.name.lower().startswith("card"))
            images = sorted(p for p in region.iterdir() if p.is_file() and p.suffix.lower() in (".raw", ".mcd", ".mci"))
        except OSError:
            continue
        for card in cards:
            for gci in sorted(card.iterdir()):
                if not gci.is_file() or gci.suffix.lower() != ".gci":
                    continue                                                  # (".gci.deleted" is Dolphin's trash)
                head = read_gci_header(gci)
                if head is None:
                    continue
                st = gci.stat()
                out.append(NinSave("dolphin", "gc", head[0].upper(), "gci", gci, 1, st.st_size, st.st_mtime,
                                   note=f"{head[2]} ({card.name})", region=region.name))
        for image in images:
            st = image.stat()
            out.append(NinSave("dolphin", "gc", "", "memory card", image, 1, st.st_size, st.st_mtime, note=image.name, region=region.name))
    return out


def dolphin_game_dirs(config: Path) -> List[str]:
    """The game folders in Dolphin's ``Dolphin.ini`` (``ISOPath0`` ...)."""
    out: List[str] = []
    try:
        for line in Path(config).read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().startswith("ISOPath") and key.strip() != "ISOPaths" and value.strip():
                out.append(value.strip())
    except OSError:
        pass
    return out


def detect_dolphin(home: Optional[Path] = None) -> Dict[str, object]:
    """Dolphin's user folder (flatpak, or native) and its game folders; {} when it is not installed."""
    home = Path(home) if home else Path.home()
    cands = [(home / ".var" / "app" / "org.DolphinEmu.dolphin-emu" / "data" / "dolphin-emu",
              home / ".var" / "app" / "org.DolphinEmu.dolphin-emu" / "config" / "dolphin-emu" / "Dolphin.ini"),
             (home / ".local" / "share" / "dolphin-emu", home / ".config" / "dolphin-emu" / "Dolphin.ini"),
             (home / ".dolphin-emu", home / ".dolphin-emu" / "Config" / "Dolphin.ini")]
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands.append((Path(appdata) / "Dolphin Emulator", Path(appdata) / "Dolphin Emulator" / "Config" / "Dolphin.ini"))
    for docs in winproc.documents_dirs():                    # Windows, older installs: "Documents\Dolphin Emulator" (wherever Documents is)
        cands.append((docs / "Dolphin Emulator", docs / "Dolphin Emulator" / "Config" / "Dolphin.ini"))
    for data, ini in cands:
        if (data / "Wii").is_dir() or (data / "GC").is_dir() or ini.is_file():
            cfg = ini if ini.is_file() else data / "Config" / "Dolphin.ini"
            return {"data": str(data), "config": str(cfg) if cfg.is_file() else "", "games": dolphin_game_dirs(cfg)}
    return {}


# --------------------------------------------------------------------------- Cemu
def find_cemu(mlc: Path) -> List[NinSave]:
    """The Wii U saves under ``<mlc01>/usr/save/00050000``: one per non-empty account folder (``80000001``, ``common`` ...)."""
    base = Path(mlc) / "usr" / "save" / "00050000"
    out: List[NinSave] = []
    try:
        titles = sorted(p for p in base.iterdir() if p.is_dir() and _HEX8.match(p.name))
    except OSError:
        return out
    for title in titles:
        try:
            users = sorted(p for p in (title / "user").iterdir() if p.is_dir())
        except OSError:
            continue
        for user in users:
            files, total, newest = _measure(user)
            if files:
                out.append(NinSave("cemu", "wiiu", "00050000" + title.name.upper(), "common" if user.name == "common" else "profile",
                                   user, files, total, newest, note=user.name))
    return out


def read_title_list(path: Path) -> Dict[str, Dict[str, str]]:
    """Cemu's ``title_list_cache.xml``: the games it has seen, by title ID: ``{name, path, version, region}``."""
    out: Dict[str, Dict[str, str]] = {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return out
    for t in root.iter("title"):
        tid = (t.get("titleId") or "").upper()
        if len(tid) == 16:
            out[tid] = {"name": (t.findtext("name") or "").strip(), "path": (t.findtext("path") or "").strip(),
                        "version": t.get("version") or "", "region": (t.findtext("region") or "").strip()}
    return out


def cemu_game_dirs(settings: Path) -> List[str]:
    """The game folders in Cemu's ``settings.xml`` (``GamePaths``)."""
    try:
        root = ET.parse(settings).getroot()
    except (OSError, ET.ParseError):
        return []
    paths = root.find("GamePaths")
    return [e.text.strip() for e in (paths.iter("Entry") if paths is not None else ()) if e.text and e.text.strip()]


def detect_cemu(home: Optional[Path] = None) -> Dict[str, object]:
    """Cemu's data folder (flatpak, or native), its ``mlc01`` folder and its game folders; {} when it is not installed."""
    home = Path(home) if home else Path.home()
    cands = [(home / ".var" / "app" / "info.cemu.Cemu" / "data" / "Cemu", home / ".var" / "app" / "info.cemu.Cemu" / "config" / "Cemu"),
             (home / ".local" / "share" / "Cemu", home / ".config" / "Cemu")]
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands.append((Path(appdata) / "Cemu", Path(appdata) / "Cemu"))
    for data, conf in cands:
        if (data / "mlc01").is_dir() or (conf / "settings.xml").is_file():
            settings = conf / "settings.xml"
            mlc = data / "mlc01"
            try:
                text = ET.parse(settings).getroot().findtext("mlc_path") or ""
                if text.strip():
                    mlc = Path(text.strip())
            except (OSError, ET.ParseError):
                pass
            return {"data": str(data), "mlc": str(mlc), "settings": str(settings) if settings.is_file() else "",
                    "title_list": str(data / "title_list_cache.xml") if (data / "title_list_cache.xml").is_file() else "",
                    "games": cemu_game_dirs(settings)}
    return {}
