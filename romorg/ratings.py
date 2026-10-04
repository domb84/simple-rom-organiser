"""Game ratings from the LaunchBox Games Database (Amendment 18): download, compact index, title matching.

Source: ``https://gamesdb.launchbox-app.com/Metadata.zip`` (about 108 MB, rebuilt daily; ``Metadata.xml`` inside is
about 512 MB). The zip is STREAM-parsed (``zipfile.open`` + ``iterparse`` + ``clear()``: bounded memory, no big temp
file) into a compact local index, ``ratings/ratings.sqlite`` (a few MB; only the platforms this app knows), and the zip
is discarded. Nothing here is needed while no rating filter is set.

Index (sqlite, ``entry`` table): one row per ``(plat, key)`` = a LaunchBox platform and a NORMALISED title (see
:func:`norm_title`) with ``rating`` (stars 0-5, float), ``votes``, ``dbid`` (LaunchBox DatabaseID), ``name`` (the LaunchBox
title), ``year`` and ``alt`` (1 = the key came from a ``GameAlternateName``). Several LaunchBox games with the same
normalised title: a game's own name beats an alternate name, then the one with the MOST votes, then the lowest DatabaseID.
Games without a rating (absent or 0) or without votes are not stored.

Matching (:meth:`Store.detail`), deterministic and conservative - a missing rating is preferred to a wrong one:
``exact`` (normalised title) -> ``alt`` (an alternate name) -> ``roman`` (II..IX read as digits, only when the result is
unique) -> ``fuzzy`` (difflib ratio >= :data:`FUZZY_MIN`, a UNIQUE best candidate, same digits, same platform).
"""

from __future__ import annotations

import contextlib
import difflib
import email.utils
import functools
import http.client
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__, paths
from .nointro import CHUNK, TIMEOUT, _now, _unlink
from .tosec import Cancelled

SOURCE_URL = "https://gamesdb.launchbox-app.com/Metadata.zip"
USER_AGENT = f"simple-rom-organiser/{__version__}"
CREDIT = "Ratings: LaunchBox Games Database community ratings"
CREDIT_URL = "https://gamesdb.launchbox-app.com/"
INDEX_NAME = "ratings.sqlite"
MANIFEST = "manifest.json"
REFRESH_DAYS = 7                  # keep fresh at startup only when the installed index is older than this
SCHEMA = 1
FUZZY_MIN = 0.92                  # difflib ratio of the best candidate
FUZZY_GAP = 0.04                  # the runner-up must be at least this much worse (uniqueness)
DEFAULT_MIN_VOTES = 5

# our platform name -> LaunchBox ``<Platform>`` string (no entry = no ratings for that system)
LB_PLATFORMS: dict[str, str] = {
    "Commodore Amiga": "Commodore Amiga",
    "Commodore Amiga - WHDLoad": "Commodore Amiga",
    "Nintendo Game Boy Advance": "Nintendo Game Boy Advance",
    "Nintendo 64": "Nintendo 64",
    "Nintendo Entertainment System": "Nintendo Entertainment System",
    "Super Nintendo Entertainment System": "Super Nintendo Entertainment System",
    "Sega Dreamcast": "Sega Dreamcast",
    "Sony PlayStation": "Sony Playstation",
    "Sony PlayStation 2": "Sony Playstation 2",
}
# DATs of a system whose GAMES are rated (the others - Workbench, Kickstart disks, firmware - are no games)
_RATED_DATS_OVERRIDE = {"Commodore Amiga": ("Commodore Amiga - Games - [ADF]",)}

ProgressFn = Callable[[int, int, str], None]
Opener = Callable[[urllib.request.Request], Any]


class RatingsError(Exception):
    """Problem downloading or indexing the LaunchBox data."""


# --------------------------------------------------------------------------- platform mapping

def lb_platform(platform: Any) -> str:
    """The LaunchBox platform of one of our platforms (a name or a ``Platform``); ``""`` = not rated."""
    return LB_PLATFORMS.get(getattr(platform, "name", platform), "")


def supported(platform: Any) -> bool:
    return bool(lb_platform(platform))


def rated_dats(platform: Any) -> tuple[str, ...]:
    """DAT names of the platform whose games take part in the rating filter (empty = system not rated)."""
    name = getattr(platform, "name", platform)
    if name not in LB_PLATFORMS:
        return ()
    return _RATED_DATS_OVERRIDE.get(name, tuple(getattr(platform, "dats", ()) or ()))


