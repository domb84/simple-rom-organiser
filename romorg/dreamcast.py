"""Sega Dreamcast (Redump DAT): CHD identification, raw sets, tidy, Build library, Convert to CHD.

Unlike the file based systems a Dreamcast *game* is a folder::

    <root>/<Redump name>/<Redump name>.chd           (+ sidecars: .zip .md5 .gdi .cue .state .sav ...)

Identification
    A CHD is read with :mod:`romorg.chd` (header + metadata give the track layout, no hashing needed to
    reject most files): the Redump games whose ``(Track N).bin`` sizes equal the CHD's track sizes are the
    candidates, then every DATA track is hashed (crc32 / md5 / sha1, streamed) and compared. Audio tracks
    are compared by length only -> level ``identified``. When every track including audio was decoded and
    equals Redump (chdman ``extractcd`` or the pure-Python reader) the level is ``verified``. Hashes are
    cached per file in the sqlite hash cache (key: path, size, mtime_ns and the CHD header sha1), so a rescan
    of an unchanged CHD never decodes it again. The ``.cue`` entries of the DAT are ignored.

Units
    A *unit* is what travels together: a CHD with the sidecar files that start with its file stem; when the CHD
    is alone in its folder the whole folder (and everything inside) is the unit. A raw Redump set (a ``.gdi`` /
    ``.cue`` with one file per track) is matched per track (level ``raw``) and can be converted to CHD
    (chdman) - never touched otherwise.

Organise / Build library
    Plans are lists of game-level :class:`DcOp` (one per folder / CHD) whose ``moves`` are the file moves;
    :func:`apply_plan` expands them and hands them to ``organiser.apply_renames`` (journalled, undoable,
    never overwrites). Nothing is ever deleted; save states and other sidecars always travel with their CHD.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import threading
from concurrent.futures import ThreadPoolExecutor

from . import chd as chdlib
from . import chdpool, chdtool, folders, organiser, scanner, tags
from .datfile import DatFile, Rom
from .folders import CONVERTED_DIR, DUPLICATES_DIR, EXCLUDED_DIR, SUPERSEDED_DIR, UNMATCHED_DIR
from .organiser import RenameOp, safe_filename

DAT_NAME = "Sega - Dreamcast"
PLATFORM_NAME = "Sega Dreamcast"
CHD_EXT = ".chd"
SHEET_EXTS = (".gdi", ".cue")
LEVEL_VERIFIED, LEVEL_IDENTIFIED, LEVEL_RAW = "verified", "identified", "raw"

ProgressFn = Callable[[int, int, str], None]
_TRACK_RE = re.compile(r"\(Track (\d+)\)\.bin$", re.IGNORECASE)
_MIB = 1 << 20


# --------------------------------------------------------------------------- the Redump index

@dataclass
class DcGame:
    """One Redump game (= one disc): its ``(Track N).bin`` roms in track order (the ``.cue`` is ignored)."""
    name: str
    category: str
    tracks: list[Rom]
    cue: Optional[Rom] = None

    @property
    def sizes(self) -> tuple[int, ...]:
        return tuple(r.size for r in self.tracks)

    @property
    def rep(self) -> Rom:
        """A stand-in Rom named like the game (what the library rules / missing list work with)."""
        return Rom(name=self.name, size=sum(self.sizes), crc="", md5="", sha1="", game=self.name,
                   dat=self.tracks[0].dat if self.tracks else DAT_NAME, set_name=self.name,
                   category=self.category)


class DcIndex:
    """Redump games by name and by their track-size tuple."""

    def __init__(self, dat: DatFile) -> None:
        self.dat = dat
        self.games: dict[str, DcGame] = {}
        by_game: dict[str, list[Rom]] = {}
        for r in dat.roms:
            by_game.setdefault(r.game, []).append(r)
        for name, roms in by_game.items():
            tracks = []
            cue = None
            for r in roms:
                m = _TRACK_RE.search(r.name)
                if m:
                    tracks.append((int(m.group(1)), r))
                elif r.name.lower().endswith(".cue"):
                    cue = r
            tracks.sort(key=lambda t: t[0])
            if tracks:
                self.games[name] = DcGame(name, roms[0].category, [r for _n, r in tracks], cue)
        self.by_sizes: dict[tuple[int, ...], list[DcGame]] = {}
        for g in self.games.values():
            self.by_sizes.setdefault(g.sizes, []).append(g)
        self._disc_total: Optional[dict[str, int]] = None

    def disc_totals(self) -> dict[str, int]:
        """Game name -> number of discs of its release in the DAT (largest disc number among the same
        title / region / languages, any revision); 0 for single-disc games."""
        if self._disc_total is None:
            groups: dict[tuple, list[tuple[str, int]]] = {}
            for g in self.games.values():
                t = tags.redump_tags(g.name, g.category)
                n = tags.disc_number(t)
                key = (tags.game_key(t), t.regions, () if t.languages_implied else t.languages)
                groups.setdefault(key, []).append((g.name, n))
            out: dict[str, int] = {}
            for members in groups.values():
                top = max(n for _name, n in members)
                for name, n in members:
                    out[name] = top if n else 0
            self._disc_total = out
        return self._disc_total


def get_index(dat: DatFile) -> DcIndex:
    idx = getattr(dat, "_dc_index", None)
    if idx is None or idx.dat is not dat or len(idx.dat.roms) != len(dat.roms):
        idx = DcIndex(dat)
        try:
            setattr(dat, "_dc_index", idx)
        except AttributeError:  # pragma: no cover - slotted stand-ins
            pass
    return idx


# --------------------------------------------------------------------------- cache

class ChdCache:
    """Per-file track hashes in the shared ``hashes.sqlite`` (table ``chd_hashes``); degrades to a no-op."""

    def __init__(self, db_path: Optional[Path]) -> None:
        self.conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        if db_path is None:
            return
        try:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chd_hashes (path TEXT NOT NULL, size INTEGER NOT NULL,"
                " mtime_ns INTEGER NOT NULL, sha1 TEXT NOT NULL, kind TEXT NOT NULL, tracks TEXT NOT NULL,"
                " level TEXT NOT NULL, via TEXT NOT NULL, PRIMARY KEY (path, size, mtime_ns, sha1))")
            conn.commit()
            self.conn = conn
        except (sqlite3.Error, OSError):
            self.conn = None

    def get(self, path: str, size: int, mtime_ns: int, sha1: str) -> Optional[dict]:
        if self.conn is None:
            return None
        try:
            with self._lock:
                row = self.conn.execute(
                    "SELECT tracks, level, via FROM chd_hashes WHERE path=? AND size=? AND mtime_ns=? AND sha1=?",
                    (path, size, mtime_ns, sha1)).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        try:
            return {"tracks": json.loads(row[0]), "level": row[1], "via": row[2]}
        except ValueError:
            return None

    def put(self, path: str, size: int, mtime_ns: int, sha1: str, kind: str, tracks: list[dict],
            level: str, via: str) -> None:
        if self.conn is None:
            return
        try:
            with self._lock:
                self.conn.execute("DELETE FROM chd_hashes WHERE path=?", (path,))
                self.conn.execute("INSERT OR REPLACE INTO chd_hashes VALUES (?,?,?,?,?,?,?,?)",
                                  (path, size, mtime_ns, sha1, kind, json.dumps(tracks), level, via))
                self.conn.commit()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.commit()
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None


# --------------------------------------------------------------------------- units

@dataclass
class DcUnit:
    kind: str                       # "chd" | "raw"
    path: Path                      # the CHD file / the .gdi or .cue sheet
    folder: Optional[Path]          # the game folder when the unit owns its folder, else None
    files: list[Path]               # every file that travels with the unit (``path`` included)
    stem: str                       # file stem of ``path``
    game: Optional[DcGame] = None
    level: str = ""                 # verified | identified | raw | "" (unmatched)
    reason: str = ""                # why it did not match / what is wrong
    tracks: list[dict] = field(default_factory=list)   # per track: number, type, size, crc32, md5, sha1 (None = not hashed)
    sha1: str = ""                  # CHD header sha1
    size: int = 0                   # CHD file size
    mtime_ns: int = 0
    via: str = ""                   # chdman | python | files
    candidates: list[str] = field(default_factory=list)
    gd: bool = True

    @property
    def top(self) -> Path:
        return self.folder if self.folder is not None else self.path

    def total_bytes(self) -> int:
        return sum(int(t.get("size") or 0) for t in self.tracks)


@dataclass
class DcEntry(scanner.Entry):
    reason: str = ""
    kind: str = ""                  # chd | raw | file


@dataclass
class DcMatch(scanner.Match):
    unit: Any = None
    level: str = ""
    kind: str = "chd"

    def item(self, state: Any) -> dict[str, Any]:
        """The row of the Matched tab."""
        u: DcUnit = self.unit
        root = Path(state.root)
        g = u.game
        canonical = is_canonical(u, root)
        t = tags.redump_tags(g.name, g.category) if g else None
        return {
            "file": self.entry.rel, "size": self.entry.size, "crc": "",
            "dat": g and g.rep.dat or DAT_NAME, "roms": [g.name] if g else [],
            "game": g.name if g else "", "set_name": g.name if g else "", "other_dats": [],
            "named_ok": canonical, "placed_ok": canonical, "via": self.kind, "header": 0, "byte_order": "",
            "tags": tags.to_json(t) if t else None,
            "level": self.level, "kind": self.kind, "engine": u.via, "folder": u.folder is not None,
            "tracks": [{"number": x.get("number"), "type": x.get("type"), "size": x.get("size"),
                        "hashed": bool(x.get("sha1"))} for x in u.tracks],
        }


def _is_hidden_junk(name: str) -> bool:
    return name.startswith(scanner.UNDO_LOG_PREFIX) or scanner.TEMP_MARKER in name or name.startswith(chdtool.TEMP_PREFIX)


def list_tree(folder: Path, skip_dirs: Iterable[Path] = ()) -> list[Path]:
    """Every file under ``folder`` (hidden files included; our own playlists / temp files / undo logs and
    sub-folders in ``skip_dirs`` left out)."""
    from . import m3u
    skip = {os.fspath(d) for d in skip_dirs}
    out: list[Path] = []
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for de in it:
                    if _is_hidden_junk(de.name):
                        continue
                    p = Path(de.path)
                    try:
                        if de.is_dir(follow_symlinks=False):
                            if de.path not in skip:
                                stack.append(p)
                        elif de.name.lower().endswith(".m3u") and m3u.read_own_m3u_text(p) is not None:
                            continue    # our own playlist: Build library rewrites it
                        else:
                            out.append(p)
                    except OSError:
                        continue
        except OSError:
            continue
    out.sort(key=lambda p: p.as_posix().lower())
    return out


def _sidecars(directory_files: list[Path], stem: str, stems: Sequence[str]) -> list[Path]:
    """Files of one directory that start with ``stem`` and are not claimed by a longer CHD stem."""
    out = []
    longer = [s for s in stems if len(s) > len(stem) and s.startswith(stem)]
    for f in directory_files:
        n = f.name
        if n.startswith(stem) and f.suffix.lower() != CHD_EXT or n == stem + CHD_EXT:
            if any(n.startswith(s) for s in longer):
                continue
            out.append(f)
    return out


def _parts(path: Path, root: Path) -> tuple[str, ...]:
    return scanner._rel_parts(path, root)


def _is_container(directory: Path, root: Path) -> bool:
    """The platform folder itself or one of the reserved folders (never a game folder)."""
    if directory == root:
        return True
    parts = _parts(directory, root)
    return len(parts) == 1 and folders.canonical_name(parts[0]) is not None


def discover_units(root: Path, files: list[Path]) -> tuple[list[DcUnit], list[Path]]:
    """Group ``files`` (from ``scanner.collect_files``) into CHD / raw-set units; returns ``(units, rest)``."""
    by_dir: dict[Path, list[Path]] = {}
    for f in files:
        by_dir.setdefault(f.parent, []).append(f)
    chd_dirs = {d for d, fs in by_dir.items() if any(f.suffix.lower() == CHD_EXT for f in fs)}
    units: list[DcUnit] = []
    claimed: set[str] = set()

    def nested_skip(d: Path) -> list[Path]:
        return [x for x in chd_dirs if x != d and d in x.parents]

    for d in sorted(chd_dirs, key=lambda p: str(p).lower()):
        fs = by_dir[d]
        chds = [f for f in fs if f.suffix.lower() == CHD_EXT]
        whole = len(chds) == 1 and not _is_container(d, root)
        if whole:
            tree = list_tree(d, nested_skip(d))
            units.append(DcUnit("chd", chds[0], d, tree, chds[0].stem))
            claimed.update(os.fspath(p) for p in tree)
        else:
            stems = [c.stem for c in chds]
            for c in chds:
                side = _sidecars(fs, c.stem, stems)
                if c not in side:
                    side.insert(0, c)
                units.append(DcUnit("chd", c, None, side, c.stem))
                claimed.update(os.fspath(p) for p in side)
    # raw sets: a .gdi (preferred) / .cue sheet in a folder without a CHD
    sheet_dirs = {d for d, fs in by_dir.items() if d not in chd_dirs
                  and any(f.suffix.lower() in SHEET_EXTS for f in fs)}
    for d in sorted(sheet_dirs, key=lambda p: str(p).lower()):
        fs = [f for f in by_dir[d] if os.fspath(f) not in claimed]
        sheets = [f for f in fs if f.suffix.lower() == ".gdi"] or [f for f in fs if f.suffix.lower() == ".cue"]
        if not sheets:
            continue
        if len(sheets) == 1 and not _is_container(d, root):
            tree = [p for p in list_tree(d, nested_skip(d)) if os.fspath(p) not in claimed]
            units.append(DcUnit("raw", sheets[0], d, tree, sheets[0].stem))
            claimed.update(os.fspath(p) for p in tree)
        else:
            stems = [s.stem for s in sheets]
            for s in sheets:
                side = _sidecars(fs, s.stem, stems)
                side = [p for p in side if p.suffix.lower() != CHD_EXT]
                if s not in side:
                    side.insert(0, s)
                refs = [r for r in sheet_track_files(s) or [] if r.exists()]
                for r in refs:
                    if r not in side:
                        side.append(r)
                units.append(DcUnit("raw", s, None, side, s.stem))
                claimed.update(os.fspath(p) for p in side)
    rest = [f for f in files if os.fspath(f) not in claimed]
    return units, rest


def sheet_track_files(sheet: Path) -> Optional[list[Path]]:
    """The track files of a ``.gdi`` / ``.cue`` in track order (None when they cannot be listed per track)."""
    try:
        text = Path(sheet).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    base = Path(sheet).parent
    out: list[Path] = []
    if sheet.suffix.lower() == ".gdi":
        lines = [ln for ln in text.splitlines() if ln.strip()]
        try:
            count = int(lines[0].strip())
            for ln in lines[1:]:
                parts = shlex.split(ln)
                out.append(base / parts[4])
        except (ValueError, IndexError):
            return None
        return out if len(out) == count else None
    files = 0
    tracks = 0
    for ln in text.splitlines():
        s = ln.strip()
        if s.upper().startswith("FILE "):
            files += 1
            try:
                name = shlex.split(s)[1]
            except (ValueError, IndexError):
                return None
            out.append(base / name)
        elif s.upper().startswith("TRACK "):
            tracks += 1
    if not out or files != tracks:     # one bin holding several tracks: cannot be matched per file
        return None
    return out


# --------------------------------------------------------------------------- identification

def _track_meta(info: chdlib.Chd) -> list[dict]:
    return [{"number": t.number, "type": t.type, "size": t.size, "audio": t.is_audio,
             "crc32": None, "md5": None, "sha1": None} for t in info.tracks]


def _merge_cached(meta: list[dict], cached: Optional[dict]) -> None:
    if not cached:
        return
    by_num = {c.get("number"): c for c in cached.get("tracks", [])}
    for m in meta:
        c = by_num.get(m["number"])
        if c and c.get("sha1") and c.get("size") == m["size"]:
            m["crc32"], m["md5"], m["sha1"] = c.get("crc32"), c.get("md5"), c.get("sha1")


class _Progress:
    """Bytes-based progress shared by a scan / verify job (reported in MiB)."""

    def __init__(self, report: Optional[ProgressFn], total: int, cancel: Any) -> None:
        self.report, self.total, self.done = report, total, 0
        self.cancel = cancel
        self.label = ""
        self._last = 0.0

    def check(self) -> None:
        if self.cancel is not None and (self.cancel.is_set() if hasattr(self.cancel, "is_set") else self.cancel()):
            raise scanner.ScanCancelled()

    def add(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if now - self._last > 0.25:
            self._last = now
            self.emit()

    def emit(self, label: Optional[str] = None) -> None:
        if label is not None:
            self.label = label
        if self.report:
            self.report(min(self.done, self.total) // _MIB, self.total // _MIB, self.label)

    def cancelled(self) -> bool:
        return self.cancel is not None and bool(self.cancel.is_set() if hasattr(self.cancel, "is_set") else self.cancel())


def hash_tracks_python(info: chdlib.Chd, wanted: Iterable[int], prog: Optional[_Progress] = None,
                       pool: Optional[chdpool.HashPool] = None) -> dict[int, dict]:
    """crc32 / md5 / sha1 of the tracks with these 0-based indexes, decoded by the pure-Python reader.

    With a ``pool`` the tracks are hashed by worker processes (in parallel); if the pool fails the rest is
    done in-process."""
    wanted = list(wanted)
    out: dict[int, dict] = {}
    cancel = prog.cancelled if prog else None
    if pool is not None and not pool.broken:
        def one(i: int) -> tuple[int, Optional[dict]]:
            try:
                return i, pool.hash_track(info.path, i, progress=(prog.add if prog else None), cancel=cancel)
            except chdpool.Cancelled:
                raise scanner.ScanCancelled() from None
            except chdpool.PoolError:
                return i, None
        order = sorted(wanted, key=lambda i: -info.tracks[i].size)       # the big ones first
        if len(order) > 1:
            with ThreadPoolExecutor(max_workers=min(len(order), pool.workers)) as ex:
                results = list(ex.map(one, order))
        else:
            results = [one(i) for i in order]
        for i, h in results:
            if h is not None:
                out[i] = {"crc32": h["crc32"], "md5": h["md5"], "sha1": h["sha1"]}
    for i in wanted:
        if i in out:
            continue
        tr = info.tracks[i]
        h = chdlib.hash_track(info, tr, progress=(prog.add if prog else None), cancel=cancel)
        out[i] = {"crc32": h.crc32, "md5": h.md5, "sha1": h.sha1}
    return out


def hash_all_chdman(path: Path, info: chdlib.Chd, chdman: chdtool.Chdman, root: Path,
                    prog: Optional[_Progress] = None) -> dict[int, dict]:
    """Every track via ``chdman extractcd`` into a temp folder next to the ROMs (deleted afterwards)."""
    need = sum(t.size for t in info.tracks)
    chdtool.check_space(root, need)
    work = chdtool.make_workdir(root)
    try:
        ex = chdtool.extract_cd(chdman, path, work, "gdrom" if info.is_gd else "cd",
                                [t.size for t in info.tracks],
                                progress=(lambda d, t, m: prog.emit(f"{Path(path).name}: {m}")) if prog else None,
                                cancel=(prog.cancel if prog else None))
        out: dict[int, dict] = {}
        if len(ex.tracks) != len(info.tracks) or any(et.size != t.size for et, t in zip(ex.tracks, info.tracks)):
            raise chdtool.ChdmanError("chdman wrote other track sizes than the CHD's metadata says")
        for i, et in enumerate(ex.tracks):
            crc, md5, sha1 = chdtool.hash_range(et.path, et.offset, et.size,
                                                progress=(prog.add if prog else None),
                                                cancel=(prog.cancel if prog else None))
            out[i] = {"crc32": crc, "md5": md5, "sha1": sha1, "size": et.size}
        return out
    except chdtool.ChdmanError as exc:
        if exc.cancelled:
            raise scanner.ScanCancelled() from exc
        raise
    finally:
        chdtool.remove_workdir(work)


def _rom_ok(rom: Rom, tr: dict) -> bool:
    return bool(tr.get("sha1")) and tr["sha1"] == rom.sha1 and (not rom.crc or tr.get("crc32") == rom.crc) \
        and (not rom.md5 or tr.get("md5") == rom.md5)


def match_chd(index: DcIndex, meta: list[dict], hasher: Callable[[list[int]], None]) -> tuple[list[DcGame], str, str]:
    """Candidates by track sizes, then by the hashes of the data tracks (smallest first), then audio if needed.

    ``hasher(indexes)`` fills ``meta[i]["crc32" / "md5" / "sha1"]`` for the 0-based track indexes it is given.
    Returns ``(matching games, level, reason)``; ``level`` is ``verified`` only when every track is hashed
    and equal, else ``identified`` (audio by length) or ``""``.
    """
    sizes = tuple(m["size"] for m in meta)
    cands = list(index.by_sizes.get(sizes, ()))
    if not cands:
        return [], "", "no Redump game has these track sizes"
    data = sorted((i for i, m in enumerate(meta) if not m["audio"]), key=lambda i: meta[i]["size"])
    order = data or sorted(range(len(meta)), key=lambda i: meta[i]["size"])
    alive = cands
    for i in order:
        if not meta[i]["sha1"]:
            hasher([i])
        alive = [g for g in alive if _rom_ok(g.tracks[i], meta[i])]
        if not alive:
            return [], "", "the data tracks do not match any Redump game with this track layout"
    if len(alive) > 1:     # same sizes and data: only the audio can tell them apart
        audio = [i for i in range(len(meta)) if i not in order]
        todo = [i for i in audio if not meta[i]["sha1"]]
        if todo:
            hasher(todo)
        alive = [g for g in alive if all(_rom_ok(g.tracks[i], meta[i]) for i in audio)]
        if not alive:
            return [], "", "the audio tracks do not match any Redump game with these data tracks"
    g = alive[0]
    hashed = [bool(m["sha1"]) for m in meta]
    if all(hashed):
        if not all(_rom_ok(g.tracks[i], meta[i]) for i in range(len(meta))):
            bad = [str(meta[i]["number"]) for i in range(len(meta)) if not _rom_ok(g.tracks[i], meta[i])]
            return [], "", f"track {', '.join(bad)} differs from Redump (data matches {g.name})"
        return alive, LEVEL_VERIFIED, ""
    return alive, LEVEL_IDENTIFIED, ""


def identify_unit(unit: DcUnit, index: DcIndex, cache: ChdCache, root: Path, chdman: Optional[chdtool.Chdman],
                  engine: str, prog: Optional[_Progress], pool: Optional[chdpool.HashPool] = None) -> None:
    """Fill ``unit.game`` / ``level`` / ``tracks`` / ``reason`` for a CHD unit (cache first)."""
    path = unit.path
    try:
        st = path.stat()
        unit.size, unit.mtime_ns = st.st_size, st.st_mtime_ns
        info = chdlib.Chd(path, load_map=False)
    except (OSError, chdlib.ChdError) as exc:
        unit.reason = f"not a readable CHD: {exc}"
        return
    try:
        if not info.is_cd:
            unit.reason = "a CHD that is not a CD / GD-ROM image"
            return
        unit.sha1, unit.gd = info.sha1, info.is_gd
        meta = _track_meta(info)
        key = str(path)
        _merge_cached(meta, cache.get(key, unit.size, unit.mtime_ns, info.sha1))
        unit.tracks = meta
        use_chdman = chdman is not None and engine in ("auto", "chdman")
        via = ["python"]

        def hasher(idx: list[int]) -> None:
            nonlocal use_chdman
            if prog:
                prog.check()
                prog.emit(f"Reading {path.name}")
            if use_chdman and not all(meta[i]["sha1"] for i in range(len(meta))):
                try:
                    got = hash_all_chdman(path, info, chdman, root, prog)
                    for i, h in got.items():
                        meta[i].update(crc32=h["crc32"], md5=h["md5"], sha1=h["sha1"])
                    via[0] = "chdman"
                    return
                except chdtool.ChdmanError:
                    use_chdman = False      # no space / chdman problem: the Python reader does it
            got = hash_tracks_python(info, [i for i in idx if not meta[i]["sha1"]], prog, pool)
            for i, h in got.items():
                meta[i].update(h)

        games, level, reason = match_chd(index, meta, hasher)
        unit.via = via[0] if any(m["sha1"] for m in meta) else ""
        if games:
            unit.game, unit.level, unit.reason = games[0], level, ""
            unit.candidates = [g.name for g in games]
        else:
            unit.reason = reason
        cached_level = LEVEL_VERIFIED if all(m["sha1"] for m in meta) else LEVEL_IDENTIFIED
        if any(m["sha1"] for m in meta):
            cache.put(key, unit.size, unit.mtime_ns, info.sha1, "gdrom" if info.is_gd else "cd",
                      [{k: m[k] for k in ("number", "type", "size", "crc32", "md5", "sha1")} for m in meta],
                      cached_level, unit.via or "python")
    except chdlib.ChdError as exc:
        unit.reason = f"cannot decode this CHD: {exc}"
    finally:
        info.close()


def identify_raw(unit: DcUnit, index: DcIndex, hasher_cache: scanner.HashCache,
                 prog: Optional[_Progress]) -> None:
    """Match a raw Redump set (a sheet with one file per track) track by track."""
    files = sheet_track_files(unit.path)
    if not files:
        unit.reason = "a disc image sheet that does not list one file per track (cannot be matched)"
        return
    if not all(f.is_file() for f in files):
        unit.reason = "track files are missing: " + ", ".join(f.name for f in files if not f.is_file())[:120]
        return
    sizes = tuple(f.stat().st_size for f in files)
    unit.tracks = [{"number": i + 1, "type": "", "size": s, "crc32": None, "md5": None, "sha1": None}
                   for i, s in enumerate(sizes)]
    cands = list(index.by_sizes.get(sizes, ()))
    if not cands:
        unit.reason = "no Redump game has these track sizes"
        return
    hashes: dict[int, tuple[str, str]] = {}

    def h(i: int) -> tuple[str, str]:
        if i not in hashes:
            st = files[i].stat()
            key = scanner._cache_key(files[i])
            got = hasher_cache.get(key, st.st_size, st.st_mtime_ns) if key else None
            if got is None:
                if prog:
                    prog.check()
                    prog.emit(f"Hashing {files[i].name}")
                got = _hash_file_progress(files[i], prog)
                if key:
                    hasher_cache.put(key, st.st_size, st.st_mtime_ns, got[0], got[1])
            hashes[i] = got
            unit.tracks[i]["crc32"], unit.tracks[i]["sha1"] = got
        return hashes[i]

    alive = cands
    for i in sorted(range(len(files)), key=lambda k: sizes[k]):
        crc, sha1 = h(i)
        alive = [g for g in alive if g.tracks[i].sha1 == sha1 and (not g.tracks[i].crc or g.tracks[i].crc == crc)]
        if not alive:
            unit.reason = "the track files do not match any Redump game (a different dump?)"
            return
    unit.game, unit.level, unit.via, unit.candidates = alive[0], LEVEL_RAW, "files", [g.name for g in alive]


def _hash_file_progress(path: Path, prog: Optional[_Progress]) -> tuple[str, str]:
    crc = 0
    sha = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            block = f.read(scanner.CHUNK_SIZE)
            if not block:
                break
            crc = zlib.crc32(block, crc)
            sha.update(block)
            if prog:
                prog.check()
                prog.add(len(block))
    return f"{crc & 0xFFFFFFFF:08x}", sha.hexdigest()


# --------------------------------------------------------------------------- scan result

def is_canonical(unit: DcUnit, root: Path) -> bool:
    """The unit already sits where Build library / Organise would put it (``<root>/<name>/<name>.chd``)."""
    g = unit.game
    if g is None or unit.kind != "chd":
        return False
    return unit_target(unit, root) == {f: f for f in unit.files}


@dataclass
class DcScanResult(scanner.ScanResult):
    units: list[DcUnit] = field(default_factory=list)
    index: Optional[DcIndex] = field(default=None, repr=False, compare=False)
    junk: list[Path] = field(default_factory=list)
    originals: list[DcUnit] = field(default_factory=list)      # raw sets kept by Convert (_converted_originals/)
    engine: str = "python"
    chdman_label: str = ""
    swept: list[str] = field(default_factory=list)

    def game_levels(self) -> dict[str, tuple[str, str]]:
        """Game name -> (level, kind) of the best unit that has it (verified > identified > raw)."""
        order = {LEVEL_VERIFIED: 0, LEVEL_IDENTIFIED: 1, LEVEL_RAW: 2}
        out: dict[str, tuple[str, str]] = {}
        for m in self.matched:
            name = m.unit.game.name
            if name not in out or order.get(m.level, 9) < order.get(out[name][0], 9):
                out[name] = (m.level, m.kind)
        return out

    def summary(self) -> dict[str, Any]:
        root = Path(self.root)
        chd_m = [m for m in self.matched if m.kind == "chd"]
        raw_m = [m for m in self.matched if m.kind == "raw"]
        active = [m for m in chd_m if not scanner.is_converted_original(m.entry.path, root)]
        by_game: dict[str, int] = {}
        for m in chd_m:
            by_game[m.unit.game.name] = by_game.get(m.unit.game.name, 0) + 1
        dup = sum(n - 1 for n in by_game.values() if n > 1)
        aside = 0
        for m in chd_m:
            if scanner.is_set_aside_duplicate(m.entry.path, root):
                aside += 1
        placed = sum(1 for m in active if is_canonical(m.unit, root))
        total = len(self.index.games) if self.index else self.dat_total
        have_names = {m.unit.game.name for m in self.matched}
        n_ident = sum(1 for m in chd_m if m.level == LEVEL_IDENTIFIED)
        n_ver = sum(1 for m in chd_m if m.level == LEVEL_VERIFIED)
        dat = self.dat_names[0] if self.dat_names else DAT_NAME
        per = {"dat_total": total, "have": len(have_names), "missing": total - len(have_names),
               "matched_files": len(self.matched), "correctly_placed": placed,
               "games_total": total, "games_have": len(have_names), "games_missing": total - len(have_names),
               "count_by": "game"}
        return {
            "dat_total": total, "have": len(have_names), "missing": total - len(have_names),
            "matched_files": len(self.matched), "unmatched_files": len(self.unmatched),
            "duplicates": max(0, dup - aside), "duplicates_set_aside": aside,
            "duplicate_groups": sum(1 for n in by_game.values() if n > 1),
            "bad_dump_files": 0, "unsupported": 0, "errors": len(self.errors),
            "correctly_named": placed, "to_rename": len(active) - placed,
            "correctly_placed": placed, "per_dat": {dat: per},
            "games_total": total, "games_have": len(have_names), "games_missing": total - len(have_names),
            "count_by": "game", "matched_via": {"raw": len(raw_m), "headerless": 0, "byteswapped": 0},
            "convertible": len(raw_m), "converted_originals": len(self.originals),
            # Dreamcast specifics
            "chd_files": len(chd_m) + sum(1 for e in self.unmatched if getattr(e, "kind", "") == "chd"),
            "identified": n_ident, "verified": n_ver, "raw": len(raw_m),
            "engine": self.engine, "chdman": self.chdman_label,
        }


def scan(root, dats, progress: Optional[ProgressFn] = None, cancel: Any = None,
         cache_path: Optional[Path] = None, use_cache: bool = True, chdman: Optional[chdtool.Chdman] = None,
         engine: str = "auto", protected_dirs: Sequence[str] = (), workers: int = 1,
         **_ignored: Any) -> DcScanResult:
    """Scan ``root`` for CHDs and raw sets and match them to the Redump DAT (``dats``: one DatFile or a list)."""
    root = Path(root).expanduser().absolute()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    dat_list = [dats] if isinstance(dats, DatFile) else list(dats)
    dat = dat_list[0]
    index = get_index(dat)
    swept = chdtool.sweep_stale(root)
    problems: list[tuple[Path, str]] = []
    files = scanner.collect_files(root, True, problems, protected_dirs)
    units, rest = discover_units(root, files)
    cache = ChdCache((cache_path or scanner.default_cache_path()) if use_cache else None)
    fcache = scanner.HashCache((cache_path or scanner.default_cache_path()) if use_cache else None)
    use_chdman = chdman is not None and engine != "python"

    try:
        # progress total: what has to be decoded / hashed (cached CHDs cost nothing)
        total = 0
        for u in units:
            try:
                if u.kind == "chd":
                    info = chdlib.Chd(u.path, load_map=False)
                    try:
                        st = u.path.stat()
                        cached = cache.get(str(u.path), st.st_size, st.st_mtime_ns, info.sha1)
                        full = all(t.get("sha1") for t in (cached or {}).get("tracks", [{}])) and bool(cached)
                        if not cached:
                            total += sum(t.size for t in info.tracks if use_chdman or not t.is_audio) * (2 if use_chdman else 1)
                        elif use_chdman and not full:
                            total += sum(t.size for t in info.tracks)
                    finally:
                        info.close()
                else:
                    total += sum(f.stat().st_size for f in (sheet_track_files(u.path) or []) if f.exists())
            except (OSError, chdlib.ChdError):
                pass
        prog = _Progress(progress, max(total, 1), cancel)
        prog.emit("Scanning...")
        pool = chdpool.make_pool(workers) if (workers > 1 and not use_chdman) else None
        try:
            chd_units = [u for u in units if u.kind == "chd"]
            if pool is not None and chd_units:
                def work(u: DcUnit) -> None:
                    prog.check()
                    prog.emit(f"Checking {u.path.name}")
                    identify_unit(u, index, cache, root, None, engine, prog, pool)
                with ThreadPoolExecutor(max_workers=min(workers, len(chd_units))) as ex:
                    for fut in [ex.submit(work, u) for u in chd_units]:
                        fut.result()
            else:
                for u in chd_units:
                    prog.check()
                    prog.emit(f"Checking {u.path.name}")
                    identify_unit(u, index, cache, root, chdman if use_chdman else None, engine, prog, pool)
            for u in units:
                if u.kind != "chd":
                    prog.check()
                    prog.emit(f"Checking {u.path.name}")
                    identify_raw(u, index, fcache, prog)
        finally:
            if pool is not None:
                pool.close()

    finally:
        fcache.close()
        cache.close()

    matched: list[DcMatch] = []
    unmatched: list[DcEntry] = []
    originals = [u for u in units if folders.is_converted(_parts(u.path, root))]
    units = [u for u in units if u not in originals]
    rest = [f for f in rest if not folders.is_converted(_parts(f, root))]
    for u in units:
        try:
            size = u.path.stat().st_size
        except OSError:
            size = 0
        if u.game is not None:
            e = DcEntry(u.path, None, size, "", u.sha1 or None, root, kind=u.kind)
            matched.append(DcMatch(e, [r for r in u.game.tracks] + ([u.game.cue] if u.game.cue else []),
                                   unit=u, level=u.level, kind=u.kind))
        else:
            unmatched.append(DcEntry(u.path, None, size, "", u.sha1 or None, root, reason=u.reason, kind=u.kind))
    for f in rest:
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        unmatched.append(DcEntry(f, None, size, "", None, root, reason="not part of a Dreamcast game", kind="file"))
    have = {m.unit.game.name for m in matched}
    missing = [g.rep for g in index.games.values() if g.name not in have]
    total_games = len(index.games)
    return DcScanResult(
        root=root, dat_names=[dat.name], matched=matched, unmatched=unmatched, unsupported=[],
        errors=list(problems), missing=missing, dat_total=total_games, dat_totals={dat.name: total_games},
        layout="game_folder", dats=dat_list, units=units, index=index, junk=list(rest), originals=originals,
        engine="chdman" if use_chdman else "python", chdman_label=chdman.label if chdman else "", swept=swept)


# --------------------------------------------------------------------------- targets

def _rename_in(name: str, old_stem: str, new_stem: str) -> str:
    """``old_stem + rest`` -> ``new_stem + rest`` (names that do not start with the stem are unchanged)."""
    if name.startswith(old_stem):
        return new_stem + name[len(old_stem):]
    if name.casefold().startswith(old_stem.casefold()):
        return new_stem + name[len(old_stem):]
    return name


def canonical_name(game: DcGame) -> str:
    return safe_filename(game.name)


def unit_target(unit: DcUnit, root: Path) -> dict[Path, Path]:
    """``{file: target}`` for the canonical place of a matched CHD unit (``<root>/<name>/``, names re-stemmed)."""
    g = unit.game
    assert g is not None
    new_stem = canonical_name(g)
    folder = root / new_stem
    out: dict[Path, Path] = {}
    base = unit.folder
    for f in unit.files:
        if base is not None:
            rel = f.relative_to(base)
            if len(rel.parts) == 1:
                out[f] = folder / (new_stem + CHD_EXT if f == unit.path else _rename_in(rel.name, unit.stem, new_stem))
            else:
                out[f] = folder / rel
        else:
            out[f] = folder / (new_stem + CHD_EXT if f == unit.path else _rename_in(f.name, unit.stem, new_stem))
    return out


def reason_target(unit: DcUnit, root: Path, reason_dir: str) -> dict[Path, Path]:
    """Where a unit goes when it is set aside: ``<root>/<reason dir>/<its path below the root>`` (names kept)."""
    out: dict[Path, Path] = {}
    if unit.folder is not None:
        top = organiser.reason_destination(unit.folder, root, reason_dir)
        for f in unit.files:
            out[f] = top / f.relative_to(unit.folder)
    else:
        for f in unit.files:
            out[f] = organiser.reason_destination(f, root, reason_dir)
    return out


# --------------------------------------------------------------------------- plan

@dataclass
class DcOp(RenameOp):
    """A game-level operation: one folder (or loose CHD) and the file moves it consists of."""
    moves: list = field(default_factory=list)        # [(src Path, dst Path)]
    game: str = ""
    unit_kind: str = ""
    level: str = ""
    n_files: int = 0
    key: int = -1
    unit: Any = None


def _fold(p: Path) -> str:
    return organiser._fold(p)


class _Plan:
    """Shared state while planning: claimed targets, existing files that are being moved away."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sources: set[str] = set()
        self.claimed: dict[str, DcOp] = {}

    def free(self, dst: Path) -> bool:
        key = _fold(dst)
        if key in self.claimed:
            return False
        return not organiser._exists(dst) or key in self.sources


