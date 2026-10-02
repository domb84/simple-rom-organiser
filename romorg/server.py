"""Local HTTP server: JSON API, static UI files and background jobs.

Security model (the server can rename files, so it is locked down):

* binds to 127.0.0.1 by default;
* rejects requests whose ``Host`` header is not ``127.0.0.1:<port>`` /
  ``localhost:<port>`` (defeats DNS-rebinding attacks from web pages);
* every mutating endpoint is POST + JSON and requires the per-run secret in
  the ``X-Romorg-Token`` header (the token is injected into index.html).

The other ``romorg`` modules are imported lazily through :func:`_mod` so the
server starts quickly and tests can substitute fakes via ``sys.modules``.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import importlib
import importlib.resources
import inspect
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

PACKAGE = "romorg"
STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
TOKEN_PLACEHOLDER = "__ROMORG_TOKEN__"
TOKEN_HEADER = "X-Romorg-Token"
MAX_BODY = 1 << 20  # 1 MiB of JSON is plenty
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _mod(name: str) -> Any:
    """Import a sibling module lazily (looked up in sys.modules first)."""
    return importlib.import_module(f"{PACKAGE}.{name}")


class ApiError(Exception):
    """An error reported to the client as ``{"error": message}``."""

    def __init__(self, status: int, message: str, error_code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_code = error_code  # machine-readable, reported on the job ("offline_no_dats", ...)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    return str(obj)


def dumps(data: Any) -> bytes:
    # ASCII-only JSON: filenames with undecodable bytes (surrogate escapes) become
    # "\udcXX" escapes, which the browser keeps and sends back unchanged, so they
    # round-trip to the same str (and path) instead of failing to encode.
    return json.dumps(data, default=_json_default, ensure_ascii=True).encode("ascii")


# --------------------------------------------------------------------------- jobs


class CancelToken(threading.Event):
    """Event that can also be called: ``cancel()`` -> True once cancelled.

    Core modules may treat ``cancel`` either as a callable or as an Event.
    """

    def __call__(self) -> bool:
        return self.is_set()


class Job:
    """A background task with progress reporting."""

    def __init__(self, job_id: int, kind: str, cancellable: bool = True) -> None:
        self.id = job_id
        self.kind = kind
        self.cancellable = cancellable
        self.status = "running"
        self.progress: dict[str, Any] = {"done": 0, "total": 0, "message": ""}
        self.result: Any = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.cancel = CancelToken()
        self.started = time.time()
        self.finished: float | None = None
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def report(self, *args: Any, **kwargs: Any) -> None:
        """Flexible progress callback.

        Accepts ``(done, total)``, ``(done, total, message)`` or a bare message;
        numbers fill done/total in order, the first string/path is the message.
        """
        numbers = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
        texts = [a for a in args if isinstance(a, (str, os.PathLike))]
        with self._lock:
            if numbers:
                self.progress["done"] = numbers[0]
                self.progress["total"] = numbers[1] if len(numbers) > 1 else 0
            for key in ("done", "total", "message"):
                if key in kwargs:
                    self.progress[key] = kwargs[key]
            if texts:
                msg = texts[0]
                self.progress["message"] = Path(msg).name if isinstance(msg, os.PathLike) else msg

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "cancellable": self.cancellable,
                "progress": dict(self.progress),
                "result": self.result,
                "error": self.error,
                "error_code": self.error_code,
                "started": self.started,
                "finished": self.finished,
            }


class JobManager:
    """Runs at most one background job at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Job | None = None
        self._next_id = 1

    def start(self, kind: str, fn: Callable[[Job], Any], cancellable: bool = True) -> Job:
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise ApiError(HTTPStatus.CONFLICT, f"A {self._current.kind} job is already running")
            job = Job(self._next_id, kind, cancellable)
            self._next_id += 1
            self._current = job
        job.thread = threading.Thread(target=self._run, args=(job, fn), name=f"job-{kind}", daemon=True)
        job.thread.start()
        return job

    @staticmethod
    def _run(job: Job, fn: Callable[[Job], Any]) -> None:
        code = ""
        try:
            result = fn(job)
            status, error = ("cancelled" if job.cancel.is_set() else "done"), None
        except Exception as exc:  # reported to the UI
            result = None
            if job.cancel.is_set():
                status, error = "cancelled", None
            else:
                traceback.print_exc()
                status, error = "error", getattr(exc, "message", "") or str(exc) or type(exc).__name__
                code = getattr(exc, "error_code", "") if isinstance(exc, ApiError) else ""
        with job._lock:
            job.result = result
            job.status = status
            job.error = error
            job.error_code = code if status == "error" and code else None
            job.finished = time.time()

    @property
    def current(self) -> Job | None:
        with self._lock:
            return self._current

    def cancel(self) -> bool:
        job = self.current
        if job is None or job.status != "running" or not job.cancellable:
            return False
        job.cancel.set()
        return True


# --------------------------------------------------------------------- app state

from . import folders  # noqa: E402  (the reserved folder names live in folders.py only)
from .folders import CONVERTED_DIR, RESERVED_DIRS, SUPERSEDED_DIR, UNMATCHED_DIR  # noqa: E402
# Organiser statuses that mean "this op changes something on disk".
ACTIONABLE = ("move", "rename", "delete")
# Display / sort order for organiser statuses ("grouped by status").
ORGANISE_ORDER = ("move", "rename", "delete", "conflict", "duplicate", "skip", "ok")
DIALOG_TIMEOUT = 300  # seconds before an unanswered native folder dialog is given up
STOP_TIMEOUT = 60     # seconds to wait for a running job when the app is told to exit
# Top-level folder names of other systems (EmuDeck / ES-DE / RetroDECK): seeing several
# of them in the plan means the chosen folder is probably the whole "roms" folder.
OTHER_SYSTEM_DIRS = {
    "3do", "3ds", "arcade", "atari2600", "atari5200", "atari7800", "atarijaguar", "atarilynx",
    "atarist", "c64", "dreamcast", "fbneo", "gamegear", "gb", "gba", "gbc", "gc", "genesis",
    "mame", "mastersystem", "megadrive", "msx", "n3ds", "n64", "nds", "neogeo", "nes", "ngp",
    "pcengine", "ps2", "ps3", "psp", "psvita", "psx", "saturn", "sega32x", "segacd", "snes",
    "switch", "wii", "wiiu", "xbox", "zxspectrum", "amstradcpc", "dos", "scummvm",
}
KICKSTART_ORDER = ("copy", "conflict", "ok", "missing", "unmatched")
CONVERT_ORDER = ("convert", "conflict", "skip")
MAX_TITLE = 120
LAYOUT_PER_DAT, LAYOUT_FLAT = "per_dat", "flat"
LAYOUT_GAME_FOLDER = "game_folder"   # Sega Dreamcast: <root>/<Redump name>/<Redump name>.chd
CHDMAN_TTL = 60.0                    # seconds a chdman detection result is reused
SOURCE_TOSEC, SOURCE_NOINTRO = "tosec", "nointro"
# Result kinds whose rows carry name tags (filterable by region / language / video / flag / rule).
TAG_KINDS = ("matched", "missing", "games")
TAG_FILTERS = ("region", "language", "video", "flag", "rule")
# Reason categories of the Build library plan (row filter ``reason``).
LIBRARY_REASONS = ("kept", "excluded", "superseded", "incomplete", "duplicate", "unmatched", "playlist")
# Profile fields the save endpoint accepts (besides ``reset``).
PROFILE_KEYS = ("exclude", "latest_only", "best_variant", "complete_only", "languages", "keep_flags",
                "rescue_only_dump", "region_priority", "one_per_game")
# Fallback labels of the exclusion rules (``tags.RULE_LABELS`` wins when present).
RULE_LABELS = {
    "bad_dump": "Bad dumps [b]", "virus": "Virus-infected [v]", "bad_size": "Over/under dumps [o] [u]",
    "pre_release": "Pre-release / beta / alpha / preview / debug", "prototype": "Prototypes",
    "demo": "Demos, samples, kiosk", "faked": "Faked [faked]", "unreleased": "Unreleased",
    "modified": "Modified [m]",
}


