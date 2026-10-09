"""The saves of the emulators that tell a game by its ID, not by the ROM's name, as ``saveindex.SaveSet`` s.

Dolphin (GameCube, Wii), Cemu (Wii U), PCSX2 (PlayStation 2), Eden / Ryujinx (Switch): a save belongs to a game ID (the 4-character
game code, the title ID, the disc's serial). The ID of each game you have is read from the game's file; the saves with the same ID
become one set keyed like the game's file (so everything that works on a RetroArch save set - the counts on the pages, "keep the
ROM that has saves", "archive the saves with the ROM" - works on these too). They are never renamed with a ROM: their ID does not
change when the file does.

A set's ``members`` are what can be moved with the game: a save folder or a ``.gci`` file. What lives inside a memory card image
(PCSX2) cannot be taken out, so it is counted but has no member.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import nintendoapp, nintendodisc, nintendosaves, pcsx2, switchfmt, switchscan, switchsaves
from .saveindex import NONE, ROM, SaveSet

__all__ = ["Owned", "applies", "active", "config_key", "owned_games", "sets_for", "PLATFORM_SOURCES"]

# platform -> the console entry of ``nintendoapp`` that holds its emulator's folder
PLATFORM_SOURCES = {"Nintendo Switch": ("switch", ""), "Nintendo GameCube": ("dolphin", "wii"), "Nintendo Wii": ("dolphin", "wii"), "Nintendo Wii U": ("cemu", "wiiu"), "Sony PlayStation 2": ("pcsx2", "ps2")}
_IDS: Dict[Tuple[str, int, float], str] = {}


@dataclass
class Owned:
    """A game you have: the folded name of its file (the key everything else is looked up by), its DAT reference and a file."""
    key: str
    ref: Tuple[str, str, str]           # (dat, game, title)
    path: Path


def applies(platform: str) -> bool:
    """True when the platform's saves come from an ID-based emulator (RetroArch is not looked at for it)."""
    return platform in PLATFORM_SOURCES


def _data(platform: str, nin_cfg: Dict[str, Dict[str, str]], switch_eff: Optional[Dict[str, Any]] = None) -> str:
    """The emulator's data folder as used ("" when it is switched off or there is none); the Switch has two, joined by ``|``."""
    source, console = PLATFORM_SOURCES[platform]
    if source == "switch":
        eff = switch_eff or {}
        return "|".join(str(eff.get(k) or "") for k in ("eden", "ryujinx"))
    return nintendoapp.effective(console, nin_cfg)["data"].strip()


def active(platform: str, nin_cfg: Dict[str, Dict[str, str]], switch_eff: Optional[Dict[str, Any]] = None) -> bool:
    """True when the platform's emulator is on and its folder exists: its saves are looked at."""
    if platform not in PLATFORM_SOURCES:
        return False
    return any(d and os.path.isdir(d) for d in _data(platform, nin_cfg, switch_eff).split("|"))


def config_key(platform: str, nin_cfg: Dict[str, Dict[str, str]], switch_eff: Optional[Dict[str, Any]] = None) -> str:
    """What a save report is built from (it is built again when this changes)."""
    return f"id:{platform}:{_data(platform, nin_cfg, switch_eff)}"


def owned_games(rows: Any, root: Path) -> List[Owned]:
    """The games you have, from the "Games" rows of a scan: one entry per game (its first disc image)."""
    from .retroarch import fold_name
    out: List[Owned] = []
    for row in rows:
        if not row.get("have"):
            continue
        ref = (row.get("dat") or "", row["name"], row.get("title") or "")
        for rel in row.get("files") or ():
            path = rel.partition("::")[0].replace("\\", "/")
            if path.lower().endswith(nintendodisc.DISC_EXTS + nintendodisc.WIIU_EXTS + pcsx2.PS2_DISC_EXTS + switchfmt.CONTAINER_EXTS):
                out.append(Owned(fold_name(Path(path).stem), ref, Path(root) / path))
                break
    return out


def _cached_id(path: Path, read: Any) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    k = (str(path), st.st_size, st.st_mtime)
    if k not in _IDS:
        _IDS[k] = read(path)
    return _IDS[k]