def _unit_op(unit: DcUnit, root: Path, mapping: dict[Path, Path], *, status: str = "move", code: str = "",
             folder: str = "", reasons: tuple[str, ...] = (), superseded_by: str = "", reason: str = "",
             flags_text: str = "") -> DcOp:
    g = unit.game
    src_top = unit.top
    dst_top = _top_of(unit, mapping)
    moves = [(s, d) for s, d in mapping.items() if s != d]
    return DcOp(src=src_top, dst=dst_top, status=status if moves else "ok", reason=reason,
                rom_name=g.name if g else unit.stem,
                kind="rename" if src_top.parent == dst_top.parent else "move",
                dat=g.rep.dat if g else "", unmatched=bool(folder), superseded_by=superseded_by, code=code,
                folder=folder, reasons=reasons, flags_text=flags_text, moves=moves, game=g.name if g else "",
                unit_kind=unit.kind, level=unit.level, n_files=len(unit.files), unit=unit)


def _top_of(unit: DcUnit, mapping: dict[Path, Path]) -> Path:
    if unit.folder is None:
        return mapping.get(unit.path, unit.path)
    rel = len(unit.path.relative_to(unit.folder).parts)
    return mapping[unit.path].parents[rel - 1] if rel else mapping[unit.path].parent


def _reason_mapping(unit: DcUnit, root: Path, reason_dir: str, plan: _Plan) -> dict[Path, Path]:
    """:func:`reason_target` with `` (2)`` ... appended to the unit's top name while that is taken."""
    base = reason_target(unit, root, reason_dir)
    n = 1
    while True:
        if n == 1:
            cand = base
        elif unit.folder is not None:
            top = _top_of(unit, base)
            new_top = top.with_name(f"{top.name} ({n})")
            cand = {s: new_top / base[s].relative_to(top) for s in base}
        else:
            cand = {}
            for s, d in base.items():
                stem = unit.stem
                cand[s] = d.with_name(_rename_in(s.name, stem, f"{stem} ({n})"))
        if all(plan.free(d) or d == s for s, d in cand.items()):
            return cand
        n += 1
        if n > 99:
            return cand


