"""Disc systems (Redump DAT + CHD): Sega Dreamcast, Sony PlayStation, Sony PlayStation 2 - ONE engine.

Everything that differs between the systems is a :class:`DiscSystem` (Redump DAT name, platform, GD-ROM / ISO
games, playlist policy, convert command): the registry :data:`SYSTEMS` is filled by :mod:`romorg.dreamcast` and
:mod:`romorg.playstation`, which are thin configuration modules. The engine finds the system from the DAT it is
given (``DatFile.name``), so ``scan(root, dat)`` / ``plan_tidy(result)`` / ... are the same calls for all of them.

Unlike the file based systems a disc *game* is a folder::

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
from . import cdimage, chdsched, chdtool, chdwrite, flacnative, folders, multihash, organiser, scanner, tags, tempspace
from . import meter, winproc
from .datfile import DatFile, Rom
from .folders import CONVERTED_DIR, DUPLICATES_DIR, EXCLUDED_DIR, SUPERSEDED_DIR, UNMATCHED_DIR
from .organiser import RenameOp, safe_filename

CHD_EXT = ".chd"
ISO_EXT = ".iso"
SHEET_EXTS = (".gdi", ".cue")
KIND_GD, KIND_CD, KIND_DVD = "gdrom", "cd", "dvd"
LEVEL_VERIFIED, LEVEL_IDENTIFIED, LEVEL_RAW = "verified", "identified", "raw"
PART_SUFFIX = ".romorg.part"        # the new CHD of a conversion while chdman writes it (next to its destination)

ProgressFn = Callable[[int, int, str], None]


def fmt_duration(seconds: float) -> str:
    """``95 -> "2 min"``, ``3700 -> "1 h 02 min"``, ``20 -> "20 s"``."""
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = (seconds + 30) // 60
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"
_TRACK_RE = re.compile(r"\(Track (\d+)\)\.bin$", re.IGNORECASE)
_MIB = 1 << 20


# --------------------------------------------------------------------------- the systems

@dataclass(frozen=True)
class DiscSystem:
    """Everything the engine needs to know about one Redump + CHD system."""
    key: str                        # "dreamcast" | "psx" | "ps2" (config key prefix / UI id)
    platform: str                   # platforms.PLATFORMS key, e.g. "Sony PlayStation 2"
    dat_name: str                   # Redump header name == DAT file stem
    label: str                      # in messages: "not part of a PlayStation 2 game"
    gd: bool = False                # GD-ROM images (Dreamcast): a .gdi sheet and a GD CHD
    iso: bool = False               # DVD games are ONE .iso (PS2): loose .iso raw sets, DVD / ISO-in-CD CHDs
    playlists: bool = True          # Build library writes an .m3u next to disc 1 of a multi-disc game
    iso_convert: str = ""           # chdman command for a single .iso ("createdvd" | "createcd" | "" = n/a)
    iso_convert_key: str = ""       # config key that overrides ``iso_convert`` ("dvd" | "cd")

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "platform": self.platform, "dat": self.dat_name, "label": self.label,
                "gd": self.gd, "iso": self.iso, "playlists": self.playlists,
                "iso_convert": self.iso_convert, "iso_convert_key": self.iso_convert_key}


SYSTEMS: dict[str, DiscSystem] = {}          # by key
_BY_DAT: dict[str, DiscSystem] = {}
_BY_PLATFORM: dict[str, DiscSystem] = {}


def register(system: DiscSystem) -> DiscSystem:
    SYSTEMS[system.key] = system
    _BY_DAT[system.dat_name] = system
    _BY_PLATFORM[system.platform] = system
    return system


def _load() -> None:
    from . import dreamcast, playstation  # noqa: F401 - importing them registers the systems


def all_systems() -> list[DiscSystem]:
    _load()
    return list(SYSTEMS.values())


def system_for_dat(name: str) -> DiscSystem:
    """The system of a Redump DAT name (an unknown name is treated as the Dreamcast, the first system)."""
    _load()
    return _BY_DAT.get(name) or SYSTEMS["dreamcast"]


def system_for_platform(name: str) -> Optional[DiscSystem]:
    _load()
    return _BY_PLATFORM.get(name)


def is_disc_platform(name: str) -> bool:
    return system_for_platform(name) is not None


# --------------------------------------------------------------------------- the Redump index

@dataclass
class DcGame:
    """One Redump game (= one disc): its ``(Track N).bin`` roms in track order (the ``.cue`` is ignored)."""
    name: str
    category: str
    tracks: list[Rom]
    cue: Optional[Rom] = None
    dat_name: str = ""
    iso: bool = False               # the game is one ``.iso`` rom (DVD)

    @property
    def sizes(self) -> tuple[int, ...]:
        return tuple(r.size for r in self.tracks)

    @property
    def rep(self) -> Rom:
        """A stand-in Rom named like the game (what the library rules / missing list work with)."""
        return Rom(name=self.name, size=sum(self.sizes), crc="", md5="", sha1="", game=self.name,
                   dat=self.tracks[0].dat if self.tracks else self.dat_name, set_name=self.name,
                   category=self.category)


class DcIndex:
    """Redump games by name and by their track-size tuple.

    A game's tracks are its ``(Track N).bin`` roms in track order; a game with one ``.bin`` without a track
    number (PlayStation 2 CD games, single-track PlayStation discs) has that one track; a game that is one
    ``.iso`` (PlayStation 2 DVD games) has the ISO as its only track. The ``.cue`` rom is kept but ignored."""

    def __init__(self, dat: DatFile) -> None:
        self.dat = dat
        self.system = system_for_dat(dat.name)
        self.games: dict[str, DcGame] = {}
        by_game: dict[str, list[Rom]] = {}
        for r in dat.roms:
            by_game.setdefault(r.game, []).append(r)
        for name, roms in by_game.items():
            tracks = []
            cue = None
            plain: list[Rom] = []
            isos: list[Rom] = []
            for r in roms:
                low = r.name.lower()
                m = _TRACK_RE.search(r.name)
                if m:
                    tracks.append((int(m.group(1)), r))
                elif low.endswith(".cue"):
                    cue = r
                elif low.endswith(".bin"):
                    plain.append(r)
                elif low.endswith(ISO_EXT):
                    isos.append(r)
            tracks.sort(key=lambda t: t[0])
            is_iso = False
            if tracks:
                chosen = [r for _n, r in tracks]
            elif len(isos) == 1 and not plain:
                chosen, is_iso = isos, True
            elif len(plain) == 1 and not isos:
                chosen = plain
            else:
                continue
            self.games[name] = DcGame(name, roms[0].category, chosen, cue, dat.name, is_iso)
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

    def __init__(self, db_path: Optional[Path], refresh: bool = False) -> None:
        self.conn: Optional[sqlite3.Connection] = None
        self.refresh = refresh                 # True: read nothing (recalculate every checksum), write everything again
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
            conn.execute("CREATE INDEX IF NOT EXISTS chd_hashes_ident ON chd_hashes(sha1, size, mtime_ns)")
            conn.commit()
            self.conn = conn
        except (sqlite3.Error, OSError):
            self.conn = None

    def get(self, path: str, size: int, mtime_ns: int, sha1: str) -> Optional[dict]:
        if self.conn is None or self.refresh:
            return None
        try:
            with self._lock:
                row = self.conn.execute(
                    "SELECT tracks, level, via FROM chd_hashes WHERE path=? AND size=? AND mtime_ns=? AND sha1=?",
                    (path, size, mtime_ns, sha1)).fetchone()
                if row is None:                    # the same CHD (header sha1, size, time) under another name or folder
                    row = self.conn.execute(
                        "SELECT tracks, level, via FROM chd_hashes WHERE sha1=? AND size=? AND mtime_ns=? LIMIT 1",
                        (sha1, size, mtime_ns)).fetchone()
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
    disc_kind: str = ""             # gdrom | cd | dvd (what the CHD is; "" for raw sets)
    needs_chdman: bool = False      # a valid CHD in a format newer than the built-in reader, and no chdman identified it
    decoded: bool = False           # identification had to decode (not served by the cache / the header)

    @property
    def top(self) -> Path:
        return self.folder if self.folder is not None else self.path

    def total_bytes(self) -> int:
        return sum(int(t.get("size") or 0) for t in self.tracks)


@dataclass
class DcEntry(scanner.Entry):
    reason: str = ""
    kind: str = ""                  # chd | raw | file
    needs_chdman: bool = False


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
            "dat": g.rep.dat if g else "", "roms": [g.name] if g else [],
            "game": g.name if g else "", "set_name": g.name if g else "", "other_dats": [],
            "named_ok": canonical, "placed_ok": canonical, "via": self.kind, "header": 0, "byte_order": "",
            "tags": tags.to_json(t) if t else None,
            "level": self.level, "kind": self.kind, "engine": u.via, "folder": u.folder is not None,
            "tracks": [{"number": x.get("number"), "type": x.get("type"), "size": x.get("size"),
                        "hashed": _is_hashed(x)} for x in u.tracks],
            "disc_kind": u.disc_kind,
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


def sheet_kinds(system: Optional["DiscSystem"]) -> tuple[str, ...]:
    """Extensions of a raw set's main file, best first: ``.gdi`` (Dreamcast only), ``.cue``, ``.iso`` (ISO systems)."""
    out = [".gdi"] if system is None or system.gd else []
    out.append(".cue")
    if system is not None and system.iso:
        out.append(ISO_EXT)
    return tuple(out)


