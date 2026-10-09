"""The save index of one scan (Amendment 31): which RetroArch saves and states exist for the games of a system.

It exists only when a RetroArch config is known (``server.App._saves_report``): without one nothing here runs, and the app
does not know that saves exist.

``build`` takes the files of the save folders (one walk, ``retroarch.SaveWalk``) and the games of a scan (the rows of the
Browse "Games" list: DAT name, title, the local files) and groups the files into *save sets*: every file of one content
name (``Game (USA).srm``, ``Game (USA).state1``, ``Game (USA).state1.png``, Flycast's ``Game (USA).A1.bin`` ... all belong to
``Game (USA)``). Each set is matched to a title:

* ``rom``: the content name is the name of a file you have (a ROM, a zip, a CHD; for a zip also the member's name) ->
  the DAT game of that file and its title ("Super Mario World");
* ``dat``: no such file, but the name is a game / set name of a DAT -> the title, "no ROM here";
* ``none``: neither.

A save file is a save (``.srm``, ``.eep``, a memory card ...), a state (``.state``, ``.state1``, ``.state.auto``) or a state's
screenshot (``.state1.png``). Screenshots are kept apart and never counted.

Names are compared without regard to case on Windows only (``retroarch.fold_name``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .retroarch import fold_name, is_save_suffix

__all__ = ["SaveSet", "SaveReport", "build", "kind_of", "content_name", "game_refs", "describe"]

_BAD = " ()"            # a core's suffix never has spaces or brackets (the ones in a game's name)
_STATE = re.compile(r"^\.state(\d*|\.auto)(\.png)?$", re.IGNORECASE)
_SHOT = re.compile(r"^\.state(\d*|\.auto)\.png$", re.IGNORECASE)
SAVE, STATE, SHOT = "save", "state", "shot"
ROM, DAT, NONE = "rom", "dat", "none"
# files that lie in a save folder but are no save, whatever they are called
_NOT_SAVES = frozenset((".png", ".jpg", ".jpeg", ".gif", ".txt", ".log", ".cfg", ".lpl", ".zip", ".md5", ".sha1", ".tmp", ".bak"))


def kind_of(suffix: str) -> str:
    """``shot`` for a state's screenshot, ``state`` for a state, else ``save``."""
    if _SHOT.match(suffix):
        return SHOT
    return STATE if _STATE.match(suffix) else SAVE


def _cuts(name: str) -> Iterator[Tuple[str, str]]:
    """``(content name, suffix)`` for every dot of ``name`` whose rest is a plain suffix (no spaces or brackets), the longest
    content name first."""
    cut = name.rfind(".")
    while cut > 0:
        rest = name[cut:]
        if not any(ch in rest for ch in _BAD):
            yield name[:cut], rest
        cut = name.rfind(".", 0, cut)


def content_name(name: str) -> Optional[Tuple[str, str]]:
    """``(content name, suffix)`` of a save file nothing else is known about: the first dot (from the left) whose rest is what
    a core writes after a game's name (``retroarch.is_save_suffix``). None when the file is not recognisable as a save."""
    for head, rest in reversed(list(_cuts(name))):
        if is_save_suffix(rest) or _STATE.match(rest):
            return head, rest
    return None


@dataclass
class SaveSet:
    """All the save files of one content name."""
    name: str                                   # as the files spell it
    key: str                                    # ``fold_name(name)``
    saves: int = 0
    states: int = 0
    shots: int = 0
    bytes: int = 0
    cores: List[str] = field(default_factory=list)     # the sub-folders of the save folder the files are in
    here: bool = False                          # some file is in a folder of a core that plays this system
    members: List[Tuple[Path, Path, str, int]] = field(default_factory=list)   # (file, save root, kind, size)
    match: str = NONE                           # rom | dat | none
    source: str = "retroarch"                   # which emulator's saves (retroarch: named after the ROM; the others are told by a game ID)
    label: str = ""                             # that emulator's name for the screen
    dat: str = ""
    game: str = ""                              # the DAT game (set) name
    title: str = ""

    @property
    def shown(self) -> str:
        """The emulator's name for the screen."""
        return self.label or ("RetroArch" if self.source == "retroarch" else self.source.capitalize())

    @property
    def renames(self) -> bool:
        """True when the saves are named after the ROM (RetroArch's): they follow a renamed ROM. The other emulators' saves are told
        by the game's ID and never change name."""
        return self.source == "retroarch"

    @property
    def files(self) -> int:
        """Saves and states (screenshots are not counted)."""
        return self.saves + self.states

    def public(self) -> Dict[str, Any]:
        return {"name": self.name, "match": self.match, "dat": self.dat, "game": self.game, "title": self.title or self.name,
                "files": self.files, "saves": self.saves, "states": self.states, "screenshots": self.shots,
                "bytes": self.bytes, "cores": list(self.cores), "source": self.source, "label": self.shown}