def _set(source: str, label: str, key: str, name: str, ref: Optional[Tuple[str, str, str]], saves: int, states: int,
         size: int, members: List[Tuple[Path, Path, str, int]]) -> SaveSet:
    s = SaveSet(name, key, saves=saves, states=states, bytes=size, members=members, source=source, label=label, here=True)
    if ref is not None:
        s.match, (s.dat, s.game, s.title) = ROM, ref
    else:
        s.match = NONE
    return s


def sets_for(platform: str, owned: List[Owned], nin_cfg: Dict[str, Dict[str, str]], switch_eff: Optional[Dict[str, Any]] = None,
             header_key: Optional[bytes] = None) -> List[SaveSet]:
    """The save sets of ``platform``'s emulator; [] when it is switched off or its folder is not there."""
    if platform not in PLATFORM_SOURCES:
        return []
    source, _console = PLATFORM_SOURCES[platform]
    if source == "switch":
        return _switch(owned, switch_eff or {}, header_key)
    data = _data(platform, nin_cfg)
    if not data or not os.path.isdir(data):
        return []
    if source == "pcsx2":
        return _pcsx2(owned, Path(data))
    if source == "cemu":
        return _cemu(owned, Path(data))
    return _dolphin(owned, Path(data), "wii" if platform == "Nintendo Wii" else "gc")


def _dolphin(owned: List[Owned], data: Path, want: str) -> List[SaveSet]:
    """Dolphin's saves of the GameCube (``want`` ``gc``: one ``.gci`` per save, memory cards) or the Wii (``wii``: one folder per game)."""
    by_code: Dict[str, List[nintendosaves.NinSave]] = {}
    cards: List[nintendosaves.NinSave] = []
    if want == "wii":
        for s in nintendosaves.find_dolphin_wii(data):
            by_code.setdefault(s.key, []).append(s)
    else:
        for s in nintendosaves.find_dolphin_gc(data):
            (by_code.setdefault(s.key, []) if s.kind == "gci" else cards).append(s)
    out: List[SaveSet] = []
    claimed: set = set()
    for g in owned:
        info = _cached_id(g.path, lambda p: (lambda d: f"{d.kind}:{d.code4}" if d else "")(nintendodisc.read_disc(p)))
        kind, _sep, code = info.partition(":")
        if kind != want or code in claimed or code not in by_code:
            continue
        claimed.add(code)
        items = by_code[code]
        out.append(_set("dolphin", "Dolphin", g.key, g.path.stem, g.ref, len(items), 0, sum(i.bytes for i in items),
                        [(i.path, i.path.parent.parent if want == "gc" else i.path.parent.parent.parent, "save", i.bytes) for i in items]))
    for code, items in sorted(by_code.items()):
        if code not in claimed:
            out.append(_set("dolphin", "Dolphin", f"dolphin:{code}", f"{code} (no game file here)", None, len(items), 0,
                            sum(i.bytes for i in items),
                        [(i.path, i.path.parent.parent if want == "gc" else i.path.parent.parent.parent, "save", i.bytes) for i in items]))
    for c in cards:                                          # a memory card image holds the saves of many games: counted, not movable
        out.append(_set("dolphin", "Dolphin", f"dolphin:card:{c.path.name}", f"Memory card {c.path.name}", None, 1, 0, c.bytes, []))
    return out


def _switch(owned: List[Owned], eff: Dict[str, Any], header_key: Optional[bytes]) -> List[SaveSet]:
    """Eden's and Ryujinx's saves of the Switch, by title ID: a game's saves are those of its base title (updates and add-ons share
    them). Eden's save folders can move with the game; Ryujinx keeps a counter folder and an ``ExtraData0`` file per save that must stay
    together, so its saves are counted, never moved."""
    eden = switchsaves.find_eden(Path(eff["eden"])) if eff.get("eden") and os.path.isdir(eff["eden"]) else []
    ryu = switchsaves.find_ryujinx(Path(eff["ryujinx"])) if eff.get("ryujinx") and os.path.isdir(eff["ryujinx"]) else []
    grouped = switchsaves.group_by_title(eden, ryu)
    db = switchscan.SwitchDb()

    def title_of(path: Path) -> str:
        return switchscan.identify(path, db, header_key).base_id or ""

    def parts(items: List[switchsaves.SwitchSave]) -> Tuple[int, int, List[Tuple[Path, Path, str, int]]]:
        members = [(i.path, i.path.parents[4], "save", i.bytes) for i in items if i.emulator == "eden" and len(i.path.parents) > 4]
        return len(items), sum(i.bytes for i in items), members

    out: List[SaveSet] = []
    claimed: set = set()
    try:
        for g in owned:
            tid = _cached_id(g.path, title_of)
            if not tid or tid in claimed or tid not in grouped:
                continue
            claimed.add(tid)
            n, size, members = parts(grouped[tid])
            labels = sorted({i.emulator for i in grouped[tid]})
            out.append(_set("switch", " + ".join(l.capitalize() for l in labels), g.key, g.path.stem, g.ref, n, 0, size, members))
        for tid in sorted(set(grouped) - claimed):
            n, size, members = parts(grouped[tid])
            title = db.title(tid)
            labels = sorted({i.emulator for i in grouped[tid]})
            out.append(_set("switch", " + ".join(l.capitalize() for l in labels), f"switch:{tid}",
                            f"{title['name'] if title else tid} (no game file here)", None, n, 0, size, members))
    finally:
        db.close()
    return out


