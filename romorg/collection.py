"""A ROM *root* as one collection: find the system folders in it and merge the global rules with a system's own.

The root is only read. Each enabled system is scanned, planned with its effective rules and exported (``libexport``)
to ``<destination>/<its folder name>/``. This module holds the pure parts; the server runs the jobs.
"""

from __future__ import annotations

import dataclasses
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from . import library, scanner

__all__ = ["ALIASES", "GLOBAL_KEYS", "detect_systems", "effective_profile", "clean_global", "check_folders", "Sys", "RootScan",
           "Mapper", "virtual_flat", "virtual_disc"]

# Folder names (casefolded) beyond the platform's ``folder_hint`` that frontends use for the same system.
ALIASES = {
    "megadrive": ("genesis", "md", "megadrive", "sega genesis", "mega drive"),
    "snes": ("sfc", "superfamicom", "super nintendo", "snes"),
    "nes": ("famicom", "nes"),
    "gb": ("gameboy", "game boy"),
    "gbc": ("gameboycolor", "game boy color"),
    "gba": ("gameboyadvance", "game boy advance"),
    "nds": ("ds",),
    "n64": ("nintendo64",),
    "gc": ("gamecube", "ngc"),
    "psx": ("ps1", "playstation", "psone"),
    "ps2": ("playstation2",),
    "dreamcast": ("dc",),
    "mastersystem": ("sms", "master system"),
    "gamegear": ("gg", "game gear"),
    "sega32x": ("32x",),
    "atarilynx": ("lynx",),
    "amiga": ("amiga500", "amiga1200"),
}

# The rule fields a collection can set for every system (the rest stay each system's default).
GLOBAL_KEYS = ("exclude", "latest_only", "best_variant", "complete_only", "languages", "keep_flags", "rescue_only_dump",
               "region_priority", "one_per_game", "borrow_other_editions", "keep_other_language", "min_rating", "top_n", "min_votes",
               "keep_unrated", "rank_scope")
_CAPABILITY_FLAGS = ("latest_only", "best_variant", "complete_only", "one_per_game", "borrow_other_editions")


