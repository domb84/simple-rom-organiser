"""Walk a local directory, hash files / list archive members and match them to a DAT.

Loose files are hashed (crc32 + sha1 in a single pass) and matched by sha1 first,
then by crc+size.  Archive members are matched by crc+size only, which is what the
archive's directory gives us cheaply (zip central directory / ``7z l -slt``).

Hashes of loose files are cached in a small sqlite database keyed on
(absolute path, size, mtime_ns) so re-scans of large collections are fast.

Alternate hashes (No-Intro consoles): when a file does not match raw, the
platform's strategies (``alt_hashes``) are tried: SNES copier header (512 bytes
skipped), NES iNES header (16 bytes skipped) and N64 byte order (.v64/.n64
normalised to big-endian .z64). Loose files are read once: the variants are
hashed in the same pass as the raw hash, and only when the size / magic bytes
make a variant possible. Files are never modified by scanning.
"""

from __future__ import annotations

import array
import hashlib
import os
import sys
import shutil
import sqlite3
import subprocess
import threading
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Container, Iterable, Optional, Sequence, Union

from . import multihash
from .datfile import archive_stem, unit_key

if TYPE_CHECKING:  # avoid a hard runtime dependency; only duck-typed methods are used
    from .datfile import DatFile, Rom

CHUNK_SIZE = 1024 * 1024
ZIP_EXTS = {".zip"}
SEVENZIP_EXTS = {".7z", ".rar"}
SKIP_SUFFIXES = {".m3u", ".part"}
UNDO_LOG_PREFIX = ".romorg-undo-"
TEMP_MARKER = ".romorg-tmp-"   # temporary names used by organiser/kickstart while moving
CONVERT_TEMP_MARKER = ".romorg-convert-"  # clean files being written by convert.py
from . import folders  # noqa: E402  (reserved folder names: folders.py is their only home)
from .folders import (CONVERTED_DIR, DUPLICATES_DIR, REASON_DIRS, RESERVED_DIRS,  # noqa: E402,F401
                      UNMATCHED_DIR)

CACHE_FILENAME = "hashes.sqlite"
LAYOUT_PER_DAT = "per_dat"
LAYOUT_FLAT = "flat"

# Alternate-hash strategies (platforms.Platform.alt_hashes)
ALT_SNES_HEADER = "snes_header"
ALT_NES_HEADER = "nes_header"
ALT_N64_BYTEORDER = "n64_byteorder"
VIA_RAW = "raw"
VIA_HEADERLESS = "headerless"
VIA_BYTESWAPPED = "byteswapped"
SNES_HEADER_SIZE = 512
NES_HEADER_SIZE = 16
NES_MAGIC = b"NES\x1a"
N64_Z64_MAGIC = b"\x80\x37\x12\x40"
N64_V64_MAGIC = b"\x37\x80\x40\x12"
N64_N64_MAGIC = b"\x40\x12\x37\x80"
SEVENZIP_TIMEOUT = 300

ProgressFn = Callable[[int, int, str], None]


class ScanCancelled(Exception):
    """Raised by :func:`scan` when the cancel callback/event fires."""


# --------------------------------------------------------------------------- data


@dataclass
class Entry:
    """A loose file, or one member of an archive (``member`` set)."""

    path: Path
    member: Optional[str]
    size: int
    crc: str
    sha1: Optional[str]
    root: Optional[Path] = field(default=None, repr=False, compare=False)

    @property
    def rel(self) -> str:
        """Path relative to the scan root (POSIX style), plus ``::member`` for archives."""
        p = self.path
        if self.root is not None:
            try:
                p = p.relative_to(self.root)
            except ValueError:
                pass
        s = p.as_posix()
        return f"{s}::{self.member}" if self.member is not None else s


@dataclass
class Match:
    """A local entry and every DAT rom it matches.

    ``roms`` is ordered by DAT priority (order of the ``dats`` passed to :func:`scan`),
    so :attr:`primary` (the roms of the highest-priority DAT) is a simple prefix.
    """

    entry: Entry
    roms: list["Rom"]
    matched_via: str = VIA_RAW   # "raw" | "headerless" | "byteswapped"
    header: int = 0              # bytes skipped (512 SNES, 16 NES)
    byte_order: str = ""         # "v64" | "n64" when byteswapped
    alt_crc: str = ""            # hashes of the normalised content (alternate matches)
    alt_sha1: str = ""

    @property
    def primary(self) -> list["Rom"]:
        if not self.roms:
            return []
        first = self.roms[0].dat
        return [r for r in self.roms if r.dat == first]

    @property
    def dat(self) -> str:
        """Name of the primary DAT."""
        return self.roms[0].dat if self.roms else ""


def _rom_key(r: "Rom") -> tuple[str, str]:
    """Counting unit: ``(dat, set_name or name)`` - a No-Intro set (alternates
    like NES .nes/.unh count once), a TOSEC rom name."""
    return unit_key(r)


def _game_key(r: "Rom") -> tuple[str, str]:
    return (r.dat, getattr(r, "set_name", "") or r.game)


@dataclass
class ScanResult:
    root: Path
    dat_names: list[str]                 # DAT names in priority order
    matched: list[Match]
    unmatched: list[Entry]
    unsupported: list[Path]
    errors: list[tuple[Path, str]]
    missing: list["Rom"]                 # across all DATs
    dat_total: int = 0
    dat_totals: dict[str, int] = field(default_factory=dict)  # distinct units (_rom_key) per DAT
    layout: str = LAYOUT_PER_DAT          # "per_dat" | "flat" (platform layout)
    dats: list["DatFile"] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.dat_names, str):  # tolerate the old single ``dat_name``
            self.dat_names = [self.dat_names]
        self.dat_names = list(self.dat_names)

    @property
    def dat_name(self) -> str:
        """Backwards-compatible single name (DAT names joined with ", ")."""
        return ", ".join(self.dat_names)

    def summary(self) -> dict[str, Any]:
        have = len(_covered_names(self.matched))
        dat_total = self.dat_total or (have + len({_rom_key(r) for r in self.missing}))
        # Originals kept by Convert are left alone by the organiser ("ok"): they count as
        # correctly named / in place, but not as duplicates or alternate-hash matches.
        # Files set aside on purpose (_excluded/ ... ) keep their own name: nothing to rename.
        active = [m for m in self.matched if not is_converted_original(m.entry.path, self.root)
                  and not is_set_aside(m.entry.path, self.root)]
        kept = len(self.matched) - len(active)
        correct, to_rename = _naming_counts(active, self.unmatched)
        placed = correctly_placed(self)
        groups = duplicate_groups(self, self.layout)
        losers = [p for _keeper, group in groups for p in group]
        aside = sum(1 for p in losers if is_set_aside_duplicate(p, self.root))
        return {
            "dat_total": dat_total,
            "have": have,
            "missing": dat_total - have,
            "matched_files": len(self.matched),
            "unmatched_files": len(self.unmatched),
            "duplicates": len(losers) - aside,
            "duplicates_set_aside": aside,
            "duplicate_groups": len(groups),
            "bad_dump_files": count_bad_dumps(active),
            "unsupported": len(self.unsupported),
            "errors": len(self.errors),
            "correctly_named": correct + kept,
            "to_rename": to_rename,
            "correctly_placed": sum(placed.values()),
            "per_dat": self._per_dat(placed),
            **self._game_counts(),
            "count_by": self.count_by(),
            "matched_via": matched_via_counts(active),
            "convertible": count_convertible(self),
            "converted_originals": sum(1 for m in self.matched if is_converted_original(m.entry.path, self.root)),
        }

    def count_by(self, dat_name: Optional[str] = None) -> str:
        """``"game"`` if (the given / any) DAT has No-Intro set names, else ``"rom"``."""
        dats = [d for d in self.dats if dat_name is None or d.name == dat_name]
        if dats:
            return "game" if any(_dat_count_by(d) == "game" for d in dats) else "rom"
        roms = [r for m in self.matched for r in m.roms] + list(self.missing)
        return "game" if any(getattr(r, "set_name", "") for r in roms
                             if dat_name is None or r.dat == dat_name) else "rom"

    def _game_totals(self) -> dict[str, int]:
        """DAT name -> distinct games ``(dat, set_name or game)``."""
        out: dict[str, int] = {}
        for d in self.dats:
            out[d.name] = len({_game_key(r) for r in d.roms})
        return out

    def _game_counts(self, dat_name: Optional[str] = None) -> dict[str, int]:
        def want(r: "Rom") -> bool:
            return dat_name is None or r.dat == dat_name

        have_keys = {_game_key(r) for m in self.matched for r in m.roms if want(r)}
        totals = self._game_totals()
        names = [dat_name] if dat_name is not None else list(self.dat_names)
        if totals and all(n in totals for n in names):
            total = sum(totals[n] for n in names)
        else:  # hand-built result without DatFiles
            total = len(have_keys | {_game_key(r) for r in self.missing if want(r)})
        have = len(have_keys)
        return {"games_total": total, "games_have": have, "games_missing": max(total - have, 0)}

    def _per_dat(self, placed: dict[str, int]) -> dict[str, dict[str, int]]:
        names = list(self.dat_names)
        for m in self.matched:  # DATs only known from roms (hand-built results)
            if m.dat not in names:
                names.append(m.dat)
        covered = _covered_names(self.matched)
        missing_by: dict[str, set[str]] = {}
        for r in self.missing:
            missing_by.setdefault(r.dat, set()).add(r.name)
        out: dict[str, dict[str, int]] = {}
        for name in names:
            have = sum(1 for k in covered if k[0] == name)
            total = self.dat_totals.get(name) or (have + len(missing_by.get(name, ())))
            out[name] = {
                "dat_total": total,
                "have": have,
                "missing": total - have,
                "matched_files": sum(1 for m in self.matched if m.dat == name),
                "correctly_placed": placed.get(name, 0),
                **self._game_counts(name),
                "count_by": self.count_by(name),
            }
        return out