@dataclass
class SaveReport:
    """The result of :func:`build` for one scan."""
    sets: Dict[str, SaveSet] = field(default_factory=dict)       # by folded content name
    playlists: Dict[str, Tuple[str, List[str]]] = field(default_factory=dict)   # folded .m3u name -> (name, its discs' names)
    per_core: bool = False                                       # RetroArch keeps one sub-folder per core
    install: str = ""

    def playlist_of(self, disc_key: str) -> Optional[str]:
        """The folded name of the first playlist that lists the disc whose folded name is ``disc_key`` (None: no playlist)."""
        cache = getattr(self, "_pl", None)
        if cache is None:
            cache = {}
            for key, (_name, discs) in self.playlists.items():
                for d in discs:
                    cache.setdefault(fold_name(d), key)
            self._pl = cache
        return cache.get(disc_key)

    def counted(self) -> List[SaveSet]:
        """The sets that are this system's business: every matched one, and an unmatched one only when it lies in the folder of
        a core that plays the system (or the save folders are not sorted per core, and nothing says otherwise)."""
        return [s for s in self.sets.values() if s.files and (s.match != NONE or s.here or not self.per_core)]

    def totals(self) -> Dict[str, Any]:
        sets = self.counted()
        rom = [s for s in sets if s.match == ROM]
        dat = [s for s in sets if s.match == DAT]
        none = [s for s in sets if s.match == NONE]
        return {"files": sum(s.files for s in sets), "saves": sum(s.saves for s in sets), "states": sum(s.states for s in sets),
                "screenshots": sum(s.shots for s in sets), "bytes": sum(s.bytes for s in sets), "sets": len(sets),
                "rom_sets": len(rom), "dat_sets": len(dat), "unmatched_sets": len(none),
                "titles_rom": len({(s.title or s.game).casefold() for s in rom}),
                "titles_dat": len({(s.title or s.game).casefold() for s in dat}
                                  - {(s.title or s.game).casefold() for s in rom}),
                "per_core": self.per_core, "install": self.install,
                "sources": sorted({name for st in sets for name in st.shown.split(" + ")}) or ([self.install] if self.install else []),
                "renames": any(st.renames for st in sets) or bool(self.install)}

    def game_counts(self, dat: str, game: str) -> Optional[Dict[str, int]]:
        """``{saves, states, total}`` of the sets that belong to one DAT game (None: no save)."""
        return self._by_game().get((dat, game))

    def title_counts(self, title: str) -> Optional[Dict[str, int]]:
        return self._by_title().get(title.casefold()) if title else None

    def _by_game(self) -> Dict[Tuple[str, str], Dict[str, int]]:
        cache = getattr(self, "_g", None)
        if cache is None:
            cache = {}
            for s in self.counted():
                if s.match != NONE:
                    c = cache.setdefault((s.dat, s.game), {"saves": 0, "states": 0, "total": 0})
                    c["saves"] += s.saves
                    c["states"] += s.states
                    c["total"] += s.files
            self._g = cache
        return cache

    def _by_title(self) -> Dict[str, Dict[str, int]]:
        cache = getattr(self, "_t", None)
        if cache is None:
            cache = {}
            for s in self.counted():
                if s.match != NONE and s.title:
                    c = cache.setdefault(s.title.casefold(), {"saves": 0, "states": 0, "total": 0})
                    c["saves"] += s.saves
                    c["states"] += s.states
                    c["total"] += s.files
            self._t = cache
        return cache

    def keys(self) -> frozenset:
        """The content names (folded) there are saves for: what the selection looks games up in."""
        return frozenset(s.key for s in self.counted() if s.files)

    def rows(self) -> List[Dict[str, Any]]:
        """One row per set, matched ones first (the "Saves" list of the Browse tab)."""
        order = {ROM: 0, DAT: 1, NONE: 2}
        return [s.public() for s in sorted(self.counted(), key=lambda s: (order[s.match], (s.title or s.name).casefold(), s.name))]


