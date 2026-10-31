"""The tiles at the top of every system page: which ones exist, which ones are shown, in what order (Settings > System page tiles).

The page draws each tile from data it already has (the scan, the library totals, the preview) or, for the ones marked ``calc``, from
``GET /api/library/insights``. The choice is one list for every system, kept in ``config.json`` as ``page_tiles`` (shown tiles, in order).
A saved list may name tiles that are gone (the old page drew them): they drop out when it is read.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

__all__ = ["MAX_TILES", "CATALOG", "DEFAULT", "CALC", "known", "shown", "normalise"]

MAX_TILES = 10

# id, label, what it says, needs a calculation of its own (insights)
CATALOG: tuple[dict[str, Any], ...] = (
    {"id": "have_all", "label": "Have, all entries", "text": "How many of the database's games you own, whatever your rules say.", "calc": False},
    {"id": "have_rules", "label": "Have, with your rules", "text": "How many of the games your library rules keep you own.", "calc": False},
    {"id": "needs_look", "label": "Needs a look", "text": "Games you own that are not the preferred version, are left out by your rules or are incomplete; each opens its list.", "calc": False},
    {"id": "files_to_change", "label": "Files to change", "text": "How many files the library build would move, rename or set aside (from the live preview).", "calc": False},
    {"id": "unmatched", "label": "Unmatched files", "text": "Files in the folder that match nothing in the database. Hidden when there are none.", "calc": False},
    {"id": "bios", "label": "BIOS and firmware", "text": "BIOS and firmware files found among your files. Hidden when there are none.", "calc": False},
    {"id": "saves", "label": "Saves", "text": "Saves and save states found for the system. Hidden without a save source.", "calc": False},
    {"id": "database", "label": "Database", "text": "The version of the system's database and whether it is up to date.", "calc": False},
    {"id": "last_scan", "label": "Last scan", "text": "When the folder was last scanned.", "calc": False},
    {"id": "convert", "label": "Convert", "text": "Games that could be converted to CHD (disc systems only).", "calc": True},
)
DEFAULT: tuple[str, ...] = ("have_all", "have_rules", "needs_look", "files_to_change", "unmatched", "bios", "saves")
# what GET /api/library/insights works out: the Convert tile, and numbers no tile shows any more (the server still has them)
CALC = frozenset({"convert", "space", "best_missing", "avg_rating", "regions"})
_IDS = tuple(t["id"] for t in CATALOG)


def known(tile_id: Any) -> bool:
    return isinstance(tile_id, str) and tile_id in _IDS


def normalise(raw: Any) -> list[str]:
    """The known tile ids of ``raw`` once each, in order, at most :data:`MAX_TILES`."""
    out: list[str] = []
    for t in raw if isinstance(raw, (list, tuple)) else ():
        if known(t) and t not in out:
            out.append(t)
    return out[:MAX_TILES]


def shown(cfg: Any) -> list[str]:
    """The tiles to show (the default set until the user chose; an empty list is a choice too)."""
    raw = cfg.get("page_tiles") if isinstance(cfg, dict) else None
    return normalise(raw) if isinstance(raw, list) else list(DEFAULT)


def full_order(cfg: Any) -> list[str]:
    """Every tile: the shown ones first, in their order, then the others in the catalogue's order (the Settings list)."""
    on = shown(cfg)
    return on + [t for t in _IDS if t not in on]