def _dat_count_by(d: "DatFile") -> str:
    value = getattr(d, "count_by", None)
    if isinstance(value, str):
        return value
    return "game" if any(getattr(r, "set_name", "") for r in d.roms) else "rom"


def matched_via_counts(matches: Iterable[Match]) -> dict[str, int]:
    out = {VIA_RAW: 0, VIA_HEADERLESS: 0, VIA_BYTESWAPPED: 0}
    for m in matches:
        via = getattr(m, "matched_via", VIA_RAW) or VIA_RAW
        out[via] = out.get(via, 0) + 1
    return out


CONVERTIBLE_EXTS = {".sfc", ".z64"}


def _rel_parts(path: Path, root: Path) -> tuple[str, ...]:
    """``path`` relative to ``root`` as name parts; ``()`` when it is not inside (cheaper than relative_to)."""
    parts, base = Path(path).parts, Path(root).parts
    return parts[len(base):] if parts[:len(base)] == base else ()


def is_converted_original(path: Path, root: Path) -> bool:
    """True for files under ``<root>/_converted_originals/`` (or the legacy ``_unmatched/_converted_originals/``)."""
    return folders.is_converted(_rel_parts(path, root))


def is_in_reason_dir(path: Path, root: Path) -> bool:
    """True for files under ``<root>/_excluded|_superseded|_incomplete|_duplicates/`` (or the legacy
    ``_unmatched/<that folder>/``)."""
    return folders.is_reason(_rel_parts(path, root))


RAW_SWAPPED_EXTS = {".v64": "v64", ".n64": "n64"}  # DAT roms that are themselves byte-swapped


def is_convertible(match: Match) -> bool:
    """Matched via an alternate hash and the DAT rom is a clean ``.sfc`` / ``.z64``, or
    matched raw to a byte-swapped ``.v64`` / ``.n64`` DAT rom (the N64 DAT lists both a
    ``.z64`` and a ``.v64`` rom for some sets; ``convert`` looks up the set's ``.z64``)."""
    via = getattr(match, "matched_via", VIA_RAW)
    if not match.primary:
        return False
    if via == VIA_RAW:
        return any(Path(r.name).suffix.lower() in RAW_SWAPPED_EXTS for r in match.primary)
    return any(Path(r.name).suffix.lower() in CONVERTIBLE_EXTS for r in match.primary)


def count_convertible(result: "ScanResult") -> int:
    """Files the Convert step would actually convert (``convert.plan_conversions`` ops with
    status ``convert``: conflicts, 7z / multi-member archives and originals already kept
    by Convert are not counted)."""
    if not any(is_convertible(m) for m in result.matched):
        return 0
    try:
        from . import convert
    except ImportError:  # convert step unavailable
        return 0
    return sum(1 for op in convert.plan_conversions(result) if op.status == convert.CONVERT)


def game_status(result: "ScanResult") -> list[dict[str, Any]]:
    """One row per set of every loaded DAT (DAT order): ``{dat, name, have, roms, files, rom}``.

    ``name`` = set name (No-Intro) or rom name (TOSEC); ``files`` = rel paths of the
    local entries matching any rom of the set.
    """
    files_by: dict[tuple[str, str], list[str]] = {}
    for m in result.matched:
        for k in dict.fromkeys(_rom_key(r) for r in m.roms):
            files_by.setdefault(k, []).append(m.entry.rel)
    rows: list[dict[str, Any]] = []
    for d in result.dats:
        sets = d.sets() if hasattr(d, "sets") else None
        if sets is None:
            sets = {}
            for r in d.roms:
                sets.setdefault(getattr(r, "set_name", "") or r.name, []).append(r)
        for name, roms in sets.items():
            if not roms:
                continue
            files = files_by.get(_rom_key(roms[0]), [])
            rows.append({"dat": d.name, "name": name, "have": bool(files),
                         "roms": [r.name for r in roms], "files": list(files), "rom": roms[0]})
    return rows


def _covered_names(matches: Iterable[Match]) -> set[tuple[str, str]]:
    """Distinct (dat, rom name) pairs present locally."""
    return {_rom_key(r) for m in matches for r in m.roms}


def count_duplicates(matches: Iterable[Match]) -> int:
    """Matched entries that add no new rom name (i.e. beyond the first for that rom).

    Superseded by :func:`duplicate_groups` (what ``summary()`` and the organiser use)."""
    seen: set[tuple[str, str]] = set()
    dups = 0
    for m in matches:
        names = {_rom_key(r) for r in m.roms}
        if names <= seen:
            dups += 1
        seen |= names
    return dups


def is_set_aside(path: Path, root: Path) -> bool:
    """True for files under a reason folder (``_excluded``, ``_superseded``, ``_incomplete``,
    ``_duplicates``; legacy ``_unmatched/<folder>/`` too): the organiser left them there on purpose."""
    return is_in_reason_dir(path, root)


def is_set_aside_duplicate(path: Path, root: Path) -> bool:
    """True for files under ``<root>/_duplicates/`` (spare copies already moved; legacy place too)."""
    return folders.reserved_of(_rel_parts(path, root)) == DUPLICATES_DIR


