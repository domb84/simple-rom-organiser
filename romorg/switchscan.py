"""Scan a folder of Switch games: what each ``.nsp`` / ``.nsz`` / ``.xci`` / ``.xcz`` is, grouped by game.

Each file is read for a few kilobytes (``switchfmt``); its title is told in this order, the first that answers wins:

1. the **``.cnmt.xml``** in the file (title ID, kind and version, exact);
2. an **NCA** in the file that the **title database** knows (title ID and version; this is how a renamed eShop file is recognised);
3. a **ticket** in the file (title ID);
4. the **``[title ID]`` / ``[v123]``** in the file name (for a game card dump this is the usual way);
5. the title ID in the header of the CNMT NCA, when the user's ``prod.keys`` are given (``switchkeys``): the only step that
   decrypts anything, 512 bytes of it.

For a game card dump the keys come before the name, so a wrong name is caught (and reported).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import switchfmt
from .switchdb import SwitchDb
from .switchkeys import nca_title_id

__all__ = ["SwitchFile", "SwitchGame", "SwitchScan", "identify", "scan_folder"]

ProgressFn = Callable[[int, int, str], None]
_TAGS = re.compile(r"\s*[\[(][^\])]*[\])]")


@dataclass
class SwitchFile:
    path: Path
    size: int
    container: str = ""                 # nsp | xci | ""
    title_id: str = ""
    base_id: str = ""
    kind: str = ""                      # application | update | addon
    version: Optional[int] = None
    how: str = ""                       # which of the ways above told it ("" = not told)
    note: str = ""
    name: str = ""                      # the name the database (or the file's own name) gives
    ncas: int = 0                       # how many NCAs the file holds, and how many of those the title database knows
    known: int = 0
    checksums: str = ""                 # "" (not checked) | ok | damaged  (``switchverify``: every NCA against its own name)
    checksum_note: str = ""

    def public(self) -> Dict[str, Any]:
        return {"path": str(self.path), "file": self.path.name, "size": self.size, "container": self.container,
                "title_id": self.title_id, "base_id": self.base_id, "kind": self.kind, "version": self.version, "how": self.how,
                "note": self.note, "name": self.name, "ncas": self.ncas, "known": self.known, "checksums": self.checksums,
                "checksum_note": self.checksum_note}


@dataclass
class SwitchGame:
    base_id: str
    name: str
    files: List[SwitchFile] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    def public(self) -> Dict[str, Any]:
        base = [f for f in self.files if f.kind == switchfmt.APPLICATION]
        updates = sorted({f.version for f in self.files if f.kind == switchfmt.UPDATE and f.version is not None})
        return {"title_id": self.base_id, "name": self.name, "files": len(self.files), "size": self.size,
                "has_base": bool(base), "base_version": next((f.version for f in base if f.version is not None), None),
                "updates": updates, "addons": sum(1 for f in self.files if f.kind == switchfmt.ADDON),
                "how": sorted({f.how for f in self.files if f.how}),
                "checksums": ("damaged" if any(f.checksums == "damaged" for f in self.files)
                              else "ok" if all(f.checksums == "ok" for f in self.files) else
                              "partly" if any(f.checksums == "ok" for f in self.files) else ""),
                "known": f"{sum(f.known for f in self.files)} of {sum(f.ncas for f in self.files)}" if any(f.known for f in self.files) else "",
                "paths": [str(f.path) for f in self.files]}


@dataclass
class SwitchScan:
    folder: Path
    games: Dict[str, SwitchGame] = field(default_factory=dict)
    unidentified: List[SwitchFile] = field(default_factory=list)
    files: int = 0
    bytes: int = 0


def _clean_name(file_name: str) -> str:
    stem = os.path.splitext(file_name)[0]
    return _TAGS.sub("", stem).strip(" -_.") or stem


def identify(path: Path, db: Optional[SwitchDb] = None, header_key: Optional[bytes] = None) -> SwitchFile:
    """Tell one file (see the module's text). Never raises: a file that cannot be read comes back without a title."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    out = SwitchFile(path, size)
    try:
        box = switchfmt.read_container(path)
    except switchfmt.SwitchFormatError as exc:
        out.note = f"not read: {exc}"
        return out
    out.container = box.kind
    ids = box.nca_ids
    out.ncas = len(ids)
    if db is not None and db.available:
        out.known = sum(1 for n in ids if db.nca(n))
    tag_id, tag_version = switchfmt.tags_from_name(path.name)
    title: Optional[str] = None
    version: Optional[int] = None
    how = ""

    def from_keys() -> Optional[str]:
        if not header_key or not box.cnmt_nca:
            return None
        try:
            with open(path, "rb") as f:
                for _name, offset, _size in box.cnmt_nca:
                    got = nca_title_id(f, offset, header_key)
                    if got:
                        return got
        except OSError:
            pass
        return None

    if box.kind == "xci":
        got = from_keys()
        if got:
            title, how = got, "NCA header (prod.keys)"
    if title is None and box.cnmt:
        title, version, how = str(box.cnmt[0]["id"]), int(box.cnmt[0]["version"]), "cnmt.xml in the file"
    if title is None and db is not None and db.available:
        for nca in box.nca_ids:
            hit = db.nca(nca)
            if hit:
                title, version, how = hit[0], hit[1], "title database (NCA ids)"
                break
    if title is None and box.ticket_title_ids:
        title, how = box.ticket_title_ids[0], "ticket in the file"
    if title is None and tag_id:
        title, how = tag_id, "file name"
    if title is None:
        got = from_keys()
        if got:
            title, how = got, "NCA header (prod.keys)"
    if title is None:
        out.note = "no title ID found: no ticket or .cnmt.xml, no known NCA, no [title ID] in the name"
        return out
    if version is None:
        version = tag_version
    out.title_id, out.how, out.version = title.upper(), how, version
    if tag_id and tag_id != out.title_id:
        out.note = f"the file name says {tag_id}, the file is {out.title_id}"
    out.kind = switchfmt.kind_of_id(out.title_id)
    out.base_id = (db.application_of(out.title_id) if db is not None and out.kind != switchfmt.APPLICATION else None) \
        or switchfmt.base_id(out.title_id)
    known = db.title(out.base_id) if db is not None and db.available else None
    out.name = known["name"] if known else _clean_name(path.name)
    return out


def scan_folder(folder: Path, db: Optional[SwitchDb] = None, header_key: Optional[bytes] = None,
                progress: Optional[ProgressFn] = None, cancel: Any = None, verdicts: Any = None) -> SwitchScan:
    """Every Switch file under ``folder``, told and grouped by game."""
    folder = Path(folder)
    found: List[Path] = []
    for dirpath, dirnames, names in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(names):
            if name.lower().endswith(switchfmt.CONTAINER_EXTS) and not name.startswith("."):
                found.append(Path(dirpath) / name)
    scan = SwitchScan(folder)
    for i, path in enumerate(found):
        if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
            break
        if progress:
            progress(i, len(found), path.name)
        info = identify(path, db, header_key)
        if verdicts is not None:                       # (what an earlier "check the checksums" found, if the file is the same)
            got = verdicts.get(path)
            if got is not None:
                info.checksums, info.checksum_note = got.status, ", ".join(got.bad[:3])
        scan.files += 1
        scan.bytes += info.size
        if not info.title_id:
            scan.unidentified.append(info)
            continue
        game = scan.games.get(info.base_id)
        if game is None:
            game = scan.games[info.base_id] = SwitchGame(info.base_id, info.name)
        elif info.kind == switchfmt.APPLICATION and info.name:
            game.name = info.name
        game.files.append(info)
    if progress:
        progress(len(found), len(found), "")
    return scan
