"""Which versions of your Amiga games a WHDLoad install can be made from (the rule "Prefer versions WHDLoad can install").

A version is the set of disks of one release of a game (the DAT names of its disks). It is WHDLoad-ready when the game's WHDLoad
install, run here on those disks (``romorg.amigainstall``), installs the game: its disks are AmigaDOS and hold what the install copies.
That takes the install package from whdload.de, so it is found by a check you start (``POST /api/whdload/check``) and kept here,
``whdinstalls/ready.json``: ``{"sets": {"<disk names, one per line>": {"ok": bool, "archive": "...", "reason": "...", "at": time}}}``.
The library only reads it (:func:`is_ready`): a version never checked is no WHDLoad version.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = ["path", "key_of", "is_ready", "record", "load", "clear_cache"]

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[Any, dict[str, Any]]] = {}


def path() -> Path:
    from .paths import data_dir
    return data_dir() / "whdinstalls" / "ready.json"


def key_of(names: Iterable[str]) -> str:
    """The key of a version: its disks' DAT names, sorted, one per line."""
    return "\n".join(sorted(str(n) for n in names))


def load() -> dict[str, Any]:
    p = path()
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _LOCK:
        hit = _CACHE.get(str(p))
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8")).get("sets") or {}
    except (OSError, ValueError, AttributeError):
        data = {}
    with _LOCK:
        _CACHE[str(p)] = (sig, data)
    return data


def is_ready(names: Iterable[str]) -> Optional[bool]:
    """True: a WHDLoad install was made from these disks; False: it could not be; None: never checked."""
    row = load().get(key_of(names))
    return None if row is None else bool(row.get("ok"))


def record(rows: dict[str, dict[str, Any]]) -> None:
    """Add (or replace) the results of a check: ``{key_of(names): {"ok", "archive", "reason"}}``."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        sets = data.get("sets") if isinstance(data.get("sets"), dict) else {}
        now = time.time()
        for k, v in rows.items():
            sets[k] = {**v, "at": now}
        fd, tmp = tempfile.mkstemp(prefix=".ready-", dir=p.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"sets": sets}, fh, ensure_ascii=False)
        os.replace(tmp, p)
        _CACHE.pop(str(p), None)


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
