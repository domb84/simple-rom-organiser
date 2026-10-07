"""Build the library in ANOTHER folder: the source folder is only read, never changed.

``Build library`` normally reorganises the folder it scanned (moves files, sets duplicates aside). This module
takes the same plan (``organiser.LibraryPlan``: the DAT matching, the rules, the names) and, instead of moving, puts
the files that the rules keep into a destination folder, laid out exactly as the in-place build would have laid
them out. Excluded, superseded, unmatched ... files simply stay where they are and are not copied.

How a file gets there (``mode``):

``copy``  the file is copied; the source keeps its file (works across drives and on exFAT / FAT SD cards)
``move``  the file is moved out of the source folder (a rename on one drive, else a copy that is checked and then removed)

Safety: the destination may not be inside the source or the other way round; a different file that is already at a
target is never overwritten (reported as a conflict); copies are written to a ``.part`` name and renamed when
complete; every file this module puts in the destination is recorded in ``<dest>/.romorg-library/library.sqlite``
so that a build can be undone (only what is still unchanged is removed) and so that a later run knows what is ours.
Running the build again only adds what is missing.

Sync (``sync=True``): the build also brings the destination in line with the source. A file this module built earlier
that the rules no longer keep (or whose source is gone) is removed; one whose source changed is replaced; one that was
edited in the destination since is left alone and reported. Only files recorded in the manifest are ever removed. A
sync that would keep nothing, or remove most of the library, is refused unless the caller says it is meant. Removed files
are recorded, so "undo" puts them back from the source where it still has them.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, List, Optional, Sequence

__all__ = ["MODES", "ExportError", "Transfer", "PlaylistWrite", "ExportPlan", "check_destination", "kept_files",
           "plan_export", "apply_export", "list_runs", "undo_run", "MANIFEST_DIR", "Removal", "MASS_MIN", "normalize_mode"]

MODES = ("copy", "move")


def normalize_mode(mode: Any) -> str:
    """``copy`` or ``move``; anything else (the old automatic / link modes of earlier settings) is a copy."""
    return mode if mode in MODES else "copy"
MANIFEST_DIR = ".romorg-library"
DB_NAME = "library.sqlite"
PART_SUFFIX = ".romorg.part"
MASS_MIN = 20                  # a sync removing more than this many files AND over half of the library needs a say-so
CHUNK = 8 << 20
COMMIT_EVERY = 100
ProgressFn = Callable[[int, int, str], None]


class ExportError(Exception):
    """The destination cannot be used, or the build could not start."""


@dataclass
class Transfer:
    src: Path
    rel: str                  # path inside the destination, with "/" separators
    size: int
    action: str               # copy | move | exists | conflict | replace
    reason: str = ""
    via: str = ""             # replace: how the new file is made (copy)

    @property
    def moves_data(self) -> bool:
        return self.action in ("copy", "move", "replace")


@dataclass
class Removal:
    rel: str
    how: str                  # how the manifest says it was made (copy | move | playlist; older builds: hardlink | symlink)
    size: int = 0
    reason: str = ""          # why it goes ("not kept by the rules any more")
    skip: str = ""            # set: it stays, and this says why (edited since it was built ...)


@dataclass
class PlaylistWrite:
    rel: str
    text: str
    action: str               # write | exists | conflict
    reason: str = ""


@dataclass
class ExportPlan:
    root: Path
    dest: Path
    mode: str
    items: List[Transfer] = field(default_factory=list)
    playlists: List[PlaylistWrite] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    sync: bool = False
    removals: List[Removal] = field(default_factory=list)
    owned_total: int = 0
    same_fs: bool = False

    def counts(self) -> dict:
        out = {"copy": 0, "move": 0, "exists": 0, "conflict": 0, "replace": 0}
        for t in self.items:
            out[t.action] = out.get(t.action, 0) + 1
        out["remove"] = sum(1 for r in self.removals if not r.skip)
        out["kept_edited"] = sum(1 for r in self.removals if r.skip)
        return out

    def to_remove(self) -> List[Removal]:
        return [r for r in self.removals if not r.skip]

    def mass_removal(self) -> bool:
        n = len(self.to_remove())
        return n > MASS_MIN and n * 2 > self.owned_total

    def bytes_to_copy(self) -> int:
        """Bytes that are written: copies, replacements and moves to another drive (a move on one drive writes nothing)."""
        return sum(t.size for t in self.items if t.action in ("copy", "replace") or (t.action == "move" and not self.same_fs))

    def bytes_linked(self) -> int:
        """Bytes moved without being written again (a rename on the same drive)."""
        return sum(t.size for t in self.items if t.action == "move" and self.same_fs)

    def pending(self) -> int:
        """Things a build would do (files transferred and playlists written)."""
        return (sum(1 for t in self.items if t.moves_data) + sum(1 for p in self.playlists if p.action == "write")
                + len(self.to_remove()))


# --------------------------------------------------------------------------- the destination
def _real(p: Path) -> str:
    return os.path.normcase(os.path.realpath(p))


def check_destination(root: Path, dest: Path) -> Path:
    """The absolute destination path, or :class:`ExportError`: the folders must not overlap, the destination must be
    a folder (or creatable) and writable."""
    if not str(dest).strip():
        raise ExportError("Choose the folder to build the library in.")
    dest = Path(os.path.abspath(os.path.expanduser(str(dest))))
    a, b = _real(root), _real(dest)
    if a == b:
        raise ExportError("The destination is the source folder itself. Choose another folder, or use "
                          "\"build in this folder\" to reorganise the source.")
    if b.startswith(a + os.sep):
        raise ExportError("The destination is inside the source folder: it would be scanned as part of the source. "
                          "Choose a folder outside it.")
    if a.startswith(b + os.sep):
        raise ExportError("The source folder is inside the destination. Choose a folder that does not contain the "
                          "source.")
    if dest.exists() and not dest.is_dir():
        raise ExportError(f"{dest} is a file, not a folder.")
    probe = dest
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        raise ExportError(f"Cannot write to {probe}.")
    return dest


# --------------------------------------------------------------------------- what a library plan keeps
def kept_files(plan: Any, root: Path, sidecars: bool = False) -> Iterator[tuple]:
    """``(source file, final path inside root)`` of every file the plan's rules keep, in plan order.

    A kept op is one that ends in the library (``move`` / ``ok``) and is not set aside for any reason or sent to a
    reserved folder. A disc unit travels as the CHD or the files of its raw set; its sidecars (save states, memory
    cards, notes) stay behind unless ``sidecars``."""
    for op in plan.ops:
        if getattr(op, "kind", "") == "m3u" or op.status not in ("move", "ok"):
            continue
        if getattr(op, "code", "") or getattr(op, "unmatched", False) or getattr(op, "folder", ""):
            continue
        unit = getattr(op, "unit", None)
        if unit is not None and getattr(unit, "files", None) is not None:
            final = {s: d for s, d in (getattr(op, "moves", None) or [])}
            keep = list(unit.files) if (sidecars or unit.kind == "raw") else [unit.path]
            for f in keep:
                yield Path(f), Path(final.get(f, f))
        else:
            yield Path(op.src), Path(op.dst)


def _rel(path: Path, root: Path) -> Optional[str]:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _same_fs(a: Path, b: Path) -> bool:
    probe = b
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return os.stat(a).st_dev == os.stat(probe).st_dev
    except OSError:
        return False


def _existing_state(src: Path, target: Path, size: int, owned: Optional[tuple]) -> Optional[str]:
    """``None`` when nothing is at ``target``; ``"exists"`` when it is (a link to / a copy of) ``src``;
    ``"conflict"`` when another file is there."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError:
        return "conflict"
    try:
        if os.path.islink(target):
            return "exists" if os.path.realpath(target) == os.path.realpath(src) else "conflict"
        if os.path.samefile(src, target):
            return "exists"
    except OSError:
        pass
    if st.st_size != size:
        return "conflict"
    sst = os.stat(src)
    if st.st_mtime_ns == sst.st_mtime_ns or (owned is not None and owned[0] == size):
        return "exists"
    return "conflict"


