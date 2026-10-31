"""Saves lying in a ROM folder, pinned to the game they belong to.

A save, a save state or a memory card in a system's folder (not in RetroArch's own save folders) is not a ROM, so a library build sets it
aside. This module tells *which game it belongs to*, so it can be set aside under that game: ``<archive>/<system>/Saves/<Title>/<file>``.

A file is pinned to a title when

* its name is the name of a game of the system's DATs (or of one of its ROMs): ``Game (USA).srm``, ``Game (USA).state1``,
  Flycast's ``Game (USA).A1.bin`` ... (the longest name that is one wins, as everywhere else saves are matched), or
* it is a memory card (:mod:`romorg.memcards`) that names its games: a GameCube card by the game codes of its saves (the GameCube
  database has them), a Dreamcast card by the title of a save when exactly one title of the DAT is called that.

A card of several games is pinned to each of them (the build moves the card under the first and copies it under the others).
Titles are the library's titles (``Spider - The Video Game``: no region, version or disc), so the editions of a game share one folder.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import memcards, saveindex
from .retroarch import is_save_suffix
from .folders import SAVES_DIR

__all__ = ["Pinner", "is_save", "destination"]

_NOT_SAVES = saveindex._NOT_SAVES            # (pictures, notes, archives: never a save, whatever their name)


def is_save(name: str) -> bool:
    """True when ``name`` looks like a save, a state or a memory card of an emulator (``Game.srm``, ``Game.state1``, ``Game.A1.bin`` ...)."""
    return saveindex.content_name(name) is not None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def destination(root: Path, title: str, name: str) -> Path:
    """``root/Saves/<title>/<name>``: the title made safe as a folder name."""
    from .organiser import safe_filename
    return root / SAVES_DIR / (safe_filename(title) or "Unknown") / name


class Pinner:
    """Pins save files to titles of the given DATs. Cheap to make; the tables are built on first use and the answers remembered."""

    def __init__(self, dats: Iterable[Any]) -> None:
        self.dats = list(dats or ())
        self._by_name: Optional[Dict[str, Tuple[Any, str]]] = None
        self._by_norm: Optional[Dict[str, set]] = None
        self._by_code: Optional[Dict[str, set]] = None
        self._titles: Dict[Tuple[str, str, str], str] = {}
        self._done: Dict[str, List[str]] = {}
        self._listing: Dict[str, List[str]] = {}

    # ---- the tables
    def _names(self) -> Dict[str, Tuple[Any, str]]:
        if self._by_name is None:
            table: Dict[str, Tuple[Any, str]] = {}
            for dat in self.dats:
                dname = getattr(dat, "name", "")
                for rom in dat.roms:
                    for n in (rom.game, rom.set_name, os.path.splitext(rom.name)[0]):
                        if n:
                            table.setdefault(n, (rom, dname))
            self._by_name = table
        return self._by_name

    def title_of(self, rom: Any, dat: str) -> str:
        from . import library
        key = (dat, rom.game, rom.name)
        if key not in self._titles:
            self._titles[key] = library.rom_title(rom, dat)
        return self._titles[key]

    def _all_titles(self) -> Dict[str, set]:
        if self._by_norm is None:
            table: Dict[str, set] = {}
            seen = set()
            for dat in self.dats:
                for rom in dat.roms:
                    if (rom.game, dat.name) in seen:
                        continue
                    seen.add((rom.game, dat.name))
                    t = self.title_of(rom, dat.name)
                    table.setdefault(_norm(t), set()).add(t)
            self._by_norm = table
        return self._by_norm

    def _codes(self) -> Dict[str, set]:
        """GameCube / Wii game codes -> titles, from the serials of the DAT (``DL-DOL-GALE-USA``)."""
        if self._by_code is None:
            table: Dict[str, set] = {}
            for dat in self.dats:
                for rom in dat.roms:
                    parts = (rom.serial or "").split("-")
                    if len(parts) >= 3 and len(parts[2]) == 4:
                        table.setdefault(parts[2], set()).add(self.title_of(rom, dat.name))
            self._by_code = table
        return self._by_code

    # ---- the answer
    def titles(self, path: Path) -> List[str]:
        """The titles the save file ``path`` belongs to (``[]``: not pinned), the first being the main one."""
        key = os.fspath(path)
        if key not in self._done:
            name = os.path.basename(key)
            if saveindex.content_name(name) is None and (os.path.splitext(name)[1].lower() in _NOT_SAVES
                                                         or not memcards.sniff_size(self._size(key))):
                self._done[key] = []                       # (named like no save, never one by its kind or the size of a card: most files
                return []                                  # of a big folder, found without a path object, a second look or a siblings listing)
            found = self._find(Path(path))
            if not found:                                  # the other files of its save set (the empty cards B, C, D of a game) go where
                found = self._siblings(Path(path))         # the one that says which game it is goes
            self._done[key] = found
        return list(self._done[key])

    def _siblings(self, path: Path) -> List[str]:
        """The titles of another file of the same save set in the same folder (same content name: ``MK-51000.A1.bin`` for
        ``MK-51000.B1.bin``) that is pinned on its own, else ``[]``."""
        cn = saveindex.content_name(path.name)
        if cn is None:
            return []
        folder = os.fspath(path.parent)
        if folder not in self._listing:                # (one look at a folder, however many of its files ask)
            try:
                self._listing[folder] = sorted(os.listdir(folder))
            except OSError:
                self._listing[folder] = []
        prefix = cn[0] + "."
        others = [path.parent / n for n in self._listing[folder] if n.startswith(prefix) and n != path.name]
        for other in others[:64]:
            ocn = saveindex.content_name(other.name)
            if ocn is not None and ocn[0] == cn[0]:
                key = os.fspath(other)
                if key not in self._done:
                    self._done[key] = self._find(other)
                if self._done[key]:
                    return list(self._done[key])
        return []

    def _find(self, path: Path) -> List[str]:
        name = path.name
        if saveindex.content_name(name) is None and not memcards.sniff_size(self._size(path)):
            return []                                               # neither named like a save nor the size of a card
        table = self._names()
        for head, rest in saveindex._cuts(name):                    # the longest name that is a game's, the rest a core's save suffix
            hit = table.get(head)
            if hit is not None and (is_save_suffix(rest) or saveindex._STATE.match(rest)):
                return [self.title_of(hit[0], hit[1])]
        card = memcards.read(path)
        if card is None or not card.entries:
            cn = saveindex.content_name(name)             # no card to ask: the name, read loosely, when it is ONE title of the DAT
            if cn is None:
                return []
            dat = next((getattr(d, "name", "") for d in self.dats), "")
            title = saveindex.loose_pick(self._all_titles(), cn[0], dat)
            return [title] if title else []
        out: List[str] = []
        for e in card.entries:
            found: set = set()
            if card.kind == memcards.GAMECUBE and e.code:
                found = self._codes().get(e.code, set())
            elif card.kind == memcards.DREAMCAST:
                t = saveindex.pick_title(self._all_titles(), e.title.split(" / ")[0].split(" (")[0])
                found = {t} if t else set()
            if len(found) == 1:
                t = next(iter(found))
                if t not in out:
                    out.append(t)
        return out

    @staticmethod
    def _size(path: Any) -> int:
        try:
            return os.stat(path).st_size
        except OSError:
            return 0
