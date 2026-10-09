"""Where Dolphin, Cemu and PCSX2 keep their data: the folder the user chose, or the one found on this machine.

One entry per emulator (stored as ``wii`` = Dolphin, ``wiiu`` = Cemu, ``ps2`` = PCSX2 in config.json, as before): ``data`` is its
folder - Dolphin's user folder, Cemu's data folder (the one with ``mlc01``), PCSX2's folder (the one with ``inis``) -, empty meaning
"the one that was found", and ``off`` switches the emulator off (its saves are then not looked at). ``games`` is a leftover of the
pages that this replaced (the platforms have their own folders now). ``emulators`` is the page for them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import nintendosaves, pcsx2

__all__ = ["CONSOLES", "normalise", "effective", "store"]

CONSOLES = ("wii", "wiiu", "ps2")


def normalise(raw: Any) -> Dict[str, Dict[str, str]]:
    """The saved settings: for each console ``games`` (the folder with the discs) and ``data`` (Dolphin's user folder / Cemu's data
    folder, the one with ``mlc01`` / PCSX2's folder, the one with ``inis``). An empty value means "the one that was found"."""
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, Dict[str, str]] = {}
    for c in CONSOLES:
        entry = raw.get(c) if isinstance(raw.get(c), dict) else {}
        out[c] = {"games": str(entry.get("games") or ""), "data": str(entry.get("data") or ""), "off": "1" if entry.get("off") else ""}
    return out


def _path(text: str) -> Optional[Path]:
    return Path(os.path.abspath(os.path.expanduser(text))) if text.strip() else None


def _detect(console: str, data: Optional[Path] = None) -> Dict[str, Any]:
    if console == "wii":
        return nintendosaves.detect_dolphin()
    if console == "wiiu":
        return nintendosaves.detect_cemu()
    return pcsx2.detect_pcsx2(root=data)


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)).rstrip("/\\") == os.path.normcase(os.path.normpath(b)).rstrip("/\\")


def effective(console: str, cfg: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """The settings as they are used: what the user chose, and for what they left empty what was found on this machine (the
    emulator's own folder, its first game folder that exists). ``auto`` says which values were found."""
    c = dict(cfg[console])
    det = _detect(console)
    auto: List[str] = []
    if c.get("off"):                                    # the emulator is switched off on the Emulators page: its folder is not used
        c["data"] = ""
    elif not c["data"].strip() and det.get("root" if console == "ps2" else "data"):
        c["data"] = str(det["root" if console == "ps2" else "data"])
        auto.append("data")
    if not c["games"].strip():
        for g in (_detect(console, _path(c["data"])).get("games") if console == "ps2" and c["data"] else det.get("games")) or []:
            if Path(g).is_dir():
                c["games"] = g
                auto.append("games")
                break
    return {**c, "auto": auto}


def store(console: str, key: str, value: str, cfg: Dict[str, Dict[str, str]]) -> None:
    """Remember a folder the user typed; the value that was found anyway is stored as "" (it stays automatic)."""
    det = _detect(console)
    found = det.get("root" if console == "ps2" else "data") if key == "data" else (det.get("games") or [""])[0]
    cfg[console][key] = "" if value and found and _same(value, str(found)) else value