def discover_units(root: Path, files: list[Path], system: Optional["DiscSystem"] = None,
                   ) -> tuple[list[DcUnit], list[Path]]:
    """Group ``files`` (from ``scanner.collect_files``) into CHD / raw-set units; returns ``(units, rest)``."""
    kinds = sheet_kinds(system)
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
    # raw sets: a .gdi (preferred) / .cue sheet (/ a loose .iso for the ISO systems) in a folder without a CHD
    sheet_dirs = {d for d, fs in by_dir.items() if d not in chd_dirs
                  and any(f.suffix.lower() in kinds for f in fs)}
    for d in sorted(sheet_dirs, key=lambda p: str(p).lower()):
        fs = [f for f in by_dir[d] if os.fspath(f) not in claimed]
        sheets: list[Path] = []
        for ext in kinds:               # the first kind present wins (.gdi > .cue > .iso)
            sheets = [f for f in fs if f.suffix.lower() == ext]
            if sheets:
                break
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
    """The track files of a ``.gdi`` / ``.cue`` in track order (None when they cannot be listed per track);
    a loose ``.iso`` is its own single track (never read as text).

    File names are read with chdman's own tokenizer (:func:`cdimage.tokenize`: ``"`` and ``'`` quote, no escapes,
    so ``"Tony's Game.bin"`` is one name) and the sheet's encoding rules (:func:`cdimage.read_sheet`), so the files
    found here are the ones chdman and the built-in writer read. Keywords are case-sensitive, as in chdman (a
    lowercase ``file`` line is not a track file to it, so the set is not listed here either)."""
    if Path(sheet).suffix.lower() == ISO_EXT:
        return [Path(sheet)]
    try:
        text = cdimage.read_sheet(sheet)
    except OSError:
        return None
    base = Path(sheet).parent
    out: list[Path] = []
    lines = cdimage.sheet_lines(text)
    if Path(sheet).suffix.lower() == ".gdi":
        count = cdimage.atoi(lines[0]) if lines else 0
        for ln in lines[1:]:
            parts = cdimage.tokenize(ln)
            if not parts:
                continue
            if len(parts) < 6 or not parts[4]:
                return None
            out.append(base / parts[4])
        return out if count > 0 and len(out) == count else None
    files = 0
    tracks = 0
    for ln in lines:
        parts = cdimage.tokenize(ln)
        word = parts[0] if parts else ""
        if word == "FILE":
            files += 1
            if len(parts) < 2 or not parts[1]:
                return None
            out.append(base / parts[1])
        elif word == "TRACK":
            tracks += 1
    if not out or files != tracks:     # one bin holding several tracks: cannot be matched per file
        return None
    return out


GD_HD_START = 45000          # first LBA of the high-density area of a GD-ROM (every GDI puts track 3 here)
_RAW_SECTOR = 2352


def gdi_from_cue(cue: Path) -> tuple[Optional[str], str]:
    """A ``.gdi`` for a Redump GD-ROM ``.cue`` (``(text, "")``), or ``(None, why not)``.

    Redump's Dreamcast cue sheets carry ``REM SINGLE-DENSITY AREA`` / ``REM HIGH-DENSITY AREA`` before the first
    track of each area; the standard GDI is then fully determined: track 1 at LBA 0, every following track of the
    single-density area right after the previous one (the audio track's 150-sector pregap is part of its file), the
    first high-density track at LBA 45000, the next ones back to back. The numbers were validated against the real
    ``.gdi`` files of Redump sets (see the tests / ``tools/bench_chd.py --check-gdi``) and by a round trip through
    ``chdman createcd`` (identical CHD header SHA-1). Without those markers the layout is unknown: no guess."""
    try:
        text = cdimage.read_sheet(cue)
    except OSError as exc:
        return None, f"cannot read {Path(cue).name}: {exc}"
    base = Path(cue).parent
    area = ""
    seen_areas: list[str] = []
    entries: list[dict[str, Any]] = []
    pending: Optional[str] = None
    for ln in cdimage.sheet_lines(text):
        parts = cdimage.tokenize(ln)            # chdman's tokenizer: the same file names chdman would open
        if not parts:
            continue
        word = parts[0]                         # chdman's keywords are case-sensitive
        marker = cdimage.gd_area_marker(ln)
        if marker:
            area = marker
            seen_areas.append(area)
        elif word == "FILE":
            if len(parts) < 2 or not parts[1]:
                return None, "unreadable FILE line in the .cue"
            pending = parts[1]
        elif word == "TRACK":
            num = cdimage.atoi(parts[1]) if len(parts) > 1 else 0          # C atoi, as chdman reads it
            if num <= 0:
                return None, "unreadable TRACK line in the .cue"
            if pending is None:
                return None, "the .cue has several tracks in one file (cannot be split into a GDI)"
            entries.append({"num": num, "file": pending, "audio": len(parts) > 2 and parts[2] == "AUDIO",
                            "area": area})
            pending = None
    if not entries:
        return None, "the .cue lists no tracks"
    if not seen_areas or seen_areas[0] != "sd" or any(e["area"] == "" for e in entries):
        return None, "the .cue has no REM SINGLE-DENSITY / HIGH-DENSITY AREA markers (a GD-ROM needs a .gdi)"
    if [e["num"] for e in entries] != list(range(1, len(entries) + 1)):
        return None, "the .cue's track numbers are not 1..n"
    if any(entries[i]["area"] == "hd" and entries[i + 1]["area"] == "sd" for i in range(len(entries) - 1)):
        return None, "the .cue lists a single-density track after the high-density area"
    lba = 0
    hd = False
    lines = [str(len(entries))]
    for e in entries:
        f = base / e["file"]
        try:
            size = f.stat().st_size
        except OSError:
            return None, f"track file missing: {e['file']}"
        if size == 0 or size % _RAW_SECTOR:
            return None, f"{e['file']} is not a whole number of 2352-byte sectors"
        if e["area"] == "hd" and not hd:
            hd = True
            if lba > GD_HD_START:
                return None, "the single-density tracks are longer than the single-density area"
            lba = GD_HD_START
        quoted = cdimage.sheet_quote(e["file"])
        if quoted is None:
            return None, f"{e['file']} cannot be named in a .gdi (it has both kinds of quote in its name)"
        lines.append(f'{e["num"]} {lba} {0 if e["audio"] else 4} {_RAW_SECTOR} {quoted} 0')
        lba += size // _RAW_SECTOR
    return "\n".join(lines) + "\n", ""


# --------------------------------------------------------------------------- identification

def _track_meta(info: chdlib.Chd) -> list[dict]:
    return [{"number": t.number, "type": t.type, "size": t.size, "audio": t.is_audio,
             "crc32": None, "md5": None, "sha1": None} for t in info.tracks]


def _is_hashed(m: dict) -> bool:
    """A track whose hashes were computed (a DVD CHD's header SHA-1 is only a *claim*, not a hash)."""
    return bool(m.get("sha1")) and not m.get("claimed")


def disc_kind(info: chdlib.Chd) -> str:
    return KIND_GD if info.is_gd else KIND_DVD if info.is_dvd else KIND_CD


def _merge_cached(meta: list[dict], cached: Optional[dict]) -> None:
    if not cached:
        return
    by_num = {c.get("number"): c for c in cached.get("tracks", [])}
    for m in meta:
        c = by_num.get(m["number"])
        if c and c.get("sha1") and c.get("size") == m["size"]:
            m["crc32"], m["md5"], m["sha1"] = c.get("crc32"), c.get("md5"), c.get("sha1")
            m.pop("claimed", None)


