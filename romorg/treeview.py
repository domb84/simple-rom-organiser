"""The folder trees a library build would leave: the system's ROM folder and its part of the archive folder, as a preview.

Nothing here touches a file. From the plan of a build (``organiser.LibraryPlan``) and the moves the build would make to the
archive folder, :func:`build` works out where every file ends up and puts the two results in an :class:`Index` each, which answers
"what is in this folder?" one level at a time (a tree of tens of thousands of files is never sent whole).

Every file carries what the build does to it:

* in the ROM folder: ``same`` (untouched), ``renamed`` (a new name in its folder), ``moved`` (another folder), ``new`` (a playlist
  the build writes), ``ignored`` (a file the scan does not look at: it stays where it is);
* in the archive: ``existing`` (already there), ``incoming`` (set aside by this build), ``copy`` (a save of several games, copied
  under each), ``saves`` (an emulator's saves archived with their game).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

__all__ = ["Index", "Entry", "build", "collect", "index", "final_files", "MISSING", "SAVEONLY", "CONVERTED", "DELETED", "LIBRARY", "ARCHIVE", "SAME", "RENAMED", "MOVED", "NEW", "IGNORED", "EXISTING", "INCOMING", "COPY", "SAVES", "WHDLOAD"]

SAME, RENAMED, MOVED, NEW, IGNORED = "same", "renamed", "moved", "new", "ignored"
EXISTING, INCOMING, COPY, SAVES = "existing", "incoming", "copy", "saves"
MISSING = "missing"            # (a game the folder lacks: no file, listed where it would be)
CONVERTED = "converted"        # (the CHD a build makes of a raw disc: a new file, its size not known before)
DELETED = "deleted"            # (an original the build deletes after converting it)
SAVEONLY = "saveonly"          # (saves of a game that is not in the database's list: no file, listed by the title the saves are known by)
WHDLOAD = "whdload"            # (a file of a WHDLoad install downloaded next to a game's disks, a WHDLoad game built from them: stays, no game of its own)
LIBRARY, ARCHIVE = "library", "archive"
_MOVE = ("move", "rename")


class Index:
    """A file tree held as folders -> (sub-folders, files), with the totals of everything below each folder."""

    def __init__(self, root: str = "") -> None:
        self.root = root
        self.dirs: Dict[Tuple[str, ...], Dict[str, Any]] = {(): self._node()}

    @staticmethod
    def _node() -> Dict[str, Any]:
        return {"dirs": set(), "files": [], "n": 0, "bytes": 0, "changed": 0, "missing": 0, "states": {}}

    def add(self, parts: Iterable[str], size: int = 0, state: str = SAME, origin: str = "", ref: Any = None) -> None:
        parts = tuple(parts)
        if not parts:
            return
        folder: Tuple[str, ...] = ()
        changed = state not in (SAME, EXISTING, IGNORED, MISSING, SAVEONLY)
        real = state not in (MISSING, SAVEONLY, DELETED)         # (a missing game is listed, but it is no file; nor is a file the build deletes)

        def count(node: Dict[str, Any]) -> None:
            node["n"] += real
            node["missing"] += state == MISSING
            node["bytes"] += size
            node["changed"] += changed
            node["states"][state] = node["states"].get(state, 0) + 1

        for i, name in enumerate(parts[:-1]):
            node = self.dirs[folder]
            node["dirs"].add(name)
            count(node)
            folder = parts[:i + 1]
            if folder not in self.dirs:
                self.dirs[folder] = self._node()
        node = self.dirs[folder]
        count(node)
        node["files"].append((parts[-1], size, state, origin, ref))

    def children(self, path: str = "", offset: int = 0, limit: int = 300) -> Optional[Dict[str, Any]]:
        """The folders and files directly in ``path`` (``/`` separated, "" = the top), or None when there is no such folder.
        Folders come with the number of files, the bytes and the changes below them; files are cut to ``limit`` from ``offset``."""
        key = tuple(p for p in path.replace("\\", "/").split("/") if p)
        node = self.dirs.get(key)
        if node is None:
            return None
        dirs = []
        for name in sorted(node["dirs"], key=str.casefold):
            sub = self.dirs[key + (name,)]
            dirs.append({"name": name, "files": sub["n"], "bytes": sub["bytes"], "changed": sub["changed"], "missing": sub["missing"],
                         "states": dict(sub["states"])})
        files = sorted(node["files"], key=lambda f: f[0].casefold())
        page = files[max(0, offset):max(0, offset) + max(1, limit)]
        return {"path": "/".join(key), "dirs": dirs, "total_files": len(files), "offset": max(0, offset),
                "files": [{"name": n, "size": s, "state": st, "from": o, "ref": r} for n, s, st, o, r in page],
                "files_below": node["n"], "bytes": node["bytes"], "changed": node["changed"]}

    def totals(self) -> Dict[str, int]:
        top = self.dirs[()]
        return {"files": top["n"], "bytes": top["bytes"], "changed": top["changed"], "missing": top["missing"]}


def _size(path: Any) -> int:
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def _files_under(path: Path) -> List[Path]:
    if not os.path.isdir(path):
        return [path]
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".romorg"))
        out.extend(Path(dirpath) / f for f in sorted(filenames) if not f.startswith(".romorg"))
    return out


def final_files(plan: Any) -> List[Tuple[Path, Optional[Path], int]]:
    """``(file, where the build leaves it, the index of its op in the plan)`` for every file of the plan (where = None: the build
    removes it - a stale playlist of the app's own). A disc game's op stands for a folder: each of its files is listed."""
    out: List[Tuple[Path, Optional[Path], int]] = []
    for n, op in enumerate(plan.ops):
        status = getattr(op, "status", "")
        if hasattr(op, "moves"):                                   # a disc game (or a loose file of a disc system)
            moves = dict(op.moves) if status in _MOVE else {}
            unit = getattr(op, "unit", None)
            files = list(unit.files) if unit is not None else _files_under(Path(op.src))
            seen = set()
            for f in files:
                seen.add(f)
                out.append((Path(f), Path(moves.get(f, f)), n))
            for s, d in moves.items():                             # (a move of a file the unit does not list)
                if s not in seen:
                    out.append((Path(s), Path(d), n))
        elif status == "delete":
            out.append((Path(op.src), None, n))
        else:
            out.append((Path(op.src), Path(op.dst) if status in _MOVE else Path(op.src), n))
    return out


def _rel(path: Path, base: Path) -> Optional[Tuple[str, ...]]:
    try:
        return Path(path).relative_to(base).parts
    except ValueError:
        return None


class Entry(NamedTuple):
    """One file of the trees."""
    side: str                   # LIBRARY | ARCHIVE
    parts: Tuple[str, ...]      # its path below the side's folder
    size: int
    state: str
    origin: str                 # where it comes from ("" when it does not move)
    op: int                     # the index of its op in the plan (-1: no op: an archived file, a hidden file, a copy)


def _in_install(d: Path, root: Path, seen: Dict[str, bool]) -> bool:
    """Whether folder ``d`` (under ``root``) is in a WHDLoad install folder (one with ``folders.WHDLOAD_MARKER``)."""
    from .folders import WHDLOAD_MARKER
    key = os.fspath(d)
    if key not in seen:
        if d == root or root not in d.parents:
            seen[key] = False
        else:
            seen[key] = (d / WHDLOAD_MARKER).is_file() or _in_install(d.parent, root, seen)
    return seen[key]


def collect(root: Path, plan: Any, archive_base: Optional[Path] = None, to_archive: Iterable[Any] = (),
            playlists: Iterable[Any] = ()) -> List[Entry]:
    """Every file of the system's ROM folder and of its part of the archive as a build would leave them.

    ``archive_base``: ``<archive>/<system folder name>`` (None: no archive folder is used, so what is set aside stays in the ROM
    folder's ``_excluded`` ...). ``to_archive``: the moves the build makes to the archive (``sortroot.SMove``: the files set aside, the
    copies of saves that belong to several games, what earlier builds left in the reserved folders, an emulator's saves)."""
    root = Path(root)
    out: List[Entry] = []
    leaving: Dict[str, Any] = {}
    extra: List[Any] = []
    for m in to_archive:
        if getattr(m, "copy", False) or _rel(m.src, root) is None:
            extra.append(m)                                         # a copy, or something from outside the ROM folder (an emulator's saves)
        else:
            leaving[os.fspath(m.src)] = m
    moving = set(leaving)
    known = set()
    for src, final, n in final_files(plan):
        known.add(os.fspath(src))
        if final is None:
            continue
        m = leaving.pop(os.fspath(src), None)
        size = _size(src)
        if m is not None and archive_base is not None:
            parts = _rel(m.dst, archive_base)
            if parts is not None:
                out.append(Entry(ARCHIVE, tuple(parts), size, INCOMING, "/".join(_rel(src, root) or (src.name,)), n))
                continue
        parts = _rel(final, root)
        if parts is None:
            continue
        was = _rel(src, root) or (src.name,)
        state = SAME if tuple(was) == tuple(parts) else RENAMED if was[:-1] == parts[:-1] else MOVED
        out.append(Entry(LIBRARY, tuple(parts), size, state, "/".join(was) if state != SAME else "", n))
    if root.is_dir():                                               # what the scan does not look at (hidden files, the app's own notes
        whd: Dict[str, bool] = {}                                   # aside, WHDLoad installs): it is in the folder all the same, and stays
        for f in _files_under(root):
            if os.fspath(f) not in known and os.fspath(f) not in moving:
                parts = _rel(f, root)
                if parts:
                    ours = f.suffix.lower() == ".lha" and _built_whdload(f)
                    out.append(Entry(LIBRARY, tuple(parts), _size(f), WHDLOAD if ours or _in_install(f.parent, root, whd) else IGNORED, "", -1))
    for p in playlists:                                             # the playlists the build writes
        if getattr(p, "status", "") == "write":
            parts = _rel(Path(p.path), root)
            if parts is not None:
                out.append(Entry(LIBRARY, tuple(parts), 0, NEW, "", -1))
    if archive_base is not None:
        for m in list(leaving.values()) + extra:                    # what the plan does not list: earlier builds' leftovers, copies, saves
            parts = _rel(m.dst, archive_base)
            if parts is None:
                continue
            for f in _files_under(Path(m.src)) if not getattr(m, "copy", False) else [Path(m.src)]:
                sub = () if f == Path(m.src) else f.relative_to(m.src).parts
                origin_parts = _rel(f, root)
                state = COPY if getattr(m, "copy", False) else INCOMING if origin_parts is not None else SAVES
                size = getattr(m, "size", 0) if getattr(m, "copy", False) else _size(f)
                out.append(Entry(ARCHIVE, tuple(parts) + tuple(sub), size, state,
                                 "/".join(origin_parts) if origin_parts is not None else os.fspath(f), -1))
        if archive_base.is_dir():
            for f in _files_under(archive_base):                    # what is in the archive already
                parts = _rel(f, archive_base)
                if parts:
                    out.append(Entry(ARCHIVE, tuple(parts), _size(f), EXISTING, "", -1))
    return out


def _built_whdload(path: Path) -> bool:
    from .scanner import is_built_whdload
    return is_built_whdload(path)


def index(entries: Iterable[Any], root: str = "", ref: Any = None) -> Index:
    """An :class:`Index` of ``entries`` (each with ``parts``, ``size``, ``state``, ``origin``); ``ref(entry)`` is kept with each file
    (what the caller wants back with it: the row it belongs to)."""
    out = Index(root)
    for e in entries:
        out.add(e.parts, e.size, e.state, e.origin, ref(e) if ref is not None else None)
    return out


def build(root: Path, plan: Any, archive_base: Optional[Path] = None, to_archive: Iterable[Any] = (),
          playlists: Iterable[Any] = ()) -> Tuple[Index, Index]:
    """``(the ROM folder after the build, the system's archive folder after it)`` (see :func:`collect`)."""
    entries = collect(root, plan, archive_base, to_archive, playlists)
    return (index((e for e in entries if e.side == LIBRARY), str(root)),
            index((e for e in entries if e.side == ARCHIVE), str(archive_base) if archive_base is not None else ""))