def _fold(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def detect_systems(root: Path, platforms: Iterable[Any]) -> List[dict]:
    """One entry per platform: ``{platform, hint, path, found}``. ``path`` is the sub-folder of ``root`` that looks like
    that system (by the platform's folder hint or a known alias, ignoring case, spaces and dashes), or ``root/hint``."""
    try:
        subs = sorted((p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=lambda p: p.name.casefold())
    except OSError:
        subs = []
    by_name = {}
    for p in subs:
        by_name.setdefault(_fold(p.name), p)
    out = []
    for plat in platforms:
        hint = getattr(plat, "folder_hint", "") or ""
        # the short name, the usual aliases, and the system's full name (a folder named like its No-Intro / Redump system)
        names = [_fold(hint)] + [_fold(a) for a in ALIASES.get(hint, ())] + [_fold(plat.name)]
        found = next((by_name[n] for n in names if n and n in by_name), None)
        out.append({"platform": plat.name, "hint": hint, "path": str(found if found else Path(root) / hint),
                    "found": found is not None})
    return out


def clean_global(values: Mapping[str, Any]) -> dict:
    """The global rule fields, normalised through the profile class (unknown names and values are dropped)."""
    if not isinstance(values, Mapping):
        return {}
    probe = library.LibraryProfile.from_dict({k: values[k] for k in GLOBAL_KEYS if k in values})
    full = probe.to_dict()
    out = {}
    for key in GLOBAL_KEYS:
        if key in values:
            out[key] = full[key]
    return out


def effective_profile(platform: Any, global_rules: Optional[Mapping[str, Any]]) -> "library.LibraryProfile":
    """The system's defaults with the collection's global rules on top. A rule that does not exist for the system
    (no latest-version DAT, no languages ...) is not switched on."""
    base = library.default_profile(platform)
    g = dict(global_rules or {})
    for key in _CAPABILITY_FLAGS:
        if key in g:
            g[key] = bool(g[key]) and bool(getattr(base, key))
    if "languages" in g and not base.languages:
        del g["languages"]
    return library.LibraryProfile.from_dict(g, base)


def check_folders(root: Path, dest: Path) -> Optional[str]:
    """An error text when the destination and the collection root overlap, else ``None``."""
    a, b = os.path.normcase(os.path.realpath(root)), os.path.normcase(os.path.realpath(dest))
    if a == b:
        return "The destination is the ROM root itself. Choose another folder."
    if b.startswith(a + os.sep):
        return "The destination is inside the ROM root. Choose a folder outside it."
    if a.startswith(b + os.sep):
        return "The ROM root is inside the destination. Choose a folder that does not contain it."
    return None


# --------------------------------------------------------------------------- the scan of a whole root
@dataclass
class Sys:
    """One system found in the root: its files, wherever they lie, and where its folder is / will be."""
    platform: Any
    std: Path                                   # <root>/<standard short name>
    current: Optional[Path] = None              # an existing folder of this system under another name (or the same)
    dats: List[Any] = field(default_factory=list)
    matches: List[Any] = field(default_factory=list)          # scanner.Match: the files (cartridge / flat systems)
    units: List[Any] = field(default_factory=list)            # discsys.DcUnit: identified discs (disc systems)
    unmatched: List[Any] = field(default_factory=list)        # scanner.Entry / DcUnit of this system's folder that match nothing
    index: Any = None                                         # discsys.DcIndex (disc systems)

    @property
    def name(self) -> str:
        return self.platform.name

    def games(self) -> int:
        if self.units:
            return len({u.game.name for u in self.units if u.game is not None})
        return len(scanner._covered_names(self.matches))

    def files(self) -> int:
        return len(self.units) if self.units else len({m.entry.path for m in self.matches})


@dataclass
class RootScan:
    """What one scan of the ROM root found. Everything the preview and the builds need is derived from this, without reading
    the files again."""
    root: Path
    systems: Dict[str, Sys] = field(default_factory=dict)
    unmatched: set = field(default_factory=set)               # ROM-like files that match nothing
    other: set = field(default_factory=set)                   # files that are not ROMs (pictures, text ...)
    entries: Dict[Any, Any] = field(default_factory=dict)     # path -> scanner.Entry of the files that match nothing
    ambiguous: set = field(default_factory=set)               # files that match several systems
    notes: List[str] = field(default_factory=list)
    files: int = 0
    bytes: int = 0
    seconds: float = 0.0
    at: float = field(default_factory=time.time)

    def flat(self) -> Dict[Path, Optional[str]]:
        out: Dict[Path, Optional[str]] = {}
        for s in self.systems.values():
            for m in s.matches:
                out[m.entry.path] = s.name
        for p in self.ambiguous:
            out[p] = None
        return out

    def discs(self) -> List[dict]:
        return [{"platform": s.name, "top": Path(u.top), "folder": u.folder is not None, "files": [Path(f) for f in u.files]}
                for s in self.systems.values() for u in s.units]


class Mapper:
    """Where a file will be after the folders are renamed and the files sorted: ``renames`` (old folder -> standard folder),
    ``moves`` (file -> new place, keys are the paths AFTER the renames), ``folder_moves`` (a game folder -> its new place)."""

    def __init__(self, renames: Iterable[Tuple[Path, Path]] = (), moves: Optional[Dict[Path, Path]] = None,
                 folder_moves: Iterable[Tuple[Path, Path]] = ()) -> None:
        self.renames = [(Path(a), Path(b)) for a, b in renames if a != b]
        self.moves = dict(moves or {})
        self.folder_moves = [(Path(a), Path(b)) for a, b in folder_moves]

    @staticmethod
    def _inside(p: Path, folder: Path) -> bool:
        return p == folder or folder in p.parents

    def renamed(self, p: Path) -> Path:
        for old, new in self.renames:
            if self._inside(p, old):
                return new / p.relative_to(old)
        return p

    def __call__(self, p: Path) -> Path:
        p = self.renamed(Path(p))
        if p in self.moves:
            return self.moves[p]
        for old, new in self.folder_moves:
            if self._inside(p, old):
                return new / p.relative_to(old)
        return p

    def unrenamed(self, p: Path) -> Path:
        """The reverse of the renames (to ask the disk about a path that does not exist yet)."""
        for old, new in self.renames:
            if self._inside(p, new):
                return old / p.relative_to(new)
        return p


def _tail(matched: list, dats: list) -> Tuple[list, int, Dict[str, int]]:
    """``(missing roms, total units, units per DAT)`` for a result, as ``scanner.scan`` computes them."""
    covered = scanner._covered_names(matched)
    seen: set = set()
    missing = []
    totals: Dict[str, int] = {}
    for d in dats:
        units = set()
        for r in d.roms:
            k = scanner._rom_key(r)
            units.add(k)
            if k not in covered and k not in seen:
                seen.add(k)
                missing.append(r)
        totals[d.name] = len(units)
    return missing, sum(totals.values()), totals


def virtual_flat(sys_: Sys, mapper: Mapper, layout: str, root: Path) -> "scanner.ScanResult":
    """The scan result of this system as it will look once its files are in place: the same matches under their new paths, so
    the library plan can be made now. ``root`` is the system's folder (its standard name, unless that name is taken)."""
    matched = [dataclasses.replace(m, entry=dataclasses.replace(m.entry, path=mapper(m.entry.path), root=root))
               for m in sys_.matches]
    unmatched = [dataclasses.replace(e, path=mapper(e.path), root=root) for e in sys_.unmatched]
    missing, total, totals = _tail(matched, sys_.dats)
    return scanner.ScanResult(root, [d.name for d in sys_.dats], matched, unmatched, [], [], missing, total, totals,
                              layout=layout, dats=list(sys_.dats))


def virtual_disc(sys_: Sys, mapper: Mapper, root: Path) -> Any:
    """The same for a disc system: the units under their new paths."""
    from . import discsys

    def unit(u: Any) -> Any:
        return dataclasses.replace(u, path=mapper(u.path), folder=(mapper(u.folder) if u.folder is not None else None),
                                   files=[mapper(f) for f in u.files])

    units = [unit(u) for u in sys_.units]
    matched = []
    for u in units:
        e = discsys.DcEntry(u.path, None, u.size, "", u.sha1 or None, root, kind=u.kind)
        matched.append(discsys.DcMatch(e, list(u.game.tracks) + ([u.game.cue] if u.game.cue else []), unit=u, level=u.level, kind=u.kind))
    bad = []
    for e in sys_.unmatched:                       # discs of this system's folder that no DAT knows
        bad.append(discsys.DcEntry(mapper(e.path), None, e.size, "", e.sha1, root, reason=e.reason, kind=e.kind,
                                   needs_chdman=e.needs_chdman))
    have = {m.unit.game.name for m in matched}
    index = sys_.index
    missing = [g.rep for g in index.games.values() if g.name not in have]
    dat = sys_.dats[0]
    return discsys.DcScanResult(root=root, dat_names=[dat.name], matched=matched, unmatched=bad, unsupported=[], errors=[],
                                missing=missing, dat_total=len(index.games), dat_totals={dat.name: len(index.games)},
                                layout="game_folder", dats=list(sys_.dats), units=units, index=index, junk=[], originals=[],
                                system=index.system)