# --------------------------------------------------------------------------- title normalisation

_SPLIT_RE = re.compile(r"\s+[-–—]\s+|:\s+|\s*/\s*")
_TRAIL_ARTICLE_RE = re.compile(r"^(.*?),\s*(the|a|an)$", re.IGNORECASE)
_LEAD_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+(?=\S)", re.IGNORECASE)
_APOS_RE = re.compile(r"['’‘`´]")
_NONALNUM_RE = re.compile(r"[^0-9a-z]+")
_ROMAN_ALL = frozenset("i ii iii iv v vi vii viii ix x xi xii xiii xiv xv".split())
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "xi": "11", "xii": "12"}


@functools.lru_cache(maxsize=200000)
def norm_title(title: str) -> str:
    """Case-, punctuation-, accent-, article- and subtitle-separator-insensitive form of a game title.

    ``"Legend of Zelda, The - A Link to the Past"`` and ``"The Legend of Zelda: A Link to the Past"`` both give
    ``"legend of zelda link to past"``-style equal keys (the article of every segment is dropped, ``&`` = ``and``,
    apostrophes vanish, other punctuation is a space)."""
    s = unicodedata.normalize("NFKD", title or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("&", " and ")
    parts: list[str] = []
    for seg in _SPLIT_RE.split(s):
        seg = seg.strip()
        m = _TRAIL_ARTICLE_RE.match(seg)
        if m:
            seg = m.group(1)           # "Legend of Zelda, The" -> "Legend of Zelda" (the article carries no information)
        else:
            seg = _LEAD_ARTICLE_RE.sub("", seg)
        parts.append(seg)
    s = _APOS_RE.sub("", " ".join(parts).casefold())
    return _NONALNUM_RE.sub(" ", s).strip()


_ROMAN_VALUE = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11,
                "xii": 12, "xiii": 13, "xiv": 14, "xv": 15}


def number_signature(key: str) -> tuple[int, ...]:
    """The numbers of a key (digits and roman numerals I..XV as values), in order: two titles that differ here are
    different games (sequels, volumes, years) however alike they read."""
    out = []
    for t in key.split():
        if t.isdigit():
            out.append(int(t))
        elif t in _ROMAN_VALUE and t not in ("i", "x", "v") or (t in ("v", "x") and key.split()[0] != t):
            out.append(_ROMAN_VALUE[t])
    return tuple(out)


def roman_key(key: str) -> str:
    """``key`` with the roman numerals II..IX / XI / XII read as digits ("final fantasy ii" = "final fantasy 2")."""
    return " ".join(_ROMAN.get(t, t) for t in key.split())


# --------------------------------------------------------------------------- the streaming parser

class _Counting:
    """File-like wrapper that reports how many bytes were read (progress) and honours a cancel token."""

    def __init__(self, fh: Any, total: int, progress: Optional[ProgressFn], message: str, cancel: Any) -> None:
        self.fh, self.total, self.progress, self.message, self.cancel = fh, total, progress, message, cancel
        self.done = 0
        self._last = 0.0

    def read(self, n: int = -1) -> bytes:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        data = self.fh.read(n)
        self.done += len(data)
        now = time.monotonic()
        if self.progress is not None and now - self._last >= 0.25:
            self._last = now
            self.progress(self.done, self.total, self.message)
        return data


