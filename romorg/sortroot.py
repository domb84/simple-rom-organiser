"""Moves of what a library build sets aside.

* :func:`plan_sweep`: what a library build set aside inside a system's folder (``_excluded``, ``_superseded``,
  ``_incomplete``, ``_duplicates``, ``_unmatched``) moves to ``<archive>/<system folder>/<same folder>``, so the ROM folder only
  holds what you keep. :func:`apply_moves` does any list of moves (never over an existing file).
"""

from __future__ import annotations

import errno
import filecmp
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import meter, winproc
from .folders import CONVERTED_DIR, RESERVED_DIRS, archive_dir

__all__ = ["SMove", "ASIDE_FOLDERS", "plan_sweep", "count_sweep", "apply_moves", "inside", "overlap", "check_folders", "Names", "UNMATCHED", "move_path", "remove_file", "remove_dir"]

# the folders a library build sets things aside in (the converted originals are the user's safety copies: they stay)
ASIDE_FOLDERS = tuple(d for d in RESERVED_DIRS if d != CONVERTED_DIR)
UNMATCHED = "_unmatched"
OTHER = "_other"
DISC_EXTS = {".chd", ".cue", ".gdi", ".iso", ".bin", ".img", ".mdf", ".ccd", ".cdi", ".nrg", ".toc", ".pbp", ".rvz", ".wbfs",
             ".gcm", ".ciso", ".cso"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar"}


ALREADY = "already archived"    # an SMove's note: the archive holds this very file at dst already (only the source goes)


@dataclass
class SMove:
    src: Path
    dst: Path
    bucket: str                 # a system name, "_unmatched", "_other", or the reserved folder for a sweep
    note: str = ""
    kind: str = "file"          # file | folder
    size: int = 0
    copy: bool = False          # a copy: ``src`` stays, ``dst`` is a new file
    rom: str = ""               # what the build identified it as (its DAT rom / disc game name; "" for a file no DAT knows)


def overlap(root: Path, dest: Path) -> str:
    """How ``dest`` lies to ``root``: ``same``, ``inside`` (it is in ``root``), ``contains`` (``root`` is in it) or "" (apart).
    Links are followed and, where the file system does, case is ignored."""
    a, b = os.path.normcase(os.path.realpath(root)), os.path.normcase(os.path.realpath(dest))
    if a == b:
        return "same"
    # (the top of a drive already ends in a separator: "E:\\" + "\\" is the start of no path, and the overlap went unseen)
    if b.startswith(a.rstrip(os.sep) + os.sep):
        return "inside"
    if a.startswith(b.rstrip(os.sep) + os.sep):
        return "contains"
    return ""


def check_folders(root: Path, dest: Path) -> Optional[str]:
    """An error text when the archive folder and the ROM folder overlap, else ``None``."""
    return {"same": "The destination is the ROM root itself. Choose another folder.",
            "inside": "The destination is inside the ROM root. Choose a folder outside it.",
            "contains": "The ROM root is inside the destination. Choose a folder that does not contain it."}.get(overlap(root, dest))


def inside(path: Path, folder: Path) -> bool:
    try:
        Path(path).relative_to(folder)
        return True
    except ValueError:
        return False


_VIRTUAL: Dict[str, Callable[[Path], bool]] = {}      # a folder that stands for something else (an S3 archive): who says what is there


def register_virtual(base: Path, exists: Callable[[Path], bool]) -> None:
    """Paths below ``base`` exist when ``exists`` says so (see ``s3archive``); a later call for the same base replaces it."""
    _VIRTUAL[os.path.normcase(str(base))] = exists


def lexists(path: Path) -> bool:
    """``os.path.lexists``, or the answer of the registered stand-in a path lies below."""
    if _VIRTUAL:
        p = os.path.normcase(str(path))
        for base, fn in _VIRTUAL.items():
            if p == base or p.startswith(base.rstrip(os.sep) + os.sep):
                return fn(path if isinstance(path, Path) else Path(path))
    return os.path.lexists(path)


class Names:
    """Free names: a target that is taken (on disk, in a registered stand-in, or by an earlier move of this plan) gets `` (2)``,
    `` (3)`` ..."""

    def __init__(self, exists: Optional[Callable[[Path], bool]] = None) -> None:
        self.taken: set = set()
        self.exists = exists or lexists

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


def plan_sweep(folders: Dict[str, Path], aside: Path, names_in_archive: Optional[Dict[str, str]] = None) -> List[SMove]:
    """Everything a build set aside inside the system folders, to ``<aside>/<system folder name>/<reserved folder>/...``."""
    aside = Path(aside)
    names = Names()
    moves: List[SMove] = []
    for plat, folder in folders.items():
        for reserved in ASIDE_FOLDERS:
            base = folder / reserved
            if not base.is_dir():
                continue
            home = aside / ((names_in_archive or {}).get(plat) or folder.name) / archive_dir(reserved)
            top = os.fspath(base)
            for dirpath, dirnames, filenames in os.walk(top):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                rel = dirpath[len(top) + 1:]                   # (the walk's own text: no Path made for each file to cut the folder off)
                for f in sorted(filenames):
                    if f.startswith(".romorg"):
                        continue
                    src = Path(dirpath) / f
                    moves.append(SMove(src, names.free(home / (rel + os.sep + f if rel else f)), reserved, size=_size(src)))
    return moves


def count_sweep(folders: Dict[str, Path]) -> int:
    """How many files :func:`plan_sweep` would move (``len(plan_sweep(...))`` without planning the targets)."""
    n = 0
    for folder in folders.values():
        for reserved in ASIDE_FOLDERS:
            base = folder / reserved
            if not base.is_dir():
                continue
            for _dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                n += sum(1 for f in filenames if not f.startswith(".romorg"))
    return n


# --------------------------------------------------------------------------- doing it
RETRIES = 3                    # Windows: a file another program holds for a moment (antivirus, the indexer, a preview)
RETRY_SLEEP = 0.1
RETRY_BUDGET = 40              # ... but a run with many blocked files does not wait for each of them
_WIN_BUSY = (5, 32, 33)        # access denied, sharing violation, lock violation
_WIN_NOT_SAME_DEVICE = 17
_budget = [RETRY_BUDGET]


def reset_retries() -> None:
    """A new run: it may wait for files held by other programs again (see ``RETRY_BUDGET``)."""
    _budget[0] = RETRY_BUDGET


def _cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == _WIN_NOT_SAME_DEVICE


def _retry(fn: Callable[..., Any], *args: Any) -> Any:
    """Call ``fn``. On Windows a sharing violation / access denied is tried again a few times after a short wait (elsewhere
    this is one plain call)."""
    n = 0
    while True:
        try:
            return fn(*args)
        except OSError as exc:
            n += 1
            if os.name != "nt" or getattr(exc, "winerror", None) not in _WIN_BUSY or n >= RETRIES or _budget[0] <= 0:
                raise
            _budget[0] -= 1
            time.sleep(RETRY_SLEEP)


def _read_only(path: Any) -> bool:
    """Windows: the read-only attribute is set (it stops a delete, not a rename)."""
    if os.name != "nt":
        return False
    try:
        return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
    except (OSError, AttributeError):
        return False


def _without_read_only(fn: Callable[[Any], None], path: Any) -> None:
    """``fn(path)`` (a delete); on Windows a read-only file / folder has its attribute cleared for it, and gets it back when
    the delete still fails."""
    if not _read_only(path):                           # always the case outside Windows: one plain call
        _retry(fn, path)
        return
    mode = os.lstat(path).st_mode
    os.chmod(path, mode | stat.S_IWRITE)
    try:
        _retry(fn, path)
    except OSError:
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        raise


def remove_file(path: Any) -> None:
    """``os.unlink`` that also removes a read-only file on Windows (as it does everywhere else)."""
    _without_read_only(os.unlink, path)


def remove_dir(path: Any) -> None:
    """``os.rmdir`` (an EMPTY folder only) that also removes a read-only folder on Windows."""
    _without_read_only(os.rmdir, path)


def _drop(path: Any) -> None:
    try:
        remove_file(path)
    except OSError:
        pass


def _move_file_across(src: Path, dst: Path) -> None:
    """Copy to another drive, then remove the original. When the original cannot be removed the copy is taken away again:
    the file is never in two places."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "a file is already there", str(dst))
    try:
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)
            if os.stat(dst).st_size != os.stat(src).st_size:
                raise OSError(errno.EIO, "the copy has another size than the original", str(dst))
    except BaseException:
        _drop(dst)
        raise
    try:
        remove_file(src)
    except BaseException:
        _drop(dst)
        raise


def _move_tree_across(src: Path, dst: Path) -> None:
    """A folder to another drive, file by file. If one file cannot be moved, those already moved go back: the folder is
    whole in one place or the other, never half in each."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "a folder is already there", str(dst))
    moved: List[tuple] = []
    made: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(src):
            here = Path(dirpath)
            there = dst / here.relative_to(src)
            os.mkdir(there)
            made.append(there)
            links = [d for d in dirnames if os.path.islink(here / d)]          # a link to a folder moves as the link it is
            dirnames[:] = [d for d in dirnames if d not in links]
            for name in sorted(filenames) + links:
                _move_file_across(here / name, there / name)
                moved.append((here / name, there / name))
    except BaseException:
        for a, b in reversed(moved):
            try:
                _move_file_across(b, a)
            except OSError:
                pass                                   # it stays in the new place: not lost
        for d in reversed(made):
            try:
                remove_dir(d)
            except OSError:
                pass
        raise
    for dirpath, _dirs, _files in os.walk(src, topdown=False):
        try:
            shutil.copystat(dirpath, dst / Path(dirpath).relative_to(src))
        except OSError:
            pass
        try:
            remove_dir(dirpath)
        except OSError:
            pass                                       # in use: an empty folder stays behind


def move_path(src: Path, dst: Path) -> None:
    """Move a file or a folder to ``dst`` (which must not exist: the callers check). A rename; only when ``dst`` is on
    another drive it is a copy followed by removing the original. Any other failure is raised with nothing changed, so a
    file another program holds open is not copied (which would leave it in two places)."""
    src, dst = Path(src), Path(dst)
    try:
        _retry(os.rename, src, dst)
        return
    except OSError as exc:
        if not _cross_device(exc):
            raise
    if os.path.isdir(src) and not os.path.islink(src):
        _move_tree_across(src, dst)
    else:
        _move_file_across(src, dst)


def _remove_empty_dirs(dirs: Iterable[Path], stop: Iterable[Path], only: Optional[Iterable[str]] = None) -> None:
    """Remove the folders that are empty now, and their parents while those are empty too. ``stop`` are never removed;
    with ``only`` no other folder than these is."""
    stops = {os.path.normcase(os.path.realpath(s)) for s in stop}
    mine = None if only is None else {os.path.normcase(str(x)) for x in only}
    for d in sorted({Path(x) for x in dirs}, key=lambda p: len(p.parts), reverse=True):
        cur = d
        while True:
            if os.path.normcase(os.path.realpath(cur)) in stops or cur == cur.parent:
                break
            if mine is not None and os.path.normcase(str(cur)) not in mine:
                break
            try:
                remove_dir(cur)
            except OSError:
                break
            cur = cur.parent


# ---- moving
def _make_dirs(folder: Path, made: List[str]) -> None:
    """``mkdir -p``; the folders this creates are added to ``made`` (removed again when a move into them fails)."""
    missing: List[Path] = []
    d = Path(folder)
    while not os.path.lexists(d) and d != d.parent:
        missing.append(d)
        d = d.parent
    for d in reversed(missing):
        try:
            os.mkdir(d)
        except FileExistsError:
            continue
        made.append(str(d))


def apply_moves(moves: List[SMove], keep: Iterable[Path] = (),
                progress: Optional[Callable[[int, int, str], None]] = None,
                cancel: Optional[Callable[[], bool]] = None) -> dict:
    """Move the files / folders; never over an existing one. Folders that were emptied are removed (``keep`` are never
    removed). Returns ``{"moved", "failed": [{path, error}], "cancelled"}``."""
    res: Dict[str, Any] = {"moved": 0, "failed": [], "cancelled": False}
    emptied: set = set()
    made: List[str] = []
    reset_retries()
    try:
        for i, m in enumerate(moves):
            if cancel and cancel():
                res["cancelled"] = True
                break
            if progress:
                progress(i, len(moves), m.src.name)
            try:
                if m.note == ALREADY and os.path.isfile(m.dst) and not m.copy:
                    if not filecmp.cmp(m.src, m.dst, shallow=False):
                        raise FileExistsError("another file is already there")
                    remove_file(m.src)                       # (the archive has it: the copy here goes)
                    emptied.add(m.src.parent)
                    res["moved"] += 1
                    continue
                if os.path.lexists(m.dst):
                    raise FileExistsError("a file is already there")
                if not os.path.lexists(m.src):
                    raise FileNotFoundError("the file is no longer there")
                _make_dirs(m.dst.parent, made)
                if m.copy:
                    shutil.copy2(m.src, m.dst)
                else:
                    move_path(m.src, m.dst)
                meter.add(m.size)
                emptied.add(m.src.parent)
                res["moved"] += 1
            except (OSError, ValueError) as exc:           # (Windows: with the hint when a path is too long)
                res["failed"].append({"path": str(m.src), "error": winproc.long_path_hint(exc, m.src, m.dst)})
        _remove_empty_dirs(emptied, keep)
    finally:
        for d in reversed(made):                       # a folder made for a move that then failed does not stay behind empty
            try:
                os.rmdir(d)
            except OSError:
                pass
    return res
