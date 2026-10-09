"""Tell a Switch file from what is in it and find its game in the catalogue (the "DAT" made from the title database).

There are no checksums to match against (the dumps are huge and the eShop files encrypted), so a file is matched by its *title ID*
(``switchscan.identify``, a few kilobytes of the file). The catalogue has the games, every update version of them and their add-ons, each as its own entry (three "DATs"), so a game, its
updates and its add-ons are told apart and the library rules (latest update only, duplicates ...) work on them as on any system.
A file the database does not know (a demo, an unreleased title, an update version it does not list) matches nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import switchscan
from .switchdb import SwitchDb

__all__ = ["make_matcher"]


def make_matcher(roms: Iterable[Any], header_key: Optional[bytes] = None) -> Callable[[Path], Optional[List[Any]]]:
    """``match(path)``: the catalogue rom (one) of the Switch file at ``path``, or None. A game or an add-on matches by its title ID,
    an update by its title ID and version (an update whose version the database does not list matches nothing)."""
    by_name: Dict[str, Any] = {}
    for rom in roms:
        by_name.setdefault(rom.game, rom)

    def match(path: Path) -> Optional[List[Any]]:
        db = SwitchDb()
        try:
            info = switchscan.identify(path, db, header_key)
            if not info.title_id:
                return None
            label = db.label_of(info.title_id if info.kind == "addon" else info.base_id, info.version or 0, info.kind)
        finally:
            db.close()
        rom = by_name.get(label) if label else None
        return [rom] if rom is not None else None

    return match