def _unchanged(dest: Path, rel: str, rec: tuple) -> bool:
    """True while the file at ``rel`` is still what the build made (``rec`` = ``(size, mtime_ns, how, src)``)."""
    p = dest / rel
    try:
        if rec[2] == "symlink":
            return os.path.islink(p) and os.path.realpath(p) == os.path.realpath(rec[3])
        st = os.stat(p)
        if os.path.islink(p):
            return False
        if rec[2] == "hardlink":
            return st.st_size == rec[0]
        return st.st_size == rec[0] and st.st_mtime_ns == rec[1]
    except OSError:
        return False


def plan_export(plan: Any, root: Path, dest: Path, mode: str = "copy", sidecars: bool = False,
                sync: bool = False) -> ExportPlan:
    """Turn a library plan into transfers for ``dest`` (nothing is written). ``sync`` also plans the removal of what an
    earlier build put there and the rules no longer keep, and the replacement of what changed in the source."""
    mode = normalize_mode(mode)
    if mode == "move" and sync:
        raise ExportError("Keeping the destination in sync needs the original files to stay where they are: use Copy, "
                          "or switch sync off.")
    root = Path(root)
    dest = check_destination(root, Path(dest))
    ep = ExportPlan(root, dest, mode, sync=sync)
    owned = _owned(dest)
    ep.owned_total = len(owned)
    same_fs = ep.same_fs = _same_fs(root, dest)
    seen: dict = {}
    for src, final in kept_files(plan, root, sidecars):
        rel = _rel(final, root)
        if rel is None:
            continue
        key = os.path.normcase(rel).casefold()
        if key in seen:
            ep.items.append(Transfer(src, rel, 0, "conflict", f"another file also becomes {rel}"))
            continue
        seen[key] = True
        try:
            size = os.stat(src).st_size
        except OSError as exc:
            ep.items.append(Transfer(src, rel, 0, "conflict", f"cannot read the source file: {exc.strerror or exc}"))
            continue
        target = dest / rel
        state = _existing_state(src, target, size, None if sync else owned.get(rel))
        if state == "conflict" and sync and rel in owned:
            if _unchanged(dest, rel, owned[rel]):          # ours, untouched, and the source moved on: refresh it
                action = "replace"
                ep.items.append(Transfer(src, rel, size, action, "the source changed", "copy"))
                continue
            ep.items.append(Transfer(src, rel, size, "conflict", "edited in the destination since it was built - left alone"))
            continue
        if state == "exists":
            ep.items.append(Transfer(src, rel, size, "exists", "already there"))
            continue
        if state == "conflict":
            ep.items.append(Transfer(src, rel, size, "conflict", "a different file is already there - left alone"))
            continue
        ep.items.append(Transfer(src, rel, size, "move" if mode == "move" else "copy"))
    for p in getattr(plan, "playlists", ()) or ():
        if p.status not in ("write", "ok"):
            continue
        rel = _rel(Path(p.path), root)
        if rel is None:
            continue
        text = "\n".join(p.lines) + "\n"
        target = dest / rel
        if not os.path.lexists(target):
            ep.playlists.append(PlaylistWrite(rel, text, "write"))
            continue
        try:
            current = target.read_text(encoding="utf-8", errors="surrogateescape").replace("\r\n", "\n")
        except OSError:
            current = None
        if current == text:
            ep.playlists.append(PlaylistWrite(rel, text, "exists", "already there"))
        elif current is not None and current.split("\n", 1)[0].startswith("# Generated by simple-rom-organiser"):
            ep.playlists.append(PlaylistWrite(rel, text, "write", "replaces an older playlist of this app"))
        else:
            ep.playlists.append(PlaylistWrite(rel, text, "conflict", "a different file is already there - left alone"))
    if sync:
        wanted = {t.rel for t in ep.items} | {p.rel for p in ep.playlists}
        if owned and not any(t.action != "conflict" for t in ep.items):
            raise ExportError("The rules keep nothing from the source (is it empty, or not mounted?). Nothing is removed "
                              "from the destination.")
        for rel, rec in sorted(owned.items()):
            if rel in wanted:
                continue
            if not os.path.lexists(dest / rel):
                continue                                    # already gone: the manifest is tidied when applied
            if _unchanged(dest, rel, rec):
                ep.removals.append(Removal(rel, rec[2], rec[0], "not kept by the rules any more"))
            else:
                ep.removals.append(Removal(rel, rec[2], rec[0], "", "edited since it was built - left in place"))
        if ep.mass_removal():
            ep.notes.append(f"This sync removes {len(ep.to_remove())} of the {ep.owned_total} files built before. "
                            "Check the rules and that the source is complete.")
    if mode == "move" and not same_fs and any(t.action == "move" for t in ep.items):
        ep.notes.append("The destination is on another drive than the source: files are copied there and then removed from the "
                        "source (a move on one drive is instant).")
    return ep