def plan_units(result: DcScanResult, profile: Any = None, move_unmatched: bool = True,
               ) -> tuple[list[DcOp], Any, dict[int, DcOp]]:
    """The game-level ops for tidy (``profile`` None) or Build library; returns ``(ops, selection, owners)``."""
    from . import library
    root = Path(result.root)
    plan = _Plan(root)
    index = result.index
    chd_units = [u for u in result.units if u.kind == "chd" and u.game is not None]
    for u in result.units:
        for f in u.files:
            plan.sources.add(_fold(f))
    for f in result.junk:
        plan.sources.add(_fold(f))
    # --- duplicates: several units of one game -> the best one stays
    groups: dict[str, list[DcUnit]] = {}
    for u in chd_units:
        groups.setdefault(u.game.name, []).append(u)

    def rank(u: DcUnit) -> tuple:
        parts = _parts(u.top, root)
        # canonical place first, then outside the reserved folders, verified before identified, then the copy
        # with the most files (the one that has the user's saves / sidecars), the shortest path, alphabetical
        return (not is_canonical(u, root), folders.reserved_of(parts + ("x",)) is not None,
                u.level != LEVEL_VERIFIED, -len(u.files), len(parts), os.fspath(u.top).casefold())

    keepers: list[DcUnit] = []
    spares: list[tuple[DcUnit, DcUnit]] = []
    for name, members in groups.items():
        members.sort(key=rank)
        keepers.append(members[0])
        spares.extend((m, members[0]) for m in members[1:])
    ops: list[DcOp] = []
    owners: dict[int, DcOp] = {}
    # --- selection (rules) over the keepers
    sel = library.Selection()
    items: list[library.Item] = []
    key_of: dict[int, DcUnit] = {}
    if profile is not None:
        totals = index.disc_totals() if index else {}
        for i, u in enumerate(keepers):
            items.append(library.Item(key=i, dat=u.game.rep.dat, rom=u.game.rep, style=tags.STYLE_REDUMP,
                                      path=u.path, member=None, disc_total=totals.get(u.game.name, 0)))
            key_of[i] = u
        sel = library.select(items, profile, result_platform())
    decisions = sel.decisions
    unit_key = {id(u): i for i, u in key_of.items()}
    for u in sorted(keepers, key=lambda x: os.fspath(x.top).casefold()):
        i = unit_key.get(id(u), -1)
        d = decisions.get(i)
        if d is not None and d.action in (library.EXCLUDED, library.SUPERSEDED, library.INCOMPLETE):
            code = {library.EXCLUDED: "excluded", library.SUPERSEDED: "superseded",
                    library.INCOMPLETE: "incomplete"}[d.action]
            rdir = folders.CODE_DIRS[code]
            mapping = _reason_mapping(u, root, rdir, plan)
            op = _unit_op(u, root, mapping, code=code, folder=rdir, reasons=tuple(d.codes),
                          superseded_by=d.superseded_by, reason=d.reason,
                          flags_text=d.detail)
        else:
            mapping = unit_target(u, root)
            op = _unit_op(u, root, mapping, reason=(d.reason if d is not None and d.action == library.KEEP else ""))
        op.key = i
        ops.append(op)
        owners[i] = op
        _claim(plan, op)
    for u, keeper in spares:
        mapping = _reason_mapping(u, root, DUPLICATES_DIR, plan)
        already = folders.reserved_of(_parts(u.top, root) + ("x",)) == DUPLICATES_DIR
        op = _unit_op(u, root, mapping, code="duplicate", folder=DUPLICATES_DIR,
                      reason=f"another copy of {u.game.name}: kept {_rel(keeper.top, root)}")
        op.keeper = _rel(keeper.top, root)
        if already:
            op.status = "ok"
            op.moves = []
        ops.append(op)
        _claim(plan, op)
    # --- everything that is not a matched CHD game: unmatched units, raw sets, junk
    other_units = [u for u in result.units if u.game is None or u.kind == "raw"]
    for u in sorted(other_units, key=lambda x: os.fspath(x.top).casefold()):
        if u.kind == "raw" and u.game is not None:
            op = DcOp(src=u.top, dst=u.top, status="ok", reason="raw Redump set - convert it to CHD",
                      rom_name=u.game.name, dat=u.game.rep.dat, game=u.game.name, unit_kind="raw",
                      level=u.level, n_files=len(u.files), kind="rename")
            ops.append(op)
            continue
        parts = _parts(u.top, root)
        inside = folders.reserved_of(parts + ("x",)) is not None
        if inside:
            ops.append(DcOp(src=u.top, dst=u.top, status="ok", reason="already in _unmatched" if inside else "",
                            rom_name=u.stem, game="", unit_kind=u.kind, n_files=len(u.files), kind="rename",
                            unmatched=True, folder=folders.reserved_of(parts + ("x",)) or ""))
            continue
        if not move_unmatched:
            ops.append(DcOp(src=u.top, dst=u.top, status="skip", reason="unmatched - left in place (Move unmatched is off)",
                            rom_name=u.stem, unit_kind=u.kind, n_files=len(u.files), kind="rename", unmatched=True))
            continue
        mapping = _reason_mapping(u, root, UNMATCHED_DIR, plan)
        op = _unit_op(u, root, mapping, folder=UNMATCHED_DIR, reason=u.reason or "no Redump match")
        ops.append(op)
        _claim(plan, op)
    for f in result.junk:
        ops.append(_junk_op(f, root, plan, move_unmatched))
    _resolve(ops, plan)
    return ops, sel, owners


