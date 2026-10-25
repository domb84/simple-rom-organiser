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

__all__ = ["MARKER", "Target", "targets", "build", "marker_of"]

MARKER = WHDLOAD_BUILD_MARKER
_BAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DISK_RE = re.compile(r"\s*\(Dis[ck] \d+ of \d+\)")
_DEMO_ARC_RE = re.compile(r"(?:demo|preview)\d*\.lha$", re.IGNORECASE)     # (an install of a game's demo: "AgonyDemo.lha")


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

    def key(self, date: str = "") -> dict[str, Any]:
        return {"source": list(self.names), "install": self.archive, "install_date": date}


def _final(op: Any) -> Path:
    """Where a kept file is: where the build puts it, once it is there (before a build: where it is now)."""
    from .organiser import _final_path
    p = _final_path(op)
    return p if p.exists() or not Path(op.src).exists() else Path(op.src)


def targets(plan: Any, platform: Any) -> list[Target]:
    """The WHDLoad games of a library plan (``organiser.LibraryPlan``): one a game, from the kept version its install works from best
    (an ADF set or an SPS release; of versions as good the best platform - CD32 > AGA > CDTV > ECS > OCS - then the floppies), at the
    place the build leaves its disks."""
    from . import library, whdready
    sel, owners = plan.selection, getattr(plan, "owners", {}) or {}
    image_sets = set(getattr(platform, "image_set_dats", ()) or ())
    games_dat = getattr(platform, "game_dat", "") or ""
    in_sets: set[int] = set()
    versions: list[tuple[tuple, Target]] = []

    def unit_item(key: int) -> Any:
        op, u = owners[key]
        return op, u, library.Item(key=key, dat=u.dat, rom=u.rom, style=u.style, path=_final(op), member=u.member)

    def add(keys: list[int], name: str) -> None:
        if not keys or any(k not in owners for k in keys):
            return
        parts = [unit_item(k) for k in keys]
        op0, u0, it0 = parts[0]
        sps = u0.dat in image_sets
        if sps:                                               # (its images: loose, in a zip / 7z of their own, or one of several)
            entry = whdready.release_of(u0.rom) or [None, None]
            level, arc, names = entry[0], entry[1] if len(entry) > 1 else None, [u0.rom.game]
            found = {_final(op) for op, _u, _it in parts}           # (a zip / 7z of the release is one unit: every image in it)
            disks = sorted(((f, None) for f in found), key=lambda d: (d[0].name.casefold(), str(d[0])))
        else:
            names = whdready.names_of(u.rom for _op, u, _it in parts)      # (the names the list knows them by: by checksum if renamed)
            sha = whdready.sha_key(u.rom for _op, u, _it in parts)
            level = whdready.level(names, sha)
            arc = whdready.archive(names) or (whdready.checked(names, sha) or {}).get("archive")
            disks = [(_final(op), u.member) for op, u, _it in parts]
        if not arc or level is None or level < whdready.IMAGES:
            return
        if "arcadia" in arc.casefold() and not any("arcadia" in n.casefold() for n in names):
            return                                            # (an Arcadia board's install wants its ROMs: never floppies - Space Ranger)
        if _DEMO_ARC_RE.search(arc) and level < whdready.BOOTS and not any(re.search(r"\b(?:demo|preview)\b", n, re.IGNORECASE) for n in names):
            return                                            # (the full game's disks with a demo's install it does not boot from: not the game)
        title = library._tags_of(it0).title
        stem = _BAD_NAME_RE.sub("", name).strip(" .") or title
        t = Target(title=title, names=names, archive=arc, level=int(level), dest=disks[0][0].parent / f"{stem}.lha", disks=disks, sps=sps)
        rank = library.tags.platform_rank(library._tags_of(it0))     # (of versions as good: the best platform - AGA before OCS)
        versions.append((library.game_key(it0, getattr(sel, "combine_alt", True)), rank, t))   # (one a game, as the library counts games)

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
        keys.sort(key=lambda k: ((library.tags._disk(library.tags.parse_name(owners[k][1].rom.name, library.STYLE_TOSEC)) or (0, 0))[1],
                                 owners[k][1].rom.name))
        add(keys, _DISK_RE.sub("", Path(owners[keys[0]][1].rom.name).stem))
    for release, keys in by_release.items():
        add(keys, release)
    best: dict[tuple, tuple[int, Target]] = {}
    for g, rank, t in versions:
        cur = best.get(g)
        if cur is None or (t.level, -rank, not t.sps) > (cur[1].level, -cur[0], not cur[1].sps):
            best[g] = (rank, t)
    return sorted((t for _r, t in best.values()), key=lambda t: (t.title.casefold(), str(t.dest)))


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


def _disk_data(t: Target) -> list[bytes]:
    from .discarchive import iter_files, list_members, open_member
    out = []
    for path, member in t.disks:
        if t.sps and Path(path).suffix.lower() in (".zip", ".7z"):     # (a release's zip / 7z: its images in order, read in one pass)
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


def workbench_disk(root: Path) -> Optional[Path]:
    """The library's Workbench 3.x "Workbench" disk (TOSEC's Workbench DAT folder under ``root``): the system an install copies
    libraries from (3.1 before 3.0, a verified dump first; not one set aside in a "_" folder)."""
    found = [p for p in Path(root).glob("*/Workbench v3.[01] *(Workbench)*.adf") if not p.parent.name.startswith("_")]
    return min(found, key=lambda p: ("v3.1" not in p.name, "[!]" not in p.name, len(p.name), p.name), default=None)


def build(todo: list[Target], root: Path, installs: list[dict[str, Any]], progress: Optional[Callable[[int, int, str], None]] = None,
          cancel: Any = None, fetch: Optional[Callable[[dict[str, Any]], bytes]] = None,
          decruncher: Optional[Callable[[], Any]] = None, workbench: Optional[Path] = None) -> dict[str, Any]:
    """Make ``todo`` real under ``root``: ``{"built", "moved", "kept", "removed", "failed": [{title, error}]}``. An archive that is the
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
    out: dict[str, Any] = {"built": [], "moved": [], "kept": 0, "removed": [], "failed": []}
    wanted: set[Path] = set()
    packages: dict[str, bytes] = {}
    def keep_old(t: Target) -> None:
        """A target that cannot be made now keeps the archive it had (its disks moved: moved with them), never loses it."""
        old = t.dest if t.dest in have else next((p for p, m in have.items() if m.get("source") == list(t.names) and p.exists()), None)
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
                    res = pkg.install(_disk_data(t), cd=False, decruncher=decruncher, hints=ai.chipset_hints(t.names), system=system)
            except Exception as exc:  # noqa: BLE001
                if not t.sps:                                 # (the install does not work from these disks: they get a playlist instead)
                    from . import whdready
                    whdready.record({whdready.key_of(t.names): {"ok": False, "archive": t.archive,
                                                                "reason": (str(exc) or type(exc).__name__)[:300]}})
                raise
            files = ai.alias_icons(res.files)
            files[MARKER] = json.dumps({"app": "simple-rom-organiser", **t.key(inst.get("date", "")), "built": time.time()},
                                       indent=1).encode()
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