def count_bad_dumps(matches: Iterable[Match]) -> int:
    """Matched files whose primary rom is a bad dump (``[b]`` / ``[b ...]`` / No-Intro ``[b]``)."""
    n = 0
    for m in matches:
        prim = m.primary
        try:
            if prim and prim[0].tags.bad:
                n += 1
        except Exception:  # tags unavailable / unparsable name
            continue
    return n


def group_duplicate_units(units: Iterable[Any], root: Path) -> list[tuple[Path, list[Path]]]:
    """Group organiser units (``matched_units``) that are the same rom; ``(keeper, losers)``.

    A unit is one file the organiser treats as one game (duck-typed attributes: ``path`` (absolute),
    ``op`` (its canonical :class:`RenameOp`), ``key`` (frozenset of ``unit_key``), ``form``,
    ``dat``, ``raw``, ``archive``). Keeper = minimum of ``(not already at its canonical path and
    name, already set aside in _duplicates/, lives in a reserved folder (_unmatched/ ...), not an
    exact-content match, is an archive, number of path parts, path length, path.casefold())``: canonical first,
    then outside the reserved folders, exact content before an alternate-hash match, loose before
    archive, then the shortest path, then alphabetical.
    """
    groups: dict[tuple, list[Any]] = {}
    for u in units:
        groups.setdefault((u.dat, u.key, u.form), []).append(u)

    def rank(u: Any) -> tuple:
        try:
            parts = Path(u.path).relative_to(root).parts
        except ValueError:
            parts = Path(u.path).parts
        canonical = u.op.status == "ok" and u.op.src == u.op.dst
        text = str(u.path)
        under = folders.reserved_of(parts) is not None
        return (not canonical, is_set_aside_duplicate(u.path, root), under, not u.raw, u.archive,
                len(parts), len(text), text.casefold())

    out: list[tuple[Path, list[Path]]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=rank)
        out.append((members[0].path, [m.path for m in members[1:]]))
    out.sort(key=lambda g: str(g[0]).casefold())
    return out


def duplicate_groups(result: "ScanResult", layout: Optional[str] = None,
                     units: Optional[Iterable[Any]] = None) -> list[tuple[Path, list[Path]]]:
    """``[(keeper path, [loser paths])]`` (absolute) for files that are the same rom.

    Single source of truth for the scan summary and the organise plan. Same content as a
    loose file, a single-member archive or inside a multi-rom archive of one game groups
    together; copies in different header forms (NES ``.nes`` vs ``.unh``) do not. Originals
    kept by Convert and symbolic links never take part. ``units`` lets the organiser pass the
    units it already built."""
    from . import organiser

    if units is None:
        units = organiser.matched_units(result, layout)
    return group_duplicate_units([u for u in units if not u.link], result.root)


def _archive_member_counts(matches: Iterable[Match], unmatched: Iterable[Entry] = ()) -> dict[Path, int]:
    counts: dict[Path, int] = {}
    for e in [m.entry for m in matches] + list(unmatched):
        if e.member is not None:
            counts[e.path] = counts.get(e.path, 0) + 1
    return counts


def _split_suffix(name: str) -> tuple[str, str]:
    suffix = Path(name).suffix
    return (name[: -len(suffix)], suffix) if suffix else (name, "")


def _target_filename(rom: "Rom", src_name: str, archive_ext: Optional[str] = None,
                     matched_via: str = VIA_RAW, byte_order: str = "") -> str:
    """``organiser.target_filename`` (AMENDMENT 4), with a local fallback."""
    from . import organiser

    fn = getattr(organiser, "target_filename", None)
    if fn is not None:
        return fn(rom, src_name, archive_ext, matched_via, byte_order)
    # Fallback mirroring the contract (until/unless the organiser provides it).
    if archive_ext is not None:
        return organiser.safe_filename(archive_stem(rom) + archive_ext)
    if matched_via == VIA_RAW:
        return organiser.safe_filename(rom.name)
    rom_stem, rom_ext = _split_suffix(rom.name)
    stem = getattr(rom, "set_name", "") or rom_stem
    if matched_via == VIA_BYTESWAPPED:
        return organiser.safe_filename(stem + (".n64" if byte_order == "n64" else ".v64"))
    ext = _split_suffix(src_name)[1]
    if not ext:
        ext = rom_ext
    elif ext.lower() == rom_ext.lower():
        ext = {".sfc": ".smc", ".unh": ".nes"}.get(rom_ext.lower(), ext)
    return organiser.safe_filename(stem + ext)


def _canonical_dir(root: Path, dat_name: str, layout: str = LAYOUT_PER_DAT) -> Path:
    """``organiser.canonical_dir`` (AMENDMENT 4), with a local fallback."""
    from . import organiser

    fn = getattr(organiser, "canonical_dir", None)
    if fn is not None:
        return fn(root, dat_name, layout)
    return root if layout == LAYOUT_FLAT else root / organiser.dat_folder_name(dat_name)


def _match_target(match: Match, rom: "Rom") -> str:
    e = match.entry
    ext = e.path.suffix if e.member is not None else None
    return _target_filename(rom, e.path.name, ext, getattr(match, "matched_via", VIA_RAW),
                            getattr(match, "byte_order", ""))


def is_correctly_named(match: Match, members_in_archive: int = 1) -> bool:
    """Loose file: filename equals a matching rom's target name (``rom.name`` for raw
    matches, an honest extension like ``.smc`` / ``.v64`` for alternate ones).
    Archive member: archive stem equals the rom's set/game name and it is the only member."""
    e = match.entry
    roms = match.primary or match.roms
    raw = getattr(match, "matched_via", VIA_RAW) == VIA_RAW
    if e.member is None:
        return any(e.path.name == _match_target(match, r) or (raw and e.path.name == r.name)
                   for r in roms)
    if members_in_archive != 1:
        return False
    stem = _split_suffix(e.path.name)[0]
    return any(stem == archive_stem(r) or e.path.name == _match_target(match, r) for r in roms)


def canonical_name(match: Match, members_in_archive: int = 1) -> Optional[str]:
    """Filename the entry should have inside its canonical folder, or None (multi-member archive).

    Its own name if it equals one of the primary roms' target names, else the first
    primary rom's target (``organiser.target_filename``). Archive: ``<set or game><ext>``.
    (The organiser refines the choice among duplicate roms with difflib.)
    """
    e = match.entry
    roms = match.primary
    if not roms:
        return None
    if e.member is not None and members_in_archive != 1:
        return None
    raw = getattr(match, "matched_via", VIA_RAW) == VIA_RAW
    targets = [_match_target(match, r) for r in roms]
    for r, target in zip(roms, targets):
        if e.path.name == target:
            return e.path.name
        if e.member is None and raw and e.path.name == r.name:
            return e.path.name
        if e.member is not None and _split_suffix(e.path.name)[0] == archive_stem(r):
            return e.path.name
    return targets[0]


def is_correctly_placed(match: Match, root: Path, members_in_archive: int = 1,
                        layout: str = LAYOUT_PER_DAT) -> bool:
    """True when the entry already sits at ``<canonical dir>/<canonical name>``
    (``per_dat``: ``<root>/<DAT folder>/``; ``flat``: ``<root>/``)."""
    name = canonical_name(match, members_in_archive)
    if name is None:
        return False
    path = match.entry.path if match.entry.path.is_absolute() else root / match.entry.path
    return path.parent == _canonical_dir(root, match.dat, layout) and path.name == name