class _Meter:
    """Busy wall-clock time and bytes of one engine (overlapping calls count once: it is a throughput, not a sum)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.since = 0.0
        self.busy = 0.0
        self.bytes = 0

    def begin(self) -> None:
        with self.lock:
            if self.active == 0:
                self.since = time.monotonic()
            self.active += 1

    def end(self, nbytes: int = 0) -> None:
        with self.lock:
            self.bytes += nbytes
            self.active = max(0, self.active - 1)
            if self.active == 0:
                self.busy += time.monotonic() - self.since

    def snapshot(self) -> tuple[int, float]:
        with self.lock:
            busy = self.busy + ((time.monotonic() - self.since) if self.active else 0.0)
            return self.bytes, busy


class _Progress:
    """Bytes-based progress shared by a scan / verify job (reported in MiB)."""

    def __init__(self, report: Optional[ProgressFn], total: int, cancel: Any) -> None:
        self.report, self.total, self.done = report, total, 0
        self.cancel = cancel
        self.label = ""
        self._last = 0.0
        self.files_total = 0            # CHDs / sets of the job (0 = do not show "i/n")
        self.files_done = 0
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self.meters = {"python": _Meter(), "chdman": _Meter()}
        self.engine_label = ""          # "built-in reader, 8 processes, native FLAC" shown next to the MB/s

    def file_done(self) -> None:
        with self._lock:
            self.files_done += 1
        self.emit()

    def progress_text(self) -> str:
        """``"[2/7] 48 MB/s, about 3 min left"`` once enough has been done to tell."""
        bits = []
        if self.files_total > 1:
            bits.append(f"file {min(self.files_done + 1, self.files_total)}/{self.files_total}")
        elapsed = time.monotonic() - self.started
        if elapsed >= 3 and self.done > 0:
            rate = self.done / elapsed
            bits.append(f"{rate / _MIB:.0f} MB/s")
            left = max(0, self.total - self.done)
            if self.done < self.total and rate > 0:
                bits.append("about " + fmt_duration(left / rate) + " left")
        if self.engine_label:
            bits.append(self.engine_label)
        return ", ".join(bits)

    def check(self) -> None:
        if self.cancel is not None and (self.cancel.is_set() if hasattr(self.cancel, "is_set") else self.cancel()):
            raise scanner.ScanCancelled()

    def add(self, n: int) -> None:
        meter.add(n)
        with self._lock:
            self.done += n
            now = time.monotonic()
            if now - self._last <= 0.25:
                return
            self._last = now
        self.emit()

    def emit(self, label: Optional[str] = None) -> None:
        if label is not None:
            self.label = label
        if self.report:
            extra = self.progress_text()
            text = f"{self.label} ({extra})" if extra and self.label else self.label
            self.report(min(self.done, self.total) // _MIB, self.total // _MIB, text)

    def cancelled(self) -> bool:
        return self.cancel is not None and bool(self.cancel.is_set() if hasattr(self.cancel, "is_set") else self.cancel())

    def engine_stats(self, sched: Optional["chdsched.Scheduler"] = None) -> dict[str, Any]:
        """Which engine ran and how fast (for the Dreamcast / PlayStation bar)."""
        py_b, py_s = self.meters["python"].snapshot()
        cm_b, cm_s = self.meters["chdman"].snapshot()
        workers = sched.workers if (sched is not None and sched.pooled) else 1
        info = {"python_bytes": py_b, "python_seconds": round(py_s, 2), "chdman_bytes": cm_b,
                "chdman_seconds": round(cm_s, 2), "workers": workers, "native_flac": flacnative.available()}
        info["python_mb_s"] = round(py_b / py_s / _MIB, 1) if py_s > 0.2 and py_b else 0.0
        info["chdman_mb_s"] = round(cm_b / cm_s / _MIB, 1) if cm_s > 0.2 and cm_b else 0.0
        info["engine"] = ("mixed" if py_b and cm_b else "chdman" if cm_b else "python" if py_b else "none")
        info["text"] = engine_text(info)
        return info


def engine_text(info: dict[str, Any]) -> str:
    """``"Built-in reader (8 processes, native FLAC): 1.1 GB in 8 s, 151 MB/s"``."""
    parts = []
    if info.get("python_bytes"):
        how = f"{info.get('workers', 1)} process{'es' if info.get('workers', 1) != 1 else ''}"
        how += ", native FLAC" if info.get("native_flac") else ", Python FLAC (slow)"
        rate = f", {info['python_mb_s']:.0f} MB/s" if info.get("python_mb_s") else ""
        parts.append(f"Built-in reader ({how}): {info['python_bytes'] / _MIB:.0f} MB in "
                     f"{fmt_duration(info.get('python_seconds', 0))}{rate}")
    if info.get("chdman_bytes"):
        rate = f", {info['chdman_mb_s']:.0f} MB/s" if info.get("chdman_mb_s") else ""
        parts.append(f"chdman: {info['chdman_bytes'] / _MIB:.0f} MB in {fmt_duration(info.get('chdman_seconds', 0))}{rate}")
    return "; ".join(parts)


def hash_tracks_python(info: chdlib.Chd, wanted: Iterable[int], prog: Optional[_Progress] = None,
                      pool: Optional[chdsched.Scheduler] = None) -> dict[int, dict]:
    """crc32 / md5 / sha1 of the tracks with these 0-based indexes, decoded by the built-in reader.

    With a scheduler (:mod:`romorg.chdsched`) the chunks of all the tracks are decoded by worker processes in
    parallel and hashed in order; without one (or when the pool fails) every track is hashed in this process."""
    wanted = list(wanted)
    out: dict[int, dict] = {}
    cancel = prog.cancelled if prog else None
    meter = prog.meters["python"] if prog else None
    if meter:
        meter.begin()
    done_bytes = 0
    try:
        if pool is not None and pool.pooled:
            try:
                got = pool.hash_tracks(info, wanted, progress=(prog.add if prog else None), cancel=cancel)
                for i, h in got.items():
                    out[i] = {"crc32": h["crc32"], "md5": h["md5"], "sha1": h["sha1"]}
                    done_bytes += h["size"]
            except chdsched.Cancelled:
                raise scanner.ScanCancelled() from None
            except chdsched.PoolError:
                pass                               # the in-process path below does the rest
        for i in wanted:
            if i in out:
                continue
            tr = info.tracks[i]
            h = chdlib.hash_track(info, tr, progress=(prog.add if prog else None), cancel=cancel)
            out[i] = {"crc32": h.crc32, "md5": h.md5, "sha1": h.sha1}
            done_bytes += h.size
    except InterruptedError:
        raise scanner.ScanCancelled() from None
    finally:
        if meter:
            meter.end(done_bytes)
    return out


def hash_all_chdman(path: Path, info: chdlib.Chd, chdman: chdtool.Chdman, root: Path,
                    prog: Optional[_Progress] = None) -> dict[int, dict]:
    """Every track via ``chdman extractcd`` into scratch space (RAM when safe, else the app's cache folder, NEVER the
    ROM folder ``root``; deleted afterwards). Raises :class:`chdtool.NoTempSpace` when neither has room."""
    chdtool.chdman_input(path)          # a non-ASCII Windows path chdman cannot open: ChdmanError before any scratch use
    need = sum(t.size for t in info.tracks)
    work = chdtool.acquire_workdir(chdman, need, [root])
    where = " (in RAM)" if work.where == "ram" else " (on disk)"
    meter = prog.meters["chdman"] if prog else None
    if meter:
        meter.begin()
    try:
        if prog:
            prog.emit(f"{Path(path).name}: {work.message}")
        pfn = (lambda d, t, m: prog.emit(f"{Path(path).name}: {m}{where}")) if prog else None
        if info.is_dvd:
            ex = chdtool.extract_dvd(chdman, path, work.path, info.tracks[0].size, progress=pfn,
                                     cancel=(prog.cancel if prog else None))
        else:
            ex = chdtool.extract_cd(chdman, path, work.path, "gdrom" if info.is_gd else "cd",
                                    [t.size for t in info.tracks], progress=pfn,
                                    cancel=(prog.cancel if prog else None))
        out: dict[int, dict] = {}
        if len(ex.tracks) != len(info.tracks) or any(et.size != t.size for et, t in zip(ex.tracks, info.tracks)):
            raise chdtool.ChdmanError("chdman wrote other track sizes than the CHD's metadata says")
        for i, et in enumerate(ex.tracks):
            crc, md5, sha1 = chdtool.hash_range(et.path, et.offset, et.size,
                                                progress=(prog.add if prog else None),
                                                cancel=(prog.cancel if prog else None))
            out[i] = {"crc32": crc, "md5": md5, "sha1": sha1, "size": et.size}
        if meter:
            meter.bytes += need
        return out
    except chdtool.ChdmanError as exc:
        if exc.cancelled:
            raise scanner.ScanCancelled() from exc
        raise
    finally:
        if meter:
            meter.end(0)
        chdtool.remove_workdir(work)


def _rom_ok(rom: Rom, tr: dict) -> bool:
    if tr.get("claimed"):        # a DVD CHD's header SHA-1 (+ the size the candidates were chosen by)
        return bool(tr.get("sha1")) and tr["sha1"] == rom.sha1
    return bool(tr.get("sha1")) and tr["sha1"] == rom.sha1 and (not rom.crc or tr.get("crc32") == rom.crc) \
        and (not rom.md5 or tr.get("md5") == rom.md5)


def match_chd(index: DcIndex, meta: list[dict], hasher: Callable[[list[int]], None],
              full: bool = False) -> tuple[list[DcGame], str, str]:
    """Candidates by track sizes, then by the hashes of the data tracks (smallest first), then audio if needed.

    ``hasher(indexes)`` fills ``meta[i]["crc32" / "md5" / "sha1"]`` for the 0-based track indexes it is given. ``full``: every
    track is read and compared (audio too), so a match is always ``verified``.
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
    if len(alive) > 1 or full:     # same sizes and data: only the audio can tell them apart (or the audio is wanted anyway)
        audio = [i for i in range(len(meta)) if i not in order]
        todo = [i for i in audio if not meta[i]["sha1"]]
        if todo:
            hasher(todo)
        alive = [g for g in alive if all(_rom_ok(g.tracks[i], meta[i]) for i in audio)]
        if not alive:
            return [], "", "the audio tracks do not match any Redump game with these data tracks"
    g = alive[0]
    hashed = [_is_hashed(m) for m in meta]
    if all(hashed):
        if not all(_rom_ok(g.tracks[i], meta[i]) for i in range(len(meta))):
            bad = [str(meta[i]["number"]) for i in range(len(meta)) if not _rom_ok(g.tracks[i], meta[i])]
            return [], "", f"track {', '.join(bad)} differs from Redump (data matches {g.name})"
        return alive, LEVEL_VERIFIED, ""
    return alive, LEVEL_IDENTIFIED, ""


