"""What the catalogue modules (``openretro``, ``whdinstalls``, ``gametdb``, ``switchdb``) share: a list fetched from a website and kept
on disk as one file, how old it may get before it is fetched again, and how it is read.

* :func:`fetch`: one GET (``opener`` stands in for the network in tests), counted by the transfer meter.
* :func:`is_fresh` / :func:`check_age`: the age gate of ``check_update`` (a list younger than its ``MAX_AGE_DAYS`` is not asked about).
* :class:`KeptJson`: a JSON (or gzip-compressed JSON) file read once and again only when it changed, written all at once.
* :class:`ReadOnlyDb`: a SQLite file opened read-only; a missing or unreadable file is an empty database.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import meter, paths

__all__ = ["USER_AGENT", "fetch", "is_fresh", "check_age", "KeptJson", "ReadOnlyDb"]

USER_AGENT = "simple-rom-organiser"


def fetch(url: str, timeout: float = 60, opener: Optional[Callable[[Any], Any]] = None) -> bytes:
    """The body of ``url``."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    resp = opener(req) if opener else urllib.request.urlopen(req, timeout=timeout)
    try:
        data = resp.read()
    finally:
        resp.close()
    meter.add(len(data))
    return data


def is_fresh(info: Dict[str, Any], max_age_days: float, gate: bool = True, now: Optional[float] = None) -> bool:
    """The kept list (``info["fetched_at"]``) is younger than ``max_age_days``, and ``gate`` asks to take that as an answer."""
    return gate and (now if now is not None else time.time()) - float(info.get("fetched_at") or 0) < max_age_days * 86400


def check_age(info: Dict[str, Any], max_age_days: float, gate: bool = True, now: Optional[float] = None) -> Dict[str, Any]:
    """``check_update`` of a list that has no cheaper way to ask whether it changed: ``missing`` (``info["available"]`` false),
    ``up_to_date`` (fresh) or ``update_available``."""
    if not info["available"]:
        return {"status": "missing"}
    if is_fresh(info, max_age_days, gate, now):
        return {"status": "up_to_date", "skipped": True}
    return {"status": "update_available"}


class KeptJson:
    """A list kept as one JSON file (``gz``: gzip-compressed). :meth:`load` returns the parsed file, or None when it is missing,
    damaged or not what ``validate`` accepts; the answer is remembered until the file's size or time changes."""

    def __init__(self, validate: Callable[[Any], bool], gz: bool = False) -> None:
        self.validate, self.gz = validate, gz
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[Any, Optional[Dict[str, Any]]]] = {}

    def load(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            st = path.stat()
        except OSError:
            return None
        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(str(path))
            if hit is not None and hit[0] == sig:
                return hit[1]
        try:
            raw = path.read_bytes()
            data = json.loads(gzip.decompress(raw) if self.gz else raw)
            if not self.validate(data):
                data = None
        except (OSError, ValueError, EOFError, AttributeError):
            data = None
        with self._lock:
            self._cache[str(path)] = (sig, data)
        return data

    def write(self, path: Path, data: Dict[str, Any], prefix: str = ".kept-") -> None:
        """Replace the file with ``data`` all at once."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.gz:
            raw = gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), 6, mtime=0)
        else:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        paths.atomic_write(path, raw, prefix)


class ReadOnlyDb:
    """A SQLite file opened read-only. ``probe`` (an SQL statement that needs the tables to be there) is run once: a file that is
    missing, not a database or lacks them leaves ``conn`` None (``available`` False) - an empty database."""

    probe = "SELECT 1"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.conn: Optional[sqlite3.Connection] = None
        if self.path.is_file():
            try:
                self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
                self.conn.execute(self.probe)
            except sqlite3.Error:
                self.close()

    @property
    def available(self) -> bool:
        return self.conn is not None

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None
