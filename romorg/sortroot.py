"""Sort a folder full of ROMs into one folder per system, and keep the ROM folders clean.

Two jobs, both plain moves with a journal (so they can be undone) and neither ever overwrites a file:

* :func:`plan_sort`: files that were identified (by checksum, against every system's DAT) go to their system's folder,
  wherever they were; files that match nothing, and files that are not ROMs at all, go to an "aside" folder
  OUTSIDE the ROM folder (``<aside>/_unmatched/...`` and ``<aside>/_other/...``, with their folders kept).
* :func:`plan_sweep`: what a library build set aside inside a system's folder (``_excluded``, ``_superseded``,
  ``_incomplete``, ``_duplicates``, ``_unmatched``) moves to ``<aside>/<system folder>/<same folder>``, so the ROM folder only
  holds what you keep.

The identification itself (scanning) is done by the server; this module takes its answer.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import meter, winproc
from .folders import CONVERTED_DIR, RESERVED_DIRS

__all__ = ["SMove", "ASIDE_FOLDERS", "plan_sort", "plan_sweep", "plan_restore", "apply_moves", "undo_moves", "inside",
           "rom_like", "remove_empty_tree", "Names", "UNMATCHED", "move_path", "remove_file", "remove_dir",
           "read_journal"]

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


def plan_sort(root: Path, aside: Optional[Path], folders: Dict[str, Path], flat: Dict[Path, Optional[str]],
              discs: Sequence[dict], unmatched: Iterable[Path], other: Iterable[Path],
              exists: Optional[Callable[[Path], bool]] = None) -> List[SMove]:
    """The moves that sort ``root``.

    ``folders``: system name -> its folder. ``flat``: identified ROM file -> system (None when it matches several systems:
    left for the user). ``discs``: ``{"platform", "top", "folder": bool, "files": [...]}`` per identified disc game.
    ``unmatched``: ROM-like files that match nothing; ``other``: everything else.

    Rules: an identified file already inside its own system's folder stays; elsewhere it moves to that folder (flat).
    Without an archive folder (``aside`` None) files that match nothing and non-ROM files are not moved at all.
    Unmatched files that lie loose (outside every system folder) go to ``<aside>/_unmatched``; those inside a system folder
    are left for that system's library build. Every other file goes to ``<aside>/_other`` (with its path kept): a note or a
    file that happens to lie beside a ROM is no ROM and does not follow it into the system's folder. The one exception is
    a disc game's own folder: whatever is in it stays there and travels with the game."""
    root = Path(root)
    names = Names(exists)
    moves: List[SMove] = []
    sys_dirs = list(folders.values())
    unit_tops = [Path(d["top"]) for d in discs if d.get("folder")]

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
        if inside(path, target):
            continue
        dst = names.free(target / path.name)
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
    if aside is None:                                  # no archive folder: files that match nothing, and non-ROMs, stay where they are
        return moves
    aside = Path(aside)
    for path in sorted(unmatched, key=lambda p: p.as_posix().lower()):
        if any(inside(path, s) for s in sys_dirs) or any(inside(path, t) for t in unit_tops):
            continue
        moves.append(SMove(path, names.free(aside / UNMATCHED / rel(path)), UNMATCHED, size=_size(path)))
    for path in sorted(other, key=lambda p: p.as_posix().lower()):
        if any(inside(path, t) for t in unit_tops):
            continue                                               # a disc game's folder keeps what is in it
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
    with ``only`` no other folder than these is (an undo removes the folders its moves created, not the user's own empty
    folder the archive lies in)."""
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


def remove_empty_tree(root: Path, keep: Iterable[Path] = ()) -> List[str]:
    """Remove every folder under ``root`` that holds nothing (``keep`` and ``root`` itself stay). Returns the folders that
    were removed, deepest first (an undo makes them again)."""
    stops = {os.path.normcase(os.path.realpath(s)) for s in [root, *keep]}
    removed: List[str] = []
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        if os.path.normcase(os.path.realpath(dirpath)) in stops or os.path.islink(dirpath):
            continue
        try:
            remove_dir(dirpath)
            removed.append(str(dirpath))
        except OSError:
            pass
    return removed


# ---- the journal
# A finished journal is one JSON document: {"kind", "moves": [{"from", "to", "kind"}], "made": [folders created], "undone",
# ...}. While the moves run, the file holds one JSON object per line instead: a header ({"kind", "partial": true, ...}),
# then each folder that is created ({"made": path}) and each move, written BEFORE it is made. A run that is killed therefore leaves a record of everything it moved (read_journal reads both forms;
# a line whose move never happened is recognised by its file still being where it was).
def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=1))
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def read_journal(journal: Path) -> dict:
    """A journal as ``{"kind", "moves", "undone", ...}``; ``"partial": True`` when the run that wrote it did not finish."""
    text = Path(journal).read_text(encoding="utf-8")
    try:
        d = json.loads(text)
        if isinstance(d, dict) and not d.get("partial"):
            d.setdefault("moves", [])
            return d
    except ValueError:
        pass
    head: dict = {}
    moves: List[dict] = []
    made: List[str] = []
    for n, line in enumerate(text.splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:
            continue                                   # a torn last line: that move was not made
        if not isinstance(rec, dict):
            continue
        if n == 0 and "from" not in rec and "made" not in rec:
            head = rec
        elif "from" in rec and "to" in rec:
            moves.append(rec)
        elif isinstance(rec.get("made"), str):
            made.append(rec["made"])
    return {"undone": False, **head, "partial": True, "moves": moves, "made": made}


class _Journal:
    def __init__(self, journal_dir: Path, kind: str, extra: Optional[dict], started: Optional[Callable[[Path], None]]) -> None:
        self.dir, self.kind, self.extra, self.started = Path(journal_dir), kind, dict(extra or {}), started
        self.path: Optional[Path] = None
        self.made: List[str] = []
        self._f: Any = None

    def _open(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while True:
            path = self.dir / (f"{self.kind}-{stamp}.json" if n == 1 else f"{self.kind}-{stamp}-{n}.json")
            try:
                self._f = open(path, "x", encoding="utf-8", newline="\n")
                break
            except FileExistsError:
                n += 1
        self.path = path
        self._line({"kind": self.kind, "undone": False, "partial": True, **self.extra})
        if self.started is not None:
            self.started(path)

    def _line(self, rec: dict) -> None:
        self._f.write(json.dumps(rec) + "\n")
        self._f.flush()                                # survives a killed process

    def intend(self, rec: dict) -> None:
        if self._f is None:
            self._open()
        self._line(rec)

    def mkdirs(self, folder: Path) -> None:
        """``mkdir -p``; the folders this creates are written down (an undo removes these, and no others, when empty)."""
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
            self.made.append(str(d))
            self.intend({"made": str(d)})

    def close(self, done: List[dict]) -> Optional[Path]:
        """The finished document (or no file at all when nothing was moved)."""
        if self._f is None:
            return None
        self._f.close()
        self._f = None
        assert self.path is not None
        if not done:
            _drop(self.path)
            return None
        _write_json(self.path, {"kind": self.kind, "moves": done, "made": self.made, "undone": False, **self.extra})
        return self.path


def apply_moves(moves: List[SMove], journal_dir: Path, kind: str, keep: Iterable[Path] = (),
                progress: Optional[Callable[[int, int, str], None]] = None,
                cancel: Optional[Callable[[], bool]] = None, extra: Optional[dict] = None,
                started: Optional[Callable[[Path], None]] = None) -> dict:
    """Move the files / folders; never over an existing one. Folders that were emptied are removed (``keep`` are never
    removed). Writes one JSON journal for :func:`undo_moves`: each move is recorded before it is made (``started(path)`` is
    called when the journal file is created), so a run that dies half-way can still be undone."""
    res: Dict[str, Any] = {"moved": 0, "failed": [], "journal": None, "cancelled": False}
    done: List[dict] = []
    emptied: set = set()
    journal = _Journal(journal_dir, kind, extra, started)
    reset_retries()
    try:
        for i, m in enumerate(moves):
            if cancel and cancel():
                res["cancelled"] = True
                break
            if progress:
                progress(i, len(moves), m.src.name)
            try:
                if os.path.lexists(m.dst):
                    raise FileExistsError("a file is already there")
                if not os.path.lexists(m.src):
                    raise FileNotFoundError("the file is no longer there")
                journal.mkdirs(m.dst.parent)
                rec = {"from": str(m.src), "to": str(m.dst), "kind": m.kind}
                journal.intend(rec)
                move_path(m.src, m.dst)
                done.append(rec)
                meter.add(m.size)
                emptied.add(m.src.parent)
                res["moved"] += 1
            except (OSError, ValueError) as exc:           # (Windows: with the hint when a path is too long)
                res["failed"].append({"path": str(m.src), "error": winproc.long_path_hint(exc, m.src, m.dst)})
        _remove_empty_dirs(emptied, keep)
    finally:
        for d in reversed(list(journal.made)):         # a folder made for a move that then failed does not stay behind empty
            try:
                os.rmdir(d)
                journal.made.remove(d)
            except OSError:
                pass
        path = journal.close(done)
        res["journal"] = str(path) if path else None
    return res


def undo_moves(journal: Path, keep: Iterable[Path] = ()) -> dict:
    """Move everything back where it came from, unless the original place is taken or the file is gone. Returns
    ``{"restored", "skipped": [{path, reason}], "left"}``; ``left`` moves could not be taken back now and stay in the journal,
    so the undo can be tried again (a file that was in use ...)."""
    journal = Path(journal)
    d = read_journal(journal)
    partial = bool(d.pop("partial", False))
    restored, skipped, dirs = 0, [], set()
    left: List[dict] = []
    reset_retries()
    for m in reversed(d.get("moves", [])):
        src, dst = Path(m["to"]), Path(m["from"])
        if not os.path.lexists(src):
            if partial and os.path.lexists(dst):
                continue                               # written down, then never moved (the run died or the move failed)
            skipped.append({"path": str(src), "reason": "no longer there"})
        elif os.path.lexists(dst):
            skipped.append({"path": str(dst), "reason": "something is at the original place now"})
            left.append(m)
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                move_path(src, dst)
                restored += 1
                dirs.add(src.parent)
            except OSError as exc:
                skipped.append({"path": str(src), "reason": winproc.long_path_hint(exc, src, dst)})
                left.append(m)
    _remove_empty_dirs(dirs, keep, d.get("made") if isinstance(d.get("made"), list) else None)
    if not skipped:
        d["undone"] = True
        _write_json(journal, d)
    elif partial or restored:                          # from now on a journal of what is still to take back
        d["moves"] = list(reversed(left))
        d["undone"] = not left
        _write_json(journal, d)
    return {"restored": restored, "skipped": skipped, "left": len(left)}