def identify_unit(unit: DcUnit, index: DcIndex, cache: ChdCache, root: Path, chdman: Optional[chdtool.Chdman],
                  engine: str, prog: Optional[_Progress], pool: Optional[chdsched.Scheduler] = None,
                  full: bool = False) -> None:
    """Fill ``unit.game`` / ``level`` / ``tracks`` / ``reason`` for a CHD unit (cache first).

    Engine policy: ``auto`` / ``python`` decode with the built-in reader (parallel scheduler + native FLAC);
    chdman is only used when the reader says it cannot decode the file (``needs_chdman``: a compression name it
    does not know, i.e. a format newer than chdman 0.289) unless ``engine`` is ``python``; ``chdman`` forces chdman
    first."""
    path = unit.path
    system = index.system
    try:
        st = path.stat()
        unit.size, unit.mtime_ns = st.st_size, st.st_mtime_ns
        info = chdlib.Chd(path, load_map=False)
    except chdlib.ChdUnsupported as exc:
        unit.reason = f"cannot read this CHD: {exc}"
        unit.needs_chdman = getattr(exc, "needs_chdman", False) or "not supported" in str(exc)
        return
    except (OSError, chdlib.ChdError) as exc:
        unit.reason = f"not a readable CHD: {exc}"
        return
    try:
        if not (info.is_cd or (info.is_dvd and system.iso)):
            unit.reason = ("a DVD CHD (this system has CD / GD-ROM games only)" if info.is_dvd
                           else "a CHD that is not a CD / DVD disc image")
            return
        unit.sha1, unit.gd, unit.disc_kind = info.sha1, info.is_gd, disc_kind(info)
        meta = _track_meta(info)
        key = str(path)
        _merge_cached(meta, cache.get(key, unit.size, unit.mtime_ns, info.sha1))
        if info.is_dvd and not _is_hashed(meta[0]):
            # chdman createdvd: the header's raw SHA-1 IS the ISO's SHA-1 - identified without decoding anything
            meta[0]["sha1"], meta[0]["claimed"] = info.raw_sha1, True
        unit.tracks = meta
        use_chdman = chdman is not None and engine == "chdman"
        fallback_ok = chdman is not None and engine != "python"
        via = ["python"]

        def with_chdman() -> bool:
            """Every track through ``chdman extract`` (scratch space); False when that is not possible."""
            nonlocal use_chdman
            try:
                got = hash_all_chdman(path, info, chdman, root, prog)
            except chdtool.ChdmanError as exc:
                use_chdman = False          # no space / chdman problem: the built-in reader does it
                tempspace.note_python(str(exc))
                if prog:
                    prog.emit(f"{path.name}: chdman not used ({str(exc).splitlines()[0]}) - built-in reader")
                return False
            for i, h in got.items():
                meta[i].update(crc32=h["crc32"], md5=h["md5"], sha1=h["sha1"])
                meta[i].pop("claimed", None)
            via[0] = "chdman"
            return True

        def hasher(idx: list[int]) -> None:
            unit.decoded = True
            if prog:
                prog.check()
                prog.emit(f"Reading {path.name}")
            if use_chdman and not all(_is_hashed(meta[i]) for i in range(len(meta))) and with_chdman():
                return
            try:
                got = hash_tracks_python(info, [i for i in idx if not _is_hashed(meta[i])], prog, pool)
            except chdlib.ChdUnsupported as exc:
                if not (getattr(exc, "needs_chdman", False) and fallback_ok):
                    raise
                if prog:
                    prog.emit(f"{path.name}: {exc} - using chdman")
                if with_chdman():
                    return
                raise
            for i, h in got.items():
                meta[i].update(h)
                meta[i].pop("claimed", None)

        games, level, reason = match_chd(index, meta, hasher, full)
        unit.via = via[0] if any(_is_hashed(m) for m in meta) else ("header" if info.is_dvd else "")
        if games:
            unit.game, unit.level, unit.reason = games[0], level, ""
            unit.candidates = [g.name for g in games]
        else:
            unit.reason = reason
        cached_level = LEVEL_VERIFIED if all(_is_hashed(m) for m in meta) else LEVEL_IDENTIFIED
        if any(_is_hashed(m) for m in meta):
            cache.put(key, unit.size, unit.mtime_ns, info.sha1, unit.disc_kind,
                      [{k: m[k] for k in ("number", "type", "size", "crc32", "md5", "sha1")} for m in meta],
                      cached_level, unit.via or "python")
    except chdlib.ChdUnsupported as exc:
        unit.reason = f"cannot decode this CHD: {exc}"
        unit.needs_chdman = bool(getattr(exc, "needs_chdman", False))
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
            got = hasher_cache.get(key, st.st_size, st.st_mtime_ns, scanner.file_ident(st)) if key else None
            if got is None:
                if prog:
                    prog.check()
                    prog.emit(f"Hashing {files[i].name}")
                got = _hash_file_progress(files[i], prog)
                if key:
                    hasher_cache.put(key, st.st_size, st.st_mtime_ns, got[0], got[1], scanner.file_ident(st))
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
    """crc32 + sha1 of a raw-set file (prefetching reader, the digests in parallel threads: :mod:`romorg.multihash`)."""
    try:
        crc, _md5, sha1, _n = multihash.hash_file(path, md5=False, progress=(prog.add if prog else None),
                                                   cancel=(prog.cancelled if prog else None))
    except multihash.Cancelled:
        raise scanner.ScanCancelled() from None
    return crc, sha1


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
    engine: str = "python"          # the engine that decoded: python (built-in reader) | chdman | mixed
    engine_info: dict = field(default_factory=dict)     # MB/s, processes, native FLAC (_Progress.engine_stats)
    chdman_label: str = ""
    swept: list[str] = field(default_factory=list)
    temp: dict = field(default_factory=dict)          # where the chdman decodes went (tempspace.report())
    system: Optional[DiscSystem] = None

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
        system = self.system or system_for_dat(self.dat_names[0] if self.dat_names else "")
        dat = self.dat_names[0] if self.dat_names else system.dat_name
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
            "engine": self.engine, "chdman": self.chdman_label, "engine_info": self.engine_info,
            "engine_text": self.engine_info.get("text", "") if self.engine_info else "",
            "system": system.key, "needs_chdman": sum(1 for e in self.unmatched
                                                       if getattr(e, "needs_chdman", False)),
            "temp": self.temp, "temp_text": self.temp.get("text", "") if self.temp else "",
        }


def _estimate(units: Sequence[DcUnit], cache: "ChdCache", use_chdman: bool, full: bool = False) -> tuple[int, int]:
    """``(bytes, files)`` that reading these units will take: what is not in the cache yet."""
    total = 0
    files_todo = 0
    for u in units:
        before = total
        try:
            if u.kind == "chd":
                info = chdlib.Chd(u.path, load_map=False)
                try:
                    st = u.path.stat()
                    cached = cache.get(str(u.path), st.st_size, st.st_mtime_ns, info.sha1)
                    all_hashed = all(t.get("sha1") for t in (cached or {}).get("tracks", [{}])) and bool(cached)
                    if info.is_dvd:
                        pass            # identified by the header SHA-1: nothing to decode
                    elif not cached:
                        total += sum(t.size for t in info.tracks if use_chdman or full or not t.is_audio) * (2 if use_chdman else 1)
                    elif (use_chdman or full) and not all_hashed:
                        total += sum(t.size for t in info.tracks)
                finally:
                    info.close()
            else:
                total += sum(f.stat().st_size for f in (sheet_track_files(u.path) or []) if f.exists())
        except (OSError, chdlib.ChdError):
            pass
        if total > before:
            files_todo += 1
    return total, files_todo