def _rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return os.fspath(p)


def _claim(plan: _Plan, op: DcOp) -> None:
    for _s, d in op.moves:
        plan.claimed[_fold(d)] = op


def _junk_op(f: Path, root: Path, plan: _Plan, move_unmatched: bool) -> DcOp:
    parts = _parts(f, root)
    name = f.name
    base = dict(src=f, dst=f, rom_name=name, unit_kind="file", n_files=1, kind="rename", unmatched=True)
    if folders.reserved_of(parts) is not None:
        return DcOp(status="ok", reason="already in a reserved folder", folder=folders.reserved_of(parts) or "", **base)
    low = name.lower()
    if (low in organiser.KEEP_NAMES or f.suffix.lower() in organiser.KEEP_SUFFIXES or low.endswith(".m3u")
            or (len(parts) > 1 and parts[0].casefold() in organiser.KEEP_DIRS) or f.is_symlink()):
        return DcOp(status="skip", reason="left in place (frontend / user file)", **base)
    if not move_unmatched:
        return DcOp(status="skip", reason="unmatched - left in place (Move unmatched is off)", **base)
    dst = organiser.reason_destination(f, root, UNMATCHED_DIR)
    n = 1
    while not plan.free(dst):
        n += 1
        dst = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
    op = DcOp(src=f, dst=dst, status="move", reason="not part of a Dreamcast game", rom_name=name,
              kind="move", unmatched=True, folder=UNMATCHED_DIR, moves=[(f, dst)], unit_kind="file", n_files=1)
    plan.claimed[_fold(dst)] = op
    return op