def correctly_placed(result: "ScanResult") -> dict[str, int]:
    """DAT name -> number of matched entries already at their canonical path (originals
    kept by Convert under ``_converted_originals/`` count: they stay there)."""
    counts = _archive_member_counts(result.matched, result.unmatched)
    layout = getattr(result, "layout", LAYOUT_PER_DAT)
    out: dict[str, int] = {}
    for m in result.matched:
        n = counts.get(m.entry.path, 1) if m.entry.member is not None else 1
        if is_converted_original(m.entry.path, result.root) or is_correctly_placed(m, result.root, n, layout):
            out[m.dat] = out.get(m.dat, 0) + 1
    return out


def _naming_counts(matches: list[Match], unmatched: Iterable[Entry] = ()) -> tuple[int, int]:
    """(correctly_named, to_rename). Multi-member archives are neither (organiser skips them)."""
    member_counts = _archive_member_counts(matches, unmatched)
    correct = to_rename = 0
    for m in matches:
        n = member_counts.get(m.entry.path, 1) if m.entry.member is not None else 1
        if m.entry.member is not None and n != 1:
            continue
        if is_correctly_named(m, n):
            correct += 1
        else:
            to_rename += 1
    return correct, to_rename


# --------------------------------------------------------------------------- hashing


BIG_FILE = 64 << 20
ENV_SCAN_THREADS = "ROMORG_SCAN_THREADS"


def scan_threads() -> int:
    """Threads that hash loose files / list archives ahead of the matching loop (1 = none)."""
    try:
        n = int(os.environ.get(ENV_SCAN_THREADS) or 0)
    except ValueError:
        n = 0
    if n <= 0:
        n = min(4, os.cpu_count() or 1)
    return max(1, n)


def hash_file(path: Path) -> tuple[str, str]:
    """Return (crc32 as 8 lowercase hex chars, sha1 hex) in one pass (big files: prefetching reader thread and the
    two digests in parallel threads, see :mod:`romorg.multihash`)."""
    crc, _md5, sha1, _size = multihash.hash_file(path, md5=False)
    return crc, sha1


# Alternate content variants: variant name -> (transform, via, header bytes, byte order)
VARIANTS: dict[str, tuple[str, str, int, str]] = {
    "snes_header": ("strip512", VIA_HEADERLESS, SNES_HEADER_SIZE, ""),
    "nes_header": ("strip16", VIA_HEADERLESS, NES_HEADER_SIZE, ""),
    "n64_v64": ("swap16", VIA_BYTESWAPPED, 0, "v64"),
    "n64_n64": ("swap32", VIA_BYTESWAPPED, 0, "n64"),
}
TRANSFORMS = ("strip512", "strip16", "swap16", "swap32")
_SKIP = {"strip512": SNES_HEADER_SIZE, "strip16": NES_HEADER_SIZE}
_WORD = {"swap16": 2, "swap32": 4}


def _swap_typecode(width: int) -> str:
    for code in ("H", "I", "L") if width == 2 else ("I", "L"):
        if array.array(code).itemsize == width:
            return code
    raise RuntimeError(f"no {width}-byte array type")


_TYPECODE = {2: _swap_typecode(2), 4: _swap_typecode(4)}


class StreamTransform:
    """Incremental content transform: drop a header (``strip512``/``strip16``) or swap
    the byte order of 2-/4-byte words (``swap16`` = .v64 -> .z64, ``swap32`` = .n64 -> .z64).

    ``feed(chunk) -> bytes`` may hold back a partial word until the next chunk;
    ``finish()`` returns what is left (a trailing partial word is passed through as is).
    """

    def __init__(self, transform: str) -> None:
        if transform not in TRANSFORMS and transform != "":
            raise ValueError(f"unknown transform: {transform}")
        self.transform = transform
        self._skip = _SKIP.get(transform, 0)
        self._word = _WORD.get(transform, 0)
        self._carry = b""

    def feed(self, data: bytes) -> bytes:
        if self._skip:
            if len(data) <= self._skip:
                self._skip -= len(data)
                return b""
            data = data[self._skip:]
            self._skip = 0
        if not self._word:
            return data
        if self._carry:
            data = self._carry + data
            self._carry = b""
        n = len(data) - len(data) % self._word
        if n != len(data):
            self._carry = bytes(data[n:])
            data = data[:n]
        if not data:
            return b""
        a = array.array(_TYPECODE[self._word])
        a.frombytes(data)
        a.byteswap()
        return a.tobytes()

    def finish(self) -> bytes:
        out, self._carry = self._carry, b""
        return out


def variant_for(strategy: str, size: int, magic: Optional[bytes],
                sizes: Optional[Container[int]] = None) -> Optional[str]:
    """Variant name a strategy yields for content of ``size`` starting with ``magic``
    (first 4 bytes), or None when it can't apply. ``magic=None`` (not read yet):
    the decision needs the content - only the size rules are checked.

    SNES: ``size % 1024 == 512``, or ``size - 512`` in ``sizes`` (rom sizes of the loaded
    DATs that are not a multiple of 1 KiB, e.g. 262143-byte homebrew / betas)."""
    if strategy == ALT_SNES_HEADER:
        if size <= SNES_HEADER_SIZE:
            return None
        if size % 1024 == SNES_HEADER_SIZE or (sizes is not None and size - SNES_HEADER_SIZE in sizes):
            return "snes_header"
        return None
    if strategy == ALT_NES_HEADER:
        return "nes_header" if size > NES_HEADER_SIZE and magic == NES_MAGIC else None
    if strategy == ALT_N64_BYTEORDER:
        if size < 4:
            return None
        if magic == N64_V64_MAGIC:
            return "n64_v64"
        if magic == N64_N64_MAGIC:
            return "n64_n64"
    return None


def variants_for(strategies: Sequence[str], size: int, magic: Optional[bytes],
                 sizes: Optional[Container[int]] = None) -> list[str]:
    out = []
    for strategy in strategies:
        v = variant_for(strategy, size, magic, sizes)
        if v is not None and v not in out:
            out.append(v)
    return out


def _may_apply(strategy: str, size: int, ext: str, sizes: Optional[Container[int]] = None) -> bool:
    """Cheap pre-check without reading content (archive members): size rule for SNES,
    member extension for NES (``.nes``) and N64 (``.v64``/``.n64``)."""
    if strategy == ALT_SNES_HEADER:
        return variant_for(strategy, size, None, sizes) is not None
    if strategy == ALT_NES_HEADER:
        return size > NES_HEADER_SIZE and ext == ".nes"
    if strategy == ALT_N64_BYTEORDER:
        return size >= 4 and ext in (".v64", ".n64")
    return False


def _variant_size(variant: str, size: int) -> int:
    return size - VARIANTS[variant][2]


class _Hasher:
    __slots__ = ("crc", "sha", "size", "transform")

    def __init__(self, transform: Optional[StreamTransform] = None) -> None:
        self.crc = 0
        self.sha = hashlib.sha1()
        self.size = 0
        self.transform = transform

    def update(self, chunk: bytes) -> None:
        if self.transform is not None:
            chunk = self.transform.feed(chunk)
        if chunk:
            self.crc = zlib.crc32(chunk, self.crc)
            self.sha.update(chunk)
            self.size += len(chunk)

    def result(self) -> tuple[str, str, int]:
        if self.transform is not None:
            tail = self.transform.finish()
            self.transform = None
            if tail:
                self.update(tail)
        return f"{self.crc & 0xFFFFFFFF:08x}", self.sha.hexdigest(), self.size


