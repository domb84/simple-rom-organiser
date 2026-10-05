"""Where large temporary files go (Amendment 12): RAM when it is safe, else the app's own cache, never the ROMs.

``chdman extractcd`` writes a disc at its full raw size (hundreds of MB). That scratch space is chosen by ONE
policy, used by scans, *Verify fully* and the verification of a converted CHD:

1. **RAM** - a tmpfs (``/dev/shm``, ``$XDG_RUNTIME_DIR``, ``/tmp`` when it is tmpfs per ``/proc/mounts``) when its
   free space is >= the required size AND ``MemAvailable`` (``/proc/meminfo``) is >= required + a safety reserve
   (default 2 GiB, ``ROMORG_TEMP_RESERVE_MB`` / config ``temp_ram_reserve_mb``), so the machine is never pushed into swap.
2. **Disk** - ``<data dir>/cache/tmp`` (override: ``ROMORG_TEMP_DIR`` / config ``temp_dir``) when it has the free
   space. Never inside a library folder and never inside a reserved folder (``_unmatched`` ...).
3. Otherwise there is no scratch space: callers use the pure-Python reader (no temp files) and report why.

Required size = the real track bytes (frames - pad, x 2352) + 5 % + 64 MiB.

Every job gets its own sub-folder ``romorg-job-<id>/`` carrying a marker file; only folders with the prefix AND the
marker are ever swept (:func:`sweep_stale`, at startup / before a scan / after a job) - nothing else is deleted.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

JOB_PREFIX = "romorg-job-"
MARKER = ".romorg-temp-marker"
MARKER_APP = "simple-rom-organiser"
ENV_DIR = "ROMORG_TEMP_DIR"
ENV_RESERVE = "ROMORG_TEMP_RESERVE_MB"
CONFIG_DIR = "temp_dir"
CONFIG_RESERVE = "temp_ram_reserve_mb"
DEFAULT_RESERVE = 2 * 1024 ** 3
MARGIN_FIXED = 64 * 1024 ** 2
STALE_AGE = 24 * 3600
_MIB = 1024 * 1024
PROC_MEMINFO = "/proc/meminfo"
PROC_MOUNTS = "/proc/mounts"

_settings: dict[str, Any] = {}
_lock = threading.Lock()
_active: set[Path] = set()
_report: dict[str, Any] = {}


class TempUnavailable(Exception):
    """No RAM and no disk location can hold the extraction (``str(exc)`` says why for each)."""


# --------------------------------------------------------------------------- settings

def configure(config: Optional[dict] = None) -> None:
    """Take ``temp_dir`` / ``temp_ram_reserve_mb`` from the saved config (env vars win)."""
    cfg = config if isinstance(config, dict) else {}
    with _lock:
        _settings.clear()
        d = cfg.get(CONFIG_DIR)
        if isinstance(d, str) and d.strip():
            _settings["dir"] = d.strip()
        r = cfg.get(CONFIG_RESERVE)
        if isinstance(r, (int, float)) and not isinstance(r, bool) and r >= 0:
            _settings["reserve"] = int(r) * _MIB


def reserve_bytes() -> int:
    raw = os.environ.get(ENV_RESERVE, "").strip()
    if raw:
        try:
            return max(0, int(float(raw) * _MIB))
        except ValueError:
            pass
    return int(_settings.get("reserve", DEFAULT_RESERVE))


def required_bytes(track_bytes: int) -> int:
    """Real track bytes + 5 % + 64 MiB."""
    return int(track_bytes) + int(track_bytes) // 20 + MARGIN_FIXED


def disk_root() -> Path:
    """The disk scratch folder: ``$ROMORG_TEMP_DIR`` / config ``temp_dir`` / ``<data dir>/cache/tmp``."""
    raw = os.environ.get(ENV_DIR, "").strip() or str(_settings.get("dir", "")).strip()
    if raw:
        return Path(os.path.expanduser(raw))
    from . import paths
    return paths._base_data_dir() / "cache" / "tmp"


# --------------------------------------------------------------------------- system probes (monkeypatched in tests)

def mem_available() -> Optional[int]:
    """``MemAvailable`` in bytes from ``/proc/meminfo`` (None when it cannot be read)."""
    try:
        with open(PROC_MEMINFO, "r", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def free_bytes(path: Path) -> int:
    """Free bytes (for an unprivileged user) of the file system holding ``path`` or its nearest existing parent."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    if not hasattr(os, "statvfs"):  # Windows
        return shutil.disk_usage(p).free
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize


def tmpfs_mounts() -> list[str]:
    """Mount points of RAM-backed file systems (``tmpfs`` / ``ramfs``) per ``/proc/mounts``."""
    out: list[str] = []
    try:
        with open(PROC_MOUNTS, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[2] in ("tmpfs", "ramfs"):
                    out.append(parts[1].replace("\\040", " "))
    except OSError:
        pass
    return out


def _on_tmpfs(path: Path, mounts: Sequence[str]) -> bool:
    """Whether ``path`` is on a tmpfs: the longest mount point that contains it is a tmpfs mount."""
    best = ""
    try:
        with open(PROC_MOUNTS, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                if (path == Path(mp) or Path(mp) in path.parents) and len(mp) >= len(best):
                    best = mp
    except OSError:
        return False
    return bool(best) and best in mounts


def ram_roots() -> list[Path]:
    """RAM-backed candidate folders, best first: ``/dev/shm``, ``$XDG_RUNTIME_DIR``, ``/tmp`` when it is a tmpfs."""
    mounts = tmpfs_mounts()
    out: list[Path] = []
    cands = [Path("/dev/shm"), Path(os.environ.get("XDG_RUNTIME_DIR", "") or "/nonexistent-run"), Path("/tmp")]
    for c in cands:
        try:
            if c.is_dir() and os.access(c, os.W_OK) and c not in out and _on_tmpfs(c.resolve(), mounts):
                out.append(c)
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------- policy

@dataclass
class Plan:
    kind: str                      # "ram" | "disk" | "none"
    root: Optional[Path]
    reason: str
    needed: int = 0

    @property
    def message(self) -> str:
        if self.kind == "ram":
            return "decoding in RAM"
        if self.kind == "disk":
            return f"decoding on disk: {self.root}"
        return "no temporary space: " + self.reason


def _mb(n: int) -> str:
    return f"{n / _MIB:,.0f} MB" if n < 1024 ** 3 else f"{n / 1024 ** 3:.1f} GB"


def _inside(child: Path, parent: Path) -> bool:
    try:
        c, p = child.resolve(), parent.resolve()
    except OSError:
        c, p = child.absolute(), parent.absolute()
    return c == p or p in c.parents


def _disk_blocked(root: Path, avoid: Iterable[Path]) -> str:
    """Why ``root`` may not be used as scratch space ('' when it may)."""
    from . import folders
    for a in avoid:
        if a is None:
            continue
        if _inside(root, Path(a)):
            return f"{root} is inside the library folder {a}"
    if any(folders.canonical_name(part) for part in root.parts):
        return f"{root} is inside a reserved folder"
    return ""


def choose(needed: int, avoid: Sequence[Path] = (), accept: Optional[Callable[[Path], str]] = None) -> Plan:
    """The scratch location for ``needed`` bytes (already including the margin) - see the module docstring.

    ``avoid`` = library / ROM folders that must never hold temp files. ``accept(root)`` may return a reason string
    to reject a candidate (e.g. a Flatpak chdman that cannot see it).
    """
    notes: list[str] = []
    reserve = reserve_bytes()
    mem = mem_available()
    for root in ram_roots():
        if _disk_blocked(root, avoid):
            continue
        try:
            free = free_bytes(root)
        except OSError:
            continue
        if free < needed:
            notes.append(f"{root} has only {_mb(free)} free, {_mb(needed)} needed")
            continue
        if mem is None:
            notes.append("available RAM unknown, RAM not used")
            continue
        if mem < needed + reserve:
            notes.append(f"RAM not used: {_mb(mem)} available, {_mb(needed)} + {_mb(reserve)} reserve needed")
            continue
        why = accept(root) if accept else ""
        if why:
            notes.append(f"{root}: {why.splitlines()[0]}")
            continue
        return Plan("ram", root, f"{root}: {_mb(free)} free, {_mb(mem)} RAM available "
                                 f"(needs {_mb(needed)} + {_mb(reserve)} reserve)", needed)
    d = disk_root()
    blocked = _disk_blocked(d, avoid)
    if blocked:
        notes.append(f"disk: {blocked}")
    else:
        try:
            free = free_bytes(d)
        except OSError as exc:
            notes.append(f"disk: {d}: {exc}")
        else:
            if free < needed:
                notes.append(f"disk {d} has only {_mb(free)} free, {_mb(needed)} needed")
            else:
                why = accept(d) if accept else ""
                if why:
                    notes.append(f"{d}: {why.splitlines()[0]}")
                else:
                    return Plan("disk", d, "; ".join(notes) or f"{d}: {_mb(free)} free", needed)
    return Plan("none", None, "; ".join(notes) or "no usable location", needed)


# --------------------------------------------------------------------------- job folders

@dataclass
class Workdir:
    path: Path
    plan: Plan

    @property
    def where(self) -> str:
        return self.plan.kind

    @property
    def message(self) -> str:
        return self.plan.message

    def remove(self) -> None:
        remove(self.path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire(needed: int, avoid: Sequence[Path] = (), accept: Optional[Callable[[Path], str]] = None) -> Workdir:
    """Pick a location (:func:`choose`) and create a marked, unique job folder in it. Raises :class:`TempUnavailable`."""
    plan = choose(needed, avoid, accept)
    if plan.root is None:
        note(plan)
        raise TempUnavailable(plan.reason)
    try:
        plan.root.mkdir(parents=True, exist_ok=True)
        work = plan.root / f"{JOB_PREFIX}{uuid.uuid4().hex[:12]}"
        work.mkdir(mode=0o700)
        (work / MARKER).write_text(json.dumps({"app": MARKER_APP, "pid": os.getpid(), "created": time.time()}),
                                   encoding="utf-8")
    except OSError as exc:
        raise TempUnavailable(f"cannot create a temporary folder in {plan.root}: {exc}") from exc
    with _lock:
        _active.add(work)
    note(plan)
    return Workdir(work, plan)


def remove(work: Optional[Path]) -> None:
    """Delete a job folder - only when it has the prefix and our marker."""
    if work is None:
        return
    work = Path(work)
    with _lock:
        _active.discard(work)
    if work.name.startswith(JOB_PREFIX) and (work / MARKER).is_file() and not work.is_symlink():
        shutil.rmtree(work, ignore_errors=True)


def _marker(p: Path) -> Optional[dict]:
    try:
        if p.is_symlink() or not p.is_dir() or not p.name.startswith(JOB_PREFIX):
            return None
        m = p / MARKER
        if m.is_symlink() or not m.is_file():
            return None
        data = json.loads(m.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("app") == MARKER_APP else None
    except (OSError, ValueError):
        return None


def sweep_stale(roots: Optional[Iterable[Path]] = None, max_age: float = STALE_AGE) -> list[str]:
    """Delete leftover job folders (prefix + marker) of dead runs in every candidate root; returns their paths.

    A folder of a live other process younger than ``max_age`` stays; our own active folders stay."""
    if roots is None:
        roots = candidate_roots()
    removed: list[str] = []
    for root in dict.fromkeys(Path(r) for r in roots):
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for p in entries:
            data = _marker(p)
            if data is None:
                continue
            with _lock:
                if p in _active:
                    continue
            try:
                pid = int(data.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            try:
                age = time.time() - p.stat().st_mtime
            except OSError:
                age = 0
            if pid and _pid_alive(pid) and age < max_age:
                continue
            shutil.rmtree(p, ignore_errors=True)
            if not p.exists():
                removed.append(str(p))
    return removed


def candidate_roots() -> list[Path]:
    return [*ram_roots(), disk_root()]


def cleanup_active() -> None:
    """Remove every job folder this process still owns (atexit / cancel)."""
    with _lock:
        mine = list(_active)
    for p in mine:
        remove(p)


atexit.register(cleanup_active)


# --------------------------------------------------------------------------- what happened (status JSON / UI)

def reset_report() -> None:
    with _lock:
        _report.clear()


def note(plan: Plan) -> None:
    """Remember where a decode went (``where`` counters + the last location and reason)."""
    with _lock:
        _report[plan.kind] = int(_report.get(plan.kind, 0)) + 1
        _report["last"] = {"where": plan.kind, "path": str(plan.root) if plan.root else "", "reason": plan.reason}


def note_python(reason: str) -> None:
    with _lock:
        _report["python"] = int(_report.get("python", 0)) + 1
        _report["last"] = {"where": "python", "path": "", "reason": reason}


def report() -> dict[str, Any]:
    """``{"ram": n, "disk": n, "python": n, "last": {where, path, reason}, "text": "..."}`` (empty when nothing was decoded)."""
    with _lock:
        out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _report.items()}
    if not out:
        return {}
    bits = []
    for k, label in (("ram", "in RAM"), ("disk", "on disk"), ("python", "with the built-in reader (no temp files)")):
        if out.get(k):
            bits.append(f"{out[k]} {label}")
    last = out.get("last", {})
    text = "Temporary space: decoded " + ", ".join(bits)
    if last.get("where") == "disk" and last.get("path"):
        text += f" ({last['path']})"
    if last.get("where") in ("ram", "disk", "python") and last.get("reason"):
        text += " - " + last["reason"]
    out["text"] = text
    return out
