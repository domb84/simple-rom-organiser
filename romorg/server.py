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
import contextlib
import dataclasses
import functools
import importlib
import importlib.resources
import inspect
import itertools
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
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


def _staged(job: "Job", index: int, count: int, label: str) -> Callable[..., None]:
    """A progress callback that maps ``(done, total, message)`` of one step onto its share of a job of ``count`` steps:
    ``done`` becomes ``index + done / total``, so the job's percentage (and ETA) follow the whole run."""
    def report(*args: Any, **kwargs: Any) -> None:
        nums = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
        texts = [a for a in args if isinstance(a, (str, os.PathLike))]
        frac = min(1.0, nums[0] / nums[1]) if len(nums) > 1 and nums[1] else 0.0
        text = Path(texts[0]).name if texts and isinstance(texts[0], os.PathLike) else (texts[0] if texts else "")
        job.report(index + frac, count, f"[{min(index + 1, count)}/{count}] {label}" + (f": {text}" if text else ""))
    return report


def _existing_parent(path: Path) -> Path:
    """``path`` itself, or its nearest ancestor that exists (for a free-space check on a folder yet to be created)."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


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

    def __init__(self, job_id: int, kind: str, cancellable: bool = True, platform: str | None = None) -> None:
        self.id = job_id
        self.kind = kind
        self.platform = platform           # the system the job works on (the UI shows it on that system's card)
        self.cancellable = cancellable
        self.status = "running"
        self.progress: dict[str, Any] = {"done": 0, "total": 0, "message": ""}
        self.result: Any = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.cancel = CancelToken()
        self.bytes = 0                     # the data the job dealt with, fixed when it ends
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
                "platform": self.platform,
                "status": self.status,
                "cancellable": self.cancellable,
                "progress": dict(self.progress),
                "bytes": self.bytes if self.finished is not None else _mod("meter").value(),
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
        self._exclusive = ""               # a request that moves files without being a job (an undo) is at work

    def start(self, kind: str, fn: Callable[[Job], Any], cancellable: bool = True,
              platform: str | None = None) -> Job:
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise ApiError(HTTPStatus.CONFLICT, f"A {self._current.kind} job is already running")
            if self._exclusive:
                raise ApiError(HTTPStatus.CONFLICT, f"Wait: {self._exclusive} is still at work")
            job = Job(self._next_id, kind, cancellable, platform)
            _mod("meter").reset()
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
            job.bytes = _mod("meter").value()
            job.finished = time.time()

    @property
    def current(self) -> Job | None:
        with self._lock:
            return self._current

    @contextlib.contextmanager
    def exclusive(self, what: str) -> Any:
        """For a request that moves files in its own thread (the undo of a collection build ...): refused while a job runs
        (they would move the same files at the same time), and no job starts until it is through."""
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise ApiError(HTTPStatus.CONFLICT, f"A {self._current.kind} job is running: wait for it to finish, or stop it", "busy")
            if self._exclusive:
                raise ApiError(HTTPStatus.CONFLICT, f"Wait: {self._exclusive} is still at work", "busy")
            self._exclusive = what
        try:
            yield
        finally:
            with self._lock:
                self._exclusive = ""

    def cancel(self) -> bool:
        job = self.current
        if job is None or job.status != "running" or not job.cancellable:
            return False
        job.cancel.set()
        return True


# --------------------------------------------------------------------- app state

from . import folders  # noqa: E402  (the reserved folder names live in folders.py only)
from .folders import CONVERTED_DIR, RESERVED_DIRS, SAVES_DIR, SUPERSEDED_DIR, UNMATCHED_DIR  # noqa: E402
# Organiser statuses that mean "this op changes something on disk".
ACTIONABLE = ("move", "rename", "delete")
# Display / sort order for organiser statuses ("grouped by status").
ORGANISE_ORDER = ("move", "rename", "delete", "conflict", "duplicate", "skip", "ok")
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
CONVERT_ORDER = ("convert", "conflict", "skip")
MAX_TITLE = 120
LAYOUT_PER_DAT, LAYOUT_FLAT = "per_dat", "flat"
LAYOUT_GAME_FOLDER = "game_folder"   # disc systems: <root>/<Redump name>/<Redump name>.chd
CHDMAN_TTL = 60.0                    # seconds a chdman detection result is reused
SOURCE_TOSEC, SOURCE_NOINTRO = "tosec", "nointro"
# Result kinds whose rows carry name tags (filterable by region / language / video / flag / rule).
TAG_KINDS = ("matched", "missing", "games")
TAG_FILTERS = ("region", "language", "video", "flag", "rule")
# Result kinds whose rows can show checksums (/api/scan/checksums, ``checksums=1`` on /api/scan/results).
CHECKSUM_KINDS = ("matched", "missing", "unmatched", "games")
HASH_KEYS = ("crc32", "md5", "sha1")
# Reason categories of the Build library plan (row filter ``reason``).
LIBRARY_REASONS = ("kept", "bios", "excluded", "superseded", "incomplete", "duplicate", "unmatched", "playlist")
# Profile fields the save endpoint accepts (besides ``reset``).
PROFILE_KEYS = ("exclude", "latest_only", "best_variant", "complete_only", "languages", "keep_flags",
                "rescue_only_dump", "region_priority", "one_per_game", "borrow_other_editions",
                "min_rating", "top_n", "min_votes", "keep_unrated", "rank_scope", "keep_other_language", "saved_games", "keep_bios")
# Fallback labels of the exclusion rules (``tags.RULE_LABELS`` wins when present).
RULE_LABELS = {
    "bad_dump": "Bad dumps [b]", "virus": "Virus-infected [v]", "bad_size": "Over/under dumps [o] [u]",
    "pre_release": "Pre-release / beta / alpha / preview / debug", "prototype": "Prototypes",
    "demo": "Demos, samples, kiosk", "faked": "Faked [faked]", "unreleased": "Unreleased",
    "modified": "Modified [m]",
}


_SCAN_SERIAL = itertools.count(1)


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
    # by latest_only
    rename_plans: dict[bool, list[Any]] = field(default_factory=dict)
    # Build library plans (organiser.LibraryPlan) by (savedisk, labels); cleared when
    # the library profile changes.
    library_plans: dict[tuple[bool, bool], Any] = field(default_factory=dict)
    dats_sig: tuple = ()               # signature of the DAT files this scan parsed
    stamp: tuple | None = None         # (files, bytes, newest change) of the folder when the scan began (scancache)
    dats_changed: bool = False         # a DAT update was installed after the scan
    convert_plans: dict[bool, list[Any]] = field(default_factory=dict)  # by latest_only
    m3u_plans: dict[tuple[bool, bool], list[Any]] = field(default_factory=dict)
    # Serialised result rows (item, lower-case search text) and the summary, built
    # once per scan so paging / searching is a cheap slice.
    items: dict[Any, list[tuple[dict[str, Any], str]]] = field(default_factory=dict)
    summary: dict[str, Any] | None = None
    # The scan objects behind the rows of each checksum kind (same index as the row's ``id``), a lazy
    # entry-path -> Match index and the multi-disk sets of the matched disks (built on first use).
    sources: dict[str, list[Any]] = field(default_factory=dict)
    match_index: dict[str, Any] | None = None
    disk_sets: dict[int, Any] | None = None
    # Identity of this scan inside the running app (new for every scan / re-scan): keys the library totals
    # and the ``plan_id`` of a Build library preview.
    serial: int = field(default_factory=lambda: next(_SCAN_SERIAL))
    # The RetroArch saves of this system (saveindex.SaveReport), built once per scan and ONLY when a RetroArch config is
    # known (Amendment 31): None = no saves are looked at. ``saves_for`` = the config it was built for.
    saves: Any = None
    saves_for: str = ""
    save_walk: Any = None                # retroarch.SaveWalk: the one walk of the save folders (shared by a Collection's systems)


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


SAVE_KINDS = ("games", "matched", "missing", "saves")
SORT_KEYS = ("rating_desc", "rating_asc", "name_asc", "name_desc", "year_asc", "year_desc", "size_asc", "size_desc")


def _sort_rows(rows: list[tuple[dict[str, Any], str]], sort: str,
               name: Callable[[dict[str, Any]], str]) -> list[tuple[dict[str, Any], str]]:
    """Sort result rows by ``rating | name | year | size`` + ``_asc | _desc`` (``rating`` alone = ``rating_desc``).
    Rows without the sorted value (unrated, no year, no size) are always last, whichever way it runs; ties fall back to
    the votes (ratings), then the name. Other values keep the order."""
    sort = "rating_desc" if sort == "rating" else sort
    if sort not in SORT_KEYS:
        return rows
    key, _, direction = sort.partition("_")
    desc = direction == "desc"
    if key == "name":
        return sorted(rows, key=lambda r: name(r[0]).casefold(), reverse=desc)
    have = [r for r in rows if r[0].get(key) is not None]
    lack = [r for r in rows if r[0].get(key) is None]
    sign = -1 if desc else 1
    have.sort(key=lambda r: (sign * r[0][key], sign * (r[0].get("votes") or 0) if key == "rating" else 0,
                             name(r[0]).casefold()))
    lack.sort(key=lambda r: name(r[0]).casefold())
    return have + lack


def _status_counts(ops: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for op in ops:
        counts[op.status] = counts.get(op.status, 0) + 1
    return counts


def _order_key(order: tuple[str, ...]) -> Callable[[Any], int]:
    return lambda op: order.index(op.status) if op.status in order else len(order)


def _rom_dat(rom: Any) -> str:
    return getattr(rom, "dat", "") or ""


def _rom_key_of(rom: Any) -> tuple[str, str]:
    return (getattr(rom, "dat", ""), getattr(rom, "set_name", "") or getattr(rom, "name", ""))


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


def _layout(platform: Any) -> str:
    """Platform layout: "per_dat" (one folder per DAT, default) or "flat" (everything in the root)."""
    return _enum_str(getattr(platform, "layout", None), LAYOUT_PER_DAT)


def _optional_mod(name: str) -> Any:
    """A sibling module, or None if it does not exist (yet)."""
    try:
        return _mod(name)
    except ImportError:
        return None


def _year_of(rom: Any) -> int | None:
    """The release year of a TOSEC rom (its name's date paren), else None (No-Intro / Redump names carry none)."""
    try:
        date = getattr(rom.tags, "date", "") or ""
    except Exception:  # noqa: BLE001 - informative only
        return None
    return int(date[:4]) if date[:4].isdigit() else None


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


def _os_name() -> str:
    """``windows``, ``linux``, ``darwin`` or ``sys.platform``: for UI text that differs per platform."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _is_hidden(path: Path) -> bool:
    """A folder the folder browser leaves out unless asked: a dot name, and on Windows what is marked hidden or system
    (``$RECYCLE.BIN``, ``System Volume Information``, ``AppData`` ...)."""
    if path.name.startswith("."):
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)          # (only Windows has it)
    except OSError:
        return False
    return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


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


def _slug(name: str) -> str:
    """URL-safe form of a system name for the UI routes (``#/system/<slug>/<tab>``)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "system"


def scan_record(summary: dict[str, Any], root: Any, dat_names: Any = (), now: float | None = None,
                saves: Any = None) -> dict[str, Any]:
    """The small last-scan record kept per platform in config.json (Amendment 15): counts + time + folder.

    Never holds lists or per-file data - it only feeds the system cards of the home screen."""
    by_game = summary.get("count_by") == "game"

    def num(*keys: str) -> int:
        for key in keys:
            value = summary.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0

    total = num("games_total", "dat_total") if by_game else num("dat_total")
    have = num("games_have", "have") if by_game else num("have")
    missing = num("games_missing", "missing") if by_game else num("missing")
    record: dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() if now is None else now)),
        "folder": str(root), "count_by": "game" if by_game else "rom",
        "total": total, "have": have, "missing": missing,
        "pct": round(100.0 * have / total, 2) if total else 0.0,
        "matched_files": num("matched_files"), "unmatched_files": num("unmatched_files"),
        "duplicates": num("duplicates"), "errors": num("errors"), "bios": num("bios_files"),
        "dats": [str(n) for n in (dat_names or ())][:8],
    }
    if summary.get("chd_files") is not None:           # disc systems
        record.update(chd_files=num("chd_files"), identified=num("identified"), verified=num("verified"),
                      raw=num("raw"))
    if saves is not None:                              # (only with a RetroArch config: the badge of the system list)
        totals = saves.totals()
        record["saves"] = {"files": int(totals["files"]), "sets": int(totals["sets"])}
    return record


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
        self._scans: dict[str, ScanState] = {}                    # the finished scans of the systems visited, most recent last
        self._undo_counts: dict[tuple[str, int, int], int] = {}
        self._dat_cache: tuple[Any, tuple[list[Any], list[str]]] | None = None
        self._lang_cache: dict[Any, list[dict[str, Any]]] = {}  # available languages of one platform
        self._region_cache: dict[str, tuple[tuple, dict[str, int]]] = {}   # platform -> regions found in its DATs
        self._lang_cache_games: dict[str, tuple[tuple, frozenset]] = {}   # platform -> games that have a version in your languages
        self._chdman_cache: tuple[float, Any, dict[str, Any]] | None = None
        self._totals_mgr: Any = None                           # totals.TotalsManager (library totals, Amendment 17)
        self._cutoffs: dict[str, tuple[tuple, Any]] = {}       # platform -> (key, rank key of the N-th best game) (Amendment 18)
        if hasattr(self.updates, "ratings_wanted"):
            self.updates.ratings_wanted = self._ratings_wanted
        self.closing = threading.Event()  # set when the app is told to exit
        self.shutdown_hook: Callable[[], None] | None = None  # set by make_server
        self._adopt_old_folders()
        self._migrate_library()

    def _migrate_library(self) -> None:
        """An older config: the rules of a Collection become your defaults, each system's saved profile its list of overrides (never raises)."""
        try:
            cfg = self._config()
            if "collection" in cfg or "library" in cfg:
                _mod("paths").update_config(lambda c: self._library_mod().migrate_config(c))
        except Exception:  # noqa: BLE001 - a convenience only
            traceback.print_exc()

    def _adopt_old_folders(self) -> None:
        """The Switch, Wii and Wii U had pages of their own with a games folder each; they are systems now, with a folder like any
        other. A folder chosen on such a page becomes the system's folder, once, when the system has none (never raises)."""
        try:
            cfg = self._config()
            have = self._folders(cfg)
            old = cfg.get("nintendo") if isinstance(cfg.get("nintendo"), dict) else {}
            sw = cfg.get("switch") if isinstance(cfg.get("switch"), dict) else {}
            for name, folder in (("Nintendo Switch", sw.get("games")),
                                 ("Nintendo Wii", (old.get("wii") or {}).get("games") if isinstance(old.get("wii"), dict) else None),
                                 ("Nintendo Wii U", (old.get("wiiu") or {}).get("games") if isinstance(old.get("wiiu"), dict) else None)):
                if name not in have and isinstance(folder, str) and folder.strip() and os.path.isdir(folder):
                    self._config_update(folder_for=(name, folder))
        except Exception:  # noqa: BLE001 - a convenience only
            traceback.print_exc()

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
                return module.UpdateManager(enabled=enabled, switchdb=_mod("switchdb"), gametdb=_mod("gametdb"))
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
                       record_for: tuple[str, dict[str, Any] | None] | None = None,
                       archive_for: tuple[str, str | None] | None = None,
                       **values: Any) -> dict[str, Any]:
        """Merge values into config.json and return the saved config.

        ``folder_for=(platform, path)`` remembers a platform's folder (``path=None`` forgets it);
        ``latest_for=(platform, bool)`` remembers the "latest version only" choice;
        ``record_for=(platform, record)`` remembers the small last-scan summary of a platform (Amendment 15).
        The change is applied to the LATEST config.json under ``paths.update_config``'s lock, so a
        concurrent writer (scan, profile save, ...) can never be overwritten by a stale snapshot.
        ``strict``: a failed write raises ``ApiError(500)`` instead of being swallowed.
        """
        self._id_memo = None                               # (what is set up may have changed: the emulators are looked up again)
        def mutate(cfg: dict[str, Any]) -> None:
            cfg.update(values)
            for key, pair in (("folders", folder_for), ("latest_only", latest_for),
                              ("scan_records", record_for),
                              ("archive_overrides", archive_for)):
                if pair is None:
                    continue
                table = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
                if pair[1] is None:
                    table.pop(pair[0], None)
                else:
                    table[pair[0]] = pair[1]
                cfg[key] = table

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

    # ---------------------------------------------------------------- ratings (Amendment 18)

    def _ratings_wanted(self) -> bool:
        """True when a rating filter is enabled in any platform's saved profile (the updater then keeps the data fresh)."""
        library = _optional_mod("library")
        if library is None:
            return False
        cfg = self._config()
        try:
            return any(library.load_profile(cfg, p).rating_active for p in _mod("platforms").list_platforms())
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return False

    @staticmethod
    def _ratings_store() -> Any:
        return _mod("ratings").default_store()

    def _rating_lookup(self, platform: Any) -> Callable[[str], Any] | None:
        """``title -> (rating, votes) | None`` of the installed index, or None when it is not installed."""
        store = self._ratings_store()
        if not store.available():
            return None
        return lambda title: store.lookup(platform, title)

    def _ratings_sig(self) -> str:
        """Identity of the installed index (changes when it is rebuilt); "" when none is installed."""
        man = self._ratings_store().meta()
        return str(man.get("built_at", "")) if man else ""

    def _ratings_pending(self, platform: Any, profile: Any) -> bool:
        """True when ``profile`` needs ratings that are not installed (and a download is asked for)."""
        if not profile.rating_active or self._rating_lookup(platform) is not None:
            return False
        try:
            self.updates.request_ratings()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return True

    def _rating_context(self, platform: Any, profile: Any) -> Any:
        """``library.RatingContext`` for a plan (None without a rating filter). Raises 409 ``ratings_pending`` while the
        data is missing: a filter is never applied with missing data."""
        if not profile.rating_active:
            return None
        library = self._library_mod()
        lookup = self._rating_lookup(platform)
        if lookup is None:
            self._ratings_pending(platform, profile)
            ust = self.updates.status()
            status = ust.get("ratings") or {}
            if not ust.get("enabled"):
                msg = ("The ratings data is not installed and automatic downloads are switched off in this run - "
                       "start the app without --no-update (or without ROMORG_OFFLINE) to download it, or clear the rating filter.")
            elif status.get("error"):
                msg = f"The ratings data could not be installed: {status['error']} (press Download ratings in the Ratings rules to retry)"
            else:
                msg = "The ratings data is not installed yet - it is being downloaded. Preview and Build wait for it."
            raise ApiError(HTTPStatus.CONFLICT, msg, "ratings_pending")
        cutoff = None
        if profile.top_n is not None and profile.rank_scope == "dat":
            totals = _mod("totals")
            dats, _missing = self._platform_dats(platform)
            key = (totals.profile_signature(profile), totals.signature_text(self._dats_signature(platform)), self._ratings_sig())
            cached = self._cutoffs.get(platform.name)
            if cached is not None and cached[0] == key:
                cutoff = cached[1]
            else:
                items = totals.target_items(platform, list(dats))
                cutoff = library.target_cutoff(items, profile, platform, lookup)
                self._cutoffs[platform.name] = (key, cutoff)
        return library.RatingContext(lookup, cutoff)

    def _profile_json(self, profile: Any, platform: str | None = None) -> dict[str, Any]:
        data = profile.to_dict() if hasattr(profile, "to_dict") else dataclasses.asdict(profile)
        out = {**data, "exclude": sorted(data.get("exclude") or ())}
        if not self._saves_available(platform):  # no save source known for it: the app does not know that saves exist
            out.pop("saved_games", None)
            out.pop("saved_overrides", None)
        return out

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

    def _available_regions(self, platform: Any) -> dict[str, int]:
        """``{region: games}`` of the platform's installed region-filtered DATs (``library.available_regions``), cached per DAT
        versions: the region list is what the data has, not a list made up in advance."""
        library = _optional_mod("library")
        scope = tuple(getattr(platform, "region_dats", ()) or ())
        func = getattr(library, "available_regions", None)
        if func is None or not scope:
            return {}
        try:
            key = (platform.name, self._dats_signature(platform))
            hit = self._region_cache.get(platform.name)
            if hit is not None and hit[0] == key:
                return hit[1]
            dats, _missing = self._platform_dats(platform)
            counts = dict(func([d for d in dats if getattr(d, "name", "") in scope]))
        except Exception:  # noqa: BLE001 - informative only: without it the list falls back to every known region
            traceback.print_exc()
            return {}
        self._region_cache[platform.name] = (key, counts)
        return counts

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

    @staticmethod
    def _ranking_short() -> str:
        """The chipset ranking in a few words ("AGA over OCS"), for the one-line rules summary."""
        library = _optional_mod("library")
        tags = getattr(library, "tags", None) or _optional_mod("tags")
        order = list(getattr(tags, "PLATFORM_ORDER", None) or ("CD32", "AGA", "OCS"))
        return " over ".join(order)

    def _profile_info(self, platform: Any, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        """The ``/api/library/profile`` answer for one platform (Amendment 8: catalog, languages, regions)."""
        library = self._library_mod()
        profile = self._profile(platform, cfg)
        labels = self._rule_labels()
        on = set(profile.exclude)
        info: dict[str, Any] = {
            "platform": platform.name,
            "profile": self._profile_json(profile, platform.name),
            "defaults": self._profile_json(library.default_profile(platform), platform.name),
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
        cfg_now = self._config() if cfg is None else cfg
        info["inherit"] = {"overridden": self._overridden(platform, cfg_now), "applicable": sorted(library.applicable_fields(platform)),
                           "defaults": self._profile_json(library.effective_profile(platform, cfg_now.get("library_defaults") or {}, {}), platform.name)}
        build = getattr(library, "profile_info", None)
        region_counts = self._available_regions(platform)
        info["region_counts"] = region_counts
        if build is not None:
            extra = _call(build, platform, profile, regions_present=(set(region_counts) if region_counts else None))
            for key in ("style", "catalog", "regions", "language_names", "rating_codes", "reason_labels"):
                if key in extra:
                    info[key] = extra[key]
            info["available"] = {**info["available"], **extra.get("available", {})}
            info["scopes"] = {**info["scopes"], **extra.get("scopes", {})}
        info["available_languages"] = self._available_languages(platform) if info["available"].get("languages") else []
        info["ranking"] = self._ranking_text(info.get("style") or SOURCE_TOSEC) if info["available"].get("best_variant") else ""
        info["ranking_short"] = self._ranking_short() if info["available"].get("best_variant") else ""
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

    def _shed_scan_caches(self) -> None:
        """A new scan is about to start: drop what the previous one derived (result pages, plans, checksum indexes) so the
        old and the new scan never sit in memory together. Pages and plans of the old results are rebuilt on demand."""
        with self._lock:
            state = self._scan
        self._shed_state(state)

    def _shed_state(self, state: "ScanState | None") -> None:
        if state is None:
            return
        self._drop_plans(state)
        state.items.clear()
        state.sources.clear()
        state.match_index = None
        result = state.result
        result.__dict__.pop("_base_units_cache", None)

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
        """Startup housekeeping: delete leftover ``.romorg-chd-*`` temp folders (dead runs) in the disc systems' folders."""
        chdtool = _optional_mod("chdtool")
        removed: list[str] = []
        if chdtool is None:
            return removed
        tempspace = _optional_mod("tempspace")
        if tempspace is not None:        # job folders of dead runs in RAM / the app cache (marker + dead pid only)
            tempspace.configure(self._config())
            removed += tempspace.sweep_stale()
        for platform in _mod("platforms").list_platforms():
            if _layout(platform) != LAYOUT_GAME_FOLDER:
                continue
            folder = self._folders().get(platform.name)
            if folder and os.path.isdir(folder):
                removed += chdtool.sweep_stale(Path(folder))
        return removed

    def _configure_temp(self, cfg: dict | None = None) -> None:
        """Scratch-space settings (``temp_dir``, ``temp_ram_reserve_mb``; env vars win) for the next decode."""
        tempspace = _optional_mod("tempspace")
        if tempspace is not None:
            tempspace.configure(cfg if cfg is not None else self._config())

    @staticmethod
    def _carry_temp(state: ScanState, res: dict[str, Any]) -> None:
        """Keep the 'where did the decode go' report visible after the job's re-scan (which decodes nothing)."""
        temp = res.get("temp")
        if not temp:
            return
        if isinstance(res.get("summary"), dict):
            res["summary"]["temp"] = temp
            res["summary"]["temp_text"] = temp.get("text", "")
        if hasattr(state.result, "temp"):
            state.result.temp = temp

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
            notes = list(getattr(chdtool, "last_notes", lambda: [])())
            if found is not None:
                info = found.to_dict()
                info["hint"] = ""
            else:
                info = {"found": False, "kind": "", "label": "",
                        "hint": (notes[0] + ". " if notes else "") + chdtool.install_hint(),
                        "steps": chdtool.install_steps()}         # Windows: chdman.exe from mamedev.org; else MAME
            info["override"] = str(cfg.get(chdtool.CONFIG_KEY) or "")
            info["notes"] = notes
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

    def _overridden(self, platform: Any, cfg: dict[str, Any] | None = None) -> list[str]:
        """The library rules this system overrides (the rest follow your defaults in Settings)."""
        try:
            return list(self._library_mod().overridden_fields(self._config() if cfg is None else cfg, platform))
        except Exception:  # noqa: BLE001 - a convenience for the systems list
            traceback.print_exc()
            return []

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
            "convert_on_build": self._convert_on_build(platform, cfg),
            "chd": layout == LAYOUT_GAME_FOLDER,
            "disc": self._disc_info(platform, cfg),
            "folder": self._folders(cfg).get(platform.name),
            "latest_only": self._latest_only(platform, cfg),
            "library": self._profile_for_info(platform, cfg),
            "rule_overrides": self._overridden(platform, cfg),
            "dats": dats,
            "complete": all(d["present"] for d in dats),
            "m3u_dats": list(platform.m3u_dats or ()),
            "kickstart_dat": platform.kickstart_dat,
            "kickstart_folder": getattr(platform, "kickstart_folder", "") or "",
            "protected_dirs": list(getattr(platform, "protected_dirs", ()) or ()),
            "slug": _slug(platform.name),
            "last_scan": self._last_scan_shown(cfg, platform.name),
        }

    def _last_scan_shown(self, cfg: dict[str, Any], name: str) -> dict[str, Any] | None:
        """The last-scan record for the system list; its saves figure only while a RetroArch config is known (no trace otherwise)."""
        record = self._last_scan(cfg, name)
        if record is not None and "saves" in record and not self._saves_available(name):
            record = {k: v for k, v in record.items() if k != "saves"}
        return record

    @staticmethod
    def _last_scan(cfg: dict[str, Any], name: str) -> dict[str, Any] | None:
        table = cfg.get("scan_records") if isinstance(cfg.get("scan_records"), dict) else {}
        record = table.get(name)
        return record if isinstance(record, dict) else None

    @staticmethod
    def _disc_info(platform: Any, cfg: dict[str, Any]) -> dict[str, Any] | None:
        """The disc-system settings of a Redump + CHD platform (None for the others)."""
        discsys = _optional_mod("discsys")
        system = discsys.system_for_platform(platform.name) if discsys is not None else None
        if system is None:
            return None
        info = system.to_dict()
        info["iso_mode"] = discsys.iso_convert_mode(system, cfg)
        return info

    def _profile_for_info(self, platform: Any, cfg: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return self._profile_json(self._profile(platform, cfg), platform.name)
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

    def _run_scan(self, job: Job, root: Path, platform: Any, cancellable: bool = True,
                  keep: bool = True, report: Callable[..., None] | None = None, force: bool = False) -> ScanState | None:
        """Scan ``root``. ``keep=False`` returns the state without making it the current scan.
        ``report`` replaces ``rep`` (it maps this scan's progress onto its share of a larger job)."""
        rep = report or job.report
        # DATs are kept current automatically: a platform whose DATs are not installed yet waits for
        # (or starts) the update here, with its progress shown as this job's progress.
        try:
            self.updates.ensure(platform, progress=rep, cancel=job.cancel)
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
        if keep:
            self._shed_scan_caches()
        rep(0, 0, f"Loading {platform.name} DATs...")
        dats, missing = self._platform_dats(platform)
        sig = self._dats_signature(platform)  # what this scan parsed (see _scan_is_stale)
        clear = getattr(self.updates, "clear_stale", None)
        if callable(clear):
            clear()
        if not dats:
            raise ApiError(HTTPStatus.CONFLICT,
                           f"No DATs for {platform.name} are installed (automatic DAT updates are off or unavailable)",
                           "no_dats")
        rep(0, 0, "Scanning...")
        stamp = _mod("scancache").folder_stamp(root) if keep else None
        layout = _layout(platform)
        if layout == LAYOUT_GAME_FOLDER:     # disc systems (Dreamcast, PlayStation, PlayStation 2): CHD / raw sets, matched per track
            cfg = self._config()
            self._configure_temp(cfg)
            result = _mod("discsys").scan(
                root, dats, progress=rep, cancel=job.cancel if cancellable else None,
                chdman=self._chdman(), engine=str(cfg.get("chd_engine") or "auto"),
                workers=_mod("chdsched").default_workers(cfg.get("chd_workers")), full=bool(cfg.get("chd_verify_scan")),
                protected_dirs=tuple(getattr(platform, "protected_dirs", ()) or ()), refresh_cache=force)
        else:
            result = _call(_mod("scanner").scan, root, dats, recursive=True, progress=rep,
                           cancel=job.cancel if cancellable else None,
                           alt_hashes=tuple(getattr(platform, "alt_hashes", ()) or ()), layout=layout,
                           protected_dirs=tuple(getattr(platform, "protected_dirs", ()) or ()),
                           containers=tuple(getattr(platform, "containers", ()) or ()), refresh_cache=force,
                           id_matcher=self._switch_matcher())
        if cancellable and job.cancel.is_set():
            return None
        try:                                             # BIOS / firmware files among what no game matched (the checksums are already there)
            bios = _mod("bios")
            result.bios_files = bios.find_in_files(result.junk) if layout == LAYOUT_GAME_FOLDER else bios.find_in_scan(result)

        except Exception:  # noqa: BLE001 - then each file is looked at when the plan needs it
            traceback.print_exc()
        if platform.name == "Nintendo Switch" and self._switch_cfg().get("verify_scan"):
            try:
                self._switch_check_files(result, rep, job.cancel if cancellable else None)
            except InterruptedError:
                return None
        dat_names = list(getattr(result, "dat_names", None) or [getattr(d, "name", "") for d in dats])
        result.cache_units = True      # a finished scan is never edited: its organiser units can be reused
        state = ScanState(result=result, root=root, platform=platform, dat_names=dat_names,
                          missing_dats=list(missing), layout=_enum_str(getattr(result, "layout", None), layout),
                          dats_sig=sig, stamp=stamp)
        if keep:
            with self._lock:
                self._scan = state
            self._remember_scan(state)
            self._record_scan(state)
        return state

    def _scan_options(self) -> tuple:
        """The settings that change what a scan finds (a saved scan made with others is not used)."""
        return (bool(self._config().get("chd_verify_scan")), bool(self._switch_cfg().get("verify_scan")), _mod("bios").signature())

    def _keep_scan_on_disk(self, state: ScanState) -> None:
        """Save the finished scan of a system, so it is there after a restart (``scancache``; never raises)."""
        try:
            scancache = _mod("scancache")
            stamp = state.stamp if state.stamp is not None else scancache.folder_stamp(state.root)
            scancache.save(state.platform.name, state.root, stamp, state.dats_sig, self._scan_options(), state.result,
                           {"dat_names": state.dat_names, "missing_dats": state.missing_dats, "layout": state.layout})
        except Exception:  # noqa: BLE001 - a convenience only
            traceback.print_exc()

    def _scan_from_disk(self, platform: Any, folder: str) -> ScanState | None:
        """The saved scan of a system when nothing changed since it was made (the folder, the DATs, the scan options)."""
        try:
            root = Path(os.path.abspath(os.path.expanduser(folder)))
            got = _mod("scancache").load(platform.name, root, self._dats_signature(platform), self._scan_options())
            if got is None:
                return None
            extra = got["extra"]
            result = got["result"]
            result.cache_units = True
            return ScanState(result=result, root=root, platform=platform, dat_names=list(extra["dat_names"]),
                             missing_dats=list(extra["missing_dats"]), layout=extra["layout"],
                             dats_sig=self._dats_signature(platform), stamp=got["stamp"])
        except Exception:  # noqa: BLE001 - then the scan is simply done again
            traceback.print_exc()
            return None

    def _switch_matcher(self) -> Any:
        """The scanner's matcher for Switch files (told by title ID, with the user's ``prod.keys`` for a game card dump)."""
        switchapp, switchmatch = _mod("switchapp"), _mod("switchmatch")
        key = switchapp.header_key(self._switch_cfg())
        return lambda roms: switchmatch.make_matcher(roms, key)

    @staticmethod
    def _switch_check_files(result: Any, rep: Callable[..., None], cancel: Any) -> None:
        """The option "also check every file's checksums": each matched ``.nsp`` / ``.xci`` has every NCA hashed and compared with its own
        name (``switchverify``). The verdict of a file that has not changed since it was last checked is remembered. ``result.checks`` is
        ``{path: ok | damaged | not checked}``."""
        switchverify = _mod("switchverify")
        files = sorted({m.entry.path for m in result.matched if getattr(m.entry, "member", None) is None
                        and m.entry.path.suffix.lower() in (".nsp", ".xci")}, key=os.fspath)
        cache = switchverify.VerifyCache()
        checks: dict[str, str] = {}
        try:
            todo = []
            for path in files:
                got = cache.get(path)
                if got is not None:
                    checks[os.fspath(path)] = got.status
                else:
                    todo.append(path)
            total = sum(p.stat().st_size for p in todo)
            base = 0
            for path in todo:
                size = path.stat().st_size
                verdict = switchverify.verify_file(path, None, lambda done, _t, label, b=base: rep(b + done, total, f"Checking {label}"), cancel)
                cache.put(path, verdict)
                checks[os.fspath(path)] = verdict.status
                base += size
        finally:
            cache.close()
        result.checks = checks

    SCANS_KEPT = 6

    def _remember_scan(self, state: ScanState, on_disk: bool = True) -> None:
        """Keep the finished scan of a system, so coming back to it later needs no new scan (``scan_select``). The oldest are
        let go (and what they derived with them)."""
        dropped: list[ScanState] = []
        with self._lock:
            self._scans.pop(state.platform.name, None)
            self._scans[state.platform.name] = state
            while len(self._scans) > self.SCANS_KEPT:
                oldest = next(iter(self._scans))
                dropped.append(self._scans.pop(oldest))
        for old in dropped:
            self._shed_state(old)
        if on_disk:
            self._keep_scan_on_disk(state)

    def scan_select(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/scan/select {platform}``: make the scan kept for that system the current one (no scanning). ``selected`` is
        false when there is none or the folder of the system is not the one that was scanned."""
        platform = self._resolve_platform(body.get("platform"))
        with self._lock:
            state = self._scans.get(platform.name)
            current = self._scan
        folder = self._folders().get(platform.name)
        if state is None and folder and Path(folder).is_dir():
            state = self._scan_from_disk(platform, folder)         # the scan made before the app was last closed
            if state is not None:
                self._remember_scan(state, on_disk=False)
        if state is None or not folder or os.path.normcase(str(state.root)) != os.path.normcase(os.path.abspath(os.path.expanduser(folder))):
            return {"selected": False}
        if current is not state:
            self._shed_state(current)
            with self._lock:
                self._scan = state
            self._config_update(last_platform=platform.name)
        return {"selected": True, "platform": platform.name}

    def _record_scan(self, state: ScanState) -> None:
        """Remember the last scan summary of this system (cards survive a restart; never raises)."""
        try:
            record = scan_record(self._summary(state), state.root, state.dat_names, saves=self._saves_report(state))
            self._config_update(record_for=(state.platform.name, record))
        except Exception:  # noqa: BLE001 - a convenience only
            traceback.print_exc()

    def _summary(self, state: ScanState) -> dict[str, Any]:
        if state.summary is None:
            summary = dict(state.result.summary())
            checks = getattr(state.result, "checks", None)
            if checks is not None:
                values = list(checks.values())
                summary["checks"] = {"ok": values.count("ok"), "damaged": values.count("damaged"),
                                     "not_checked": len(values) - values.count("ok") - values.count("damaged")}
            summary["missing_dats"] = list(state.missing_dats)
            bios = _mod("bios")        # (the files no game matched that are BIOS files, and the games of a "- Firmware" DAT: the Amiga's Kickstarts)
            summary["bios_files"] = len(getattr(state.result, "bios_files", None) or {}) + bios.count_firmware_matches(state.result)
            state.summary = summary
        return dict(state.summary)

    def _rename_plan(self, state: ScanState, latest_only: bool = False) -> list[Any]:
        key = latest_only
        if key not in state.rename_plans and self._is_dc(state):
            state.rename_plans[key] = sorted(_mod("discsys").plan_tidy(state.result),
                                              key=_order_key(ORGANISE_ORDER))
        if key not in state.rename_plans:
            kwargs: dict[str, Any] = {"missing_dats": list(state.missing_dats),
                                      "layout": state.layout}
            if latest_only:  # only passed when wanted: older organisers lack the option
                kwargs["latest_only"] = True
            ops = list(_call(_mod("organiser").plan_renames, state.result, **kwargs))
            ops.sort(key=_order_key(ORGANISE_ORDER))  # stable: keeps organiser order per status
            state.rename_plans[key] = ops
        return state.rename_plans[key]

    def _rename_rows(self, state: ScanState, latest_only: bool = False) -> list[tuple[dict[str, Any], str]]:
        key = ("rename", latest_only)
        if key not in state.items:
            state.items[key] = _rows([self._rename_item(op, state.root)
                                      for op in self._rename_plan(state, latest_only)])
        return state.items[key]

    def _convert_plan(self, state: ScanState, latest_only: bool = False) -> list[Any]:
        if not getattr(state.platform, "convertible", False):
            return []
        if latest_only not in state.convert_plans and self._is_dc(state):
            ops = list(_mod("discsys").plan_convert(state.result, self._chdman() is not None, self._config()))
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
        self._drop_all_plans()

    def _drop_plans_of(self, platform: Any) -> None:
        self._drop_all_plans()

    def _drop_all_plans(self) -> None:
        """The rules changed: no plan made with the old ones is served again (the current scan's, nor those of the scans kept)."""
        with self._lock:
            states = [self._scan, *self._scans.values()]
        for state in states:
            if state is not None:
                self._drop_plans(state)

    def _plan_warnings(self, state: ScanState, rows: list[tuple[dict[str, Any], str]]) -> list[str]:
        """Signs that the chosen folder is not just this platform's ROM folder."""
        to_unmatched = matched = 0
        systems: set[str] = set()
        for item, _ in rows:
            if item.get("superseded_by") or item.get("code"):  # matched, archived for a library rule
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

    def _lang_games(self, platform: Any, prof: Any) -> frozenset | None:
        """Games of the platform's whole DAT with a version in the selected languages (``keep_other_language``), cached
        per rules and DAT versions; None when the rule is off."""
        if not (getattr(prof, "keep_other_language", False) and prof.languages):
            return None
        totals = _mod("totals")
        key = (platform.name, totals.profile_signature(prof), totals.signature_text(self._dats_signature(platform)))
        cached = self._lang_cache_games.get(platform.name)
        if cached is not None and cached[0] == key:
            return cached[1]
        dats, _missing = self._platform_dats(platform)
        value = self._library_mod().language_games(totals.target_items(platform, list(dats)), prof, platform)
        self._lang_cache_games[platform.name] = (key, value)
        return value

    # ---------------------------------------------------------------- saves of a scan (Amendment 31)
    # Nothing below runs unless a RetroArch config is known (``_ra_active``): then every scan gets a save report.
    def _saves_available(self, platform: str | None = None) -> bool:
        """True when the saves of ``platform`` are looked at: its own emulator is set up (Dolphin, PCSX2 ...), or, for a system
        RetroArch plays, a RetroArch config is known. Without a platform (Collection): any of them."""
        idsaves = _mod("idsaves")
        if platform is not None and idsaves.applies(platform):
            now = time.monotonic()                              # (asked for every row of the systems list: looked up at most every 1.5 s)
            memo = getattr(self, "_id_memo", None)
            if memo is None or now - memo[0] >= 1.5:
                nin, eff = self._nin_cfg(), self._switch_eff()
                memo = self._id_memo = (now, {p: idsaves.active(p, nin, eff) for p in idsaves.PLATFORM_SOURCES})
            return memo[1].get(platform, False)
        if platform is None:
            return self._saves_anywhere()                    # (Collection's rules: any save source of any system)
        return self._ra_active() is not None

    def _saves_anywhere(self) -> bool:
        """True when some system's saves are looked at: a RetroArch config is known or an emulator of an ID-based system is set up."""
        idsaves = _mod("idsaves")
        return self._ra_active() is not None or any(idsaves.active(p, self._nin_cfg(), self._switch_eff()) for p in idsaves.PLATFORM_SOURCES)

    def _ra_active(self) -> Any:
        """The RetroArch install whose saves count, or None. With none the app looks at no save folder at all."""
        now = time.monotonic()
        memo = getattr(self, "_ra_memo", None)
        if memo is not None and now - memo[0] < 1.5:
            return memo[1]
        try:
            _ra, installs, saved = self._ra_state()
            sel = self._ra_selected(installs, saved)
        except Exception:  # noqa: BLE001 - a broken RetroArch folder never stops anything
            traceback.print_exc()
            sel = None
        self._ra_memo = (now, sel)
        return sel

    def _saves_forget(self) -> None:
        """The RetroArch choice changed: every scan's save report is built again when it is next needed."""
        self._ra_memo = None
        self._id_memo = None
        for holder in (self._scan,):
            if holder is not None:
                for attr, empty in (("saves", None), ("saves_for", ""), ("save_walk", None), ("save_reports", {})):
                    if hasattr(holder, attr):
                        setattr(holder, attr, empty)

    def _saves_forget_all(self) -> None:
        """`_saves_forget` and the plans / result pages built with the old choice."""
        self._saves_forget()
        if self._scan is not None:
            self._drop_plans(self._scan)

    def _playlists_of(self, paths: Iterable[Path]) -> list[tuple[str, list[str]]]:
        """``(name, disc names)`` of the given ``.m3u`` files (RetroArch names a multi-disc game's saves after its playlist)."""
        m3u = _mod("m3u")
        out: list[tuple[str, list[str]]] = []
        for path in paths:
            info = m3u.read_m3u(path)
            if info is not None and info[1]:
                out.append((Path(path).stem, [Path(p).stem for p in info[1]]))
        return out

    def _saves_report(self, state: ScanState, walk: Any = None, m3us: Iterable[Path] | None = None) -> Any:
        """The ``saveindex.SaveReport`` of a scan (built once, kept on the scan), or None when no save source is set up for the system:
        RetroArch's (a system RetroArch plays, when a config is known) or its emulator's (PCSX2, Dolphin ...)."""
        idsaves = _mod("idsaves")
        by_id = idsaves.applies(state.platform.name)
        if by_id:
            if not idsaves.active(state.platform.name, self._nin_cfg(), self._switch_eff()):
                return None
            sel, key = None, idsaves.config_key(state.platform.name, self._nin_cfg(), self._switch_eff())
        else:
            sel = self._ra_active()
            if sel is None:
                return None
            key = str(sel.cfg)
        if state.saves is not None and state.saves_for == key:
            return state.saves
        try:
            saveindex = _mod("saveindex")
            by_file, by_dat = saveindex.game_refs(item for item, _t in self._kind_rows(state, "games"))
            if by_id:
                owned = idsaves.owned_games((item for item, _t in self._kind_rows(state, "games")), Path(state.root))
                state.saves = saveindex.SaveReport(per_core=False, install="")
                for st in idsaves.sets_for(state.platform.name, owned, self._nin_cfg(), self._switch_eff(), _mod("switchapp").header_key(self._switch_cfg())):
                    state.saves.sets[st.key] = st
            else:
                ra = _mod("retroarch")
                walk = walk or state.save_walk
                if walk is None or str(walk.install.cfg) != key:
                    walk = ra.SaveWalk(sel)
                state.save_walk = walk
                if m3us is None:
                    m3us = _mod("m3u").find_m3us(state.root) if Path(state.root).is_dir() else ()
                state.saves = saveindex.build(walk.for_platform(state.platform), by_file, by_dat, walk.per_core, sel.label,
                                              playlists=self._playlists_of(m3us))
            state.saves_for = key
        except Exception:  # noqa: BLE001 - the saves are a courtesy: a broken save folder never stops a scan
            traceback.print_exc()
            return None
        return state.saves

    def _make_library_plan(self, state: ScanState, prof: Any, savedisk: bool, labels: bool) -> Any:
        """A fresh ``LibraryPlan`` of the scan for the given rules (not cached). With a RetroArch config the plan carries
        ``save_report`` (the scan's saves) and a game the rules would set aside whose choice is "keep" is kept."""
        rctx = self._rating_context(state.platform, prof)
        report = self._saves_report(state)
        saved = report.keys() if report is not None else None
        saved_arg = {"saved": saved} if saved else {}
        plan = self._make_library_plan_core(state, prof, savedisk, labels, rctx, saved_arg)
        if report is not None:
            plan.save_report = report
        return plan

    def _make_library_plan_core(self, state: ScanState, prof: Any, savedisk: bool, labels: bool, rctx: Any,
                                saved_arg: dict[str, Any]) -> Any:
        if self._is_dc(state):
            plan = _mod("discsys").plan_library(state.result, prof, savedisk=savedisk, labels=labels,
                                                **({"ratings": rctx} if rctx is not None else {}), **saved_arg)
            plan.ops.sort(key=_order_key(ORGANISE_ORDER))
            return plan
        organiser = _mod("organiser")
        planner = getattr(organiser, "plan_library", None)
        if planner is None:
            raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "Build library is not available in this version")
        plan = _call(planner, state.result, prof,
                     missing_dats=list(state.missing_dats),
                     layout=state.layout, savedisk=savedisk, labels=labels, platform=state.platform,
                     lang_games=self._lang_games(state.platform, prof),
                     **({"ratings": rctx} if rctx is not None else {}), **saved_arg)
        try:
            plan.ops.sort(key=_order_key(ORGANISE_ORDER))  # stable: keeps organiser order per status
        except AttributeError:
            pass
        return plan

    def _library_plan(self, state: ScanState, savedisk: bool = False,
                      labels: bool = True) -> Any:
        """The cached ``organiser.LibraryPlan`` for the platform's current profile."""
        key = (savedisk, labels)
        cached = state.library_plans.get(key)
        if cached is not None and getattr(cached, "profile", None) not in (None, self._profile(state.platform)):
            # the rules changed behind our back (config.json edited, another client): never serve an old plan
            self._drop_plans(state)
        if key not in state.library_plans:
            state.library_plans[key] = self._make_library_plan(state, self._profile(state.platform), savedisk, labels)
        return state.library_plans[key]

    @staticmethod
    def _library_category(item: dict[str, Any]) -> str:
        """Reason category of a Build library row (the ``reason`` filter)."""
        if item.get("item") == "playlist" or item.get("status") == "delete":
            return "playlist"
        if item.get("bios"):
            return "bios"
        if item.get("code"):
            return str(item["code"])
        return "unmatched" if item.get("dest") in RESERVED_DIRS else "kept"

    def _library_rows(self, state: ScanState, key: tuple[bool, bool]) -> list[tuple[dict[str, Any], str]]:
        ikey = ("library", key)
        if ikey not in state.items:
            plan = self._library_plan(state, *key)
            items = [self._rename_item(op, state.root) for op in plan.ops]
            keep_bios = getattr(getattr(plan, "profile", None), "keep_bios", True)
            for item, op in zip(items, plan.ops):
                if not keep_bios and not getattr(op, "bios", False):
                    item["bios"] = False              # (rule off: a Kickstart is a game of its DAT, like any other)
                item["category"] = self._library_category(item)
                info = self._saves_effect(plan, op)           # (None without a RetroArch config: the row has no such field)
                if info is not None:
                    item["saves"] = info
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
            **({"checks": state.result.checks.get(path, "not checked")} if getattr(state.result, "checks", None) is not None else {}),
            "set_name": getattr(primary[0], "set_name", "") if primary else "",
            "other_dats": sorted({_rom_dat(r) for r in match.roms} - {dat, ""}),
            "named_ok": named,
            "placed_ok": placed,
            # archived on purpose (_excluded/, _superseded/ ...): not "needs moving"
            "aside": _rel(path, root).split("/", 1)[0] in RESERVED_DIRS,
            "via": via,
            "header": getattr(match, "header", 0) or 0,
            "byte_order": getattr(match, "byte_order", "") or "",
            "tags": _tags_json(primary[0], tags_mod) if primary else None,
        }

    @staticmethod
    def _missing_item(rom: Any, tags_mod: Any = None) -> dict[str, Any]:
        return {"name": rom.name, "game": rom.game, "size": rom.size, "crc": rom.crc, "dat": _rom_dat(rom),
                "set_name": getattr(rom, "set_name", "") or "", "tags": _tags_json(rom, tags_mod)}

    @staticmethod
    def _game_title(rom: Any, dat: str = "") -> str:
        library = _optional_mod("library")
        if library is None or rom is None:
            return ""
        try:
            return library.rom_title(rom, dat)
        except Exception:  # noqa: BLE001 - informative only
            return ""

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
                       "tags": _tags_json(rom, tags_mod), "title": self._game_title(rom, g.get("dat", "")),
                       "year": _year_of(rom), "size": getattr(rom, "size", None)}
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
                                              "files": [], "tags": _tags_json(rom, tags_mod),
                                              "title": self._game_title(rom, key[0])})
                if rom.name not in row["roms"]:
                    row["roms"].append(rom.name)
                rel = _entry_rel(match.entry, state.root)
                if rel not in row["files"]:
                    row["files"].append(rel)
        for rom in state.result.missing:
            key = (_rom_dat(rom), getattr(rom, "set_name", "") or rom.name)
            groups.setdefault(key, {"name": key[1], "dat": key[0], "have": False, "roms": [rom.name],
                                    "files": [], "tags": _tags_json(rom, tags_mod),
                                    "title": self._game_title(rom, key[0])})
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
            "bios": bool(getattr(op, "bios", False)) or bool(
                not getattr(op, "code", "") and not getattr(op, "unmatched", False) and op.status not in ("conflict", "skip", "delete")
                and _mod("bios").is_firmware_dat(getattr(op, "dat", ""))),
            "item": "file",
        }
        if hasattr(op, "moves"):           # Sega Dreamcast: a game folder / CHD with its sidecars
            item.update(dest=getattr(op, "folder", "") or "", game=op.game, level=op.level, unit_kind=op.unit_kind,
                        n_files=op.n_files, files=len(op.moves) or op.n_files)
        return item

    @staticmethod
    def _convert_item(op: Any, root: Path) -> dict[str, Any]:
        src = _rel(op.src, root)
        row = {
            "from": f"{src}::{op.member}" if getattr(op, "member", None) else src,
            "to": _rel(op.dst, root),
            "original_to": _rel(op.original_dst, root),
            "status": op.status,
            "reason": getattr(op, "reason", "") or "",
            "rom_name": getattr(op, "rom_name", "") or "",
            "via": getattr(op, "via", "") or "",
        }
        if getattr(op, "mode", ""):          # disc systems: the chdman command (createcd | createdvd)
            row["mode"] = op.mode
        if getattr(op, "note", ""):          # e.g. "the .gdi was generated from the .cue ..."
            row["note"] = op.note
        return row

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
            "notes": list(getattr(op, "notes", ()) or ()),
        }

    # ------------------------------------------------------------- endpoints

    def status(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": getattr(importlib.import_module(PACKAGE), "__version__", "0"),
            "release": None, "dats_count": 0, "default_platform": None,
            "last_platform": None, "last_dir": None, "folders": {},
            "has_7z": _which_7z() is not None,
            "data_dir": None, "scan": None, "os": _os_name(),
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
        exp = cfg.get("library_export") if isinstance(cfg.get("library_export"), dict) else {}
        out["library_export"] = {"enabled": bool(exp.get("enabled")), "dest": str(exp.get("dest") or ""),
                                 "mode": _mod("libexport").normalize_mode(exp.get("mode")),
                                 "sidecars": bool(exp.get("sidecars")), "sync": bool(exp.get("sync")),
                                 "aside": exp.get("aside", True) is not False}      # (archiving is on unless it was switched off)
        out["archive"] = self._archive_settings(cfg)
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
                "recursive": state.recursive, "summary": self._summary(state), "id": state.serial,
            }
            report = self._saves_report(state)
            if report is not None:                 # (the field exists only when a RetroArch config is known)
                out["scan"]["saves"] = report.totals()
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

    @staticmethod
    def _convert_on_build(platform: Any, cfg: dict[str, Any]) -> bool:
        """The Library option "convert raw discs to CHD" / "clean up dumps" of a system (off by default: it takes a while)."""
        table = cfg.get("convert_on_build") if isinstance(cfg.get("convert_on_build"), dict) else {}
        return bool(getattr(platform, "convertible", False)) and bool(table.get(platform.name))

    def platform_options(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Remember per-platform options right away (``latest_only``, ``convert``), not only on the next request - so a scan
        or DAT download in between keeps them."""
        platform = self._resolve_platform(body.get("platform"))
        if "latest_only" not in body and "convert" not in body:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Nothing to save (expected latest_only or convert)")
        if "latest_only" in body:
            self._save_latest_only(platform, _bool_arg(body.get("latest_only")))
        if "convert" in body:
            def mutate(cfg: dict[str, Any]) -> None:
                table = cfg.get("convert_on_build") if isinstance(cfg.get("convert_on_build"), dict) else {}
                table[platform.name] = _bool_arg(body.get("convert"))
                cfg["convert_on_build"] = table
            _mod("paths").update_config(mutate)
        return {"platform": platform.name, "latest_only": self._latest_only(platform),
                "convert": self._convert_on_build(platform, self._config())}

    def updates_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        return self.updates_status()

    def databases_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """``GET /api/databases``: every database the app uses with its address, the systems that use it and its state."""
        return {"databases": _mod("databases").describe(self.updates_status())}

    def bios_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """``GET /api/bios``: the lists BIOS / firmware files are told by, the library rule, and what the last scans found."""
        bios, library = _mod("bios"), self._library_mod()
        cfg = self._config()
        found, over = [], []
        for p in _mod("platforms").list_platforms():
            record = self._last_scan(cfg, p.name)
            if record and record.get("bios"):
                found.append({"platform": p.name, "slug": _slug(p.name), "files": int(record["bios"]), "at": record.get("at", "")})
            if "keep_bios" in library.overridden_fields(cfg, p):
                over.append(p.name)
        return {"sources": bios.sources(), "found": found, "overridden": over, "keep_default": library.global_profile(cfg).keep_bios,
                "keys": sorted(bios.KEY_NAMES)}

    def updates_check(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """The "Check for updates" button: check now and download whatever is newer, in the background."""
        started = bool(self.updates.check(force=_bool_arg((body or {}).get("force"))))
        return {"started": started, "updates": self.updates_status()}

    def ratings_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """``GET /api/ratings``: the LaunchBox ratings index (status block of the updater plus the credit line)."""
        block = dict(self.updates.status().get("ratings") or {})
        ratings = _mod("ratings")
        block.setdefault("credit", ratings.CREDIT)
        block.setdefault("credit_url", ratings.CREDIT_URL)
        block["supported"] = [p.name for p in _mod("platforms").list_platforms() if ratings.supported(p)]
        block["refresh_days"] = ratings.REFRESH_DAYS
        return block

    def ratings_download(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """The "Download ratings" button: fetch + index the LaunchBox data now (background; progress in /api/updates)."""
        started = bool(self.updates.request_ratings())
        return {"started": started, "ratings": self.ratings_get({}, None), "updates": self.updates_status()}

    def updates_cancel(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        return {"cancelled": bool(self.updates.cancel())}

    def library_profile(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        return self._profile_info(self._resolve_platform(query.get("platform")))

    # ---- your defaults (Settings): the rules every system uses unless it overrides them
    def _defaults_platform(self) -> Any:
        """A system that has every rule, to build the Settings panel from the same catalog as a system's Library tab."""
        platforms = _mod("platforms")
        return platforms.Platform(name="Library defaults", dats=("*",), m3u_dats=("*",), kickstart_dat=None, source="tosec", layout="per_dat",
                                  latest_dats=("*",), best_variant_dats=("*",), language_dats=("*",), region_dats=("*",))

    def _defaults_info(self) -> dict[str, Any]:
        library, tags = self._library_mod(), _mod("tags")
        plat = self._defaults_platform()
        profile = library.global_profile(self._config())
        info = self._profile_info(plat, {"library_defaults": self._config().get("library_defaults") or {}})
        info["profile"] = self._profile_json(profile, "")
        info["defaults"] = self._profile_json(library.app_defaults(), "")
        info["available"] = {**info["available"], "ratings": True}
        info["available_languages"] = [{"code": c, "name": n, "count": None, "games": None} for c, n in tags.LANGUAGES.items()]
        info["defaults_page"] = True
        info["changed"] = [f for f in library.RULE_FIELDS if profile.to_dict()[f] != library.app_defaults().to_dict()[f]]
        return info

    def library_defaults_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """``GET /api/library/defaults``: the same answer as a system's profile, for your defaults."""
        return self._defaults_info()

    def library_defaults_save(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/library/defaults``: the same body as ``/api/library/profile`` without a platform (``reset`` = the app's own)."""
        library = self._library_mod()
        if not any(k in body for k in PROFILE_KEYS + ("reset",)):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Nothing to save")
        profile = library.app_defaults() if _bool_arg(body.get("reset")) else library.global_profile(self._config())
        profile = dataclasses.replace(profile, **self._profile_changes(body, library))
        _mod("paths").update_config(lambda cfg: library.store_global(cfg, profile))
        self._drop_all_plans()
        return self._defaults_info()

    def _profile_changes(self, body: dict[str, Any], library: Any) -> dict[str, Any]:
        """The rules a ``/api/library/profile`` (or ``/defaults``) body sets, validated, as ``LibraryProfile`` fields."""
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
            if isinstance(body.get("region_priority"), list):
                body = {**body, "region_priority": [tags.canon_region(r) if isinstance(r, str) else r
                                                    for r in body["region_priority"]]}
            changes["region_priority"] = tuple(names("region_priority", getattr(tags, "REGIONS", None), "regions"))
        for key in ("latest_only", "best_variant", "complete_only", "rescue_only_dump", "one_per_game",
                    "borrow_other_editions", "keep_other_language", "keep_bios"):
            if key in body:
                changes[key] = _bool_arg(body[key])
        changes.update(self._rating_changes(body, library))
        if "saved_games" in body:
            if body["saved_games"] not in library.SAVED_GAMES:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"saved_games must be one of: {', '.join(library.SAVED_GAMES)}")
            changes["saved_games"] = body["saved_games"]
        return changes

    def library_profile_save(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        platform = self._resolve_platform(body.get("platform"))
        library = self._library_mod()
        if not any(k in body for k in PROFILE_KEYS + ("reset", "override")):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Nothing to save")
        if _bool_arg(body.get("reset")):                       # every rule back to your defaults (the choices for single games stay)
            _mod("paths").update_config(lambda cfg: library.reset_overrides(cfg, platform))
        if "override" in body:
            spec = body["override"]
            if not isinstance(spec, dict) or not isinstance(spec.get("fields"), list) or not all(isinstance(f, str) for f in spec["fields"]):
                raise ApiError(HTTPStatus.BAD_REQUEST, "override must be {fields: [rule names], on: true | false}")
            _mod("paths").update_config(lambda cfg: library.set_override(cfg, platform, spec["fields"], _bool_arg(spec.get("on"), True)))
        profile = self._profile(platform)
        changes = self._profile_changes(body, library)
        if changes:
            self._save_profile(platform, dataclasses.replace(profile, **changes))
        else:
            self._drop_plans_of(platform)
        return self._profile_info(platform)

    def library_override(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/library/override``: ``{platform, action: keep | exclude | clear, games: [{dat, game}]}`` - the
        user's own "always keep" / "always exclude" of single games (stored in the platform's library profile)."""
        platform = self._resolve_platform(body.get("platform"))
        library = self._library_mod()
        action = _str_arg(body.get("action"))
        if action not in library.OVERRIDE_ACTIONS + ("clear",):
            raise ApiError(HTTPStatus.BAD_REQUEST, "action must be keep, exclude or clear")
        games = body.get("games")
        if not isinstance(games, list) or not games or len(games) > 5000 or not all(
                isinstance(g, dict) and _str_arg(g.get("dat")) and _str_arg(g.get("game")) for g in games):
            raise ApiError(HTTPStatus.BAD_REQUEST, "games must be a list of {dat, game} (1 to 5000)")
        profile = self._profile(platform)
        current = {(d, n): a for d, n, a in profile.overrides}
        for g in games:
            key = (_str_arg(g["dat"]), _str_arg(g["game"]))
            if action == "clear":
                current.pop(key, None)
            else:
                current[key] = action
        self._save_profile(platform, dataclasses.replace(
            profile, overrides=tuple((d, n, a) for (d, n), a in current.items())))
        return self._profile_info(platform)

    def library_saved(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/library/saved``: ``{platform, choice: keep | archive | leave | default, games: [{dat, game}]}`` - what
        a build does with the saves of single games the rules would replace or archive (stored in the platform's library
        profile; ``default`` = follow the profile's ``saved_games``). Only when a RetroArch config is known."""
        platform = self._resolve_platform(body.get("platform"))
        if not self._saves_available(platform.name):
            raise ApiError(HTTPStatus.CONFLICT, "No save source is set up for this system, so saves are not looked at.", "no_retroarch")
        library = self._library_mod()
        choice = _str_arg(body.get("choice"))
        if choice not in library.SAVED_GAMES + ("default",):
            raise ApiError(HTTPStatus.BAD_REQUEST, "choice must be keep, archive, leave or default")
        games = body.get("games")
        if not isinstance(games, list) or not games or len(games) > 5000 or not all(
                isinstance(g, dict) and _str_arg(g.get("dat")) and _str_arg(g.get("game")) for g in games):
            raise ApiError(HTTPStatus.BAD_REQUEST, "games must be a list of {dat, game} (1 to 5000)")
        profile = self._profile(platform)
        current = {(d, n): a for d, n, a in profile.saved_overrides}
        for g in games:
            key = (_str_arg(g["dat"]), _str_arg(g["game"]))
            if choice == "default":
                current.pop(key, None)
            else:
                current[key] = choice
        self._save_profile(platform, dataclasses.replace(
            profile, saved_overrides=tuple((d, n, a) for (d, n), a in current.items())))
        return self._profile_info(platform)

    @staticmethod
    def _rating_changes(body: dict[str, Any], library: Any) -> dict[str, Any]:
        """The rating fields of a profile save (strict: a wrong type or range is a 400, never silently ignored)."""
        out: dict[str, Any] = {}

        def number(key: str, lo: float, hi: float, integer: bool, nullable: bool) -> Any:
            v = body[key]
            if v is None or v == "":
                if nullable:
                    return None
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} needs a number")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be a number")
            if integer and float(v) != int(v):
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be a whole number")
            if not lo <= v <= hi:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be between {lo:g} and {hi:g}")
            return int(v) if integer else round(float(v), 1)

        if "min_rating" in body:
            v = number("min_rating", 0, 10, False, True)
            out["min_rating"] = None if not v else v          # 0 = off
        if "top_n" in body:
            out["top_n"] = number("top_n", 1, 1_000_000, True, True)
        if "min_votes" in body:
            out["min_votes"] = number("min_votes", 1, 1_000_000, True, False)
        if "keep_unrated" in body:
            if not isinstance(body["keep_unrated"], bool):
                raise ApiError(HTTPStatus.BAD_REQUEST, "keep_unrated must be true or false")
            out["keep_unrated"] = body["keep_unrated"]
        if "rank_scope" in body:
            if body["rank_scope"] not in library.RANK_SCOPES:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"rank_scope must be one of: {', '.join(library.RANK_SCOPES)}")
            out["rank_scope"] = body["rank_scope"]
        return out

    def fs_list(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        raw = query.get("path", "").strip()
        path = self._validate_dir(raw) if raw else Path.home().resolve()
        hidden = _bool_arg(query.get("hidden"))
        try:
            dirs = []
            for child in path.iterdir():
                if not hidden and _is_hidden(child):
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

    def scan_start(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        root = self._validate_dir(body.get("path"))
        _check_scan_root(root)
        platform = self._resolve_platform(body.get("platform"))
        force = _bool_arg(body.get("force"))              # recalculate every checksum (else only new or changed files are read)

        def work(job: Job) -> Any:
            state = self._run_scan(job, root, platform, force=force)
            if state is None:
                return None
            self._config_update(folder_for=(platform.name, str(root)), last_platform=platform.name,
                                last_dir=str(root))
            return self._summary(state)

        return {"job": self.jobs.start("scan", work, platform=platform.name).to_dict()}

    def scan_results(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        state = self._require_scan()
        kind = query.get("kind", "matched")
        dat = query.get("dat", "").strip()
        result, root = state.result, state.root
        facets = None
        if kind == "rename":
            latest = _bool_arg(query.get("latest_only"), self._latest_only(state.platform))
            rows = self._rename_rows(state, latest)
            if dat:
                folder = canonical if (canonical := folders.canonical_name(dat)) else _safe_name(dat)
                rows = [r for r in rows if r[0]["dest"] == folder]
        elif kind == "m3u":
            rows = self._m3u_rows(state, False, True)
        elif kind == "saves":                      # the save sets and the title each one belongs to (needs a RetroArch config)
            report = self._saves_report(state)
            if report is None:
                raise ApiError(HTTPStatus.CONFLICT, "No RetroArch config is known, so saves are not looked at.", "no_retroarch")
            rows = _rows(report.rows())
            match = query.get("match", "").strip()
            if match in ("rom", "dat", "none"):
                rows = [r for r in rows if r[0]["match"] == match]
        elif kind in ("matched", "unmatched", "missing", "unsupported", "errors", "games"):
            self._kind_rows(state, kind)
            rows = state.items[kind]
            if dat and kind in TAG_KINDS:
                rows = [r for r in rows if r[0]["dat"] == dat]
            rated_q = ""
            if kind == "games":
                have = query.get("have", "").strip()
                if have:
                    wanted = _bool_arg(have)
                    rows = [r for r in rows if r[0]["have"] is wanted]
                self._annotate_ratings(state, rows)
                rated_q = query.get("rated", "").strip()
                if rated_q:
                    wanted = _bool_arg(rated_q)
                    rows = [r for r in rows if (r[0].get("rating") is not None) is wanted]
                rows = _sort_rows(rows, query.get("sort", "").strip(), lambda i: i["name"])
            if kind in TAG_KINDS:
                key = ("facets", kind, dat, (query.get("have", "").strip() + "|" + rated_q) if kind == "games" else "")
                if key not in state.items:
                    state.items[key] = _facets(rows)  # type: ignore[assignment]
                facets = state.items[key]
                rows = _tag_filter(rows, query)
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Unknown result kind: {kind}")
        report = self._saves_report(state) if kind in SAVE_KINDS else None
        if report is not None and kind in ("games", "matched", "missing"):
            rows = self._saves_filter_sort(report, kind, rows, _bool_arg(query.get("saves")), query.get("sort", "").strip())
        elif kind in ("matched", "unmatched", "missing"):
            rows = _sort_rows(rows, query.get("sort", "").strip(),
                              lambda i: i.get("file") or i.get("set_name") or i.get("name") or "")
        elif kind == "saves":
            rows = self._saves_sort_sets(rows, query.get("sort", "").strip())
        page = _page(rows, query.get("offset"), query.get("limit"), query.get("q"))
        page["kind"] = kind
        if report is not None and kind in ("games", "matched", "missing"):
            page["saves_on"] = True
            page["items"] = [dict(it, saves=self._row_saves(report, kind, it)) for it in page["items"]]
        if kind == "games":
            page["has_year"] = any(r[0].get("year") is not None for r in state.items["games"][:2000])
        if facets is not None:
            page["facets"] = facets
        if kind in CHECKSUM_KINDS and _bool_arg(query.get("checksums")):
            # only the rows of this page, built from the scan result in memory (nothing is re-hashed)
            page["items"] = [dict(it, checksums=self._checksums(state, kind, it)) for it in page["items"]]
        return page

    @staticmethod
    def _saves_key(kind: str, item: dict[str, Any]) -> tuple[str, str]:
        """The (DAT, game) a Browse row stands for, as the save report names its games."""
        if kind == "games":
            return item.get("dat", ""), item.get("name", "")
        if kind == "missing":
            return item.get("dat", ""), item.get("set_name") or item.get("name", "")
        roms = item.get("roms") or [""]
        return item.get("dat", ""), item.get("set_name") or roms[0]

    def _row_saves(self, report: Any, kind: str, item: dict[str, Any]) -> dict[str, Any] | None:
        """``{saves, states, total, title_total}``: the saves of the game of a Browse row and, in ``title_total``, of every
        edition of its title (None: nothing)."""
        own = report.game_counts(*self._saves_key(kind, item))
        title = report.title_counts(item.get("title") or "") if kind == "games" else None
        if not own and not title:
            return None
        return {"saves": (own or {}).get("saves", 0), "states": (own or {}).get("states", 0), "total": (own or {}).get("total", 0),
                "title_total": (title or own or {}).get("total", 0)}

    def _saves_filter_sort(self, report: Any, kind: str, rows: list[tuple[dict[str, Any], str]], only: bool,
                           sort: str) -> list[tuple[dict[str, Any], str]]:
        """Browse rows: only the games with saves (``saves=1``), sorted by their saves (``saves_desc`` | ``saves_asc``) or as
        the other sorts do."""
        def total(item: dict[str, Any]) -> int:
            return (report.game_counts(*self._saves_key(kind, item)) or {}).get("total", 0)
        if only:
            rows = [r for r in rows if total(r[0])]
        if sort in ("saves_asc", "saves_desc"):
            return self._sort_saves(rows, sort.endswith("desc"), total)
        return _sort_rows(rows, sort, lambda i: i.get("file") or i.get("set_name") or i.get("name") or "")

    @staticmethod
    def _saves_sort_sets(rows: list[tuple[dict[str, Any], str]], sort: str) -> list[tuple[dict[str, Any], str]]:
        if sort in ("files_asc", "files_desc"):
            return sorted(rows, key=lambda r: r[0]["files"], reverse=sort.endswith("desc"))
        if sort in ("name_asc", "name_desc"):
            return sorted(rows, key=lambda r: (r[0]["title"] or r[0]["name"]).casefold(), reverse=sort.endswith("desc"))
        return rows

    def _annotate_ratings(self, state: ScanState, rows: list[tuple[dict[str, Any], str]]) -> None:
        """Browse -> Games: ``rating`` (0-10) / ``votes`` / ``rating_match`` on every game of a rated DAT (None = unrated or
        no ratings installed). Computed once per scan and ratings index, on the first page that needs it."""
        ratings = _mod("ratings")
        lookup_store = self._ratings_store()
        sig = self._ratings_sig() if lookup_store.available() else ""
        key = ("ratings", sig)
        if state.items.get(key) is True:
            return
        rated = set(ratings.rated_dats(state.platform))
        for item, _text in self._kind_rows(state, "games"):
            item["rating"] = item["votes"] = item["rating_match"] = None
            if not sig or item.get("dat") not in rated:
                continue
            title = item.get("title") or ""
            d = lookup_store.detail(state.platform, title) if title else None
            if d is not None:
                item["rating"], item["votes"], item["rating_match"] = d["rating"], d["votes"], d["kind"]
        for k in [k for k in state.items if isinstance(k, tuple) and k and k[0] == "ratings"]:
            del state.items[k]
        state.items[key] = True  # type: ignore[assignment]

    def _kind_rows(self, state: ScanState, kind: str) -> list[tuple[dict[str, Any], str]]:
        """The (item, search text) rows of one result kind, built once per scan.

        Every row of the checksum kinds carries ``id`` (its index in the unfiltered list) so that
        ``/api/scan/checksums`` can find the scan objects behind it."""
        if kind in state.items:
            return state.items[kind]
        result, root = state.result, state.root
        tags_mod = _optional_mod("tags") if kind in TAG_KINDS else None
        sources: list[Any] | None = None
        if kind == "matched":
            sources = list(result.matched)
            items = [self._matched_item(m, state, tags_mod) for m in sources]
        elif kind == "unmatched":
            sources = list(result.unmatched)
            items = [{"file": _entry_rel(e, root), "size": e.size, "crc": e.crc,
                      "reason": getattr(e, "reason", ""), "kind": getattr(e, "kind", "")}
                     for e in sources]
            bios = _mod("bios")
            for e, item in zip(sources, items):            # a BIOS / firmware file (told by its checksum) or an emulator's key file
                found = bios.lookup(e.size, e.crc or "", getattr(e, "sha1", "") or "")     # (a member of a zip: by its size and CRC-32)
                if found is not None:
                    item["bios"] = f"{found.system} ({found.name})"
                elif getattr(e, "member", None) is None and bios.is_key_file(e.path):
                    item["bios"] = "key file of an emulator"

        elif kind == "missing":
            sources = list(result.missing)
            items = [self._missing_item(r, tags_mod) for r in sources]
        elif kind == "games":
            items = self._game_items(state, tags_mod)
        elif kind == "unsupported":
            items = [{"file": _rel(p, root)} for p in result.unsupported]
        else:
            items = [{"file": _rel(p, root), "error": str(msg)} for p, msg in result.errors]
        if kind in CHECKSUM_KINDS:
            for i, item in enumerate(items):
                item["id"] = i
            if sources is not None:
                state.sources[kind] = sources
        state.items[kind] = _rows(items)
        return state.items[kind]

    # ------------------------------------------------------- checksums (Amendment 15)

    def scan_checksums(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """The checksum detail of ONE result row (``kind`` + the row's ``id``), from the scan in memory."""
        state = self._require_scan()
        kind = query.get("kind", "matched")
        if kind not in CHECKSUM_KINDS:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"No checksums for result kind: {kind}")
        rows = self._kind_rows(state, kind)
        idx = _int_arg(query.get("id"), -1, -1)
        if not 0 <= idx < len(rows):
            raise ApiError(HTTPStatus.NOT_FOUND, "No such result row")
        return self._checksums(state, kind, rows[idx][0])

    @staticmethod
    def _rom_hashes(rom: Any) -> dict[str, Any]:
        return {"name": getattr(rom, "name", ""), "size": getattr(rom, "size", None),
                "crc32": getattr(rom, "crc", "") or None, "md5": getattr(rom, "md5", "") or None,
                "sha1": getattr(rom, "sha1", "") or None}

    @staticmethod
    def _equal(dat: dict[str, Any], local: dict[str, Any] | None) -> dict[str, Any]:
        """Per hash: True / False when both sides know it, None when one of them is unknown."""
        out: dict[str, Any] = {}
        for key in HASH_KEYS:
            a, b = dat.get(key), (local or {}).get(key)
            out[key] = None if not a or not b else a == b
        return out

    def _match_index(self, state: ScanState) -> dict[str, Any]:
        if state.match_index is None:
            index: dict[str, Any] = {}
            for m in state.result.matched:
                index.setdefault(_entry_rel(m.entry, state.root), m)
                rel = getattr(m.entry, "rel", None)
                if isinstance(rel, str):
                    index.setdefault(rel, m)
            state.match_index = index
        return state.match_index

    def _local_file(self, state: ScanState, entry: Any, match: Any = None, dat_rom: Any = None) -> dict[str, Any]:
        """The hashes the scan holds for a local file (CRC32 + SHA-1; MD5 is not computed while scanning;
        an archive member has only the CRC32 stored in the archive)."""
        member = getattr(entry, "member", None)
        raw = {"crc32": getattr(entry, "crc", "") or None, "md5": None, "sha1": getattr(entry, "sha1", None) or None}
        out: dict[str, Any] = {"file": _entry_rel(entry, state.root), "size": getattr(entry, "size", None),
                               "archive": bool(member), "member": member or None, "raw": raw,
                               "via": "raw", "via_text": "", "normalised": None}
        via = (getattr(match, "matched_via", "raw") or "raw") if match is not None else "raw"
        if via != "raw":
            header = getattr(match, "header", 0) or 0
            order = getattr(match, "byte_order", "") or ""
            out["via"] = via
            out["via_text"] = (
                f"{header}-byte {'SNES copier' if header == 512 else 'iNES' if header == 16 else ''} header skipped before hashing"
                .replace("  ", " ") if via == "headerless"
                else f"{order + ' ' if order else ''}byte order swapped to the DAT's before hashing"
                if via == "byteswapped"
                else f"the disc image inside the .{getattr(match, 'container', '') or 'rvz'} file was rebuilt and hashed"
                if via == "container" else via)
            out["normalised"] = {"crc32": getattr(match, "alt_crc", "") or None, "md5": None,
                                 "sha1": getattr(match, "alt_sha1", "") or None,
                                 "size": getattr(dat_rom, "size", None)}
        return out

    def _file_payload(self, state: ScanState, match: Any) -> dict[str, Any]:
        """A matched local file (loose, archive member, or normalised) against its primary DAT rom."""
        primary = _primary(match, state.dat_names) or list(getattr(match, "roms", []) or [])
        rom = primary[0] if primary else None
        dat = self._rom_hashes(rom) if rom is not None else {}
        local = self._local_file(state, match.entry, match, rom)
        effective = local["normalised"] or local["raw"]
        local["equal"] = self._equal(dat, effective)
        out = {"kind": "file", "source": _source(state.platform), "dat_name": _rom_dat(rom) if rom is not None else "",
               "dat": [dat] if dat else [], "also_named": max(0, len(primary) - 1), "local": [local]}
        disks = self._disk_set_info(state, match)
        if disks:
            out["disks"] = disks
        return out

    def _set_roms(self, state: ScanState, dat_name: str, rom: Any = None, set_key: str = "") -> list[Any]:
        """Every DAT rom of a set (No-Intro alternates share a set name) - or just ``rom``."""
        for d in getattr(state.result, "dats", None) or []:
            if getattr(d, "name", "") == dat_name and hasattr(d, "sets") and set_key:
                roms = d.sets().get(set_key)
                if roms:
                    return list(roms)
        return [rom] if rom is not None else []

    def _disc_payload(self, state: ScanState, unit: Any, game: Any, entry: Any = None, matched: bool = True) -> dict[str, Any]:
        """Per-track DAT vs local hashes of a disc unit (CHD or raw set)."""
        dat_tracks = list(getattr(game, "tracks", []) or [])
        tracks = []
        for i, t in enumerate(getattr(unit, "tracks", []) or []):
            rom = dat_tracks[i] if i < len(dat_tracks) else None
            claimed = bool(t.get("claimed"))
            hashed = bool(t.get("sha1")) and not claimed
            local = {"crc32": t.get("crc32") if hashed else None, "md5": t.get("md5") if hashed else None,
                     "sha1": t.get("sha1") if (hashed or claimed) else None}
            dat = self._rom_hashes(rom) if rom is not None else {}
            tracks.append({"number": t.get("number"), "type": t.get("type") or "", "size": t.get("size"),
                           "dat": dat, "local": local,
                           "state": "hashed" if hashed else "header" if claimed else "length",
                           "equal": self._equal(dat, local) if matched else {k: None for k in HASH_KEYS}})
        if not tracks:                       # a game nobody has: just the DAT side
            for i, rom in enumerate(dat_tracks):
                tracks.append({"number": i + 1, "type": "", "size": rom.size, "dat": self._rom_hashes(rom),
                               "local": None, "state": "none", "equal": {k: None for k in HASH_KEYS}})
        out = {"kind": "disc", "source": _source(state.platform), "dat_name": getattr(game, "dat_name", ""),
               "game": getattr(game, "name", ""), "level": getattr(unit, "level", "") if unit is not None else "",
               "disc_kind": getattr(unit, "disc_kind", "") if unit is not None else "",
               "tracks": tracks, "local": []}
        if unit is not None and entry is not None:
            out["local"] = [{"file": _entry_rel(entry, state.root), "size": getattr(entry, "size", None),
                             "chd_sha1": getattr(unit, "sha1", "") or None, "kind": getattr(unit, "kind", "")}]
        return out

    def _unit_of(self, state: ScanState, entry: Any) -> Any:
        for u in getattr(state.result, "units", None) or []:
            if os.fspath(u.path) == os.fspath(entry.path):
                return u
        return None

    def _disk_set_info(self, state: ScanState, match: Any) -> dict[str, Any] | None:
        """The multi-disk set a matched disk belongs to: every disk with its DAT and local hashes."""
        if not getattr(state.platform, "m3u_dats", None):
            return None
        if state.disk_sets is None:
            table: dict[int, Any] = {}
            try:
                for ds in _mod("m3u").group_disk_sets(state.result, dats=tuple(state.platform.m3u_dats)):
                    for m in ds.disks.values():
                        table.setdefault(id(m), ds)
            except Exception:  # noqa: BLE001 - informative only
                table = {}
            state.disk_sets = table
        ds = state.disk_sets.get(id(match))
        if ds is None:
            return None
        disks = []
        for n in sorted(set(ds.disks) | set(ds.missing)):
            m = ds.disks.get(n)
            if m is None:
                disks.append({"number": n, "missing": True})
                continue
            rom = ds.roms.get(n)
            dat = self._rom_hashes(rom) if rom is not None else {}
            local = self._local_file(state, m.entry, m, rom)
            local["equal"] = self._equal(dat, local["normalised"] or local["raw"])
            disks.append({"number": n, "missing": False, "dat": dat, "local": local, "this": m is match})
        return {"name": ds.key, "total": ds.total, "complete": bool(ds.complete), "disks": disks}

    def _checksums(self, state: ScanState, kind: str, item: dict[str, Any]) -> dict[str, Any]:
        """DAT vs local checksums of one result row (see Amendment 15 for the payload shape)."""
        idx = item.get("id")
        src = state.sources.get(kind) or []
        obj = src[idx] if isinstance(idx, int) and 0 <= idx < len(src) else None
        label = _source(state.platform)
        if kind == "matched" and obj is not None:
            if hasattr(obj, "unit") and obj.unit is not None and getattr(obj.unit, "game", None) is not None:
                return self._disc_payload(state, obj.unit, obj.unit.game, obj.entry)
            return self._file_payload(state, obj)
        if kind == "unmatched" and obj is not None:
            unit = self._unit_of(state, obj) if getattr(state.result, "units", None) else None
            if unit is not None and getattr(unit, "tracks", None):
                out = self._disc_payload(state, unit, None, obj, matched=False)
                out["kind"] = "disc_unmatched"
                return out
            local = self._local_file(state, obj)
            local["equal"] = {k: None for k in HASH_KEYS}
            out = {"kind": "unmatched", "source": label, "dat": [], "local": [local]}
            bios = _mod("bios")                      # (no game, but perhaps a BIOS: or a dump TOSEC lists as a bad BIOS)
            found = bios.lookup(obj.size, obj.crc or "", getattr(obj, "sha1", "") or "")
            if found is not None:
                out["bios"] = f"{found.system} ({found.name})"

            return out
        if kind == "missing" and obj is not None:
            game = getattr(getattr(state.result, "index", None), "games", {}).get(getattr(obj, "game", "")) \
                if getattr(state.result, "index", None) is not None else None
            if game is not None:
                return self._disc_payload(state, None, game, matched=False)
            roms = self._set_roms(state, _rom_dat(obj), obj, getattr(obj, "set_name", "") or getattr(obj, "name", ""))
            return {"kind": "missing", "source": label, "dat_name": _rom_dat(obj),
                    "dat": [self._rom_hashes(r) for r in roms], "local": []}
        if kind == "games":
            return self._game_checksums(state, item)
        return {"kind": "none", "source": label, "dat": [], "local": []}

    def _game_checksums(self, state: ScanState, item: dict[str, Any]) -> dict[str, Any]:
        label = _source(state.platform)
        index = self._match_index(state)
        matches = []
        for rel in item.get("files") or []:
            m = index.get(rel)
            if m is not None and m not in matches:
                matches.append(m)
        disc_index = getattr(state.result, "index", None)
        if disc_index is not None and getattr(disc_index, "games", None) is not None:   # disc systems
            game = disc_index.games.get(item.get("name", ""))
            best = next((m for m in matches if getattr(m, "unit", None) is not None), None)
            if best is not None:
                return self._disc_payload(state, best.unit, best.unit.game, best.entry)
            return self._disc_payload(state, None, game, matched=False) if game is not None else \
                {"kind": "none", "source": label, "dat": [], "local": []}
        roms = self._set_roms(state, item.get("dat", ""), None, item.get("name", ""))
        if not roms:
            return {"kind": "none", "source": label, "dat": [], "local": []}
        dat_rows = [self._rom_hashes(r) for r in roms]
        locals_ = []
        for m in matches[:6]:
            prim = _primary(m, state.dat_names)
            rom = next((r for r in prim if _rom_key_of(r) == _rom_key_of(roms[0])), prim[0] if prim else roms[0])
            local = self._local_file(state, m.entry, m, rom)
            local["equal"] = self._equal(self._rom_hashes(rom), local["normalised"] or local["raw"])
            locals_.append(local)
        return {"kind": "file" if locals_ else "missing", "source": label, "dat_name": item.get("dat", ""),
                "dat": dat_rows, "also_named": 0, "local": locals_, "more_files": max(0, len(matches) - 6)}

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
                self._scans.pop(state.platform.name, None)
            res["rescan_error"] = (exc.message if isinstance(exc, ApiError) else str(exc)) or type(exc).__name__
        return res

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
                st = log.stat()
                mtime = st.st_mtime
                key = (str(log), st.st_size, st.st_mtime_ns)
                count = self._undo_counts.get(key)
                if count is None:          # counted once per log version: this is asked every time a tab opens
                    counter = getattr(_mod("organiser"), "count_undo_log", None)
                    reader = getattr(_mod("organiser"), "read_undo_log", None)
                    if counter is not None:
                        count = counter(log)
                    elif reader is not None:
                        info = reader(log)
                        count = len(info["moves"]) + len(info.get("created_files") or ())
                    else:
                        count = _undo_log_count(json.loads(log.read_text(encoding="utf-8")))
                    if len(self._undo_counts) > 64:
                        self._undo_counts.clear()
                    self._undo_counts[key] = count
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
            back = self._undo_library_sweep(str(log))
            res = dict(_call(_mod("organiser").undo, log, root=state.root))
            res["action"] = "undo"
            conv = self._undo_linked_conversion(str(log), state.root) if kind == "library" else None
            if conv:
                res["converted_back"] = conv.get("restored")
            if back:
                res["aside_restored"] = back["restored"]
            saves = self._ra_undo_follow(str(log))
            if saves:
                res["saves"] = saves
            return self._rescan_into(job, state, res)

        return {"job": self.jobs.start(kind, work, cancellable=False, platform=state.platform.name).to_dict()}

    @staticmethod
    def _library_flags(body: dict[str, Any]) -> tuple[bool, bool]:
        # a stale client may still send ``move_unmatched``: unmatched files are always moved, it is ignored
        return (_bool_arg(body.get("savedisk")),
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
    def _borrow_summary(plan: Any) -> dict[str, Any]:
        """``{"sets", "disks", "by_difference"}``: playlists completed with disks of other editions."""
        sel = getattr(plan, "selection", None)
        func = getattr(sel, "borrow_summary", None)
        out = func() if callable(func) else {}
        return {"sets": int(out.get("sets", 0)), "disks": int(out.get("disks", 0)),
                "by_difference": dict(out.get("by_difference", {}))}

    @staticmethod
    def _vanish_summary(plan: Any) -> dict[str, Any]:
        func = getattr(plan, "vanish_summary", None)
        out = func() if callable(func) else {}
        return {"titles": int(out.get("titles", 0)), "by_reason": dict(out.get("by_reason", {})),
                "by_code": dict(out.get("by_code", {}))}

    def _plan_id(self, state: ScanState, key: tuple[bool, bool]) -> str:
        """Identity of the Build library plan for this scan + these rules + these options (``plan_id``)."""
        profile = self._profile(state.platform)
        sig = _mod("totals").profile_signature(profile)        # (the totals do not look at saves)
        if self._saves_report(state) is not None:             # a plan with saves in it changes when their choices change
            sig += "-" + _mod("totals").signature_text([profile.saved_games, *map(str, profile.saved_overrides)])[:6]
        if profile.rating_active:                     # a rebuilt ratings index changes the plan
            sig += "-" + _mod("totals").signature_text([self._ratings_sig()])[:6]
        return f"{state.serial}.{sig}.{int(key[0])}{int(key[1])}"

    def _totals(self) -> Any:
        """The background library-totals worker (created on first use)."""
        with self._lock:
            if self._totals_mgr is None:
                totals = _mod("totals")

                def persist(name: str, record: dict[str, Any]) -> None:
                    def mutate(cfg: dict[str, Any]) -> None:
                        table = cfg.get("library_totals") if isinstance(cfg.get("library_totals"), dict) else {}
                        table[name] = record
                        cfg["library_totals"] = table
                    _mod("paths").update_config(mutate)

                self._totals_mgr = totals.TotalsManager(
                    lambda platform: list(self._platform_dats(platform)[0]), persist, self._rating_lookup)
            return self._totals_mgr

    def library_totals(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """``GET /api/library/totals?platform=``: "with your library rules: have N of M games" (Amendment 17).

        Never blocks: the whole-DAT selection runs on a background worker; ``calculating`` is true until it is
        ready (``stale`` = the numbers shown are of the previous rules / scan)."""
        platform = self._resolve_platform(query.get("platform"))
        totals = _mod("totals")
        cfg = self._config()
        profile = self._profile(platform, cfg)
        infos = self._locate_dats(platform)
        if not infos:
            return {"platform": platform.name, "available": False, "calculating": False, "stale": False, "scanned": False,
                    "error": "", "target_games": None, "have_games": None, "missing_games": None, "percent": None,
                    "not_preferred": None, "owned_but_excluded": None, "owned_incomplete": None, "computed_at": None,
                    "profile_signature": totals.profile_signature(profile), "by_dat": {}}
        pending = self._ratings_pending(platform, profile)
        if pending:     # a rating filter without its data: no number would be right (the download was asked for)
            return {"platform": platform.name, "available": True, "calculating": False, "stale": False, "scanned": False,
                    "error": "", "target_games": None, "have_games": None, "missing_games": None, "percent": None,
                    "not_preferred": None, "owned_but_excluded": None, "owned_incomplete": None, "computed_at": None,
                    "profile_signature": totals.profile_signature(profile), "by_dat": {}, "ratings_pending": True,
                    "rating_coverage": None, "rating": None}
        dats_sig = totals.signature_text(tuple(self._dats_signature(platform)) + (self._ratings_sig(),))
        with self._lock:
            state = self._scan
        if state is not None and state.platform.name != platform.name:
            state = None
        table = cfg.get("library_totals") if isinstance(cfg.get("library_totals"), dict) else {}
        persisted = table.get(platform.name) if isinstance(table.get(platform.name), dict) else None
        out = self._totals().request(platform, profile, dats_sig, state, persisted)
        out["available"] = True
        out["ratings_pending"] = False
        out["rank_scope"] = profile.rank_scope if profile.top_n is not None else None
        return out

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
        rows = _sort_rows(rows, _str_arg(body.get("sort")), lambda i: i["title"])
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
        if _bool_arg(body.get("refresh")):       # "Recalculate preview": never reuse the cached plan
            self._drop_plans(state)
        plan = self._library_plan(state, *key)
        rows = self._library_rows(state, key)
        file_rows = [r for r in rows if r[0].get("item") == "file"]
        reasons = self._reason_counts(plan, rows)
        # rows per category, whether they move or already sit in place (``reasons`` counts only what this build changes)
        categories: dict[str, int] = {}
        for item, _t in rows:
            categories[item["category"]] = categories.get(item["category"], 0) + 1
        status, reason, dest = _str_arg(body.get("status")), _str_arg(body.get("reason")), _str_arg(body.get("dest"))
        saves_filter = _str_arg(body.get("saves"))        # "" | "with" (games with saves) | "affected" (saves this build changes)
        if saves_filter and saves_filter not in ("with", "affected"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "saves must be with or affected")
        saves_rows = {"with": 0, "affected": 0}
        for r in rows:
            sv = r[0].get("saves")
            if sv:
                saves_rows["with"] += 1
                if sv.get("effect"):
                    saves_rows["affected"] += 1
        if saves_filter:
            rows = [r for r in rows if r[0].get("saves") and (saves_filter == "with" or r[0]["saves"].get("effect"))]
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
        self._annotate_library(state, rows)
        rows = _sort_rows(rows, _str_arg(body.get("sort")),
                          lambda i: i.get("to_name") or Path(i.get("path") or "").name)
        if _str_arg(body.get("sort")) in ("saves_asc", "saves_desc"):
            rows = self._sort_saves(rows, _str_arg(body.get("sort")).endswith("desc"), lambda i: (i.get("saves") or {}).get("total", 0))
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
            plan_id=self._plan_id(state, key), files=len(ops),
            root=str(state.root), layout=state.layout, profile=self._profile_json(plan.profile, state.platform.name),
            counts=_status_counts(ops), reasons=reasons, categories=categories, by_dest=by_dest,
            playlists={"write": writes, "ok": statuses.count("ok"),
                       "remove": sum(1 for op in ops if op.status == "delete"),
                       "conflict": statuses.count("conflict")},
            incomplete_sets=incomplete,
            borrowed=self._borrow_summary(plan),
            incomplete_total=len(getattr(plan.selection, "incomplete", ()) or ()),
            exclusions=self._exclusion_counts(plan), vanish=self._vanish_summary(plan),
            rating=dict(getattr(plan.selection, "rating", None) or {}),
            warnings=[w["text"] for w, _ in state.items[wkey]],
            actionable=actionable, empty=actionable == 0,
            missing_dats=list(state.missing_dats), unmatched_dir=UNMATCHED_DIR,
            reserved_dirs=list(RESERVED_DIRS))
        page["has_year"] = any(r[0].get("year") is not None for r in file_rows[:2000])
        export_asked = _str_arg(body.get("export_to"))
        try:
            info = self._saves_plan_info(
                plan, state.root,
                self._library_aside_dir(state, body) if _str_arg(body.get("aside_to")) and not export_asked else None,
                bool(export_asked))
            if info is not None:                                    # (no field at all without a RetroArch config)
                page["saves"] = {**info, "rows_with": saves_rows["with"], "rows_affected": saves_rows["affected"]}
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - the saves line is a courtesy of the preview
            traceback.print_exc()
            page["saves"] = {"found": False, "error": str(exc)}
        if self._convert_on_build(state.platform, self._config()):
            ops = self._convert_plan(state, self._latest_arg(state, {}))
            page["convert"] = {"count": sum(1 for op in ops if op.status == "convert"), "kind": "chd" if self._is_dc(state) else "clean"}
        aside_raw = _str_arg(body.get("aside_to"))
        if aside_raw and not _str_arg(body.get("export_to")):
            aside = self._library_aside_dir(state, body)
            existing = len(_mod("sortroot").plan_sweep({state.platform.name: state.root}, aside)) if aside else 0
            r = page["reasons"]
            page["aside"] = {"path": str(aside), "existing": existing,
                             "coming": sum(r.get(k, 0) for k in ("excluded", "superseded", "incomplete", "duplicates", "unmatched"))}
        export_to = _str_arg(body.get("export_to"))
        if export_to:
            page["export"] = self._export_summary(state, plan, export_to, _mod("libexport").normalize_mode(_str_arg(body.get("export_mode"))),
                                                  _bool_arg(body.get("export_sidecars")), _bool_arg(body.get("export_sync")))
            page["actionable"] = page["export"].get("pending", 0)
            page["empty"] = page["actionable"] == 0
        if _bool_arg(body.get("checksums")):     # only this page's rows, from the scan in memory
            for it in page["items"]:
                if it.get("cs_kind"):
                    src = self._kind_rows(state, it["cs_kind"])
                    it["checksums"] = self._checksums(state, it["cs_kind"], src[it["cs_id"]][0])
        return page

    @staticmethod
    def _sort_saves(rows: list[tuple[dict[str, Any], str]], desc: bool, total: Callable[[dict[str, Any]], int]) -> list[tuple[dict[str, Any], str]]:
        """Rows sorted by their number of saves; the rows without any are last whichever way it runs."""
        have = [r for r in rows if total(r[0])]
        lack = [r for r in rows if not total(r[0])]
        have.sort(key=lambda r: -total(r[0]) if desc else total(r[0]))
        return have + lack

    def _annotate_library(self, state: ScanState, rows: list[tuple[dict[str, Any], str]]) -> None:
        """Library rows: ``rating`` / ``votes`` of the game the file belongs to, and ``cs_kind`` / ``cs_id`` - the scan
        result row (matched, unmatched or game) whose checksums ``/api/scan/checksums`` shows for it."""
        games = self._kind_rows(state, "games")
        try:
            self._annotate_ratings(state, games)
        except ImportError:                      # no ratings module: the rows simply have no rating
            for item, _t in games:
                item.setdefault("rating", None)
                item.setdefault("votes", None)
        ikey = ("libref",)
        if ikey not in state.items:
            self._kind_rows(state, "matched")
            self._kind_rows(state, "unmatched")
            by_file: dict[str, tuple[str, int]] = {}
            for i, m in enumerate(state.sources.get("matched") or []):
                by_file.setdefault(_entry_rel(m.entry, state.root).split("::", 1)[0], ("matched", i))
            for i, e in enumerate(state.sources.get("unmatched") or []):
                by_file.setdefault(_entry_rel(e, state.root).split("::", 1)[0], ("unmatched", i))
            game_of_file: dict[str, dict[str, Any]] = {}
            game_of_name: dict[str, dict[str, Any]] = {}
            for item, _t in games:
                game_of_name.setdefault(item["name"], item)
                for rel in item.get("files") or ():
                    game_of_file.setdefault(rel.split("::", 1)[0], item)
            state.items[ikey] = (by_file, game_of_file, game_of_name)  # type: ignore[assignment]
        by_file, game_of_file, game_of_name = state.items[ikey]  # type: ignore[misc]
        for item, _t in rows:
            if item.get("item") != "file":
                continue
            src = item["from"].split("::", 1)[0]
            game = game_of_file.get(src) or game_of_name.get(item.get("game") or "")
            item["rating"], item["votes"] = (game.get("rating"), game.get("votes")) if game else (None, None)
            item["year"], item["size"] = (game.get("year"), game.get("size")) if game else (None, None)
            item["game_ref"] = {"dat": game["dat"], "game": game["name"]} if game else None
            item["tags"] = game.get("tags") if game else None
            ref = by_file.get(src)
            if ref is None and game is not None:
                ref = ("games", game["id"])
            item["cs_kind"], item["cs_id"] = ref if ref else (None, None)

    def _export_plan(self, state: ScanState, plan: Any, export_to: str, mode: str, sidecars: bool,
                     sync: bool = False) -> Any:
        libexport = _mod("libexport")
        try:
            return libexport.plan_export(plan, state.root, Path(export_to), mode, sidecars, sync)
        except libexport.ExportError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc), "bad_destination")

    def _export_summary(self, state: ScanState, plan: Any, export_to: str, mode: str, sidecars: bool,
                        sync: bool = False) -> dict[str, Any]:
        ep = self._export_plan(state, plan, export_to, mode, sidecars, sync)
        problems = [{"rel": t.rel, "reason": t.reason, "src": str(t.src)} for t in ep.items if t.action == "conflict"]
        try:
            free = shutil.disk_usage(_existing_parent(ep.dest)).free
        except OSError:
            free = None
        need = ep.bytes_to_copy()
        notes = list(ep.notes)
        if free is not None and need > free:
            notes.append(f"Not enough space on the destination: {need} bytes needed, {free} free.")
        return {"dest": str(ep.dest), "mode": ep.mode, "counts": ep.counts(), "pending": ep.pending(),
                "bytes_copy": need, "bytes_linked": ep.bytes_linked(), "free": free, "enough_space": free is None or need <= free,
                "playlists": len([p for p in ep.playlists if p.action == "write"]),
                "conflicts": problems[:200], "notes": notes, "runs": _mod("libexport").list_runs(ep.dest),
                "sync": sync, "mass_removal": ep.mass_removal(),
                "removals": [{"rel": r.rel, "reason": r.reason, "skip": r.skip} for r in ep.removals[:200]]}

    @staticmethod
    def _archive_pass(folder: Path, plan: Any, aside: Path) -> tuple[list[Any], list[Any]]:
        """What a build in place does, in one move per file: the ``RenameOp`` list for ``organiser.apply_renames`` (inside the system's
        folder) and the moves of what the rules set aside (``_excluded``, ``_superseded`` ... ``_unmatched``) from where it lies straight
        to ``<archive>/<system folder name>/...`` (outside the folder, so ``sortroot`` journals them)."""
        organiser, discsys, sortroot = _mod("organiser"), _mod("discsys"), _mod("sortroot")
        # (compared as text: on Windows two Path objects that differ only in case are equal, and a rename that only changes
        # the case of a name - "ALPHA.SFC" to "Alpha.sfc" - is a move that has to be made)
        ops = [op for op in discsys.file_ops(plan.ops) if op.status != "move" or str(op.src) != str(op.dst)]
        arch: list[Any] = []
        names = sortroot.Names()
        keep: list[Any] = []
        for op in ops:
            top = ""
            if op.status == "move":
                try:
                    rel = op.dst.relative_to(folder)
                    top = rel.parts[0] if rel.parts else ""
                except ValueError:
                    pass
            if top in sortroot.ASIDE_FOLDERS:
                if top == sortroot.UNMATCHED:                           # everything lies under the system's own folder name
                    try:
                        sub = op.src.relative_to(folder)
                    except ValueError:
                        sub = Path(op.src.name)
                    new = aside / folder.name / top / sub
                else:
                    new = aside / folder.name / rel
                size = 0
                try:
                    size = op.src.stat().st_size
                except OSError:
                    pass
                arch.append(sortroot.SMove(op.src, names.free(new), top, size=size))
            else:
                keep.append(op)
        return keep, arch

    def _library_aside_dir(self, state: ScanState, body: dict[str, Any]) -> Path | None:
        """The folder a build in place moves what it sets aside to (``aside_to``): checked not to lie in this folder."""
        raw = _str_arg(body.get("aside_to"))
        if not raw:
            return None
        aside = Path(os.path.abspath(os.path.expanduser(raw)))
        problem = _mod("sortroot").check_folders(state.root, aside)
        if problem:
            raise ApiError(HTTPStatus.BAD_REQUEST, problem.replace("destination", "archive folder").replace("ROM root", "ROM folder"),
                           "bad_aside")
        return aside

    def library_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        key = self._library_flags(body)
        export_to = _str_arg(body.get("export_to"))
        if export_to:
            return self._library_export_apply(state, key, body, export_to)
        sent = _str_arg(body.get("plan_id"))
        if sent and sent != self._plan_id(state, key):
            raise ApiError(HTTPStatus.CONFLICT, "The rules or the folder changed since that preview - "
                           "recalculate the preview and check it again", "stale_plan")
        self._library_plan(state, *key)  # errors (e.g. module missing) before starting a job
        aside = self._library_aside_dir(state, body)

        def work(job: Job) -> Any:
            st = state
            conv_res: dict[str, Any] | None = None
            if self._convert_on_build(st.platform, self._config()):      # raw discs / odd dumps are converted first
                latest = self._latest_arg(st, {})
                if any(op.status == "convert" for op in self._convert_plan(st, latest)):
                    conv_res = self._apply_conversions(job, st, latest)
                    self._rescan_into(job, st, conv_res)
                    self._carry_temp(st, conv_res)
                    st = self._require_scan()
                    if job.cancel.is_set():
                        return conv_res
            plan = self._library_plan(st, *key)
            todo = sum(1 for op in plan.ops if op.status in ACTIONABLE) + sum(
                1 for p in plan.playlists if p.status == "write")
            job.report(0, todo, "Building library...")
            sortroot = _mod("sortroot")
            arch_out: dict[str, Any] | None = None
            if aside is not None:
                # what the rules archive goes from where it is straight to the archive folder (not via _excluded/ ... first)
                ops, arch = self._archive_pass(st.root, plan, aside)
                if arch:
                    arch_out = sortroot.apply_moves(arch, _mod("paths").data_dir() / "collection-undo", "libsweep",
                                                    keep=[st.root, aside], cancel=job.cancel)
                res = dict(_mod("organiser").apply_renames(ops, st.root, progress=job.report, cancel=job.cancel,
                                                           playlists=list(plan.playlists)))
                if arch_out and arch_out.get("journal") and not res.get("undo_log"):
                    # (only the archive moves changed anything: the undo log is the handle "Undo" takes, with no steps of its own)
                    res["undo_log"] = str(_mod("organiser").write_marker_log(st.root))
                if arch_out and arch_out.get("journal") and res.get("undo_log"):      # one undo takes both back
                    jp = Path(arch_out["journal"])
                    data = json.loads(jp.read_text(encoding="utf-8"))
                    data["library_log"] = res["undo_log"]
                    sortroot._write_json(jp, data)
            else:
                apply = _mod("discsys").apply_plan if self._is_dc(st) else _mod("organiser").apply_renames
                res = dict(_call(apply, plan.ops, st.root, progress=job.report,
                                 cancel=job.cancel, playlists=list(plan.playlists)))
            res["action"] = "library"
            if conv_res is not None:
                res["converted"] = {"count": len(conv_res.get("converted") or []) if isinstance(conv_res.get("converted"), list) else conv_res.get("converted", 0),
                                    "failed": list(conv_res.get("failed") or [])[:20]}
                if conv_res.get("undo_log") and res.get("undo_log"):    # one undo reverts both
                    self._link_undo(res["undo_log"], conv_res["undo_log"])
                elif conv_res.get("undo_log"):                           # the build itself changed nothing: undo the conversion
                    res["undo_log"] = conv_res["undo_log"]
            saves_moves, _saves_info = self._saves_moves(plan, st.root, aside)
            if saves_moves:             # before the saves follow renames: the saves of an archived game go with it, under its old name
                res["saves_archived"] = self._apply_saves_moves(saves_moves, res.get("undo_log", ""))
            moved = self._moved_pairs(plan.ops)
            if moved:
                follow = self._ra_follow(moved, "move", {"library_log": res.get("undo_log", "")}, plan)
                if follow:
                    res["saves"] = follow
            if aside is not None and not job.cancel.is_set():       # the tidy-up: what was archived leaves this folder
                sweep = sortroot.plan_sweep({st.platform.name: st.root}, aside)       # what earlier builds left in _excluded/ ...
                out = {"moved": 0, "failed": []}
                if sweep:
                    out = sortroot.apply_moves(sweep, _mod("paths").data_dir() / "collection-undo", "libsweep",
                                               keep=[state.root, aside], progress=_staged(job, 0, 1, "moving the archived files out"),
                                               extra={"library_log": res.get("undo_log") or ""})
                if sweep or arch_out:
                    res["aside"] = {"path": str(aside), "moved": out["moved"] + (arch_out or {}).get("moved", 0),
                                    "failed": (out["failed"] + (arch_out or {}).get("failed", []))[:20]}
            return self._rescan_into(job, st, res)

        return {"job": self.jobs.start("library", work, cancellable=False, platform=state.platform.name).to_dict()}

    @staticmethod
    def _moved_pairs(ops: Any) -> list[tuple[Any, Any]]:
        """``(old, new)`` of every file a build actually moved (the new place exists, the old one does not)."""
        pairs: list[tuple[Any, Any]] = []
        for op in ops:
            if getattr(op, "status", "") not in ("move", "rename"):
                continue
            for src, dst in (getattr(op, "moves", None) or [(op.src, op.dst)]):
                if os.path.lexists(dst) and not os.path.lexists(src):
                    pairs.append((src, dst))
        return pairs

    def _library_export_apply(self, state: ScanState, key: Any, body: dict[str, Any], export_to: str) -> dict[str, Any]:
        sent = _str_arg(body.get("plan_id"))
        if sent and sent != self._plan_id(state, key):
            raise ApiError(HTTPStatus.CONFLICT, "The rules or the folder changed since that preview - "
                           "recalculate the preview and check it again", "stale_plan")
        mode = _mod("libexport").normalize_mode(_str_arg(body.get("export_mode")))
        sidecars = _bool_arg(body.get("export_sidecars"))
        sync, allow_mass = _bool_arg(body.get("export_sync")), _bool_arg(body.get("allow_mass_removal"))
        self._export_plan(state, self._library_plan(state, *key), export_to, mode, sidecars, sync)   # errors before the job

        def work(job: Job) -> Any:
            libexport = _mod("libexport")
            ep = self._export_plan(state, self._library_plan(state, *key), export_to, mode, sidecars, sync)
            if ep.bytes_to_copy() and shutil.disk_usage(_existing_parent(ep.dest)).free < ep.bytes_to_copy():
                raise ApiError(HTTPStatus.CONFLICT, "Not enough free space on the destination for this build.", "no_space")
            job.report(0, ep.pending(), "Building library in " + str(ep.dest))
            try:
                res = libexport.apply_export(ep, progress=lambda done, total, name: job.report(done, total, name),
                                             cancel=job.cancel, allow_mass=allow_mass)
            except libexport.ExportError as exc:
                raise ApiError(HTTPStatus.CONFLICT, str(exc), "mass_removal")
            res["action"] = "library_export"
            res["dest"] = str(ep.dest)
            follow = self._ra_follow([(t.src, ep.dest / t.rel) for t in ep.items if t.moves_data], "copy",
                                     {"collection": str(ep.dest)}, self._library_plan(state, *key))
            if follow:
                res["saves"] = follow
            return res

        return {"job": self.jobs.start("library", work, platform=state.platform.name).to_dict()}

    def library_export_settings(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/library/export/settings``: remember where the library is built (``enabled, dest, mode, sidecars``)."""
        mode = _mod("libexport").normalize_mode(_str_arg(body.get("mode")))
        value = {"enabled": _bool_arg(body.get("enabled")), "dest": _str_arg(body.get("dest")), "mode": mode,
                 "sidecars": _bool_arg(body.get("sidecars")), "sync": _bool_arg(body.get("sync")) and mode != "move",
                 "aside": _bool_arg(body.get("aside"), True)}
        self._config_update(library_export=value)
        return value

    # ---------------------------------------------------------------- RetroArch (v0.2): saves and config
    def _ra_state(self) -> tuple[Any, list[Any], dict[str, Any]]:
        """``(retroarch module, installs, saved settings)``: the detected installs plus the custom ones the user added."""
        ra = _mod("retroarch")
        cfg = self._config()
        saved = cfg.get("retroarch") if isinstance(cfg.get("retroarch"), dict) else {}
        custom = [c for c in saved.get("custom", []) if isinstance(c, str)]
        return ra, ra.detect_installs(custom=custom), saved

    def _ra_selected(self, installs: list[Any], saved: dict[str, Any]) -> Any:
        want = saved.get("selected")
        for inst in installs:
            if str(inst.cfg) == want:
                return inst
        if os.name == "nt" and isinstance(want, str) and want:      # Windows: the same file in another spelling
            for inst in installs:
                if os.path.normcase(os.path.normpath(str(inst.cfg))) == os.path.normcase(os.path.normpath(want)):
                    return inst
        return installs[0] if installs else None

    def _ra_dirs(self, saved: dict[str, Any]) -> tuple[Path, Path]:
        base = _mod("paths").data_dir()
        return base / "retroarch-undo", Path(saved.get("backup_dir") or (base / "save-backups"))

    def _ra_info(self) -> dict[str, Any]:
        ra, installs, saved = self._ra_state()
        sel = self._ra_selected(installs, saved)
        journal_dir, backup_dir = self._ra_dirs(saved)
        out: dict[str, Any] = {"installs": [i.to_dict() for i in installs], "selected": str(sel.cfg) if sel else "",
                               "custom": list(saved.get("custom", [])), "running": ra.is_running(),
                               "backup": {"enabled": saved.get("backup", True) is not False, "dir": str(backup_dir)},
                               "follow": saved.get("follow", True) is not False,
                               "undo": ra.list_undo(journal_dir), "settings": None, "overrides": []}
        if sel is not None:
            out["settings"] = ra.settings_of(sel)
            out["overrides"] = ra.override_warnings(sel)
        return out

    def retroarch_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        return self._ra_info()

    def retroarch_select(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/retroarch/select {cfg}`` picks a detected install; ``{custom}`` adds a retroarch.cfg (or its folder)."""
        ra, installs, saved = self._ra_state()
        changes: dict[str, Any] = {}
        custom = _str_arg(body.get("custom"))
        if custom:
            probe = ra.detect_installs(platform="none", custom=[custom])
            if not probe:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"No retroarch.cfg found in {custom}")
            changes["custom"] = list(dict.fromkeys([*saved.get("custom", []), custom]))
            changes["selected"] = str(probe[0].cfg)
        elif _str_arg(body.get("cfg")):
            if _str_arg(body.get("cfg")) not in {str(i.cfg) for i in installs}:
                raise ApiError(HTTPStatus.BAD_REQUEST, "That RetroArch is not in the list")
            changes["selected"] = _str_arg(body.get("cfg"))
        if "backup" in body:
            changes["backup"] = _bool_arg(body.get("backup"))
        if "backup_dir" in body:
            changes["backup_dir"] = _str_arg(body.get("backup_dir"))
        self._config_update(retroarch={**saved, **changes})
        self._saves_forget_all()
        return self._ra_info()

    def _ra_relocation(self, body: dict[str, Any]) -> tuple[Any, Any, Any]:
        ra, installs, saved = self._ra_state()
        sel = self._ra_selected(installs, saved)
        if sel is None:
            raise ApiError(HTTPStatus.CONFLICT, "No RetroArch was found. Choose its retroarch.cfg first.", "no_retroarch")
        save_dir = _str_arg(body.get("save_dir"))
        state_dir = _str_arg(body.get("state_dir")) or save_dir
        if not save_dir:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Choose the folder for save files.")
        paths = []
        for raw in (save_dir, state_dir):
            try:
                p = Path(os.path.abspath(os.path.expanduser(raw)))
                is_file, anchored = p.is_file(), _existing_parent(p).exists()
            except (OSError, ValueError):
                raise ApiError(HTTPStatus.BAD_REQUEST, f"Not a usable folder: {raw}") from None
            for bad in (sel.base,):
                if p == bad:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Choose a folder for the saves, not RetroArch's own folder.")
            if is_file:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"That is a file, not a folder: {p}")
            if not anchored:                                  # Windows: a drive that is not there (on POSIX "/" always is)
                raise ApiError(HTTPStatus.BAD_REQUEST, f"The drive of that folder does not exist: {p}")
            paths.append(p)
        try:
            rel = ra.plan_relocation(sel, paths[0], paths[1], _bool_arg(body.get("sort_saves")),
                                     _bool_arg(body.get("sort_states")))
        except ValueError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc), "saves_in_content_dir") from None
        return ra, sel, rel

    def retroarch_plan(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        ra, sel, rel = self._ra_relocation(body)
        _j, backup_dir = self._ra_dirs(self._ra_state()[2])
        try:
            free = shutil.disk_usage(_existing_parent(backup_dir)).free
        except OSError:
            free = None
        shown = [m for m in rel.moves if m.status != "ok"][:300]
        return {"counts": rel.counts(), "bytes": rel.bytes_to_move(), "old": rel.old, "new": rel.new, "notes": rel.notes,
                "running": ra.is_running(), "free": free, "overrides": ra.override_warnings(sel),
                "cfg_changes": {k: (v if isinstance(v, bool) else str(v)) for k, v in rel.changes.items()},
                "items": [{"from": str(m.src), "to": str(m.dst), "kind": m.kind, "status": m.status, "note": m.note}
                          for m in shown],
                "empty": rel.counts()["move"] + rel.counts()["needs_core"] == 0 and not self._ra_cfg_differs(sel, rel)}

    @staticmethod
    def _ra_cfg_differs(sel: Any, rel: Any) -> bool:
        cur = _mod("retroarch").read_cfg(sel.cfg)
        for k, v in rel.changes.items():
            have = cur.get(k, "")
            want = "true" if v is True else "false" if v is False else str(v)
            if have != want:
                if os.name == "nt" and k.endswith("_directory") and have and want:
                    # Windows: the same folder in another spelling (case, slashes, ":\saves" against the full path)
                    ra = _mod("retroarch")
                    a, b = ra.resolve(have, sel), ra.resolve(want, sel)
                    if a is not None and b is not None and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b)):
                        continue
                return True
        return False

    def retroarch_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        ra, sel, rel = self._ra_relocation(body)
        if ra.is_running():
            raise ApiError(HTTPStatus.CONFLICT, "RetroArch is running. Close it first: it rewrites its config when it closes.",
                           "retroarch_running")
        saved = self._ra_state()[2]
        journal_dir, backup_dir = self._ra_dirs(saved)
        want_backup = _bool_arg(body.get("backup"), saved.get("backup", True) is not False)
        zip_path = backup_dir / f"saves-{time.strftime('%Y%m%d-%H%M%S')}.zip" if want_backup else None

        def work(job: Job) -> Any:
            job.report(0, 0, "Moving saves...")
            res = ra.apply_relocation(sel, rel, journal_dir, zip_path, progress=lambda d, t, m: job.report(d, t, m))
            res["action"] = "retroarch"
            self._saves_forget_all()            # the saves are somewhere else now: every scan's save report is read again
            return res

        return {"job": self.jobs.start("retroarch", work, cancellable=False).to_dict()}

    def retroarch_undo(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        ra, installs, saved = self._ra_state()
        journal_dir, _b = self._ra_dirs(saved)
        journal = _str_arg(body.get("journal"))
        known = {x["journal"] for x in ra.list_undo(journal_dir)}
        if journal not in known:
            raise ApiError(HTTPStatus.BAD_REQUEST, "That change cannot be undone (it was already undone, or is unknown).")
        try:
            res = ra.undo_relocation(Path(journal))
            self._saves_forget_all()
            return res
        except RuntimeError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc), "retroarch_running") from None

    @staticmethod
    def _report_files(report: Any) -> list[tuple[Path, Path]]:
        """``[(file, save root)]`` of every file of a save report that is still there."""
        return [(f, root) for st in report.sets.values() if st.renames for f, root, _k, _z in st.members if os.path.lexists(f)]

    def _ra_follow(self, pairs_from: list[tuple[Any, Any]], mode: str, extra: dict[str, Any], plan: Any) -> dict[str, Any] | None:
        """Saves and states follow ROMs that got new names (``pairs_from``: ``(old path, new path)``). ``mode`` ``move`` for
        a build in place, ``copy`` for a build into another folder. None when no RetroArch config is known (``plan`` has no
        save report) or the option is off. The games the plan sets aside keep their saves where they are (or archive them)."""
        sp = self._saves_plan(plan)
        if sp is None:
            return None
        try:
            ra, installs, saved = self._ra_state()
            sel = self._ra_selected(installs, saved)
            if sel is None or saved.get("follow", True) is False:
                return None
            pairs = [(a, b) for a, b in ra.pairs_from_moves(pairs_from) if ra.fold_name(a) not in sp["gone"]]
            pairs += self._playlist_pairs(plan, sp["report"], sp["gone"])      # the saves of a multi-disc game follow its playlist
            if not pairs:
                return None
            ops = ra.plan_follow(sel, pairs, mode, files=self._report_files(sp["report"]))
            res = ra.apply_follow(ops, self._ra_dirs(saved)[0], extra, sel)
            res["conflicts"] = sum(1 for o in ops if o.status == "conflict")
            return res
        except Exception as exc:  # noqa: BLE001 - saves are a courtesy of the build: never fail it
            traceback.print_exc()
            return {"error": str(exc), "followed": 0, "copied": 0, "failed": []}

    # ---- saves are part of the build (Amendment 31)
    @staticmethod
    def _plan_stems(plan: Any) -> tuple[dict[str, str], set[str]]:
        """``({content name: reason code}, {content names})``: the games (as RetroArch names their saves, see
        ``retroarch.fold_name``) this plan sets aside, and those of the files that stay. A name that a file in its place
        also has is not set aside: its saves belong to that file too."""
        ra = _mod("retroarch")
        gone: dict[str, str] = {}
        stay: set[str] = set()
        for op in plan.ops:
            if getattr(op, "status", "") == "delete" or getattr(op, "kind", "") == "m3u":
                continue
            stem = ra.fold_name(App._op_stem(op))
            if op.code:
                if op.status in ("move", "rename"):
                    gone[stem] = op.code
            elif not op.unmatched:
                stay.add(stem)
        for stem in stay:
            gone.pop(stem, None)
        return gone, stay

    @staticmethod
    def _op_stem(op: Any) -> str:
        """The content name RetroArch gives the saves of the file(s) of an op: the name of the game's image / ROM file."""
        unit = getattr(op, "unit", None)
        return Path(getattr(unit, "path", None) or op.src).stem

    @staticmethod
    def _planned_pairs(plan: Any) -> list[tuple[str, str]]:
        """``(old content name, new content name)`` of every file the plan renames (what ``_moved_pairs`` finds afterwards)."""
        moves: list[tuple[Any, Any]] = []
        for op in plan.ops:
            if getattr(op, "status", "") not in ("move", "rename") or getattr(op, "kind", "") == "m3u":
                continue
            moves.extend(getattr(op, "moves", None) or [(op.src, op.dst)])
        return _mod("retroarch").pairs_from_moves(moves)

    @staticmethod
    def _playlist_pairs(plan: Any, report: Any, gone: Any) -> list[tuple[str, str]]:
        """``(old playlist name, new playlist name)``: RetroArch names the saves of a multi-disc game after its ``.m3u``. A playlist
        you have saves under is replaced by the plan's playlist for the same discs (as renamed by the plan); when the name
        differs the saves follow it."""
        if not report.playlists or not getattr(plan, "playlists", None):
            return []
        ra, m3u = _mod("retroarch"), _mod("m3u")
        disc_map = {ra.fold_name(a): b for a, b in App._planned_pairs(plan)}
        planned: dict[frozenset, str] = {}
        for op in plan.playlists:
            if getattr(op, "status", "") in ("write", "ok") and op.lines:
                names = frozenset(ra.fold_name(Path(e).stem) for e in m3u.entry_paths(op.lines, op.path.parent))
                planned.setdefault(names, op.path.stem)
        out: list[tuple[str, str]] = []
        for key, (name, discs) in report.playlists.items():
            if key in gone:
                continue
            new = frozenset(ra.fold_name(disc_map.get(ra.fold_name(d)) or d) for d in discs)
            target = planned.get(new)
            if target and ra.fold_name(target) != key:
                out.append((name, target))
        return out

    def _saves_plan(self, plan: Any) -> dict[str, Any] | None:
        """What the build does with the saves, from the plan alone (None: no RetroArch config is known, nothing to do):

        * ``gone``: the games the plan sets aside; of those ``archive`` (their saves go to the archive with them) and ``leave``
          (their saves stay where they are) as save sets, by the game's own choice or the profile's;
        * ``kept``: content names of games kept only because of their saves;
        * ``pairs``: the renames the saves of the games that stay follow."""
        report = getattr(plan, "save_report", None)
        if report is None:
            return None
        cached = getattr(plan, "_saves_plan", None)
        if cached is not None:
            return cached
        ra, library = _mod("retroarch"), self._library_mod()
        prof = plan.profile
        gone, _stay = self._plan_stems(plan)
        for pk, (_name, discs) in report.playlists.items():        # a playlist whose discs all go is gone with them
            fds = [ra.fold_name(d) for d in discs]
            if pk not in gone and fds and all(d in gone for d in fds):
                gone[pk] = gone[fds[0]]
        archive: list[Any] = []
        leave: list[Any] = []
        for stem in gone:
            st = report.sets.get(stem)
            if st is None or not st.files:
                continue
            choice = library.saved_choice(prof, st.dat, st.game) if st.match != "none" else prof.saved_games
            (archive if choice == "archive" else leave).append(st)
        kept = {ra.fold_name(self._op_stem(op)) for op in plan.ops
                if library.SAVED_KEEP_TEXT in (getattr(op, "reason", "") or "") and getattr(op, "status", "") != "delete"}
        pairs = [(a, b) for a, b in self._planned_pairs(plan) if ra.fold_name(a) not in gone]
        pairs += self._playlist_pairs(plan, report, gone)
        sp = {"gone": gone, "archive": archive, "leave": leave, "kept": kept, "pairs": pairs, "report": report}
        plan._saves_plan = sp
        return sp

    def _saves_moves(self, plan: Any, folder: Path, aside: Path | None) -> tuple[list[Any], dict[str, Any]]:
        """The moves that take the saves and states of the games the plan sets aside (and whose choice is "archive") to
        ``<archive>/<system folder name>/_saves/<path below the save folder>`` (inside the ROM folder, ``<folder>/_saves``,
        while no archive folder is used). Never over an existing file (the name gets `` (2)``). ``([], {})`` otherwise."""
        sp = self._saves_plan(plan)
        if sp is None or not sp["archive"]:
            return [], {}
        sortroot = _mod("sortroot")
        base = (aside / folder.name if aside is not None else folder) / SAVES_DIR
        names = sortroot.Names()
        moves: list[Any] = []
        for st in sp["archive"]:
            for f, root, _kind, size in st.members:
                if os.path.lexists(f):
                    moves.append(sortroot.SMove(f, names.free(base / f.relative_to(root)), SAVES_DIR, note=st.source, size=size,
                                                kind="folder" if os.path.isdir(f) else "file"))
        if not moves:
            return [], {}
        emulators = _mod("emulators")
        running = sorted({emulators.source_label(m.note) for m in moves if emulators.source_running(m.note)})
        return moves, {"files": len(moves), "games": len(sp["archive"]), "states": sum(st.states for st in sp["archive"]),
                       "to": str(base), "running": running}

    def _apply_saves_moves(self, moves: list[Any], library_log: str) -> dict[str, Any]:
        """Do :meth:`_saves_moves` (journalled like a sweep, linked to the build's undo log). The saves of a program that runs are
        not moved (RetroArch writes its saves back when it exits; an emulator keeps its save files open): they stay, and the answer says
        which program it was."""
        if not moves:
            return {}
        emulators = _mod("emulators")
        busy = {m.note for m in moves if emulators.source_running(m.note)}
        todo = [m for m in moves if m.note not in busy]
        names = sorted(emulators.source_label(b) for b in busy)
        if not todo:
            return {"skipped_running": True, "running": names, "moved": 0, "planned": len(moves), "failed": []}
        keep = [m.src.parent for m in todo] + [m.dst.parent.parent for m in todo]
        res = _mod("sortroot").apply_moves(todo, _mod("paths").data_dir() / "collection-undo", "libsweep", keep=keep,
                                           extra={"library_log": library_log})
        return {"moved": res["moved"], "planned": len(moves), "failed": res["failed"][:20], "journal": res["journal"],
                "skipped_running": bool(busy), "running": names}

    def _saves_plan_info(self, plan: Any, folder: Path, aside: Path | None, elsewhere: bool = False) -> dict[str, Any] | None:
        """What a build does with RetroArch's saves, from the plan (nothing is looked at after the build): ``kept`` games
        kept because of their saves, ``rename`` save files that follow renamed ROMs (a dry run of the real thing; copies when
        the build is into another folder), ``archive`` save files that go with the games the rules archive, ``leave`` the games
        the rules archive whose saves stay. None when no RetroArch config is known."""
        sp = self._saves_plan(plan)
        if sp is None:
            return None
        ra = _mod("retroarch")
        _ra, _installs, saved = self._ra_state()
        report, prof = sp["report"], plan.profile
        info: dict[str, Any] = {
            "install": True, "mode": prof.saved_games, "follow": saved.get("follow", True) is not False,
            "kept": sum(1 for k in sp["kept"] if k in report.sets and report.sets[k].files), "running": ra.is_running(),
            "elsewhere": elsewhere, "overrides": len(prof.saved_overrides), **{k: report.totals()[k] for k in ("files", "sets")},
            "rename": {"files": 0, "games": 0, "conflicts": 0}, "archive": {"files": 0, "games": 0, "states": 0, "to": ""},
            "leave": {"files": 0, "games": 0}}
        info["found"] = bool(info["files"])
        if info["follow"] and sp["pairs"] and self._ra_active() is not None:
            ops = ra.plan_follow(self._ra_active(), sp["pairs"], "copy" if elsewhere else "move", files=self._report_files(report))
            hits = ra.saves_of(self._report_files(report), {a for a, _b in sp["pairs"]})
            info["rename"] = {"files": sum(1 for o in ops if o.status in ("move", "copy")), "games": len({h.stem for h in hits}),
                              "conflicts": sum(1 for o in ops if o.status == "conflict")}
        if not elsewhere:
            _moves, arch = self._saves_moves(plan, folder, aside)
            if arch:
                info["archive"] = arch
            if sp["leave"]:
                info["leave"] = {"files": sum(len(st.members) for st in sp["leave"]), "games": len(sp["leave"])}
        return info

    def _saves_effect(self, plan: Any, op: Any) -> dict[str, Any] | None:
        """The saves of the game of a plan row, and what the build does with them (``effect``: ``rename`` | ``keep`` |
        ``archive`` | ``leave`` | ``""``)."""
        sp = self._saves_plan(plan)
        if sp is None:
            return None
        ra = _mod("retroarch")
        stem = ra.fold_name(self._op_stem(op))
        st = sp["report"].sets.get(stem)
        if st is None or not st.files:
            stem = sp["report"].playlist_of(stem) or stem          # a multi-disc game's saves are named after its playlist
            st = sp["report"].sets.get(stem)
        if st is None or not st.files:
            return None
        effect = ""
        if stem in sp["kept"]:
            effect = "keep"
        elif stem in sp["gone"]:
            effect = "archive" if st in sp["archive"] else "leave"
        elif st.renames and any(ra.fold_name(a) == stem for a, _b in sp["pairs"]):
            effect = "rename"
        return {"saves": st.saves, "states": st.states, "total": st.files, "effect": effect,
                "game": st.game, "dat": st.dat}

    def _link_undo(self, library_log: str, convert_log: str) -> None:
        folder = _mod("paths").data_dir() / "collection-undo"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"libconv-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(
            json.dumps({"library_log": library_log, "convert_log": convert_log}), encoding="utf-8")

    def _undo_linked_conversion(self, library_log: str, root: Path) -> dict[str, Any] | None:
        """Undoing a library build that converted first also undoes the conversion (its log was linked to the build's)."""
        folder = _mod("paths").data_dir() / "collection-undo"
        try:
            for j in sorted(folder.glob("libconv-*.json"), reverse=True):
                data = json.loads(j.read_text(encoding="utf-8"))
                if Path(data["library_log"]).resolve() == Path(library_log).resolve() and Path(data["convert_log"]).is_file():
                    res = dict(_mod("organiser").undo(Path(data["convert_log"]), root=root))
                    j.rename(j.with_suffix(".json.undone"))
                    return res
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return None

    def _undo_library_sweep(self, library_log: str) -> dict[str, Any] | None:
        """Undoing a build that moved its archived files out brings them back first (so the build's own undo finds them)."""
        sortroot = _mod("sortroot")
        folder = _mod("paths").data_dir() / "collection-undo"
        out: dict[str, Any] | None = None
        try:
            # one build can leave two journals: what its rules archived, and what earlier builds had left in _excluded/ ...
            for j in sorted(folder.glob("libsweep-*.json"), reverse=True):
                data = json.loads(j.read_text(encoding="utf-8"))
                if not data.get("undone") and data.get("library_log") and Path(data["library_log"]).resolve() == Path(library_log).resolve():
                    back = sortroot.undo_moves(j)
                    out = back if out is None else {"restored": out["restored"] + back["restored"],
                                                    "skipped": out["skipped"] + back["skipped"]}
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return out

    def _ra_undo_follow(self, library_log: str) -> dict[str, Any] | None:
        """Undoing a library build also gives the saves their old names back (the follow journal made by that build)."""
        try:
            ra, _installs, saved = self._ra_state()
            journal_dir = self._ra_dirs(saved)[0]
            for item in ra.list_undo(journal_dir):
                if item["kind"] != "follow":
                    continue
                data = json.loads(Path(item["journal"]).read_text(encoding="utf-8"))
                if data.get("library_log") and Path(data["library_log"]).resolve() == Path(library_log).resolve():
                    return ra.undo_relocation(Path(item["journal"]))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"error": str(exc)}
        return None

    def retroarch_shared(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/retroarch/shared {base?}``: the asset folders RetroArch uses against those in ``base`` (default: the
        folder that holds its playlists / saves now)."""
        ra, installs, saved = self._ra_state()
        sel = self._ra_selected(installs, saved)
        if sel is None:
            raise ApiError(HTTPStatus.CONFLICT, "No RetroArch was found. Choose its retroarch.cfg first.", "no_retroarch")
        base = _str_arg(body.get("base")) or saved.get("shared_base") or ra.shared_base(sel)
        return {"base": base, "rows": ra.shared_folders(sel, base)}

    def retroarch_shared_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        ra, installs, saved = self._ra_state()
        sel = self._ra_selected(installs, saved)
        if sel is None:
            raise ApiError(HTTPStatus.CONFLICT, "No RetroArch was found. Choose its retroarch.cfg first.", "no_retroarch")
        base = _str_arg(body.get("base"))
        rows = ra.shared_folders(sel, base)
        raw_keys = body.get("keys")
        keys = [k for k in (raw_keys if isinstance(raw_keys, list) else []) if isinstance(k, str)]
        try:
            res = ra.apply_shared(sel, rows, keys, self._ra_dirs(saved)[0])
        except RuntimeError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc), "retroarch_running") from None
        self._config_update(retroarch={**saved, "shared_base": base})
        res["rows"] = ra.shared_folders(sel, base)
        return res

    def retroarch_follow(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/retroarch/follow {follow: bool}`` remembers whether saves follow ROM renames."""
        saved = self._ra_state()[2]
        self._config_update(retroarch={**saved, "follow": _bool_arg(body.get("follow"), True)})
        self._saves_forget_all()
        return self._ra_info()

    def _ra_bios_scope(self, body: dict[str, Any]) -> tuple[Any, Any, list[dict[str, Any]], list[Path], list[Any], str]:
        """``(retroarch module, install, cores, folders to search, systems, label)`` for ``platform``: a system's name, or
        ``""`` / ``*configured`` (every system that has a ROM folder: the default), or ``*all`` (every installed core)."""
        ra, installs, saved = self._ra_state()
        sel = self._ra_selected(installs, saved)
        if sel is None:
            raise ApiError(HTTPStatus.CONFLICT, "No RetroArch was found. Choose its retroarch.cfg first.", "no_retroarch")
        scope = _str_arg(body.get("platform"))
        folders = self._folders()
        platforms = list(_mod("platforms").list_platforms())
        infos = ra.core_infos(sel)
        dirs: list[Path] = []
        extra = _str_arg(body.get("search_dir"))
        if scope in ("", "*configured", "*all"):
            configured = [p for p in platforms if folders.get(p.name)]
            if not configured and not extra:
                raise ApiError(HTTPStatus.CONFLICT, "No system has a ROM folder yet. Set one on a system's Overview, or "
                               "choose a folder to search below.", "no_folders")
            dirs = [Path(folders[p.name]) for p in configured]
            if scope == "*all":
                cores, systems, label = infos, platforms, "every installed core"
            else:
                cores, systems, label = ra.cores_for_platforms(infos, configured), configured, "the systems with a ROM folder"
        else:
            platform = self._resolve_platform(scope)
            if folders.get(platform.name):
                dirs.append(Path(folders[platform.name]))
            cores, systems, label = ra.cores_for_platform(infos, platform), [platform], platform.name
        if extra:
            dirs.append(Path(os.path.expanduser(extra)))
        return ra, sel, cores, dirs, systems, label

    def _keys_check(self, dirs: list[Path], progress: Callable[[str], None] | None) -> dict[str, Any]:
        """The key files of the emulators that need them (Eden / yuzu, Ryujinx, Cemu): see ``keyfiles``."""
        return _mod("keyfiles").check(self._nin_cfg(), self._switch_eff(), dirs, progress=progress)

    def retroarch_bios(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/retroarch/bios {platform?, search_dir?}``: the firmware the cores expect, what is in place and where the
        missing files lie (synchronous; the UI uses ``/bios/scan``, a job with progress)."""
        ra, sel, cores, dirs, systems, label = self._ra_bios_scope(body)
        out = ra.check_bios_cores(sel, cores, dirs, platforms=systems)
        out["scope"], out["searched"] = label, [str(d) for d in dirs]
        out["keys"] = self._keys_check(dirs, None)
        return out

    def retroarch_bios_scan(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        ra, sel, cores, dirs, systems, label = self._ra_bios_scope(body)

        def work(job: Job) -> Any:
            job.report(0, 0, "Reading the cores' firmware lists...")
            out = ra.check_bios_cores(sel, cores, dirs, platforms=systems, progress=lambda m: job.report(0, 0, m))
            out["scope"], out["searched"], out["action"] = label, [str(d) for d in dirs], "retroarch_bios_check"
            out["keys"] = self._keys_check(dirs, lambda m: job.report(0, 0, m))
            return out

        return {"job": self.jobs.start("retroarch", work, cancellable=False).to_dict()}

    def retroarch_bios_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        ra, sel, cores, dirs, systems, _label = self._ra_bios_scope(body)
        mode = "copy" if _str_arg(body.get("mode")) == "copy" else "move"
        raw_paths = body.get("paths")
        wanted = {p for p in (raw_paths if isinstance(raw_paths, list) else []) if isinstance(p, str)}
        journal_dir = self._ra_dirs(self._ra_state()[2])[0]

        def work(job: Job) -> Any:
            job.report(0, 0, "Looking for the files...")
            check = ra.check_bios_cores(sel, cores, dirs, platforms=systems, progress=lambda m: job.report(0, 0, m))
            items = [{"target": i["target"], "source": i["source"]} for c in check["cores"] for i in c["firmware"]
                     if i["status"] == "found" and (not wanted or i["path"] in wanted)]
            items = list({i["target"]: i for i in items}.values())
            job.report(0, len(items), "Placing files...")
            res = ra.apply_bios(sel, items, journal_dir, mode)
            keys = _mod("keyfiles").place(r for r in self._keys_check(dirs, lambda m: job.report(0, 0, m))["rows"]
                                          if not wanted or r["source"] in wanted)
            res["keys_placed"] = keys["placed"]
            res["failed"] = list(res.get("failed", [])) + keys["failed"]
            res["action"] = "retroarch_bios"
            return res

        return {"job": self.jobs.start("retroarch", work, cancellable=False).to_dict()}


    # ---------------------------------------------------------------- Nintendo Switch: games and the two emulators' saves
    def _switch_cfg(self) -> dict[str, Any]:
        return _mod("switchapp").normalise(self._config().get("switch"))

    def switch_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """``GET /api/switch``: the settings, what was found on this machine, the title database."""
        return _mod("switchapp").describe(self._switch_cfg())

    def switch_config(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/switch/config {games?, eden?, ryujinx?, keys?}``: remember the folders (an empty text forgets one)."""
        cfg = self._switch_cfg()
        for key in ("games", "eden", "ryujinx", "keys", "archive"):
            if key in body:
                value = _str_arg(body.get(key))
                if value:
                    value = os.path.abspath(os.path.expanduser(value))
                    if key != "keys" and os.path.isfile(value):
                        raise ApiError(HTTPStatus.BAD_REQUEST, "That is a file, not a folder.", "bad_switch_folder")
                _mod("switchapp").store(key, value, cfg)
        if "verify_scan" in body:
            cfg["verify_scan"] = _bool_arg(body.get("verify_scan"), False)
        self._config_update(switch=cfg, strict=True)
        return self.switch_get({}, None)

    def _switch_eff(self) -> dict[str, Any]:
        """The Switch settings as they are used (what was found on this machine fills what the user left empty)."""
        cfg = self._switch_cfg()
        folder = self._folders().get("Nintendo Switch")          # the games folder is the system's folder, like any other system
        if folder:
            cfg = {**cfg, "games": folder}
        eff = _mod("switchapp").effective(cfg)
        eff.pop("auto", None)
        return eff

    # ---------------------------------------------------------------- Nintendo Wii / Wii U: games and the saves of Dolphin and Cemu
    def _nin_cfg(self) -> dict[str, dict[str, str]]:
        return _mod("nintendoapp").normalise(self._config().get("nintendo"))

    def emulators_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """``GET /api/emulators``: the emulators whose saves are looked at: platforms, folder (found or chosen), on or off."""
        switch = self._switch_cfg()
        folder = self._folders().get("Nintendo Switch")
        if folder:
            switch = {**switch, "games": folder}                    # (a portable Ryujinx is looked for next to the games)
        return {"emulators": _mod("emulators").describe(self._nin_cfg(), switch)}

    def emulators_config(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/emulators/config {source, folder?, enabled?}``: remember an emulator's folder (an empty text goes back to the
        one that was found) and whether its saves are looked at."""
        emu = _mod("emulators")
        key = _str_arg(body.get("source"))
        if key not in emu.KEYS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "unknown emulator", "bad_emulator")
        folder = None
        if "folder" in body:
            folder = _str_arg(body.get("folder"))
            if folder:
                folder = os.path.abspath(os.path.expanduser(folder))
                if os.path.isfile(folder):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "That is a file, not a folder.", "bad_folder")
        enabled = _bool_arg(body.get("enabled"), True) if "enabled" in body else None
        nin, switch = self._nin_cfg(), self._switch_cfg()
        emu.store(key, nin, switch, folder, enabled)
        self._config_update(nintendo=nin, switch=switch, strict=True)
        return self.emulators_get({}, None)

    @staticmethod
    def _archive_settings(cfg: dict[str, Any]) -> dict[str, Any]:
        """The archive folder every system uses (``dir``; empty = none: nothing is archived) and the
        systems that have their own (``overrides``)."""
        table = cfg.get("archive_overrides") if isinstance(cfg.get("archive_overrides"), dict) else {}
        return {"dir": str(cfg.get("archive_dir") or ""),
                "overrides": {k: v for k, v in table.items() if isinstance(k, str) and isinstance(v, str) and v}}

    def archive_save(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/settings/archive {dir, platform?}``: the archive folder for every system, or (with ``platform``) for that
        system alone. An empty ``dir`` goes back to the default (for a system: to the global setting)."""
        raw = _str_arg(body.get("dir"))
        if raw:
            folder = Path(os.path.abspath(os.path.expanduser(raw)))
            if folder.is_file():
                raise ApiError(HTTPStatus.BAD_REQUEST, "That is a file, not a folder.", "bad_archive")
            raw = str(folder)
        if _str_arg(body.get("platform")):
            platform = self._resolve_platform(body.get("platform"))
            self._config_update(archive_for=(platform.name, raw or None), strict=True)
        else:
            self._config_update(archive_dir=raw, strict=True)
        return self._archive_settings(self._config())

    def library_export_runs(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``GET /api/library/export/runs?dest=``: the builds recorded in a destination folder."""
        dest = _str_arg(query.get("dest"))
        if not dest:
            raise ApiError(HTTPStatus.BAD_REQUEST, "dest is required")
        return {"dest": dest, "runs": _mod("libexport").list_runs(Path(dest))}

    def library_export_undo(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/library/export/undo``: ``{dest, run?}`` removes what a build put in the destination."""
        dest = _str_arg(body.get("dest"))
        if not dest:
            raise ApiError(HTTPStatus.BAD_REQUEST, "dest is required")
        libexport = _mod("libexport")
        run = body.get("run")
        try:
            with self.jobs.exclusive("the undo of a library build"):
                res = libexport.undo_run(Path(dest), int(run) if run not in (None, "") else None)
                follow = _str_arg(body.get("follow"))          # the saves that build copied (its result names the journal)
                if follow and Path(follow).is_file() and self._ra_active() is not None:
                    try:
                        _mod("retroarch").undo_relocation(Path(follow))
                    except Exception:  # noqa: BLE001 - e.g. RetroArch is running: the copied saves stay, harmless
                        traceback.print_exc()
                return res
        except libexport.ExportError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc), "nothing_to_undo")

    def _run_conversions(self, job: Job, ops: list[Any], root: Path, platform: Any, index: Any,
                         report: Callable[..., None] | None = None) -> dict[str, Any]:
        """Do the ``convert`` ops (raw disc sets to CHD, or SNES / N64 dumps cleaned up); ``root`` holds the undo log."""
        rep = report or job.report
        rep(0, sum(1 for op in ops if op.status == "convert"), "Converting files...")
        if _layout(platform) == LAYOUT_GAME_FOLDER:
            chdman = self._chdman()         # optional: the built-in writer converts without it
            cfg = self._config()
            self._configure_temp(cfg)
            res = dict(_mod("discsys").apply_conversions(
                ops, root, chdman, index, progress=rep, cancel=job.cancel,
                workers=_mod("chdsched").default_workers(cfg.get("chd_workers")),
                engine=str(cfg.get("chd_engine") or "auto"), writer=self._chd_writer(cfg),
                preset=self._chd_preset(cfg)))
        else:
            res = dict(_call(_mod("convert").apply_conversions, ops, root, progress=rep, cancel=job.cancel))
        res["action"] = "convert"
        return res

    def _apply_conversions(self, job: Job, state: ScanState, latest_only: bool, report: Callable[..., None] | None = None) -> dict[str, Any]:
        """Do the conversions of the state's plan (raw disc sets to CHD, or SNES / N64 dumps cleaned up); returns the result
        (with ``undo_log``). The caller re-scans."""
        ops = self._convert_plan(state, latest_only)
        index = state.result.index if self._is_dc(state) else None
        return self._run_conversions(job, ops, state.root, state.platform, index, report)

    # ---- Sega Dreamcast: chdman + Verify fully

    def convert_plan(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        if _bool_arg(body.get("refresh")):       # "Recalculate preview": never reuse a cached plan
            self._drop_plans(state)
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
            page["chdman"] = dict(self._chdman_info())
            self._writer_facts(page["chdman"])
            page["kind"] = "chd"
            page["disc"] = self._disc_info(state.platform, self._config())
        return page

    def convert_apply(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        if not getattr(state.platform, "convertible", False):
            raise ApiError(HTTPStatus.CONFLICT, f"{state.platform.name} files are not converted")
        latest_only = self._latest_arg(state, body)
        self._convert_plan(state, latest_only)  # errors (e.g. module missing) before starting a job

        def work(job: Job) -> Any:
            res = self._apply_conversions(job, state, latest_only)
            self._rescan_into(job, state, res)
            self._carry_temp(state, res)
            return res

        return {"job": self.jobs.start("convert", work, platform=state.platform.name).to_dict()}

    def m3u_plan(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        state = self._require_scan()
        if _bool_arg(body.get("refresh")):       # "Recalculate preview": never reuse a cached plan
            self._drop_plans(state)
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

        return {"job": self.jobs.start("m3u", work, cancellable=False, platform=state.platform.name).to_dict()}

    def _engine_facts(self, info: dict[str, Any]) -> None:
        """How the built-in reader will run: native libFLAC or not, number of decode processes."""
        try:
            info["flac"] = _mod("flacnative").status()
            info["workers"] = _mod("chdsched").default_workers(self._config().get("chd_workers"))
        except Exception:  # noqa: BLE001 - informational only
            pass

    @staticmethod
    def _chd_writer(cfg: dict[str, Any]) -> str:
        return "chdman" if str(cfg.get("chd_writer") or "auto") == "chdman" else "auto"

    @staticmethod
    def _chd_preset(cfg: dict[str, Any]) -> str:
        """``zstd`` only while a Zstandard library can compress; else the default codecs."""
        if str(cfg.get("chd_preset") or "default") == "zstd":
            try:
                if _mod("chdwrite").zstd_available():
                    return "zstd"
            except Exception:  # noqa: BLE001 - fall back to the codecs every emulator reads
                pass
        return "default"

    def _writer_facts(self, info: dict[str, Any]) -> None:
        """The writer settings and what this installation can do: ``zstd_writer`` (a Zstandard library compresses),
        ``flac_encoder`` (libFLAC encodes; without it audio tracks are stored with LZMA, larger than chdman's) and
        ``os`` (``windows`` / ``linux`` / ``darwin`` / ...: which install steps and path examples the UI shows)."""
        cfg = self._config()
        info["writer"] = self._chd_writer(cfg)
        info["preset"] = self._chd_preset(cfg)
        info["os"] = _os_name()
        try:
            info["zstd_writer"] = bool(_mod("chdwrite").zstd_available())
        except Exception:  # noqa: BLE001 - informational only
            info["zstd_writer"] = False
        try:
            info["flac_encoder"] = bool(_mod("flacenc").available())
        except Exception:  # noqa: BLE001 - informational only
            info["flac_encoder"] = False

    def chdman_get(self, query: dict[str, str], body: Any) -> dict[str, Any]:
        """chdman detection result (``?refresh=1`` forces a new look)."""
        info = self._chdman_info(refresh=_bool_arg(query.get("refresh")))
        info["engine"] = str(self._config().get("chd_engine") or "auto")
        info["verify_scan"] = bool(self._config().get("chd_verify_scan"))
        self._engine_facts(info)
        self._writer_facts(info)
        return info

    def chdman_save(self, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Remember (empty = forget) the path of a chdman binary and the engine (``auto`` | ``python`` | ``chdman``)."""
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
            if engine not in ("auto", "python", "chdman"):
                raise ApiError(HTTPStatus.BAD_REQUEST, "engine must be auto, python or chdman")
            values["chd_engine"] = engine
        writer = body.get("writer")
        if writer is not None:
            if writer not in ("auto", "chdman"):
                raise ApiError(HTTPStatus.BAD_REQUEST, "writer must be auto or chdman")
            values["chd_writer"] = writer
        preset = body.get("preset")
        if preset is not None:
            if preset not in ("default", "zstd"):
                raise ApiError(HTTPStatus.BAD_REQUEST, "preset must be default or zstd")
            if preset == "zstd" and not _mod("chdwrite").zstd_available():
                raise ApiError(HTTPStatus.CONFLICT, "Zstandard compression needs a Zstandard library (libzstd)")
            values["chd_preset"] = preset
        if "verify_scan" in body:
            values["chd_verify_scan"] = _bool_arg(body.get("verify_scan"))
        if not values:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Nothing to save (expected path, engine, writer, preset or verify_scan)")
        self._config_update(strict=True, **values)
        info = self._chdman_info(refresh=True)
        info["engine"] = str(self._config().get("chd_engine") or "auto")
        info["verify_scan"] = bool(self._config().get("chd_verify_scan"))
        self._engine_facts(info)
        self._writer_facts(info)
        if raw and not info.get("found"):
            info["warning"] = f"{raw} did not answer like chdman"
        return info

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
    ("GET", "/api/updates"): App.updates_get,
    ("GET", "/api/databases"): App.databases_get,
    ("GET", "/api/bios"): App.bios_get,
    ("POST", "/api/updates/check"): App.updates_check,
    ("POST", "/api/updates/cancel"): App.updates_cancel,
    ("GET", "/api/ratings"): App.ratings_get,
    ("POST", "/api/ratings/download"): App.ratings_download,
    ("GET", "/api/library/profile"): App.library_profile,
    ("GET", "/api/library/defaults"): App.library_defaults_get,
    ("POST", "/api/library/defaults"): App.library_defaults_save,
    ("GET", "/api/library/totals"): App.library_totals,
    ("POST", "/api/library/profile"): App.library_profile_save,
    ("POST", "/api/library/override"): App.library_override,
    ("POST", "/api/library/saved"): App.library_saved,
    ("POST", "/api/library/plan"): App.library_plan,
    ("POST", "/api/library/vanished"): App.library_vanished,
    ("POST", "/api/library/apply"): App.library_apply,
    ("POST", "/api/library/undo"): App.library_undo,
    ("GET", "/api/library/export/runs"): App.library_export_runs,
    ("GET", "/api/emulators"): App.emulators_get,
    ("POST", "/api/emulators/config"): App.emulators_config,
    ("GET", "/api/retroarch"): App.retroarch_get,
    ("POST", "/api/retroarch/select"): App.retroarch_select,
    ("POST", "/api/retroarch/plan"): App.retroarch_plan,
    ("POST", "/api/retroarch/apply"): App.retroarch_apply,
    ("POST", "/api/retroarch/undo"): App.retroarch_undo,
    ("POST", "/api/retroarch/follow"): App.retroarch_follow,
    ("POST", "/api/retroarch/shared"): App.retroarch_shared,
    ("POST", "/api/retroarch/shared/apply"): App.retroarch_shared_apply,
    ("POST", "/api/retroarch/bios"): App.retroarch_bios,
    ("POST", "/api/retroarch/bios/scan"): App.retroarch_bios_scan,
    ("POST", "/api/retroarch/bios/apply"): App.retroarch_bios_apply,
    ("POST", "/api/scan/select"): App.scan_select,
    ("GET", "/api/switch"): App.switch_get,
    ("POST", "/api/switch/config"): App.switch_config,
    ("POST", "/api/settings/archive"): App.archive_save,
    ("POST", "/api/library/export/settings"): App.library_export_settings,
    ("POST", "/api/library/export/undo"): App.library_export_undo,
    ("POST", "/api/folders"): App.folders_save,
    ("POST", "/api/platforms/options"): App.platform_options,
    ("GET", "/api/fs/list"): App.fs_list,
    ("POST", "/api/scan"): App.scan_start,
    ("GET", "/api/scan/results"): App.scan_results,
    ("GET", "/api/scan/checksums"): App.scan_checksums,
    ("GET", "/api/organise/undo-logs"): App.undo_logs,
    ("POST", "/api/organise/undo"): App.organise_undo,
    ("POST", "/api/convert/plan"): App.convert_plan,
    ("POST", "/api/convert/apply"): App.convert_apply,
    ("POST", "/api/m3u/plan"): App.m3u_plan,
    ("POST", "/api/m3u/apply"): App.m3u_apply,
    ("GET", "/api/chdman"): App.chdman_get,
    ("POST", "/api/chdman"): App.chdman_save,
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
            self._discard_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid Host header"})
            return
        url = urlsplit(self.path)
        if url.path.startswith("/api/"):
            self._api(method, url.path, {k: v[-1] for k, v in parse_qs(url.query).items()})
        elif method == "GET":
            self._static(url.path)
        else:
            self._discard_body()
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _api(self, method: str, path: str, query: dict[str, str]) -> None:
        app = self.server.app
        handler = ROUTES.get((method, path))
        if handler is None:
            exists = any(p == path for _, p in ROUTES)
            status = HTTPStatus.METHOD_NOT_ALLOWED if exists else HTTPStatus.NOT_FOUND
            self._discard_body()
            self._send_json(status, {"error": status.phrase})
            return
        try:
            body: Any = None
            if method == "POST":
                token = self.headers.get(TOKEN_HEADER, "")
                if not secrets.compare_digest(token.encode(), app.token.encode()):
                    self._discard_body()
                    raise ApiError(HTTPStatus.FORBIDDEN, "Missing or invalid token")
                body = self._read_json()
            data = handler(app, query, body)
            self._send_json(HTTPStatus.OK, data)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message, **({"code": exc.error_code} if exc.error_code else {})})
        except Exception as exc:
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})

    def _discard_body(self) -> None:
        """Read and drop the request body of a request answered without it (403, 404, 405). Windows resets a socket
        closed with unread data (RST), which can destroy the response before the client reads it (WinError 10053
        on the client). A body larger than ``MAX_BODY`` or one that does not arrive just ends the connection."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0:
            if length < 0:
                self.close_connection = True
            return
        if length > MAX_BODY:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except OSError:
            self.close_connection = True

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
        """Send one response. A browser that went away meanwhile (a closed tab, a cancelled fetch: WinError 10053
        ``ConnectionAbortedError`` on Windows, ``BrokenPipeError`` / ``ConnectionResetError``) only ends this
        connection: no traceback, and :meth:`_api` never tries a 500 on the dead socket."""
        try:
            self._write_response(status, data, content_type)
        except ConnectionError:
            self.close_connection = True

    def _write_response(self, status: int, data: bytes, content_type: str) -> None:
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
    if pid and not _mod("winproc").pid_alive(pid):
        return None  # owner gone (never a signal on Windows; someone else's process counts as alive)
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
