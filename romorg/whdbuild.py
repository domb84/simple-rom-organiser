"""Playable WHDLoad games built from the user's own disks, next to them (Build library, the Amiga's rule "Build WHDLoad games").

For every game the library keeps, the version its WHDLoad install works from - known from the shipped list (``whdknown.json.gz``)
or the user's own check (``whdready``): boots, installs its files, installs disk images - is installed here (``amigainstall``:
the install's own script, RawDIC / Patcher / DIC, xfdDecrunch) and written as ``<release>.lha`` beside that version's disks, the
way PUAE runs a WHDLoad game (its WHDLoad helper boots the archive as a hard disk). The release's TOSEC name is kept: PUAE takes
the machine from the path's tags.

The first entry of every archive written here is ``.romorg-whdload-build.json`` (what it was made from): the scanner leaves such an
archive alone (it is no file of a DAT), and the next build knows it - left as it is when its disks and install are the same, moved
when its disks moved, made again when they changed, removed when its game is no longer kept. An archive of another's is never
touched. Install packages come from whdload.de (one at a time, cached: ``whdinstalls.package``); Retroplay's pre-installed packs,
which hold game data, never.

Expansions (``whdexpansions``): a game whose install is one package for the game and an expansion is built with the expansion's disks after
its own (a blank save disk between them where the install wants one), the expansion's questions forced to yes (``amigainstall.Ask``): one
archive, one drawer, one slave. The expansion's disks are part of the archive's ``source`` - a changed set is made again. When that does not
install, the game is built alone (the result says which expansion was left out and why). An install that cannot be run here at all (it
mounts a volume) is skipped with its reason, not failed. The version of the game that is built is the kept one; when it does not install,
other versions of the game the rules set aside are tried (the original before a crack, the one the table knows boots first), a few at most.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .cancellation import is_cancelled
from .scanner import WHDLOAD_BUILD_MARKER, WHDLOAD_BUILD_TEMP, is_built_whdload

__all__ = ["MARKER", "Target", "Extra", "targets", "build", "marker_of"]

MARKER = WHDLOAD_BUILD_MARKER
_BAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DISK_RE = re.compile(r"\s*\(Dis[ck] \d+ of \d+\)")
_DEMO_ARC_RE = re.compile(r"(?:demo|preview)\d*\.lha$", re.IGNORECASE)     # (an install of a game's demo: "AgonyDemo.lha")


_MAX_VARIANTS = 4                                    # (versions of a game tried when the first does not install)
_BLANK_DISK = bytes(901120)                          # (a blank save disk: HeroQuest's install wants one between the game and its expansion)


@dataclass
class Extra:
    """An expansion installed with its game: its disks (in disk order) and the names the library knows them by."""
    expansion: Any                                    # ``whdexpansions.Expansion``
    names: list[str]
    disks: list[tuple[Path, Optional[str]]] = field(default_factory=list)
    sps: bool = False                                 # its disks are an SPS release's images (a zip / 7z of them, or loose .ipf files)


@dataclass
class Target:
    """One game to have as a WHDLoad archive: the version it is made from and where it goes."""
    title: str
    names: list[str]                      # the version's disks (their DAT names): what the shipped list knows it by
    archive: str                          # the install (``...lha`` on whdload.de)
    level: int                            # ``whdready`` level: 3 boots, 2 installs the files, 1 disk images
    dest: Path                            # <the version's folder>/<release>.lha
    disks: list[tuple[Path, Optional[str]]] = field(default_factory=list)   # (file, member) in disk order; a whole SPS zip / 7z: (it, None)
    sps: bool = False
    was: Optional[Path] = None            # ``targets(planned=True)``: where the archive is now, beside the disks as they are now (dest: where the build puts it)

    extra: list[Extra] = field(default_factory=list)       # the expansions installed with the game, in the install's order
    merge: Any = None                     # ``whdexpansions.Merge`` of the install that takes them
    alternates: list["Target"] = field(default_factory=list)   # other versions of the game (set aside by the rules): tried when this one does not install
    prefer_disk: str = ""                 # a file the disk must hold (Bloodwych's Extended Levels: ``C/BEXT``)
    asks: tuple = ()                      # answers forced for the install of an expansion that has one of its own (``amigainstall.Ask``)

    def key(self, date: str = "") -> dict[str, Any]:
        """What the archive is made from: the game's disks, then each expansion's (a changed set is made again)."""
        return {"source": list(self.names) + [f"+ {n}" for x in self.extra for n in x.names], "install": self.archive, "install_date": date}


def _final(op: Any, planned: bool = False) -> Path:
    """Where a kept file is: where the build puts it, once it is there (before a build: where it is now; ``planned``: where it will be)."""
    from .organiser import _final_path
    p = _final_path(op)
    return p if planned or p.exists() or not Path(op.src).exists() else Path(op.src)


def targets(plan: Any, platform: Any, planned: bool = False) -> list[Target]:
    """The WHDLoad games of a library plan (``organiser.LibraryPlan``): one a game, from the kept version its install works from best
    (an ADF set or an SPS release; of versions as good the best platform - CD32 > AGA > CDTV > ECS > OCS - then the floppies), at the
    place the build leaves its disks (``planned``: the place the build WILL leave them, also before it has moved them: what the file list shows).
    An expansion the library keeps with its game (``library.merge_expansions``) is no game of its own: its disks are the game's target's
    ``extra`` (installed with it); where the game needs one, the versions the rules set aside are its ``alternates``."""
    from . import amigainstall as ai, library, whdexpansions, whdready
    sel, owners = plan.selection, getattr(plan, "owners", {}) or {}
    image_sets = set(getattr(platform, "image_set_dats", ()) or ())
    games_dat = getattr(platform, "game_dat", "") or ""
    in_sets: set[int] = set()
    versions: list[tuple[tuple, int, Target]] = []
    merged = {k for k, d in sel.decisions.items() if d.action == library.KEEP and d.merged_into and k in owners}

    def unit_item(key: int) -> Any:
        op, u = owners[key]
        return op, u, library.Item(key=key, dat=u.dat, rom=u.rom, style=u.style, path=_final(op, planned), member=u.member)

    assumed: dict[tuple, str] = {}                            # (games whose install takes them with their expansion: ``Merge.assume``)
    for key in merged:
        item = unit_item(key)[2]
        for m, _e in whdexpansions.merge_for_expansion(library._tags_of(item).title):
            if m.assume:
                assumed[library.counted_key(item, sel)] = m.archive

    def disk_number(key: int) -> tuple:
        return ((library.tags._disk(library.tags.parse_name(owners[key][1].rom.name, library.STYLE_TOSEC)) or (0, 0))[1], owners[key][1].rom.name)

    def add(keys: list[int], name: str, bucket: Optional[list] = None, only: str = "") -> None:
        """One version of a game as a target (``only``: the install of the game's kept version - an alternate needs no list to say so)."""
        bucket = versions if bucket is None else bucket
        if not keys or any(k not in owners or k in merged for k in keys):
            return
        parts = [unit_item(k) for k in keys]
        op0, u0, it0 = parts[0]
        sps = u0.dat in image_sets
        if sps:                                               # (its images: loose, in a zip / 7z of their own, or one of several)
            entry = whdready.release_of(u0.rom) or [None, None]
            level, arc, names = entry[0], entry[1] if len(entry) > 1 else None, [u0.rom.game]
            found = {_final(op, planned) for op, _u, _it in parts}           # (a zip / 7z of the release is one unit: every image in it)
            disks = sorted(((f, None) for f in found), key=lambda d: (d[0].name.casefold(), str(d[0])))
        else:
            names = whdready.names_of(u.rom for _op, u, _it in parts)      # (the names the list knows them by: by checksum if renamed)
            sha = whdready.sha_key(u.rom for _op, u, _it in parts)
            level = whdready.level(names, sha)
            arc = whdready.archive(names) or (whdready.checked(names, sha) or {}).get("archive")
            disks = [(_final(op, planned), u.member) for op, u, _it in parts]
        title = library._tags_of(it0).title
        row = None
        want = assumed.get(library.counted_key(it0, sel))
        if want and not sps and arc in (None, "", want) and (level is None or level < whdready.IMAGES):
            arc, level = want, whdready.IMAGES                   # (the list has no working result for the game alone: with its expansion it installs)
        if only:
            arc, level = arc or only, max(level or 0, whdready.IMAGES)
        elif not sps and (row := whdexpansions.standalone_row(title)) is not None:       # (an expansion with an install of its own)
            if arc != row["archive"]:                         # (the list has none for it, or another game's: PowerMonger's for its World War 1 disk)
                arc, level = row["archive"], int(row["level"])
            elif int(row["level"]) >= whdready.BOOTS or level is None:     # (seen to boot from these disks: beats the list's "does not boot")
                level = max(level or 0, int(row["level"]))
        if not arc or level is None or level < whdready.IMAGES:
            return
        if "arcadia" in arc.casefold() and not any("arcadia" in n.casefold() for n in names):
            return                                            # (an Arcadia board's install wants its ROMs: never floppies - Space Ranger)
        if _DEMO_ARC_RE.search(arc) and level < whdready.BOOTS and not any(re.search(r"\b(?:demo|preview)\b", n, re.IGNORECASE) for n in names):
            return                                            # (the full game's disks with a demo's install it does not boot from: not the game)
        stem = _BAD_NAME_RE.sub("", name).strip(" .") or title
        t = Target(title=title, names=names, archive=arc, level=int(level), dest=disks[0][0].parent / f"{stem}.lha", disks=disks, sps=sps,
                   was=Path(op0.src).parent / f"{stem}.lha" if planned else None)
        if row is not None:
            t.prefer_disk = row.get("prefer_disk", "")
            t.asks = tuple(ai.Ask(kind=a["kind"], prompt=a.get("prompt", ""), choice=a.get("choice", ""), want=True) for a in row.get("asks") or ())
        rank = library.tags.platform_rank(library._tags_of(it0))     # (of versions as good: the best platform - AGA before OCS)
        bucket.append((library.counted_key(it0, sel), rank, t))      # (one a game, as the library counts games)

    for cs in sel.sets:                                       # (the kept multi-disk sets: their slots in disk order)
        keys = [k for _s, k in sorted(cs.slots.items())]
        in_sets.update(keys)
        if cs.dat == games_dat:
            add(keys, cs.name)
    by_release: dict[str, list[int]] = {}
    loose_sets: dict[tuple, list[int]] = {}                   # (the disks of a release kept with no playlist - for its saves, say: one version)
    for key, d in sel.decisions.items():
        if d.action != library.KEEP or key in in_sets or key not in owners:
            continue
        _op, u = owners[key]
        if u.dat in image_sets:
            by_release.setdefault(u.rom.game, []).append(key)
        elif u.dat == games_dat:
            rel = library._release_of_parts(unit_item(key)[2])
            if rel is None:
                add([key], _DISK_RE.sub("", Path(u.rom.name).stem))
            else:
                loose_sets.setdefault(rel, []).append(key)
    for keys in loose_sets.values():
        keys.sort(key=disk_number)
        add(keys, _DISK_RE.sub("", Path(owners[keys[0]][1].rom.name).stem))
    for release, keys in by_release.items():
        add(keys, release)
    best: dict[tuple, tuple[tuple, Target]] = {}
    for g, rank, t in versions:
        cur = best.get(g)
        if cur is None or (t.level, -rank, not t.sps) > (cur[1].level, -cur[0], not cur[1].sps):
            best[g] = (rank, t)
    got = {g: t for g, (_r, t) in best.items()}
    if merged:
        _attach_expansions(got, merged, unit_item, disk_number, owners, sel, image_sets)
    wanted = {g for g, t in got.items() if t.extra or t.prefer_disk}
    if wanted:                                                # (the versions the rules set aside: what an install that fails falls back to)
        sets: dict[tuple, list[int]] = {}
        for key, d in sel.decisions.items():
            if d.action != library.SUPERSEDED or key not in owners or key in merged or owners[key][1].dat != games_dat:
                continue
            item = unit_item(key)[2]
            g = library.counted_key(item, sel)
            if g in wanted:
                sets.setdefault((g, library._release_of_parts(item) or ("one", key)), []).append(key)
        alts: list[tuple] = []
        for (g, _rel), keys in sets.items():
            keys.sort(key=disk_number)
            add(keys, _DISK_RE.sub("", Path(owners[keys[0]][1].rom.name).stem), bucket=alts, only=got[g].archive)
        for g, _r, a in alts:
            if a.archive == got[g].archive and a.disks != got[g].disks:
                got[g].alternates.append(a)
    return sorted(got.values(), key=lambda t: (t.title.casefold(), str(t.dest)))


def _attach_expansions(got: dict[tuple, Target], merged: set[int], unit_item: Callable[[int], Any], disk_number: Callable[[int], tuple],
                       owners: dict, sel: Any, image_sets: Any = ()) -> None:
    """Give each game's target the disks of the expansions its install takes in (``Target.extra``, in the install's own order): per
    expansion one kept release - a set in disk order, or a disk."""
    from . import library, whdexpansions, whdready
    groups: dict[tuple, dict[Any, list[int]]] = {}
    objs: dict[tuple, tuple] = {}
    for key in sorted(merged):
        item = unit_item(key)[2]
        title = library._tags_of(item).title
        for m, e in whdexpansions.merge_for_expansion(title):
            rel = library._release_of_parts(item) or ("one", key)
            groups.setdefault((library.counted_key(item, sel), m.archive, e.titles), {}).setdefault(rel, []).append(key)
            objs[(m.archive, e.titles)] = (m, e)
    for (g, arc, titles), rels in groups.items():
        m, e = objs[(arc, titles)]
        t = got.get(g)
        if t is None or t.archive != m.archive:
            continue
        if any(owners[k][1].dat in image_sets for ks in rels.values() for k in ks):       # (an SPS release: its images, a zip / 7z of them one unit)
            games: dict[str, list[int]] = {}
            for ks in rels.values():
                for k in ks:
                    if owners[k][1].dat in image_sets:
                        games.setdefault(owners[k][1].rom.game, []).append(k)
            ks = games[sorted(games)[0]]
            parts = [unit_item(k) for k in ks]
            found = {it.path for _op, _u, it in parts}
            t.extra.append(Extra(expansion=e, names=[parts[0][1].rom.game], sps=True,
                                 disks=sorted(((f, None) for f in found), key=lambda d: (d[0].name.casefold(), str(d[0])))))
            t.merge = m
            continue
        keys = sorted(sorted(rels.values(), key=lambda ks: min(owners[k][1].rom.name for k in ks))[0], key=disk_number)
        parts = [unit_item(k) for k in keys]
        t.extra.append(Extra(expansion=e, names=whdready.names_of(u.rom for _op, u, _it in parts),
                             disks=[(it.path, u.member) for _op, u, it in parts]))
        t.merge = m
    for t in got.values():
        if t.merge is not None:
            order = {x.titles: i for i, x in enumerate(t.merge.expansions)}
            t.extra.sort(key=lambda x: order.get(x.expansion.titles, 99))


def marker_of(path: Path) -> Optional[dict[str, Any]]:
    """What a WHDLoad archive built here was made from (None: not one of ours)."""
    if not is_built_whdload(path):
        return None
    from .lha import LhaError, LhaFile
    try:
        data = json.loads(LhaFile(Path(path).read_bytes()).read(MARKER))
        return data if isinstance(data, dict) else None
    except (OSError, LhaError, ValueError):
        return None


def _ours(root: Path) -> dict[Path, dict[str, Any]]:
    """The WHDLoad games built here under ``root`` with what each was made from (``{}``: one whose marker cannot be read - ours, made
    again or removed). What an interrupted build left half written is deleted."""
    out: dict[Path, dict[str, Any]] = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            if f.lower().endswith(".lha"):
                p = Path(dp) / f
                if is_built_whdload(p):
                    out[p] = marker_of(p) or {}
            elif f.endswith(WHDLOAD_BUILD_TEMP):
                try:
                    (Path(dp) / f).unlink()
                except OSError:
                    pass
    return out


def _read_disks(disks: list[tuple[Path, Optional[str]]], sps: bool) -> list[bytes]:
    from .discarchive import iter_files, list_members, open_member
    out = []
    for path, member in disks:
        if sps and Path(path).suffix.lower() in (".zip", ".7z"):     # (a release's zip / 7z: its images in order, read in one pass)
            images = sorted((m for m in list_members(Path(path)) if m.name.lower().endswith(".ipf")), key=lambda m: m.name.casefold())
            parts: dict[str, list[bytes]] = {m.name: [] for m in images}
            for m, piece in iter_files(Path(path), images):
                parts[m.name].append(piece)
            out.extend(b"".join(parts[m.name]) for m in images)
        elif member:
            with open_member(path, member) as f:
                out.append(f.read())
        else:
            out.append(Path(path).read_bytes())
    return out


def _disk_data(t: Target, extra: Optional[list[Extra]] = None) -> list[bytes]:
    """The disks an install is given, in order: the game's, a blank save disk where its install wants one, then each expansion's (``extra``)."""
    out = _read_disks(t.disks, t.sps)
    if extra:
        if t.merge is not None and t.merge.save_disk:
            out.append(_BLANK_DISK)
        for x in extra:
            out.extend(_read_disks(x.disks, x.sps))
    return out


def _holds(t: Target, path: str) -> bool:
    """Whether the disk of ``t`` holds the file ``path`` (``C/BEXT``) - an unreadable disk does not."""
    from . import amigainstall as ai
    try:
        disk = ai.floppy(_read_disks(t.disks[:1], t.sps)[0])
        return any(f.casefold() == path.casefold() for f, _size in disk.files())
    except Exception:  # noqa: BLE001
        return False


def _in_order(t: Target) -> list[Target]:
    """``t`` and its alternates in the order they are tried (at most :data:`_MAX_VARIANTS`): a disk that holds the file the install needs
    (``prefer_disk``), a version the table knows boots (``Merge.base_pick``), an original before a crack - then as the rules ranked them."""
    cands = [t, *t.alternates]
    if len(cands) == 1:
        return cands
    pick = t.merge.base_pick if t.merge is not None else ""

    def rank(i: int) -> tuple:
        c = cands[i]
        return (0 if not t.prefer_disk or _holds(c, t.prefer_disk) else 1,
                0 if not pick or any(re.search(pick, n) for n in c.names) else 1,
                1 if any(re.search(r"\[cr\b", n) for n in c.names) else 0, i)
    return [cands[i] for i in sorted(range(len(cands)), key=rank)][:_MAX_VARIANTS]


def workbench_disk(root: Path) -> Optional[Path]:
    """The library's Workbench 3.x "Workbench" disk (TOSEC's Workbench DAT folder under ``root``): the system an install copies
    libraries from (3.1 before 3.0, a verified dump first; not one set aside in a "_" folder)."""
    found = [p for p in Path(root).glob("*/Workbench v3.[01] *(Workbench)*.adf") if not p.parent.name.startswith("_")]
    return min(found, key=lambda p: ("v3.1" not in p.name, "[!]" not in p.name, len(p.name), p.name), default=None)


def _install(t: Target, pkg: Any, decruncher: Optional[Callable[[], Any]], system: list) -> tuple[Any, Target, list[dict[str, str]]]:
    """Install ``t`` from its disks: ``(the result, the version of the game it was made from, the expansions left out)``. With expansions
    (``t.extra``) the install is given their disks and the answers they need; when that does not install, other versions of the game are
    tried the same way, then the game alone (the expansions are then ``left_out`` with the reason). Raises the first reason when nothing
    installs, :class:`amigainstall.Unsupported` when no version can be run here."""
    from . import amigainstall as ai, whdexpansions
    first: Optional[Exception] = None
    unsupported = True
    cands = _in_order(t)
    plans = [(c, t.extra) for c in cands] + ([(c, []) for c in cands[:2]] if t.extra else [])
    why = ""
    for cand, extra in plans:
        asks = t.asks + (whdexpansions.asks_for(t.merge, [x.expansion for x in extra]) if t.merge is not None else ())
        try:
            res = pkg.install(_disk_data(cand, extra), cd=False, decruncher=decruncher, hints=ai.chipset_hints(cand.names), system=system,
                              asks=asks)
        except ai.Unsupported as exc:
            first = first or exc
            continue
        except Exception as exc:  # noqa: BLE001 - this version does not install: the next
            unsupported = False
            first = first or exc
            if extra and not why:
                why = (str(exc) or type(exc).__name__)[:200]
            continue
        left = [{"title": x.expansion.titles[0], "reason": why or "the install did not ask for it"} for x in t.extra
                if x.expansion.titles[0] not in res.forced]
        return res, cand, left
    if first is not None and unsupported:
        raise ai.Unsupported(str(first))
    raise first  # type: ignore[misc]


def build(todo: list[Target], root: Path, installs: list[dict[str, Any]], progress: Optional[Callable[[int, int, str], None]] = None,
          cancel: Any = None, fetch: Optional[Callable[[dict[str, Any]], bytes]] = None,
          decruncher: Optional[Callable[[], Any]] = None, workbench: Optional[Path] = None) -> dict[str, Any]:
    """Make ``todo`` real under ``root``: ``{"built", "moved", "kept", "removed", "failed": [{title, error}], "skipped": [{title, reason}],
    "expansions": [{title, installed: [...], left_out: [{title, reason}]}]}``. An archive that is the
    same is kept, one whose disks moved is moved, the others are made (atomically); ours that no target wants any more are removed.
    ``workbench``: the Workbench disk whose LIBS: an install may copy (default: the library's own - see :func:`workbench_disk`)."""
    from . import amigainstall as ai
    wb = workbench or workbench_disk(Path(root))
    system = ai.system_volumes(wb) if wb is not None else []
    by_arc = {i["archive"]: i for i in installs}
    if fetch is None:
        from . import whdinstalls
        fetch = lambda inst: whdinstalls.package(inst)[0]          # noqa: E731
    if decruncher is None:
        state: dict[str, Any] = {}

        def decruncher() -> Any:
            if "x" not in state:
                from . import whdinstalls, xfd
                state["x"] = xfd.Decruncher(whdinstalls.xfd_package())
            return state["x"]
    stopped = lambda: is_cancelled(cancel)   # noqa: E731
    have = _ours(Path(root))
    by_key = {json.dumps({k: m.get(k) for k in ("source", "install", "install_date")}, sort_keys=True): p for p, m in have.items()}
    out: dict[str, Any] = {"built": [], "moved": [], "kept": 0, "removed": [], "failed": [], "skipped": [], "expansions": []}
    wanted: set[Path] = set()
    packages: dict[str, bytes] = {}
    def keep_old(t: Target) -> None:
        """A target that cannot be made now keeps the archive it had (its disks moved: moved with them), never loses it."""
        old = t.dest if t.dest in have else next((p for p, m in have.items() if m.get("source") == t.key()["source"] and p.exists()), None)
        if old is None:
            return
        if old != t.dest and not t.dest.exists() and old not in wanted:
            os.replace(old, t.dest)
            have[t.dest] = have.pop(old)
            out["moved"].append(str(t.dest))
        else:
            wanted.add(old)

    for n, t in enumerate(todo):
        if stopped():
            break
        wanted.add(t.dest)
        inst = by_arc.get(t.archive)
        if inst is None:
            out["failed"].append({"title": t.title, "error": f"{t.archive} is not in the list of installs"})
            keep_old(t)
            continue
        key = json.dumps(t.key(inst.get("date", "")), sort_keys=True)
        if t.dest in have and json.dumps({k: have[t.dest].get(k) for k in ("source", "install", "install_date")}, sort_keys=True) == key:
            out["kept"] += 1
            continue
        if t.dest.exists() and t.dest not in have:            # (one of another's: never touched)
            out["failed"].append({"title": t.title, "error": f"{t.dest.name} is there already and is not one this app built"})
            continue
        if progress:
            progress(n, len(todo), f"WHDLoad: {t.title}")
        src = by_key.get(key)
        if src is not None and src != t.dest and src.exists() and src not in wanted:
            os.replace(src, t.dest)                           # (its disks moved: the archive goes with them)
            have[t.dest] = have.pop(src)
            out["moved"].append(str(t.dest))
            continue
        try:
            if t.archive not in packages:
                packages.clear()                              # (one package held at a time: they are small, the list is not)
                packages[t.archive] = fetch(inst)
        except Exception as exc:  # noqa: BLE001 - a download that failed says nothing of the game: tried again next time
            out["failed"].append({"title": t.title, "error": (str(exc) or type(exc).__name__)[:300]})
            keep_old(t)
            continue
        try:
            try:
                with ai.Package(packages[t.archive]) as pkg:
                    res, used, left_out = _install(t, pkg, decruncher, system)
            except ai.Unsupported as exc:                     # (an install this app cannot run: no failure of the game, no result recorded)
                out["skipped"].append({"title": t.title, "reason": (str(exc) or type(exc).__name__)[:300]})
                keep_old(t)
                continue
            except Exception as exc:  # noqa: BLE001
                if not t.sps:                                 # (the install does not work from these disks: they get a playlist instead)
                    from . import whdready
                    whdready.record({whdready.key_of(t.names): {"ok": False, "archive": t.archive,
                                                                "reason": (str(exc) or type(exc).__name__)[:300]}})
                raise
            if t.extra:
                out["expansions"].append({"title": t.title, "installed": [x.expansion.titles[0] for x in t.extra
                                                                           if x.expansion.titles[0] in res.forced],
                                          "left_out": left_out})
            files = ai.alias_icons(res.files)
            files[MARKER] = json.dumps({"app": "simple-rom-organiser", **t.key(inst.get("date", "")), "built": time.time(),
                                        "expansions": [x.expansion.titles[0] for x in t.extra if x.expansion.titles[0] in res.forced],
                                        **({"built_from": used.names} if used is not t else {})}, indent=1).encode()
            tmp = t.dest.with_name(f"{t.dest.name}{WHDLOAD_BUILD_TEMP}")
            try:
                ai.write_lha(tmp, files, res.dirs)
                os.replace(tmp, t.dest)
            except BaseException:
                tmp.unlink(missing_ok=True)                   # (a disk that filled up: nothing half written is left)
                raise
            have[t.dest] = {}
            out["built"].append(str(t.dest))
        except Exception as exc:  # noqa: BLE001 - one game that cannot be built does not stop the others
            out["failed"].append({"title": t.title, "error": (str(exc) or type(exc).__name__)[:300]})
            keep_old(t)
    if not stopped():
        for p in list(have):
            if p not in wanted and p.exists() and is_built_whdload(p):
                p.unlink()                                    # (its game is no longer kept, or its version changed: ours, made again at will)
                out["removed"].append(str(p))
    if progress:
        progress(len(todo), len(todo), "WHDLoad games built")
    return out
