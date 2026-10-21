"""Which versions of your Amiga games a WHDLoad install can be made from (the rule "Prefer versions WHDLoad can install").

A version is the set of disks of one release of a game (the DAT names of its disks). It is WHDLoad-ready when the game's WHDLoad
install, run here on those disks (``romorg.amigainstall``), installs the game: its disks are AmigaDOS and hold what the install copies.
That takes the install package from whdload.de, so it is found by a check you start (``POST /api/whdload/check``) and kept here,
``whdinstalls/ready.json``: ``{"sets": {"<disk names, one per line>": {"ok": bool, "archive": "...", "reason": "...", "at": time}}}``.
The library only reads it (:func:`is_ready`): a version never checked is no WHDLoad version.

What is known already ships with the app (``whdknown.json.gz``, made from TOSEC-named ADFs: an install run on every copy, and the
builds booted headless in PUAE): :func:`level` of a version - 3 boots, 2 installs the game's files, 1 installs disk images, 0 does not
install, -1 installs but does not boot. The user's own check counts first for whether it installs; whether it boots only the shipped
list knows. It also lists the installs whose ReadMe does not say they take the game's manual / code-wheel check out
(:func:`leaves_manual`): an original of a game that has one asks for the manual even installed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = ["path", "key_of", "is_ready", "level", "known", "archive", "release", "has_install", "leaves_manual", "record", "load",
           "clear_cache"]

BOOTS, FILES, IMAGES, NONE, NO_BOOT = 3, 2, 1, 0, -1

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


_KNOWN: Optional[dict[str, Any]] = None
_MANUAL_KEPT: Optional[frozenset] = None


def _shipped() -> dict[str, Any]:
    import gzip
    import importlib.resources
    try:
        raw = importlib.resources.files(__package__).joinpath("whdknown.json.gz").read_bytes()
        data = json.loads(gzip.decompress(raw))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def known() -> dict[str, Any]:
    """The shipped list: ``{disk names: [level, install archive]}`` (empty when it is missing)."""
    global _KNOWN, _MANUAL_KEPT
    if _KNOWN is None:
        data = _shipped()
        _KNOWN = data.get("sets") or {}
        _MANUAL_KEPT = frozenset(data.get("manual_kept") or ())
    return _KNOWN


def archive(names: Iterable[str]) -> Optional[str]:
    """The install the shipped list ran on these disks (None: none known)."""
    ship = known().get(key_of(names))
    return (ship[1] or None) if isinstance(ship, list) and len(ship) > 1 else None


_INDEX: Optional[tuple[int, dict[str, list], frozenset]] = None
_VERSION_RE = re.compile(r"\s+v[\w.\-]*\d[\w.\-]*$")


def _title(name: str) -> str:
    """A TOSEC name's title without its version ("Civilization v855e.01 (1994)(MicroProse)" -> "civilization")."""
    return _VERSION_RE.sub("", name.split(" (")[0]).casefold()


def _index() -> tuple[dict[str, list], frozenset]:
    """``(releases, titles)``: the shipped entry of each image-set release by its name (an SPS release is one zip, its images
    "<release> - <file>.ipf" - the library has one of them, the list all), and the titles (the name before " (") that have an entry
    at all: their game has an install (one that did not install is listed too, as 0)."""
    global _INDEX
    sets = known()
    if _INDEX is None or _INDEX[0] != id(sets):
        releases: dict[str, list] = {}
        titles = set()
        for key, v in sets.items():
            lines = key.split("\n")
            titles.add(_title(lines[0]))
            if all(n.lower().endswith(".ipf") and " - " in n for n in lines):
                releases[lines[0].rsplit(" - ", 1)[0]] = v
        _INDEX = (id(sets), releases, frozenset(titles))
    return _INDEX[1], _INDEX[2]


def release(name: str) -> Optional[list]:
    """The shipped ``[level, install archive]`` of the image-set release (SPS / IPF) named ``name``, None: not known."""
    return _index()[0].get(name)


def has_install(title: str) -> bool:
    """True: the shipped list knows a WHDLoad install of the game of this title (the TOSEC title: the name before " (")."""
    return _title(title) in _index()[1]


def leaves_manual(arc: Optional[str]) -> bool:
    """True: this install's ReadMe does not say it takes the manual / code-wheel check out (it keeps it, takes it out of some versions
    only, or says nothing of it)."""
    known()
    return bool(arc) and arc in (_MANUAL_KEPT or ())


def level(names: Iterable[str]) -> Optional[int]:
    """How well a WHDLoad install works from these disks (``BOOTS`` .. ``NO_BOOT``), None: nothing known."""
    key = key_of(names)
    ship = known().get(key)
    shipped = ship[0] if isinstance(ship, list) and ship else None
    if shipped in (BOOTS, NO_BOOT):
        return shipped                                     # (booted - or not - in PUAE: what a check here cannot tell)
    row = load().get(key)
    if row is not None:
        if not row.get("ok"):
            return NONE
        return shipped if shipped in (FILES, IMAGES) else FILES
    return shipped


def is_ready(names: Iterable[str]) -> Optional[bool]:
    """True: a WHDLoad install can be made from these disks; False: it cannot; None: never checked."""
    lv = level(names)
    return None if lv is None else lv > NONE


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
