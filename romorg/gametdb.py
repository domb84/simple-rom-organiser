"""Official game names for Wii, GameCube, Wii U and PlayStation 2 discs: GameTDB's plain-text lists (``wiitdb.txt``, ``wiiutdb.txt``) and
PCSX2's ``GameIndex.yaml`` (serial to name).

Each line is ``GAMEID = Title`` (6-character IDs, ``SB4E01 = Super Mario Galaxy 2``, or the 4-character code). They are small (about
1 MB together), fetched when the app starts if the copy is missing or older than a few days (the same way the other databases are),
and kept in a SQLite file in the data folder. Without them a game is named by its disc header or its file.
"""

from __future__ import annotations

import gzip
import os
import sqlite3
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import meter

__all__ = ["URLS", "WIIU_DAT", "build_dat", "GameTdb", "db_path", "db_info", "build", "check_update", "download_and_build", "MAX_AGE_DAYS"]

URLS = {"wii": "https://www.gametdb.com/wiitdb.txt", "wiiu": "https://www.gametdb.com/wiiutdb.txt",
        "wiiu_xml": "https://www.gametdb.com/wiiutdb.zip",       # the same list with the type (disc / eShop / Virtual Console), region and languages
        "ps2": "https://raw.githubusercontent.com/PCSX2/pcsx2/master/bin/resources/GameIndex.yaml"}   # (PCSX2's own serial list)
USER_AGENT = "simple-rom-organiser"
WIIU_DAT = "Nintendo - Wii U"          # the DAT made from GameTDB's list: Redump has none for the Wii U (no checksums, a catalogue of the games)
MAX_AGE_DAYS = 3
ProgressFn = Callable[[int, int, str], None]


def db_path() -> Path:
    from .paths import data_dir
    return data_dir() / "nintendo" / "gametdb.sqlite"


class GameTdb:
    """Read access (a missing file is an empty database)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else db_path()
        self.conn: Optional[sqlite3.Connection] = None
        if self.path.is_file():
            try:
                self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
                self.conn.execute("SELECT 1 FROM names LIMIT 1")
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

    def name(self, kind: str, game_id: str) -> str:
        """The official title of a disc ID (``kind``: ``wii`` | ``gc`` | ``wiiu``); "" when unknown. A 6-character ID is tried first,
        then the first game with the same 4 characters."""
        if self.conn is None or not game_id:
            return ""
        table = "wiiu" if kind == "wiiu" else "ps2" if kind == "ps2" else "wii"
        try:
            row = self.conn.execute("SELECT name FROM names WHERE kind=? AND id=?", (table, game_id.upper())).fetchone()
            if row is None and len(game_id) >= 4:
                row = self.conn.execute("SELECT name FROM names WHERE kind=? AND id LIKE ? ORDER BY id LIMIT 1",
                                        (table, game_id[:4].upper() + "%")).fetchone()
        except sqlite3.Error:
            return ""
        return row[0] if row else ""

    def disc_label(self, game_id: str) -> str:
        """The catalogue name (``Title (Region) (En,Fr)``) of a Wii U disc by its game code; "" when unknown."""
        if self.conn is None or not game_id:
            return ""
        try:
            row = self.conn.execute("SELECT name FROM names WHERE kind='wiiu_disc' AND id LIKE ? ORDER BY id LIMIT 1", (game_id[:4].upper() + "%",)).fetchone()
        except sqlite3.Error:
            return ""
        return row[0] if row else ""

    def all(self, kind: str) -> list:
        """Every ``(id, name)`` of a list."""
        if self.conn is None:
            return []
        try:
            return [(r[0], r[1]) for r in self.conn.execute("SELECT id, name FROM names WHERE kind=?", (kind,))]
        except sqlite3.Error:
            return []

    def info(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"available": self.available, "titles": 0, "fetched": "", "fetched_at": 0.0, "path": str(self.path)}
        if self.conn is None:
            return out
        try:
            out["titles"] = self.conn.execute("SELECT COUNT(*) FROM names").fetchone()[0]
            for key, value in self.conn.execute("SELECT key, value FROM info"):
                if key == "fetched":
                    out["fetched"] = value
                elif key == "fetched_at":
                    out["fetched_at"] = float(value)
        except (sqlite3.Error, ValueError):
            pass
        return out


def db_info(path: Optional[Path] = None) -> Dict[str, Any]:
    db = GameTdb(path)
    try:
        return db.info()
    finally:
        db.close()


def parse(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        gid, sep, title = line.partition(" = ")
        if sep and gid != "TITLES" and 4 <= len(gid) <= 6 and gid.isalnum() and title.strip():
            out.setdefault(gid.upper(), title.strip())
    return out


def parse_wiiu_xml(raw: bytes) -> Dict[str, str]:
    """The Wii U disc games of GameTDB's ``wiiutdb.xml`` (plain or in a zip): ``{ID: "Title (Region) (En,Fr)"}``. eShop titles, Virtual
    Console games and channels are not discs and are left out."""
    import io
    import re
    import zipfile
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            raw = z.read(next(n for n in z.namelist() if n.lower().endswith(".xml")))
    text = raw.decode("utf-8", errors="replace")
    out: Dict[str, str] = {}
    for m in re.finditer(r'<game name="([^"]*)">\s*<id>([^<]*)</id>\s*<type>([^<]*)</type>', text):
        name, gid, kind = m.group(1), m.group(2).strip().upper(), m.group(3)
        if kind != "WiiU" or not gid:
            continue
        name = name.replace("&amp;", "&").replace("&quot;", '"').replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">")
        # the languages come as EN,FR,DE; the library rules know En,Fr,De
        name = re.sub(r"\(([A-Z]{2}(?:,[A-Z]{2})*)\)\s*$", lambda g: "(" + ",".join(x.capitalize() for x in g.group(1).split(",")) + ")", name)
        out.setdefault(gid, name)
    return out


def build(files: Dict[str, Path], out: Path) -> Dict[str, int]:
    """Make the SQLite file from the downloaded lists (``{"wii": path, "wiiu": path}``; plain or gzip)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(str(tmp))
    counts: Dict[str, int] = {}
    try:
        conn.executescript("CREATE TABLE names (kind TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (kind, id));"
                           "CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT);")
        for kind, path in files.items():
            raw = Path(path).read_bytes()
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
            text = raw.decode("utf-8", errors="replace")
            if kind == "wiiu_xml":
                discs = parse_wiiu_xml(raw)
                conn.executemany("INSERT OR REPLACE INTO names VALUES (?,?,?)", [("wiiu_disc", k, v) for k, v in discs.items()])
                counts[kind] = len(discs)
                continue
            if kind == "ps2":
                from .pcsx2 import parse_game_index
                names = parse_game_index(text)
            else:
                names = parse(text)
            conn.executemany("INSERT OR REPLACE INTO names VALUES (?,?,?)", [(kind, k, v) for k, v in names.items()])
            counts[kind] = len(names)
        conn.executemany("INSERT OR REPLACE INTO info VALUES (?,?)", [("fetched", time.strftime("%Y-%m-%d %H:%M")), ("fetched_at", str(time.time()))])
        conn.commit()
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp, out)
    return counts


