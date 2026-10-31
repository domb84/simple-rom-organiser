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
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from . import paths

__all__ = ["path", "key_of", "is_ready", "level", "known", "archive", "release", "has_install", "leaves_manual", "record", "load",
           "clear_cache"]

BOOTS, FILES, IMAGES, NONE, NO_BOOT = 3, 2, 1, 0, -1

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[Any, dict[str, Any]]] = {}


def path() -> Path:
    return paths.data_dir() / "whdinstalls" / "ready.json"


def key_of(names: Iterable[str]) -> str:
    """The key of a version: its disks' DAT names, sorted, one per line."""
    return "\n".join(sorted(str(n) for n in names))


_RECENT = 2.0                  # seconds a load is reused without even a stat (a library plan asks tens of thousands of times)
_LAST: list = [0.0, "", None]  # [when, path, data] of the last load


def load() -> dict[str, Any]:
    now = time.monotonic()
    where = os.environ.get("ROMORG_DATA_DIR", "")             # (what the path depends on: finding it costs more than the answer)
    with _LOCK:
        if _LAST[2] is not None and _LAST[1] == where and now - _LAST[0] < _RECENT:
            return _LAST[2]
    p = path()
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _LOCK:
            _LAST[:] = [now, where, {}]                        # (no check made yet: nothing, for a moment too)
        return {}
    with _LOCK:
        hit = _CACHE.get(str(p))
        if hit is not None and hit[0] == sig:
            _LAST[:] = [now, where, hit[1]]
            return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8")).get("sets") or {}
    except (OSError, ValueError, AttributeError):
        data = {}
    with _LOCK:
        _CACHE[str(p)] = (sig, data)
        _LAST[:] = [time.monotonic(), where, data]
    return data


_KNOWN: Optional[dict[str, Any]] = None
_MANUAL_KEPT: Optional[frozenset] = None
_NEEDS: Optional[dict[str, int]] = None                   # install archive -> the WHDLoad version its slave needs (newer than PUAE's)
PUAE_WHDLOAD = 19                                         # the WHDLoad PUAE 5.3 comes with (19.1)
_SHA: Optional[dict[str, str]] = None                     # the first 16 hex digits of a disk's SHA-1 -> its name in the list


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
    global _KNOWN, _MANUAL_KEPT, _SHA, _NEEDS
    if _KNOWN is None:
        data = _shipped()
        _KNOWN = data.get("sets") or {}
        _MANUAL_KEPT = frozenset(data.get("manual_kept") or ())
        _NEEDS = {str(a): int(v) for a, v in (data.get("needs_whdload") or {}).items() if str(v).isdigit()}
        _SHA = {str(h).lower(): n for n, h in (data.get("sha1") or {}).items()}
    return _KNOWN


_NAMES: Optional[tuple[int, frozenset]] = None


def _names() -> frozenset:
    global _NAMES
    sets = known()
    if _NAMES is None or _NAMES[0] != id(sets):
        _NAMES = (id(sets), frozenset(n for k in sets for n in k.split("\n")))
    return _NAMES[1]


def shipped_name(name: str, sha1: str = "") -> str:
    """The name the shipped list knows a disk by: its own, or - when a TOSEC update renamed it ("Ingrid's Back! (1989)(Rainbird)" is
    "(Level 9)" now) - the one of the same checksum (``sha1``: the DAT's)."""
    if name in _names() or not sha1:
        return name
    known()
    return (_SHA or {}).get(sha1[:16].lower(), name)


def names_of(roms: Iterable[Any]) -> list[str]:
    """:func:`shipped_name` of each DAT rom (``name``, ``sha1``): what :func:`level` and :func:`archive` take."""
    return [shipped_name(r.name, getattr(r, "sha1", "") or "") for r in roms]


def release_of(rom: Any) -> Optional[list]:
    """:func:`release` of the image-set release a DAT rom (an image "<release> - <file>.ipf") is of, by its name or its checksum."""
    hit = release(getattr(rom, "game", "") or "")
    if hit is not None:
        return hit
    n = shipped_name(rom.name, getattr(rom, "sha1", "") or "")
    return release(n.rsplit(" - ", 1)[0]) if n != rom.name and " - " in n else None


def sets_with(name: str) -> list[str]:
    """The keys of the shipped sets that have the disk ``name``."""
    global _WITH
    sets = known()
    if _WITH is None or _WITH[0] != id(sets):
        idx: dict[str, list[str]] = {}
        for k in sets:
            for n in k.split("\n"):
                idx.setdefault(n, []).append(k)
        _WITH = (id(sets), idx)
    return _WITH[1].get(name, [])


_WITH: Optional[tuple[int, dict[str, list[str]]]] = None


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