def _resolve(ops: list[DcOp], plan: _Plan) -> None:
    """Conflicts: a target already taken (another op, or a file that stays) makes the op a ``conflict``."""
    seen: dict[str, DcOp] = {}
    for op in ops:
        if op.status != "move":
            continue
        bad = ""
        for s, d in op.moves:
            key = _fold(d)
            holder = seen.get(key)
            if holder is not None and holder is not op:
                bad = f"another folder is moved to the same name ({holder.rom_name})"
                break
        if bad:
            op.status, op.reason = "conflict", bad
            continue
        for _s, d in op.moves:
            seen[_fold(d)] = op
    # an existing file at a target must be moved away by another (surviving) op
    changed = True
    while changed:
        changed = False
        leaving = {_fold(s) for op in ops if op.status == "move" for s, _d in op.moves}
        for op in ops:
            if op.status != "move":
                continue
            for s, d in op.moves:
                key = _fold(d)
                if organiser._exists(d) and key not in leaving and not organiser._same_file(s, d):
                    op.status, op.reason = "conflict", f"target exists: {d.name}"
                    changed = True
                    break


_PLATFORM: Any = None


def result_platform() -> Any:
    global _PLATFORM
    if _PLATFORM is None:
        from . import platforms
        _PLATFORM = platforms.get_platform(PLATFORM_NAME)
    return _PLATFORM


