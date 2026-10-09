"""Tell a Wii disc image from its header when its checksum cannot be had, and find the Redump game it is.

Redump hashes the original ``.iso``. A Wii disc in RVZ / WIA / WBFS / CISO is not that image (its encrypted partitions are stored
decrypted) and cannot be turned back into it without a rebuild of the whole disc, so these files are *identified*, not verified:
the 6-character game ID in the header gives the title (GameTDB; the header's own title when GameTDB is not installed), its 4th
character the region, its revision and disc number the edition. The Redump game with that title, a region that includes the
region, the revision and the disc number is the match. A game several Redump entries fit (language variants) takes the first by
name. A plain ``.iso`` is hashed as ever and uses this only when its hash matches nothing (a scrubbed or trimmed copy).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import gametdb, nintendodisc

__all__ = ["ID_EXTS", "DiscNameIndex", "norm_title", "regions_of", "make_matcher"]

# the containers whose checksum is not the disc's (an .iso that matches nothing is tried too)
ID_EXTS = (".rvz", ".wia", ".wbfs", ".ciso", ".wux", ".wud")
_TAG = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_REV = re.compile(r"^(?:Rev|v)\s*(\d+)\b", re.IGNORECASE)
_DISC = re.compile(r"^Disc\s*(\d+)", re.IGNORECASE)
_ID_REGION: Dict[str, Set[str]] = {
    "E": {"usa", "world", "canada"}, "J": {"japan", "asia", "world"}, "K": {"korea", "asia"}, "W": {"taiwan", "asia", "china", "hong kong"},
    "U": {"australia", "europe", "world"}, "R": {"russia", "europe"}, "D": {"germany", "europe", "world"}, "F": {"france", "europe", "world"},
    "S": {"spain", "europe", "world"}, "I": {"italy", "europe", "world"}, "H": {"netherlands", "europe", "world"},
    "Q": {"korea", "asia"}, "N": {"usa"}, "L": {"japan"}, "M": {"europe"},
}
for _c in "PXYZ":
    _ID_REGION[_c] = {"europe", "world", "australia"}


def norm_title(name: str) -> str:
    """A title as compared: no tags, no punctuation, no case, ``Legend of Zelda, The`` = ``The Legend of Zelda``."""
    text = _TAG.sub(" ", name).strip()
    m = re.match(r"^(.*?),\s*(The|A|An)(\s+-\s+.*|:.*)?$", text, re.IGNORECASE)
    if m:
        text = f"{m.group(2)} {m.group(1)}{m.group(3) or ''}"
    text = re.sub(r"^(the|a|an)\s+", "", text.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "", text.lower().replace("&", "and"))


def regions_of(name: str) -> Set[str]:
    """The regions in the first tag of a Redump name that names one (``(USA, Europe)``)."""
    known = set().union(*_ID_REGION.values()) | {"usa", "europe"}
    for m in _TAG.finditer(name):
        parts = {p.strip().lower() for p in (m.group(1) or "").split(",")}
        if parts & known:
            return parts
    return set()


def _tag_number(name: str, pattern: "re.Pattern[str]") -> Optional[int]:
    for m in _TAG.finditer(name):
        hit = pattern.match((m.group(1) or "").strip())
        if hit:
            return int(hit.group(1))
    return None


class DiscNameIndex:
    """The games of a Redump DAT by normalised title."""

    def __init__(self, roms: Iterable[Any]) -> None:
        self.by_title: Dict[str, List[Tuple[Any, Set[str], int, int]]] = {}
        self.by_game: Dict[str, Any] = {}
        seen: Set[str] = set()
        for rom in roms:
            if rom.game in seen:
                continue
            seen.add(rom.game)
            self.by_game[rom.game] = rom
            disc = _tag_number(rom.game, _DISC)
            self.by_title.setdefault(norm_title(rom.game), []).append(
                (rom, regions_of(rom.game), _tag_number(rom.game, _REV) or 0, (disc or 1) - 1))
        for items in self.by_title.values():
            items.sort(key=lambda t: t[0].game)

    def find(self, titles: Iterable[str], region_char: str, revision: int, disc: int) -> Optional[Any]:
        want = _ID_REGION.get(region_char.upper(), set())
        for title in titles:
            items = self.by_title.get(norm_title(title)) if title else None
            if not items:
                continue
            fit = [t for t in items if (not want or not t[1] or t[1] & want) and t[3] == disc]
            if not fit:
                continue
            exact = [t for t in fit if t[2] == revision]
            return (exact or fit)[0][0]
        return None


def make_matcher(roms: Iterable[Any]) -> Callable[[Path], Optional[List[Any]]]:
    """``match(path)``: the DAT rom list (one rom) of the Wii or Wii U disc image at ``path``, or None."""
    roms = list(roms)
    # a title can be on two consoles (Resident Evil 4: GameCube and Wii), and a Collection scan has every system's DATs: a disc is
    # only looked up among the games of its own console's DAT (all of them when that DAT is not among them: a test's own DAT)
    own = {kind: [r for r in roms if getattr(r, "dat", "") == dat] for kind, dat in (("wii", "Nintendo - Wii"), ("wiiu", "Nintendo - Wii U"))}
    indexes = {kind: DiscNameIndex(items or roms) for kind, items in own.items()}

    def match(path: Path) -> Optional[List[Any]]:
        wiiu = path.suffix.lower() in nintendodisc.WIIU_EXTS
        info = nintendodisc.read_wiiu(path) if wiiu else nintendodisc.read_disc(path)
        if info is None or info.kind not in ("wii", "wiiu"):
            return None
        kind = info.kind
        index = indexes[kind]
        tdb = gametdb.GameTdb()
        try:
            titles = [tdb.name(kind, info.game_id), info.name]
            label = tdb.disc_label(info.game_id) if kind == "wiiu" else ""
        finally:
            tdb.close()
        if label and label in index.by_game:                   # a Wii U disc: GameTDB's list is the catalogue, the name is the entry
            return [index.by_game[label]]
        if not any(titles):
            return None
        rom = index.find(titles, info.game_id[3:4], info.revision, info.disc)
        return [rom] if rom is not None else None

    return match