# Games the PUAE core shows only black with its default CPU option (``puae_cpu_compatibility = "memory"``) and runs with another: title -> the value
# to set. Anything but the default is tagged in the Files view. Found by booting the build's installs: Star Trek - 25th Anniversary (WHDLoad core dumps
# show the game waiting for a signal that never comes; "compatible" runs it, "exact" and a 68030 do not); The Chaos Engine (ECS and AGA floppies),
# Cyber World and Skidoo run with "exact" (of 49 black-screen games tried with it, only these three did). The app does not change the user's core options.
CPU_MODE: dict[str, str] = {"star trek 25th anniversary": "compatible", "chaos engine": "exact", "cyber world": "exact", "skidoo": "exact"}


def cpu_mode(title: str) -> str:
    """The ``puae_cpu_compatibility`` value a game needs (``"compatible"``), "" when the default runs it (the TOSEC title: the name before " (")."""
    from . import alttitles
    return CPU_MODE.get(alttitles.node(title or ""), "")


def release(name: str) -> Optional[list]:
    """The shipped ``[level, install archive]`` of the image-set release (SPS / IPF) named ``name``, None: not known."""
    return _index()[0].get(name)


def has_install(title: str) -> bool:
    """True: the shipped list knows a WHDLoad install of the game of this title (the TOSEC title: the name before " (")."""
    return _title(title) in _index()[1]


def needs_whdload(arc: Optional[str]) -> int:
    """The WHDLoad version this install's slave needs when PUAE's own (:data:`PUAE_WHDLOAD`) is too old for it, else 0. Its sets are
    known to boot with that WHDLoad in PUAE's ``WHDLoad/C`` drawer: Emerald Mine, Zeewolf, Oilmania ... need WHDLoad 20."""
    known()
    return (_NEEDS or {}).get(arc or "", 0)


def leaves_manual(arc: Optional[str]) -> bool:
    """True: this install's ReadMe does not say it takes the manual / code-wheel check out (it keeps it, takes it out of some versions
    only, or says nothing of it)."""
    known()
    return bool(arc) and arc in (_MANUAL_KEPT or ())


def sha_key(roms: Iterable[Any]) -> str:
    """The disks of a version by checksum (the first 16 hex digits of each DAT rom's SHA-1, sorted, one per line); "" when one has none."""
    shas = [str(getattr(r, "sha1", "") or "")[:16].lower() for r in roms]
    return "\n".join(sorted(shas)) if shas and all(shas) else ""


_BY_SHA: Optional[tuple[int, dict[str, str]]] = None


def checked(names: Iterable[str], sha: str = "") -> Optional[dict[str, Any]]:
    """The user's own check of a version (``record``): by its disks' names, else - a TOSEC update renamed them - by their checksums."""
    global _BY_SHA
    rows = load()
    row = rows.get(key_of(names))
    if row is not None or not sha:
        return row
    if _BY_SHA is None or _BY_SHA[0] != id(rows):
        _BY_SHA = (id(rows), {v["sha1"]: k for k, v in rows.items() if isinstance(v, dict) and v.get("sha1")})
    k = _BY_SHA[1].get(sha)
    return rows.get(k) if k is not None else None


def level_of(roms: Iterable[Any]) -> Optional[int]:
    """:func:`level` of a version given as its DAT roms (``name``, ``sha1``): a renamed disk found by its checksum, in the shipped
    list and in the user's own check."""
    roms = list(roms)
    return level(names_of(roms), sha_key(roms))


def level(names: Iterable[str], sha: str = "") -> Optional[int]:
    """How well a WHDLoad install works from these disks (``BOOTS`` .. ``NO_BOOT``), None: nothing known. ``sha``: their
    :func:`sha_key`, to find the user's own check when the names changed."""
    names = list(names)
    key = key_of(names)
    ship = known().get(key)
    shipped = ship[0] if isinstance(ship, list) and ship else None
    if shipped in (BOOTS, NO_BOOT):
        return shipped                                     # (booted - or not - in PUAE: what a check here cannot tell)
    row = checked(names, sha)
    if row is not None:
        if not row.get("ok"):
            return NONE
        return shipped if shipped in (FILES, IMAGES) else FILES
    return shipped


def is_ready(names: Iterable[str], sha: str = "") -> Optional[bool]:
    """True: a WHDLoad install can be made from these disks; False: it cannot; None: never checked."""
    lv = level(names, sha)
    return None if lv is None else lv > NONE


def record(rows: dict[str, dict[str, Any]]) -> None:
    """Add (or replace) the results of a check: ``{key_of(names): {"ok", "archive", "reason", "sha1": sha_key(roms)}}``."""
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
        paths.atomic_write(p, json.dumps({"sets": sets}, ensure_ascii=False), ".ready-")
        _CACHE.pop(str(p), None)
        _LAST[:] = [0.0, "", None]


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        _LAST[:] = [0.0, "", None]
