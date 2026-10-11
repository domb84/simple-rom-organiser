"""IGDB (https://www.igdb.com), an optional second source of ratings, with the user's own key.

IGDB is free to use with a Twitch developer application (a Client ID and a Client secret that the user makes and pastes into the
app: nothing is shipped with it). For each system the rated games are read in pages (500 at a time, four requests a second at most:
IGDB's limit) into a small index of the same shape as LaunchBox's (``ratings.write_index``), kept next to it in the ratings folder; the
rating of a title is then the better evidenced of the two (the one with more votes).

IGDB's ``total_rating`` (the users' and the critics' ratings together, 0 - 100) is the rating and ``total_rating_count`` the votes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__, paths, ratings
from .tosec import Cancelled

__all__ = ["IgdbError", "PLATFORM_IDS", "INDEX_NAME", "credentials", "installed", "needs_refresh", "build_index", "check_credentials", "forget"]

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_URL = "https://api.igdb.com/v4/"
PAGE = 500
PAUSE = 0.27                       # seconds between two requests (IGDB allows four a second)
TIMEOUT = 30
INDEX_NAME = "igdb.sqlite"
MANIFEST_NAME = "igdb.json"
ID_OFFSET = 1_000_000_000          # IGDB ids are kept apart from LaunchBox's in the shared index shape
CREDIT = "Ratings: IGDB.com (with the user's own Twitch API key)"
CREDIT_URL = "https://www.igdb.com"

# LaunchBox platform name (the key of ``ratings.LB_PLATFORMS``'s values) -> IGDB platform id
PLATFORM_IDS: Dict[str, int] = {
    "Commodore Amiga": 16, "Nintendo Entertainment System": 18, "Super Nintendo Entertainment System": 19, "Nintendo 64": 4,
    "Nintendo Game Boy": 33, "Nintendo Game Boy Color": 22, "Nintendo Game Boy Advance": 24, "Nintendo DS": 20,
    "Nintendo GameCube": 21, "Nintendo Wii": 5, "Nintendo Wii U": 41, "Nintendo Switch": 130,
    "Sega Genesis": 29, "Sega Master System": 64, "Sega Game Gear": 35, "Sega 32X": 30, "Sega Dreamcast": 23,
    "Sony Playstation": 7, "Sony Playstation 2": 8, "Atari Lynx": 61,
}

Opener = Callable[[urllib.request.Request], Any]
ProgressFn = Callable[[int, int, str], None]


class IgdbError(Exception):
    """Problem asking IGDB (a refused key, no network, an answer that is not what was expected)."""


# --------------------------------------------------------------------------- the key

def credentials(cfg: Any) -> Optional[Tuple[str, str]]:
    """``(client id, client secret)`` of the user's key in ``config.json`` (``cfg["igdb"]``), or None."""
    entry = cfg.get("igdb") if isinstance(cfg, dict) else None
    if not isinstance(entry, dict):
        return None
    cid, secret = str(entry.get("client_id") or "").strip(), str(entry.get("client_secret") or "").strip()
    return (cid, secret) if cid and secret else None