def parse_metadata(stream: Any, wanted: set[str], total: int = 0, progress: Optional[ProgressFn] = None,
                   cancel: Any = None) -> tuple[dict[int, dict[str, Any]], dict[int, list[str]]]:
    """Stream-parse ``Metadata.xml``: ``({dbid: game}, {dbid: [alternate names]})`` for the ``wanted`` platforms.

    ``game`` = ``{name, plat, rating (stars), votes, year}``; unrated games (rating absent / 0, no votes) are
    dropped. Memory stays bounded (``clear()`` after every element); alternate names are kept for the rated
    games only (they carry no platform of their own: the game's DatabaseID links them)."""
    src = _Counting(stream, total, progress, "Building the ratings index", cancel)
    games: dict[int, dict[str, Any]] = {}
    alts: dict[int, list[str]] = {}
    root = None
    try:
        for ev, el in ET.iterparse(src, events=("start", "end")):
            if ev == "start":
                if root is None:
                    root = el
                continue
            tag = el.tag
            if tag == "Game":
                plat = el.findtext("Platform") or ""
                if plat in wanted:
                    try:
                        dbid = int(el.findtext("DatabaseID") or 0)
                        rating = float(el.findtext("CommunityRating") or 0)
                        votes = int(float(el.findtext("CommunityRatingCount") or 0))
                    except ValueError:
                        dbid, rating, votes = 0, 0.0, 0
                    name = (el.findtext("Name") or "").strip()
                    if dbid and name and rating > 0 and votes > 0:
                        year = (el.findtext("ReleaseYear") or (el.findtext("ReleaseDate") or "")[:4] or "").strip()
                        games[dbid] = {"name": name, "plat": plat, "rating": rating, "votes": votes,
                                       "year": int(year) if year.isdigit() else 0}
            elif tag == "GameAlternateName":
                try:
                    dbid = int(el.findtext("DatabaseID") or 0)
                except ValueError:
                    dbid = 0
                alt = (el.findtext("AlternateName") or "").strip()
                if dbid and alt:
                    alts.setdefault(dbid, []).append(alt)
            if tag in ("Game", "GameAlternateName", "GameImage", "Platform", "PlatformAlternateName", "Emulator",
                       "EmulatorPlatform") and root is not None:
                root.clear()
    except ET.ParseError as exc:
        raise RatingsError(f"the LaunchBox metadata is not valid XML ({exc})") from exc
    if progress is not None:
        progress(src.done, total, "Building the ratings index")
    return games, {k: v for k, v in alts.items() if k in games}


def build_rows(games: dict[int, dict[str, Any]], alts: dict[int, list[str]]) -> list[tuple]:
    """Index rows ``(plat, key, rating, votes, dbid, name, year, alt)`` (one per platform + normalised title).

    Ranking of candidates for one key: own name before an alternate name, then most votes, then the lowest dbid."""
    best: dict[tuple[str, str], tuple] = {}

    def offer(g: dict[str, Any], dbid: int, key: str, alt: int) -> None:
        if not key:
            return
        cand = (alt, -g["votes"], dbid)
        cur = best.get((g["plat"], key))
        if cur is None or cand < cur[0]:
            best[(g["plat"], key)] = (cand, (g["plat"], key, g["rating"], g["votes"], dbid, g["name"], g["year"], alt))

    for dbid, g in games.items():
        offer(g, dbid, norm_title(g["name"]), 0)
    for dbid, names in alts.items():
        g = games[dbid]
        for nm in names:
            offer(g, dbid, norm_title(nm), 1)
    return [v[1] for _k, v in sorted(best.items())]


# --------------------------------------------------------------------------- the local index

def _dir(directory: Any = None) -> Path:
    return Path(directory) if directory is not None else paths.ratings_dir()


def index_path(directory: Any = None) -> Path:
    return _dir(directory) / INDEX_NAME


