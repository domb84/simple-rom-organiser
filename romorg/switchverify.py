"""Checksums of a Switch file: does every NCA in it match its own name?

A Switch content archive names each NCA by its **content ID**, the first 16 bytes of the SHA-256 of the NCA. So a file carries its
own checksums in the clear, and checking a dump needs no key and no database: hash every ``.nca`` in the container and compare with
its name. A wrong result means the file is damaged or was altered. (Compressed files, ``.nsz`` / ``.xcz``, keep their NCAs as
``.ncz`` and are not checked without decompressing them.)

The check reads the whole file, so it is only done on request and the result is remembered (path, size, modification time).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from . import meter, switchfmt

__all__ = ["Verdict", "verify_file", "VerifyCache", "cache_path"]

ProgressFn = Callable[[int, int, str], None]
CHUNK = 1 << 20


@dataclass
class Verdict:
    status: str                                   # ok | damaged | not checked
    checked: int = 0
    bad: List[str] = field(default_factory=list)
    why: str = ""

    def public(self) -> dict:
        return {"status": self.status, "checked": self.checked, "bad": list(self.bad), "why": self.why}


def cache_path() -> Path:
    from .paths import data_dir
    return data_dir() / "switch" / "verified.sqlite"


def _is_cancelled(cancel: Any) -> bool:
    return cancel is not None and (cancel() if callable(cancel) else cancel.is_set())


def verify_file(path: Path, box: Optional[switchfmt.Container] = None, progress: Optional[ProgressFn] = None,
                cancel: Any = None) -> Verdict:
    """Hash every NCA of the container and compare with its name. Raises ``InterruptedError`` when cancelled."""
    path = Path(path)
    try:
        box = box or switchfmt.read_container(path)
    except switchfmt.SwitchFormatError as exc:
        return Verdict("not checked", why=str(exc))
    ncas = [(n, o, s) for n, o, s in box.entries if n.lower().endswith(".nca") and len(n.split(".")[0]) == 32]
    if not ncas:
        packed = any(n.lower().endswith(".ncz") for n, _o, _s in box.entries)
        return Verdict("not checked", why="compressed: its NCAs are packed (.ncz)" if packed else "no NCA in it")
    total = sum(s for _n, _o, s in ncas)
    done = 0
    bad: List[str] = []
    with open(path, "rb") as f:
        for name, offset, size in ncas:
            h = hashlib.sha256()
            f.seek(offset)
            left = size
            while left > 0:
                if _is_cancelled(cancel):
                    raise InterruptedError("cancelled")
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                h.update(chunk)
                left -= len(chunk)
                done += len(chunk)
                meter.add(len(chunk))
                if progress:
                    progress(done, total, path.name)
            if left > 0 or h.hexdigest()[:32] != name.split(".")[0].lower():
                bad.append(name)
    return Verdict("damaged" if bad else "ok", len(ncas), bad)


class VerifyCache:
    """Remembers a verdict per file (path, size, modification time) in a small SQLite file."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.conn: Optional[sqlite3.Connection] = None
        try:
            target = Path(path) if path else cache_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(target), check_same_thread=False)
            self.conn.execute("CREATE TABLE IF NOT EXISTS verdicts (path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,"
                              " verdict TEXT NOT NULL, PRIMARY KEY (path, size, mtime_ns))")
            self.conn.commit()
        except (sqlite3.Error, OSError):
            self.conn = None

    @staticmethod
    def _key(path: Path) -> Optional[Tuple[str, int, int]]:
        try:
            st = Path(path).stat()
        except OSError:
            return None
        return str(path), st.st_size, st.st_mtime_ns

    def get(self, path: Path) -> Optional[Verdict]:
        key = self._key(path)
        if self.conn is None or key is None:
            return None
        try:
            row = self.conn.execute("SELECT verdict FROM verdicts WHERE path=? AND size=? AND mtime_ns=?", key).fetchone()
            if row is None:
                return None
            d = json.loads(row[0])
            return Verdict(d["status"], int(d.get("checked", 0)), list(d.get("bad", [])), d.get("why", ""))
        except (sqlite3.Error, ValueError, KeyError):
            return None

    def put(self, path: Path, verdict: Verdict) -> None:
        key = self._key(path)
        if self.conn is None or key is None or verdict.status == "not checked":
            return
        try:
            self.conn.execute("DELETE FROM verdicts WHERE path=?", (key[0],))
            self.conn.execute("INSERT INTO verdicts VALUES (?,?,?,?)", (*key, json.dumps(verdict.public())))
            self.conn.commit()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None
