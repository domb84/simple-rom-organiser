"""RetroArch: find the installs, read and write ``retroarch.cfg``, and move the save files and save states to where
RetroArch will look for them.

Everything here is optional: with no RetroArch found (and none chosen) the rest of the app does not care. Nothing is
written without a backup of ``retroarch.cfg``, and files are only ever moved, never overwritten or deleted.

Layout facts (RetroArch): ``savefile_directory`` / ``savestate_directory`` (empty = next to the content), with
``sort_savefiles_enable`` / ``sort_savestates_enable`` putting each core's files in a sub-folder named after the core.
A save file is ``<content name>.srm`` (or .sav, .rtc ...); a state is ``<content name>.state``, ``.state1`` ...,
``.state.auto``, and a thumbnail adds ``.png``."""

from __future__ import annotations

import errno
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
           "classify", "plan_relocation", "apply_relocation", "override_warnings",
           "SETTING_KEYS", "split_save", "match_save", "pairs_from_moves", "plan_follow", "apply_follow", "core_infos",
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
    kind: str                 # steam | flatpak | native | custom
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
    """Every RetroArch whose ``retroarch.cfg`` can be found, plus the custom ones (a path to a cfg, to its folder, or to
    the program next to it).

    With a ``home``, ``env`` or ``platform`` given, nothing outside them is read. ``ROMORG_RETROARCH_DETECT=0`` in the
    environment turns the search of this machine off for a plain call (only the custom ones are found): the test suite
    sets it, so that no test ever works on the user's own RetroArch."""
    live = home is None and env is None and platform is None
    home = Path(home) if home else Path.home()
    env = dict(os.environ if env is None else env)
    platform = platform or sys.platform
    if live and os.environ.get("ROMORG_RETROARCH_DETECT", "").strip() == "0":
        platform = "none"
    found: List[Install] = []
    seen: set = set()

    def add(kind: str, label: str, cfg: Path, base: Optional[Path] = None) -> None:
        try:
            if not cfg.is_file():
                return
            key = os.path.normcase(os.path.realpath(cfg))
        except (OSError, ValueError):
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
    elif platform == "darwin":
        add("native", "RetroArch", home / "Library/Application Support/RetroArch/config/retroarch.cfg",
            home / "Library/Application Support/RetroArch")
    for raw in custom:
        p = Path(os.path.expanduser(str(raw)))
        try:
            is_file = p.is_file()
        except (OSError, ValueError):
            continue
        if is_file and p.suffix.lower() != ".cfg":           # not a config (the program itself was named):
            p, is_file = p.parent, False                      # it stands for its folder
        cfg = p if is_file else p / "retroarch.cfg"
        add("custom", f"RetroArch ({cfg.parent})", cfg)
    return found


# --------------------------------------------------------------------------- reading
def read_cfg(cfg: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = Path(cfg).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.lstrip("﻿").splitlines():           # a byte order mark is not part of the first key
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
        out = subprocess.run(["pgrep", "-i", "retroarch"], capture_output=True, timeout=10)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError, AttributeError, ImportError):
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


def _key(path: Any) -> str:
    """A path as a comparison key: links resolved."""
    return os.path.normcase(os.path.realpath(path))


def _is_within(child: Any, parent: Any) -> bool:
    """True when ``child`` is ``parent`` or lies inside it (by folder, not by text: ``/ra2`` is not inside ``/ra``)."""
    try:
        c, p = Path(_key(child)), Path(_key(parent))
    except (OSError, ValueError):
        return False
    return c == p or p in c.parents


