"""Sort a folder full of ROMs into one folder per system, and keep the ROM folders clean.

Two jobs, both plain moves with a journal (so they can be undone) and neither ever overwrites a file:

* :func:`plan_sort`: files that were identified (by checksum, against every system's DAT) go to their system's folder,
  wherever they were; files that match nothing, and files that are not ROMs at all, go to an "aside" folder OUTSIDE the ROM
  folder (``<aside>/_unmatched/...`` and ``<aside>/_other/...``, with their folders kept).
* :func:`plan_sweep`: what a library build set aside inside a system's folder (``_excluded``, ``_superseded``,
  ``_incomplete``, ``_duplicates``, ``_unmatched``) moves to ``<aside>/<system folder>/<same folder>``, so the ROM folder only
  holds what you keep.

The identification itself (scanning) is done by the server; this module takes its answer.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import meter
from .folders import CONVERTED_DIR, RESERVED_DIRS

__all__ = ["SMove", "ASIDE_FOLDERS", "plan_sort", "plan_sweep", "plan_restore", "apply_moves", "undo_moves", "inside",
           "default_aside", "rom_like", "remove_empty_tree", "Names", "UNMATCHED"]

# the folders a library build sets things aside in (the converted originals are the user's safety copies: they stay)
ASIDE_FOLDERS = tuple(d for d in RESERVED_DIRS if d != CONVERTED_DIR)
UNMATCHED = "_unmatched"
OTHER = "_other"
DISC_EXTS = {".chd", ".cue", ".gdi", ".iso", ".bin", ".img", ".mdf", ".ccd", ".cdi", ".nrg", ".toc", ".pbp", ".rvz", ".wbfs",
             ".gcm", ".ciso", ".cso"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar"}


@dataclass
class SMove:
    src: Path
    dst: Path
    bucket: str                 # a system name, "_unmatched", "_other", or the reserved folder for a sweep
    note: str = ""
    kind: str = "file"          # file | folder
    size: int = 0


def default_aside(root: Path) -> Path:
    """Next to the ROM folder: ``<root>-archive``."""
    root = Path(root)
    return root.with_name(root.name + "-archive")


def inside(path: Path, folder: Path) -> bool:
    try:
        Path(path).relative_to(folder)
        return True
    except ValueError:
        return False


def rom_like(path: Path, extensions: Iterable[str]) -> bool:
    """A file that could be a ROM or disc image of some system (by extension)."""
    ext = Path(path).suffix.lower()
    return ext in set(extensions) or ext in DISC_EXTS or ext in ARCHIVE_EXTS


class Names:
    """Free names: a target that is taken (on disk or by an earlier move of this plan) gets `` (2)``, `` (3)`` ..."""

    def __init__(self, exists: Optional[Callable[[Path], bool]] = None) -> None:
        self.taken: set = set()
        self.exists = exists or os.path.lexists

    def free(self, dst: Path, folder: bool = False) -> Path:
        n = 1
        cand = dst
        while self.exists(cand) or os.path.normcase(str(cand)).casefold() in self.taken:
            n += 1
            cand = dst.with_name(f"{dst.name} ({n})") if folder or not dst.suffix else dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
        self.taken.add(os.path.normcase(str(cand)).casefold())
        return cand


_Names = Names


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def plan_sort(root: Path, aside: Path, folders: Dict[str, Path], flat: Dict[Path, Optional[str]],
              discs: Sequence[dict], unmatched: Iterable[Path], other: Iterable[Path],
              exists: Optional[Callable[[Path], bool]] = None) -> List[SMove]:
    """The moves that sort ``root``.

    ``folders``: system name -> its folder. ``flat``: identified ROM file -> system (None when it matches several systems:
    left for the user). ``discs``: ``{"platform", "top", "folder": bool, "files": [...]}`` per identified disc game.
    ``unmatched``: ROM-like files that match nothing; ``other``: everything else.

    Rules: an identified file already inside its own system's folder stays; elsewhere it moves to that folder (flat). A
    file next to a moved ROM with the same name (a save, a note) moves with it. Unmatched files that lie loose (outside every
    system folder) go to ``<aside>/_unmatched``; those inside a system folder are left for that system's library build.
    Other files go to ``<aside>/_other`` unless they belong to a disc game or to a ROM that stays."""
    root, aside = Path(root), Path(aside)
    names = Names(exists)
    moves: List[SMove] = []
    sys_dirs = list(folders.values())
    unit_tops = [Path(d["top"]) for d in discs if d.get("folder")]
    stays_stem: set = set()
    moved_stem: Dict[tuple, Path] = {}        # (folder, stem) -> the folder the ROM went to

    def rel(p: Path) -> Path:
        try:
            return p.relative_to(root)
        except ValueError:
            return Path(p.name)

    for path in sorted(flat, key=lambda p: p.as_posix().lower()):
        plat = flat[path]
        if plat is None or plat not in folders:
            continue
        target = folders[plat]
        key = (path.parent, path.stem.casefold())
        if inside(path, target):
            stays_stem.add(key)
            continue
        dst = names.free(target / path.name)
        moved_stem[key] = target
        moves.append(SMove(path, dst, plat, size=_size(path)))
    for d in discs:
        plat = d["platform"]
        if plat not in folders:
            continue
        target = folders[plat]
        top = Path(d["top"])
        if inside(top, target) and top != target:
            continue
        if d.get("folder"):
            moves.append(SMove(top, names.free(target / top.name, folder=True), plat, kind="folder"))
        else:
            for f in d["files"]:
                moves.append(SMove(Path(f), names.free(target / Path(f).name), plat, size=_size(Path(f))))
    for path in sorted(unmatched, key=lambda p: p.as_posix().lower()):
        if any(inside(path, s) for s in sys_dirs) or any(inside(path, t) for t in unit_tops):
            continue
        moves.append(SMove(path, names.free(aside / UNMATCHED / rel(path)), UNMATCHED, size=_size(path)))
    for path in sorted(other, key=lambda p: p.as_posix().lower()):
        key = (path.parent, path.stem.casefold())
        if any(inside(path, t) for t in unit_tops) or key in stays_stem:
            continue
        if key in moved_stem:                                      # a save / note beside a ROM that moved: goes along
            moves.append(SMove(path, names.free(moved_stem[key] / path.name), "sidecar", size=_size(path)))
            continue
        moves.append(SMove(path, names.free(aside / OTHER / rel(path)), OTHER, size=_size(path)))
    return moves


def plan_sweep(folders: Dict[str, Path], aside: Path) -> List[SMove]:
    """Everything a build set aside inside the system folders, to ``<aside>/<system folder name>/<reserved folder>/...``."""
    aside = Path(aside)
    names = Names()
    moves: List[SMove] = []
    for plat, folder in folders.items():
        for reserved in ASIDE_FOLDERS:
            base = folder / reserved
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for f in sorted(filenames):
                    if f.startswith(".romorg"):
                        continue
                    src = Path(dirpath) / f
                    moves.append(SMove(src, names.free(aside / folder.name / reserved / src.relative_to(base)), reserved,
                                       size=_size(src)))
    return moves


def plan_restore(folders: Dict[str, Path], aside: Path) -> List[SMove]:
    """The reverse of a sweep: ``<aside>/<system folder name>/<reserved>/...`` back into the system's folder."""
    names = Names()
    moves: List[SMove] = []
    for plat, folder in folders.items():
        for reserved in ASIDE_FOLDERS:
            base = Path(aside) / folder.name / reserved
            if not base.is_dir():
                continue
            for dirpath, _dirs, filenames in os.walk(base):
                for f in sorted(filenames):
                    src = Path(dirpath) / f
                    moves.append(SMove(src, names.free(folder / reserved / src.relative_to(base)), reserved, size=_size(src)))
    return moves