def _cemu(owned: List[Owned], data: Path) -> List[SaveSet]:
    """Cemu's saves of the Wii U: a folder per account (and a common one) under the game's title ID. Cemu's own list of the games it has
    seen says which title ID a disc file is."""
    listing = nintendosaves.read_title_list(data / "title_list_cache.xml")
    by_path = {os.path.normcase(os.path.abspath(v["path"])): k for k, v in listing.items() if v["path"]}
    grouped: Dict[str, List[nintendosaves.NinSave]] = {}
    for s in nintendosaves.find_cemu(data / "mlc01"):
        grouped.setdefault(s.key, []).append(s)

    def parts(items: List[nintendosaves.NinSave]) -> Tuple[int, int, List[Tuple[Path, Path, str, int]]]:
        return len(items), sum(i.bytes for i in items), [(i.path, i.path.parents[3], "save", i.bytes) for i in items]

    out: List[SaveSet] = []
    claimed: set = set()
    for g in owned:
        tid = by_path.get(os.path.normcase(os.path.abspath(g.path)), "")
        if not tid or tid in claimed or tid not in grouped:
            continue
        claimed.add(tid)
        n, size, members = parts(grouped[tid])
        out.append(_set("cemu", "Cemu", g.key, g.path.stem, g.ref, n, 0, size, members))
    for tid in sorted(set(grouped) - claimed):
        n, size, members = parts(grouped[tid])
        name = listing.get(tid, {}).get("name") or tid
        out.append(_set("cemu", "Cemu", f"cemu:{tid}", f"{name} (no game file here)", None, n, 0, size, members))
    return out


def _pcsx2(owned: List[Owned], root: Path) -> List[SaveSet]:
    det = pcsx2.detect_pcsx2(root=root)
    if not det:
        return []
    cards = pcsx2.find_cards([Path(x) for x in det.get("cards", [])])
    states = pcsx2.find_states(Path(str(det["states"]))) if det.get("states") else []
    saves_by: Dict[str, List[pcsx2.Ps2Save]] = {}
    states_by: Dict[str, List[pcsx2.Ps2Save]] = {}
    for s in cards:
        if s.serial:
            saves_by.setdefault(s.serial, []).append(s)
    for s in states:
        states_by.setdefault(s.serial, []).append(s)

    def parts(serial: str) -> Tuple[int, int, int, List[Tuple[Path, Path, str, int]]]:
        sv, st = saves_by.get(serial, []), states_by.get(serial, [])
        members = [(s.path, s.path.parent.parent, "save", s.bytes) for s in sv if s.path.is_dir()]       # (a folder card's save folder)
        members += [(s.path, s.path.parent.parent, "state", s.bytes) for s in st]
        return len(sv), len(st), sum(s.bytes for s in sv) + sum(s.bytes for s in st), members

    out: List[SaveSet] = []
    claimed: set = set()
    for g in owned:
        serial = _cached_id(g.path, pcsx2.read_serial)
        if not serial or serial in claimed or not (saves_by.get(serial) or states_by.get(serial)):
            continue
        claimed.add(serial)
        n, st, size, members = parts(serial)
        out.append(_set("pcsx2", "PCSX2", g.key, g.path.stem, g.ref, n, st, size, members))
    for serial in sorted((set(saves_by) | set(states_by)) - claimed):
        n, st, size, members = parts(serial)
        out.append(_set("pcsx2", "PCSX2", f"pcsx2:{serial}", f"{serial} (no game file here)", None, n, st, size, members))
    return out