def hash_stream(f: BinaryIO, size: int, strategies: Sequence[str] = (), raw: bool = True,
                variants: Optional[Sequence[str]] = None, sizes: Optional[Container[int]] = None,
                ) -> tuple[Optional[tuple[str, str]], dict[str, tuple[str, str, int]]]:
    """Hash a stream in one pass: raw (crc, sha1) (if ``raw``) plus every variant the
    strategies allow for this content, decided from ``size`` and the first 4 bytes.

    ``variants`` restricts the alternates to these names. Returns
    ``((crc, sha1) | None, {variant: (crc, sha1, size)})``.
    """
    first = f.read(CHUNK_SIZE)
    eligible = variants_for(strategies, size, first[:4], sizes)
    if variants is not None:
        eligible = [v for v in eligible if v in variants]
    raw_h = _Hasher() if raw else None
    alt = {v: _Hasher(StreamTransform(VARIANTS[v][0])) for v in eligible}
    hashers = ([raw_h] if raw_h is not None else []) + list(alt.values())
    chunk = first
    if hashers:
        while chunk:
            for h in hashers:
                h.update(chunk)
            chunk = f.read(CHUNK_SIZE)
    raw_out = raw_h.result()[:2] if raw_h is not None else None
    return raw_out, {v: h.result() for v, h in alt.items()}


def hash_file_variants(path: Path, strategies: Sequence[str] = (), raw: bool = True,
                       variants: Optional[Sequence[str]] = None, sizes: Optional[Container[int]] = None,
                       ) -> tuple[Optional[tuple[str, str]], dict[str, tuple[str, str, int]]]:
    """:func:`hash_stream` over a file (a single read of its content)."""
    with open(path, "rb") as f:
        return hash_stream(f, os.fstat(f.fileno()).st_size, strategies, raw, variants, sizes)