# --------------------------------------------------------------------------- doing it
def _remove_empty_dirs(dirs: Iterable[Path], stop: Iterable[Path]) -> None:
    stops = {os.path.normcase(os.path.realpath(s)) for s in stop}
    for d in sorted({Path(x) for x in dirs}, key=lambda p: len(p.parts), reverse=True):
        cur = d
        while True:
            if os.path.normcase(os.path.realpath(cur)) in stops or cur == cur.parent:
                break
            try:
                os.rmdir(cur)
            except OSError:
                break
            cur = cur.parent


def remove_empty_tree(root: Path, keep: Iterable[Path] = ()) -> None:
    """Remove every folder under ``root`` that holds nothing (``keep`` and ``root`` itself stay)."""
    stops = {os.path.normcase(os.path.realpath(s)) for s in [root, *keep]}
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        if os.path.normcase(os.path.realpath(dirpath)) in stops or os.path.islink(dirpath):
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def apply_moves(moves: List[SMove], journal_dir: Path, kind: str, keep: Iterable[Path] = (),
                progress: Optional[Callable[[int, int, str], None]] = None,
                cancel: Optional[Callable[[], bool]] = None, extra: Optional[dict] = None) -> dict:
    """Move the files / folders; never over an existing one. Folders that were emptied are removed (``keep`` are never
    removed). Writes one JSON journal for :func:`undo_moves`."""
    res: Dict[str, Any] = {"moved": 0, "failed": [], "journal": None, "cancelled": False}
    done: List[dict] = []
    emptied: set = set()
    for i, m in enumerate(moves):
        if cancel and cancel():
            res["cancelled"] = True
            break
        if progress:
            progress(i, len(moves), m.src.name)
        try:
            if os.path.lexists(m.dst):
                raise FileExistsError("a file is already there")
            m.dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(m.src, m.dst)
            except OSError:
                shutil.move(str(m.src), str(m.dst))
            done.append({"from": str(m.src), "to": str(m.dst), "kind": m.kind})
            meter.add(m.size)
            emptied.add(m.src.parent)
            res["moved"] += 1
        except OSError as exc:
            res["failed"].append({"path": str(m.src), "error": str(exc)})
    _remove_empty_dirs(emptied, keep)
    if done:
        journal_dir = Path(journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        j = journal_dir / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        n = 1
        while j.exists():
            n += 1
            j = journal_dir / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{n}.json"
        j.write_text(json.dumps({"kind": kind, "moves": done, "undone": False, **(extra or {})}, indent=1), encoding="utf-8")
        res["journal"] = str(j)
    return res


def undo_moves(journal: Path, keep: Iterable[Path] = ()) -> dict:
    """Move everything back where it came from, unless the original place is taken or the file is gone."""
    journal = Path(journal)
    d = json.loads(journal.read_text(encoding="utf-8"))
    restored, skipped, dirs = 0, [], set()
    for m in reversed(d.get("moves", [])):
        src, dst = Path(m["to"]), Path(m["from"])
        if not os.path.lexists(src):
            skipped.append({"path": str(src), "reason": "no longer there"})
        elif os.path.lexists(dst):
            skipped.append({"path": str(dst), "reason": "something is at the original place now"})
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.rename(src, dst)
                except OSError:
                    shutil.move(str(src), str(dst))
                restored += 1
                dirs.add(src.parent)
            except OSError as exc:
                skipped.append({"path": str(src), "reason": str(exc)})
    _remove_empty_dirs(dirs, keep)
    if not skipped:
        d["undone"] = True
        journal.write_text(json.dumps(d, indent=1), encoding="utf-8")
    return {"restored": restored, "skipped": skipped}
