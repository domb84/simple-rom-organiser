"""No-Intro DATs from the libretro-database mirror on GitHub (clrmamepro format).

The DATs are small (one file per console), stored in ``paths.nointro_dir()`` -
deliberately *not* in the TOSEC ``dats/`` folder, which is swapped atomically
whenever a TOSEC pack is extracted. ``manifest.json`` there remembers each
file's ETag so unchanged DATs are not downloaded again.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import http.client
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

from . import __version__, paths
from .tosec import Cancelled, CancelToken, DatInfo

BASE_URL = "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/no-intro/"
USER_AGENT = f"simple-rom-organiser/{__version__}"
TIMEOUT = 30  # seconds, per socket operation
CHUNK = 256 * 1024
NOINTRO_DATS = (
    "Nintendo - Game Boy Advance",
    "Nintendo - Nintendo 64",
    "Nintendo - Nintendo Entertainment System",
    "Nintendo - Super Nintendo Entertainment System",
    "Nintendo - Game Boy",
    "Nintendo - Game Boy Color",
    "Nintendo - Nintendo DS",
    "Sega - Mega Drive - Genesis",
    "Sega - Master System - Mark III",
    "Sega - Game Gear",
    "Sega - 32X",
    "Atari - Lynx",
)
MANIFEST = "manifest.json"   # {name: {"version", "etag", "sha1", "size", "url", "downloaded_at"}}

_VERSION_RE = re.compile(rb'\bversion\s+"([^"]*)"')

ProgressFn = Callable[[int, int, str], None]
Opener = Callable[[urllib.request.Request], Any]
PathLike = Union[str, "os.PathLike[str]"]


class NoIntroError(Exception):
    """Problem downloading or validating a No-Intro DAT."""


def _dir(directory: Optional[PathLike]) -> Path:
    return Path(directory) if directory is not None else paths.nointro_dir()


def dat_url(name: str) -> str:
    return BASE_URL + urllib.parse.quote(name + ".dat")


def header_version(path: PathLike) -> str:
    """``version "..."`` from the first 4 KiB of a clrmamepro DAT ("" if absent/unreadable)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return ""
    m = _VERSION_RE.search(head)
    return m.group(1).decode("utf-8", errors="replace").strip() if m else ""


