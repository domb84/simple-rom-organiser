"""The OpenRetro game database (the FS-UAE Launcher's, "an open game configuration database for Amiga games with liberal usage
terms"): every Amiga game it knows with the releases ("variants") of it, a release by the SHA-1 of each of its disks.

What it adds to the DATs: which releases are one game (by checksum, across the titles TOSEC files them under: "Der Clou!" and
"Clue!", "Detroit" and "Rüsselsheim"), what a release needs (model, chipset, Kickstart, PAL / NTSC), its protection (manual, code
wheel ...), the game's year, publisher, developer and its pages on the Hall of Light, Lemon Amiga and MobyGames.

It is fetched as the launcher does - the public sync of ``openretro.org/api/sync/amiga/games``, 500 records a page, each a sync id,
a 16-byte id, a length and zlib-compressed JSON (an empty one: deleted) - all of it the first time, after that only what changed
since the last sync id. Kept: the fields used here, ``openretro/amiga.json.gz`` in the data folder (about 1.5 MB).
"""

from __future__ import annotations

import gzip
import json
import os
import re
import struct
import tempfile
import threading
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Callable, Optional

from . import meter

__all__ = ["SYNC_URL", "db_path", "load", "db_info", "check_update", "download_and_build", "parse_page", "Database", "database"]

SYNC_URL = "https://openretro.org/api/sync/amiga/games?v=3&sync={sync}"
USER_AGENT = "simple-rom-organiser"
MAX_AGE_DAYS = 7
PAGE_PAUSE = 0.5                                   # (between pages: a small site)

ProgressFn = Callable[[int, int, str], None]

_GAME_FIELDS = {"game_name": "name", "year": "year", "publisher": "publisher", "developer": "developer", "players": "players",
                "hol_url": "hol", "lemon_url": "lemon", "mobygames_url": "moby", "languages": "languages"}
_VARIANT_FIELDS = {"x_name": "name", "variant_name": "variant", "languages": "languages", "protection": "protection",
                   "amiga_model": "model", "chipset": "chipset", "kickstart": "kickstart", "video_standard": "video",
                   "whdload_args": "whdload_args", "requirements": "requires"}


class OpenRetroError(Exception):
    pass


def db_path() -> Path:
    from .paths import data_dir
    return data_dir() / "openretro" / "amiga.json.gz"


def _uuid(u: str) -> str:
    return (u or "").replace("-", "").lower()


def parse_page(raw: bytes) -> list[tuple[int, str, Optional[dict[str, Any]]]]:
    """The records of a sync page: ``(sync id, id, data or None - deleted)``."""
    out, o = [], 0
    while o + 24 <= len(raw):
        sid = struct.unpack_from(">I", raw, o)[0]
        uid = raw[o + 4:o + 20].hex()
        size = struct.unpack_from(">I", raw, o + 20)[0]
        o += 24
        if o + size > len(raw):
            raise OpenRetroError("a sync page that ends in the middle of a record")
        body = raw[o:o + size]
        o += size
        try:
            data = json.loads(zlib.decompress(body)) if size else None
        except (zlib.error, ValueError) as exc:
            raise OpenRetroError(f"a record that cannot be read ({exc})") from None
        out.append((sid, uid, data if isinstance(data, dict) else None))
    return out


def _compact(data: dict[str, Any]) -> Optional[tuple[str, dict[str, Any]]]:
    """``("game" | "variant", the fields kept)`` of a record, or None (another kind)."""
    if data.get("_type") == "1" or ("game_name" in data and "parent_uuid" not in data):
        return "game", {k2: str(data[k]) for k, k2 in _GAME_FIELDS.items() if data.get(k)}
    if "parent_uuid" in data:
        v = {k2: str(data[k]) for k, k2 in _VARIANT_FIELDS.items() if data.get(k)}
        v["game"] = _uuid(data["parent_uuid"])
        fl = data.get("file_list")
        try:
            files = json.loads(fl) if isinstance(fl, str) else (fl or [])
        except ValueError:
            files = []
        v["sha1"] = [f["sha1"].lower() for f in files if isinstance(f, dict) and isinstance(f.get("sha1"), str)
                     and _DISK_RE.search(str(f.get("name") or ""))]        # (the disks: not every file of an installed game)
        return "variant", v
    return None


_DISK_RE = re.compile(r"\.(?:adf|adz|ipf|dms|fdi|scp|raw|iso|cue|bin|img|ccd|chd)$", re.IGNORECASE)
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[Any, Any]] = {}


