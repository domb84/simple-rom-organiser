"""The Switch title database: which title and version a file is, and what the game is called.

The Switch has no checksum DATs a file can be matched against without reading all of it (the card dumps run to many gigabytes
and the eShop files are encrypted), so games are told by *title ID*, which the container headers give (``switchfmt``). The names
and the NCA content IDs come from the community title database ``blawar/titledb`` on GitHub. Its files are large, so the app
reads two of them once, keeps what it needs in a small SQLite file in its data folder, and uses that from then on:

* ``US.en.json`` (about 90 MB, 26 MB on the wire): title ID -> name, publisher, release date;
* ``cnmts.json`` (about 50 MB, 6 MB on the wire): the content ID of every NCA -> title ID, version, kind; and which application an
  update or add-on belongs to.

Nothing is fetched unless the user asks (Switch page, *Update the title database*). Without the database a file is still
recognised by its tickets, its ``.cnmt.xml``, its name or (with ``prod.keys``) its NCA header; it just has no official name.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from . import meter

__all__ = ["SWITCH_DAT", "SWITCH_DATS", "SWITCH_UPDATES_DAT", "SWITCH_DLC_DAT", "VERSIONS_URL", "REGION_FILES", "build_dat", "TITLES_URL", "CNMTS_URL", "SwitchDb", "db_path", "db_info", "build", "download", "update", "check_update",
           "download_and_build", "MAX_AGE_DAYS"]

MAX_AGE_DAYS = 3        # a database younger than this is not even asked about (no request at all)

BASE_URL = "https://raw.githubusercontent.com/blawar/titledb/master/"
TITLES_URL = BASE_URL + "US.en.json"
CNMTS_URL = BASE_URL + "cnmts.json"
USER_AGENT = "simple-rom-organiser"
SWITCH_DAT = "Nintendo - Switch"        # made from the title database (no checksums): the games, a catalogue to have / miss against
SWITCH_UPDATES_DAT = "Nintendo - Switch (Updates)"
SWITCH_DLC_DAT = "Nintendo - Switch (DLC)"
SWITCH_DATS = (SWITCH_DAT, SWITCH_UPDATES_DAT, SWITCH_DLC_DAT)
VERSIONS_URL = BASE_URL + "versions.json"
# the eShop lists of titledb, one per store: the store a title is in is its region (US.en is the base list)
REGION_FILES = {"USA": "US.en.json", "Europe": "GB.en.json", "Japan": "JP.ja.json"}
ProgressFn = Callable[[int, int, str], None]


def db_info(path: Optional[Path] = None) -> Dict[str, Any]:
    """``SwitchDb(path).info()`` with the database closed again."""
    db = SwitchDb(path)
    try:
        return db.info()
    finally:
        db.close()


def db_path() -> Path:
    from .paths import data_dir
    return data_dir() / "switch" / "titles.sqlite"


class SwitchDb:
    """Read access to the title database (a missing file is an empty database)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else db_path()
        self.conn: Optional[sqlite3.Connection] = None
        if self.path.is_file():
            try:
                self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
                self.conn.execute("SELECT 1 FROM titles LIMIT 1")
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

    def info(self) -> Dict[str, Any]:
        """``{available, titles, ncas, fetched, source}``."""
        out: Dict[str, Any] = {"available": self.available, "titles": 0, "ncas": 0, "fetched": "", "source": "", "path": str(self.path),
                               "fetched_at": 0.0}
        if self.conn is None:
            return out
        try:
            out["titles"] = self.conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
            out["ncas"] = self.conn.execute("SELECT COUNT(*) FROM ncas").fetchone()[0]
            for key, value in self.conn.execute("SELECT key, value FROM info"):
                if key in ("fetched", "source"):
                    out[key] = value
                elif key == "fetched_at":
                    try:
                        out[key] = float(value)
                    except ValueError:
                        pass
        except sqlite3.Error:
            pass
        return out

    def nca(self, nca_id: str) -> Optional[Tuple[str, int, int]]:
        """``(title ID, version, content type)`` of an NCA's content ID (32 hex digits), or None."""
        if self.conn is None:
            return None
        try:
            row = self.conn.execute("SELECT title_id, version, type FROM ncas WHERE nca=?", (nca_id.lower(),)).fetchone()
        except sqlite3.Error:
            return None
        return (row[0], int(row[1]), int(row[2])) if row else None

    def title(self, title_id: str) -> Optional[Dict[str, Any]]:
        """``{id, name, publisher, released}`` of a title ID, or None."""
        if self.conn is None:
            return None
        try:
            row = self.conn.execute("SELECT id, name, publisher, released FROM titles WHERE id=?", (title_id.upper(),)).fetchone()
        except sqlite3.Error:
            return None
        return {"id": row[0], "name": row[1], "publisher": row[2], "released": row[3]} if row else None

    def catalogue(self) -> list:
        """``[(dat, title ID, version, label)]`` as built (see ``_catalogue``)."""
        if self.conn is None:
            return []
        try:
            return [tuple(r) for r in self.conn.execute("SELECT dat, id, version, label FROM catalogue ORDER BY label COLLATE NOCASE")]
        except sqlite3.Error:
            return []

    def label_of(self, title_id: str, version: int, kind: str) -> str:
        """The catalogue name of a file: a game or add-on by its title ID, an update by title ID and version; "" when it is not in it."""
        if self.conn is None:
            return ""
        dat = {"application": SWITCH_DAT, "update": SWITCH_UPDATES_DAT, "addon": SWITCH_DLC_DAT}.get(kind, "")
        try:
            row = self.conn.execute("SELECT label FROM catalogue WHERE dat=? AND id=? AND version=?",
                                    (dat, title_id.upper(), int(version) if kind == "update" else -1)).fetchone()
        except sqlite3.Error:
            return ""
        return row[0] if row else ""

    def application_of(self, title_id: str) -> Optional[str]:
        """The application an update or add-on belongs to, as the database records it (None: not known)."""
        if self.conn is None:
            return None
        try:
            row = self.conn.execute("SELECT base FROM apps WHERE id=?", (title_id.upper(),)).fetchone()
        except sqlite3.Error:
            return None
        return row[0] if row and row[0] else None