def read_manifest(directory: Optional[PathLike] = None) -> dict[str, Any]:
    try:
        data = json.loads((_dir(directory) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(folder: Path, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=str(folder))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, folder / MANIFEST)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_dats(directory: Optional[PathLike] = None) -> list[DatInfo]:
    """Every ``<name>.dat`` in the No-Intro folder, sorted by name (version from manifest/header)."""
    folder = _dir(directory)
    manifest = read_manifest(folder)
    out: list[DatInfo] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for p in entries:
        if not p.is_file() or not p.name.lower().endswith(".dat") or p.name.startswith("."):
            continue
        name = p.name[:-4]
        entry = manifest.get(name)
        version = entry.get("version", "") if isinstance(entry, dict) else ""
        out.append(DatInfo(name=name, version=version or header_version(p), path=p))
    return sorted(out, key=lambda d: d.name.casefold())


def find_dat(name: str, directory: Optional[PathLike] = None) -> Optional[DatInfo]:
    for info in list_dats(directory):
        if info.name == name:
            return info
    return None


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def download_dat(name: str, directory: Optional[PathLike] = None,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 force: bool = False, opener: Optional[Opener] = None,
                 commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Download ``<name>.dat`` from the libretro mirror (conditional on the stored ETag).

    Returns ``{"name", "version", "status": "downloaded" | "unchanged"}``. Network, HTTP and
    validation problems raise :class:`NoIntroError` and leave an existing file untouched;
    a cancel raises :class:`tosec.Cancelled`.
    """
    folder = _dir(directory)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.dat"
    part = folder / f"{name}.dat.part"
    manifest = read_manifest(folder)
    entry = manifest.get(name) if isinstance(manifest.get(name), dict) else {}
    url = dat_url(name)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    etag = entry.get("etag") if entry else None
    if target.is_file() and not force and etag:
        req.add_header("If-None-Match", etag)
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=TIMEOUT))

    def unchanged() -> dict[str, Any]:
        version = (entry or {}).get("version") or header_version(target)
        if progress:
            progress(1, 1, f"{name} is up to date")
        return {"name": name, "version": version, "status": "unchanged"}

    if progress:
        progress(0, 0, f"Downloading {name}")
    try:
        resp = open_fn(req)
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and target.is_file():
            return unchanged()
        raise NoIntroError(f"{name}: HTTP {exc.code} from {url}") from exc
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise NoIntroError(f"{name}: could not download ({reason})") from exc

    try:
        status = getattr(resp, "status", 200) or 200
        if status == 304 and target.is_file():
            return unchanged()
        if status >= 400 or status == 304:
            raise NoIntroError(f"{name}: HTTP {status} from {url}")
        headers = getattr(resp, "headers", None)
        new_etag = headers.get("ETag") if headers is not None else None
        try:
            total = int((headers.get("Content-Length") if headers is not None else 0) or 0)
        except ValueError:
            total = 0
        done = 0
        sha = hashlib.sha1()
        try:
            with part.open("wb") as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    sha.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total, f"Downloading {name}")
        except (Cancelled, KeyboardInterrupt):
            _unlink(part)
            raise
        except (OSError, http.client.HTTPException) as exc:
            _unlink(part)
            raise NoIntroError(f"{name}: download failed ({exc})") from exc
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001 - closing a broken response must not mask the error
            pass

    if total and done != total:
        _unlink(part)
        raise NoIntroError(f"{name}: incomplete download (got {done} of {total} bytes)")
    from .datfile import parse_dat
    try:
        dat = parse_dat(part, set_names=True)
    except (ValueError, OSError) as exc:
        _unlink(part)
        raise NoIntroError(f"{name}: downloaded file is not a valid DAT ({exc})") from exc
    if not dat.roms:
        _unlink(part)
        raise NoIntroError(f"{name}: downloaded DAT contains no roms")
    if dat.name != name:
        _unlink(part)
        raise NoIntroError(f"{name}: downloaded DAT is named {dat.name!r}")
    digest = sha.hexdigest()
    version = dat.version or header_version(part)
    same = target.is_file() and entry and entry.get("sha1") == digest
    with (commit_lock if commit_lock is not None else contextlib.nullcontext()):
        os.replace(part, target)
    manifest = read_manifest(folder)
    manifest[name] = {"version": version, "etag": new_etag or "", "sha1": digest, "size": done,
                      "url": url, "downloaded_at": _now()}
    _write_manifest(folder, manifest)
    if progress:
        progress(done, total or done, f"Downloaded {name}")
    return {"name": name, "version": version, "status": "unchanged" if same else "downloaded"}


def _norm_etag(value: Optional[str]) -> str:
    v = (value or "").strip()
    return v[2:] if v.startswith("W/") else v


def check_updates(names: Optional[Iterable[str]] = None, opener: Optional[Opener] = None,
                  directory: Optional[PathLike] = None, timeout: float = 10) -> list[dict[str, Any]]:
    """Check-only: compare each DAT's remote ETag (HEAD) with the stored manifest.

    One row per name: ``{"name", "installed": version|None, "status", "error"?}`` with status
    ``up_to_date`` | ``update_available`` | ``missing`` (no local file) | ``unknown`` (local file but
    no stored ETag - treated as an update) | ``error`` (network/HTTP problem; never raises).
    """
    folder = _dir(directory)
    manifest = read_manifest(folder)
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=timeout))
    rows: list[dict[str, Any]] = []
    for name in (list(names) if names is not None else list(NOINTRO_DATS)):
        local = find_dat(name, folder)
        entry = manifest.get(name) if isinstance(manifest.get(name), dict) else {}
        row: dict[str, Any] = {"name": name, "installed": (local.version or None) if local else None,
                               "status": "unknown"}
        req = urllib.request.Request(dat_url(name), method="HEAD",
                                     headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            resp = open_fn(req)
            try:
                status = getattr(resp, "status", 200) or 200
                remote = _norm_etag(resp.headers.get("ETag") if resp.headers is not None else "")
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            if status >= 400:
                row.update(status="error", error=f"HTTP {status}")
            else:
                stored = _norm_etag((entry or {}).get("etag"))
                if local is None:
                    row["status"] = "missing"
                elif not stored or not remote:
                    row["status"] = "unknown"
                else:
                    row["status"] = "up_to_date" if stored == remote else "update_available"
        except urllib.error.HTTPError as exc:
            row.update(status="error", error=f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            row.update(status="error", error=str(getattr(exc, "reason", exc)))
        rows.append(row)
    return rows


def update_dats(names: Optional[Iterable[str]] = None, progress: Optional[ProgressFn] = None,
                cancel: Optional[CancelToken] = None, force: bool = False,
                opener: Optional[Opener] = None,
                directory: Optional[PathLike] = None,
                commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Download/refresh the No-Intro DATs (default: all of :data:`NOINTRO_DATS`).

    Returns ``{"source": "nointro", "dats": [...], "downloaded", "unchanged", "failed", "count"}``.
    Raises :class:`NoIntroError` only when every DAT failed and none is present locally.
    """
    wanted = list(names) if names is not None else list(NOINTRO_DATS)
    folder = _dir(directory)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    first_exc: Optional[Exception] = None
    for i, name in enumerate(wanted):
        if cancel is not None and cancel.is_set():
            raise Cancelled()

        def report(done: int, total: int, msg: str, _i: int = i) -> None:
            if progress:
                progress(done, total, f"[{_i + 1}/{len(wanted)}] {msg}")

        try:
            rows.append(download_dat(name, folder, progress=report, cancel=cancel, force=force,
                                     opener=opener, commit_lock=commit_lock))
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - one broken DAT must not skip the others
            if not isinstance(exc, NoIntroError):
                wrapped = NoIntroError(f"{name}: {exc}")
                wrapped.__cause__ = exc
                exc = wrapped
            if first_exc is None:
                first_exc = exc
            errors.append(str(exc))
            local = find_dat(name, folder)
            rows.append({"name": name, "version": local.version if local else None,
                         "status": "error", "error": str(exc)})
    present = {d.name for d in list_dats(folder)}
    failed = sum(1 for r in rows if r["status"] == "error")
    if wanted and failed == len(wanted) and not any(n in present for n in wanted):
        raise NoIntroError("Could not reach GitHub (offline?): " + "; ".join(errors[:2])) \
            from (first_exc.__cause__ if first_exc is not None else None)
    result = {
        "source": "nointro",
        "dats": rows,
        "downloaded": sum(1 for r in rows if r["status"] == "downloaded"),
        "unchanged": sum(1 for r in rows if r["status"] == "unchanged"),
        "failed": failed,
        "count": sum(1 for n in wanted if n in present),
    }
    if progress:
        progress(len(wanted), len(wanted),
                 f"No-Intro DATs: {result['downloaded']} downloaded, {result['unchanged']} "
                 f"unchanged, {failed} failed")
    return result