def _fingerprint(client_id: str) -> str:
    return hashlib.sha1(client_id.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- talking to IGDB

def _open(req: urllib.request.Request, opener: Optional[Opener]) -> Any:
    return (opener or (lambda r: urllib.request.urlopen(r, timeout=TIMEOUT)))(req)


def _read_json(resp: Any) -> Any:
    try:
        return json.loads(resp.read().decode("utf-8"))
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def get_token(client_id: str, client_secret: str, opener: Optional[Opener] = None) -> str:
    """An access token for the key (Twitch's client credentials flow)."""
    url = TOKEN_URL + "?" + urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret,
                                                     "grant_type": "client_credentials"})
    req = urllib.request.Request(url, data=b"", method="POST", headers={"User-Agent": f"simple-rom-organiser/{__version__}"})
    try:
        doc = _read_json(_open(req, opener))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            raise IgdbError("Twitch does not accept that Client ID and secret") from exc
        raise IgdbError(f"Twitch answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise IgdbError(f"could not reach Twitch ({getattr(exc, 'reason', exc)})") from exc
    token = doc.get("access_token") if isinstance(doc, dict) else None
    if not token:
        raise IgdbError("Twitch gave no access token")
    return str(token)


def query(endpoint: str, body: str, client_id: str, token: str, opener: Optional[Opener] = None,
          sleep: Callable[[float], None] = time.sleep) -> List[dict]:
    """One Apicalypse query to ``endpoint`` (``games``, ``platforms``); a rate-limit answer waits and tries again."""
    req = urllib.request.Request(API_URL + endpoint, data=body.encode("utf-8"), method="POST",
                                 headers={"Client-ID": client_id, "Authorization": f"Bearer {token}", "Accept": "application/json",
                                          "User-Agent": f"simple-rom-organiser/{__version__}"})
    for attempt in range(4):
        try:
            doc = _read_json(_open(req, opener))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                sleep(1.0 + attempt)
                continue
            if exc.code in (401, 403):
                raise IgdbError("IGDB refused the key (is the Twitch application still there?)") from exc
            raise IgdbError(f"IGDB answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise IgdbError(f"could not reach IGDB ({getattr(exc, 'reason', exc)})") from exc
        if not isinstance(doc, list):
            raise IgdbError("IGDB's answer is not a list")
        return doc
    raise IgdbError("IGDB keeps answering 'too many requests'")


def fetch_platform(platform_id: int, client_id: str, token: str, opener: Optional[Opener] = None,
                   cancel: Any = None, sleep: Callable[[float], None] = time.sleep):
    """The rated games of one IGDB platform, a page at a time (by id: no offset limit to hit)."""
    last = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        body = (f"fields name,alternative_names.name,total_rating,total_rating_count,first_release_date; "
                f"where platforms = ({platform_id}) & total_rating_count > 0 & id > {last}; sort id asc; limit {PAGE};")
        page = query("games", body, client_id, token, opener, sleep)
        for game in page:
            yield game
        if len(page) < PAGE:
            return
        last = max(int(g.get("id") or 0) for g in page)
        sleep(PAUSE)


# --------------------------------------------------------------------------- the index

def _dir(directory: Any = None) -> Path:
    return Path(directory) if directory is not None else paths.ratings_dir()


def index_path(directory: Any = None) -> Path:
    return _dir(directory) / INDEX_NAME


def _manifest_path(directory: Any = None) -> Path:
    return _dir(directory) / MANIFEST_NAME


def installed(directory: Any = None) -> Optional[Dict[str, Any]]:
    """The manifest of the installed IGDB index (None when there is none)."""
    try:
        doc = json.loads(_manifest_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and index_path(directory).is_file() else None


def needs_refresh(man: Optional[Dict[str, Any]], creds: Optional[Tuple[str, str]], now: Optional[float] = None) -> bool:
    """True when the index is missing, was made with another key, lacks a system, or is older than a week."""
    if creds is None:
        return False
    if man is None or man.get("client") != _fingerprint(creds[0]):
        return True
    if sorted(set(PLATFORM_IDS) - set(man.get("built_for") or [])):
        return True
    built = ratings._iso_to_ts(str(man.get("built_at") or ""))
    return not built or ((now if now is not None else time.time()) - built) >= ratings.REFRESH_DAYS * 86400


def forget(directory: Any = None) -> None:
    """Remove the index (the key was taken away)."""
    for p in (index_path(directory), _manifest_path(directory)):
        try:
            p.unlink()
        except OSError:
            pass


def _game_row(g: dict, plat: str) -> Optional[dict]:
    try:
        votes = int(g.get("total_rating_count") or 0)
        rating = float(g.get("total_rating") or 0)
        dbid = int(g["id"])
    except (TypeError, ValueError, KeyError):
        return None
    name = str(g.get("name") or "").strip()
    if not name or votes <= 0 or rating <= 0:
        return None
    year = 0
    if g.get("first_release_date"):
        try:
            year = time.gmtime(int(g["first_release_date"])).tm_year
        except (TypeError, ValueError, OverflowError, OSError):
            year = 0
    return {"name": name, "plat": plat, "rating": rating / 20.0, "votes": votes, "year": year, "dbid": ID_OFFSET + dbid}


def build_index(creds: Tuple[str, str], directory: Any = None, progress: Optional[ProgressFn] = None, cancel: Any = None,
                opener: Optional[Opener] = None, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Read the rated games of every system from IGDB into ``igdb.sqlite`` (atomic replace); returns the manifest."""
    cid, secret = creds
    folder = _dir(directory)
    folder.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(0, 0, "Asking Twitch for an IGDB access token")
    token = get_token(cid, secret, opener)
    games: Dict[int, Dict[str, Any]] = {}
    alts: Dict[int, List[str]] = {}
    stats: Dict[str, int] = {}
    wanted = sorted(PLATFORM_IDS.items())
    for n, (plat, pid) in enumerate(wanted, 1):
        if progress:
            progress(n - 1, len(wanted), f"IGDB: {plat} ({n} of {len(wanted)})")
        count = 0
        for g in fetch_platform(pid, cid, token, opener, cancel, sleep):
            row = _game_row(g, plat)
            if row is None:
                continue
            games[row["dbid"]] = row
            names = [a.get("name") for a in (g.get("alternative_names") or []) if isinstance(a, dict) and a.get("name")]
            if names:
                alts[row["dbid"]] = [str(x) for x in names]
            count += 1
        stats[plat] = count
    rows = ratings.build_rows(games, alts)
    manifest = {"built_at": ratings._now(), "client": _fingerprint(cid), "games": len(games), "entries": len(rows),
                "platforms": stats, "built_for": sorted(PLATFORM_IDS), "schema": ratings.SCHEMA}
    part = folder / (INDEX_NAME + ".part")
    try:
        ratings.write_index(rows, part, manifest)
        os.replace(part, index_path(directory))
        _manifest_path(directory).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    finally:
        try:
            part.unlink()
        except OSError:
            pass
    if progress:
        progress(len(wanted), len(wanted), f"IGDB ratings ready ({len(games)} titles)")
    return manifest


def check_credentials(client_id: str, client_secret: str, opener: Optional[Opener] = None) -> str:
    """Try the key: a token and one small query. Returns a sentence that says it works; raises :class:`IgdbError` if not."""
    token = get_token(client_id.strip(), client_secret.strip(), opener)
    query("games", "fields id; limit 1;", client_id.strip(), token, opener)
    return "The key works: Twitch gave a token and IGDB answered."