# --------------------------------------------------------------------------- the manifest
def _db_path(dest: Path) -> Path:
    return dest / MANIFEST_DIR / DB_NAME


def _connect(dest: Path, create: bool = True) -> Optional[sqlite3.Connection]:
    path = _db_path(dest)
    if not create and not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started TEXT, source TEXT, mode TEXT,
                                         summary TEXT, undone INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, src TEXT, size INTEGER, mtime_ns INTEGER,
                                          how TEXT, run INTEGER);
        CREATE TABLE IF NOT EXISTS dirs (path TEXT PRIMARY KEY, run INTEGER);
        CREATE TABLE IF NOT EXISTS removed (path TEXT, src TEXT, size INTEGER, mtime_ns INTEGER, how TEXT, run INTEGER);
    """)
    return db


def _owned(dest: Path) -> dict:
    """``{rel: (size, mtime_ns, how, src)}`` of what earlier builds put in ``dest``."""
    db = _connect(dest, create=False)
    if db is None:
        return {}
    try:
        return {r[0]: (r[1], r[2], r[3], r[4]) for r in db.execute("SELECT path, size, mtime_ns, how, src FROM files")}
    except sqlite3.Error:
        return {}
    finally:
        db.close()


# --------------------------------------------------------------------------- applying
def _copy(src: Path, tmp: Path, cancel: Optional[Callable[[], bool]], tick: Callable[[int], None]) -> None:
    with open(src, "rb") as fin, open(tmp, "xb") as fout:
        if hasattr(os, "copy_file_range"):
            try:
                while True:
                    n = os.copy_file_range(fin.fileno(), fout.fileno(), CHUNK)
                    if n == 0:
                        break
                    tick(n)
                    if cancel is not None and cancel():
                        raise InterruptedError("cancelled")
                return
            except OSError:
                fin.seek(0)
                fout.seek(0)
                fout.truncate()
                tick(-1)                        # progress restarts for this file
        while True:
            block = fin.read(CHUNK)
            if not block:
                break
            fout.write(block)
            tick(len(block))
            if cancel is not None and cancel():
                raise InterruptedError("cancelled")


def _transfer(t: Transfer, target: Path, mode: str, cancel, tick) -> str:
    """Do one transfer; returns how it was done (``copy`` | ``move``). A move that had to copy and then could not remove the
    original is reported as a copy (``t.reason`` says why)."""
    src = t.src
    tmp = target.with_name(target.name + PART_SUFFIX)
    if t.action == "move":
        if os.path.lexists(target):
            raise FileExistsError("a file appeared there")
        try:
            os.replace(src, target)
            tick(t.size)
            return "move"                               # same drive: a rename, nothing is written again
        except OSError:
            pass                                        # another drive: copy, check, then remove the original
    done = [0]

    def count(n: int) -> None:
        if n < 0:
            tick(-done[0])
            done[0] = 0
        else:
            done[0] += n
            tick(n)
    try:
        _copy(src, tmp, cancel, count)
        sst = os.stat(src)
        if os.stat(tmp).st_size != sst.st_size:
            raise OSError("the copy has another size than the original")
        os.utime(tmp, ns=(sst.st_atime_ns, sst.st_mtime_ns))
        os.replace(tmp, target)
    except BaseException:
        tick(-done[0])
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if t.action == "move":
        try:
            os.unlink(src)
            return "move"
        except OSError as exc:
            t.reason = f"copied, but the original could not be removed: {exc}"
    return "copy"


def apply_export(plan: ExportPlan, progress: Optional[ProgressFn] = None,
                 cancel: Optional[Callable[[], bool]] = None, allow_mass: bool = False) -> dict:
    """Perform the plan. ``progress(done bytes, total bytes, current file)``. Returns ``{"created", "copied",
    "moved", "playlists", "skipped", "failed": [{rel, error}], "bytes", "cancelled", "run"}``."""
    dest = plan.dest
    if plan.mass_removal() and not allow_mass:
        raise ExportError(f"This sync would remove {len(plan.to_remove())} of the {plan.owned_total} files built before. "
                          "Confirm that on purpose, or check the rules and the source first.")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        db = _connect(dest)
    except (OSError, sqlite3.Error) as exc:
        raise ExportError(f"cannot prepare the destination: {exc}") from exc
    assert db is not None
    todo = [t for t in plan.items if t.moves_data]
    writes = [p for p in plan.playlists if p.action == "write"]
    total = sum(t.size for t in todo) or 1
    done_bytes = [0]
    out = {"created": 0, "copied": 0, "moved": 0, "playlists": 0, "replaced": 0, "removed": 0,
           "skipped": sum(1 for t in plan.items if t.action in ("exists", "conflict")) + sum(
               1 for p in plan.playlists if p.action in ("exists", "conflict")),
           "failed": [], "bytes": 0, "cancelled": False, "run": None}
    cur = db.execute("INSERT INTO runs (started, source, mode, summary) VALUES (?, ?, ?, '')",
                     (time.strftime("%Y-%m-%dT%H:%M:%S"), str(plan.root), plan.mode))
    run = out["run"] = cur.lastrowid
    db.commit()
    made_dirs: List[str] = []
    pending = 0

    def make_dirs(directory: Path) -> None:
        missing = []
        d = directory
        while not d.exists() and d != dest.parent:
            missing.append(d)
            d = d.parent
        for d in reversed(missing):
            try:
                d.mkdir()
            except FileExistsError:
                continue
            made_dirs.append(str(d))
            db.execute("INSERT OR IGNORE INTO dirs (path, run) VALUES (?, ?)", (str(d.relative_to(dest).as_posix()) or ".", run))

    def tick(n: int) -> None:
        done_bytes[0] += n

    cancelled = lambda: bool(cancel and cancel())   # noqa: E731
    try:
        for t in todo:
            if cancelled():
                out["cancelled"] = True
                break
            target = dest / t.rel
            if progress:
                progress(done_bytes[0], total, t.rel)
            try:
                make_dirs(target.parent)
                if os.path.lexists(target) and t.action != "replace":
                    raise FileExistsError("a file appeared there since the preview")
                how = _transfer(t, target, plan.mode, cancel, tick)
            except InterruptedError:
                out["cancelled"] = True
                break
            except (OSError, ValueError) as exc:
                out["failed"].append({"rel": t.rel, "error": str(exc)})
                continue
            try:
                mt = os.stat(target).st_mtime_ns
            except OSError:
                mt = 0
            if t.action == "move" and how == "copy":
                out["failed"].append({"rel": t.rel, "error": t.reason})
            db.execute("INSERT OR REPLACE INTO files (path, src, size, mtime_ns, how, run) VALUES (?, ?, ?, ?, ?, ?)",
                       (t.rel, str(t.src), t.size, mt, how, run))
            out["created"] += 1
            out["replaced"] += 1 if t.action == "replace" else 0
            out["bytes"] += t.size if how == "copy" or (how == "move" and not plan.same_fs) else 0
            out["moved" if how == "move" else "copied"] += 1
            pending += 1
            if pending >= COMMIT_EVERY:
                db.commit()
                pending = 0
        if not out["cancelled"]:
            for p in writes:
                if cancelled():
                    out["cancelled"] = True
                    break
                target = dest / p.rel
                try:
                    make_dirs(target.parent)
                    tmp = target.with_name(target.name + PART_SUFFIX)
                    with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
                        f.write(p.text)
                    os.replace(tmp, target)
                except OSError as exc:
                    out["failed"].append({"rel": p.rel, "error": str(exc)})
                    continue
                st = os.stat(target)
                db.execute("INSERT OR REPLACE INTO files (path, src, size, mtime_ns, how, run) VALUES (?, '', ?, ?, 'playlist', ?)",
                           (p.rel, st.st_size, st.st_mtime_ns, run))
                out["playlists"] += 1
        if not out["cancelled"]:
            _remove_stale(plan, db, run, out, cancelled)
        if progress:
            progress(done_bytes[0], total, "")
    finally:
        summary = {k: out[k] for k in ("created", "copied", "moved", "playlists", "skipped", "removed")}
        summary["failed"] = len(out["failed"])
        db.execute("UPDATE runs SET summary = ? WHERE id = ?", (repr(summary), run))
        if not out["created"] and not out["playlists"] and not out["removed"]:
            db.execute("DELETE FROM runs WHERE id = ?", (run,))        # a run that changed nothing is not worth undoing
            out["run"] = None
        db.commit()
        db.close()
    return out


def _remove_stale(plan: ExportPlan, db: sqlite3.Connection, run: int, out: dict, cancelled: Callable[[], bool]) -> None:
    """Sync: delete what the plan says the rules no longer keep (re-checked now: only a file that is still what the
    build made), remember it for undo, tidy empty folders and manifest rows of files that are already gone."""
    dest = plan.dest
    wanted = {t.rel for t in plan.items} | {p.rel for p in plan.playlists}
    for rel, size, mtime_ns, how, src in db.execute("SELECT path, size, mtime_ns, how, src FROM files").fetchall():
        if rel not in wanted and not os.path.lexists(dest / rel):
            db.execute("DELETE FROM files WHERE path = ?", (rel,))
    folders = set()
    for r in plan.to_remove():
        if cancelled():
            out["cancelled"] = True
            break
        row = db.execute("SELECT size, mtime_ns, how, src FROM files WHERE path = ?", (r.rel,)).fetchone()
        if row is None or not _unchanged(dest, r.rel, row):
            out["failed"].append({"rel": r.rel, "error": "changed since the preview - left in place"})
            continue
        try:
            os.unlink(dest / r.rel)
        except OSError as exc:
            out["failed"].append({"rel": r.rel, "error": str(exc)})
            continue
        db.execute("INSERT INTO removed (path, src, size, mtime_ns, how, run) VALUES (?, ?, ?, ?, ?, ?)",
                   (r.rel, row[3], row[0], row[1], row[2], run))
        db.execute("DELETE FROM files WHERE path = ?", (r.rel,))
        out["removed"] += 1
        folders.add((dest / r.rel).parent)
    for d in sorted(folders, key=lambda x: len(x.parts), reverse=True):
        while d != dest and MANIFEST_DIR not in d.parts:
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


# --------------------------------------------------------------------------- undo
def list_runs(dest: Path) -> List[dict]:
    """Builds recorded in ``dest`` that were not undone, newest first: ``{id, started, source, mode, files}``."""
    db = _connect(Path(dest), create=False)
    if db is None:
        return []
    try:
        rows = db.execute("SELECT r.id, r.started, r.source, r.mode, COUNT(f.path) FROM runs r "
                          "LEFT JOIN files f ON f.run = r.id WHERE r.undone = 0 GROUP BY r.id ORDER BY r.id DESC").fetchall()
        gone = dict(db.execute("SELECT run, COUNT(*) FROM removed GROUP BY run").fetchall())
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [{"id": r[0], "started": r[1], "source": r[2], "mode": r[3], "files": r[4], "removed": gone.get(r[0], 0)}
            for r in rows if r[4] or gone.get(r[0])]


def undo_run(dest: Path, run: Optional[int] = None) -> dict:
    """Remove what a build put in ``dest`` (the newest one unless ``run``): a file only while it is still what the
    build made (a copy of the same size and time, a link that still points at the source); anything changed since is
    left and reported. Folders the build created go when they are empty."""
    dest = Path(dest)
    db = _connect(dest, create=False)
    if db is None:
        raise ExportError("There is no library build recorded in that folder.")
    removed = 0
    skipped: List[dict] = []
    try:
        if run is None:
            row = db.execute("SELECT MAX(id) FROM runs WHERE undone = 0").fetchone()
            run = row[0] if row else None
        if run is None:
            raise ExportError("Nothing to undo in that folder.")
        files = db.execute("SELECT path, src, size, mtime_ns, how FROM files WHERE run = ?", (run,)).fetchall()
        for rel, src, size, mtime_ns, how in files:
            p = dest / rel
            if not os.path.lexists(p):
                db.execute("DELETE FROM files WHERE path = ?", (rel,))
                continue
            if how == "move":                               # put the file back where it came from (never over another file)
                try:
                    unchanged = not os.path.islink(p) and os.stat(p).st_size == size and os.stat(p).st_mtime_ns == mtime_ns
                except OSError:
                    unchanged = False
                if not unchanged:
                    skipped.append({"rel": rel, "reason": "changed since it was built - left in place"})
                elif os.path.lexists(src):
                    skipped.append({"rel": rel, "reason": "the original place is taken - left in the library"})
                else:
                    try:
                        Path(src).parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(p), src)
                        removed += 1
                        db.execute("DELETE FROM files WHERE path = ?", (rel,))
                    except OSError as exc:
                        skipped.append({"rel": rel, "reason": str(exc)})
                continue
            try:
                if how == "symlink":
                    ok = os.path.islink(p) and os.path.realpath(p) == os.path.realpath(src)
                elif how == "hardlink":
                    ok = os.path.isfile(p) and os.stat(p).st_size == size
                else:                                   # copy, playlist
                    st = os.stat(p)
                    ok = not os.path.islink(p) and st.st_size == size and st.st_mtime_ns == mtime_ns
            except OSError:
                ok = False
            if not ok:
                skipped.append({"rel": rel, "reason": "changed since it was built - left in place"})
                continue
            try:
                os.unlink(p)
                removed += 1
                db.execute("DELETE FROM files WHERE path = ?", (rel,))
            except OSError as exc:
                skipped.append({"rel": rel, "reason": str(exc)})
        dirs = [r[0] for r in db.execute("SELECT path FROM dirs WHERE run = ?", (run,)).fetchall()]
        removed_dirs = 0
        for d in sorted(dirs, key=lambda x: x.count("/"), reverse=True):
            try:
                os.rmdir(dest / d)
                removed_dirs += 1
            except OSError:
                pass
            db.execute("DELETE FROM dirs WHERE path = ?", (d,))
        restored = 0
        for rel, src, size, mtime_ns, how in db.execute(
                "SELECT path, src, size, mtime_ns, how FROM removed WHERE run = ?", (run,)).fetchall():
            if how == "playlist":
                continue                                    # written again by the next build
            p = dest / rel
            if os.path.lexists(p):
                skipped.append({"rel": rel, "reason": "a file is there now - not restored"})
            elif not os.path.isfile(src):
                skipped.append({"rel": rel, "reason": "the source file is gone - cannot restore"})
            else:
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    t = Transfer(Path(src), rel, size, "copy")
                    got = _transfer(t, p, "copy", None, lambda n: None)
                    st = os.stat(p)
                    db.execute("INSERT OR REPLACE INTO files (path, src, size, mtime_ns, how, run) VALUES (?, ?, ?, ?, ?, 0)",
                               (rel, src, st.st_size, st.st_mtime_ns, got))
                    restored += 1
                except OSError as exc:
                    skipped.append({"rel": rel, "reason": f"cannot restore: {exc}"})
                    continue
            db.execute("DELETE FROM removed WHERE path = ? AND run = ?", (rel, run))
        if not skipped:
            db.execute("UPDATE runs SET undone = 1 WHERE id = ?", (run,))
        db.commit()
    finally:
        db.close()
    return {"run": run, "removed": removed, "restored": restored, "removed_dirs": removed_dirs, "skipped": skipped}
