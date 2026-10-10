"""The key files some emulators need before they open anything: Eden / yuzu and Ryujinx (``prod.keys``, ``title.keys``) and Cemu
(``keys.txt``). They are searched for with the BIOS and firmware files: where each emulator expects them, whether the file there is
usable, and, when it is missing, where a copy lies in the folders searched (the other Switch emulator's folder, the ROM folders, the
downloads). A found file is *copied* into place (never moved, never over an existing file): they are the user's own keys.

Keys have no checksum to go by (each console has its own), so a file is told by its name and by what is in it: a ``prod.keys`` needs
the header key, a ``keys.txt`` at least one 32-digit hexadecimal key.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import nintendoapp, switchkeys

__all__ = ["Spec", "SPECS", "targets", "check", "place", "usable", "MAX_FILES"]

MAX_FILES = 200_000                 # a search of the folders stops after this many files (the answer says so)
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}\b")


@dataclass(frozen=True)
class Spec:
    emulator: str                   # "eden" | "ryujinx" | "cemu"
    label: str
    platform: str
    files: tuple                    # (name, required)


SPECS = (
    Spec("eden", "Eden / yuzu", "Nintendo Switch", (("prod.keys", True), ("title.keys", False))),
    Spec("ryujinx", "Ryujinx", "Nintendo Switch", (("prod.keys", True), ("title.keys", False))),
    Spec("cemu", "Cemu", "Nintendo Wii U", (("keys.txt", True),)),
)


def targets(nin_cfg: Dict[str, Dict[str, str]], switch_eff: Dict[str, Any]) -> Dict[str, Optional[Path]]:
    """The folder each emulator reads its keys from, for the emulators that are set up (None: not set up)."""
    out: Dict[str, Optional[Path]] = {"eden": None, "ryujinx": None, "cemu": None}
    if switch_eff.get("eden"):
        out["eden"] = Path(switch_eff["eden"]).parent / "keys"                       # <user folder>/nand -> <user folder>/keys
    if switch_eff.get("ryujinx"):
        out["ryujinx"] = Path(switch_eff["ryujinx"]) / "system"
    data = nintendoapp.effective("wiiu", nin_cfg)["data"].strip()
    if data:
        out["cemu"] = Path(data)
    return out


def usable(path: Path) -> bool:
    """Is it a key file the emulator can use (not an empty or foreign file with the right name)?"""
    try:
        if path.name.lower() == "keys.txt":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return any(_HEX32.match(line.strip()) for line in f.readlines()[:2000])
        keys = switchkeys.load_keys(path)
        return bool(keys.get("header_key")) if path.name.lower() == "prod.keys" else bool(keys) or path.stat().st_size > 0
    except (OSError, ValueError):
        return False


def _find(search_dirs: Iterable[Path], names: Iterable[str], progress: Optional[Callable[[str], None]]) -> tuple:
    """``({name: [paths]}, complete)``: the files with these names under ``search_dirs`` (exact name, any case)."""
    want = {n.casefold(): n for n in names}
    found: Dict[str, List[Path]] = {n: [] for n in want.values()}
    seen = 0
    for base in search_dirs:
        for dirpath, dirnames, files in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".git"))
            for f in files:
                seen += 1
                hit = want.get(f.casefold())
                if hit:
                    found[hit].append(Path(dirpath) / f)
            if seen > MAX_FILES:
                return found, False
            if progress and seen % 5000 < len(files):
                progress(f"Looking for key files ({seen:,} files)...")
    return found, True


def check(nin_cfg: Dict[str, Dict[str, str]], switch_eff: Dict[str, Any], search_dirs: Iterable[Path], home: Optional[Path] = None,
          progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """What each emulator that is set up expects, what is there and where a missing file lies.

    ``{"rows": [{emulator, label, platform, file, required, target, status: ok | invalid | found | missing, source}], "complete": bool}``
    """
    dirs = targets(nin_cfg, switch_eff)
    active = [s for s in SPECS if dirs.get(s.emulator)]
    names = {n for s in active for n, _r in s.files}
    home = Path(home) if home else Path.home()
    # the usual places first (one emulator's keys serve the other), then a walk of the folders given
    known: Dict[str, List[Path]] = {n: [] for n in names}
    for other in dirs.values():
        if other:
            for n in names:
                if (other / n).is_file():
                    known[n].append(other / n)
    for n in names:
        usual = switchkeys.find_prod_keys(home=home) if n == "prod.keys" else None
        if usual is not None and usual not in known[n]:
            known[n].append(usual)
    walked, complete = _find([Path(d) for d in search_dirs if Path(d).is_dir()] + [home / "Downloads"], names, progress) if names else ({}, True)
    rows: List[Dict[str, Any]] = []
    for s in active:
        for name, required in s.files:
            target = dirs[s.emulator] / name
            status, source = "missing", ""
            if target.is_file():
                status = "ok" if usable(target) else "invalid"
            else:
                for cand in known.get(name, []) + walked.get(name, []):
                    if cand != target and cand.is_file() and usable(cand):
                        status, source = "found", str(cand)
                        break
            rows.append({"emulator": s.emulator, "label": s.label, "platform": s.platform, "file": name, "required": required,
                         "target": str(target), "status": status, "source": source})
    return {"rows": rows, "complete": complete}


def place(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Copy the found key files into place (``rows`` from :func:`check`). Never over a file that is there."""
    res: Dict[str, Any] = {"placed": 0, "failed": [], "files": []}
    for r in rows:
        if r.get("status") != "found":
            continue
        src, dst = Path(r["source"]), Path(r["target"])
        try:
            if os.path.lexists(dst):
                raise FileExistsError("a file is already there")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            res["placed"] += 1
            res["files"].append(str(dst))
        except OSError as exc:
            res["failed"].append({"path": str(dst), "error": str(exc)})
    return res