def scan(root, dats, progress: Optional[ProgressFn] = None, cancel: Any = None,
         cache_path: Optional[Path] = None, use_cache: bool = True, chdman: Optional[chdtool.Chdman] = None,
         engine: str = "auto", protected_dirs: Sequence[str] = (), workers: int = 1, full: bool = False,
         refresh_cache: bool = False, **_ignored: Any) -> DcScanResult:
    """Scan ``root`` for CHDs and raw sets and match them to the Redump DAT (``dats``: one DatFile or a list)."""
    root = Path(root).expanduser().absolute()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    dat_list = [dats] if isinstance(dats, DatFile) else list(dats)
    dat = dat_list[0]
    index = get_index(dat)
    system = index.system
    swept = chdtool.sweep_stale(root) + tempspace.sweep_stale()
    tempspace.reset_report()
    problems: list[tuple[Path, str]] = []
    files = scanner.collect_files(root, True, problems, protected_dirs)
    units, rest = discover_units(root, files, system)
    cache = ChdCache((cache_path or scanner.default_cache_path()) if use_cache else None, refresh=refresh_cache)
    fcache = scanner.HashCache((cache_path or scanner.default_cache_path()) if use_cache else None, refresh=refresh_cache)
    use_chdman = chdman is not None and engine == "chdman"        # forced; "auto" = built-in reader first

    sched = chdsched.make_scheduler(workers)
    try:
        # progress total: what has to be decoded / hashed (cached CHDs cost nothing)
        total, files_todo = _estimate(units, cache, use_chdman, full)
        prog = _Progress(progress, max(total, 1), cancel)
        prog.files_total = files_todo
        prog.emit("Scanning...")
        pool = None if use_chdman else sched
        stats_info: dict[str, Any] = {}
        try:
            chd_units = [u for u in units if u.kind == "chd"]
            if pool is not None and pool.pooled and len(chd_units) > 1:
                def work(u: DcUnit) -> None:
                    prog.check()
                    prog.emit(f"Checking {u.path.name}")
                    identify_unit(u, index, cache, root, chdman, engine, prog, pool, full)
                    if u.decoded:
                        prog.file_done()
                # several CHDs at once: their chunks share the worker processes (the early-reject strategy of one
                # CHD is sequential, the others fill the gaps)
                with ThreadPoolExecutor(max_workers=min(max(2, pool.workers), len(chd_units))) as ex:
                    for fut in [ex.submit(work, u) for u in chd_units]:
                        fut.result()
            else:
                for u in chd_units:
                    prog.check()
                    prog.emit(f"Checking {u.path.name}")
                    identify_unit(u, index, cache, root, chdman, engine, prog, pool, full)
                    if u.decoded:
                        prog.file_done()
            for u in units:
                if u.kind != "chd":
                    prog.check()
                    prog.emit(f"Checking {u.path.name}")
                    before = prog.done
                    identify_raw(u, index, fcache, prog)
                    if prog.done > before:
                        prog.file_done()
        finally:
            stats_info = prog.engine_stats(sched)
            sched.close()

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
            unmatched.append(DcEntry(u.path, None, size, "", u.sha1 or None, root, reason=u.reason, kind=u.kind,
                                     needs_chdman=u.needs_chdman))
    for f in rest:
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        unmatched.append(DcEntry(f, None, size, "", None, root, reason=f"not part of a {system.label} game", kind="file"))
    have = {m.unit.game.name for m in matched}
    missing = [g.rep for g in index.games.values() if g.name not in have]
    total_games = len(index.games)
    return DcScanResult(
        root=root, dat_names=[dat.name], matched=matched, unmatched=unmatched, unsupported=[],
        errors=list(problems), missing=missing, dat_total=total_games, dat_totals={dat.name: total_games},
        layout="game_folder", dats=dat_list, units=units, index=index, junk=list(rest), originals=originals,
        engine=(stats_info.get("engine") if stats_info.get("engine") not in (None, "none")
                else ("chdman" if use_chdman else "python")),
        engine_info=stats_info, chdman_label=chdman.label if chdman else "", swept=swept,
        temp=tempspace.report(), system=system)


def scan_many(root, dats: Sequence[DatFile], files: Optional[Sequence[Path]] = None,
              progress: Optional[ProgressFn] = None, cancel: Any = None, cache_path: Optional[Path] = None,
              use_cache: bool = True, chdman: Optional[chdtool.Chdman] = None, engine: str = "auto",
              workers: int = 1, full: bool = False,
              refresh_cache: bool = False) -> tuple[list[tuple[DcUnit, "DiscSystem"]], list[DcUnit], list[Path]]:
    """Every disc image under ``root`` against the Redump DATs of ALL disc systems in ONE pass: the folder is walked and the
    CHDs / sheets found once, each disc is read once (its track hashes are cached and shared by every DAT it is tried
    against; a disc whose track sizes fit no game of a DAT is rejected without decoding anything).

    Returns ``(matched [(unit, system)], unmatched units, other files)``. ``files`` is the list from
    ``scanner.collect_files`` when the caller already has it."""
    root = Path(root).expanduser().absolute()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    indexes = [get_index(d) for d in dats]
    swept = chdtool.sweep_stale(root) + tempspace.sweep_stale()
    tempspace.reset_report()
    if files is None:
        files = scanner.collect_files(root, True, [], ())
    units, rest = discover_units(root, list(files), None)
    units = [u for u in units if not folders.is_converted(_parts(u.path, root))]
    cache = ChdCache((cache_path or scanner.default_cache_path()) if use_cache else None, refresh=refresh_cache)
    fcache = scanner.HashCache((cache_path or scanner.default_cache_path()) if use_cache else None, refresh=refresh_cache)
    use_chdman = chdman is not None and engine == "chdman"
    sched = chdsched.make_scheduler(workers)
    try:
        total, files_todo = _estimate(units, cache, use_chdman, full)
        prog = _Progress(progress, max(total, 1), cancel)
        prog.files_total = files_todo
        prog.emit("Reading the disc images...")
        pool = None if use_chdman else sched

        def one_chd(u: DcUnit) -> None:
            prog.check()
            prog.emit(f"Checking {u.path.name}")
            for idx in indexes:
                u.game, u.level, u.reason, u.candidates = None, "", "", []
                identify_unit(u, idx, cache, root, chdman, engine, prog, pool, full)
                if u.game is not None:
                    u.system_key = idx.system.key
                    break
            if u.decoded:
                prog.file_done()

        chd_units = [u for u in units if u.kind == "chd"]
        try:
            if pool is not None and pool.pooled and len(chd_units) > 1:
                with ThreadPoolExecutor(max_workers=min(max(2, pool.workers), len(chd_units))) as ex:
                    for fut in [ex.submit(one_chd, u) for u in chd_units]:
                        fut.result()
            else:
                for u in chd_units:
                    one_chd(u)
            for u in units:
                if u.kind == "chd":
                    continue
                prog.check()
                prog.emit(f"Checking {u.path.name}")
                before = prog.done
                for idx in indexes:
                    u.game, u.level, u.reason, u.candidates = None, "", "", []
                    identify_raw(u, idx, fcache, prog)
                    if u.game is not None:
                        u.system_key = idx.system.key
                        break
                if prog.done > before:
                    prog.file_done()
        finally:
            sched.close()
    finally:
        fcache.close()
        cache.close()
    matched = [(u, system_for_dat(u.game.rep.dat)) for u in units if u.game is not None]
    return matched, [u for u in units if u.game is None], rest


def identify_isos(paths: Sequence[Path], dats: Sequence[DatFile], progress: Optional[ProgressFn] = None,
                  cancel: Any = None, cache_path: Optional[Path] = None) -> list[tuple[DcUnit, "DiscSystem"]]:
    """Loose ``.iso`` files (PlayStation 2 DVD games) against the DATs of the ISO systems; the ones that match."""
    indexes = [get_index(d) for d in dats if system_for_dat(d.name).iso]
    out: list[tuple[DcUnit, DiscSystem]] = []
    if not indexes or not paths:
        return out
    fcache = scanner.HashCache(cache_path or scanner.default_cache_path())
    try:
        prog = _Progress(progress, max(sum(Path(p).stat().st_size for p in paths if Path(p).exists()), 1), cancel)
        for p in paths:
            prog.check()
            u = DcUnit("raw", Path(p), None, [Path(p)], Path(p).stem)
            for idx in indexes:
                u.game, u.level, u.reason, u.candidates = None, "", "", []
                identify_raw(u, idx, fcache, prog)
                if u.game is not None:
                    out.append((u, idx.system))
                    break
    finally:
        fcache.close()
    return out


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