def load(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """``{fetched, fetched_at, sync, games, variants}`` of the kept copy, or None (not fetched yet)."""
    p = Path(path) if path else db_path()
    try:
        st = p.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _CACHE.get(str(p))
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
        data = json.loads(gzip.decompress(p.read_bytes()))
        if not isinstance(data.get("variants"), dict) or not isinstance(data.get("games"), dict):
            data = None
    except (OSError, ValueError, EOFError, AttributeError):
        data = None
    with _LOCK:
        _CACHE[str(p)] = (sig, data)
    return data


def db_info(path: Optional[Path] = None) -> dict[str, Any]:
    data = load(path)
    if data is None:
        return {"available": False, "games": 0, "variants": 0, "fetched": "", "fetched_at": 0.0}
    return {"available": True, "games": len(data["games"]), "titles": len(data["games"]), "variants": len(data["variants"]),
            "fetched": data.get("fetched", ""),
            "fetched_at": float(data.get("fetched_at") or 0)}


def check_update(timeout: float = 10, gate: bool = True, path: Optional[Path] = None, now: Optional[float] = None) -> dict[str, Any]:
    """``missing`` (fetch all), ``up_to_date`` (younger than :data:`MAX_AGE_DAYS`), else ``update_available`` (fetch what changed)."""
    info = db_info(path)
    if not info["available"]:
        return {"status": "missing"}
    if gate and (now if now is not None else time.time()) - info["fetched_at"] < MAX_AGE_DAYS * 86400:
        return {"status": "up_to_date", "skipped": True}
    return {"status": "update_available"}


def _fetch(url: str, timeout: float, opener: Optional[Callable[[Any], Any]]) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    resp = opener(req) if opener else urllib.request.urlopen(req, timeout=timeout)
    try:
        data = resp.read()
    finally:
        resp.close()
    meter.add(len(data))
    return data


def download_and_build(progress: Optional[ProgressFn] = None, cancel: Any = None, out: Optional[Path] = None,
                       opener: Optional[Callable[[Any], Any]] = None, timeout: float = 60, pause: float = PAGE_PAUSE) -> dict[str, int]:
    """Bring the kept copy up to date: the pages after its last sync id (all of them the first time); atomic replace."""
    out = Path(out) if out else db_path()
    old = load(out) or {}
    games: dict[str, Any] = dict(old.get("games") or {})
    variants: dict[str, Any] = dict(old.get("variants") or {})
    sync = int(old.get("sync") or 0)
    changed = pages = 0
    while True:
        if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
            raise InterruptedError("cancelled")
        if progress:
            progress(pages, 0, f"OpenRetro: {len(games)} games, {len(variants)} releases")
        recs = parse_page(_fetch(SYNC_URL.format(sync=sync), timeout, opener))
        if not recs:
            break
        for sid, uid, data in recs:
            sync = max(sync, sid)
            games.pop(uid, None)
            variants.pop(uid, None)
            got = _compact(data) if data else None
            if got:
                (games if got[0] == "game" else variants)[uid] = got[1]
            changed += 1
        pages += 1
        if pause:
            time.sleep(pause)
    if len(variants) < 1000:
        raise OpenRetroError("the OpenRetro database could not be read (too few releases: the site has changed?)")
    now = time.time()
    data = {"source": SYNC_URL.split("?")[0], "fetched": time.strftime("%Y-%m-%d", time.gmtime(now)), "fetched_at": now,
            "sync": sync, "games": games, "variants": variants}
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".openretro-", dir=out.parent)
    with os.fdopen(fd, "wb") as fh:
        fh.write(gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), 6, mtime=0))
    os.replace(tmp, out)
    if progress:
        progress(1, 1, f"OpenRetro: {len(games)} games, {len(variants)} releases")
    return {"games": len(games), "variants": len(variants), "changed": changed}


# --------------------------------------------------------------------------- what is in it

_NAME_RE = re.compile(r"\s*\((?:Amiga|CD32|CDTV)[,)].*$")
_MARK_RE = re.compile(r"(?:\s*\[[^\]]*\])+$")                       # ("Pinball Fantasies [AGA]", "... [CD32]")


class Database:
    """The kept copy, indexed: the release of a disk (by its SHA-1), the game of a release, the titles of a game."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.games: dict[str, dict[str, Any]] = data.get("games") or {}
        self.variants: dict[str, dict[str, Any]] = data.get("variants") or {}
        self.by_sha1: dict[str, str] = {}
        for uid, v in self.variants.items():
            for h in v.get("sha1") or ():
                self.by_sha1.setdefault(h, uid)

    def release(self, sha1s: list[str]) -> Optional[dict[str, Any]]:
        """The release these disks are (the one most of them belong to), with its game's fields under ``"game"``."""
        counts: dict[str, int] = {}
        for h in sha1s:
            u = self.by_sha1.get((h or "").lower())
            if u:
                counts[u] = counts.get(u, 0) + 1
        if not counts:
            return None
        uid = max(counts, key=lambda u: (counts[u], u))
        v = dict(self.variants[uid])
        v["id"] = uid
        g = self.games.get(v.get("game", ""))
        if g:
            v["game_fields"] = g
        return v

    def titles(self) -> list[tuple[str, str]]:
        """``(title, game id)``: each game's name and the titles of the releases it was made from ("Anstoss (Amiga, TOSEC, ADF,
        v1.04, ...)": "Anstoss"); a game's name in TOSEC's way ("Impossible Mission 2025: The Special Edition" also as
        "Impossible Mission 2025 - The Special Edition")."""
        out = []
        for uid, g in self.games.items():
            n = _MARK_RE.sub("", g.get("name") or "").strip()
            if n:
                out.append((n, uid))
                if ": " in n:
                    out.append((n.replace(": ", " - ", 1), uid))
        for v in self.variants.values():
            n = _NAME_RE.sub("", v.get("name") or "").strip()
            if n and v.get("game") in self.games:
                out.append((n, v["game"]))
        return out


_DB: dict[str, tuple[Any, Optional[Database]]] = {}


def database(path: Optional[Path] = None) -> Optional[Database]:
    """The kept copy indexed (cached while the file is the same), or None."""
    p = Path(path) if path else db_path()
    data = load(p)
    key = str(p)
    with _LOCK:
        hit = _DB.get(key)
        if hit is not None and hit[0] is data:
            return hit[1]
    db = Database(data) if data else None
    with _LOCK:
        _DB[key] = (data, db)
    return db
