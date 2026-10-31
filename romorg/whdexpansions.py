"""Expansions of an Amiga game that its WHDLoad install takes in with the game, and the data / mission disks with an install of their own.

The user's rule: when a game's WHDLoad install is ONE package for the base game and an expansion (the Install script asks "Do you want to
install the Expansion Disk?", offers "The Payoff data disk" among its options, names a version "Standard + Mission Disk Release"), the
expansion is not a game of its own: the library keeps it with its game ("installed with ..."), the build runs the install ONCE with the
expansion's disks after the game's and the expansion's questions answered yes, and one archive plays the game with the expansion. The
files of the expansion stay (nothing is deleted).

``whdexpansions.json`` (shipped; made by ``tools/gen_whdexpansions.py`` from the install packages - it checks that each package asks what
the table says it asks) holds:

* ``merge``: per install package, the base game's TOSEC title(s), the expansions it takes (their TOSEC titles; ``asks``: the forced answers
  - see :class:`romorg.amigainstall.Ask`) and what the install needs besides (``save_disk``: a blank disk to save to after the game's;
  ``base_pick``: a version of the game whose disks the slave is known to boot, tried first; ``assume``: the shipped list has no working
  result for the game alone from the disks at hand, but with the expansion the install was seen to run and boot);
* ``standalone``: the expansions that have an install of their own (``KickOff2ReturnToEurope.lha``) - and the install of one disk of a
  package that holds two (Bloodwych's Extended Levels): the library keeps them as games of their own, built from their disks alone.

Only expansions seen to install and boot with their game are listed (conservative: an expansion nobody has tried stays a game of its own).
"""

from __future__ import annotations

import importlib.resources
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Optional

from .amigainstall import Ask

__all__ = ["norm", "Expansion", "Merge", "merges", "merge_for_base", "merge_for_expansion", "standalone_row", "asks_for", "table"]



def norm(title: str) -> str:
    """A TOSEC title as the table keys it: lower case, letters and digits only (``Battle Isle '93`` = ``battleisle93``)."""
    return re.sub(r"[^0-9a-z]+", "", (title or "").casefold())


@dataclass(frozen=True)
class Expansion:
    titles: tuple[str, ...]               # its TOSEC titles (one expansion may be dumped under two)
    asks: tuple[dict, ...]                # the forced answers: ``{"kind", "prompt", "choice"}``

    @property
    def keys(self) -> frozenset:
        return frozenset(norm(t) for t in self.titles)

    def forced(self, want: bool) -> tuple[Ask, ...]:
        return tuple(Ask(kind=a["kind"], prompt=a.get("prompt", ""), choice=a.get("choice", ""), expansion=self.titles[0], want=want)
                     for a in self.asks)


@dataclass(frozen=True)
class Merge:
    archive: str                          # the install package that takes the base game and the expansions
    base: tuple[str, ...]                 # the base game's TOSEC titles
    expansions: tuple[Expansion, ...]
    save_disk: bool = False               # a blank 901,120-byte disk between the game's disks and the expansion's (HeroQuest)
    base_pick: str = ""                   # regex on the DAT names of a version of the game this install is known to boot
    assume: bool = False                  # the install takes the game WITH the expansion from the library's kept disks (seen to install and
                                          # boot) though the shipped list has no result for the game alone from them (Battle Isle's ADFs)

    @property
    def base_keys(self) -> frozenset:
        return frozenset(norm(t) for t in self.base)


def table() -> dict[str, Any]:
    return _table()


@lru_cache(maxsize=1)
def _table() -> dict[str, Any]:
    try:
        data = json.loads(importlib.resources.files(__package__).joinpath("whdexpansions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"merge": [], "standalone": []}
    return data if isinstance(data, dict) else {"merge": [], "standalone": []}


@lru_cache(maxsize=1)
def merges() -> tuple[Merge, ...]:
    out = []
    for m in _table().get("merge") or []:
        exps = tuple(Expansion(titles=tuple(e["titles"]), asks=tuple(e.get("asks") or ())) for e in m.get("expansions") or [])
        if m.get("archive") and m.get("base") and exps:
            out.append(Merge(archive=m["archive"], base=tuple(m["base"]), expansions=exps, save_disk=bool(m.get("save_disk")),
                             base_pick=m.get("base_pick", ""), assume=bool(m.get("assume"))))
    return tuple(out)


@lru_cache(maxsize=1)
def _by_base() -> dict[str, list[Merge]]:
    out: dict[str, list[Merge]] = {}
    for m in merges():
        for k in m.base_keys:
            out.setdefault(k, []).append(m)
    return out


@lru_cache(maxsize=1)
def _by_expansion() -> dict[str, list[tuple[Merge, Expansion]]]:
    out: dict[str, list[tuple[Merge, Expansion]]] = {}
    for m in merges():
        for e in m.expansions:
            for k in e.keys:
                out.setdefault(k, []).append((m, e))
    return out


def merge_for_base(title: str) -> list[Merge]:
    """The installs that take expansions in with the game of this TOSEC title."""
    return _by_base().get(norm(title), [])


def merge_for_expansion(title: str) -> list[tuple[Merge, Expansion]]:
    """The installs (with the expansion) that take the expansion of this TOSEC title in."""
    return _by_expansion().get(norm(title), [])


@lru_cache(maxsize=1)
def _standalone() -> dict[str, dict[str, Any]]:
    return {norm(r["title"]): r for r in _table().get("standalone") or [] if r.get("title") and r.get("archive")}


def standalone_row(title: str) -> Optional[dict[str, Any]]:
    """``{title, archive, level, asks?, prefer_disk?}`` of the expansion with an install of its own (None: this title has none)."""
    return _standalone().get(norm(title))


def asks_for(merge: Merge, present: Iterable[Expansion]) -> tuple[Ask, ...]:
    """The forced answers for an install of ``merge`` with the expansions ``present``: yes to theirs, no to the others' (an expansion that
    is not among the disks)."""
    have = {e.titles for e in present}
    return tuple(a for e in merge.expansions for a in e.forced(e.titles in have))