def plan_units(result: DcScanResult, profile: Any = None, ratings: Any = None, saved: Any = None,
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
        lang_games = None
        if getattr(profile, "keep_other_language", False) and profile.languages and index:
            # the whole DAT decides whether a game has a version in the selected languages
            lang_games = library.language_games(
                (library.Item(key=n, dat=g.rep.dat, rom=g.rep, style=tags.STYLE_REDUMP, path=Path(g.name), member=None,
                              disc_total=totals.get(g.name, 0)) for n, g in enumerate(index.games.values())),
                profile, result_platform(result))
        sel = library.select(items, profile, result_platform(result), ratings=ratings, lang_games=lang_games, saved=saved)
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
        if u.needs_chdman:
            ops.append(DcOp(src=u.top, dst=u.top, status="skip", rom_name=u.stem, unit_kind=u.kind,
                            n_files=len(u.files), kind="rename", unmatched=True,
                            reason="left in place - " + (u.reason or "needs chdman to be identified")))
            continue
        mapping = _reason_mapping(u, root, UNMATCHED_DIR, plan)
        op = _unit_op(u, root, mapping, folder=UNMATCHED_DIR, reason=u.reason or "no Redump match")
        ops.append(op)
        _claim(plan, op)
    for f in result.junk:
        ops.append(_junk_op(f, root, plan, result.system.label if result.system else "Dreamcast"))
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


def _junk_op(f: Path, root: Path, plan: _Plan, label: str = "Dreamcast") -> DcOp:
    parts = _parts(f, root)
    name = f.name
    base = dict(src=f, dst=f, rom_name=name, unit_kind="file", n_files=1, kind="rename", unmatched=True)
    if folders.reserved_of(parts) is not None:
        return DcOp(status="ok", reason="already in a reserved folder", folder=folders.reserved_of(parts) or "", **base)
    low = name.lower()
    if (low in organiser.KEEP_NAMES or f.suffix.lower() in organiser.KEEP_SUFFIXES or low.endswith(".m3u")
            or (len(parts) > 1 and parts[0].casefold() in organiser.KEEP_DIRS) or f.is_symlink()):
        return DcOp(status="skip", reason="left in place (frontend / user file)", **base)
    kept = organiser.bios_op(f)                    # a console's BIOS, told by its checksum: it stays with the discs, under its name
    if kept is not None:
        if kept.status != "move" or not plan.free(kept.dst):
            return DcOp(status="skip" if kept.status == "move" else kept.status, reason=kept.reason, **{**base, "unmatched": False})
        op = DcOp(src=f, dst=kept.dst, status="move", reason=kept.reason, rom_name=kept.dst.name, kind="rename", unmatched=False,
                  moves=[(f, kept.dst)], unit_kind="file", n_files=1)
        plan.claimed[_fold(kept.dst)] = op
        return op
    dst = organiser.reason_destination(f, root, UNMATCHED_DIR)
    n = 1
    while not plan.free(dst):
        n += 1
        dst = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
    op = DcOp(src=f, dst=dst, status="move", reason=f"not part of a {label} game", rom_name=name,
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


def result_platform(result: Any = None) -> Any:
    """The :class:`platforms.Platform` of a scan result (the Dreamcast's when none is given)."""
    from . import platforms
    system = getattr(result, "system", None) or system_for_dat("Sega - Dreamcast")
    return platforms.get_platform(system.platform)


def plan_tidy(result: DcScanResult) -> list[DcOp]:
    """Organise only: rename folder + CHD + sidecars to the Redump name; no rules."""
    ops, _sel, _owners = plan_units(result, None)
    return ops


def plan_library(result: DcScanResult, profile: Any, savedisk: bool = False,
                 labels: bool = False, ratings: Any = None, saved: Any = None) -> Any:
    """Build library: the profile's rules plus tidy plus playlists of the kept multi-disc games."""
    from . import m3u
    ops, sel, owners = plan_units(result, profile, ratings, saved)
    root = Path(result.root)
    if result.system is not None and not result.system.playlists:
        # PlayStation 2: PCSX2 does not read .m3u files - no playlist is written and none is touched
        return organiser.LibraryPlan(ops, [], sel, profile)
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

    The built-in reader decodes (parallel scheduler, native FLAC); chdman ``extractcd`` only when the reader cannot
    (``needs_chdman``) or when ``engine`` is ``chdman``. Results go into the hash cache; the caller re-scans.
    Returns ``{"verified", "failed": [{file, error}], "already", "checked", "engine_info"}``.
    """
    root = Path(result.root)
    cache = ChdCache(cache_path or scanner.default_cache_path())
    todo = [m for m in result.matched if m.kind == "chd" and m.level != LEVEL_VERIFIED]
    already = sum(1 for m in result.matched if m.kind == "chd" and m.level == LEVEL_VERIFIED)
    force_chdman = chdman is not None and engine == "chdman"
    fallback_ok = chdman is not None and engine != "python"
    total = sum(m.unit.total_bytes() * (2 if force_chdman else 1) for m in todo) or 1
    prog = _Progress(progress, total, cancel)
    prog.files_total = len(todo)
    verified, failed = 0, []
    tempspace.sweep_stale()
    tempspace.reset_report()
    sched = chdsched.make_scheduler(workers)
    pool = None if force_chdman else sched
    lock = threading.Lock()
    finished = [0]

    def one(m: Any) -> None:
        nonlocal verified
        prog.check()
        u: DcUnit = m.unit
        prog.emit(f"Verifying {u.path.name}")
        try:
            info = chdlib.Chd(u.path, load_map=False)
        except (OSError, chdlib.ChdError) as exc:
            with lock:
                failed.append({"file": _rel(u.path, root), "error": str(exc)})
            return
        try:
            meta = _track_meta(info)
            _merge_cached(meta, cache.get(str(u.path), u.size, u.mtime_ns, info.sha1))
            via = "python"
            wanted = [i for i, x in enumerate(meta) if not x["sha1"]]

            def by_chdman() -> dict[int, dict]:
                return hash_all_chdman(u.path, info, chdman, root, prog)

            try:
                if force_chdman:
                    got = by_chdman()
                    via = "chdman"
                else:
                    try:
                        got = hash_tracks_python(info, wanted, prog, pool)
                    except chdlib.ChdUnsupported as exc:
                        if not (getattr(exc, "needs_chdman", False) and fallback_ok):
                            raise
                        prog.emit(f"{u.path.name}: {exc} - using chdman")
                        got = by_chdman()
                        via = "chdman"
            except chdtool.ChdmanError as exc:
                if exc.cancelled:
                    raise scanner.ScanCancelled() from exc
                tempspace.note_python(str(exc))
                prog.emit(f"{u.path.name}: chdman not used ({str(exc).splitlines()[0]}) - built-in reader")
                got = hash_tracks_python(info, wanted, prog, pool)
                via = "python"
            for i, h in got.items():
                meta[i].update(crc32=h["crc32"], md5=h["md5"], sha1=h["sha1"])
            bad = [str(meta[i]["number"]) for i in range(len(meta)) if not _rom_ok(u.game.tracks[i], meta[i])]
            cache.put(str(u.path), u.size, u.mtime_ns, info.sha1, disc_kind(info),
                      [{k: x[k] for k in ("number", "type", "size", "crc32", "md5", "sha1")} for x in meta],
                      LEVEL_VERIFIED, via)
            with lock:
                if bad:
                    failed.append({"file": _rel(u.path, root),
                                   "error": f"track {', '.join(bad)} does not match Redump ({u.game.name})"})
                else:
                    verified += 1
        except chdlib.ChdError as exc:
            with lock:
                failed.append({"file": _rel(u.path, root), "error": f"cannot decode: {exc}"})
        finally:
            info.close()
            with lock:
                finished[0] += 1
                prog.files_done = finished[0]

    try:
        if sched.pooled and len(todo) > 1:
            with ThreadPoolExecutor(max_workers=min(max(2, sched.workers), len(todo))) as ex:
                for fut in [ex.submit(one, m) for m in todo]:
                    fut.result()
        else:
            for m in todo:
                one(m)
    finally:
        info_stats = prog.engine_stats(sched)
        sched.close()
        cache.close()
    return {"verified": verified, "failed": failed, "already": already, "checked": len(todo),
            "temp": tempspace.report(), "engine_info": info_stats, "engine_text": info_stats.get("text", "")}


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
    via: str = "builtin"            # who wrote the CHD: builtin | chdman (set when it was converted)
    unit: Any = None
    moves: list = field(default_factory=list)   # [(src, dst)] raw files -> originals
    raw_bytes: int = 0
    mode: str = "createcd"          # chdman command: createcd (cue / gdi sheets) | createdvd (a single .iso)
    gdi_text: Optional[str] = None  # a GD-ROM set that has only a .cue: the .gdi generated from it (None = use ``src``)
    note: str = ""


def iso_convert_mode(system: DiscSystem, config: Optional[dict] = None) -> str:
    """The chdman command for a single ``.iso`` of ``system``: the system's default (PlayStation 2:
    ``createdvd``), overridden by the config key ``<system.iso_convert_key>`` (``"dvd"`` | ``"cd"``)."""
    mode = system.iso_convert
    val = str((config or {}).get(system.iso_convert_key, "") or "").lower() if system.iso_convert_key else ""
    if mode and val in ("dvd", "createdvd"):
        mode = "createdvd"
    elif mode and val in ("cd", "createcd"):
        mode = "createcd"
    return mode


def plan_convert(result: DcScanResult, chdman_found: bool = False,
                 config: Optional[dict] = None) -> list[DcConvertOp]:
    """The raw sets that can become a CHD. ``chdman_found`` no longer decides anything: the built-in writer
    (:mod:`romorg.chdwrite`) converts when there is no chdman."""
    root = Path(result.root)
    system = result.system or system_for_dat(result.dat_names[0] if result.dat_names else "")
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
        is_iso = u.path.suffix.lower() == ISO_EXT
        op = DcConvertOp(u.path, None, dst, orig, "convert", rom_name=g.name, unit=u, moves=moves,
                         raw_bytes=sum(f.stat().st_size for f in u.files if f.exists()),
                         mode=(iso_convert_mode(system, config) or "createcd") if is_iso else "createcd")
        gd_cue = system.gd and u.path.suffix.lower() == ".cue"
        if gd_cue:
            # a GD-ROM needs a .gdi (chdman createcd of a .cue would write a CD CHD, type CHT2): generate the GDI from
            # the Redump layout markers of the .cue, or refuse
            text, why = gdi_from_cue(u.path)
            if text is None:
                op.status, op.reason = "skip", f"needs a .gdi: {why}"
            else:
                op.gdi_text, op.note = text, "the .gdi was generated from the .cue (Redump single / high-density layout)"
        if op.status != "convert":
            pass
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


def assert_new_chd(info: chdlib.Chd, system: DiscSystem, mode: str) -> None:
    """The CHD chdman just wrote must be the KIND of disc the system has (checked before it is accepted: the track
    hashes alone cannot tell a GD-ROM CHD (CHGD) from a CD CHD (CHT2) with the same tracks)."""
    if mode == "createdvd":
        if not info.is_dvd:
            raise chdtool.ChdmanError("the new CHD is not a DVD image")
        return
    if not info.is_cd:
        raise chdtool.ChdmanError("the new CHD is not a CD / GD-ROM image")
    if system.gd and not info.is_gd:
        raise chdtool.ChdmanError("the new CHD is a CD image (CHT2) but a Dreamcast game needs a GD-ROM image (CHGD) - "
                                  "the set needs a .gdi; nothing was changed")
    if info.is_gd and not system.gd:
        raise chdtool.ChdmanError("the new CHD is a GD-ROM image (CHGD) but this system has CD images - nothing was changed")


def verify_new_chd(path: Path, info: chdlib.Chd, root: Path, chdman: Optional[chdtool.Chdman], engine: str,
                   sched: Optional[chdsched.Scheduler], progress: Optional[ProgressFn], label: str,
                   cancel: Any) -> tuple[dict[int, dict], str]:
    """Hash every track of the NEW CHD, independent of the tool that created it: the built-in reader decodes it
    (parallel scheduler); chdman ``extract`` only when ``engine`` is ``chdman`` or the reader cannot decode the file.
    Returns ``(hashes, text for the job message)``."""
    prog = _Progress(None, max(1, sum(t.size for t in info.tracks)), cancel)
    prog.engine_label = ""

    def report(m: str) -> None:
        if progress:
            progress(0, 1, f"{label}: {m}")

    def by_chdman(why: str) -> tuple[dict[int, dict], str]:
        try:
            return hash_all_chdman(path, info, chdman, root, None), f"verified with chdman extract ({why})"
        except chdtool.NoTempSpace as exc:
            tempspace.note_python(str(exc))
            report(f"{exc} - verifying with the built-in reader")
            return hash_tracks_python(info, range(len(info.tracks)), prog, sched), "verified with the built-in reader"

    if chdman is not None and engine == "chdman":
        try:
            chdtool.chdman_input(path)
        except chdtool.ChdmanError:
            pass                                    # chdman cannot be given this path: the built-in reader below
        else:
            report("verifying with chdman extract")
            try:
                return by_chdman("engine chdman")
            except chdtool.ChdmanError as exc:
                if exc.cancelled:
                    raise
                report(f"chdman extract failed ({str(exc).splitlines()[0]}) - verifying with the built-in reader")
    report("verifying the new CHD with the built-in reader (independent of chdman)")
    if progress:
        prog.report = lambda d, t, m: progress(d, t, f"{label}: {m}")
        prog.label = "Verifying the new CHD"
    try:
        got = hash_tracks_python(info, range(len(info.tracks)), prog, sched)
    except chdlib.ChdUnsupported as exc:
        if not (getattr(exc, "needs_chdman", False) and chdman is not None and engine != "python"):
            raise
        report(f"{exc} - verifying with chdman extract instead")
        return by_chdman("the built-in reader cannot decode this CHD")
    stats = prog.engine_stats(sched)
    return got, "verified independently with the built-in reader" + (f" ({stats['text']})" if stats.get("text") else "")


# chdman 0.289 on Windows is not long-path aware: it opens "<folder of the .gdi>\<name>" exactly as joined (the
# ".." segments are not resolved first), and from 260 characters on that open fails - or chdman spins forever
# (measured with crafted sheets: 258 characters work, 260 hang). A relative name must keep the joined path below it.
_CHDMAN_MAX_PATH = 259
_CHDMAN_PATH_LIMIT = os.name == "nt"


def _chdman_reads(work: Path, rel: str) -> bool:
    """Whether chdman can open the track file named ``rel`` in a ``.gdi`` written to ``work``."""
    if cdimage.sheet_quote(rel) is None:
        return False
    return not _CHDMAN_PATH_LIMIT or len(os.path.abspath(work)) + 1 + len(rel) < _CHDMAN_MAX_PATH


def _chdman_input_problem(path: Path) -> str:
    """Why chdman cannot be given ``path`` as its ``-i`` file on this system ("" = it can): chdman 0.289 on Windows
    cannot open an input file whose path has a non-ASCII character in it (measured: ``createcd``, ``createdvd`` and
    ``extractcd`` fail with "No such file or directory"; non-ASCII names *inside* a UTF-8 sheet, and a non-ASCII
    ``-o`` path, are fine). Linux / macOS take any name."""
    if _CHDMAN_PATH_LIMIT and not str(path).isascii():
        return "chdman cannot open a file with a non-ASCII character in its path on Windows"
    return ""


def _chdman_path_problem(op: "DcConvertOp", new: Path) -> str:
    """Why chdman cannot convert ``op`` into ``new`` on this system ("" = it can): on Windows a path chdman opens
    (the sheet, its track files as the sheet names them, the new CHD) of 260 characters or more fails - or, for a
    track file, makes chdman spin forever (see ``_CHDMAN_MAX_PATH``) - and so does a sheet or ISO whose path has a
    non-ASCII character (:func:`_chdman_input_problem`). The built-in writer has neither limit."""
    if not _CHDMAN_PATH_LIMIT:
        return ""
    if op.gdi_text is None:                 # a generated GDI lives in a scratch folder, see _convert_one
        why = _chdman_input_problem(Path(os.path.abspath(op.src)))
        if why:
            return why
    paths = [new]
    if op.gdi_text is None:                 # a generated GDI's own names are checked by _gdi_track_ref
        paths += [op.src] + (sheet_track_files(op.src) or [])
    for p in paths:
        full = str(p) if Path(p).is_absolute() else os.path.join(os.getcwd(), str(p))
        if len(full) >= _CHDMAN_MAX_PATH:
            return f"chdman cannot open paths of 260 characters or more on Windows ({Path(p).name})"
    return ""


def _gdi_track_ref(src: Path, work: Path, name: str) -> str:
    """What the generated ``.gdi`` in ``work`` names for the track file ``src``, without copying it if at all possible.

    chdman reads a GDI's track files as ``<folder of the .gdi> + <name>``, so the file must be reachable from
    ``work`` under a plain name. In order of preference (the files can be gigabytes):

    1. a symlink ``work/name`` (POSIX; Windows only with Developer Mode or as administrator - else WinError 1314);
    2. a hard link ``work/name`` (same volume, NTFS; no privilege needed);
    3. no file at all: a relative path from ``work`` to the original (same drive; chdman 0.289 joins it as is),
       when chdman can read it back: a name it can tokenize (:func:`cdimage.sheet_quote` - apostrophes, double
       spaces and non-ASCII letters are fine, the sheet is written in UTF-8, which chdman reads on every
       platform) and, on Windows, a joined path under 260 characters (see ``_CHDMAN_MAX_PATH``);
    4. a copy, the last resort (another drive and no links, or a path chdman could not open), after checking the
       free space.

    Returns the name to write into the ``.gdi`` (unquoted: :func:`_link_gdi_dir` quotes it)."""
    src = Path(os.path.abspath(src))
    dst = work / name
    try:
        os.symlink(src, dst)
        return name
    except (OSError, NotImplementedError):
        pass
    try:
        if os.name == "nt" and not os.access(src, os.W_OK):
            raise OSError("read-only source: a link to it could not be deleted again")
        os.link(src, dst)
        return name
    except OSError:
        pass
    try:
        rel = os.path.relpath(src, work)
    except ValueError:                                      # another drive (Windows): no relative path exists
        rel = ""
    if rel and _chdman_reads(work, rel):
        return rel
    size = src.stat().st_size
    chdtool.check_space(work, size)
    shutil.copyfile(src, dst)
    return name


def _link_gdi_dir(op: "DcConvertOp", chdman: chdtool.Chdman, root: Path) -> tuple[Any, Path]:
    """A scratch folder with the generated ``.gdi`` that names the set's track files (links where possible, see
    :func:`_gdi_track_ref`; the library is only read)."""
    files = sheet_track_files(op.src) or []
    if not files or op.gdi_text is None:
        raise chdtool.ChdmanError("the .cue cannot be turned into a .gdi")
    work = chdtool.acquire_workdir(chdman, 0, [root])
    try:
        lines = cdimage.sheet_lines(op.gdi_text)
        out = [lines[0].strip()]
        rows = [r for r in (cdimage.tokenize(ln) for ln in lines[1:]) if r]
        for parts, f in zip(rows, files):
            ext = "raw" if parts[2] == "0" else "bin"
            link = _gdi_track_ref(f, work.path, f"track{int(parts[0]):02d}.{ext}")
            quoted = cdimage.sheet_quote(link)              # never None: plain names, or checked relative paths
            out.append(f'{parts[0]} {parts[1]} {parts[2]} {parts[3]} {quoted} {parts[5]}')
        sheet = work.path / "disc.gdi"
        # UTF-8, as chdman reads sheets; surrogateescape keeps a POSIX name that is not UTF-8 as its raw bytes
        sheet.write_text("\n".join(out) + "\n", encoding="utf-8", errors="surrogateescape")
    except BaseException:
        chdtool.remove_workdir(work)
        raise
    return work, sheet


def write_builtin(op: DcConvertOp, new: Path, preset: str, progress: Optional[ProgressFn], cancel: Any,
                  label: str, workers: Optional[int] = None) -> dict:
    """Create ``new`` from the set of ``op`` with the built-in writer, compressing in ``workers`` processes (the
    ``chd_workers`` setting; None = the writer's default). :class:`cdimage.ImageError` = this set has a layout the
    writer does not take (nothing was written); other failures are :class:`chdtool.ChdmanError`."""
    files = sheet_track_files(op.src) if op.gdi_text is not None else None
    if op.gdi_text is not None and not files:
        raise cdimage.ImageError("the .cue cannot be turned into a .gdi")
    image = cdimage.open_image(op.src, op.mode, gdi_text=op.gdi_text, gdi_files=files)

    def report(done: int, total: int) -> None:
        if progress:
            progress(done, total, f"{label}: Compressing, {100 * done // max(1, total)}% complete")
    try:
        if os.environ.get(chdwrite.ENV_THREADS, "").strip().isdigit():
            workers = None                          # the writer's own override wins
        return chdwrite.write_chd(new, image, preset=preset, progress=report, threads=workers,
                                  cancel=lambda: organiser._is_cancelled(cancel))
    except chdwrite.Cancelled:
        raise chdtool.ChdmanError("cancelled", cancelled=True) from None
    except cdimage.ImageError as exc:               # a file vanished or shrank while it was read
        raise chdtool.ChdmanError(f"could not read the set: {exc}") from exc
    except chdwrite.ChdWriteError as exc:
        raise chdtool.ChdmanError(f"could not write the CHD: {exc}") from exc


def _convert_one(op: DcConvertOp, root: Path, chdman: Optional[chdtool.Chdman], index: DcIndex, journal: Any,
                 created: list[str], progress: Optional[ProgressFn], cancel: Any,
                 sched: Optional[chdsched.Scheduler] = None, engine: str = "auto",
                 verify_notes: Optional[list[str]] = None, writer: str = "auto", preset: str = "default") -> None:
    unit: DcUnit = op.unit
    game = unit.game
    system = index.system
    for s, _d in op.moves:
        if not organiser._exists(s):
            raise FileNotFoundError(f"source missing: {s}")
    if organiser._exists(op.dst):
        raise FileExistsError(f"target exists: {op.dst}")
    # The new CHD is the intended output: created next to its final place as a .part file (atomic rename in),
    # on the file system that will hold it. Only the verification extract below uses scratch space elsewhere.
    chdtool.check_space(op.dst.parent if op.dst.parent.exists() else root, op.raw_bytes)
    new = op.dst.with_name(op.dst.name + PART_SUFFIX)
    placed = False
    moved: list[tuple[Path, Path]] = []
    gdi_work = None
    try:
        organiser.make_dirs(op.dst.parent, created, journal)
        if new.is_file() and not new.is_symlink():
            new.unlink()                      # a leftover of a crashed run (our own distinctive name)
        label = op.rom_name
        # Who writes: the built-in writer, unless chdman was chosen and is there. A set whose layout the built-in
        # writer does not take goes to chdman when there is one.
        # chdman cannot open long paths on Windows: the built-in writer takes such a set, whatever was chosen.
        too_long = _chdman_path_problem(op, new) if chdman is not None else ""
        use_chdman = chdman is not None and writer == "chdman" and not too_long
        if too_long and writer == "chdman" and progress:
            progress(0, 1, f"{label}: {too_long} - converting with the built-in writer")
        def builtin() -> bool:
            """Write with the built-in writer; False = this set has a layout it does not take (nothing written)."""
            try:
                write_builtin(op, new, preset, progress, cancel, label,
                              workers=sched.workers if sched is not None else None)
                op.via = "builtin"
                return True
            except cdimage.ImageError as exc:
                if chdman is None or too_long:
                    raise chdtool.ChdmanError(f"this set cannot be converted: {exc}"
                                              + (f"; {too_long}" if too_long else "")) from exc
                if progress:
                    progress(0, 1, f"{label}: {exc} - converting with chdman")
                return False
        if not use_chdman:
            use_chdman = not builtin()
        if use_chdman:
            make = chdtool.create_dvd if op.mode == "createdvd" else chdtool.create_cd
            source = op.src
            if op.gdi_text is not None:
                chdtool.check_access(chdman, Path(op.src).parent)
                gdi_work, source = _link_gdi_dir(op, chdman, root)
                too_long = _chdman_input_problem(Path(os.path.abspath(source)))
                if too_long:                    # the scratch folder's path: the built-in writer takes the set
                    chdtool.remove_workdir(gdi_work)
                    gdi_work = None
                    if progress:
                        progress(0, 1, f"{label}: {too_long} - converting with the built-in writer")
                    if builtin():
                        use_chdman = False
            if use_chdman:
                make(chdman, source, new,
                     progress=(lambda d, t, m: progress(d, t, f"{label}: {m}")) if progress else None,
                     cancel=cancel)
                op.via = "chdman"
                if gdi_work is not None:
                    chdtool.remove_workdir(gdi_work)
                    gdi_work = None
        # verify the new CHD against Redump before anything of the original is touched
        info = chdlib.Chd(new, load_map=False)
        try:
            assert_new_chd(info, system, op.mode)
            if tuple(t.size for t in info.tracks) != game.sizes:
                raise chdtool.ChdmanError("the new CHD has a different track layout than Redump")
            try:
                got, how = verify_new_chd(new, info, root, chdman, engine, sched, progress, label, cancel)
            except scanner.ScanCancelled:
                raise chdtool.ChdmanError("cancelled", cancelled=True) from None
            if verify_notes is not None:
                verify_notes.append(how)
        finally:
            info.close()
            if sched is not None and not sched.release(new):
                # the workers close it: Windows renames / deletes no file that is open. One that did not answer
                # in time is killed with the others (the next request starts new ones).
                sched.restart_workers()
        bad = [str(i + 1) for i, t in enumerate(game.tracks) if not _rom_ok(t, got.get(i, {}))]
        if bad:
            raise chdtool.ChdmanError(f"verification failed: track {', '.join(bad)} of the new CHD does not "
                                      f"match Redump - nothing was changed")
        sha1 = hashlib.sha1()
        with open(new, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                sha1.update(block)
        size = new.stat().st_size
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
        for s, d in list(reversed(moved)):
            try:
                rseq = journal.record({"op": "move", "src": organiser.rel_str(d, root), "dst": organiser.rel_str(s, root)})
                try:
                    organiser.rename_no_overwrite(d, s)
                except BaseException:
                    journal.failed(rseq)
                    raise
                moved.remove((s, d))            # back in place: only what could not be restored stays listed
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
        if gdi_work is not None:
            chdtool.remove_workdir(gdi_work)
        try:
            if new.is_file() and not new.is_symlink():
                new.unlink()                   # failed / cancelled: no half-written CHD stays behind
        except OSError:
            pass


def apply_conversions(ops: Iterable[DcConvertOp], root: Path, chdman: Optional[chdtool.Chdman], index: DcIndex,
                      progress: Optional[ProgressFn] = None, cancel: Any = None, workers: int = 1,
                      engine: str = "auto", writer: str = "auto", preset: str = "default") -> dict[str, Any]:
    """Convert the ``convert`` ops: create -> verify against Redump -> place -> keep originals.

    ``writer``: ``auto`` = the built-in writer (:mod:`romorg.chdwrite`; chdman only for a set it does not take),
    ``chdman`` = chdman when there is one. ``preset``: ``default`` (chdman's codecs, read by every emulator) or
    ``zstd`` (Zstandard: much faster, needs an emulator from 2024 on; built-in writer only).

    The new CHD is verified INDEPENDENTLY of chdman: the built-in reader decodes it (parallel scheduler, ``workers``
    processes) and compares every track with Redump; chdman ``extract`` is only the fallback for a CHD the reader
    cannot decode (or ``engine`` = ``chdman``). ``result["verify_text"]`` says which."""
    root = Path(root)
    todo = [o for o in ops if o.status == "convert"]
    journal = organiser.Journal(root)
    converted: list[DcConvertOp] = []
    failed: list[dict[str, str]] = []
    created: list[str] = []
    removed: list[str] = []
    cancelled = False
    error: Optional[str] = None
    notes: list[str] = []
    sched = chdsched.make_scheduler(workers)
    try:
        chdtool.sweep_stale(root)
        tempspace.sweep_stale()
        tempspace.reset_report()
        for i, op in enumerate(todo):
            if organiser._is_cancelled(cancel):
                cancelled = True
                break
            if progress:
                progress(i, len(todo), op.rom_name)
            try:
                _convert_one(op, root, chdman, index, journal, created, progress, cancel, sched, engine, notes,
                             writer, preset)
                converted.append(op)
            except organiser.UndoLogError:
                raise
            except chdtool.ChdmanError as exc:
                if exc.cancelled:
                    cancelled = True
                    break
                failed.append({"src": str(op.src), "dst": str(op.dst), "error": str(exc)})
            except (OSError, ValueError, chdlib.ChdError) as exc:
                failed.append({"src": str(op.src), "dst": str(op.dst),
                               "error": winproc.long_path_hint(exc, op.src, op.dst)})
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
        sched.close()
        log_path = journal.close()
    return {"converted": len(converted), "failed": failed, "removed_dirs": removed,
            "undo_log": str(log_path) if log_path else None, "cancelled": cancelled, "error": error,
            "temp": tempspace.report(), "verify_text": notes[-1] if notes else "",
            "generated_gdi": sum(1 for o in converted if o.gdi_text is not None),
            "written_by": {v: sum(1 for o in converted if o.via == v) for v in ("builtin", "chdman")
                           if any(o.via == v for o in converted)},
            "preset": preset}
