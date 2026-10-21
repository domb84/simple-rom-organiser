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
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .scanner import WHDLOAD_BUILD_MARKER, is_built_whdload

__all__ = ["MARKER", "Target", "targets", "build", "marker_of"]

MARKER = WHDLOAD_BUILD_MARKER
_BAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DISK_RE = re.compile(r"\s*\(Dis[ck] \d+ of \d+\)")


@dataclass
class Target:
    """One game to have as a WHDLoad archive: the version it is made from and where it goes."""
    title: str
    names: list[str]                      # the version's disks (their DAT names): what the shipped list knows it by
    archive: str                          # the install (``...lha`` on whdload.de)
    level: int                            # ``whdready`` level: 3 boots, 2 installs the files, 1 disk images
    dest: Path                            # <the version's folder>/<release>.lha
    disks: list[tuple[Path, Optional[str]]] = field(default_factory=list)   # (file, member) in disk order; an SPS zip: (zip, None)
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
    (an ADF set or an SPS release; the floppies first when both are as good), at the place the build leaves its disks."""
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
        if sps:
            entry = whdready.release(u0.rom.game) or [None, None]
            level, arc, names = entry[0], entry[1] if len(entry) > 1 else None, [u0.rom.game]
            disks = [(_final(op0), None)]
        else:
            names = [u.rom.name for _op, u, _it in parts]
            level = whdready.level(names)
            arc = whdready.archive(names) or (whdready.load().get(whdready.key_of(names)) or {}).get("archive")
            disks = [(_final(op), u.member) for op, u, _it in parts]
        if not arc or level is None or level < whdready.IMAGES:
            return
        title = library._tags_of(it0).title
        stem = _BAD_NAME_RE.sub("", name).strip(" .") or title
        t = Target(title=title, names=names, archive=arc, level=int(level), dest=disks[0][0].parent / f"{stem}.lha", disks=disks, sps=sps)
        versions.append((library.game_key(it0, True), t))

    for cs in sel.sets:                                       # (the kept multi-disk sets: their slots in disk order)
        keys = [k for _s, k in sorted(cs.slots.items())]
        in_sets.update(keys)
        if cs.dat == games_dat:
            add(keys, cs.name)
    by_release: dict[str, list[int]] = {}
    for key, d in sel.decisions.items():
        if d.action != library.KEEP or key in in_sets or key not in owners:
            continue
        _op, u = owners[key]
        if u.dat in image_sets:
            by_release.setdefault(u.rom.game, []).append(key)
        elif u.dat == games_dat:
            add([key], _DISK_RE.sub("", Path(u.rom.name).stem))
    for release, keys in by_release.items():
        add(keys[:1], release)
    best: dict[tuple, Target] = {}
    for g, t in versions:
        cur = best.get(g)
        if cur is None or (t.level, not t.sps) > (cur.level, not cur.sps):
            best[g] = t
    return sorted(best.values(), key=lambda t: (t.title.casefold(), str(t.dest)))


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
    out: dict[Path, dict[str, Any]] = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            if f.lower().endswith(".lha"):
                p = Path(dp) / f
                m = marker_of(p)
                if m is not None:
                    out[p] = m
    return out


def _disk_data(t: Target) -> list[bytes]:
    if t.sps:
        with zipfile.ZipFile(t.disks[0][0]) as z:
            return [z.read(n) for n in sorted(z.namelist()) if n.lower().endswith(".ipf")]
    out = []
    for path, member in t.disks:
        if member:
            from .discarchive import open_member
            with open_member(path, member) as f:
                out.append(f.read())
        else:
            out.append(Path(path).read_bytes())
    return out


def build(todo: list[Target], root: Path, installs: list[dict[str, Any]], progress: Optional[Callable[[int, int, str], None]] = None,
          cancel: Any = None, fetch: Optional[Callable[[dict[str, Any]], bytes]] = None,
          decruncher: Optional[Callable[[], Any]] = None) -> dict[str, Any]:
    """Make ``todo`` real under ``root``: ``{"built", "moved", "kept", "removed", "failed": [{title, error}]}``. An archive that is the
    same is kept, one whose disks moved is moved, the others are made (atomically); ours that no target wants any more are removed."""
    from . import amigainstall as ai
    from .organiser import TEMP_MARKER
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
    stopped = lambda: cancel is not None and (cancel() if callable(cancel) else cancel.is_set())   # noqa: E731
    have = _ours(Path(root))
    by_key = {json.dumps({k: m.get(k) for k in ("source", "install", "install_date")}, sort_keys=True): p for p, m in have.items()}
    out: dict[str, Any] = {"built": [], "moved": [], "kept": 0, "removed": [], "failed": []}
    wanted: set[Path] = set()
    packages: dict[str, bytes] = {}
    for n, t in enumerate(todo):
        if stopped():
            break
        wanted.add(t.dest)
        inst = by_arc.get(t.archive)
        if inst is None:
            out["failed"].append({"title": t.title, "error": f"{t.archive} is not in the list of installs"})
            continue
        key = json.dumps(t.key(inst.get("date", "")), sort_keys=True)
        if t.dest in have and json.dumps({k: have[t.dest].get(k) for k in ("source", "install", "install_date")}, sort_keys=True) == key:
            out["kept"] += 1
            continue
        if t.dest.exists() and t.dest not in have:
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
            with ai.Package(packages[t.archive]) as pkg:
                res = pkg.install(_disk_data(t), cd=False, decruncher=decruncher, hints=ai.chipset_hints(t.names))
            files = ai.alias_icons(res.files)
            files[MARKER] = json.dumps({"app": "simple-rom-organiser", **t.key(inst.get("date", "")), "built": time.time()},
                                       indent=1).encode()
            tmp = t.dest.with_name(f"{t.dest.name}{TEMP_MARKER}whd")
            ai.write_lha(tmp, files, res.dirs)
            os.replace(tmp, t.dest)
            have[t.dest] = {}
            out["built"].append(str(t.dest))
        except Exception as exc:  # noqa: BLE001 - one game that cannot be built does not stop the others
            out["failed"].append({"title": t.title, "error": (str(exc) or type(exc).__name__)[:300]})
    if not stopped():
        for p in list(have):
            if p not in wanted and p.exists() and marker_of(p) is not None:
                p.unlink()                                    # (its game is no longer kept, or its version changed: ours, made again at will)
                out["removed"].append(str(p))
    if progress:
        progress(len(todo), len(todo), "WHDLoad games built")
    return out