def read_manifest(directory: Any = None) -> dict[str, Any]:
    try:
        data = json.loads((_dir(directory) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(directory: Path, data: dict[str, Any]) -> None:
    tmp = directory / (MANIFEST + ".part")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, directory / MANIFEST)


def installed(directory: Any = None) -> Optional[dict[str, Any]]:
    """The manifest of the installed index (``None`` when there is no usable index)."""
    m = read_manifest(directory)
    if m.get("schema") != SCHEMA or not index_path(directory).is_file():
        return None
    return m


def write_index(rows: list[tuple], dest: Path, meta: dict[str, Any]) -> None:
    """Create the sqlite index at ``dest`` (any existing file is replaced)."""
    _unlink(dest)
    con = sqlite3.connect(str(dest))
    try:
        con.execute("CREATE TABLE entry (plat TEXT NOT NULL, key TEXT NOT NULL, rating REAL NOT NULL, votes INTEGER NOT NULL,"
                    " dbid INTEGER NOT NULL, name TEXT NOT NULL, year INTEGER NOT NULL, alt INTEGER NOT NULL,"
                    " PRIMARY KEY (plat, key)) WITHOUT ROWID")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID")
        con.executemany("INSERT INTO entry VALUES (?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in meta.items()])
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()


def build_index(zip_path: Any, directory: Any = None, progress: Optional[ProgressFn] = None, cancel: Any = None,
                meta: Optional[dict[str, Any]] = None, commit_lock: Any = None) -> dict[str, Any]:
    """Stream-parse ``Metadata.xml`` out of ``zip_path`` into ``ratings.sqlite`` (atomic replace); returns the manifest.

    The new index is validated (rows present, every wanted platform read) before it replaces the old one."""
    folder = _dir(directory)
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / (INDEX_NAME + ".part")
    t0 = time.time()
    try:
        with zipfile.ZipFile(str(zip_path)) as zf:
            try:
                info = zf.getinfo("Metadata.xml")
            except KeyError:
                raise RatingsError("the download holds no Metadata.xml") from None
            with zf.open(info) as fh:
                games, alts = parse_metadata(fh, set(LB_PLATFORMS.values()), info.file_size, progress, cancel)
    except zipfile.BadZipFile as exc:
        raise RatingsError(f"the download is not a zip file ({exc})") from exc
    rows = build_rows(games, alts)
    plats = {r[0] for r in rows}
    if not rows or not plats:
        raise RatingsError("the LaunchBox metadata holds no ratings for the supported systems")
    stats = {p: sum(1 for r in rows if r[0] == p) for p in sorted(plats)}
    seconds = round(time.time() - t0, 1)
    manifest = {**(meta or {}), "schema": SCHEMA, "built_at": _now(), "games": len(games), "entries": len(rows),
                "platforms": stats, "parse_seconds": seconds}
    try:
        write_index(rows, part, manifest)
        with (commit_lock if commit_lock is not None else contextlib.nullcontext()):
            os.replace(part, index_path(directory))
            _write_manifest(folder, manifest)
    finally:
        _unlink(part)
    manifest["size"] = index_path(directory).stat().st_size
    if progress is not None:
        progress(1, 1, f"Ratings index ready ({len(rows)} titles, {seconds} s)")
    return manifest


# --------------------------------------------------------------------------- lookup

class _Plat:
    """One platform of the index in memory: primary map, roman map, token index."""

    def __init__(self, rows: list[tuple]) -> None:
        self.by_key: dict[str, tuple] = {r[1]: r for r in rows}
        roman: dict[str, set[str]] = {}
        tokens: dict[str, list[str]] = {}
        for key in self.by_key:
            roman.setdefault(roman_key(key), set()).add(key)
            for t in set(key.split()):
                if len(t) >= 4 and not t.isdigit():
                    tokens.setdefault(t, []).append(key)
        self.roman = roman
        self.tokens = tokens

    def fuzzy(self, key: str) -> Optional[tuple[tuple, float]]:
        if len(key) < 7:
            return None
        toks = [t for t in set(key.split()) if len(t) >= 4 and not t.isdigit()]
        if not toks:
            return None
        pool: set[str] = set()
        for t in toks:
            pool.update(self.tokens.get(t, ()))
        digits = number_signature(key)
        kset = set(key.split())
        scored: list[tuple[float, str]] = []
        sm = difflib.SequenceMatcher(autojunk=False)
        sm.set_seq2(key)
        for cand in pool:
            if abs(len(cand) - len(key)) > 0.2 * max(len(cand), len(key)):
                continue
            if number_signature(cand) != digits:
                continue
            cset = set(cand.split())
            if {t.rstrip("s") for t in cset} == {t.rstrip("s") for t in kset}:
                continue                  # only a singular / plural difference: not obviously the same game
            if cset < kset or kset < cset:
                continue                  # one title only ADDS whole words (a subtitle, "DX", a hack tag): another game
            sm.set_seq1(cand)
            if sm.real_quick_ratio() < FUZZY_MIN or sm.quick_ratio() < FUZZY_MIN:
                continue
            r = sm.ratio()
            if r >= FUZZY_MIN:
                scored.append((r, cand))
        if not scored:
            return None
        scored.sort(key=lambda x: (-x[0], x[1]))
        if len(scored) > 1 and scored[0][0] - scored[1][0] < FUZZY_GAP and \
                self.by_key[scored[0][1]][4] != self.by_key[scored[1][1]][4]:
            return None             # not unique: two different games are about equally close
        return self.by_key[scored[0][1]], scored[0][0]


class Store:
    """The local ratings index (read-only). Cheap to create; platforms load lazily and reload when the file changes."""

    def __init__(self, directory: Any = None) -> None:
        self.directory = directory
        self._lock = threading.RLock()
        self._sig: Optional[tuple] = None
        self._plats: dict[str, _Plat] = {}
        self._cache: dict[tuple[str, str], Optional[dict[str, Any]]] = {}

    def _check(self) -> bool:
        p = index_path(self.directory)
        try:
            st = p.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig != self._sig:
            self._sig, self._plats, self._cache = sig, {}, {}
        return sig is not None

    def available(self) -> bool:
        with self._lock:
            return self._check()

    def meta(self) -> Optional[dict[str, Any]]:
        return installed(self.directory)

    def _plat(self, lb: str) -> Optional[_Plat]:
        if lb in self._plats:
            return self._plats[lb]
        con = sqlite3.connect(f"file:{index_path(self.directory)}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT plat,key,rating,votes,dbid,name,year,alt FROM entry WHERE plat=?", (lb,)).fetchall()
        finally:
            con.close()
        pd = _Plat(rows) if rows else None
        self._plats[lb] = pd           # type: ignore[assignment]
        return pd

    def detail(self, platform: Any, title: str) -> Optional[dict[str, Any]]:
        """``{rating (0-10, 1 decimal), votes, dbid, name, year, kind, score}`` or ``None`` (no / unsure match)."""
        lb = lb_platform(platform)
        if not lb:
            return None
        with self._lock:
            if not self._check():
                return None
            ck = (lb, title)
            if ck in self._cache:
                return self._cache[ck]
            out = self._match(lb, title)
            if len(self._cache) > 400000:
                self._cache.clear()
            self._cache[ck] = out
            return out

    def _match(self, lb: str, title: str) -> Optional[dict[str, Any]]:
        pd = self._plat(lb)
        key = norm_title(title)
        if pd is None or len(key) < 2:
            return None
        row = pd.by_key.get(key)
        kind, score = ("alt" if row and row[7] else "exact"), 1.0
        if row is None:
            cands = pd.roman.get(roman_key(key))
            if cands:
                hit = {pd.by_key[k][4]: pd.by_key[k] for k in cands}
                if len(hit) == 1:
                    row, kind, score = next(iter(hit.values())), "roman", 0.99
        if row is None:
            f = pd.fuzzy(key)
            if f is not None:
                row, score = f
                kind = "fuzzy"
        if row is None:
            return None
        return {"rating": round(row[2] * 2, 1), "votes": row[3], "dbid": row[4], "name": row[5], "year": row[6],
                "kind": kind, "score": round(score, 3)}

    def lookup(self, platform: Any, title: str) -> Optional[tuple[float, int]]:
        """``(rating out of 10, votes)`` or ``None``."""
        d = self.detail(platform, title)
        return (d["rating"], d["votes"]) if d else None


_STORES: dict[str, Store] = {}


def default_store() -> Store:
    """The store of the current data directory (one per directory, so tests with ``ROMORG_DATA_DIR`` stay apart)."""
    key = str(paths.ratings_dir())
    st = _STORES.get(key)
    if st is None:
        st = _STORES[key] = Store(key)
    return st


def lookup(platform: Any, title: str) -> Optional[tuple[float, int]]:
    """``ratings.lookup(platform, title) -> (rating10, votes) | None`` against the installed index."""
    return default_store().lookup(platform, title)


def format_rating(rating: float, votes: int) -> str:
    return f"{rating:.1f} · {votes} vote" + ("" if votes == 1 else "s")


# --------------------------------------------------------------------------- download / update

def _parse_http_date(s: str) -> float:
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


def source_date(manifest: Optional[dict[str, Any]]) -> str:
    """``YYYY-MM-DD`` of the LaunchBox data an index was built from ("" when unknown)."""
    if not manifest:
        return ""
    ts = _parse_http_date(manifest.get("last_modified", ""))
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else str(manifest.get("built_at", ""))[:10]


def age_days(manifest: Optional[dict[str, Any]], now: Optional[float] = None) -> Optional[float]:
    """Days since the index was built (``None`` when there is none)."""
    if not manifest:
        return None
    ts = _iso_to_ts(str(manifest.get("built_at", "")))
    return None if not ts else ((now if now is not None else time.time()) - ts) / 86400.0


def _iso_to_ts(s: str) -> float:
    import calendar
    try:
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0.0


def _open_head(open_fn: Opener) -> Any:
    req = urllib.request.Request(SOURCE_URL, method="HEAD", headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        return open_fn(req)
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 405, 501):
            raise
    return open_fn(urllib.request.Request(SOURCE_URL, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}))


def check_update(opener: Optional[Opener] = None, directory: Any = None, timeout: float = 10,
                 now: Optional[float] = None, gate: bool = True) -> dict[str, Any]:
    """Check-only (one HEAD): ``{"status", "latest", "etag", "installed", "age_days", "error"?}``.

    Status: ``missing`` (no index: download), ``up_to_date``, ``update_available`` (the server's data is newer AND
    the installed index is older than :data:`REFRESH_DAYS`, or ``gate=False``), ``recent`` (newer data exists but the
    index is younger than the gate: nothing to do), ``error`` (never raises). An index younger than the gate is not
    even asked about (no network at all)."""
    man = installed(directory)
    age = age_days(man, now)
    row: dict[str, Any] = {"status": "missing" if man is None else "up_to_date", "latest": None, "etag": None,
                           "installed": source_date(man) or None, "age_days": age}
    if man is not None and gate and age is not None and age < REFRESH_DAYS:
        return row
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=timeout))
    try:
        resp = _open_head(open_fn)
        try:
            status = getattr(resp, "status", 200) or 200
            headers = getattr(resp, "headers", None)
            lm = headers.get("Last-Modified") if headers is not None else None
            etag = headers.get("ETag") if headers is not None else None
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        if status >= 400:
            row.update(status="error", error=f"HTTP {status}")
            return row
        row["latest"], row["etag"] = lm, etag
        if man is None:
            return row
        newer = (etag and etag != man.get("etag")) or (lm and lm != man.get("last_modified"))
        if lm and man.get("last_modified") and _parse_http_date(lm) and _parse_http_date(man["last_modified"]):
            newer = _parse_http_date(lm) > _parse_http_date(man["last_modified"])
        row["status"] = "update_available" if newer else "up_to_date"
        if newer and gate and age is not None and age < REFRESH_DAYS:
            row["status"] = "recent"
    except urllib.error.HTTPError as exc:
        row.update(status="error", error=f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        row.update(status="error", error=str(getattr(exc, "reason", exc)))
    return row


def download_and_build(directory: Any = None, progress: Optional[ProgressFn] = None, cancel: Any = None,
                       opener: Optional[Opener] = None, commit_lock: Any = None) -> dict[str, Any]:
    """Download ``Metadata.zip`` (to ``ratings/Metadata.zip.part``), build the index from it, delete the zip.

    Network / HTTP / validation problems raise :class:`RatingsError` and leave an installed index untouched; a cancel
    raises :class:`tosec.Cancelled`."""
    folder = _dir(directory)
    folder.mkdir(parents=True, exist_ok=True)
    zpart = folder / "Metadata.zip.part"
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=TIMEOUT))
    if progress:
        progress(0, 0, "Downloading the LaunchBox ratings")
    try:
        resp = open_fn(urllib.request.Request(SOURCE_URL, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}))
    except urllib.error.HTTPError as exc:
        raise RatingsError(f"HTTP {exc.code} from {SOURCE_URL}") from exc
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        raise RatingsError(f"could not download the ratings ({getattr(exc, 'reason', exc)})") from exc
    meta: dict[str, Any] = {}
    try:
        status = getattr(resp, "status", 200) or 200
        if status >= 400:
            raise RatingsError(f"HTTP {status} from {SOURCE_URL}")
        headers = getattr(resp, "headers", None)
        meta = {"url": SOURCE_URL, "etag": headers.get("ETag") if headers is not None else None,
                "last_modified": headers.get("Last-Modified") if headers is not None else None,
                "downloaded_at": _now()}
        try:
            total = int((headers.get("Content-Length") if headers is not None else 0) or 0)
        except ValueError:
            total = 0
        done = 0
        try:
            with zpart.open("wb") as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total, "Downloading the LaunchBox ratings")
        except (Cancelled, KeyboardInterrupt):
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise RatingsError(f"download failed ({exc})") from exc
    except BaseException:
        _unlink(zpart)
        raise
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        if total and done != total:
            raise RatingsError(f"incomplete download (got {done} of {total} bytes)")
        return build_index(zpart, directory, progress, cancel, meta, commit_lock)
    finally:
        _unlink(zpart)
