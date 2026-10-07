"""A ROM *root* as one collection: find the system folders in it and merge the global rules with a system's own.

The root is only read. Each enabled system is scanned, planned with its effective rules and exported (``libexport``)
to ``<destination>/<its folder name>/``. This module holds the pure parts; the server runs the jobs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

from . import library

__all__ = ["ALIASES", "GLOBAL_KEYS", "detect_systems", "effective_profile", "clean_global", "check_folders"]

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
        names = [_fold(hint)] + [_fold(a) for a in ALIASES.get(hint, ())]
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