_REGION = {"USA": "USA", "EUR": "Europe", "JPN": "Japan", "KOR": "Korea", "CHN": "China"}


def _region_tag(regions: Iterable[str]) -> str:
    """``USA, Europe, Japan`` -> ``World``; ``{USA, Europe}`` -> ``USA, Europe`` ("" when no store lists the title)."""
    got = set(regions)
    if {"USA", "Europe", "Japan"} <= got:
        return "World"
    return ", ".join(r for r in ("USA", "Europe", "Japan") if r in got)


_LANG = {"ja": "Ja", "en": "En", "fr": "Fr", "de": "De", "es": "Es", "it": "It", "nl": "Nl", "pt": "Pt", "ru": "Ru", "ko": "Ko", "zh": "Zh"}


def _lang_tag(codes: Iterable[str]) -> str:
    """``['ja', 'en', 'zh', 'zh']`` -> ``Ja,En,Zh`` (Redump's spelling, in the order the list gives them)."""
    out: list = []
    for c in codes:
        tag = _LANG.get(str(c).lower()[:2])
        if tag and tag not in out:
            out.append(tag)
    return ",".join(out)


def _clean_name(name: str) -> str:
    name = re.sub(r"[\u2122\u00ae\u00a9]", "", name)
    return re.sub(r"[\\/:*?\"<>|]", " -", re.sub(r"\s+", " ", name)).strip(" .")


def _dlc_base(title_id: str) -> str:
    n = int(title_id, 16)
    return f"{(n & ~0xFFF) - 0x1000:016X}"


