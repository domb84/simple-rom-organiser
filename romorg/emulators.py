"""The emulators whose saves the app looks at: one list for all of them, with the folder each one keeps its data in.

RetroArch is handled by ``retroarch`` (its own config file says where everything is); the others are listed here: which platforms
they play, where their data folder is (found on this machine unless the user chose one) and whether the user switched them off.
A platform whose emulators are all off or not found has no saves, and its pages show none.

The folders are stored where the platform pages already keep them (``nintendoapp`` for Dolphin, Cemu and PCSX2, ``switchapp`` for Eden
and Ryujinx), so there is one place per setting.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import nintendoapp, switchapp

__all__ = ["SOURCES", "KEYS", "describe", "store", "platforms_of", "enabled_for", "running", "source_running", "source_label"]

# key, label, platforms it plays, what the folder is
SOURCES = (
    {"key": "dolphin", "label": "Dolphin", "platforms": ["Nintendo GameCube", "Nintendo Wii"], "console": "wii",
     "what": "Dolphin's user folder (the one with Wii and GC)"},
    {"key": "cemu", "label": "Cemu", "platforms": ["Nintendo Wii U"], "console": "wiiu",
     "what": "Cemu's data folder (the one with mlc01)"},
    {"key": "pcsx2", "label": "PCSX2", "platforms": ["Sony PlayStation 2"], "console": "ps2",
     "what": "PCSX2's folder (the one with inis)"},
    {"key": "eden", "label": "Eden (or another yuzu fork)", "platforms": ["Nintendo Switch"], "console": "",
     "what": "Eden's NAND folder (the one with user/save)"},
    {"key": "ryujinx", "label": "Ryujinx", "platforms": ["Nintendo Switch"], "console": "",
     "what": "Ryujinx's data folder (the one with bis; a portable one is found next to the games)"},
)
KEYS = tuple(s["key"] for s in SOURCES)
_BY_KEY = {s["key"]: s for s in SOURCES}


def _off_list(switch: Dict[str, Any]) -> List[str]:
    return [x for x in str(switch.get("off") or "").split(",") if x]


def _is_off(key: str, nin: Dict[str, Dict[str, str]], switch: Dict[str, Any]) -> bool:
    src = _BY_KEY[key]
    return bool(nin[src["console"]].get("off")) if src["console"] else key in _off_list(switch)


def _detected(key: str, nin: Dict[str, Dict[str, str]], switch: Dict[str, Any]) -> str:
    """The folder found on this machine, "" when there is none."""
    from . import nintendosaves, pcsx2, switchsaves
    if key == "dolphin":
        return str(nintendosaves.detect_dolphin().get("data") or "")
    if key == "cemu":
        return str(nintendosaves.detect_cemu().get("data") or "")
    if key == "pcsx2":
        return str(pcsx2.detect_pcsx2().get("root") or "")
    if key == "eden":
        return str(switchsaves.detect_eden().get("nand") or "")
    games = switch.get("games") or ""
    return str(switchsaves.detect_ryujinx(near=[Path(games)] if games.strip() else []).get("data") or "")


def _folder(key: str, nin: Dict[str, Dict[str, str]], switch: Dict[str, Any]) -> Dict[str, Any]:
    """``{folder, auto}``: the folder the app uses and whether it was found (the user chose none)."""
    src = _BY_KEY[key]
    if src["console"]:
        chosen = nin[src["console"]]["data"]
    else:
        chosen = str(switch.get(key) or "")
    if chosen.strip():
        return {"folder": chosen, "auto": False}
    found = _detected(key, nin, switch)
    return {"folder": found, "auto": bool(found)}


def describe(nin: Dict[str, Dict[str, str]], switch: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every emulator the app knows, for the Emulators page."""
    out: List[Dict[str, Any]] = []
    for src in SOURCES:
        key = src["key"]
        f = _folder(key, nin, switch)
        off = _is_off(key, nin, switch)
        out.append({"key": key, "label": src["label"], "platforms": list(src["platforms"]), "what": src["what"],
                    "folder": f["folder"], "auto": f["auto"], "enabled": not off,
                    "found": bool(f["folder"]) and os.path.isdir(f["folder"]),
                    "active": not off and bool(f["folder"]) and os.path.isdir(f["folder"])})
    return out


def store(key: str, nin: Dict[str, Dict[str, str]], switch: Dict[str, Any], folder: Any = None, enabled: Any = None) -> None:
    """Remember a folder the user typed (a folder that is the one found anyway stays automatic) and/or the on/off switch."""
    if key not in _BY_KEY:
        raise KeyError(key)
    src = _BY_KEY[key]
    if folder is not None:
        if src["console"]:
            nintendoapp.store(src["console"], "data", str(folder), nin)
        else:
            switchapp.store(key, str(folder), switch)
    if enabled is not None:
        if src["console"]:
            nin[src["console"]]["off"] = "" if enabled else "1"
        else:
            off = [k for k in _off_list(switch) if k != key]
            if not enabled:
                off.append(key)
            switch["off"] = ",".join(off)


def platforms_of(key: str) -> List[str]:
    return list(_BY_KEY[key]["platforms"])


def enabled_for(platform: str, nin: Dict[str, Dict[str, str]], switch: Dict[str, Any]) -> List[str]:
    """The emulators (keys) that are on, have a folder that exists and play ``platform``: where its saves come from besides RetroArch."""
    return [e["key"] for e in describe(nin, switch) if e["active"] and platform in e["platforms"]]


# the process each emulator runs as: the start of its name on Linux (/proc/<pid>/comm is cut at 15 characters), the image on Windows
_PROCESS = {
    "dolphin": (("dolphin-emu",), ("dolphin.exe",)),
    "cemu": (("cemu",), ("cemu.exe",)),
    "pcsx2": (("pcsx2",), ("pcsx2-qt.exe", "pcsx2.exe")),
    "eden": (("eden", "yuzu", "sudachi", "suyu"), ("eden.exe", "yuzu.exe", "sudachi.exe", "suyu.exe")),
    "ryujinx": (("ryujinx",), ("ryujinx.exe",)),
}
# the save source of a save set -> the emulator whose process must be closed before its files are moved
_SOURCE_EMULATOR = {"dolphin": "dolphin", "cemu": "cemu", "pcsx2": "pcsx2", "switch": "eden"}


def running(key: str) -> bool:
    """True while the emulator ``key`` runs (an emulator writes to its save files, and keeps them open, while it plays)."""
    if key not in _PROCESS:
        return False
    names, images = _PROCESS[key]
    try:
        if sys.platform.startswith("linux"):
            for d in Path("/proc").iterdir():
                if d.name.isdigit():
                    try:
                        if (d / "comm").read_text().strip().lower().startswith(names):
                            return True
                    except OSError:
                        continue
            return False
        if sys.platform == "win32":
            from . import retroarch
            return any(str(name).lower() in images for _pid, name in retroarch._win_processes())
        return subprocess.run(["pgrep", "-i", "-f", names[0]], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError, AttributeError, ImportError):
        return False


def source_running(source: str) -> bool:
    """True when the program that owns the saves of a save set's ``source`` runs (RetroArch or one of the emulators here)."""
    if source == "retroarch":
        from . import retroarch
        return retroarch.is_running()
    return running(_SOURCE_EMULATOR.get(source, ""))


def source_label(source: str) -> str:
    return {"retroarch": "RetroArch", "dolphin": "Dolphin", "cemu": "Cemu", "pcsx2": "PCSX2", "switch": "Eden"}.get(source, source)