def read_magic(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read(4)


class HashCache:
    """sqlite hash cache; silently degrades to a no-op if the db can't be used."""

    def __init__(self, db_path: Optional[Path]) -> None:
        self.conn: Optional[sqlite3.Connection] = None
        self._pending: list[tuple[str, int, int, str, str]] = []
        self._pending_alt: list[tuple[str, int, int, str, str, str, str]] = []
        if db_path is None:
            return
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hashes (path TEXT NOT NULL, size INTEGER NOT NULL,"
                " mtime_ns INTEGER NOT NULL, crc TEXT NOT NULL, sha1 TEXT NOT NULL,"
                " PRIMARY KEY (path, size, mtime_ns))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS alt_hashes (path TEXT NOT NULL, size INTEGER NOT NULL,"
                " mtime_ns INTEGER NOT NULL, member TEXT NOT NULL, variant TEXT NOT NULL,"
                " crc TEXT NOT NULL, sha1 TEXT NOT NULL,"
                " PRIMARY KEY (path, size, mtime_ns, member, variant))"
            )
            conn.commit()
            self.conn = conn
        except (sqlite3.Error, OSError):
            self.conn = None

    def get(self, path: str, size: int, mtime_ns: int) -> Optional[tuple[str, str]]:
        if self.conn is None:
            return None
        try:
            row = self.conn.execute(
                "SELECT crc, sha1 FROM hashes WHERE path=? AND size=? AND mtime_ns=?",
                (path, size, mtime_ns),
            ).fetchone()
        except sqlite3.Error:
            return None
        return (row[0], row[1]) if row else None

    def put(self, path: str, size: int, mtime_ns: int, crc: str, sha1: str) -> None:
        if self.conn is not None:
            self._pending.append((path, size, mtime_ns, crc, sha1))
            if len(self._pending) >= 500:
                self.flush()

    def get_alt(self, path: str, size: int, mtime_ns: int, member: str, variant: str,
                ) -> Optional[tuple[str, str]]:
        """Cached (crc, sha1) of a variant of a loose file (``member=""``) or archive member
        (keyed by the archive's path/size/mtime)."""
        if self.conn is None:
            return None
        try:
            row = self.conn.execute(
                "SELECT crc, sha1 FROM alt_hashes WHERE path=? AND size=? AND mtime_ns=?"
                " AND member=? AND variant=?",
                (path, size, mtime_ns, member, variant),
            ).fetchone()
        except sqlite3.Error:
            return None
        return (row[0], row[1]) if row else None

    def put_alt(self, path: str, size: int, mtime_ns: int, member: str, variant: str,
                crc: str, sha1: str) -> None:
        if self.conn is not None:
            self._pending_alt.append((path, size, mtime_ns, member, variant, crc, sha1))
            if len(self._pending_alt) >= 500:
                self.flush()

    def flush(self) -> None:
        if self.conn is None or not (self._pending or self._pending_alt):
            return
        try:
            if self._pending:
                # Drop stale rows for the same path, then insert the fresh ones.
                self.conn.executemany("DELETE FROM hashes WHERE path=?", [(p[0],) for p in self._pending])
                self.conn.executemany("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?,?)", self._pending)
            if self._pending_alt:
                # Stale = same path, other size/mtime (rows of other members/variants stay).
                stale = {(p[0], p[1], p[2]) for p in self._pending_alt}
                self.conn.executemany(
                    "DELETE FROM alt_hashes WHERE path=? AND NOT (size=? AND mtime_ns=?)", sorted(stale))
                self.conn.executemany("INSERT OR REPLACE INTO alt_hashes VALUES (?,?,?,?,?,?,?)",
                                      self._pending_alt)
            self.conn.commit()
        except sqlite3.Error:
            pass  # read-only / locked db: just lose the cache writes
        self._pending.clear()
        self._pending_alt.clear()

    def close(self) -> None:
        self.flush()
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None


def _cache_key(path: Path) -> Optional[str]:
    """Cache key for a path; None for names sqlite can't store (undecodable bytes)."""
    key = str(path)
    try:
        key.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return key


def default_cache_path() -> Optional[Path]:
    try:
        from .paths import cache_dir

        return cache_dir() / CACHE_FILENAME
    except Exception:  # paths module missing or data dir not creatable
        return None


# --------------------------------------------------------------------------- archives


def find_7z() -> Optional[str]:
    """Locate a 7-Zip command line binary on PATH."""
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    return None


def list_zip(path: Path) -> list[tuple[str, int, str]]:
    """(member name, size, crc) for each file in a zip (directories skipped)."""
    with zipfile.ZipFile(path) as zf:
        return [(i.filename, i.file_size, f"{i.CRC & 0xFFFFFFFF:08x}") for i in zf.infolist() if not i.is_dir()]


def parse_7z_slt(text: str) -> list[tuple[str, int, str]]:
    """Parse ``7z l -slt -ba`` output into (path, size, crc) for files (folders skipped)."""
    out: list[tuple[str, int, str]] = []
    block: dict[str, str] = {}

    def flush() -> None:
        if "Path" in block:
            is_dir = block.get("Folder", "-").strip() == "+" or block.get("Attributes", "").strip().startswith("D")
            if not is_dir:
                try:
                    size = int(block.get("Size", "0").strip() or 0)
                except ValueError:
                    size = 0
                crc = block.get("CRC", "").strip().lower()
                if not crc and size == 0:
                    crc = "00000000"  # 7z omits the CRC of empty files
                if crc:
                    crc = crc.zfill(8)
                out.append((block["Path"], size, crc))
        block.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
            continue
        key, sep, value = line.partition(" = ")
        if not sep:
            key, sep, value = line.partition(" =")  # e.g. "CRC =" with empty value
        if sep:
            if key == "Path" and "Path" in block:
                flush()  # tolerate missing blank separators
            block[key] = value
    flush()
    return out


def list_7z(path: Path, exe: str) -> list[tuple[str, int, str]]:
    proc = subprocess.run(
        [exe, "l", "-slt", "-ba", "-p", "--", str(path)],  # -p: empty password, never prompt
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if proc.returncode != 0:
        lines = [ln.strip() for ln in (proc.stderr + "\n" + proc.stdout).splitlines() if ln.strip()]
        errs = [ln for ln in lines if "ERROR:" in ln]
        msg = "; ".join(errs) if errs else (lines[-1] if lines else f"7z exited with code {proc.returncode}")
        raise RuntimeError(msg)
    return parse_7z_slt(proc.stdout)


class _CountingReader:
    """Wraps a stream and counts the bytes read from it."""

    def __init__(self, f: BinaryIO) -> None:
        self.f = f
        self.count = 0

    def read(self, n: int = -1) -> bytes:
        data = self.f.read(n)
        self.count += len(data)
        return data


def hash_7z_member(path: Path, member: str, exe: str, size: int, strategies: Sequence[str],
                   timeout: float = SEVENZIP_TIMEOUT, sizes: Optional[Container[int]] = None,
                   ) -> dict[str, tuple[str, str, int]]:
    """Alternate hashes of one 7z/rar member, streamed via ``7z e -so`` (nothing on disk).

    When the first bytes rule out every variant (e.g. a ``.v64``-named member that is
    really big-endian), 7z is stopped right away instead of decompressing the rest."""
    proc = subprocess.Popen(
        [exe, "e", "-so", "-p", "--", str(path), member],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    timer = threading.Timer(timeout, proc.kill)
    timer.daemon = True
    timer.start()
    stopped = False
    try:
        assert proc.stdout is not None
        reader = _CountingReader(proc.stdout)  # type: ignore[arg-type]
        _, alt = hash_stream(reader, size, strategies, raw=False, sizes=sizes)  # type: ignore[arg-type]
        if not alt and reader.count > 0 and proc.poll() is None:
            # content arrived but no variant applies: nothing more to read
            proc.kill()
            stopped = True
        else:
            while proc.stdout.read(CHUNK_SIZE):  # drain what an ineligible member left
                pass
        stderr = proc.stderr.read() if proc.stderr is not None and not stopped else b""
        code = proc.wait()
        if stopped:
            code = 0
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    if code != 0:
        text = stderr.decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        errs = [ln for ln in lines if "ERROR:" in ln]
        raise RuntimeError("; ".join(errs) if errs else (lines[-1] if lines else f"7z exited with code {code}"))
    for v, (_, _, n) in alt.items():
        if n != _variant_size(v, size):
            raise RuntimeError(f"7z returned {n + VARIANTS[v][2]} bytes for {member} (expected {size})")
    return alt


# --------------------------------------------------------------------------- walking


def _skip_name(name: str) -> bool:
    return (name.startswith(".") or Path(name).suffix.lower() in SKIP_SUFFIXES
            or name.startswith(UNDO_LOG_PREFIX) or TEMP_MARKER in name or CONVERT_TEMP_MARKER in name)


def collect_files(root: Path, recursive: bool = True,
                  problems: Optional[list[tuple[Path, str]]] = None,
                  protected: Sequence[str] = ()) -> list[Path]:
    """All candidate files under root (hidden files/dirs, m3u, .part skipped), sorted.

    If ``problems`` is given, files that need the user's attention are appended
    to it instead of being skipped silently: leftovers of an interrupted move
    (``*.romorg-tmp-*``) and dangling symbolic links.
    """
    files: list[Path] = []
    protected_folded = {n.casefold() for n in protected}
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for de in it:
                    if _skip_name(de.name):
                        if (problems is not None and CONVERT_TEMP_MARKER in de.name
                                and not de.name.startswith(".")):
                            problems.append((Path(de.path), "partial output of an interrupted convert - the "
                                                            "original is kept (run Undo); safe to delete"))
                        elif (problems is not None and TEMP_MARKER in de.name
                                and not de.name.startswith(".")):
                            original = de.name.split(TEMP_MARKER, 1)[0]
                            problems.append((Path(de.path), "leftover of an interrupted move - rename it "
                                                            f"back to '{original}' (or run Undo)"))
                        continue
                    try:
                        if de.is_dir(follow_symlinks=False):
                            if protected and d == root and de.name.casefold() in protected_folded:
                                continue    # a protected top-level folder (e.g. Kickstarts/): never scanned
                            if recursive:
                                stack.append(Path(de.path))
                        elif de.is_file():
                            files.append(Path(de.path))
                        elif problems is not None and de.is_symlink():
                            problems.append((Path(de.path), "dangling symbolic link (its target is missing)"))
                    except OSError:
                        continue
        except OSError:
            continue
    files.sort(key=lambda p: p.as_posix().lower())
    if problems is not None:
        problems.sort(key=lambda t: t[0].as_posix().lower())
    return files


def _is_cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    if hasattr(cancel, "is_set"):
        return bool(cancel.is_set())
    return bool(cancel())


# --------------------------------------------------------------------------- scan


class _Index:
    """Combined lookup over several DATs; results are in DAT priority order."""

    def __init__(self, dats: Sequence["DatFile"]) -> None:
        self.dats = list(dats)
        self.sha1 = [d.by_sha1() for d in self.dats]
        self.crc = [d.by_crc_size() for d in self.dats]

    def loose(self, size: int, crc: str, sha1: str) -> list["Rom"]:
        """sha1 first, then crc+size (ignoring roms whose known sha1 disagrees), per DAT."""
        out: list["Rom"] = []
        for by_sha1, by_crc in zip(self.sha1, self.crc):
            hit = by_sha1.get(sha1)
            if not hit:
                hit = [r for r in by_crc.get((crc, size), ()) if not r.sha1 or r.sha1 == sha1]
            out.extend(hit)
        return out

    def alt(self, size: int, crc: str, sha1: str) -> list["Rom"]:
        """Like :meth:`loose`; without a sha1 (not computed) crc+size only."""
        if sha1:
            return self.loose(size, crc, sha1)
        return self.member(size, crc)

    def member(self, size: int, crc: str) -> list["Rom"]:
        out: list["Rom"] = []
        if crc:
            for by_crc in self.crc:
                out.extend(by_crc.get((crc, size), ()))
        return out


def _normalise_dats(dats: Union["DatFile", Sequence["DatFile"]]) -> list["DatFile"]:
    """Accept one DatFile or a list; make sure every rom carries its DAT's name."""
    lst = [dats] if hasattr(dats, "roms") else list(dats)  # type: ignore[arg-type]
    for d in lst:
        if any(r.dat != d.name for r in d.roms):
            d.roms = [r if r.dat == d.name else replace(r, dat=d.name) for r in d.roms]
            # drop cached indexes built from the old objects
            for attr in ("_by_sha1", "_by_crc_size", "_games", "_sets"):
                if hasattr(d, attr):
                    setattr(d, attr, None)
    return lst


STRATEGY_VARIANTS: dict[str, tuple[str, ...]] = {
    ALT_SNES_HEADER: ("snes_header",),
    ALT_NES_HEADER: ("nes_header",),
    ALT_N64_BYTEORDER: ("n64_v64", "n64_n64"),
}


def _alt_match(index: _Index, strategies: Sequence[str], alts: dict[str, tuple[str, str, int]],
               ) -> Optional[tuple[str, list["Rom"]]]:
    """First strategy (in ``strategies`` order) whose variant matches: (variant, roms)."""
    for strategy in strategies:
        for v in STRATEGY_VARIANTS.get(strategy, ()):
            if v in alts:
                crc, sha1, size = alts[v]
                roms = index.alt(size, crc, sha1)
                if roms:
                    return v, roms
    return None


def _alt_match_obj(entry: Entry, variant: str, roms: list["Rom"], crc: str, sha1: str) -> Match:
    _, via, header, order = VARIANTS[variant]
    return Match(entry, roms, matched_via=via, header=header, byte_order=order,
                 alt_crc=crc, alt_sha1=sha1)


def scan(
    root: Path | str,
    dats: Union["DatFile", Sequence["DatFile"]],
    recursive: bool = True,
    progress: Optional[ProgressFn] = None,
    cancel: Any = None,
    cache_path: Optional[Path] = None,
    use_cache: bool = True,
    alt_hashes: Sequence[str] = (),
    layout: str = LAYOUT_PER_DAT,
    protected_dirs: Sequence[str] = (),
) -> ScanResult:
    """Scan ``root`` (recursively, incl. DAT folders and the reserved ``_unmatched/``,
    ``_excluded/`` ... folders) against ``dats``.

    ``dats`` is one DatFile or a list in priority order; a file matching several
    DATs keeps all roms (``Match.roms``, priority-ordered), ``Match.primary`` is
    the highest-priority DAT's roms.

    ``alt_hashes``: alternate-hash strategies (``snes_header``, ``nes_header``,
    ``n64_byteorder``) tried in order when an entry has no raw match; the first
    one that matches wins (``Match.matched_via`` etc.). ``layout`` is stored on the
    result (``per_dat`` | ``flat``) for the placement counts.

    ``progress(done_files, total_files, current_name)``; ``cancel`` is a callable
    returning True or a ``threading.Event`` — raises :class:`ScanCancelled`.
    ``cache_path`` overrides the default ``cache_dir()/hashes.sqlite``. ``protected_dirs``: top-level
    folders of ``root`` that are not scanned at all (``Platform.protected_dirs``, e.g. ``Kickstarts``).
    """
    root = Path(root).expanduser().absolute()
    if not root.is_dir():
        raise NotADirectoryError(str(root))

    dat_list = _normalise_dats(dats)
    index = _Index(dat_list)
    exe = find_7z()
    strategies = [a for a in dict.fromkeys(alt_hashes or ()) if a in STRATEGY_VARIANTS]
    # rom sizes that are not a multiple of 1 KiB (SNES copier-header rule, see variant_for)
    odd_sizes = ({r.size for d in dat_list for r in d.roms if r.size and r.size % 1024}
                 if ALT_SNES_HEADER in strategies else set())
    # cache row meaning "computed for these strategies: no variant applies" (archive members)
    no_variants = "none:" + "+".join(strategies)

    matched: list[Match] = []
    unmatched: list[Entry] = []
    unsupported: list[Path] = []
    errors: list[tuple[Path, str]] = []

    problems: list[tuple[Path, str]] = []
    files = collect_files(root, recursive, problems, protected_dirs)
    errors.extend(problems)
    total = len(files)
    cache = HashCache((cache_path or default_cache_path()) if use_cache else None)

    def cached_alts(key: Optional[str], size: int, mtime_ns: int, member: str,
                    content_size: int, negative: bool = False) -> Optional[dict[str, tuple[str, str, int]]]:
        """Cached variants; with ``negative``, None when nothing is cached at all (``{}`` =
        cached "no variant applies")."""
        out: dict[str, tuple[str, str, int]] = {}
        if key is None:
            return None if negative else out
        for strategy in strategies:
            for v in STRATEGY_VARIANTS[strategy]:
                row = cache.get_alt(key, size, mtime_ns, member, v)
                if row is not None:
                    out[v] = (row[0], row[1], _variant_size(v, content_size))
        if negative and not out and cache.get_alt(key, size, mtime_ns, member, no_variants) is None:
            return None
        return out

    def store_alts(key: Optional[str], size: int, mtime_ns: int, member: str,
                   alts: dict[str, tuple[str, str, int]], negative: bool = False) -> None:
        if key is not None:
            for v, (crc, sha1, _) in alts.items():
                cache.put_alt(key, size, mtime_ns, member, v, crc, sha1)
            if negative and not alts:
                cache.put_alt(key, size, mtime_ns, member, no_variants, "", "")

    def add_member(path: Path, member: str, size: int, crc: str, st: Optional[os.stat_result],
                   compute: Optional[Callable[[], dict[str, tuple[str, str, int]]]]) -> None:
        e = Entry(path, member, size, crc, None, root)
        roms = index.member(size, crc)
        if roms:
            matched.append(Match(e, roms))
            return
        if compute is not None:
            key = _cache_key(path) if st is not None else None
            mtime = st.st_mtime_ns if st is not None else 0
            asize = st.st_size if st is not None else 0
            alts = cached_alts(key, asize, mtime, member, size, negative=True)
            if alts is None:
                try:
                    alts = compute()
                    ok = True
                except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError,
                        subprocess.SubprocessError, ValueError, EOFError, zlib.error) as exc:
                    errors.append((path, f"{member}: {type(exc).__name__}: {exc}"))
                    alts, ok = {}, False
                store_alts(key, asize, mtime, member, alts, negative=ok)
            hit = _alt_match(index, strategies, alts)
            if hit is not None:
                v, roms = hit
                matched.append(_alt_match_obj(e, v, roms, alts[v][0], alts[v][1]))
                return
        unmatched.append(e)

    def scan_zip(path: Path, st: os.stat_result) -> None:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                size, crc = info.file_size, f"{info.CRC & 0xFFFFFFFF:08x}"
                compute = None
                if strategies:
                    def compute(info: zipfile.ZipInfo = info, size: int = size,
                                ) -> dict[str, tuple[str, str, int]]:
                        magic: Optional[bytes] = None
                        if any(st_ != ALT_SNES_HEADER for st_ in strategies):
                            with zf.open(info) as f:  # first bytes only: cheap
                                magic = f.read(4)
                        eligible = variants_for(strategies, size, magic, odd_sizes)
                        if not eligible:
                            return {}
                        with zf.open(info) as f:
                            return hash_stream(f, size, strategies, raw=False, variants=eligible,
                                               sizes=odd_sizes)[1]
                add_member(path, info.filename, size, crc, st, compute)

    def scan_7z(path: Path, st: os.stat_result) -> None:
        assert exe is not None
        fut = pre_list.pop(path, None)
        for member, size, crc in (fut.result() if fut is not None else list_7z(path, exe)):
            mext = Path(member).suffix.lower()
            usable = [s_ for s_ in strategies if _may_apply(s_, size, mext, odd_sizes)]
            compute = None
            if usable:
                def compute(member: str = member, size: int = size, usable: list[str] = usable,
                            ) -> dict[str, tuple[str, str, int]]:
                    return hash_7z_member(path, member, exe, size, usable, sizes=odd_sizes)
            add_member(path, member, size, crc, st, compute)

    def scan_loose(path: Path) -> None:
        st = path.stat()
        key = _cache_key(path)
        hit = cache.get(key, st.st_size, st.st_mtime_ns) if key is not None else None
        alts: Optional[dict[str, tuple[str, str, int]]] = None
        if hit is None:
            fut = pre_hash.pop(path, None)
            if fut is not None:                  # hashed ahead by the thread pool (same result, same error)
                crc, sha1, alts = fut.result()
            elif strategies:  # one read: raw + the variants the size/magic allow
                raw, alts = hash_file_variants(path, strategies, sizes=odd_sizes)
                assert raw is not None
                crc, sha1 = raw
            else:
                crc, sha1 = hash_file(path)
            if alts is not None:
                store_alts(key, st.st_size, st.st_mtime_ns, "", alts)
            if key is not None:
                cache.put(key, st.st_size, st.st_mtime_ns, crc, sha1)
        else:
            crc, sha1 = hit
        e = Entry(path, None, st.st_size, crc, sha1, root)
        roms = index.loose(st.st_size, crc, sha1)
        if roms:
            matched.append(Match(e, roms))
            return
        if strategies:
            if alts is None:  # raw hash came from the cache
                alts = cached_alts(key, st.st_size, st.st_mtime_ns, "", st.st_size)
                if not alts:
                    magic = read_magic(path) if any(s_ != ALT_SNES_HEADER for s_ in strategies) else None
                    eligible = variants_for(strategies, st.st_size, magic, odd_sizes)
                    if eligible:
                        alts = hash_file_variants(path, strategies, raw=False, variants=eligible,
                                                  sizes=odd_sizes)[1]
                        store_alts(key, st.st_size, st.st_mtime_ns, "", alts)
            hit2 = _alt_match(index, strategies, alts or {})
            if hit2 is not None:
                v, roms = hit2
                assert alts is not None
                matched.append(_alt_match_obj(e, v, roms, alts[v][0], alts[v][1]))
                return
        unmatched.append(e)

    # Ahead of the (ordered, single-threaded) matching loop a small thread pool hashes the loose files that are not in
    # the cache and lists the 7z / rar archives: file reads, hashlib / zlib and the 7z child processes all run without
    # the GIL, so they overlap (measured on the Steam Deck: 3000 files of ~120 KB, warm cache, 0.47 s -> 0.31 s; the
    # gain is larger on a cold cache). Results are consumed in file order, so the output is the same as before.
    pre_hash: dict[Path, Any] = {}
    pre_list: dict[Path, Any] = {}
    pool: Optional[ThreadPoolExecutor] = None
    workers = scan_threads()
    if workers > 1 and len(files) > 1:
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="romorg-scan")

        def hash_ahead(p: Path) -> tuple[str, str, Optional[dict[str, tuple[str, str, int]]]]:
            if strategies:
                raw, a = hash_file_variants(p, strategies, sizes=odd_sizes)
                assert raw is not None
                return raw[0], raw[1], a
            if os.path.getsize(p) < BIG_FILE:
                c, sh = hash_file(p)
                return c, sh, None
            try:                  # a big file: stop reading it as soon as the scan is cancelled
                c, _m, sh, _n = multihash.hash_file(p, md5=False, cancel=lambda: _is_cancelled(cancel))
            except multihash.Cancelled:
                raise ScanCancelled() from None
            return c, sh, None

        for path in files:
            ext = path.suffix.lower()
            try:
                if ext in SEVENZIP_EXTS and exe is not None:
                    pre_list[path] = pool.submit(list_7z, path, exe)
                elif ext not in ZIP_EXTS and ext not in SEVENZIP_EXTS:
                    st0 = path.stat()
                    key0 = _cache_key(path)
                    if key0 is None or cache.get(key0, st0.st_size, st0.st_mtime_ns) is None:
                        pre_hash[path] = pool.submit(hash_ahead, path)
            except OSError:
                pass            # the main loop reports it the way it always did

    try:
        for i, path in enumerate(files):
            if _is_cancelled(cancel):
                raise ScanCancelled()
            if progress is not None:
                progress(i, total, path.name)
            ext = path.suffix.lower()
            try:
                if ext in ZIP_EXTS:
                    scan_zip(path, path.stat())
                elif ext in SEVENZIP_EXTS:
                    if exe is None:
                        unsupported.append(path)
                        continue
                    scan_7z(path, path.stat())
                else:
                    scan_loose(path)
            except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError,
                    subprocess.SubprocessError, ValueError, EOFError) as exc:
                errors.append((path, f"{type(exc).__name__}: {exc}"))
        if progress is not None:
            progress(total, total, "")
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        cache.close()

    covered = _covered_names(matched)
    seen: set[tuple[str, str]] = set()
    missing = []
    totals: dict[str, int] = {}
    for d in dat_list:
        units = set()
        for r in d.roms:
            k = _rom_key(r)
            units.add(k)
            if k not in covered and k not in seen:
                seen.add(k)
                missing.append(r)
        totals[d.name] = len(units)
    return ScanResult(root, [d.name for d in dat_list], matched, unmatched, unsupported, errors,
                      missing, sum(totals.values()), totals, layout=layout or LAYOUT_PER_DAT,
                      dats=dat_list)