def plan_tidy(result: DcScanResult, move_unmatched: bool = True) -> list[DcOp]:
    """Organise only: rename folder + CHD + sidecars to the Redump name; no rules."""
    ops, _sel, _owners = plan_units(result, None, move_unmatched)
    return ops


def plan_library(result: DcScanResult, profile: Any, move_unmatched: bool = True, savedisk: bool = False,
                 labels: bool = False) -> Any:
    """Build library: the profile's rules plus tidy plus playlists of the kept multi-disc games."""
    from . import m3u
    ops, sel, owners = plan_units(result, profile, move_unmatched)
    root = Path(result.root)
    specs = []
    for cs in sel.sets:
        disks = []
        for slot, key in sorted(cs.slots.items()):
            op = owners.get(key)
            if op is None:
                continue
            unit = op.unit
            final = dict(op.moves).get(unit.path, unit.path)
            if op.code:       # set aside discs are no playlist members
                disks = []
                break
            disks.append(m3u.PlaylistDisk(slot, final, None, cs.labels.get(slot, "")))
        if disks and len(disks) == len(cs.slots):
            specs.append(m3u.PlaylistSpec(cs.name, cs.dat, cs.total, disks))
    playlists = m3u.plan_playlists(specs, root, savedisk=savedisk, labels=labels)
    moving = [os.fspath(s) for op in ops if op.status == "move" for s, _d in op.moves]
    rewritten = {_fold(p.path) for p in playlists if p.status in ("write", "ok")}
    for st in m3u.plan_stale(root, playlists, moving):
        if _fold(st.path) in rewritten:
            continue
        ops.append(RenameOp(st.path, st.path, organiser.DELETE_STATUS, st.reason, "", "m3u"))
    return organiser.LibraryPlan(ops, playlists, sel, profile)