@dataclass
class ScanState:
    """The most recent completed scan plus derived plans (cached)."""

    result: Any
    root: Path
    platform: Any                      # platforms.Platform
    dat_names: list[str]               # DATs actually loaded, priority order
    missing_dats: list[str]            # platform DATs that are not downloaded
    recursive: bool = True
    layout: str = LAYOUT_PER_DAT       # platform layout: "per_dat" | "flat"
    # by (move_unmatched, latest_only)
    rename_plans: dict[tuple[bool, bool], list[Any]] = field(default_factory=dict)
    # Build library plans (organiser.LibraryPlan) by (move_unmatched, savedisk, labels); cleared when
    # the library profile changes.
    library_plans: dict[tuple[bool, bool, bool], Any] = field(default_factory=dict)
    dats_sig: tuple = ()               # signature of the DAT files this scan parsed
    dats_changed: bool = False         # a DAT update was installed after the scan
    convert_plans: dict[bool, list[Any]] = field(default_factory=dict)  # by latest_only
    m3u_plans: dict[tuple[bool, bool], list[Any]] = field(default_factory=dict)
    # Serialised result rows (item, lower-case search text) and the summary, built
    # once per scan so paging / searching is a cheap slice.
    items: dict[Any, list[tuple[dict[str, Any], str]]] = field(default_factory=dict)
    summary: dict[str, Any] | None = None


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` passing only the keyword arguments its signature accepts.

    Lets the server pass optional hints (e.g. ``m3u_dats``) to core functions
    whose exact signature may grow over time without breaking older ones.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


def _rel(path: Any, root: Path) -> str:
    """POSIX path relative to root (string ops: this runs for every row of big scans)."""
    text, base = os.fspath(path), os.fspath(root).rstrip(os.sep)
    if text.startswith(base + os.sep):
        return text[len(base) + 1:].replace(os.sep, "/")
    try:
        return Path(path).relative_to(root).as_posix()
    except (ValueError, TypeError):
        return str(path)


def _entry_rel(entry: Any, root: Path) -> str:
    text = _rel(entry.path, root)
    return f"{text}::{entry.member}" if getattr(entry, "member", None) else text


def _int_arg(value: Any, default: int, lo: int = 0, hi: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    number = max(lo, number)
    return min(hi, number) if hi is not None else number


def _bool_arg(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _str_arg(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _search_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in item.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(v for v in value if isinstance(v, str))
    return "\n".join(parts).lower()


def _rows(items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    return [(i, _search_text(i)) for i in items]


def _page(items: list[Any], offset: Any, limit: Any, q: Any) -> dict[str, Any]:
    """Filter by case-insensitive substring over all string values, then slice.

    ``items`` are dicts, or ``(dict, search text)`` rows with the text precomputed.
    """
    query = (q or "").strip().lower() if isinstance(q, str) else ""
    rows = [i if isinstance(i, tuple) else (i, None) for i in items]
    if query:
        rows = [r for r in rows if query in (r[1] if r[1] is not None else _search_text(r[0]))]
    items = [r[0] for r in rows]
    off = _int_arg(offset, 0)
    lim = _int_arg(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
    return {"total": len(items), "offset": off, "limit": lim, "items": items[off:off + lim]}


def _status_counts(ops: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for op in ops:
        counts[op.status] = counts.get(op.status, 0) + 1
    return counts


def _order_key(order: tuple[str, ...]) -> Callable[[Any], int]:
    return lambda op: order.index(op.status) if op.status in order else len(order)


def _rom_dat(rom: Any) -> str:
    return getattr(rom, "dat", "") or ""


def _primary(match: Any, order: list[str]) -> list[Any]:
    """Matching roms from the highest-priority DAT (``Match.primary`` if provided)."""
    prim = getattr(match, "primary", None)
    if callable(prim):
        prim = prim()
    if prim:
        return list(prim)
    roms = list(match.roms)
    for name in order:
        chosen = [r for r in roms if _rom_dat(r) == name]
        if chosen:
            return chosen
    return roms


@functools.lru_cache(maxsize=4096)
def _safe_name_cached(organiser: Any, name: str) -> str:
    return str(organiser.safe_filename(name))


def _safe_name(name: str) -> str:
    """Folder name of a DAT inside the platform root (organiser.safe_filename)."""
    try:
        return _safe_name_cached(_mod("organiser"), name)
    except Exception:
        return name


def _enum_str(value: Any, default: str) -> str:
    """``str`` of a plain string or a ``str``-Enum member (``DatSource.NOINTRO`` -> ``"nointro"``)."""
    value = getattr(value, "value", value)
    return value if isinstance(value, str) and value else default


def _source(platform: Any) -> str:
    """Where a platform's DATs come from: "tosec" (default) or "nointro"."""
    return _enum_str(getattr(platform, "source", None), SOURCE_TOSEC)


def _has_kickstart(platform: Any) -> bool:
    """A Kickstart step exists: a TOSEC firmware DAT or the platform's own Kickstart folder."""
    return bool(getattr(platform, "kickstart_dat", None) or getattr(platform, "kickstart_folder", ""))


def _layout(platform: Any) -> str:
    """Platform layout: "per_dat" (one folder per DAT, default) or "flat" (everything in the root)."""
    return _enum_str(getattr(platform, "layout", None), LAYOUT_PER_DAT)


def _optional_mod(name: str) -> Any:
    """A sibling module, or None if it does not exist (yet)."""
    try:
        return _mod(name)
    except ImportError:
        return None


def _tags_json(rom: Any, tags_mod: Any) -> dict[str, Any] | None:
    """``tags.to_json`` of a rom's parsed name tags (``Rom.tags``), or None if unavailable."""
    if tags_mod is None or rom is None:
        return None
    try:
        parsed = getattr(rom, "tags", None)
        if parsed is None:
            set_name = getattr(rom, "set_name", "") or ""
            parsed = tags_mod.parse_name(set_name or rom.name, "nointro" if set_name else "tosec")
        return dict(parsed) if isinstance(parsed, dict) else dict(tags_mod.to_json(parsed))
    except Exception:  # noqa: BLE001 - tags are informative only
        return None


def _tag_values(tags: dict[str, Any] | None, key: str) -> list[str]:
    """Values of one tag facet: region / language / video / flag (status, flags, bad, bios)."""
    if not tags:
        return []
    if key == "region":
        return list(tags.get("regions") or ())
    if key == "language":
        return list(tags.get("languages") or ())
    if key == "video":
        return list(tags.get("video") or ())
    if key == "rule":  # library exclusion rules this name hits (tags.excluded_by)
        return [e["rule"] for e in tags.get("excluded_by") or () if isinstance(e, dict) and e.get("rule")]
    out = [tags["status"]] if tags.get("status") else []
    out.extend(tags.get("flags") or ())
    if tags.get("bad"):
        out.append("bad")
    if tags.get("bios"):
        out.append("bios")
    out.extend(_dump_flag_values(tags))
    return out


def _dump_flag_values(tags: dict[str, Any]) -> list[str]:
    """TOSEC / No-Intro ``[..]`` flags as filter values by their code: ``[cr Fairlight]`` ->
    ``"[cr]"``, ``[a2]`` -> ``"[a]"``, ``[!]`` -> ``"[!]"`` (``[b]`` / ``[BIOS]`` are ``bad`` / ``bios``)."""
    out: list[str] = []
    for flag in tags.get("dump_flags") or ():
        m = re.match(r"^(!|[a-z]+)(?=\d|\s|$)", str(flag))
        if m and m.group(1) != "b":
            out.append(f"[{m.group(1)}]")
    return out


# Flag kinds (tags.flag_kind) that make sense as filter chips; free text such as publishers
# or developers ("other"), disk labels, dates and serials are left out of the facet (a filter
# on them still works when passed explicitly).
FACET_SKIP_FLAG_KINDS = {"other", "disk", "date", "serial"}


def _facet_flags(tags: dict[str, Any], tags_mod: Any) -> list[str]:
    values = _tag_values(tags, "flag")
    if tags_mod is None or not hasattr(tags_mod, "flag_kind"):
        return values
    plain = set(tags.get("flags") or ())
    status = tags.get("status")
    return [v for v in values
            if v not in plain or v == status or tags_mod.flag_kind(v) not in FACET_SKIP_FLAG_KINDS]


def _facets(rows: list[tuple[dict[str, Any], str]]) -> dict[str, dict[str, int]]:
    """``{regions, languages, video, flags, rules: {value: count}}`` over the rows' tags."""
    out: dict[str, dict[str, int]] = {"regions": {}, "languages": {}, "video": {}, "flags": {}, "rules": {}}
    tags_mod = _optional_mod("tags")
    for item, _ in rows:
        tags = item.get("tags")
        if not tags:
            continue
        for key, bucket in (("region", "regions"), ("language", "languages"), ("video", "video"), ("flag", "flags"),
                           ("rule", "rules")):
            counts = out[bucket]
            values = _facet_flags(tags, tags_mod) if key == "flag" else _tag_values(tags, key)
            for value in dict.fromkeys(values):
                counts[value] = counts.get(value, 0) + 1
    return {k: dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0].casefold()))) for k, v in out.items()}


def _tag_filter(rows: list[tuple[dict[str, Any], str]], query: dict[str, str]) -> list[tuple[dict[str, Any], str]]:
    """Keep rows whose tags have every requested region / language / video / flag (case-insensitive)."""
    wanted = [(key, query[key].strip().casefold()) for key in TAG_FILTERS if query.get(key, "").strip()]
    if not wanted:
        return rows
    return [r for r in rows
            if all(value in {v.casefold() for v in _tag_values(r[0].get("tags"), key)} for key, value in wanted)]


def _which_7z() -> str | None:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _in_game_mode() -> bool:
    """True under SteamOS Game Mode (gamescope), where native dialogs may never show."""
    env = os.environ
    return (env.get("SteamGamepadUI") == "1" or env.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope"
            or bool(env.get("GAMESCOPE_WAYLAND_DISPLAY")))


def _dialog_command() -> str | None:
    """Name of an available native folder dialog tool (Linux desktop only)."""
    if not sys.platform.startswith("linux"):
        return None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or _in_game_mode():
        return None
    for name in ("kdialog", "zenity"):
        if shutil.which(name):
            return name
    return None


def _places() -> list[dict[str, str]]:
    """Useful starting points for the folder browser (home, SD cards, USB...)."""
    places: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(name: str, path: Path) -> None:
        try:
            if not path.is_dir():
                return
        except OSError:
            return
        key = str(path)
        if key not in seen:
            seen.add(key)
            places.append({"name": name, "path": key})

    home = Path.home()
    add("Home", home)
    add("Emulation ROMs", home / "Emulation" / "roms")

    def children(base: Path) -> list[Path]:
        try:
            return sorted((p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
                          key=lambda p: p.name.lower())
        except OSError:
            return []

    if sys.platform.startswith("linux"):
        # SteamOS mounts SD cards / USB drives under /run/media/<user>/<label>
        # (older images used /run/media/<device>).
        for base in (Path("/run/media"), Path("/media")):
            for child in children(base):
                if os.path.ismount(child):
                    add(child.name, child)
                else:
                    for sub in children(child):
                        add(sub.name, sub)
        for child in children(Path("/mnt")):
            add(child.name, child)
    elif sys.platform == "darwin":
        for child in children(Path("/Volumes")):
            add(child.name, child)
    elif sys.platform.startswith("win"):
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            add(f"{letter}:", Path(f"{letter}:\\"))
    if not sys.platform.startswith("win"):
        add("Computer (/)", Path("/"))
    return places


def _check_scan_root(path: Path) -> None:
    """Refuse folders that are far too broad to organise (/, home, a drive's root)."""
    home = Path.home()
    try:
        home = home.resolve()
    except OSError:
        pass
    hint = " - choose the platform folder itself (e.g. .../Emulation/roms/amiga)"
    if path == Path(path.anchor) or path == home or path in home.parents:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{path} is too broad to organise{hint}")
    try:
        is_mount = os.path.ismount(path)
    except OSError:
        is_mount = False
    if is_mount:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{path} is the root of a drive or SD card{hint}")
    try:
        data = Path(_mod("paths").data_dir()).resolve()
    except Exception:
        data = None
    if data is not None and (path == data or data in path.parents):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{path} is inside the app's own data folder{hint}")


# The single ``kickstart_dest`` of older versions belonged to the TOSEC Amiga system only; it is
# migrated to ``kickstart_dests["Commodore Amiga"]`` (per-platform Kickstart destinations).
LEGACY_KICKSTART_PLATFORM = "Commodore Amiga"


def _migrate_config(cfg: dict[str, Any]) -> None:
    """In-place config migrations (idempotent): ``kickstart_dest`` -> ``kickstart_dests[Amiga]``."""
    legacy = cfg.pop("kickstart_dest", None) if "kickstart_dest" in cfg else None
    if isinstance(legacy, str) and legacy:
        table = cfg.get("kickstart_dests") if isinstance(cfg.get("kickstart_dests"), dict) else {}
        table.setdefault(LEGACY_KICKSTART_PLATFORM, legacy)
        cfg["kickstart_dests"] = table


def _kick_dest(cfg: dict[str, Any], platform_name: str) -> str | None:
    """The saved Kickstart destination of one platform (legacy single value = the Amiga's)."""
    table = cfg.get("kickstart_dests") if isinstance(cfg.get("kickstart_dests"), dict) else {}
    value = table.get(platform_name)
    if not value and platform_name == LEGACY_KICKSTART_PLATFORM:
        value = cfg.get("kickstart_dest")
    return value if isinstance(value, str) and value else None


def _undo_log_count(data: Any) -> int | None:
    """Number of moves recorded in an undo log (list, or dict holding a list)."""
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("moves", "ops", "renames", "entries"):
            if isinstance(data.get(key), list):
                return len(data[key])
    return None


class _NoUpdates:
    """Stand-in when ``romorg.autoupdate`` is unavailable: never updates, never blocks."""

    enabled = False
    dat_lock = None

    def start_background(self) -> None:
        pass

    def check(self, force: bool = False) -> bool:
        return False

    def cancel(self) -> bool:
        return False

    def ensure(self, platform: Any, progress: Any = None, cancel: Any = None) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"enabled": False, "state": "idle", "running": False, "offline": False, "notice": "",
                "last_checked": None, "scan_stale": False,
                "progress": {"done": 0, "total": 0, "message": "", "source": ""}, "error": None,
                "tosec": {"installed": None, "latest": None, "status": "unknown", "checked_at": None},
                "nointro": {"installed": None, "latest": None, "status": "unknown", "dats": [],
                            "checked_at": None},
                "whdload": {"installed": None, "latest": None, "status": "unknown", "dats": [],
                            "checked_at": None}}


class App:
    """Server-side application state shared by all request handlers."""

    def __init__(self, token: str | None = None, auto_update: bool = True) -> None:
        self.token = token or secrets.token_urlsafe(24)
        self.jobs = JobManager()
        self._lock = threading.Lock()
        self._fallback_dat_lock = threading.Lock()
        self.updates = self._make_updates(auto_update)
        self._scan: ScanState | None = None
        self._dat_cache: tuple[Any, tuple[list[Any], list[str]]] | None = None
        self._lang_cache: dict[Any, list[dict[str, Any]]] = {}  # available languages of one platform
        self._chdman_cache: tuple[float, Any, dict[str, Any]] | None = None
        self.closing = threading.Event()  # set when the app is told to exit
        self.shutdown_hook: Callable[[], None] | None = None  # set by make_server

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _make_updates(auto_update: bool) -> Any:
        """The DAT update manager (``autoupdate.UpdateManager``); a disabled stand-in if unavailable."""
        enabled = bool(auto_update)
        try:
            if _mod("paths").offline_forced():
                enabled = False
        except Exception:  # noqa: BLE001 - older paths module / import problems
            pass
        module = _optional_mod("autoupdate")
        if module is not None:
            try:
                return module.UpdateManager(enabled=enabled)
            except Exception:  # noqa: BLE001 - never stop the app from starting
                traceback.print_exc()
        return _NoUpdates()

    @property
    def dat_lock(self) -> Any:
        """Held while DATs are swapped in or parsed (``UpdateManager.dat_lock``)."""
        return getattr(self.updates, "dat_lock", None) or self._fallback_dat_lock

    @staticmethod
    def _config() -> dict[str, Any]:
        try:
            return dict(_mod("paths").load_config())
        except Exception:
            traceback.print_exc()
            return {}

    def _config_update(self, folder_for: tuple[str, str | None] | None = None,
                       latest_for: tuple[str, bool] | None = None, strict: bool = False,
                       kickstart_for: tuple[str, str | None] | None = None,
                       **values: Any) -> dict[str, Any]:
        """Merge values into config.json and return the saved config.

        ``folder_for=(platform, path)`` remembers a platform's folder (``path=None`` forgets it);
        ``latest_for=(platform, bool)`` remembers the "latest version only" choice;
        ``kickstart_for=(platform, path)`` remembers a platform's Kickstart destination.
        The change is applied to the LATEST config.json under ``paths.update_config``'s lock, so a
        concurrent writer (scan, profile save, ...) can never be overwritten by a stale snapshot.
        ``strict``: a failed write raises ``ApiError(500)`` instead of being swallowed.
        """
        def mutate(cfg: dict[str, Any]) -> None:
            cfg.update(values)
            for key, pair in (("folders", folder_for), ("latest_only", latest_for),
                              ("kickstart_dests", kickstart_for)):
                if pair is None:
                    continue
                table = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
                if pair[1] is None:
                    table.pop(pair[0], None)
                else:
                    table[pair[0]] = pair[1]
                cfg[key] = table
            _migrate_config(cfg)

        try:
            return dict(_mod("paths").update_config(mutate))
        except Exception as exc:  # config is a convenience only (but a folder save must say so)
            traceback.print_exc()
            if strict:
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                               f"Could not write config.json ({exc}) - the setting was not saved") from exc
            return {}

    def _folders(self, cfg: dict[str, Any] | None = None) -> dict[str, str]:
        cfg = self._config() if cfg is None else cfg
        folders = cfg.get("folders") if isinstance(cfg.get("folders"), dict) else {}
        return {k: v for k, v in folders.items() if isinstance(v, str)}

    def _latest_only(self, platform: Any, cfg: dict[str, Any] | None = None) -> bool:
        """The "latest versions only" choice of a platform = ``profile.latest_only`` (legacy flag included)."""
        cfg = self._config() if cfg is None else cfg
        library = _optional_mod("library")
        if library is not None:
            try:
                return bool(library.load_profile(cfg, platform).latest_only)
            except Exception:  # noqa: BLE001 - fall back to the legacy flag
                traceback.print_exc()
        table = cfg.get("latest_only") if isinstance(cfg.get("latest_only"), dict) else {}
        return table.get(platform.name) is True

    @staticmethod
    def _library_mod() -> Any:
        library = _optional_mod("library")
        if library is None:
            raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "Library rules are not available in this version")
        return library

    def _profile(self, platform: Any, cfg: dict[str, Any] | None = None) -> Any:
        """The platform's ``library.LibraryProfile`` (defaults + what is saved in config.json)."""
        cfg = self._config() if cfg is None else cfg
        return self._library_mod().load_profile(cfg, platform)

    @staticmethod
    def _profile_json(profile: Any) -> dict[str, Any]:
        data = profile.to_dict() if hasattr(profile, "to_dict") else dataclasses.asdict(profile)
        return {**data, "exclude": sorted(data.get("exclude") or ())}

    @staticmethod
    def _rule_keys() -> list[str]:
        tags = _optional_mod("tags")
        return list(getattr(tags, "RULES", None) or RULE_LABELS)

    @staticmethod
    def _rule_labels() -> dict[str, str]:
        tags = _optional_mod("tags")
        return {**RULE_LABELS, **dict(getattr(tags, "RULE_LABELS", None) or {})}

    def _available_languages(self, platform: Any) -> list[dict[str, Any]]:
        """Languages of the platform's installed language-filtered DATs (``library.available_languages``), cached."""
        library = _optional_mod("library")
        scope = tuple(getattr(platform, "language_dats", ()) or ())
        func = getattr(library, "available_languages", None)
        if func is None or not scope:
            return []
        try:
            key = (platform.name, self._dats_signature(platform))
            if key in self._lang_cache:
                return self._lang_cache[key]
            dats, _missing = self._platform_dats(platform)
            rows = list(func([d for d in dats if getattr(d, "name", "") in scope], platform))
        except Exception:  # noqa: BLE001 - the language list is informative; the panel still works without it
            traceback.print_exc()
            return []
        self._lang_cache = {key: rows}  # one platform at a time
        return rows

    @staticmethod
    def _ranking_text(style: str = SOURCE_TOSEC) -> str:
        """The variant ranking as one line (from ``tags.PLATFORM_ORDER``)."""
        library = _optional_mod("library")
        tags = getattr(library, "tags", None) or _optional_mod("tags")
        order = list(getattr(tags, "PLATFORM_ORDER", None) or ("CD32", "AGA", "OCS"))
        if style == "whdload":
            return ("Preference: your language order, then " + " over ".join(order)
                    + ", then standard memory over 512KB / Low Mem builds, then PAL / untagged over NTSC, "
                    "then the newest version (highest build number last)")
        return "Preference: cracked, then " + " over ".join(order) + ", then newest (your language order comes first)"

    def _profile_info(self, platform: Any, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        """The ``/api/library/profile`` answer for one platform (Amendment 8: catalog, languages, regions)."""
        library = self._library_mod()
        profile = self._profile(platform, cfg)
        labels = self._rule_labels()
        on = set(profile.exclude)
        info: dict[str, Any] = {
            "platform": platform.name,
            "profile": self._profile_json(profile),
            "defaults": self._profile_json(library.default_profile(platform)),
            "rules": [{"key": k, "label": labels.get(k, k), "on": k in on} for k in self._rule_keys()],
            "available": {
                "latest_only": bool(getattr(platform, "latest_dats", ())),
                "best_variant": bool(getattr(platform, "best_variant_dats", ())),
                "complete_only": bool(getattr(platform, "m3u_dats", ())),
            },
            "scopes": {
                "latest_dats": list(getattr(platform, "latest_dats", ()) or ()),
                "best_variant_dats": list(getattr(platform, "best_variant_dats", ()) or ()),
                "complete_dats": list(getattr(platform, "m3u_dats", ()) or ()),
                "exclude_dats": list(platform.dats),
            },
            "style": SOURCE_TOSEC, "catalog": [], "regions": [], "language_names": {},
        }
        build = getattr(library, "profile_info", None)
        if build is not None:
            extra = build(platform, profile)
            for key in ("style", "catalog", "regions", "language_names"):
                if key in extra:
                    info[key] = extra[key]
            info["available"] = {**info["available"], **extra.get("available", {})}
            info["scopes"] = {**info["scopes"], **extra.get("scopes", {})}
        info["available_languages"] = self._available_languages(platform) if info["available"].get("languages") else []
        info["ranking"] = self._ranking_text(info.get("style") or SOURCE_TOSEC) if info["available"].get("best_variant") else ""
        return info

    @staticmethod
    def _drop_plans(state: ScanState) -> None:
        """Forget every cached plan (the library profile changed)."""
        state.rename_plans.clear()
        state.convert_plans.clear()
        state.m3u_plans.clear()
        state.library_plans.clear()
        for key in [k for k in state.items if isinstance(k, tuple) and k[0] in ("rename", "convert", "m3u", "library", "warnings")]:
            del state.items[key]

    def _require_scan(self) -> ScanState:
        with self._lock:
            state = self._scan
        if state is None:
            raise ApiError(HTTPStatus.CONFLICT, "No scan results yet - run a scan first")
        return state

    def _resolve_platform(self, raw: Any) -> Any:
        """Platform by name; empty -> last used platform, else the first one."""
        platforms = _mod("platforms")
        name = _str_arg(raw)
        if not name:
            last = self._config().get("last_platform")
            known = [p.name for p in platforms.list_platforms()]
            if not known:
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "No platforms defined")
            default = getattr(platforms, "DEFAULT_PLATFORM", None)
            name = last if last in known else default if default in known else known[0]
        try:
            platform = platforms.get_platform(name)
        except (KeyError, ValueError, LookupError):
            platform = None
        if platform is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Unknown platform: {name}")
        return platform

    @staticmethod
    def _locate_dats(platform: Any) -> dict[str, Any]:
        """Present DATs of a platform by exact name (``platforms.locate_dats``; TOSEC fallback)."""
        locate = getattr(_mod("platforms"), "locate_dats", None)
        if locate is not None:
            return dict(locate(platform))
        if _source(platform) == SOURCE_NOINTRO:
            nointro = _optional_mod("nointro")
            infos = list(nointro.list_dats()) if nointro is not None else []
        else:
            infos = list(_mod("tosec").list_dats())
        return {i.name: i for i in infos if i.name in tuple(platform.dats)}

    def _dats_signature(self, platform: Any) -> tuple:
        """(name, path, mtime) of every installed DAT of a platform: changes when an update is installed."""
        infos = self._locate_dats(platform)
        signature: list[tuple[str, str, int]] = []
        for name in platform.dats:
            info = infos.get(name)
            if info is not None:
                try:
                    mtime = Path(info.path).stat().st_mtime_ns
                except OSError:
                    mtime = 0
                signature.append((name, str(info.path), mtime))
        return tuple(signature)

    def _platform_dats(self, platform: Any) -> tuple[list[Any], list[str]]:
        """Load a platform's DATs, cached while the DAT files are unchanged.

        Takes the updater's ``dat_lock`` so a DAT is never parsed while it is being swapped in."""
        with self.dat_lock:
            key = (platform.name, self._dats_signature(platform))
            cached = self._dat_cache
            if cached is not None and cached[0] == key:
                return cached[1]
            loaded, missing = _mod("platforms").load_platform_dats(platform)
            value = (list(loaded), list(missing))
            self._dat_cache = (key, value)
            return value

    def sweep_chd_temp(self) -> list[str]:
        """Startup housekeeping: delete leftover ``.romorg-chd-*`` temp folders (dead runs) in the Dreamcast folder."""
        chdtool = _optional_mod("chdtool")
        removed: list[str] = []
        if chdtool is None:
            return removed
        for platform in _mod("platforms").list_platforms():
            if _layout(platform) != LAYOUT_GAME_FOLDER:
                continue
            folder = self._folders().get(platform.name)
            if folder and os.path.isdir(folder):
                removed += chdtool.sweep_stale(Path(folder))
        return removed

    # ---- chdman (Sega Dreamcast): detected lazily, result reused for a minute

    def _chdman(self, refresh: bool = False) -> Any:
        """The detected ``chdtool.Chdman`` (or None)."""
        return self._chdman_state(refresh)[1]

    def _chdman_info(self, refresh: bool = False) -> dict[str, Any]:
        """JSON for the UI: ``{found, label, kind, hint, ...}`` (``chdtool.info``)."""
        return dict(self._chdman_state(refresh)[2])

    def _chdman_state(self, refresh: bool = False) -> tuple[float, Any, dict[str, Any]]:
        with self._lock:
            cached = self._chdman_cache
        if cached is not None and not refresh and time.monotonic() - cached[0] < CHDMAN_TTL:
            return cached
        chdtool = _optional_mod("chdtool")
        if chdtool is None:
            state: tuple[float, Any, dict[str, Any]] = (time.monotonic(), None, {"found": False, "label": "", "kind": "",
                                                        "hint": "chdman support is not available in this version"})
        else:
            cfg = self._config()
            try:
                found = chdtool.detect(cfg)
            except Exception:  # noqa: BLE001 - detection must never break the page
                traceback.print_exc()
                found = None
            if found is not None:
                info = found.to_dict()
                info["hint"] = ""
            else:
                info = {"found": False, "kind": "", "label": "", "hint": chdtool.INSTALL_HINT}
                info["steps"] = [
                    "Open Discover (Desktop Mode) and install \"MAME\" (org.mamedev.MAME) - it ships chdman",
                    "or install any chdman and put it on PATH",
                    f"or save the path of a chdman binary below (config key \"{chdtool.CONFIG_KEY}\", "
                    f"environment {chdtool.ENV_VAR})"]
            info["override"] = str(cfg.get(chdtool.CONFIG_KEY) or "")
            state = (time.monotonic(), found, info)
        with self._lock:
            self._chdman_cache = state
        return state

    @staticmethod
    def _is_dc(state: "ScanState") -> bool:
        return state.layout == LAYOUT_GAME_FOLDER

    def _scan_is_stale(self) -> bool:
        """True when the DATs of the scanned platform changed after that scan parsed them."""
        with self._lock:
            state = self._scan
        if state is None:
            return False
        if not state.dats_changed:
            try:
                state.dats_changed = self._dats_signature(state.platform) != state.dats_sig
            except Exception:  # noqa: BLE001 - informative only
                return False
        return state.dats_changed

    def updates_status(self) -> dict[str, Any]:
        """``UpdateManager.status()`` plus the server's own ``scan_stale`` (DATs updated since the scan)."""
        status = dict(self.updates.status())
        # precise: the DATs of the scanned platform changed after that scan parsed them (the
        # manager-level flag is not platform-aware and would stay on after a rescan)
        status["scan_stale"] = self._scan_is_stale()
        return status

    def _platform_info(self, platform: Any, infos: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        source, layout = _source(platform), _layout(platform)
        dats = []
        for name in platform.dats:
            info = infos.get(name)
            dats.append({
                "name": name,
                "source": source,
                "version": info.version if info else None,
                "present": info is not None,
                "file": Path(info.path).name if info else None,
                "folder": _safe_name(name) if layout == LAYOUT_PER_DAT else "",
                "m3u": name in tuple(platform.m3u_dats or ()),
                "kickstart": name == platform.kickstart_dat,
            })
        return {
            "name": platform.name,
            "source": source,
            "layout": layout,
            "folder_hint": getattr(platform, "folder_hint", "") or "",
            "extensions": list(getattr(platform, "extensions", ()) or ()),
            "convertible": bool(getattr(platform, "convertible", False)),
            "chd": layout == LAYOUT_GAME_FOLDER,
            "folder": self._folders(cfg).get(platform.name),
            "latest_only": self._latest_only(platform, cfg),
            "library": self._profile_for_info(platform, cfg),
            "dats": dats,
            "complete": all(d["present"] for d in dats),
            "m3u_dats": list(platform.m3u_dats or ()),
            "kickstart_dat": platform.kickstart_dat,
            "kickstart_folder": getattr(platform, "kickstart_folder", "") or "",
            "has_kickstart": bool(platform.kickstart_dat or getattr(platform, "kickstart_folder", "")),
            "kickstart_dest": _kick_dest(cfg, platform.name),
            "protected_dirs": list(getattr(platform, "protected_dirs", ()) or ()),
        }

    def _profile_for_info(self, platform: Any, cfg: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return self._profile_json(self._profile(platform, cfg))
        except Exception:  # noqa: BLE001 - e.g. the library module is missing
            return None

    @staticmethod
    def _validate_dir(raw: Any) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "No folder given")
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            raise ApiError(HTTPStatus.BAD_REQUEST, "Folder must be an absolute path")
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Folder does not exist: {raw}") from None
        if not path.is_dir():
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Not a folder: {raw}")
        return path

    @staticmethod
    def _validate_dest(raw: Any) -> Path:
        """A Kickstart destination: an existing folder, or a new one in an existing folder."""
        if not isinstance(raw, str) or not raw.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "No destination folder given")
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            raise ApiError(HTTPStatus.BAD_REQUEST, "Destination must be an absolute path")
        try:
            path = path.resolve()
        except (OSError, RuntimeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Invalid destination: {raw}") from None
        if path.exists():
            if not path.is_dir():
                raise ApiError(HTTPStatus.BAD_REQUEST, f"Not a folder: {raw}")
        elif not path.parent.is_dir():
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Parent folder does not exist: {path.parent}")
        return path

    def _run_scan(self, job: Job, root: Path, platform: Any, cancellable: bool = True) -> ScanState | None:
        # DATs are kept current automatically: a platform whose DATs are not installed yet waits for
        # (or starts) the update here, with its progress shown as this job's progress.
        try:
            self.updates.ensure(platform, progress=job.report, cancel=job.cancel)
        except Exception as exc:  # noqa: BLE001 - UpdateError (offline / failed / cancelled)
            if type(exc).__name__ != "UpdateError":
                raise
            code = getattr(exc, "code", "")
            if job.cancel.is_set():
                return None
            if code == "cancelled":  # the updater was cancelled (Check for updates), not this job
                raise ApiError(HTTPStatus.CONFLICT, f"The {platform.name} DAT update was cancelled - "
                               "press Retry to scan again.", "update_cancelled") from None
            if code == "offline":
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE,
                               f"The {platform.name} DATs are not installed yet and the update servers cannot be "
                               "reached. Connect to the internet and press Retry.", "offline_no_dats") from None
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"Updating the {platform.name} DATs failed: {exc}",
                           "update_failed") from None
        job.report(0, 0, f"Loading {platform.name} DATs...")
        dats, missing = self._platform_dats(platform)
        sig = self._dats_signature(platform)  # what this scan parsed (see _scan_is_stale)
        clear = getattr(self.updates, "clear_stale", None)
        if callable(clear):
            clear()
        if not dats:
            raise ApiError(HTTPStatus.CONFLICT,
                           f"No DATs for {platform.name} are installed (automatic DAT updates are off or unavailable)",
                           "no_dats")
        job.report(0, 0, "Scanning...")
        layout = _layout(platform)
        if layout == LAYOUT_GAME_FOLDER:     # Sega Dreamcast: CHD / raw sets, matched per track
            cfg = self._config()
            result = _mod("dreamcast").scan(
                root, dats, progress=job.report, cancel=job.cancel if cancellable else None,
                chdman=self._chdman(), engine=str(cfg.get("chd_engine") or "auto"),
                workers=_mod("chdpool").default_workers(cfg.get("chd_workers")),
                protected_dirs=tuple(getattr(platform, "protected_dirs", ()) or ()))
        else:
            result = _call(_mod("scanner").scan, root, dats, recursive=True, progress=job.report,
                           cancel=job.cancel if cancellable else None,
                           alt_hashes=tuple(getattr(platform, "alt_hashes", ()) or ()), layout=layout,
                           protected_dirs=tuple(getattr(platform, "protected_dirs", ()) or ()))
        if cancellable and job.cancel.is_set():
            return None
        dat_names = list(getattr(result, "dat_names", None) or [getattr(d, "name", "") for d in dats])
        state = ScanState(result=result, root=root, platform=platform, dat_names=dat_names,
                          missing_dats=list(missing), layout=_enum_str(getattr(result, "layout", None), layout),
                          dats_sig=sig)
        with self._lock:
            self._scan = state
        return state

    def _summary(self, state: ScanState) -> dict[str, Any]:
        if state.summary is None:
            summary = dict(state.result.summary())
            summary["missing_dats"] = list(state.missing_dats)
            state.summary = summary
        return dict(state.summary)

    def _rename_plan(self, state: ScanState, move_unmatched: bool = True, latest_only: bool = False) -> list[Any]:
        key = (move_unmatched, latest_only)
        if key not in state.rename_plans and self._is_dc(state):
            state.rename_plans[key] = sorted(_mod("dreamcast").plan_tidy(state.result, move_unmatched),
                                              key=_order_key(ORGANISE_ORDER))
        if key not in state.rename_plans:
            kwargs: dict[str, Any] = {"missing_dats": list(state.missing_dats), "move_unmatched": move_unmatched,
                                      "layout": state.layout}
            if latest_only:  # only passed when wanted: older organisers lack the option
                kwargs["latest_only"] = True
            ops = list(_call(_mod("organiser").plan_renames, state.result, **kwargs))
            ops.sort(key=_order_key(ORGANISE_ORDER))  # stable: keeps organiser order per status
            state.rename_plans[key] = ops
        return state.rename_plans[key]

    def _rename_rows(self, state: ScanState, move_unmatched: bool = True,
                     latest_only: bool = False) -> list[tuple[dict[str, Any], str]]:
        key = ("rename", move_unmatched, latest_only)
        if key not in state.items:
            state.items[key] = _rows([self._rename_item(op, state.root)
                                      for op in self._rename_plan(state, move_unmatched, latest_only)])
        return state.items[key]

    def _convert_plan(self, state: ScanState, latest_only: bool = False) -> list[Any]:
        if not getattr(state.platform, "convertible", False):
            return []
        if latest_only not in state.convert_plans and self._is_dc(state):
            ops = list(_mod("dreamcast").plan_convert(state.result, self._chdman() is not None))
            ops.sort(key=_order_key(CONVERT_ORDER))
            state.convert_plans[latest_only] = ops
        if latest_only not in state.convert_plans:
            convert = _optional_mod("convert")
            if convert is None:
                raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "Conversion is not available in this version")
            ops = list(_call(convert.plan_conversions, state.result, layout=state.layout, latest_only=latest_only))
            ops.sort(key=_order_key(CONVERT_ORDER))
            state.convert_plans[latest_only] = ops
        return state.convert_plans[latest_only]

    def _convert_rows(self, state: ScanState, latest_only: bool = False) -> list[tuple[dict[str, Any], str]]:
        key = ("convert", latest_only)
        if key not in state.items:
            state.items[key] = _rows([self._convert_item(op, state.root)
                                      for op in self._convert_plan(state, latest_only)])
        return state.items[key]

    def _latest_arg(self, state: ScanState, body: dict[str, Any]) -> bool:
        """``latest_only`` from a request body; remembered per platform, default = the remembered value."""
        if body.get("latest_only") is None:
            return self._latest_only(state.platform)
        value = _bool_arg(body.get("latest_only"))
        if value != self._latest_only(state.platform):
            self._save_latest_only(state.platform, value)
        return value

    def _save_latest_only(self, platform: Any, value: bool) -> None:
        """Remember "latest versions only" = ``profile.latest_only`` (legacy config flag without a library module)."""
        library = _optional_mod("library")
        if library is None:
            self._config_update(latest_for=(platform.name, value))
            return
        self._save_profile(platform, dataclasses.replace(self._profile(platform), latest_only=value))

    def _save_profile(self, platform: Any, profile: Any) -> None:
        library = self._library_mod()
        _mod("paths").update_config(lambda cfg: library.store_profile(cfg, platform, profile))
        with self._lock:
            state = self._scan
        if state is not None:
            self._drop_plans(state)

    def _plan_warnings(self, state: ScanState, rows: list[tuple[dict[str, Any], str]]) -> list[str]:
        """Signs that the chosen folder is not just this platform's ROM folder."""
        to_unmatched = matched = 0
        systems: set[str] = set()
        for item, _ in rows:
            if item.get("superseded_by") or item.get("code"):  # matched, set aside for a library rule
                matched += 1
                continue
            if item["dest"] in RESERVED_DIRS and item["dest"] != UNMATCHED_DIR:
                continue    # already in / moving to one of the app's other folders: neither matched nor unmatched
            if item["status"] not in ACTIONABLE or item["status"] == "delete":
                if item["status"] == "ok" and item["dest"] != UNMATCHED_DIR:
                    matched += 1
                continue
            if item["dest"] == UNMATCHED_DIR:
                to_unmatched += 1
                top = item["from"].split("/", 1)[0].lower() if "/" in item["from"] else ""
                if top in OTHER_SYSTEM_DIRS:
                    systems.add(top)
            else:
                matched += 1
        out: list[str] = []
        if to_unmatched > 50 and to_unmatched > matched:
            out.append(f"{to_unmatched:,} files match no {state.platform.name} DAT - more than the files that "
                       f"do ({matched:,}). Is {state.root} really the {state.platform.name} folder?")
        if len(systems) >= 2:
            out.append("Folders of other systems would be moved into _unmatched/: "
                       + ", ".join(sorted(systems)) + ". Choose the platform's own folder instead.")
        return out

    def _m3u_plan(self, state: ScanState, savedisk: bool, labels: bool) -> list[Any]:
        key = (savedisk, labels)
        if key not in state.m3u_plans:
            m3u_dats = list(state.platform.m3u_dats or ())
            exclude: Any = None
            if _optional_mod("library") is not None:
                exclude = frozenset(self._profile(state.platform).exclude)
            state.m3u_plans[key] = list(_call(_mod("m3u").plan_m3us, state.result, savedisk=savedisk,
                                              labels=labels, m3u_dats=m3u_dats, dats=m3u_dats,
                                              exclude_rules=exclude))
            state.items.pop(("m3u", key), None)
        return state.m3u_plans[key]

    def _m3u_rows(self, state: ScanState, savedisk: bool, labels: bool) -> list[tuple[dict[str, Any], str]]:
        ops = self._m3u_plan(state, savedisk, labels)
        key = ("m3u", (savedisk, labels))
        if key not in state.items:
            state.items[key] = _rows([self._m3u_item(op, state.root) for op in ops])
        return state.items[key]

    def _kick_plan(self, state: ScanState, dest: Path) -> list[Any]:
        ops = list(_call(_mod("kickstart").plan_kickstarts, state.result, dest,
                         kickstart_dat=state.platform.kickstart_dat))
        ops.sort(key=_order_key(KICKSTART_ORDER))
        return ops

    def _kick_scope(self, raw_platform: Any) -> tuple[Any, ScanState | None]:
        """The platform a Kickstart request is about (+ the scan it needs, for a DAT-based one).

        ``platform`` given -> that one; otherwise the scanned platform, else the default. A platform with a
        Kickstart DAT (TOSEC Amiga) needs a scan OF THAT platform; one with its own Kickstart folder
        (WHDLoad) only needs its platform folder."""
        name = _str_arg(raw_platform)
        with self._lock:
            state = self._scan
        if name:
            platform = self._resolve_platform(name)
        elif state is not None:
            platform = state.platform
        else:
            platform = self._resolve_platform("")
        if not _has_kickstart(platform):
            raise ApiError(HTTPStatus.CONFLICT, "This platform has no Kickstart DAT or Kickstart folder")
        if getattr(platform, "kickstart_folder", ""):
            return platform, (state if state is not None and state.platform.name == platform.name else None)
        if state is None:
            raise ApiError(HTTPStatus.CONFLICT, "No scan results yet - run a scan first")
        if state.platform.name != platform.name:
            raise ApiError(HTTPStatus.CONFLICT, f"Scan {platform.name} first (the last scan was of {state.platform.name})")
        return platform, state

    def _kick_folder(self, platform: Any, state: ScanState | None) -> tuple[Path, Path | None]:
        """``(platform root, the Kickstart sub-folder or None when it does not exist)`` of a folder-source platform."""
        if state is not None:
            root = state.root
        else:
            saved = self._folders().get(platform.name)
            if not saved:
                raise ApiError(HTTPStatus.CONFLICT, f"Choose the {platform.name} folder first (step 1) - "
                               f"its Kickstart ROMs are read from the {platform.kickstart_folder}/ folder inside it")
            root = self._validate_dir(saved)
        want = platform.kickstart_folder.casefold()
        try:
            for child in sorted(root.iterdir(), key=lambda p: p.name):
                if child.name.casefold() == want and child.is_dir():
                    return root, child
        except OSError:
            pass
        return root, None

    def _kick_ops(self, platform: Any, state: ScanState | None, dest: Path) -> tuple[list[Any], dict[str, Any]]:
        """``(ops, extra response fields)`` for the platform's Kickstart plan."""
        kick = _mod("kickstart")
        if getattr(platform, "kickstart_folder", ""):
            root, folder = self._kick_folder(platform, state)
            ops = list(kick.plan_kickstarts_from_folder(folder, dest))
            ops.sort(key=_order_key(KICKSTART_ORDER))
            return ops, {"source": "folder", "root": str(root),
                         "source_dir": str(folder if folder is not None else root / platform.kickstart_folder),
                         "source_exists": folder is not None}
        assert state is not None
        return self._kick_plan(state, dest), {"source": "dat", "root": str(state.root),
                                              "kickstart_dat": platform.kickstart_dat}

    def _library_plan(self, state: ScanState, move_unmatched: bool = True, savedisk: bool = False,
                      labels: bool = True) -> Any:
        """The cached ``organiser.LibraryPlan`` for the platform's current profile."""
        key = (move_unmatched, savedisk, labels)
        if key not in state.library_plans and self._is_dc(state):
            plan = _mod("dreamcast").plan_library(state.result, self._profile(state.platform),
                                                  move_unmatched=move_unmatched, savedisk=savedisk, labels=labels)
            plan.ops.sort(key=_order_key(ORGANISE_ORDER))
            state.library_plans[key] = plan
        if key not in state.library_plans:
            organiser = _mod("organiser")
            planner = getattr(organiser, "plan_library", None)
            if planner is None:
                raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "Build library is not available in this version")
            plan = _call(planner, state.result, self._profile(state.platform),
                         missing_dats=list(state.missing_dats), move_unmatched=move_unmatched,
                         layout=state.layout, savedisk=savedisk, labels=labels, platform=state.platform)
            try:
                plan.ops.sort(key=_order_key(ORGANISE_ORDER))  # stable: keeps organiser order per status
            except AttributeError:
                pass
            state.library_plans[key] = plan
        return state.library_plans[key]

    @staticmethod
    def _library_category(item: dict[str, Any]) -> str:
        """Reason category of a Build library row (the ``reason`` filter)."""
        if item.get("item") == "playlist" or item.get("status") == "delete":
            return "playlist"
        if item.get("code"):
            return str(item["code"])
        return "unmatched" if item.get("dest") in RESERVED_DIRS else "kept"

    def _library_rows(self, state: ScanState, key: tuple[bool, bool, bool]) -> list[tuple[dict[str, Any], str]]:
        ikey = ("library", key)
        if ikey not in state.items:
            plan = self._library_plan(state, *key)
            items = [self._rename_item(op, state.root) for op in plan.ops]
            for item in items:
                item["category"] = self._library_category(item)
            for op in plan.playlists:
                item = self._m3u_item(op, state.root)
                item.update(item="playlist", category="playlist")
                items.append(item)
            state.items[ikey] = _rows(items)
        return state.items[ikey]

    @staticmethod
    def _reason_counts(plan: Any, rows: list[tuple[dict[str, Any], str]]) -> dict[str, int]:
        counter = getattr(_mod("organiser"), "reason_counts", None)
        if counter is not None:
            return dict(counter(plan))
        out = dict.fromkeys(("kept", "renamed", "moved", "excluded", "superseded", "incomplete", "duplicates",
                             "unmatched", "conflict", "skip", "playlists_write", "playlists_ok",
                             "playlists_remove", "playlists_conflict"), 0)
        plural = {"duplicate": "duplicates"}
        for item, _ in rows:
            cat, status = item["category"], item["status"]
            if item.get("item") == "playlist":
                out[f"playlists_{status}"] = out.get(f"playlists_{status}", 0) + 1
            elif status == "delete":
                out["playlists_remove"] += 1
            elif status in ("conflict", "skip"):
                out[status] += 1
            elif cat == "kept":
                out["kept"] += 1
                if status == "rename":
                    out["renamed"] += 1
                elif status == "move":
                    out["moved"] += 1
            else:
                out[plural.get(cat, cat)] = out.get(plural.get(cat, cat), 0) + 1
        return out

    def _matched_item(self, match: Any, state: ScanState, tags_mod: Any = None) -> dict[str, Any]:
        if hasattr(match, "item"):          # Sega Dreamcast: level / tracks / canonical place
            return match.item(state)
        entry, root = match.entry, state.root
        primary = _primary(match, state.dat_names)
        dat = _rom_dat(primary[0]) if primary else ""
        names = [r.name for r in primary]
        path = os.fspath(entry.path)
        own = entry.member if entry.member else os.path.basename(path)
        home = os.fspath(root) if state.layout == LAYOUT_FLAT else os.path.join(os.fspath(root), _safe_name(dat))
        placed = bool(dat) and os.path.dirname(path) == home
        via = getattr(match, "matched_via", "raw") or "raw"
        stems = {getattr(r, "set_name", "") or r.game for r in primary}
        named = own in names or (bool(entry.member) and os.path.splitext(os.path.basename(path))[0] in stems)
        if not named and via != "raw" and not entry.member:
            target = getattr(_mod("organiser"), "target_filename", None)
            try:
                named = target is not None and own in {
                    target(r, own, None, via, getattr(match, "byte_order", "") or "") for r in primary}
            except Exception:  # noqa: BLE001 - informative only
                named = False
        return {
            "file": _entry_rel(entry, root), "size": entry.size, "crc": entry.crc,
            "dat": dat, "roms": names, "game": primary[0].game if primary else "",
            "set_name": getattr(primary[0], "set_name", "") if primary else "",
            "other_dats": sorted({_rom_dat(r) for r in match.roms} - {dat, ""}),
            "named_ok": named,
            "placed_ok": placed,
            "via": via,
            "header": getattr(match, "header", 0) or 0,
            "byte_order": getattr(match, "byte_order", "") or "",
            "tags": _tags_json(primary[0], tags_mod) if primary else None,
        }

    @staticmethod
    def _missing_item(rom: Any, tags_mod: Any = None) -> dict[str, Any]:
        return {"name": rom.name, "game": rom.game, "size": rom.size, "crc": rom.crc, "dat": _rom_dat(rom),
                "set_name": getattr(rom, "set_name", "") or "", "tags": _tags_json(rom, tags_mod)}

    def _game_items(self, state: ScanState, tags_mod: Any = None) -> list[dict[str, Any]]:
        """One row per logical game ("set") of every loaded DAT: have / missing, roms and local files."""
        rows: list[dict[str, Any]] = []
        game_status = getattr(_mod("scanner"), "game_status", None)
        if game_status is not None:
            levels = getattr(state.result, "game_levels", lambda: {})()
            for g in game_status(state.result):
                rom = g.get("rom")
                row = {"name": g["name"], "dat": g.get("dat", ""), "have": bool(g.get("have")),
                       "roms": list(g.get("roms") or ()),
                       "files": [_entry_rel(f, state.root) if hasattr(f, "path") else _rel(f, state.root)
                                 for f in g.get("files") or ()],
                       "tags": _tags_json(rom, tags_mod)}
                if g["name"] in levels:
                    row["level"], row["kind"] = levels[g["name"]]
                rows.append(row)
            return rows
        # Fallback (older scanner): every set is either matched or listed in ``missing``.
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for match in state.result.matched:
            for rom in _primary(match, state.dat_names):
                key = (_rom_dat(rom), getattr(rom, "set_name", "") or rom.name)
                row = groups.setdefault(key, {"name": key[1], "dat": key[0], "have": True, "roms": [],
                                              "files": [], "tags": _tags_json(rom, tags_mod)})
                if rom.name not in row["roms"]:
                    row["roms"].append(rom.name)
                rel = _entry_rel(match.entry, state.root)
                if rel not in row["files"]:
                    row["files"].append(rel)
        for rom in state.result.missing:
            key = (_rom_dat(rom), getattr(rom, "set_name", "") or rom.name)
            groups.setdefault(key, {"name": key[1], "dat": key[0], "have": False, "roms": [rom.name],
                                    "files": [], "tags": _tags_json(rom, tags_mod)})
        order = {name: i for i, name in enumerate(state.dat_names)}
        rows = sorted(groups.values(), key=lambda r: (order.get(r["dat"], len(order)), r["name"].casefold()))
        return rows

    @staticmethod
    def _dest_of(to_rel: str) -> str:
        """Top-level folder of a target path relative to root ('' if at root)."""
        return to_rel.split("/", 1)[0] if "/" in to_rel else ""

    def _rename_item(self, op: Any, root: Path) -> dict[str, Any]:
        to_rel = _rel(op.dst, root)
        item = {
            "from": _rel(op.src, root),
            "to": to_rel,
            "from_name": Path(op.src).name,
            "to_name": Path(op.dst).name,
            "dest": self._dest_of(to_rel),
            "status": op.status,
            "kind": getattr(op, "kind", "") or "",
            "reason": getattr(op, "reason", "") or "",
            "rom_name": getattr(op, "rom_name", "") or "",
            "superseded_by": getattr(op, "superseded_by", "") or "",
            "code": getattr(op, "code", "") or "",
            "keeper": getattr(op, "keeper", "") or "",
            "missing": list(getattr(op, "missing", ()) or ()),
            "flags_text": getattr(op, "flags_text", "") or "",
            "reasons": list(getattr(op, "reasons", ()) or ()),
            "item": "file",
        }
        if hasattr(op, "moves"):           # Sega Dreamcast: a game folder / CHD with its sidecars
            item.update(dest=getattr(op, "folder", "") or "", game=op.game, level=op.level, unit_kind=op.unit_kind,
                        n_files=op.n_files, files=len(op.moves) or op.n_files)
        return item

    @staticmethod
    def _convert_item(op: Any, root: Path) -> dict[str, Any]:
        src = _rel(op.src, root)
        return {
            "from": f"{src}::{op.member}" if getattr(op, "member", None) else src,
            "to": _rel(op.dst, root),
            "original_to": _rel(op.original_dst, root),
            "status": op.status,
            "reason": getattr(op, "reason", "") or "",
            "rom_name": getattr(op, "rom_name", "") or "",
            "via": getattr(op, "via", "") or "",
        }

    @staticmethod
    def _m3u_item(op: Any, root: Path) -> dict[str, Any]:
        path = Path(op.path)
        lines = list(op.lines or [])
        return {
            "name": path.name,
            "path": _rel(path, root),
            "dir": _rel(path.parent, root) if path.parent != root else ".",
            "lines": lines,
            "disks": sum(1 for line in lines if line.strip() and not line.startswith("#")),
            "status": op.status,
            "reason": getattr(op, "reason", "") or "",
        }

    @staticmethod
    def _kick_item(op: Any, root: Path) -> dict[str, Any]:
        source = getattr(op, "source", None)
        return {
            "file": Path(op.target).name,
            "target": str(op.target),
            "source": _entry_rel(source, root) if source is not None else None,
            "status": op.status,
            "reason": getattr(op, "reason", "") or "",
            "description": getattr(op, "description", "") or "",
            "rom_name": getattr(op, "rom_name", "") or "",
        }

    # ------------------------------------------------------------- endpoints

    def status(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": getattr(importlib.import_module(PACKAGE), "__version__", "0"),
            "release": None, "dats_count": 0, "default_platform": None,
            "last_platform": None, "last_dir": None, "folders": {}, "kickstart_dest": None,
            "kickstart_dests": {},
            "has_7z": _which_7z() is not None, "dialog_available": _dialog_command() is not None,
            "data_dir": None, "scan": None,
            "nointro": {"dir": None, "count": 0, "dats": []}, "redump": {"dir": None, "count": 0, "dats": []},
            "updates": None,
        }
        try:
            paths = _mod("paths")
            out["data_dir"] = str(paths.data_dir())
            release_file = Path(paths.dats_dir()) / "release.json"
            if release_file.is_file():
                out["release"] = json.loads(release_file.read_text(encoding="utf-8")).get("release")
        except Exception:
            traceback.print_exc()
        cfg = self._config()
        out["folders"] = self._folders(cfg)
        out["last_platform"] = cfg.get("last_platform")
        out["last_dir"] = out["folders"].get(out["last_platform"]) or cfg.get("last_dir")
        out["kickstart_dest"] = _kick_dest(cfg, LEGACY_KICKSTART_PLATFORM)     # the TOSEC Amiga's (legacy key)
        dests = cfg.get("kickstart_dests") if isinstance(cfg.get("kickstart_dests"), dict) else {}
        out["kickstart_dests"] = {k: v for k, v in dests.items() if isinstance(v, str)}
        if out["kickstart_dest"]:
            out["kickstart_dests"].setdefault(LEGACY_KICKSTART_PLATFORM, out["kickstart_dest"])
        try:
            out["dats_count"] = len(_mod("tosec").list_dats())
        except Exception:
            traceback.print_exc()
        out["nointro"] = self._nointro_status()
        out["redump"] = self._redump_status()
        try:
            out["updates"] = self.updates_status()
        except Exception:  # noqa: BLE001 - never break the page over the update line
            traceback.print_exc()
            out["updates"] = _NoUpdates().status()
        try:
            platforms = _mod("platforms")
            names = [p.name for p in platforms.list_platforms()]
            default = getattr(platforms, "DEFAULT_PLATFORM", None)
            out["default_platform"] = default if default in names else (names[0] if names else None)
        except Exception:
            traceback.print_exc()
        with self._lock:
            state = self._scan
        if state is not None:
            out["scan"] = {
                "root": str(state.root), "platform": state.platform.name, "layout": state.layout,
                "dat_names": list(state.dat_names), "missing_dats": list(state.missing_dats),
                "recursive": state.recursive, "summary": self._summary(state),
            }
        return out

    @staticmethod
    def _nointro_status() -> dict[str, Any]:
        """``{dir, count, dats: [{name, version|None, present}]}`` of the No-Intro DAT folder."""
        out: dict[str, Any] = {"dir": None, "count": 0, "dats": []}
        nointro = _optional_mod("nointro")
        if nointro is None:
            return out
        try:
            nointro_dir = getattr(_mod("paths"), "nointro_dir", None)
            if nointro_dir is not None:
                out["dir"] = str(nointro_dir())
            local = {d.name: d for d in nointro.list_dats()}
            names = list(getattr(nointro, "NOINTRO_DATS", ()) or ())
            names += [n for n in sorted(local) if n not in names]
            out["dats"] = [{"name": n, "version": local[n].version if n in local else None, "present": n in local}
                           for n in names]
            out["count"] = sum(1 for d in out["dats"] if d["present"])
        except Exception:
            traceback.print_exc()
        return out

    @staticmethod
    def _redump_status() -> dict[str, Any]:
        """``{dir, count, dats: [{name, version|None, present}]}`` of the Redump DAT folder."""
        out: dict[str, Any] = {"dir": None, "count": 0, "dats": []}
        redump = _optional_mod("redump")
        if redump is None:
            return out
        try:
            out["dir"] = str(_mod("paths").redump_dir())
            local = {d.name: d for d in redump.list_dats()}
            out["dats"] = [{"name": n, "version": local[n].version if n in local else None, "present": n in local}
                           for n in redump.REDUMP_DATS]
            out["count"] = sum(1 for d in out["dats"] if d["present"])
        except Exception:
            traceback.print_exc()
        return out

    def platforms_list(self, query: dict[str, str], body: Any) -> list[dict[str, Any]]:
        cfg = self._config()
        out = []
        for platform in _mod("platforms").list_platforms():
            try:
                infos = self._locate_dats(platform)
            except Exception:  # e.g. an unreadable DAT folder: show the DATs as missing
                traceback.print_exc()
                infos = {}
            out.append(self._platform_info(platform, infos, cfg))
        return out

    def folders_save(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Remember (or, with an empty path, forget) the folder of one platform."""
        platform = self._resolve_platform(body.get("platform"))
        raw = body.get("path")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            cfg = self._config_update(folder_for=(platform.name, None), strict=True)
        else:
            path = self._validate_dir(raw)
            _check_scan_root(path)
            cfg = self._config_update(folder_for=(platform.name, str(path)), strict=True)
        return {"folders": self._folders(cfg)}

    def platform_options(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Remember per-platform options right away (``latest_only``), not only on the next
        organise / convert request - so a scan or DAT download in between keeps them."""
        platform = self._resolve_platform(body.get("platform"))
        if "latest_only" not in body:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Nothing to save (expected latest_only)")
        self._save_latest_only(platform, _bool_arg(body.get("latest_only")))
        return {"platform": platform.name, "latest_only": self._latest_only(platform)}

    def dats_update(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Alias of ``/api/updates/check`` (kept for older clients; the job form is gone)."""
        return self.updates_check(query, body)

    def updates_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        return self.updates_status()

    def updates_check(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """The "Check for updates" button: check now and download whatever is newer, in the background."""
        started = bool(self.updates.check(force=_bool_arg((body or {}).get("force"))))
        return {"started": started, "updates": self.updates_status()}

    def updates_cancel(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        return {"cancelled": bool(self.updates.cancel())}

    def library_profile(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        return self._profile_info(self._resolve_platform(query.get("platform")))

    def library_profile_save(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        platform = self._resolve_platform(body.get("platform"))
        library = self._library_mod()
        if not any(k in body for k in PROFILE_KEYS + ("reset",)):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Nothing to save")
        profile = library.default_profile(platform) if _bool_arg(body.get("reset")) else self._profile(platform)
        tags = getattr(library, "tags", None) or _optional_mod("tags")  # the module the profile itself validates with
        changes: dict[str, Any] = {}

        def names(key: str, known: Any, what: str) -> list[str]:
            raw = body[key]
            if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be a list of {what}")
            if known is not None:
                unknown = sorted(set(raw) - set(known))
                if unknown:
                    raise ApiError(HTTPStatus.BAD_REQUEST, f"Unknown {what.rstrip('s')}: {', '.join(unknown)}")
            return list(dict.fromkeys(raw))  # de-duplicated, order kept

        if "exclude" in body:
            changes["exclude"] = frozenset(names("exclude", self._rule_keys(), "rule names"))
        if "languages" in body:
            changes["languages"] = tuple(names("languages", getattr(tags, "LANGUAGES", None), "language codes"))
        if "keep_flags" in body:
            changes["keep_flags"] = frozenset(names("keep_flags", getattr(tags, "KEEP_FLAGS", None), "flag types"))
        if "region_priority" in body:
            changes["region_priority"] = tuple(names("region_priority", getattr(tags, "REGIONS", None), "regions"))
        for key in ("latest_only", "best_variant", "complete_only", "rescue_only_dump", "one_per_game"):
            if key in body:
                changes[key] = _bool_arg(body[key])
        self._save_profile(platform, dataclasses.replace(profile, **changes))
        return self._profile_info(platform)

    def dats_list(self, query: dict[str, str], body: Any) -> list[dict[str, Any]]:
        q = query.get("q", "").strip().lower()
        out = []
        for info in _mod("tosec").list_dats():
            if q and q not in info.name.lower():
                continue
            out.append({"name": info.name, "version": info.version, "file": Path(info.path).name})
        return out

    def fs_list(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        raw = query.get("path", "").strip()
        path = self._validate_dir(raw) if raw else Path.home().resolve()
        hidden = _bool_arg(query.get("hidden"))
        try:
            dirs = []
            for child in path.iterdir():
                if not hidden and child.name.startswith("."):
                    continue
                try:
                    if child.is_dir():
                        dirs.append({"name": child.name, "path": str(child)})
                except OSError:
                    continue
        except PermissionError:
            raise ApiError(HTTPStatus.FORBIDDEN, f"Permission denied: {path}") from None
        except OSError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Cannot list {path}: {exc}") from None
        dirs.sort(key=lambda d: d["name"].lower())
        parent = str(path.parent) if path.parent != path else None
        return {"path": str(path), "parent": parent, "dirs": dirs, "places": _places()}

    def fs_pick(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        tool = _dialog_command()
        if tool is None:
            raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "No native folder dialog available - use the folder browser")
        start = _str_arg(body.get("start"))
        title = _str_arg(body.get("title"))[:MAX_TITLE] or "Choose a folder"
        start_dir = Path(start).expanduser() if start else Path.home()
        if not start_dir.is_dir():
            start_dir = start_dir.parent if start_dir.parent.is_dir() else Path.home()
        if tool == "kdialog":
            cmd = ["kdialog", "--title", title, "--getexistingdirectory", str(start_dir)]
        else:
            cmd = ["zenity", "--file-selection", "--directory", f"--title={title}",
                   f"--filename={start_dir}{os.sep}"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=DIALOG_TIMEOUT)
        except subprocess.TimeoutExpired:  # dialog hidden / never answered: give up
            return {"cancelled": True, "timeout": True}
        except OSError as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not open dialog: {exc}") from None
        chosen = proc.stdout.strip()
        if proc.returncode != 0 or not chosen:
            return {"cancelled": True}
        return {"path": chosen}

    def scan_start(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        root = self._validate_dir(body.get("path"))
        _check_scan_root(root)
        platform = self._resolve_platform(body.get("platform"))

        def work(job: Job) -> Any:
            state = self._run_scan(job, root, platform)
            if state is None:
                return None
            self._config_update(folder_for=(platform.name, str(root)), last_platform=platform.name,
                                last_dir=str(root))
            return self._summary(state)

        return {"job": self.jobs.start("scan", work).to_dict()}

    def scan_results(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        state = self._require_scan()
        kind = query.get("kind", "matched")
        dat = query.get("dat", "").strip()
        result, root = state.result, state.root
        facets = None
        if kind == "rename":
            latest = _bool_arg(query.get("latest_only"), self._latest_only(state.platform))
            rows = self._rename_rows(state, _bool_arg(query.get("move_unmatched"), True), latest)
            if dat:
                folder = canonical if (canonical := folders.canonical_name(dat)) else _safe_name(dat)
                rows = [r for r in rows if r[0]["dest"] == folder]
        elif kind == "m3u":
            rows = self._m3u_rows(state, False, True)
        elif kind in ("matched", "unmatched", "missing", "unsupported", "errors", "games"):
            if kind not in state.items:
                tags_mod = _optional_mod("tags") if kind in TAG_KINDS else None
                if kind == "matched":
                    items = [self._matched_item(m, state, tags_mod) for m in result.matched]
                elif kind == "unmatched":
                    items = [{"file": _entry_rel(e, root), "size": e.size, "crc": e.crc,
                              "reason": getattr(e, "reason", ""), "kind": getattr(e, "kind", "")}
                             for e in result.unmatched]
                elif kind == "missing":
                    items = [self._missing_item(r, tags_mod) for r in result.missing]
                elif kind == "games":
                    items = self._game_items(state, tags_mod)
                elif kind == "unsupported":
                    items = [{"file": _rel(p, root)} for p in result.unsupported]
                else:
                    items = [{"file": _rel(p, root), "error": str(msg)} for p, msg in result.errors]
                state.items[kind] = _rows(items)
            rows = state.items[kind]
            if dat and kind in TAG_KINDS:
                rows = [r for r in rows if r[0]["dat"] == dat]
            if kind == "games":
                have = query.get("have", "").strip()
                if have:
                    wanted = _bool_arg(have)
                    rows = [r for r in rows if r[0]["have"] is wanted]
            if kind in TAG_KINDS:
                key = ("facets", kind, dat, query.get("have", "").strip() if kind == "games" else "")
                if key not in state.items:
                    state.items[key] = _facets(rows)  # type: ignore[assignment]
                facets = state.items[key]
                rows = _tag_filter(rows, query)
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Unknown result kind: {kind}")
        page = _page(rows, query.get("offset"), query.get("limit"), query.get("q"))
        page["kind"] = kind
        if facets is not None:
            page["facets"] = facets
        return page

    def organise_plan(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        move_unmatched = _bool_arg(body.get("move_unmatched"), True)
        latest_only = self._latest_arg(state, body)
        ops = self._rename_plan(state, move_unmatched, latest_only)
        rows = self._rename_rows(state, move_unmatched, latest_only)
        status = _str_arg(body.get("status"))
        dest = _str_arg(body.get("dest"))  # "." = the root folder itself
        by_dest: dict[str, int] = {}
        to_superseded = 0
        for item, _ in rows:
            if item["status"] in ACTIONABLE:
                by_dest[item["dest"]] = by_dest.get(item["dest"], 0) + 1
                if item["superseded_by"]:
                    to_superseded += 1
        if status or dest:
            folder = "" if dest == "." else dest
            rows = [r for r in rows
                    if (not status or r[0]["status"] == status) and (not dest or r[0]["dest"] == folder)]
        key = ("warnings", move_unmatched, latest_only)
        if key not in state.items:
            state.items[key] = [(dict(text=w), "") for w in self._plan_warnings(
                state, self._rename_rows(state, move_unmatched, latest_only))]
        page = _page(rows, body.get("offset"), body.get("limit"), body.get("q"))
        page.update(counts=_status_counts(ops), all=len(ops), root=str(state.root),
                    actionable=sum(by_dest.values()), by_dest=by_dest,
                    to_unmatched=by_dest.get(UNMATCHED_DIR, 0), unmatched_dir=UNMATCHED_DIR, reserved_dirs=list(RESERVED_DIRS),
                    move_unmatched=move_unmatched, missing_dats=list(state.missing_dats),
                    warnings=[w["text"] for w, _ in state.items[key]],
                    latest_only=latest_only, to_superseded=to_superseded, superseded_dir=SUPERSEDED_DIR,
                    layout=state.layout)
        return page

    def _rescan_after(self, job: Job, state: ScanState) -> dict[str, Any]:
        job.report(0, 0, "Re-scanning folder...")
        new_state = self._run_scan(job, state.root, state.platform, cancellable=False)
        return self._summary(new_state) if new_state else {}

    def _rescan_into(self, job: Job, state: ScanState, res: dict[str, Any]) -> dict[str, Any]:
        """Attach the re-scan summary; a failing re-scan never hides what was done."""
        if self.closing.is_set() or (job.cancel.is_set() and not job.cancellable):
            # app is shutting down: skip the re-scan (a user-cancelled convert still re-scans)
            res["rescan_error"] = "skipped (the app is closing)"
            return res
        try:
            res["summary"] = self._rescan_after(job, state)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            traceback.print_exc()
            with self._lock:
                if self._scan is state:
                    self._scan = None  # the old plan is stale: force a new scan
            res["rescan_error"] = (exc.message if isinstance(exc, ApiError) else str(exc)) or type(exc).__name__
        return res

    def organise_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        move_unmatched = _bool_arg(body.get("move_unmatched"), True)
        latest_only = self._latest_arg(state, body)

        def work(job: Job) -> Any:
            ops = self._rename_plan(state, move_unmatched, latest_only)
            todo = sum(1 for op in ops if op.status in ACTIONABLE)
            job.report(0, todo, "Moving files...")
            apply = _mod("dreamcast").apply_plan if self._is_dc(state) else _mod("organiser").apply_renames
            res = dict(_call(apply, ops, state.root, progress=job.report, cancel=job.cancel))
            res["action"] = "apply"
            return self._rescan_into(job, state, res)

        return {"job": self.jobs.start("organise", work, cancellable=False).to_dict()}

    def _root_for(self, raw: Any) -> Path:
        if isinstance(raw, str) and raw.strip():
            return self._validate_dir(raw)
        return self._require_scan().root

    def undo_logs(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        root = self._root_for(query.get("path"))
        logs = []
        for log in _mod("organiser").list_undo_logs(root):
            log = Path(log)
            try:
                mtime = log.stat().st_mtime
                reader = getattr(_mod("organiser"), "read_undo_log", None)
                if reader is not None:
                    info = reader(log)
                    count = len(info["moves"]) + len(info.get("created_files") or ())
                else:
                    count = _undo_log_count(json.loads(log.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, KeyError):
                mtime, count = 0.0, None
            logs.append({"log": str(log), "name": log.name, "mtime": mtime, "count": count})
        logs.sort(key=lambda item: item["mtime"], reverse=True)
        return {"root": str(root), "logs": logs}

    def organise_undo(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        return self._undo(body, "organise")

    def library_undo(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """One undo for a whole Build library run (moves, created playlists, deleted playlists)."""
        return self._undo(body, "library")

    def _undo(self, body: dict[str, Any], kind: str) -> dict[str, Any]:
        state = self._require_scan()
        raw = body.get("log")
        if not isinstance(raw, str) or not raw:
            raise ApiError(HTTPStatus.BAD_REQUEST, "No undo log given")
        # Only accept logs that the organiser itself lists for the scanned folder.
        known = {str(Path(p).resolve()): Path(p) for p in _mod("organiser").list_undo_logs(state.root)}
        try:
            log = known.get(str(Path(raw).resolve()))
        except (OSError, RuntimeError):
            log = None
        if log is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Unknown undo log")

        def work(job: Job) -> Any:
            job.report(0, 0, "Undoing moves...")
            res = dict(_call(_mod("organiser").undo, log, root=state.root))
            res["action"] = "undo"
            return self._rescan_into(job, state, res)

        return {"job": self.jobs.start(kind, work, cancellable=False).to_dict()}

    @staticmethod
    def _library_flags(body: dict[str, Any]) -> tuple[bool, bool, bool]:
        return (_bool_arg(body.get("move_unmatched"), True), _bool_arg(body.get("savedisk")),
                _bool_arg(body.get("labels"), True))

    @staticmethod
    def _exclusion_codes() -> list[str]:
        codes = getattr(_optional_mod("library"), "ALL_CODES", None)
        return list(codes) if codes else []

    @staticmethod
    def _exclusion_counts(plan: Any) -> dict[str, dict[str, int]]:
        """``{"exclusive": {code: files}, "any": {code: files}}`` over every excluded decision."""
        func = getattr(plan, "exclusion_counts", None)
        try:
            out = func() if callable(func) else {}
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            out = {}
        return {"exclusive": dict(out.get("exclusive", {})), "any": dict(out.get("any", {}))}

    @staticmethod
    def _vanish_summary(plan: Any) -> dict[str, Any]:
        func = getattr(plan, "vanish_summary", None)
        out = func() if callable(func) else {}
        return {"titles": int(out.get("titles", 0)), "by_reason": dict(out.get("by_reason", {})),
                "by_code": dict(out.get("by_code", {}))}

    def library_vanished(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Paged, searchable list of the titles that have no kept version (``library.vanish_report``)."""
        state = self._require_scan()
        plan = self._library_plan(state, *self._library_flags(body))
        report = getattr(self._library_mod(), "vanish_report", None)
        if report is None or not hasattr(plan, "selection"):
            raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "The vanish report is not available in this version")
        reason = _str_arg(body.get("reason"))
        data = report(plan.selection, None, "")
        rows = [(item, f"{item['title']} {item['name']} {' '.join(item['languages'])}".lower())
                for item in data["items"] if not reason or item["reason"] == reason]
        page = _page(rows, body.get("offset"), body.get("limit"), body.get("q"))
        by_language: dict[str, int] = {}
        for item in data["items"]:
            if item["reason"] == "language":
                for code in item["languages"]:
                    by_language[code] = by_language.get(code, 0) + 1
        page.update(titles=data["titles"], by_reason=data["by_reason"], by_code=data["by_code"], by_language=by_language)
        return page

    def library_plan(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """ONE preview of Build library: file moves / renames by reason plus the playlists to write."""
        state = self._require_scan()
        key = self._library_flags(body)
        plan = self._library_plan(state, *key)
        rows = self._library_rows(state, key)
        file_rows = [r for r in rows if r[0].get("item") == "file"]
        reasons = self._reason_counts(plan, rows)
        status, reason, dest = _str_arg(body.get("status")), _str_arg(body.get("reason")), _str_arg(body.get("dest"))
        if reason and reason not in LIBRARY_REASONS:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Unknown reason filter: {reason}")
        why = _str_arg(body.get("why"))  # primary exclusion code (excluded_<code> counts)
        if why and why not in self._exclusion_codes():
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Unknown exclusion code: {why}")
        if status or reason or dest or why:
            folder = "" if dest == "." else dest
            rows = [r for r in rows
                    if (not status or r[0]["status"] == status) and (not reason or r[0]["category"] == reason)
                    and (not dest or r[0].get("dest") == folder)
                    and (not why or (r[0]["category"] == "excluded" and r[0].get("reasons", [""])[:1] == [why]))]
        wkey = ("warnings", "library", key)
        if wkey not in state.items:
            state.items[wkey] = [(dict(text=w), "") for w in self._plan_warnings(state, file_rows)]
        ops = list(plan.ops)
        playlists = list(plan.playlists)
        statuses = [p.status for p in playlists]
        by_dest: dict[str, int] = {}
        for item, _ in file_rows:
            if item["status"] in ACTIONABLE:
                by_dest[item["dest"]] = by_dest.get(item["dest"], 0) + 1
        writes = statuses.count("write")
        actionable = sum(1 for op in ops if op.status in ACTIONABLE) + writes
        incomplete = []
        for entry in list(getattr(plan.selection, "incomplete", ()) or ())[:200]:
            present = getattr(entry, "present", ()) or ()
            incomplete.append({"name": entry.name, "dat": entry.dat, "total": entry.total,
                               "missing": sorted(entry.missing), "present": sorted(present)})
        page = _page(rows, body.get("offset"), body.get("limit"), body.get("q"))
        page.update(
            root=str(state.root), layout=state.layout, profile=self._profile_json(plan.profile),
            counts=_status_counts(ops), reasons=reasons, by_dest=by_dest,
            playlists={"write": writes, "ok": statuses.count("ok"),
                       "remove": sum(1 for op in ops if op.status == "delete"),
                       "conflict": statuses.count("conflict")},
            incomplete_sets=incomplete,
            incomplete_total=len(getattr(plan.selection, "incomplete", ()) or ()),
            exclusions=self._exclusion_counts(plan), vanish=self._vanish_summary(plan),
            warnings=[w["text"] for w, _ in state.items[wkey]],
            actionable=actionable, empty=actionable == 0,
            missing_dats=list(state.missing_dats), unmatched_dir=UNMATCHED_DIR,
            reserved_dirs=list(RESERVED_DIRS))
        return page

    def library_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        key = self._library_flags(body)
        self._library_plan(state, *key)  # errors (e.g. module missing) before starting a job

        def work(job: Job) -> Any:
            plan = self._library_plan(state, *key)
            todo = sum(1 for op in plan.ops if op.status in ACTIONABLE) + sum(
                1 for p in plan.playlists if p.status == "write")
            job.report(0, todo, "Building library...")
            apply = _mod("dreamcast").apply_plan if self._is_dc(state) else _mod("organiser").apply_renames
            res = dict(_call(apply, plan.ops, state.root, progress=job.report,
                             cancel=job.cancel, playlists=list(plan.playlists)))
            res["action"] = "library"
            return self._rescan_into(job, state, res)

        return {"job": self.jobs.start("library", work, cancellable=False).to_dict()}

    def convert_plan(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        available = bool(getattr(state.platform, "convertible", False))
        latest_only = self._latest_arg(state, body)
        ops = self._convert_plan(state, latest_only) if available else []
        rows = self._convert_rows(state, latest_only) if available else []
        status = _str_arg(body.get("status"))
        if status:
            rows = [r for r in rows if r[0]["status"] == status]
        page = _page(rows, body.get("offset"), body.get("limit"), body.get("q"))
        page.update(counts=_status_counts(ops), all=len(ops), root=str(state.root), available=available,
                    latest_only=latest_only, originals_dir=CONVERTED_DIR)
        if self._is_dc(state):
            page["chdman"] = self._chdman_info()
            page["kind"] = "chd"
        return page

    def convert_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        if not getattr(state.platform, "convertible", False):
            raise ApiError(HTTPStatus.CONFLICT, f"{state.platform.name} files are not converted")
        latest_only = self._latest_arg(state, body)
        self._convert_plan(state, latest_only)  # errors (e.g. module missing) before starting a job
        if self._is_dc(state) and self._chdman() is None:   # never pretend to convert
            raise ApiError(HTTPStatus.CONFLICT, _mod("chdtool").INSTALL_HINT)

        def work(job: Job) -> Any:
            ops = self._convert_plan(state, latest_only)
            todo = sum(1 for op in ops if op.status == "convert")
            job.report(0, todo, "Converting files...")
            if self._is_dc(state):
                chdman = self._chdman()
                if chdman is None:
                    raise ApiError(HTTPStatus.CONFLICT, _mod("chdtool").INSTALL_HINT)
                res = dict(_mod("dreamcast").apply_conversions(ops, state.root, chdman, state.result.index,
                                                              progress=job.report, cancel=job.cancel))
            else:
                res = dict(_call(_mod("convert").apply_conversions, ops, state.root, progress=job.report,
                                 cancel=job.cancel))
            res["action"] = "convert"
            return self._rescan_into(job, state, res)

        return {"job": self.jobs.start("convert", work).to_dict()}

    def m3u_plan(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        ops = self._m3u_plan(state, _bool_arg(body.get("savedisk")), _bool_arg(body.get("labels"), True))
        status = _str_arg(body.get("status"))
        rows = self._m3u_rows(state, _bool_arg(body.get("savedisk")), _bool_arg(body.get("labels"), True))
        if status:
            rows = [r for r in rows if r[0]["status"] == status]
        page = _page(rows, body.get("offset"), body.get("limit"), body.get("q"))
        page.update(counts=_status_counts(ops), all=len(ops), root=str(state.root),
                    m3u_dats=list(state.platform.m3u_dats or ()))
        return page

    def m3u_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        savedisk, labels = _bool_arg(body.get("savedisk")), _bool_arg(body.get("labels"), True)

        def work(job: Job) -> Any:
            ops = self._m3u_plan(state, savedisk, labels)
            job.report(0, sum(1 for op in ops if op.status in ("write", "stale")), "Writing M3U playlists...")
            res = _mod("m3u").write_m3us(ops)
            state.m3u_plans.clear()  # statuses changed (write -> ok)
            for key in [k for k in state.items if isinstance(k, tuple) and k[0] == "m3u"]:
                del state.items[key]
            return res

        return {"job": self.jobs.start("m3u", work, cancellable=False).to_dict()}

    def kickstart_dirs(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        with self._lock:
            state = self._scan
        raw = _str_arg(query.get("platform"))
        platform = self._resolve_platform(raw) if raw else (state.platform if state is not None else self._resolve_platform(""))
        dirs = []
        for item in _mod("kickstart").detect_system_dirs():
            if isinstance(item, dict) and isinstance(item.get("path"), (str, os.PathLike)):
                dirs.append({"path": str(item["path"]), "label": str(item.get("label") or ""),
                             "exists": bool(item.get("exists", True))})
        dirs.sort(key=lambda d: not d["exists"])  # existing first, otherwise keep order
        return {"dirs": dirs, "last": _kick_dest(self._config(), platform.name),
                "platform": platform.name, "kickstart_dat": platform.kickstart_dat,
                "kickstart_folder": getattr(platform, "kickstart_folder", "") or ""}

    @staticmethod
    def _require_kickstart(state: ScanState) -> None:
        if not getattr(state.platform, "kickstart_dat", None):
            raise ApiError(HTTPStatus.CONFLICT, "This platform has no Kickstart DAT")

    def kickstart_plan(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        platform, state = self._kick_scope(body.get("platform"))
        dest = self._validate_dest(body.get("dest"))
        ops, extra = self._kick_ops(platform, state, dest)
        status = _str_arg(body.get("status"))
        root = Path(extra["root"])
        items = [self._kick_item(op, root) for op in ops if not status or op.status == status]
        page = _page(items, body.get("offset"), body.get("limit"), body.get("q"))
        page.update(counts=_status_counts(ops), all=len(ops), dest=str(dest), dest_exists=dest.is_dir(),
                    platform=platform.name, **extra)
        return page

    def kickstart_dest_save(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Remember the Kickstart destination of ONE platform as soon as the user picked it."""
        platform = self._resolve_platform(body.get("platform"))
        if not _has_kickstart(platform):
            raise ApiError(HTTPStatus.CONFLICT, "This platform has no Kickstart DAT or Kickstart folder")
        dest = self._validate_dest(body.get("dest"))
        self._config_update(kickstart_for=(platform.name, str(dest)), strict=True)
        return {"platform": platform.name, "dest": str(dest)}

    def kickstart_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        platform, state = self._kick_scope(body.get("platform"))
        dest = self._validate_dest(body.get("dest"))
        if getattr(platform, "kickstart_folder", ""):
            self._kick_folder(platform, state)    # a clear 409 now when the platform folder is unknown

        def work(job: Job) -> Any:
            ops, extra = self._kick_ops(platform, state, dest)  # fresh plan: the folder may have changed
            root = Path(extra["root"])
            todo = sum(1 for op in ops if op.status == "copy")
            job.report(0, todo, "Copying Kickstart ROMs...")
            if todo:
                dest.mkdir(exist_ok=True)
            res = dict(_call(_mod("kickstart").apply_kickstarts, ops, root=root, progress=job.report))
            self._config_update(kickstart_for=(platform.name, str(dest)))
            res["counts"] = _status_counts(self._kick_ops(platform, state, dest)[0])
            res["dest"] = str(dest)
            res["platform"] = platform.name
            return res

        return {"job": self.jobs.start("kickstart", work, cancellable=False).to_dict()}

    # ---- Sega Dreamcast: chdman + Verify fully

    def chdman_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """chdman detection result (``?refresh=1`` forces a new look)."""
        info = self._chdman_info(refresh=_bool_arg(query.get("refresh")))
        info["engine"] = str(self._config().get("chd_engine") or "auto")
        return info

    def chdman_save(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Remember (empty = forget) the path of a chdman binary and the engine (``auto`` | ``python``)."""
        chdtool = _mod("chdtool")
        raw = body.get("path")
        values: dict[str, Any] = {}
        if raw is not None:
            if not isinstance(raw, str):
                raise ApiError(HTTPStatus.BAD_REQUEST, "path must be text")
            raw = raw.strip()
            if raw:
                p = Path(raw).expanduser()
                if not p.is_absolute() or not p.is_file() or not os.access(p, os.X_OK):
                    raise ApiError(HTTPStatus.BAD_REQUEST, f"Not an executable file: {raw}")
                raw = str(p)
            values[chdtool.CONFIG_KEY] = raw
        engine = body.get("engine")
        if engine is not None:
            if engine not in ("auto", "python"):
                raise ApiError(HTTPStatus.BAD_REQUEST, "engine must be auto or python")
            values["chd_engine"] = engine
        if not values:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Nothing to save (expected path and / or engine)")
        self._config_update(strict=True, **values)
        info = self._chdman_info(refresh=True)
        info["engine"] = str(self._config().get("chd_engine") or "auto")
        if raw and not info.get("found"):
            info["warning"] = f"{raw} did not answer like chdman"
        return info

    def dc_verify(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Verify fully: decode EVERY track of the ``identified`` CHDs (audio included) and compare with Redump."""
        state = self._require_scan()
        if not self._is_dc(state):
            raise ApiError(HTTPStatus.CONFLICT, f"{state.platform.name} has no CHD verification")
        cfg = self._config()

        def work(job: Job) -> Any:
            res = dict(_mod("dreamcast").verify_units(
                state.result, self._chdman(), progress=job.report, cancel=job.cancel,
                engine=str(cfg.get("chd_engine") or "auto"),
                workers=_mod("chdpool").default_workers(cfg.get("chd_workers"))))
            res["action"] = "verify"
            return self._rescan_into(job, state, res)

        return {"job": self.jobs.start("verify", work).to_dict()}

    def job_get(self, query: dict[str, str], body: Any) -> dict[str, Any] | None:
        job = self.jobs.current
        return job.to_dict() if job else None

    def job_cancel(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        return {"cancelled": self.jobs.cancel()}

    def quit(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """Stop the server (there is no terminal when launched as an AppImage / from Steam)."""
        job = self.jobs.current
        if job is not None and job.status == "running" and not job.cancellable:
            raise ApiError(HTTPStatus.CONFLICT, f"Wait for the {job.kind} job to finish first")
        self.closing.set()
        self.jobs.cancel()
        hook = self.shutdown_hook
        if hook is not None:
            def later() -> None:
                time.sleep(0.2)  # let this response go out first
                self.stop_jobs(STOP_TIMEOUT)  # e.g. a cancelled download cleaning up its temp files
                hook()
            threading.Thread(target=later, name="quit", daemon=True).start()
        return {"quitting": True}

    def stop_jobs(self, timeout: float = STOP_TIMEOUT) -> bool:
        """Ask a running job to stop at its next safe point and wait for it.

        Used on exit (SIGTERM, Ctrl+C, Quit). Organise / undo stop between two
        moves (everything done so far is in the undo log). True if no job is left running.
        """
        self.closing.set()
        try:
            self.updates.cancel()  # a DAT download stops at its next safe point (the .part file stays resumable)
        except Exception:  # noqa: BLE001
            pass
        job = self.jobs.current
        if job is None or job.status != "running":
            return True
        job.cancel.set()
        if job.thread is not None:
            job.thread.join(timeout)
        return job.status != "running"


ROUTES: dict[tuple[str, str], Callable[[App, dict[str, str], Any], Any]] = {
    ("GET", "/api/status"): App.status,
    ("GET", "/api/platforms"): App.platforms_list,
    ("GET", "/api/dats"): App.dats_list,
    ("POST", "/api/dats/update"): App.dats_update,
    ("GET", "/api/updates"): App.updates_get,
    ("POST", "/api/updates/check"): App.updates_check,
    ("POST", "/api/updates/cancel"): App.updates_cancel,
    ("GET", "/api/library/profile"): App.library_profile,
    ("POST", "/api/library/profile"): App.library_profile_save,
    ("POST", "/api/library/plan"): App.library_plan,
    ("POST", "/api/library/vanished"): App.library_vanished,
    ("POST", "/api/library/apply"): App.library_apply,
    ("POST", "/api/library/undo"): App.library_undo,
    ("POST", "/api/folders"): App.folders_save,
    ("POST", "/api/platforms/options"): App.platform_options,
    ("GET", "/api/fs/list"): App.fs_list,
    ("POST", "/api/fs/pick"): App.fs_pick,
    ("POST", "/api/scan"): App.scan_start,
    ("GET", "/api/scan/results"): App.scan_results,
    ("POST", "/api/organise/plan"): App.organise_plan,
    ("POST", "/api/organise/apply"): App.organise_apply,
    ("GET", "/api/organise/undo-logs"): App.undo_logs,
    ("POST", "/api/organise/undo"): App.organise_undo,
    ("POST", "/api/convert/plan"): App.convert_plan,
    ("POST", "/api/convert/apply"): App.convert_apply,
    ("GET", "/api/chdman"): App.chdman_get,
    ("POST", "/api/chdman"): App.chdman_save,
    ("POST", "/api/dc/verify"): App.dc_verify,
    ("POST", "/api/m3u/plan"): App.m3u_plan,
    ("POST", "/api/m3u/apply"): App.m3u_apply,
    ("GET", "/api/kickstart/dirs"): App.kickstart_dirs,
    ("POST", "/api/kickstart/plan"): App.kickstart_plan,
    ("POST", "/api/kickstart/apply"): App.kickstart_apply,
    ("POST", "/api/kickstart/dest"): App.kickstart_dest_save,
    ("GET", "/api/job"): App.job_get,
    ("POST", "/api/job/cancel"): App.job_cancel,
    ("POST", "/api/quit"): App.quit,
}


def read_static(name: str) -> bytes:
    """Read a UI file from the package (works from a directory or a zipapp)."""
    return importlib.resources.files(PACKAGE).joinpath("static").joinpath(name).read_bytes()


# ------------------------------------------------------------------- HTTP layer


class RomorgServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: App, verbose: bool = False) -> None:
        if ":" in address[0]:  # IPv6 literal such as ::1
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)
        self.app = app
        self.verbose = verbose

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def allowed_hosts(self) -> set[str]:
        port = self.port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        bind = str(self.server_address[0])
        if bind not in ("", "0.0.0.0", "::"):
            hosts.add(f"{bind}:{port}")
        return hosts


class Handler(BaseHTTPRequestHandler):
    server: RomorgServer
    server_version = "simple-rom-organiser"
    timeout = 30  # a stalled client (short body, open socket) can't pin a thread forever

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        if self.server.verbose:
            super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Method not allowed"})

    do_DELETE = do_PUT

    # ----------------------------------------------------------------- dispatch

    def _dispatch(self, method: str) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid Host header"})
            return
        url = urlsplit(self.path)
        if url.path.startswith("/api/"):
            self._api(method, url.path, {k: v[-1] for k, v in parse_qs(url.query).items()})
        elif method == "GET":
            self._static(url.path)
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _api(self, method: str, path: str, query: dict[str, str]) -> None:
        app = self.server.app
        handler = ROUTES.get((method, path))
        if handler is None:
            exists = any(p == path for _, p in ROUTES)
            status = HTTPStatus.METHOD_NOT_ALLOWED if exists else HTTPStatus.NOT_FOUND
            self._send_json(status, {"error": status.phrase})
            return
        try:
            body: Any = None
            if method == "POST":
                token = self.headers.get(TOKEN_HEADER, "")
                if not secrets.compare_digest(token.encode(), app.token.encode()):
                    raise ApiError(HTTPStatus.FORBIDDEN, "Missing or invalid token")
                body = self._read_json()
            data = handler(app, query, body)
            self._send_json(HTTPStatus.OK, data)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Bad Content-Length") from None
        if length > MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
        try:
            raw = self.rfile.read(length) if length > 0 else b""
        except OSError:  # incl. socket timeout: the client never sent the whole body
            self.close_connection = True
            raise ApiError(HTTPStatus.REQUEST_TIMEOUT, "Request body not received") from None
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Body must be JSON") from None
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")
        return data

    def _static(self, path: str) -> None:
        if path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        name = "index.html" if path in ("/", "/index.html") else path.rsplit("/", 1)[-1]
        if path not in ("/", "/index.html", f"/static/{name}", f"/{name}") or name not in STATIC_TYPES:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            data = read_static(name)
        except OSError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        if name == "index.html":
            data = data.replace(TOKEN_PLACEHOLDER.encode(), self.server.app.token.encode())
        self._send(HTTPStatus.OK, data, STATIC_TYPES[name])

    def _send_json(self, status: int, data: Any) -> None:
        self._send(status, dumps(data), "application/json; charset=utf-8")

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)


def make_server(host: str = "127.0.0.1", port: int = 0, token: str | None = None,
                verbose: bool = False, auto_update: bool = True) -> RomorgServer:
    """Create (but do not start) the server; ``port=0`` picks a free port.

    Unless ``auto_update`` is off (or ``ROMORG_OFFLINE=1``), the DAT update check starts in the
    background right after the port is bound."""
    server = RomorgServer((host, port), App(token, auto_update=auto_update), verbose=verbose)
    server.app.shutdown_hook = server.shutdown
    try:
        server.app.sweep_chd_temp()
    except Exception:  # noqa: BLE001 - housekeeping only
        traceback.print_exc()
    try:
        server.app.updates.start_background()
    except Exception:  # noqa: BLE001 - updates are a convenience, never fatal
        traceback.print_exc()
    return server


def open_browser(url: str) -> None:
    """Open the UI in the default browser, falling back to xdg-open."""
    try:
        if webbrowser.open(url):
            return
    except Exception:
        pass
    opener = shutil.which("xdg-open")
    if opener:
        try:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    print(f"Could not open a browser - visit {url} manually.", file=sys.stderr)


INSTANCE_FILE = "instance.json"


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def _instance_file() -> Path | None:
    try:
        return Path(_mod("paths").data_dir()) / INSTANCE_FILE
    except Exception:
        return None


def running_instance() -> str | None:
    """URL of another live instance of the app (from data_dir/instance.json), or None."""
    path = _instance_file()
    if path is None:
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        port, host, pid = int(info["port"]), str(info.get("host") or "127.0.0.1"), int(info.get("pid") or 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid == os.getpid():
        return None
    if pid and os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except OSError:
            pass  # exists but belongs to someone else: let the probe decide
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{port}/"
    try:
        import urllib.request

        with urllib.request.urlopen(url + "api/status", timeout=1.5) as resp:
            if "version" in json.loads(resp.read()):
                return url
    except (OSError, ValueError):
        pass
    return None


def _write_instance(host: str, port: int) -> Path | None:
    path = _instance_file()
    if path is None:
        return None
    try:
        path.write_text(json.dumps({"pid": os.getpid(), "host": host, "port": port}), encoding="utf-8")
    except OSError:
        return None
    return path


def _remove_instance(path: Path | None) -> None:
    if path is None:
        return
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simple-rom-organiser",
                                     description="Match and organise ROM folders against TOSEC and No-Intro DATs.")
    parser.add_argument("--port", type=int, default=0, help="port to listen on (default: random free port)")
    parser.add_argument("--host", default="127.0.0.1", help="loopback address to bind (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a web browser")
    parser.add_argument("--new-instance", action="store_true",
                        help="start a new server even if one is already running")
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request")
    parser.add_argument("--no-update", action="store_true",
                        help="do not check for / download newer DATs at startup (cached DATs are still used)")
    args = parser.parse_args(argv)

    if not _is_loopback(args.host):
        # Anyone who can reach the port could read the token from the page and move files.
        print(f"Refusing to bind to {args.host}: the app may only listen on this machine "
              "(127.0.0.1, localhost or ::1).", file=sys.stderr)
        return 2
    if not args.new_instance:
        existing = running_instance()
        if existing:
            print(f"Simple ROM Organiser is already running at {existing}", flush=True)
            if not args.no_browser:
                open_browser(existing)
            return 0
    try:
        _mod("tosec").recover_dats_dir(Path(_mod("paths").dats_dir()))  # after a killed extract
    except Exception:
        pass
    try:
        server = make_server(args.host, args.port, verbose=args.verbose, auto_update=not args.no_update)
    except OSError as exc:
        print(f"Could not start server: {exc}", file=sys.stderr)
        return 1
    shown_host = "127.0.0.1" if args.host == "localhost" else args.host
    if ":" in shown_host:
        shown_host = f"[{shown_host}]"
    url = f"http://{shown_host}:{server.port}/"
    instance = _write_instance(args.host, server.port)
    print(f"Simple ROM Organiser is running at {url}", flush=True)
    print("Press Ctrl+C to quit.", flush=True)

    def _terminate(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, AttributeError, OSError):
        pass
    if not args.no_browser:
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
    finally:
        try:
            # A job (e.g. organise) stops at its next safe point; its undo log is complete.
            if not server.app.stop_jobs(STOP_TIMEOUT):
                print("A job is still running; exiting anyway.", file=sys.stderr)
        except KeyboardInterrupt:  # second Ctrl+C / SIGTERM: exit now
            pass
        server.server_close()
        _remove_instance(instance)
    return 0


if __name__ == "__main__":
    sys.exit(main())