def _same_file_other_case(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` are one file whose two names differ only by case (a file system that ignores case)."""
    if a.parent != b.parent or a.name == b.name or a.name.casefold() != b.name.casefold():
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _fresh(path: Path) -> Path:
    """``path``, or ``<name>-2``, ``-3`` ... (before the extension) when it is taken: two changes within one second
    must not share a backup zip or a config backup."""
    path = Path(path)
    stem, suffix = (path.name, "") if BACKUP_SUFFIX in path.name else (path.stem, path.suffix)
    n, out = 1, path
    while os.path.lexists(out):
        n += 1
        out = path.with_name(f"{stem}-{n}{suffix}")
    return out


def to_cfg_path(path: Path, home: Optional[Path] = None) -> str:
    """How a folder is written in ``retroarch.cfg``: ``~/...`` under the home folder (as RetroArch itself does), else
    absolute."""
    home = Path(home) if home else Path.home()
    try:
        rel = path.relative_to(home)
        return "~/" + rel.as_posix() if rel.parts else "~"
    except ValueError:
        pass
    return str(path)


def write_cfg(cfg: Path, changes: Dict[str, Any]) -> Path:
    """Set the keys in ``retroarch.cfg`` after copying it to ``retroarch.cfg.romorg-backup-<time>``. Every other byte
    stays as it is: the line endings (LF or CRLF, per line), a byte order mark, bytes that are not UTF-8. A value of
    None removes the key's line. Returns the backup path."""
    cfg = Path(cfg)
    text = cfg.read_bytes().decode("utf-8", errors="surrogateescape")
    backup = _fresh(cfg.with_name(cfg.name + BACKUP_SUFFIX + time.strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(cfg, backup)
    bom = "﻿" if text.startswith("﻿") else ""
    lines = text[len(bom):].split("\n")
    eol = "\r" if text.count("\r\n") * 2 > text.count("\n") else ""        # what most lines end with (before the \n)
    pending = dict(changes)
    out: List[str] = []
    for line in lines:
        m = _LINE_RE.match(line)
        if m and m.group(1) in pending and not line.lstrip().startswith("#"):
            value = pending.pop(m.group(1))
            if value is None:
                continue
            line = f'{m.group(1)} = "{_cfg_text(value)}"' + ("\r" if line.endswith("\r") else "")
        out.append(line)
    pending = {k: v for k, v in pending.items() if v is not None}
    if pending:
        if out and out[-1] == "":
            out.pop()
        elif out:                                             # the last line had no line end: give it one
            out[-1] += eol
        out += [f'{k} = "{_cfg_text(v)}"{eol}' for k, v in pending.items()] + [""]
    tmp = cfg.with_name(cfg.name + ".romorg.part")
    tmp.write_bytes((bom + "\n".join(out)).encode("utf-8", errors="surrogateescape"))
    try:
        os.replace(tmp, cfg)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return backup


def _cross_device(exc: OSError, src: Any, dst: Any) -> bool:
    if exc.errno == errno.EXDEV:
        return True
    try:
        return os.stat(src).st_dev != os.stat(os.path.dirname(os.path.abspath(dst))).st_dev
    except OSError:
        return False


def _move(src: Any, dst: Any) -> None:
    """Move one file: never onto an existing file, and never leaving it in both places.

    A hard link plus removing the old name (a rename that cannot replace anything); where the file system has no hard
    links, a plain rename; a copy only onto another drive. When the old name cannot be removed in the end, the new one is
    taken away again and the error raised, so a failed move leaves exactly what was there before. Two names of one file
    that differ only by case are a rename."""
    src, dst = Path(src), Path(dst)
    if os.path.lexists(dst):
        if _same_file_other_case(src, dst):
            os.rename(src, dst)
            return
        raise FileExistsError(errno.EEXIST, "a file is already there", str(dst))
    linked = True
    try:
        os.link(src, dst)
    except OSError:
        if os.path.lexists(dst):
            raise
        linked = False
    if not linked:
        try:
            os.rename(src, dst)
            return
        except OSError as exc:
            if os.path.lexists(dst) or not _cross_device(exc, src, dst):
                raise
        try:
            shutil.copy2(src, dst)
        except OSError:
            _discard(dst)
            raise
    try:
        os.unlink(src)
    except OSError as exc:
        _discard(dst)
        if os.path.lexists(dst):                              # could not be taken away either: say so
            raise OSError(exc.errno, f"{exc} (it is now also at {dst}: remove one of the two by hand)") from None
        raise


def _discard(path: Any) -> None:
    try:
        if os.path.lexists(path):
            os.unlink(path)
    except OSError:
        pass


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
    rel.changes = {"savefile_directory": to_cfg_path(save_dir, home),
                   "savestate_directory": to_cfg_path(state_dir, home),
                   "sort_savefiles_enable": sort_saves, "sort_savestates_enable": sort_states,
                   "savefiles_in_content_dir": False, "savestates_in_content_dir": False}
    old_sorted = {"save": bool(cur["sort_savefiles_enable"]), "state": bool(cur["sort_savestates_enable"])}
    new_sorted = {"save": sort_saves, "state": sort_states}
    new_root = {"save": Path(save_dir), "state": Path(state_dir)}
    roots = {os.path.normcase(os.path.realpath(old_save)): old_save, os.path.normcase(os.path.realpath(old_state)): old_state}
    claimed: Dict[str, Path] = {}
    for root in roots.values():
        # a new folder inside the old one (e.g. saves/ -> saves/states) is not scanned as if it held old files
        skip = [d for d in new_root.values() if _key(d) != _key(root) and _is_within(d, root)]
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


def apply_relocation(install: Install, rel: Relocation, backup_zip: Optional[Path] = None,
                     progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """Back up, move the files, remove the folders that were emptied, write ``retroarch.cfg`` (its old content kept as a
    backup). Returns a summary. Refuses while RetroArch runs.

    A file that cannot be put into the backup zip (it is open in another program) is not moved. When writing
    ``retroarch.cfg`` fails in the end the files stay moved (``cfg_error``); the backup zip has them."""
    if is_running():
        raise RuntimeError("RetroArch is running. Close it first: it rewrites its config when it exits.")
    todo = [m for m in rel.moves if m.status in ("move", "needs_core") and m.src != m.dst]
    res: Dict[str, Any] = {"moved": 0, "failed": [], "removed_dirs": 0, "backup": None, "cfg_backup": None}
    if backup_zip is not None and todo:
        backup_zip = Path(backup_zip)
        backup_zip.parent.mkdir(parents=True, exist_ok=True)
        backup_zip = _fresh(backup_zip)
        saved: List[Move] = []
        # strict_timestamps=False: a save dated before 1980 (a zeroed time stamp) is stored as 1980, not refused
        with zipfile.ZipFile(backup_zip, "w", zipfile.ZIP_STORED, allowZip64=True, strict_timestamps=False) as z:
            for i, m in enumerate(todo):
                try:
                    z.write(m.src, arcname=f"{m.kind}/{i:05d}/{m.src.name}")
                    saved.append(m)
                except OSError as exc:
                    res["failed"].append({"path": str(m.src), "error": f"not backed up, so left where it is: {exc}"})
                if progress:
                    progress(i, len(todo) * 2, f"backing up {m.src.name}")
            z.writestr("manifest.json", json.dumps([{"i": i, "from": str(m.src), "name": m.src.name, "kind": m.kind}
                                                    for i, m in enumerate(todo) if m in saved], indent=1))
        todo = saved
        res["backup"] = str(backup_zip)
    dirs: set = set()
    for i, m in enumerate(todo):
        if progress:
            progress(len(todo) + i, len(todo) * 2, f"moving {m.src.name}")
        try:
            m.dst.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(m.dst):
                raise FileExistsError("a file appeared there since the preview")
            _move(m.src, m.dst)
            dirs.add(m.src.parent)
            res["moved"] += 1
        except OSError as exc:
            res["failed"].append({"path": str(m.src), "error": str(exc)})
    res["removed_dirs"] = _remove_empty(dirs, keep=[Path(rel.new["save"]), Path(rel.new["state"]), install.base])
    old_roots = [Path(rel.old["save"]), Path(rel.old["state"])]
    res["removed_dirs"] += _remove_empty(old_roots, keep=[Path(rel.new["save"]), Path(rel.new["state"]), install.base])
    if not res["failed"]:
        try:
            res["cfg_backup"] = str(write_cfg(install.cfg, rel.changes))
        except OSError as exc:
            res["cfg_error"] = str(exc)
            res["failed"].append({"path": str(install.cfg), "error": f"retroarch.cfg was not changed: {exc}"})
    return res


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


def fold_name(name: str) -> str:
    """A content name as it is compared with a save's name: case matters, RetroArch writes the name exactly as the content file has it."""
    return name


_SUFFIX_BAD = " ()"            # a core's suffix never has spaces (or the brackets of a game's name)


class SaveMatcher:
    """Finds the game a save or state file name belongs to (see :func:`match_save`); build it once for many names."""

    def __init__(self, wanted: Iterable[str]) -> None:
        self.wanted = wanted if isinstance(wanted, (set, dict, frozenset)) else set(wanted)

    def match(self, name: str) -> Optional[tuple]:
        cut = name.rfind(".")
        while cut > 0:
            head = name[:cut]
            if head in self.wanted and not any(ch in name[cut:] for ch in _SUFFIX_BAD):
                return head, name[cut:]
            cut = name.rfind(".", 0, cut)
        return None


def match_save(name: str, wanted: Iterable[str]) -> Optional[tuple]:
    """``(content name, suffix)`` when ``name`` is a save or state file of one of the ``wanted`` games: the game's name, then a
    dot, then whatever the core adds (``.srm``, ``.state1.png``, Flycast's ``.A1.bin`` memory cards, ``.1.srm``, ``.eep`` ...).
    The longest game name wins, so ``Game (USA).A1.bin`` belongs to ``Game (USA)`` and not to a shorter ``Game``; a dot
    inside a name (``Dr. Mario``) is only a cut where the part before it is itself one of the games and what follows is a plain suffix (no spaces)."""
    return SaveMatcher(wanted).match(name)


def save_name_keys(names: Iterable[str]) -> frozenset:
    """Every content name (as :func:`fold_name` writes it) the file names in ``names`` could be a save of: the part before
    each dot whose rest is a plain suffix. Looking a game up in this set tells whether any save belongs to it, without
    knowing the games when the saves are read."""
    out: set = set()
    for name in names:
        cut = name.rfind(".")
        while cut > 0:
            if not any(ch in name[cut:] for ch in _SUFFIX_BAD):
                out.add(fold_name(name[:cut]))
            cut = name.rfind(".", 0, cut)
    return frozenset(out)


# What a core writes after the game's name. Used where a save has to be told from the ROM's other companions (the Redump
# .zip / .md5 / .cue of a disc game's folder), which match_save cannot do: it accepts any suffix.
_SAVE_EXTS = frozenset(("srm", "sav", "mcr", "mcd", "eep", "sra", "fla", "mpk", "nvr", "rtc", "uss"))
_STATE_SUFFIX_RE = re.compile(r"^\.state(\d*|\.auto)(\.png)?$", re.IGNORECASE)
_CARD_SUFFIX_RE = re.compile(r"^\.[A-D][1-6]\.bin$", re.IGNORECASE)         # Flycast: Game.A1.bin .. Game.D6.bin
_NUMBERED_RE = re.compile(r"^\.\d+\.(srm|sav)$", re.IGNORECASE)             # some cores: Game.1.srm


def is_save_suffix(suffix: str) -> bool:
    """True for what follows the game's name in an emulator save or state file: ``.srm .sav .mcr .mcd .eep .sra .fla .mpk
    .nvr .rtc .uss``, ``.state``, ``.state<N>``, ``.state.auto`` (each state also with ``.png``), a numbered ``.<N>.srm`` and
    Flycast's ``.A1.bin`` ... ``.D6.bin`` memory cards."""
    if _STATE_SUFFIX_RE.match(suffix) or _CARD_SUFFIX_RE.match(suffix) or _NUMBERED_RE.match(suffix):
        return True
    return suffix.count(".") == 1 and suffix[1:].lower() in _SAVE_EXTS


def is_save_of(name: str, stem: str) -> bool:
    """``name`` is a save or state file of the game ``stem``: it starts with the stem and a dot, and what follows is a
    :func:`is_save_suffix` suffix."""
    return len(name) > len(stem) + 1 and name[:len(stem)] == stem and name[len(stem)] == "." and is_save_suffix(name[len(stem):])


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


class SaveWalk:
    """The save and state files of one install, read ONCE (a walk of the save roots) and the cores' names read once: asked for
    one system after the other (a Collection has up to 18) it never walks again. See :func:`list_saves`."""

    def __init__(self, install: Install, home: Optional[Path] = None) -> None:
        self.install = install
        self.home = home
        self.roots = save_roots(install, home)
        cur = settings_of(install, home)
        # RetroArch writes a sub-folder per core only when it is asked to: that is what tells a core's folder from a game's
        self.per_core = bool(cur.get("sort_savefiles_enable") or cur.get("sort_savestates_enable"))
        self.files: List[tuple] = []
        seen: set = set()
        for root in self.roots:
            for f in _walk(root):
                key = os.path.normcase(str(f))
                if key not in seen:
                    seen.add(key)
                    self.files.append((f, root))
        self._infos: Optional[List[dict]] = None
        self._cores: Dict[str, tuple] = {}

    def _cores_of(self, platform: Any) -> tuple:
        """``(names of every core, names of the cores that play platform)``, folded."""
        if self._infos is None:
            self._infos = core_infos(self.install, self.home, firmware_only=False)
        name = getattr(platform, "name", "")
        if name not in self._cores:
            self._cores[name] = ({fold_name(c["core"]) for c in self._infos},
                                 {fold_name(c["core"]) for c in cores_for_platform(self._infos, platform)})
        return self._cores[name]

    def for_platform(self, platform: Any = None) -> List[tuple]:
        """``[(file, save root, here)]``: ``here`` is True for a file in a folder of a core that plays ``platform``, and for a
        file that lies directly in the root when RetroArch does not make a folder per core (then nothing says which system it
        is for); False for a file in a folder that no core is named after (a game's folder) and for a file in the root of a
        folder that is sorted per core: they can still match a game, but they are not counted as this system's own."""
        if platform is None:
            return [(f, root, True) for f, root in self.files]
        known, mine = self._cores_of(platform)
        out: List[tuple] = []
        for f, root in self.files:
            try:
                parts = f.relative_to(root).parts
            except ValueError:
                parts = (f.name,)
            if len(parts) > 1 and fold_name(parts[0]) in known and fold_name(parts[0]) not in mine:
                continue
            out.append((f, root, (len(parts) == 1 and not self.per_core) or (len(parts) > 1 and fold_name(parts[0]) in mine)))
        return out


def list_saves(install: Install, platform: Any = None, home: Optional[Path] = None, walk: Optional[SaveWalk] = None) -> List[tuple]:
    """``[(file, save root)]`` for every save and state file that can belong to a game of ``platform`` (all of them when
    ``platform`` is None): the save roots are walked ONCE (``walk``: one already made). When RetroArch sorts the files into
    a sub-folder per core, only the folders of the cores that play the platform count, so a Genesis save never belongs to a
    same-named SNES game. A sub-folder that is no core's name (sorted by content folder, or files directly in the root)
    counts for every system."""
    walk = walk if walk is not None else SaveWalk(install, home)
    return [(f, root) for f, root, _here in walk.for_platform(platform)]


@dataclass
class SaveFile:
    src: Path
    rel: Path                 # the path below its save root (``bsnes/Game (USA).srm``)
    stem: str                 # the game it belongs to
    kind: str                 # save | state
    size: int = 0


def saves_of(files: Iterable[tuple], stems: Iterable[str]) -> List[SaveFile]:
    """The files of :func:`list_saves` that belong to one of the games ``stems`` (a longer name wins, see :func:`match_save`)."""
    matcher = SaveMatcher(set(stems))
    out: List[SaveFile] = []
    for f, root in files:
        hit = matcher.match(f.name)
        if hit is None:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        out.append(SaveFile(f, f.relative_to(root), hit[0], "state" if re.match(r"^\.state", hit[1], re.IGNORECASE) else "save", size))
    return out


@dataclass
class Follow:
    src: Path
    dst: Path
    kind: str
    status: str = "move"      # move | copy | replace | conflict
    note: str = ""
    size: int = 0
    data: Optional[bytes] = None          # a copy is written from these bytes (a save converted for another region) instead of copied


def plan_follow(install: Install, pairs: Iterable[tuple], mode: str = "move", home: Optional[Path] = None,
                files: Optional[Iterable[tuple]] = None) -> List[Follow]:
    """Where saves and states go when games get new names: each file stays in its own folder (core folders are kept apart)
    and only the content name part changes. ``mode`` is ``move`` (the old name goes) or ``copy`` (both names stay).
    ``files``: the files to look at, ``[(file, save root)]`` as :func:`list_saves` gives them (default: every file of
    every save root)."""
    wanted = dict(pairs)
    out: List[Follow] = []
    if not wanted:
        return out
    claimed: set = set()
    matcher = SaveMatcher(wanted)
    if files is None:
        files = [(f, root) for root in save_roots(install, home) for f in _walk(root)]
    for f, _root in files:
        hit = matcher.match(f.name)
        if hit is None:
            continue
        stem, suffix = hit
        kind = "state" if re.match(r"^\.state", suffix, re.IGNORECASE) else "save"
        dst = f.with_name(wanted[stem] + suffix)
        status, note = mode, ""
        key = os.path.normcase(str(dst))
        if _same_file_other_case(f, dst):
            # the new name differs only by case and the file system ignores case: it is this very file, not a
            # file in the way. A move gives it the new spelling; a copy has nothing to do.
            if mode != "move":
                continue
        elif os.path.lexists(dst) or key in claimed:
            status, note = "conflict", "a file with the new name is already there - left alone"
        claimed.add(key)
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        out.append(Follow(f, dst, kind, status, note, size))
    return out


def apply_follow(ops: List[Follow], backup_dir: Path, running: Optional[bool] = None) -> dict:
    """Do the renames / copies (and ``replace``: a file rewritten in place, its old content kept in ``backup_dir``). Skipped
    (not an error) while RetroArch runs: it writes its saves back on exit; ``running`` says so for another program (None: RetroArch)."""
    todo = [o for o in ops if o.status in ("move", "copy", "replace")]
    res: Dict[str, Any] = {"followed": 0, "copied": 0, "failed": [], "skipped_running": False}
    if not todo:
        return res
    if is_running() if running is None else running:
        res["skipped_running"] = True
        return res
    for o in todo:
        try:
            if o.status == "replace":
                backup = Path(backup_dir) / "replaced" / f"{time.strftime('%Y%m%d-%H%M%S')}-{o.src.name}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup = Path(_fresh(backup))
                shutil.copy2(o.src, backup)
                tmp = o.src.with_name(o.src.name + ".romorg-tmp")
                try:
                    tmp.write_bytes(o.data or b"")
                    shutil.copystat(o.src, tmp)
                    os.replace(tmp, o.src)
                except OSError:
                    _discard(tmp)
                    raise
                res["replaced"] = res.get("replaced", 0) + 1
                continue
            if os.path.lexists(o.dst) and not _same_file_other_case(o.src, o.dst):
                raise FileExistsError("a file appeared there")
            if o.status == "copy":
                try:
                    o.dst.parent.mkdir(parents=True, exist_ok=True)
                    if o.data is not None:
                        tmp = o.dst.with_name(o.dst.name + ".romorg-tmp")
                        tmp.write_bytes(o.data)
                        try:
                            shutil.copystat(o.src, tmp)
                        except OSError:
                            pass
                        os.replace(tmp, o.dst)
                    else:
                        shutil.copy2(o.src, o.dst)
                except OSError:
                    _discard(o.dst)                         # no half-written copy under the new name
                    _discard(o.dst.with_name(o.dst.name + ".romorg-tmp"))
                    raise
                res["copied"] += 1
            else:
                _move(o.src, o.dst)
                res["followed"] += 1
        except OSError as exc:
            res["failed"].append({"path": str(o.src), "error": str(exc)})
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


def core_infos(install: Install, home: Optional[Path] = None, firmware_only: bool = True) -> List[dict]:
    """Every installed core's ``.info`` that lists firmware (every core with ``firmware_only=False``): ``{core, display, extensions, firmware:[{path, optional, md5}]}``."""
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
            if n <= 0 and firmware_only:
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


_SUCCESSORS = ("portable", "advance", "vita")


def cores_for_platform(infos: List[dict], platform: Any) -> List[dict]:
    """The cores that play this system: the core's system name equals / starts with the system's name (not followed by a
    digit: PlayStation is not PlayStation 2), or it reads one of the system's own, non-generic file extensions."""
    pname = getattr(platform, "name", "")
    name = _fold(pname)
    # the maker is the first word, or the second ("Super Nintendo Entertainment System" is Nintendo's)
    makers = {_fold(w) for w in pname.split()[:2]} - {""}
    exts = {e.lstrip(".").lower() for e in getattr(platform, "extensions", ()) or ()} - GENERIC_EXTENSIONS
    out = []
    for c in infos:
        system = _fold(c["display"].split("(")[0].replace(" - ", " "))
        rest = system[len(name):]
        # a longer name is the same system with an add-on ("Dreamcast/Naomi"), not its successor ("PlayStation 2",
        # "PlayStation Portable", "Game Boy Advance")
        by_name = bool(name) and (system == name or (system.startswith(name) and not rest[:1].isdigit()
                                                     and not rest.startswith(_SUCCESSORS)))
        same_maker = _fold(c["display"].split(" - ")[0]) in makers
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
    from . import bios as _bios
    folders = _outermost(search_dirs)
    for n, d in enumerate(folders, 1):
        if progress:
            progress(f"Reading folder {n} of {len(folders)}: {d}")
        for f in _walk(d):
            names.setdefault(f.name.casefold(), []).append(f)
            low = f.name.casefold()
            ext = low.rsplit(".", 1)[-1] if "." in low else ""
            if ext in _BIOS_EXT or any(w in low for w in _BIOS_WORDS) or _bios.is_database_name(low):
                try:
                    if 0 < f.stat().st_size <= 4 * 1024 * 1024:
                        maybe.append(f)
                except OSError:
                    pass
    found: Dict[str, Path] = {}
    cache: Dict[Path, str] = {}
    end = time.time() + limit_seconds
    complete = True

    base = ""

    def md5(f: Path) -> str:
        if f not in cache:
            cache[f] = md5_of(f)
            if progress and len(cache) % 20 == 0:          # (the first file name that needs it reads them all: say so)
                progress(f"Reading the checksums of {len(maybe)} files that could be a BIOS ({len(cache)} done), looking for {base}")
        return cache[f]

    for n, fw in enumerate(wanted, 1):
        base = fw["path"].split("/")[-1].casefold()
        if progress:
            progress(f"Looking for {base} ({n} of {len(wanted)}): {len(names)} file names read, {len(maybe)} possible BIOS files")
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
    # (a few cores list a folder, e.g. LRPS2's "pcsx2/bios": it is there when the folder is)
    missing = [fw for c in cores for fw in c["firmware"] if not (system / fw["path"]).exists()]
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
            elif target.is_dir():
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


def apply_bios(install: Install, items: List[dict], mode: str = "move", home: Optional[Path] = None) -> dict:
    """Put the found files where the cores look for them (``items``: ``{target, source}`` from :func:`check_bios`). Never
    overwrites."""
    res: Dict[str, Any] = {"placed": 0, "failed": []}
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
            if mode == "copy":
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    _discard(dst)
                    raise
            else:
                _move(src, dst)
            res["placed"] += 1
        except OSError as exc:
            res["failed"].append({"path": str(dst), "error": str(exc)})
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
        if p is not None and p.parent.is_dir() and not _is_within(p, install.base):
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
        local = cur is not None and _is_within(cur, install.base)
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


def asset_folders(install: Install, home: Optional[Path] = None) -> List[dict]:
    """The asset settings (:data:`SHARED`) and the folder RetroArch uses for each now (``own``: RetroArch's default, inside its folder)."""
    cfg = read_cfg(install.cfg)
    rows = []
    for key, label, _names in SHARED:
        raw = cfg.get(key, "")
        cur = resolve(raw, install, home)
        rows.append({"key": key, "label": label, "current": raw, "current_path": str(cur) if cur else "",
                     "own": cur is None or _is_within(cur, install.base) or raw in ("", "default")})
    return rows


def set_asset_folder(install: Install, key: str, folder: str, home: Optional[Path] = None) -> dict:
    """Point one asset setting at ``folder`` ("" = back to RetroArch's own). Only retroarch.cfg changes (backed up)."""
    if key not in {k for k, _l, _n in SHARED}:
        raise KeyError(key)
    want = Path(os.path.expanduser(folder)) if folder else None
    row = {"key": key, "want": str(want) if want else "default", "status": "set",
           "want_cfg": to_cfg_path(want, home) if want else "default"}
    return apply_shared(install, [row], [key], home)


def apply_shared(install: Install, rows: List[dict], keys: Iterable[str], home: Optional[Path] = None) -> dict:
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
        return {"changed": [], "cfg_backup": None}
    backup = write_cfg(install.cfg, changes)
    return {"changed": sorted(changes), "cfg_backup": str(backup)}
