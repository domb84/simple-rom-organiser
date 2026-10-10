"""The Switch's settings: where the two emulators keep their data (chosen, or found on this machine), the keys and the option that
makes a scan check every file's checksums. The games folder is the system's own folder, like any other system's.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import switchdb, switchkeys, switchsaves

__all__ = ["header_key", "normalise", "effective", "store", "describe"]

TEXT_KEYS = ("games", "eden", "ryujinx", "keys", "archive", "off")


def normalise(raw: Any) -> Dict[str, Any]:
    """The saved Switch settings: ``games`` (the folder with the game files), ``eden`` (its NAND folder), ``ryujinx`` (its data
    folder), ``keys`` (a ``prod.keys``, optional) and ``off`` (the emulators switched off). The games folder is the system's own folder
    (the platform's); what was set here before is only a fallback."""
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {k: str(raw.get(k) or "") for k in TEXT_KEYS}
    out["verify_scan"] = bool(raw.get("verify_scan"))             # a scan also checks every file's checksums (reads all the files)
    return out


def _path(text: str) -> Optional[Path]:
    return Path(os.path.abspath(os.path.expanduser(text))) if text.strip() else None


def effective(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The settings as they are used: what the user chose, and for what they left empty what was found on this machine - Eden's NAND
    folder, a games folder from Eden's or Ryujinx's own settings, and Ryujinx's data folder (a portable one next to the games first).
    ``auto`` names the values that were found."""
    out = dict(cfg)
    auto: List[str] = []
    eden = switchsaves.detect_eden()
    off = {x for x in out["off"].split(",") if x}                    # emulators switched off on the Emulators page
    if "eden" in off:
        out["eden"] = ""
    elif not out["eden"].strip() and eden.get("nand"):
        out["eden"] = str(eden["nand"])
        auto.append("eden")
    if not out["games"].strip():
        ryu0 = switchsaves.detect_ryujinx()
        for g in [*(eden.get("games") or []), *(ryu0.get("games") or [])]:
            if Path(g).is_dir():
                out["games"] = g
                auto.append("games")
                break
    if "ryujinx" in off:
        out["ryujinx"] = ""
    elif not out["ryujinx"].strip():
        ryu = switchsaves.detect_ryujinx(near=[Path(out["games"])] if out["games"].strip() else [])
        if ryu.get("data"):
            out["ryujinx"] = str(ryu["data"])
            auto.append("ryujinx")
    out["auto"] = auto
    return out


def store(key: str, value: str, cfg: Dict[str, Any]) -> None:
    """Remember what the user typed; a value that is the one found anyway is stored as "" (it stays automatic)."""
    cfg[key] = value
    if value and key in ("games", "eden", "ryujinx"):
        eff = effective({**cfg, key: ""})
        if key in eff["auto"] and os.path.normcase(os.path.normpath(eff[key])) == os.path.normcase(os.path.normpath(value)):
            cfg[key] = ""


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The settings (as used), what was found on this machine, the title database and the keys."""
    eff = effective(cfg)
    keys = _path(cfg["keys"])
    found = switchkeys.find_prod_keys()
    shown = {k: v for k, v in eff.items() if k != "auto"}
    return {"config": shown, "auto": eff["auto"],
            "detected": {"eden": switchsaves.detect_eden(), "ryujinx": switchsaves.detect_ryujinx(near=[Path(eff["games"])] if eff["games"].strip() else [])},
            "db": switchdb.db_info(),
            "keys": {"path": str(keys) if keys and keys.is_file() else (str(found) if found else ""),
                     "chosen": bool(keys and keys.is_file())}}


def header_key(cfg: Dict[str, Any]) -> Optional[bytes]:
    keys_path = _path(cfg["keys"])
    if keys_path is None or not keys_path.is_file():
        keys_path = switchkeys.find_prod_keys()
    return switchkeys.load_keys(keys_path).get("header_key") if keys_path else None