def _catalogue(info: Dict[str, Dict[str, Any]], versions: Dict[str, Iterable[int]], base_of: Dict[str, str]) -> list:
    """``[(dat, title ID, version, label)]``: one entry per game, per update version, per add-on. ``info`` is what the store lists say of
    each title: ``name``, ``regions`` (set), ``langs`` (list), ``demo``. ``version`` is -1 for an entry any version of a file matches (a game,
    an add-on). A label two entries share gets the title ID after it."""
    rows = []
    apps = {tid: i for tid, i in info.items() if tid.endswith("000") and _clean_name(i["name"])}
    for tid, i in apps.items():
        name = _clean_name(i["name"])
        tags = "".join(f" ({t})" for t in (_region_tag(i["regions"]), _lang_tag(i["langs"])) if t) + (" (Demo)" if i["demo"] else "")
        rows.append((SWITCH_DAT, tid, -1, f"{name}{tags}"))
        for v in sorted({int(x) for x in versions.get(tid, ()) if int(x) > 0}):
            rows.append((SWITCH_UPDATES_DAT, tid, v, f"{name} - Update{tags} (v{v >> 16})"))
    for tid, i in info.items():
        if tid.endswith(("000", "800")) or not _clean_name(i["name"]):
            continue
        base = base_of.get(tid) or _dlc_base(tid)
        if base in apps:
            rows.append((SWITCH_DLC_DAT, tid, -1, f"{_clean_name(i['name'])} (DLC)"))
    seen: Dict[str, int] = {}
    for _dat, _tid, _v, label in rows:
        seen[label] = seen.get(label, 0) + 1
    return [(dat, tid, v, label if seen[label] == 1 else f"{label} [{tid}{'' if v < 0 else f' v{v}'}]") for dat, tid, v, label in rows]


def build_dat(db: Optional[Path] = None, out_dir: Optional[Path] = None) -> int:
    """Write the three Switch "DATs" from the catalogue in the database: the games (with the stores they are sold in and their languages), their updates
    (one entry per version, ``Game - Update (USA) (v3)``) and their add-ons. No checksums exist for any of them; a file is matched by
    its title ID and version (``switchmatch``). Returns the number of games (0: no database yet)."""
    from xml.sax.saxutils import quoteattr
    from . import paths
    sdb = SwitchDb(db)
    try:
        rows = sdb.catalogue() if sdb.available else []
    finally:
        sdb.close()
    if not rows:
        return 0
    out_dir = Path(out_dir) if out_dir else paths.redump_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    version = time.strftime("%Y-%m-%d %H-%M-%S")
    for dat in SWITCH_DATS:
        games = []
        for label in (r[3] for r in rows if r[0] == dat):
            q = quoteattr(label)
            games.append(f"<game name={q}><category>{'Demos' if '(Demo)' in label else 'Games'}</category><description>{q[1:-1]}</description>"
                         f"<rom name={quoteattr(label + '.nsp')} size=\"0\"/></game>")
        tmp = out_dir / f"{dat}.dat.part"
        tmp.write_text(f'<?xml version="1.0"?>\n<datafile><header><name>{dat}</name><description>{dat} - made from the title database</description>'
                       f"<version>{version}</version><author>blawar/titledb</author></header>" + "".join(games) + "</datafile>",
                       encoding="utf-8")
        os.replace(tmp, out_dir / f"{dat}.dat")
    return sum(1 for r in rows if r[0] == SWITCH_DAT)


def _load(path: Path) -> Any:
    with open(path, "rb") as probe:
        gz = probe.read(2) == b"\x1f\x8b"
    with (gzip.open(path, "rb") if gz else open(path, "rb")) as f:
        return json.load(f)