def build_dat(db: Optional[Path] = None, out_dir: Optional[Path] = None) -> int:
    """Write the Wii U "DAT": every disc game of GameTDB's list as ``Title (Region) (Languages)`` with no checksum (a Wii U image cannot be hashed
    as Redump would; the games are told by the product code, see ``discmatch``). Returns the number of games (0: no list yet)."""
    from xml.sax.saxutils import quoteattr
    from . import paths
    names = GameTdb(db)
    try:
        rows = names.all("wiiu_disc") if names.available else []
    finally:
        names.close()
    if not rows:
        return 0
    seen: set = set()
    games = []
    for gid, label in sorted(rows, key=lambda r: (r[1].casefold(), r[0])):
        name = label if label not in seen else f"{label} [{gid}]"
        seen.add(name)
        games.append(f"<game name={quoteattr(name)}><category>Games</category><description>{name.replace('&', '&amp;').replace('<', '&lt;')}</description>"
                     f"<rom name={quoteattr(name + '.wux')} size=\"0\"/></game>")
    out_dir = Path(out_dir) if out_dir else paths.redump_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    version = time.strftime("%Y-%m-%d %H-%M-%S")
    tmp = out_dir / f"{WIIU_DAT}.dat.part"
    tmp.write_text(f'<?xml version="1.0"?>\n<datafile><header><name>{WIIU_DAT}</name><description>{WIIU_DAT} - made from GameTDB</description>'
                   f"<version>{version}</version><author>GameTDB</author></header>" + "".join(games) + "</datafile>", encoding="utf-8")
    os.replace(tmp, out_dir / f"{WIIU_DAT}.dat")
    return len(games)


def check_update(timeout: float = 10, gate: bool = True, path: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """``missing`` (fetch), ``up_to_date`` (younger than :data:`MAX_AGE_DAYS`), or ``update_available`` (older: fetch again; the lists
    are small and GameTDB gives no cheap way to ask if they changed)."""
    info = db_info(path)
    if not info["available"]:
        return {"status": "missing"}
    from . import paths
    if path is None and not (paths.redump_dir() / f"{WIIU_DAT}.dat").is_file():
        return {"status": "update_available"}                    # (the Wii U DAT is made when the list is fetched)
    if gate and (now if now is not None else time.time()) - float(info.get("fetched_at") or 0) < MAX_AGE_DAYS * 86400:
        return {"status": "up_to_date", "skipped": True}
    return {"status": "update_available"}


def download_and_build(progress: Optional[ProgressFn] = None, cancel: Any = None, out: Optional[Path] = None) -> Dict[str, int]:
    out = Path(out) if out else db_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="gametdb-", dir=out.parent))
    files: Dict[str, Path] = {}
    try:
        for kind, url in URLS.items():
            if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
                raise InterruptedError("cancelled")
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            meter.add(len(data))
            if progress:
                progress(len(data), len(data), f"GameTDB: {kind} titles")
            files[kind] = work / f"{kind}.txt"
            files[kind].write_bytes(data)
        counts = build(files, out)
        try:
            counts["wiiu_dat"] = build_dat(out)
        except OSError:
            pass
        return counts
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