def file_ops(ops: Iterable[RenameOp]) -> list[RenameOp]:
    """Expand game-level ops into the file-level ``RenameOp`` list ``organiser.apply_renames`` takes."""
    out: list[RenameOp] = []
    for op in ops:
        if isinstance(op, DcOp):
            if op.status != "move":
                continue
            for s, d in op.moves:
                out.append(RenameOp(s, d, "move", op.reason, op.rom_name, "move", op.dat, op.unmatched))
        else:
            out.append(op)
    return out


def apply_plan(ops: Iterable[RenameOp], root: Path, progress: Optional[ProgressFn] = None, cancel: Any = None,
               playlists: Sequence[Any] = ()) -> dict[str, Any]:
    """Apply a tidy / library plan through ``organiser.apply_renames`` (one journal, one undo)."""
    flat = file_ops(ops)
    res = organiser.apply_renames(flat, root, progress=progress, cancel=cancel, playlists=list(playlists))
    return res


# --------------------------------------------------------------------------- Verify fully

def verify_units(result: DcScanResult, chdman: Optional[chdtool.Chdman] = None, progress: Optional[ProgressFn] = None,
                 cancel: Any = None, cache_path: Optional[Path] = None, engine: str = "auto",
                 workers: int = 1) -> dict[str, Any]:
    """Upgrade ``identified`` CHDs to ``verified``: decode EVERY track (audio too) and compare with Redump.

    Uses chdman ``extractcd`` when available (faster, FLAC) else the pure-Python reader. Results go into the
    hash cache; the caller re-scans. Returns ``{"verified", "failed": [{file, error}], "already", "checked"}``.
    """
    root = Path(result.root)
    cache = ChdCache(cache_path or scanner.default_cache_path())
    todo = [m for m in result.matched if m.kind == "chd" and m.level != LEVEL_VERIFIED]
    already = sum(1 for m in result.matched if m.kind == "chd" and m.level == LEVEL_VERIFIED)
    use_chdman = chdman is not None and engine != "python"
    total = sum(m.unit.total_bytes() * (2 if use_chdman else 1) for m in todo) or 1
    prog = _Progress(progress, total, cancel)
    verified, failed = 0, []
    pool = chdpool.make_pool(workers) if (workers > 1 and not use_chdman) else None
    try:
        for m in todo:
            prog.check()
            u: DcUnit = m.unit
            prog.emit(f"Verifying {u.path.name}")
            try:
                info = chdlib.Chd(u.path, load_map=False)
            except (OSError, chdlib.ChdError) as exc:
                failed.append({"file": _rel(u.path, root), "error": str(exc)})
                continue
            try:
                meta = _track_meta(info)
                _merge_cached(meta, cache.get(str(u.path), u.size, u.mtime_ns, info.sha1))
                via = "python"
                try:
                    if use_chdman:
                        got = hash_all_chdman(u.path, info, chdman, root, prog)
                        via = "chdman"
                    else:
                        got = hash_tracks_python(info, [i for i, x in enumerate(meta) if not x["sha1"]], prog, pool)
                except chdtool.ChdmanError:
                    got = hash_tracks_python(info, [i for i, x in enumerate(meta) if not x["sha1"]], prog, pool)
                for i, h in got.items():
                    meta[i].update(crc32=h["crc32"], md5=h["md5"], sha1=h["sha1"])
                bad = [str(meta[i]["number"]) for i in range(len(meta)) if not _rom_ok(u.game.tracks[i], meta[i])]
                cache.put(str(u.path), u.size, u.mtime_ns, info.sha1, "gdrom" if info.is_gd else "cd",
                          [{k: x[k] for k in ("number", "type", "size", "crc32", "md5", "sha1")} for x in meta],
                          LEVEL_VERIFIED, via)
                if bad:
                    failed.append({"file": _rel(u.path, root),
                                   "error": f"track {', '.join(bad)} does not match Redump ({u.game.name})"})
                else:
                    verified += 1
            except chdlib.ChdError as exc:
                failed.append({"file": _rel(u.path, root), "error": f"cannot decode: {exc}"})
            finally:
                info.close()
    finally:
        if pool is not None:
            pool.close()
        cache.close()
    return {"verified": verified, "failed": failed, "already": already, "checked": len(todo)}