# --------------------------------------------------------------------------- json


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def _entry_json(e: Entry, root: Path) -> dict[str, Any]:
    return {
        "path": _rel(root, e.path),
        "member": e.member,
        "rel": e.rel if e.root is not None else _rel(root, e.path) + (f"::{e.member}" if e.member else ""),
        "size": e.size,
        "crc": e.crc,
        "sha1": e.sha1,
    }


def tags_json(r: "Rom") -> Optional[dict[str, Any]]:
    """``tags.to_json(rom.tags)``; None if the tag parser is unavailable or fails."""
    try:
        from . import tags as tags_mod

        return tags_mod.to_json(r.tags)  # type: ignore[attr-defined]
    except Exception:  # tags.py not available / unparsable name: tags are optional
        return None


def _rom_json(r: "Rom") -> dict[str, Any]:
    return {"name": r.name, "game": r.game, "dat": r.dat, "size": r.size, "crc": r.crc,
            "md5": r.md5, "sha1": r.sha1, "set_name": getattr(r, "set_name", ""),
            "tags": tags_json(r)}


def to_json(result: ScanResult, limit: Optional[int] = None) -> dict[str, Any]:
    """JSON-friendly dict; each list is truncated to ``limit`` items if given."""
    root = result.root

    def cut(items: list) -> list:
        return items if limit is None else items[:limit]

    member_counts = _archive_member_counts(result.matched, result.unmatched)
    layout = getattr(result, "layout", LAYOUT_PER_DAT)
    matched = []
    for m in cut(result.matched):
        d = _entry_json(m.entry, root)
        n = member_counts.get(m.entry.path, 1) if m.entry.member is not None else 1
        d["roms"] = [r.name for r in m.roms]
        d["games"] = sorted({r.game for r in m.roms})
        d["dat"] = m.dat
        d["dats"] = list(dict.fromkeys(r.dat for r in m.roms))
        d["primary"] = [r.name for r in m.primary]
        d["correctly_named"] = is_correctly_named(m, n)
        d["correctly_placed"] = is_correctly_placed(m, root, n, layout)
        d["via"] = getattr(m, "matched_via", VIA_RAW)
        d["header"] = getattr(m, "header", 0)
        d["byte_order"] = getattr(m, "byte_order", "")
        d["tags"] = tags_json((m.primary or m.roms)[0]) if m.roms else None
        matched.append(d)
    return {
        "root": str(root),
        "dat_name": result.dat_name,
        "dat_names": list(result.dat_names),
        "layout": layout,
        "summary": result.summary(),
        "matched": matched,
        "unmatched": [_entry_json(e, root) for e in cut(result.unmatched)],
        "unsupported": [_rel(root, p) for p in cut(result.unsupported)],
        "errors": [{"path": _rel(root, p), "error": msg} for p, msg in cut(result.errors)],
        "missing": [_rom_json(r) for r in cut(result.missing)],
    }
