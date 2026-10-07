"""RetroArch: find the installs, read and write ``retroarch.cfg``, and move the save files and save states to where
RetroArch will look for them.

Everything here is optional: with no RetroArch found (and none chosen) the rest of the app does not care. Nothing is
written without a backup of ``retroarch.cfg``, and files are only ever moved, never overwritten or deleted.

Layout facts (RetroArch): ``savefile_directory`` / ``savestate_directory`` (empty = next to the content), with
``sort_savefiles_enable`` / ``sort_savestates_enable`` putting each core's files in a sub-folder named after the core.
A save file is ``<content name>.srm`` (or .sav, .rtc ...); a state is ``<content name>.state``, ``.state1`` ...,
``.state.auto``, and a thumbnail adds ``.png``."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

__all__ = ["Install", "detect_installs", "read_cfg", "resolve", "settings_of", "is_running", "write_cfg",
           "classify", "plan_relocation", "apply_relocation", "undo_relocation", "list_undo", "override_warnings",
           "SETTING_KEYS", "split_save", "pairs_from_moves", "plan_follow", "apply_follow", "core_infos",
           "cores_for_platform", "cores_for_platforms", "check_bios_cores", "check_bios", "apply_bios", "shared_base", "shared_folders", "apply_shared"]

SETTING_KEYS = ("savefile_directory", "savestate_directory", "sort_savefiles_enable", "sort_savestates_enable",
                "sort_savefiles_by_content_enable", "sort_savestates_by_content_enable", "savefiles_in_content_dir",
                "savestates_in_content_dir", "system_directory", "rgui_browser_directory")
_STATE_RE = re.compile(r"\.state(\d*|\.auto)(\.png)?$", re.IGNORECASE)
_LINE_RE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=\s*"?(.*?)"?\s*$')
BACKUP_SUFFIX = ".romorg-backup-"


@dataclass
class Install:
    id: str
    kind: str                 # steam | flatpak | native | windows | portable | custom
    label: str
    cfg: Path
    base: Path                # what ":" means in the config (the folder that holds the config / the program)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "cfg": str(self.cfg), "base": str(self.base)}


# --------------------------------------------------------------------------- finding installs
def _steam_libraries(roots: Iterable[Path]) -> List[Path]:
    out: List[Path] = []
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if root.is_dir() and root not in out:
            out.append(root)
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(m.group(1).replace("\\\\", "\\"))
            if p not in out:
                out.append(p)
    return out


def detect_installs(home: Optional[Path] = None, env: Optional[dict] = None, platform: Optional[str] = None,
                    custom: Iterable[str] = ()) -> List[Install]:
    """Every RetroArch whose ``retroarch.cfg`` can be found, plus the custom ones (a path to a cfg or its folder)."""
    home = Path(home) if home else Path.home()
    env = dict(os.environ if env is None else env)
    platform = platform or sys.platform
    found: List[Install] = []
    seen: set = set()

    def add(kind: str, label: str, cfg: Path, base: Optional[Path] = None) -> None:
        try:
            if not cfg.is_file():
                return
            key = os.path.normcase(os.path.realpath(cfg))
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        found.append(Install(f"{kind}:{len(found)}", kind, label, cfg, base or cfg.parent))

    if platform.startswith("linux"):
        steam_roots = [home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root"]
        for lib in _steam_libraries(steam_roots):
            d = lib / "steamapps" / "common" / "RetroArch"
            add("steam", "RetroArch (Steam)", d / "retroarch.cfg", d)
        add("flatpak", "RetroArch (Flatpak)", home / ".var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg")
        add("native", "RetroArch", Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "retroarch" / "retroarch.cfg")
        add("native", "RetroArch (Snap)", home / "snap/retroarch/current/.config/retroarch/retroarch.cfg")
    elif platform == "win32":
        appdata = Path(env.get("APPDATA") or home / "AppData/Roaming")
        add("windows", "RetroArch", appdata / "RetroArch" / "retroarch.cfg")
        steam_roots = [Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
                       Path(env.get("ProgramFiles", r"C:\Program Files")) / "Steam"]
        for lib in _steam_libraries(steam_roots):
            d = lib / "steamapps" / "common" / "RetroArch"
            add("steam", "RetroArch (Steam)", d / "retroarch.cfg", d)
        for d in (Path(r"C:\RetroArch-Win64"), Path(r"C:\RetroArch"), home / "RetroArch", home / "Desktop" / "RetroArch-Win64"):
            add("portable", f"RetroArch ({d})", d / "retroarch.cfg", d)
    elif platform == "darwin":
        add("native", "RetroArch", home / "Library/Application Support/RetroArch/config/retroarch.cfg",
            home / "Library/Application Support/RetroArch")
    for raw in custom:
        p = Path(os.path.expanduser(str(raw)))
        cfg = p if p.is_file() else p / "retroarch.cfg"
        add("custom", f"RetroArch ({cfg.parent})", cfg)
    return found


# --------------------------------------------------------------------------- reading
def read_cfg(cfg: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = Path(cfg).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            out[m.group(1)] = m.group(2)
    return out


def resolve(value: str, install: Install, home: Optional[Path] = None) -> Optional[Path]:
    """The folder a config value stands for: ``~`` is the home folder, ``:`` the install's base folder; ``""`` and
    ``default`` mean "RetroArch decides" (None)."""
    v = (value or "").strip()
    if not v or v == "default":
        return None
    home = Path(home) if home else Path.home()
    if v.startswith("~"):
        return home / v[1:].lstrip("/\\")
    if v.startswith(":"):
        return install.base / v[1:].lstrip("/\\")
    return Path(v)


def _bool(v: Optional[str]) -> bool:
    return str(v).strip().lower() == "true"


def settings_of(install: Install, home: Optional[Path] = None) -> dict:
    """The save-related settings, read and resolved."""
    cfg = read_cfg(install.cfg)
    out: Dict[str, Any] = {"cfg": str(install.cfg)}
    for key in SETTING_KEYS:
        raw = cfg.get(key, "")
        out[key] = _bool(raw) if key.endswith("_enable") or key.endswith("_in_content_dir") else raw
    out["savefile_path"] = _p(resolve(cfg.get("savefile_directory", ""), install, home))
    out["savestate_path"] = _p(resolve(cfg.get("savestate_directory", ""), install, home))
    out["system_path"] = _p(resolve(cfg.get("system_directory", ""), install, home))
    out["content_path"] = _p(resolve(cfg.get("rgui_browser_directory", ""), install, home))
    out["raw"] = {k: cfg.get(k, "") for k in SETTING_KEYS}
    return out


def _p(path: Optional[Path]) -> str:
    return str(path) if path else ""


def is_running() -> bool:
    """True while a RetroArch process runs (it rewrites ``retroarch.cfg`` when it closes, undoing any edit)."""
    try:
        if sys.platform.startswith("linux"):
            for d in Path("/proc").iterdir():
                if d.name.isdigit():
                    try:
                        if (d / "comm").read_text().strip().lower().startswith("retroarch"):
                            return True
                    except OSError:
                        continue
            return False
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq retroarch.exe"], capture_output=True, text=True,
                                 timeout=10).stdout
            return "retroarch.exe" in out.lower()
        out = subprocess.run(["pgrep", "-i", "retroarch"], capture_output=True, timeout=10)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def override_warnings(install: Install, home: Optional[Path] = None) -> List[str]:
    """Core / game override files (``config/<core>/*.cfg``) that set their own save or state folder."""
    out: List[str] = []
    base = install.base / "config"
    try:
        files = sorted(base.rglob("*.cfg"))
    except OSError:
        return out
    for f in files[:500]:
        cfg = read_cfg(f)
        hit = [k for k in ("savefile_directory", "savestate_directory", "sort_savefiles_enable",
                           "sort_savestates_enable") if k in cfg]
        if hit:
            out.append(f"{f.relative_to(base).as_posix()} sets {', '.join(hit)}")
    return out


# --------------------------------------------------------------------------- writing the config
def _cfg_text(value: Any) -> str:
    return "true" if value is True else "false" if value is False else str(value)


def to_cfg_path(path: Path, home: Optional[Path] = None) -> str:
    """How a folder is written in ``retroarch.cfg``: ``~/...`` under the home folder (as RetroArch itself does), else
    absolute."""
    home = Path(home) if home else Path.home()
    try:
        rel = path.relative_to(home)
        if os.name != "nt":
            return "~/" + rel.as_posix() if rel.parts else "~"
    except ValueError:
        pass
    return str(path)


def write_cfg(cfg: Path, changes: Dict[str, Any]) -> Path:
    """Set the keys in ``retroarch.cfg`` (other lines untouched) after copying it to ``retroarch.cfg.romorg-backup-<time>``.
    Returns the backup path."""
    cfg = Path(cfg)
    text = cfg.read_text(encoding="utf-8", errors="surrogateescape")
    backup = cfg.with_name(cfg.name + BACKUP_SUFFIX + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(cfg, backup)
    lines = text.split("\n")
    pending = dict(changes)
    for i, line in enumerate(lines):
        m = _LINE_RE.match(line)
        if m and m.group(1) in pending and not line.lstrip().startswith("#"):
            lines[i] = f'{m.group(1)} = "{_cfg_text(pending.pop(m.group(1)))}"'
    if pending:
        if lines and lines[-1] == "":
            lines.pop()
        lines += [f'{k} = "{_cfg_text(v)}"' for k, v in pending.items()] + [""]
    tmp = cfg.with_name(cfg.name + ".romorg.part")
    tmp.write_text("\n".join(lines), encoding="utf-8", errors="surrogateescape")
    os.replace(tmp, cfg)
    return backup


# --------------------------------------------------------------------------- moving saves
def classify(name: str) -> str:
    """``state`` for a save state (or its thumbnail), else ``save``."""
    return "state" if _STATE_RE.search(name) else "save"


def _walk(root: Path, skip: Iterable[Path] = ()) -> Iterable[Path]:
    skip_set = {os.path.normcase(os.path.realpath(s)) for s in skip}
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        here = os.path.normcase(os.path.realpath(dirpath))
        dirnames[:] = [d for d in dirnames if os.path.normcase(os.path.realpath(os.path.join(dirpath, d))) not in skip_set
                       and not d.startswith(".")]
        for f in filenames:
            if not f.startswith(".") and not f.endswith(".romorg.part"):
                yield Path(dirpath) / f


@dataclass
class Move:
    src: Path
    dst: Path
    kind: str                  # save | state
    status: str = "move"       # move | ok | conflict | needs_core
    note: str = ""
    size: int = 0


@dataclass
class Relocation:
    moves: List[Move] = field(default_factory=list)
    changes: Dict[str, Any] = field(default_factory=dict)       # retroarch.cfg keys to set
    old: dict = field(default_factory=dict)
    new: dict = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def counts(self) -> dict:
        out = {"move": 0, "ok": 0, "conflict": 0, "needs_core": 0}
        for m in self.moves:
            out[m.status] = out.get(m.status, 0) + 1
        return out

    def bytes_to_move(self) -> int:
        return sum(m.size for m in self.moves if m.status in ("move", "needs_core") and m.src != m.dst)


def plan_relocation(install: Install, save_dir: Path, state_dir: Path, sort_saves: bool, sort_states: bool,
                    home: Optional[Path] = None) -> Relocation:
    """Where every existing save file and state goes when RetroArch is set to the given folders and sorting.

    A file keeps its position inside the old tree. Turning "one folder per core" OFF flattens each core folder. Turning
    it ON cannot work out the core of a flat file: those files are moved as they are and reported (``needs_core``)."""
    cur = settings_of(install, home)
    old_save = Path(cur["savefile_path"]) if cur["savefile_path"] else None
    old_state = Path(cur["savestate_path"]) if cur["savestate_path"] else None
    if cur["savefiles_in_content_dir"] or cur["savestates_in_content_dir"] or old_save is None or old_state is None:
        raise ValueError("RetroArch keeps its saves next to the games (or uses its default folder) right now. Choose the "
                         "folders in RetroArch first, or tell the app where the saves are.")
    rel = Relocation(old={"save": str(old_save), "state": str(old_state), "sort_saves": cur["sort_savefiles_enable"],
                          "sort_states": cur["sort_savestates_enable"]},
                     new={"save": str(save_dir), "state": str(state_dir), "sort_saves": sort_saves, "sort_states": sort_states})
    rel.changes = {"savefile_directory": to_cfg_path(save_dir, home), "savestate_directory": to_cfg_path(state_dir, home),
                   "sort_savefiles_enable": sort_saves, "sort_savestates_enable": sort_states,
                   "savefiles_in_content_dir": False, "savestates_in_content_dir": False}
    old_sorted = {"save": bool(cur["sort_savefiles_enable"]), "state": bool(cur["sort_savestates_enable"])}
    new_sorted = {"save": sort_saves, "state": sort_states}
    new_root = {"save": Path(save_dir), "state": Path(state_dir)}
    roots = {os.path.normcase(os.path.realpath(old_save)): old_save, os.path.normcase(os.path.realpath(old_state)): old_state}
    claimed: Dict[str, Path] = {}
    for root in roots.values():
        # a new folder inside the old one (e.g. saves/ -> saves/states) is not scanned as if it held old files
        skip = [d for d in new_root.values()
                if os.path.normcase(os.path.realpath(d)) != os.path.normcase(os.path.realpath(root))
                and os.path.normcase(os.path.realpath(d)).startswith(os.path.normcase(os.path.realpath(root)) + os.sep)]
        for f in _walk(root, skip):
            kind = classify(f.name)
            if old_save != old_state:
                # separate old folders: a save file is only expected under the save folder, a state under the state folder
                if (kind == "state") != (root == old_state):
                    continue
            r = f.relative_to(root)
            parts = list(r.parts)
            status, note = "move", ""
            if old_sorted[kind] and not new_sorted[kind] and len(parts) > 1:
                parts = parts[1:]                                        # flatten: drop the core folder
            elif not old_sorted[kind] and new_sorted[kind]:
                status, note = "needs_core", "RetroArch will look for it in its core's folder; the core is not known"
            dst = new_root[kind].joinpath(*parts)
            if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(f)):
                rel.moves.append(Move(f, dst, kind, "ok" if status == "move" else status, note))
                continue
            key = os.path.normcase(str(dst)).casefold()
            if os.path.lexists(dst) or key in claimed:
                status, note = "conflict", "a file with that name is already there - left where it is"
            claimed.setdefault(key, f)
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            rel.moves.append(Move(f, dst, kind, status, note, size))
    if old_sorted["save"] != sort_saves or old_sorted["state"] != sort_states:
        if sort_saves or sort_states:
            rel.notes.append("Turning on one folder per core: files that are not in a core folder yet stay flat and "
                             "RetroArch will not find them until they are in the right core's folder.")
    return rel


def _remove_empty(dirs: Iterable[Path], keep: Iterable[Path]) -> int:
    keep_set = {os.path.normcase(os.path.realpath(k)) for k in keep}
    removed = 0
    for d in sorted({Path(x) for x in dirs}, key=lambda p: len(p.parts), reverse=True):
        try:
            if os.path.normcase(os.path.realpath(d)) in keep_set:
                continue
            os.rmdir(d)
            removed += 1
        except OSError:
            pass
    return removed


def apply_relocation(install: Install, rel: Relocation, journal_dir: Path, backup_zip: Optional[Path] = None,
                     progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """Back up, move the files, remove the folders that were emptied, write ``retroarch.cfg``. Returns a summary and the
    undo journal's path. Refuses while RetroArch runs."""
    if is_running():
        raise RuntimeError("RetroArch is running. Close it first: it rewrites its config when it exits.")
    todo = [m for m in rel.moves if m.status in ("move", "needs_core") and m.src != m.dst]
    res: Dict[str, Any] = {"moved": 0, "failed": [], "removed_dirs": 0, "backup": None, "cfg_backup": None, "journal": None}
    journal_dir = Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    journal = journal_dir / f"saves-{stamp}.json"
    if backup_zip is not None and todo:
        backup_zip = Path(backup_zip)
        backup_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(backup_zip, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for i, m in enumerate(todo):
                z.write(m.src, arcname=f"{m.kind}/{i:05d}/{m.src.name}")
                if progress:
                    progress(i, len(todo) * 2, f"backing up {m.src.name}")
            z.writestr("manifest.json", json.dumps([{"i": i, "from": str(m.src), "name": m.src.name, "kind": m.kind}
                                                    for i, m in enumerate(todo)], indent=1))
        res["backup"] = str(backup_zip)
    done: List[dict] = []
    dirs: set = set()
    for i, m in enumerate(todo):
        if progress:
            progress(len(todo) + i, len(todo) * 2, f"moving {m.src.name}")
        try:
            m.dst.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(m.dst):
                raise FileExistsError("a file appeared there since the preview")
            try:
                os.link(m.src, m.dst)                       # never replaces; falls back to a plain rename below
                os.unlink(m.src)
            except OSError:
                if os.path.lexists(m.dst):
                    raise
                shutil.move(str(m.src), str(m.dst))
            done.append({"from": str(m.src), "to": str(m.dst)})
            dirs.add(m.src.parent)
            res["moved"] += 1
        except OSError as exc:
            res["failed"].append({"path": str(m.src), "error": str(exc)})
    res["removed_dirs"] = _remove_empty(dirs, keep=[Path(rel.new["save"]), Path(rel.new["state"]), install.base])
    old_roots = [Path(rel.old["save"]), Path(rel.old["state"])]
    res["removed_dirs"] += _remove_empty(old_roots, keep=[Path(rel.new["save"]), Path(rel.new["state"]), install.base])
    cfg_backup = None
    if not res["failed"]:
        cfg_backup = write_cfg(install.cfg, rel.changes)
        res["cfg_backup"] = str(cfg_backup)
    journal.write_text(json.dumps({"install": install.cfg.as_posix(), "moves": done, "cfg_backup": str(cfg_backup or ""),
                                   "old": rel.old, "new": rel.new, "undone": False}, indent=1), encoding="utf-8")
    res["journal"] = str(journal)
    return res


def list_undo(journal_dir: Path) -> List[dict]:
    out = []
    for p in sorted(Path(journal_dir).glob("saves-*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not d.get("undone"):
            out.append({"journal": str(p), "name": p.name, "files": len(d.get("moves", [])) or len(d.get("changes", {})),
                        "kind": d.get("kind", "relocate"), "at": p.stat().st_mtime})
    return out


def undo_relocation(journal: Path) -> dict:
    """Move the files back (only where the original place is free and the file is still where it was put) and restore
    ``retroarch.cfg`` from its backup when it has not changed since."""
    if is_running():
        raise RuntimeError("RetroArch is running. Close it first.")
    journal = Path(journal)
    d = json.loads(journal.read_text(encoding="utf-8"))
    restored, skipped, dirs = 0, [], set()
    for m in reversed(d.get("moves", [])):
        src, dst = Path(m["to"]), Path(m["from"])
        if m.get("copy"):                                   # a copy made by a build: remove it again if still the same
            try:
                if os.path.lexists(src) and os.path.getsize(src) == m.get("size", -1):
                    os.unlink(src)
                    restored += 1
                    dirs.add(src.parent)
                elif os.path.lexists(src):
                    skipped.append({"path": str(src), "reason": "changed since it was copied - left in place"})
            except OSError as exc:
                skipped.append({"path": str(src), "reason": str(exc)})
            continue
        if not os.path.lexists(src):
            skipped.append({"path": str(src), "reason": "no longer there"})
        elif os.path.lexists(dst):
            skipped.append({"path": str(dst), "reason": "a file is already at the original place"})
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                restored += 1
                dirs.add(src.parent)
            except OSError as exc:
                skipped.append({"path": str(src), "reason": str(exc)})
    cfg_restored = False
    cb = d.get("cfg_backup")
    if cb and Path(cb).is_file() and not skipped:
        shutil.copy2(cb, d["install"])
        cfg_restored = True
    old = d.get("old") or {}
    _remove_empty(dirs, keep=[Path(p) for p in (old.get("save"), old.get("state")) if p] + [Path(d["install"]).parent])
    if not skipped:
        d["undone"] = True
        journal.write_text(json.dumps(d, indent=1), encoding="utf-8")
    return {"restored": restored, "skipped": skipped, "cfg_restored": cfg_restored}


# --------------------------------------------------------------------------- saves follow the games
def split_save(name: str) -> Optional[tuple]:
    """``(content name, suffix, kind)`` of a save file or state file name, e.g. ``Mario (USA).state1.png`` ->
    ``("Mario (USA)", ".state1.png", "state")``; None when it has no extension."""
    m = re.match(r"^(.*?)(\.state(?:\d*|\.auto)(?:\.png)?)$", name, re.IGNORECASE)
    if m and m.group(1):
        return m.group(1), m.group(2), "state"
    if "." in name.strip("."):
        stem, ext = name.rsplit(".", 1)
        if stem and ext:
            return stem, "." + ext, "save"
    return None


def pairs_from_moves(moves: Iterable[tuple]) -> List[tuple]:
    """``(old content name, new content name)`` for every file whose name (without extension) changes."""
    out: Dict[str, str] = {}
    for src, dst in moves:
        a, b = Path(src).stem, Path(dst).stem
        if a and b and a != b:
            out.setdefault(a, b)
    return sorted(out.items())


def save_roots(install: Install, home: Optional[Path] = None) -> List[Path]:
    cur = settings_of(install, home)
    if cur["savefiles_in_content_dir"] or cur["savestates_in_content_dir"]:
        return []
    roots: List[Path] = []
    for key in ("savefile_path", "savestate_path"):
        if cur[key] and Path(cur[key]) not in roots and Path(cur[key]).is_dir():
            roots.append(Path(cur[key]))
    return roots


@dataclass
class Follow:
    src: Path
    dst: Path
    kind: str
    status: str = "move"      # move | copy | conflict
    note: str = ""
    size: int = 0


def plan_follow(install: Install, pairs: Iterable[tuple], mode: str = "move", home: Optional[Path] = None) -> List[Follow]:
    """Where saves and states go when games get new names: each file stays in its own folder (core folders are kept apart)
    and only the content name part changes. ``mode`` is ``move`` (the old name goes) or ``copy`` (both names stay)."""
    wanted = dict(pairs)
    out: List[Follow] = []
    if not wanted:
        return out
    claimed: set = set()
    for root in save_roots(install, home):
        for f in _walk(root):
            parts = split_save(f.name)
            if parts is None or parts[0] not in wanted:
                continue
            stem, suffix, kind = parts
            dst = f.with_name(wanted[stem] + suffix)
            status, note = mode, ""
            key = os.path.normcase(str(dst))
            if os.path.lexists(dst) or key in claimed:
                status, note = "conflict", "a file with the new name is already there - left alone"
            claimed.add(key)
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            out.append(Follow(f, dst, kind, status, note, size))
    return out


def apply_follow(ops: List[Follow], journal_dir: Path, extra: Optional[dict] = None, install: Optional[Install] = None) -> dict:
    """Do the renames / copies. Skipped (not an error) while RetroArch runs: it writes its saves back on exit."""
    todo = [o for o in ops if o.status in ("move", "copy")]
    res: Dict[str, Any] = {"followed": 0, "copied": 0, "failed": [], "skipped_running": False, "journal": None}
    if not todo:
        return res
    if is_running():
        res["skipped_running"] = True
        return res
    done: List[dict] = []
    for o in todo:
        try:
            if os.path.lexists(o.dst):
                raise FileExistsError("a file appeared there")
            if o.status == "copy":
                shutil.copy2(o.src, o.dst)
                res["copied"] += 1
            else:
                try:
                    os.link(o.src, o.dst)
                    os.unlink(o.src)
                except OSError:
                    if os.path.lexists(o.dst):
                        raise
                    shutil.move(str(o.src), str(o.dst))
                res["followed"] += 1
            done.append({"from": str(o.src), "to": str(o.dst), "copy": o.status == "copy", "size": o.size})
        except OSError as exc:
            res["failed"].append({"path": str(o.src), "error": str(exc)})
    if done:
        journal_dir = Path(journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        j = journal_dir / f"saves-{time.strftime('%Y%m%d-%H%M%S')}-follow.json"
        j.write_text(json.dumps({"install": str(install.cfg) if install else "", "kind": "follow", "moves": done,
                                 "cfg_backup": "", "undone": False, **(extra or {})}, indent=1), encoding="utf-8")
        res["journal"] = str(j)
    return res


# --------------------------------------------------------------------------- BIOS / firmware
GENERIC_EXTENSIONS = {"zip", "7z", "bin", "iso", "cue", "chd", "m3u", "dat", "img", "rom", "gz", "pbp", "mdf", "toc", "ccd", "nrg"}
MAX_BIOS_BYTES = 256 * 1024 * 1024


def _fold(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def read_info(path: Path) -> Dict[str, str]:
    return read_cfg(path)


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def core_infos(install: Install, home: Optional[Path] = None) -> List[dict]:
    """Every installed core's ``.info`` that lists firmware: ``{core, display, extensions, firmware:[{path, optional, md5}]}``."""
    cfg = read_cfg(install.cfg)
    dirs = [d for d in (resolve(cfg.get("libretro_info_path", ""), install, home), install.base / "cores", install.base / "info") if d]
    seen: set = set()
    out: List[dict] = []
    for d in dirs:
        if not d.is_dir() or d in seen:
            continue
        seen.add(d)
        for f in sorted(d.glob("*.info")):
            info = read_info(f)
            try:
                n = int(info.get("firmware_count", "0") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                continue
            md5s = {m.group(1).replace("\\", "/").split("/")[-1].casefold(): m.group(2).lower()
                    for m in re.finditer(r"\(!\)\s*([^|]*?)\s*\(md5\):\s*([0-9a-fA-F]{32})", info.get("notes", ""))}
            firmware = []
            for i in range(n):
                path = info.get(f"firmware{i}_path", "")
                if not path:
                    continue
                firmware.append({"path": path.replace("\\", "/"), "optional": info.get(f"firmware{i}_opt", "false").lower() == "true",
                                 "md5": md5s.get(path.replace("\\", "/").split("/")[-1].casefold(), ""),
                                 "desc": info.get(f"firmware{i}_desc", "")})
            out.append({"core": info.get("corename") or f.stem, "display": info.get("display_name", f.stem),
                        "extensions": [e for e in info.get("supported_extensions", "").lower().split("|") if e],
                        "firmware": firmware, "file": f.name})
    return out


def cores_for_platform(infos: List[dict], platform: Any) -> List[dict]:
    """The cores that play this system: the core's system name equals / starts with the system's name (not followed by a
    digit: PlayStation is not PlayStation 2), or it reads one of the system's own, non-generic file extensions."""
    pname = getattr(platform, "name", "")
    name = _fold(pname)
    maker = _fold(pname.split()[0]) if pname.split() else ""
    exts = {e.lstrip(".").lower() for e in getattr(platform, "extensions", ()) or ()} - GENERIC_EXTENSIONS
    out = []
    for c in infos:
        system = _fold(c["display"].split("(")[0].replace(" - ", " "))
        by_name = bool(name) and (system == name or (system.startswith(name) and not system[len(name):][:1].isdigit()))
        same_maker = bool(maker) and _fold(c["display"].split(" - ")[0]) == maker
        if by_name or (same_maker and exts and exts & set(c["extensions"])):
            out.append(c)
    return out


_BIOS_EXT = {"bin", "rom", "bios", "img", "sms", "gg", "a500", "a600", "a1200", "a4000", "cd32", "cdtv"}
_BIOS_WORDS = ("bios", "boot", "scph", "kick", "firmware", "flash", "sysrom", "fw")


def _outermost(dirs: Iterable[Path]) -> List[Path]:
    """The search folders without those that lie inside another one (no file is indexed twice)."""
    real = []
    for d in dirs:
        try:
            r = Path(os.path.realpath(d))
        except OSError:
            continue
        if r.is_dir() and r not in real:
            real.append(r)
    return [r for r in real if not any(o != r and (o in r.parents) for o in real)]


def _find_candidates(search_dirs: Iterable[Path], wanted: List[dict], limit_seconds: float = 90.0,
                     progress: Optional[Callable[[str], None]] = None) -> tuple:
    """For each wanted firmware path, a file in the search folders: the same name (verified by md5 when known), else a file
    with the right md5 under another name (only files that look like a BIOS: small, a BIOS-like extension or name).
    Returns ``(found, complete)``: ``complete`` is False when the time budget ended the search early."""
    names: Dict[str, List[Path]] = {}
    maybe: List[Path] = []
    for d in _outermost(search_dirs):
        if progress:
            progress(f"reading {d}")
        for f in _walk(d):
            names.setdefault(f.name.casefold(), []).append(f)
            low = f.name.casefold()
            ext = low.rsplit(".", 1)[-1] if "." in low else ""
            if ext in _BIOS_EXT or any(w in low for w in _BIOS_WORDS):
                try:
                    if 0 < f.stat().st_size <= 4 * 1024 * 1024:
                        maybe.append(f)
                except OSError:
                    pass
    found: Dict[str, Path] = {}
    cache: Dict[Path, str] = {}
    end = time.time() + limit_seconds
    complete = True

    def md5(f: Path) -> str:
        if f not in cache:
            cache[f] = md5_of(f)
        return cache[f]

    for fw in wanted:
        base = fw["path"].split("/")[-1].casefold()
        if progress:
            progress(f"looking for {base}")
        for f in names.get(base, []):
            try:
                if f.stat().st_size > MAX_BIOS_BYTES:
                    continue
                if not fw["md5"] or md5(f) == fw["md5"]:
                    found[fw["path"]] = f
                    break
            except OSError:
                continue
        if fw["path"] in found or not fw["md5"]:
            continue
        if time.time() > end:
            complete = False
            continue
        for f in maybe:
            try:
                if md5(f) == fw["md5"]:
                    found[fw["path"]] = f
                    break
            except OSError:
                continue
            if time.time() > end:
                complete = False
                break
    return found, complete


def cores_for_platforms(infos: List[dict], platforms: Iterable[Any]) -> List[dict]:
    """The cores that play any of the systems, each once, in core order."""
    seen: Dict[str, dict] = {}
    for p in platforms:
        for c in cores_for_platform(infos, p):
            seen.setdefault(c["file"], c)
    return [seen[k] for k in sorted(seen, key=lambda f: seen[f]["core"].casefold())]


def check_bios_cores(install: Install, cores: List[dict], search_dirs: Iterable[Path], home: Optional[Path] = None,
                     platforms: Iterable[Any] = (), progress: Optional[Callable[[str], None]] = None) -> dict:
    """What these cores expect in RetroArch's system folder, what is there (checksum verified where the core lists one) and,
    for what is missing, where a matching file lies in ``search_dirs``. ``platforms`` only labels which systems a core serves."""
    cur = settings_of(install, home)
    system = Path(cur["system_path"]) if cur["system_path"] else install.base / "system"
    missing = [fw for c in cores for fw in c["firmware"] if not (system / fw["path"]).is_file()]
    found, complete = _find_candidates(search_dirs, missing, progress=progress) if missing else ({}, True)
    platforms = list(platforms)
    rows = []
    for c in cores:
        items = []
        for fw in c["firmware"]:
            target = system / fw["path"]
            status, src = "missing", ""
            if target.is_file():
                if fw["md5"]:
                    try:
                        status = "ok" if md5_of(target) == fw["md5"] else "wrong"
                    except OSError:
                        status = "wrong"
                else:
                    status = "present"
            elif fw["path"] in found:
                status, src = "found", str(found[fw["path"]])
            items.append({**fw, "status": status, "target": str(target), "source": src})
        rows.append({"core": c["core"], "display": c["display"], "firmware": items,
                     "serves": [p.name for p in platforms if any(x["file"] == c["file"] for x in cores_for_platform([c], p))]})
    return {"system_dir": str(system), "cores": rows, "complete": complete,
            "counts": {k: sum(1 for r in rows for i in r["firmware"] if i["status"] == k)
                       for k in ("ok", "present", "wrong", "found", "missing")},
            "required_missing": sum(1 for r in rows for i in r["firmware"] if i["status"] in ("missing", "found") and not i["optional"])}


def check_bios(install: Install, platform: Any, search_dirs: Iterable[Path], home: Optional[Path] = None) -> dict:
    """:func:`check_bios_cores` for the cores of one system."""
    return check_bios_cores(install, cores_for_platform(core_infos(install, home), platform), search_dirs, home, [platform])


def apply_bios(install: Install, items: List[dict], journal_dir: Path, mode: str = "move",
               home: Optional[Path] = None) -> dict:
    """Put the found files where the cores look for them (``items``: ``{target, source}`` from :func:`check_bios`). Never
    overwrites; one journal for undo."""
    res: Dict[str, Any] = {"placed": 0, "failed": [], "journal": None}
    done: List[dict] = []
    system = Path(settings_of(install, home)["system_path"] or install.base / "system")
    for it in items:
        src, dst = Path(it["source"]), Path(it["target"])
        try:
            dst.resolve().relative_to(system.resolve())      # only ever inside the system folder
        except ValueError:
            res["failed"].append({"path": str(dst), "error": "not inside the system folder"})
            continue
        try:
            if not src.is_file():
                raise FileNotFoundError("the source file is gone")
            if os.path.lexists(dst):
                raise FileExistsError("a file is already there")
            dst.parent.mkdir(parents=True, exist_ok=True)
            size = src.stat().st_size
            if mode == "copy":
                shutil.copy2(src, dst)
            else:
                shutil.move(str(src), str(dst))
            done.append({"from": str(src), "to": str(dst), "copy": mode == "copy", "size": size})
            res["placed"] += 1
        except OSError as exc:
            res["failed"].append({"path": str(dst), "error": str(exc)})
    if done:
        journal_dir = Path(journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        j = journal_dir / f"saves-{time.strftime('%Y%m%d-%H%M%S')}-bios.json"
        j.write_text(json.dumps({"install": str(install.cfg), "kind": "bios", "moves": done, "cfg_backup": "", "undone": False},
                                indent=1), encoding="utf-8")
        res["journal"] = str(j)
    return res


# --------------------------------------------------------------------------- shared folders (assets kept outside RetroArch)
# (config key, label, the sub-folder names that mean it). A sub-folder that does not exist in the base is never proposed.
SHARED = (
    ("assets_directory", "Menu assets", ("menu", "assets")),
    ("content_database_path", "Content database (rdb)", ("rdb", "database")),
    ("cheat_database_path", "Cheats", ("cht", "cheats")),
    ("playlist_directory", "Playlists", ("playlists",)),
    ("thumbnails_directory", "Thumbnails", ("thumbnails",)),
    ("core_assets_directory", "Downloaded core assets", ("downloads",)),
    ("input_remapping_directory", "Controller remaps", ("remaps",)),
    ("rgui_config_directory", "Config browser folder", ("config",)),
)
_BUILTIN_LISTS = ("content_favorites_path", "content_history_path", "content_image_history_path",
                  "content_music_history_path", "content_video_history_path")


def shared_base(install: Install, home: Optional[Path] = None) -> str:
    """The folder most likely to hold the shared assets: the parent of the playlist (else save) folder, if it exists."""
    cfg = read_cfg(install.cfg)
    for key in ("playlist_directory", "thumbnails_directory", "savefile_directory"):
        p = resolve(cfg.get(key, ""), install, home)
        if p is not None and p.parent.is_dir() and not str(p).startswith(str(install.base)):
            return str(p.parent)
    return ""


def _has_entries(path: Path) -> bool:
    try:
        return path.is_dir() and any(True for _ in path.iterdir())
    except OSError:
        return False


def shared_folders(install: Install, base: str, home: Optional[Path] = None) -> List[dict]:
    """One row per setting: what RetroArch uses now, the folder inside ``base`` that stands for it, and whether they agree.
    ``status``: ``ok`` (already that folder), ``set`` (points elsewhere), ``unset`` (RetroArch's own default folder) or
    ``none`` (no such folder in the base)."""
    cfg = read_cfg(install.cfg)
    root = Path(os.path.expanduser(base)) if base else None
    rows: List[dict] = []
    for key, label, names in SHARED:
        raw = cfg.get(key, "")
        cur = resolve(raw, install, home)
        want: Optional[Path] = None
        if root is not None:
            for n in names:
                if (root / n).is_dir():
                    want = root / n
                    break
        local = cur is not None and str(cur).startswith(str(install.base))
        if want is None:
            status = "none"
        elif cur is not None and os.path.normcase(os.path.realpath(cur)) == os.path.normcase(os.path.realpath(want)):
            status = "ok"
        elif cur is None or local or raw in ("", "default"):
            status = "unset"
        else:
            status = "set"
        rows.append({"key": key, "label": label, "current": raw, "current_path": str(cur) if cur else "",
                     "want": str(want) if want else "", "want_cfg": to_cfg_path(want, home) if want else "",
                     "status": status, "want_has_files": bool(want and _has_entries(want)),
                     "current_has_files": bool(cur and _has_entries(cur))})
    return rows


def apply_shared(install: Install, rows: List[dict], keys: Iterable[str], journal_dir: Path,
                 home: Optional[Path] = None) -> dict:
    """Point the chosen settings at the shared folders (files are not moved). Refuses while RetroArch runs."""
    if is_running():
        raise RuntimeError("RetroArch is running. Close it first: it rewrites its config when it exits.")
    chosen = set(keys)
    changes: Dict[str, Any] = {}
    playlist = None
    for r in rows:
        if r["key"] in chosen and r["want"] and r["status"] != "ok":
            changes[r["key"]] = r["want_cfg"]
            if r["key"] == "playlist_directory":
                playlist = Path(r["want"])
    if playlist is not None:                              # the favourites / history lists live in <playlists>/builtin
        cfg = read_cfg(install.cfg)
        for key in _BUILTIN_LISTS:
            name = (cfg.get(key, "").replace("\\", "/").rsplit("/", 1)[-1]) or ""
            if name and (playlist / "builtin" / name).is_file():
                changes[key] = to_cfg_path(playlist / "builtin" / name, home)
    if not changes:
        return {"changed": [], "cfg_backup": None, "journal": None}
    backup = write_cfg(install.cfg, changes)
    journal_dir = Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True)
    j = journal_dir / f"saves-{time.strftime('%Y%m%d-%H%M%S')}-config.json"
    j.write_text(json.dumps({"install": str(install.cfg), "kind": "config", "moves": [], "cfg_backup": str(backup),
                             "changes": {k: str(v) for k, v in changes.items()}, "undone": False}, indent=1), encoding="utf-8")
    return {"changed": sorted(changes), "cfg_backup": str(backup), "journal": str(j)}