def game_refs(rows: Iterable[Dict[str, Any]]) -> Tuple[Dict[str, Tuple[str, str, str]], Dict[str, Tuple[str, str, str]]]:
    """``(by file, by DAT name)``: from the "Games" rows of a scan (``dat``, ``name``, ``title``, ``have``, ``files`` as
    ``path`` or ``path::member``, ``roms``), the name of every file you have (and of a zip's member) -> ``(dat, game, title)``
    and the name of every game (and of its ROMs) -> the same. Folded names. The first game wins a name."""
    by_file: Dict[str, Tuple[str, str, str]] = {}
    by_dat: Dict[str, Tuple[str, str, str]] = {}
    for row in rows:
        ref = (row.get("dat") or "", row["name"], row.get("title") or "")
        by_dat.setdefault(fold_name(row["name"]), ref)
        for rom in row.get("roms") or ():
            by_dat.setdefault(fold_name(os.path.splitext(rom)[0]), ref)
        if row.get("have"):
            for rel in row.get("files") or ():
                path, _sep, member = rel.partition("::")
                by_file.setdefault(fold_name(Path(path.replace("\\", "/")).stem), ref)
                if member:
                    by_file.setdefault(fold_name(Path(member.replace("\\", "/")).stem), ref)
    return by_file, by_dat


def playlist_refs(playlists: Iterable[Tuple[str, List[str]]], by_file: Dict[str, Tuple[str, str, str]]) -> Dict[str, Tuple[str, List[str]]]:
    """The playlists (``(name, disc names)``) that belong to the games of ``by_file`` (the first disc is one of them), by folded name."""
    out: Dict[str, Tuple[str, List[str]]] = {}
    for name, discs in playlists:
        if discs and fold_name(discs[0]) in by_file:
            out.setdefault(fold_name(name), (name, list(discs)))
    return out


def build(entries: Iterable[Tuple[Path, Path, bool]], by_file: Dict[str, Tuple[str, str, str]],
          by_dat: Dict[str, Tuple[str, str, str]], per_core: bool = False, install: str = "",
          playlists: Iterable[Tuple[str, List[str]]] = ()) -> SaveReport:
    """Group the save files ``entries`` (``(file, save root, here)`` from ``retroarch.SaveWalk.for_platform``) into sets and
    match each set to a title (see the module's text). One pass over the files; every name is looked up in two dictionaries.

    ``playlists``: the ``.m3u`` files of the system as ``(name, disc names)``. RetroArch names the saves of a multi-disc game after
    the playlist it was started from, so a playlist is a name a game's saves can have: it belongs to the game of its first disc."""
    report = SaveReport(per_core=per_core, install=install)
    report.playlists = playlist_refs(playlists, by_file)
    if report.playlists:
        by_file = dict(by_file)
        for key, (_name, discs) in report.playlists.items():
            by_file.setdefault(key, by_file[fold_name(discs[0])])
    for f, root, here in entries:
        name = f.name
        pick: Optional[Tuple[str, str]] = None
        for head, rest in _cuts(name):                       # the longest name that is a file or a game of this system
            k = fold_name(head)
            if (k in by_file or k in by_dat) and rest.lower() not in _NOT_SAVES:
                pick = (head, rest)
                break
        if pick is None:
            pick = content_name(name)
            if pick is None:
                continue                                     # no save at all (a note, a picture ...)
        head, rest = pick
        key = fold_name(head)
        s = report.sets.get(key)
        if s is None:
            s = report.sets[key] = SaveSet(head, key)
            ref = by_file.get(key)
            s.match = ROM if ref is not None else DAT
            ref = ref or by_dat.get(key)
            if ref is None:
                s.match = NONE
            else:
                s.dat, s.game, s.title = ref
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        kind = kind_of(rest)
        if kind == SHOT:
            s.shots += 1
        elif kind == STATE:
            s.states += 1
        else:
            s.saves += 1
        s.bytes += size
        s.here = s.here or here
        try:
            parts = f.relative_to(root).parts
        except ValueError:
            parts = (name,)
        if len(parts) > 1 and parts[0] not in s.cores:
            s.cores.append(parts[0])
        s.members.append((f, root, kind, size))
    return report


def describe(counts: Optional[Dict[str, int]]) -> str:
    """``1 save · 6 states`` (empty for none)."""
    if not counts or not counts.get("total"):
        return ""
    bits = []
    if counts.get("saves"):
        bits.append(f"{counts['saves']} save{'' if counts['saves'] == 1 else 's'}")
    if counts.get("states"):
        bits.append(f"{counts['states']} state{'' if counts['states'] == 1 else 's'}")
    return " · ".join(bits)