def build(titles_json: Path, cnmts_json: Path, out: Path, source: str = "US.en", etags: Optional[Dict[str, str]] = None,
          versions_json: Optional[Path] = None, region_jsons: Optional[Dict[str, Path]] = None) -> Dict[str, int]:
    """Make the SQLite database from the JSON files (plain or gzip): the base eShop list (``titles_json``, the US store), the NCA list, the
    update versions and, when given, the other stores' lists (``region_jsons``: region -> file) that say where a title is sold.
    Written next to ``out`` and moved into place."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(str(tmp))
    try:
        conn.executescript(
            "CREATE TABLE titles (id TEXT PRIMARY KEY, name TEXT NOT NULL, publisher TEXT, released INTEGER);"
            "CREATE TABLE ncas (nca TEXT PRIMARY KEY, title_id TEXT NOT NULL, version INTEGER NOT NULL, type INTEGER NOT NULL);"
            "CREATE TABLE apps (id TEXT PRIMARY KEY, kind INTEGER NOT NULL, base TEXT);"
            "CREATE TABLE catalogue (dat TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, label TEXT NOT NULL, PRIMARY KEY (dat, id, version));"
            "CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT);")
        info: Dict[str, Dict[str, Any]] = {}

        def read_store(path: Path, region: str) -> list:
            data = _load(Path(path))
            got = []
            for entry in data.values():
                tid = (entry.get("id") or "").upper() if isinstance(entry, dict) else ""
                name = (entry.get("name") or "").strip() if isinstance(entry, dict) else ""
                if len(tid) != 16 or not name:
                    continue
                got.append((tid, name, entry.get("publisher") or "", int(entry.get("releaseDate") or 0)))
                i = info.setdefault(tid, {"name": name, "regions": set(), "langs": [], "demo": False})
                i["regions"].add(region)
                i["demo"] = i["demo"] or bool(entry.get("isDemo"))
                for lang in entry.get("languages") or ():
                    if lang not in i["langs"]:
                        i["langs"].append(lang)
            return got

        rows = read_store(Path(titles_json), "USA")
        for region, path in (region_jsons or {}).items():
            rows += read_store(path, region)
        conn.executemany("INSERT OR IGNORE INTO titles VALUES (?,?,?,?)", rows)
        titles = len(rows)
        cn = _load(Path(cnmts_json))
        nca_rows, app_rows = [], []
        for tid, versions in cn.items():
            if not isinstance(versions, dict):
                continue
            for ver, d in versions.items():
                if not isinstance(d, dict):
                    continue
                try:
                    v = int(ver)
                except ValueError:
                    continue
                for e in d.get("contentEntries") or ():
                    if e.get("ncaId"):
                        nca_rows.append((e["ncaId"].lower(), tid.upper(), v, int(e.get("type") or 0)))
                other = (d.get("otherApplicationId") or "").upper()
                app_rows.append((tid.upper(), int(d.get("titleType") or 0), other if len(other) == 16 else ""))
        del cn
        conn.executemany("INSERT OR REPLACE INTO ncas VALUES (?,?,?,?)", nca_rows)
        conn.executemany("INSERT OR REPLACE INTO apps VALUES (?,?,?)", app_rows)
        vers: Dict[str, list] = {}
        if versions_json is not None and Path(versions_json).is_file():
            for tid, table in _load(Path(versions_json)).items():
                if isinstance(table, dict) and len(tid) == 16:
                    vers[tid.upper()] = [int(v) for v in table if str(v).isdigit()]
        base_of = {tid: base for tid, _k, base in app_rows if base}
        cat = _catalogue(info, vers, base_of)
        conn.executemany("INSERT OR REPLACE INTO catalogue VALUES (?,?,?,?)", cat)
        conn.executemany("INSERT OR REPLACE INTO info VALUES (?,?)",
                         [("fetched", time.strftime("%Y-%m-%d %H:%M")), ("fetched_at", str(time.time())), ("source", source),
                          *[(f"etag_{k}", v) for k, v in (etags or {}).items()]])
        conn.commit()
        counts = {"titles": titles, "ncas": len(nca_rows), "games": sum(1 for c in cat if c[0] == SWITCH_DAT), "updates": sum(1 for c in cat if c[0] == SWITCH_UPDATES_DAT)}
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp, out)
    return counts


def download(url: str, dest: Path, progress: Optional[ProgressFn] = None, cancel: Any = None, label: str = "") -> str:
    """Fetch ``url`` (gzip on the wire) to ``dest`` as received; returns the ETag. ``cancel`` is a callable or an Event."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    tmp = Path(dest).with_name(Path(dest).name + ".part")
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
                raise InterruptedError("cancelled")
            chunk = resp.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            meter.add(len(chunk))
            if progress:
                progress(done, total or done, label)
        encoded = resp.headers.get("Content-Encoding", "")
        etag = resp.headers.get("ETag", "") or ""
    if encoded != "gzip":                      # (plain JSON: gzip it so that ``build`` reads both the same way)
        with open(tmp, "rb") as src, gzip.open(str(tmp) + ".gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(str(tmp) + ".gz", tmp)
    os.replace(tmp, dest)
    return etag


def _head_etag(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers.get("ETag", "") or ""


def _stored_etags(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if path.is_file():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                out = {k[5:]: v for k, v in conn.execute("SELECT key, value FROM info WHERE key LIKE 'etag_%'")}
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return out


def check_update(timeout: float = 10, gate: bool = True, path: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Is there a newer title database? ``{status: missing | update_available | up_to_date | error, ...}``.

    With ``gate`` a database fetched less than :data:`MAX_AGE_DAYS` days ago is not asked about (no request). Otherwise the ETags of the
    two source files are compared with the ones stored when it was built (two small HEAD requests)."""
    path = Path(path) if path else db_path()
    info = db_info(path)
    if not info["available"]:
        return {"status": "missing"}
    from . import paths
    if path == db_path() and not all((paths.redump_dir() / f"{d}.dat").is_file() for d in SWITCH_DATS):
        return {"status": "update_available"}                        # (the catalogue DAT is made when the database is built)
    if gate and (now if now is not None else time.time()) - float(info.get("fetched_at") or 0) < MAX_AGE_DAYS * 86400:
        return {"status": "up_to_date", "skipped": True}
    try:
        remote = {"titles": _head_etag(TITLES_URL, timeout), "cnmts": _head_etag(CNMTS_URL, timeout),
                  **{f"store_{r}": _head_etag(BASE_URL + f, timeout) for r, f in REGION_FILES.items() if r != "USA"}}
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc) or type(exc).__name__}
    local = _stored_etags(path)
    same = bool(remote["titles"]) and all(remote[k] == local.get(k) for k in remote)
    return {"status": "up_to_date" if same else "update_available", "etag": remote}


def download_and_build(progress: Optional[ProgressFn] = None, cancel: Any = None, out: Optional[Path] = None) -> Dict[str, int]:
    """The same as :func:`update` (the name the update manager calls)."""
    return update(progress, cancel, out)


def update(progress: Optional[ProgressFn] = None, cancel: Any = None, out: Optional[Path] = None) -> Dict[str, int]:
    """Download the two title database files and rebuild the SQLite file (and the catalogue "DAT" made from it). Returns ``{titles, ncas}``."""
    own = out is None
    out = Path(out) if out else db_path()
    work = Path(tempfile.mkdtemp(prefix="titledb-", dir=out.parent if out.parent.is_dir() else None))
    try:
        e1 = download(CNMTS_URL, work / "cnmts.json.gz", progress, cancel, "Switch title database: NCA list")
        e2 = download(TITLES_URL, work / "titles.json.gz", progress, cancel, "Switch title database: names")
        e3 = ""
        try:
            e3 = download(VERSIONS_URL, work / "versions.json.gz", progress, cancel, "Switch title database: update versions")
        except (OSError, ValueError):
            pass                                                     # (optional: games and add-ons still work)
        stores: Dict[str, Path] = {}
        store_etags: Dict[str, str] = {}
        for region, name in REGION_FILES.items():
            if region == "USA":
                continue
            try:
                store_etags[f"store_{region}"] = download(BASE_URL + name, work / f"{region}.json.gz", progress, cancel,
                                                          f"Switch title database: {region} store")
                stores[region] = work / f"{region}.json.gz"
            except (OSError, ValueError):
                pass                                                 # (optional: without it a title sold only there has no region)
        if progress:
            progress(0, 0, "Building the Switch title database...")
        counts = build(work / "titles.json.gz", work / "cnmts.json.gz", out,
                       etags={"cnmts": e1 or "", "titles": e2 or "", "versions": e3 or "", **store_etags},
                       versions_json=(work / "versions.json.gz") if (work / "versions.json.gz").is_file() else None, region_jsons=stores)
        if own:
            try:
                build_dat(out)
            except OSError:
                pass
        return counts
    finally:
        shutil.rmtree(work, ignore_errors=True)