# --------------------------------------------------------------------------- Convert raw sets to CHD

@dataclass
class DcConvertOp:
    src: Path                       # the .gdi / .cue sheet
    member: Optional[str]
    dst: Path                       # the new CHD (<root>/<Redump name>/<Redump name>.chd)
    original_dst: Path              # where the raw set goes (_converted_originals/...)
    status: str                     # convert | conflict | skip
    reason: str = ""
    rom_name: str = ""
    via: str = "chdman"
    unit: Any = None
    moves: list = field(default_factory=list)   # [(src, dst)] raw files -> originals
    raw_bytes: int = 0


def plan_convert(result: DcScanResult, chdman_found: bool) -> list[DcConvertOp]:
    root = Path(result.root)
    have_chd = {u.game.name for u in result.units if u.kind == "chd" and u.game is not None}
    ops: list[DcConvertOp] = []
    claimed: set[str] = set()
    for u in sorted((u for u in result.units if u.kind == "raw" and u.game is not None),
                    key=lambda x: os.fspath(x.top).casefold()):
        g = u.game
        name = canonical_name(g)
        dst = root / name / (name + CHD_EXT)
        if u.folder is not None:
            top = organiser.reason_destination(u.folder, root, CONVERTED_DIR)
            moves = [(f, top / f.relative_to(u.folder)) for f in u.files]
            orig = top
        else:
            moves = [(f, organiser.reason_destination(f, root, CONVERTED_DIR)) for f in u.files]
            orig = moves[0][1] if moves else root / CONVERTED_DIR
        op = DcConvertOp(u.path, None, dst, orig, "convert", rom_name=g.name, unit=u, moves=moves,
                         raw_bytes=sum(f.stat().st_size for f in u.files if f.exists()))
        if not chdman_found:
            op.status, op.reason = "skip", "chdman not found - install MAME (Flatpak) or put chdman on PATH"
        elif g.name in have_chd:
            op.status, op.reason = "skip", "a CHD of this game already exists"
        elif _fold(dst) in claimed:
            op.status, op.reason = "conflict", "another set converts to the same name"
        elif organiser._exists(dst):
            op.status, op.reason = "conflict", "target exists"
        elif any(organiser._exists(d) for _s, d in moves):
            op.status, op.reason = "conflict", "a converted original with this name is already kept"
        if op.status == "convert":
            claimed.add(_fold(dst))
        ops.append(op)
    return ops


def _convert_one(op: DcConvertOp, root: Path, chdman: chdtool.Chdman, index: DcIndex, journal: Any,
                 created: list[str], progress: Optional[ProgressFn], cancel: Any) -> None:
    unit: DcUnit = op.unit
    game = unit.game
    for s, _d in op.moves:
        if not organiser._exists(s):
            raise FileNotFoundError(f"source missing: {s}")
    if organiser._exists(op.dst):
        raise FileExistsError(f"target exists: {op.dst}")
    chdtool.check_space(root, 2 * op.raw_bytes)
    work = chdtool.make_workdir(root)
    placed = False
    moved: list[tuple[Path, Path]] = []
    try:
        new = work / "new.chd"
        label = op.rom_name
        chdtool.create_cd(chdman, op.src, new,
                          progress=(lambda d, t, m: progress(d, t, f"{label}: {m}")) if progress else None,
                          cancel=cancel)
        # verify the new CHD against Redump before anything of the original is touched
        info = chdlib.Chd(new, load_map=False)
        try:
            if tuple(t.size for t in info.tracks) != game.sizes:
                raise chdtool.ChdmanError("the new CHD has a different track layout than Redump")
            got = hash_all_chdman(new, info, chdman, root, None)
        finally:
            info.close()
        bad = [str(i + 1) for i, t in enumerate(game.tracks) if not _rom_ok(t, got.get(i, {}))]
        if bad:
            raise chdtool.ChdmanError(f"verification failed: track {', '.join(bad)} of the new CHD does not "
                                      f"match Redump - nothing was changed")
        sha1 = hashlib.sha1()
        with open(new, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                sha1.update(block)
        size = new.stat().st_size
        organiser.make_dirs(op.dst.parent, created, journal)
        seq = journal.record({"op": "create", "path": organiser.rel_str(op.dst, root),
                              "sha1": sha1.hexdigest(), "size": size}, sync=True)
        try:
            organiser.move_exclusive(new, op.dst)
        except BaseException:
            journal.failed(seq)
            raise
        placed = True
        journal.useful = True
        # keep the originals: every raw file moves to _converted_originals/ (journalled one by one)
        for s, d in op.moves:
            organiser.make_dirs(d.parent, created, journal)
            mseq = journal.record({"op": "move", "src": organiser.rel_str(s, root), "dst": organiser.rel_str(d, root)})
            try:
                organiser.rename_no_overwrite(s, d)
            except BaseException:
                journal.failed(mseq)
                raise
            moved.append((s, d))
    except BaseException as exc:
        # roll back what was done: originals back, new CHD removed
        for s, d in reversed(moved):
            try:
                rseq = journal.record({"op": "move", "src": organiser.rel_str(d, root), "dst": organiser.rel_str(s, root)})
                try:
                    organiser.rename_no_overwrite(d, s)
                except BaseException:
                    journal.failed(rseq)
                    raise
            except (OSError, ValueError):
                pass
        if placed and not moved:
            try:
                os.unlink(op.dst)
            except OSError:
                pass
        if isinstance(exc, organiser.UndoLogError):
            raise
        raise
    finally:
        chdtool.remove_workdir(work)


def apply_conversions(ops: Iterable[DcConvertOp], root: Path, chdman: chdtool.Chdman, index: DcIndex,
                      progress: Optional[ProgressFn] = None, cancel: Any = None) -> dict[str, Any]:
    """Convert the ``convert`` ops with chdman: create -> verify against Redump -> place -> keep originals."""
    root = Path(root)
    todo = [o for o in ops if o.status == "convert"]
    journal = organiser.Journal(root)
    converted: list[DcConvertOp] = []
    failed: list[dict[str, str]] = []
    created: list[str] = []
    removed: list[str] = []
    cancelled = False
    error: Optional[str] = None
    try:
        chdtool.sweep_stale(root)
        for i, op in enumerate(todo):
            if organiser._is_cancelled(cancel):
                cancelled = True
                break
            if progress:
                progress(i, len(todo), op.rom_name)
            try:
                _convert_one(op, root, chdman, index, journal, created, progress, cancel)
                converted.append(op)
            except organiser.UndoLogError:
                raise
            except chdtool.ChdmanError as exc:
                if exc.cancelled:
                    cancelled = True
                    break
                failed.append({"src": str(op.src), "dst": str(op.dst), "error": str(exc)})
            except (OSError, ValueError, chdlib.ChdError) as exc:
                failed.append({"src": str(op.src), "dst": str(op.dst), "error": str(exc)})
        protected = organiser.protected_dirs(root, [], set())
        sources = [Path(s).parent for op in converted for s, _d in op.moves] + [Path(c) for c in created]
        mine = set(created)
        for d in organiser.remove_empty_dirs(sources, root, protected):
            if d in mine:
                created.remove(d)
                mine.discard(d)
                journal.record({"op": "unmkdir", "path": organiser.rel_str(Path(d), root)})
            else:
                journal.record({"op": "rmdir", "path": organiser.rel_str(Path(d), root)})
                journal.useful = True
                removed.append(d)
    except organiser.UndoLogError as exc:
        error = str(exc)
    finally:
        log_path = journal.close()
    return {"converted": len(converted), "failed": failed, "removed_dirs": removed,
            "undo_log": str(log_path) if log_path else None, "cancelled": cancelled, "error": error}
