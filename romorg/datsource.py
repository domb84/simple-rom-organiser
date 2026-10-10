"""What the DAT sources have in common (``nointro``, ``whdload``, ``redump``): a folder of ``<name>.dat`` files with a
``manifest.json`` beside them, a download that is validated before it replaces the installed file, and one run over several DATs.

Each source module keeps its own names, addresses, error class and what its version is read from; the work is here.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import http.client
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

from . import __version__
from .tosec import Cancelled, CancelToken, DatInfo

__all__ = ["CHUNK", "TIMEOUT", "MANIFEST", "USER_AGENT", "ProgressFn", "Opener", "PathLike", "now", "unlink", "norm_etag",
           "header_version", "read_manifest", "write_manifest", "list_dats", "find_dat", "request", "open_url", "receive",
           "close", "download_etag", "check_etag", "update_all"]

USER_AGENT = f"simple-rom-organiser/{__version__}"
TIMEOUT = 30  # seconds, per socket operation
CHUNK = 256 * 1024
MANIFEST = "manifest.json"

ProgressFn = Callable[[int, int, str], None]
Opener = Callable[[urllib.request.Request], Any]
PathLike = Union[str, "os.PathLike[str]"]


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def norm_etag(value: Optional[str]) -> str:
    v = (value or "").strip()
    return v[2:] if v.startswith("W/") else v


def header_version(path: PathLike, pattern: Any) -> str:
    """What ``pattern`` (a bytes regex with one group) finds in the first 4 KiB of a DAT ("" if absent / unreadable)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return ""
    m = pattern.search(head)
    return m.group(1).decode("utf-8", errors="replace").strip() if m else ""


def read_manifest(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_manifest(folder: Path, data: dict[str, Any]) -> None:
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


def list_dats(folder: Path, pattern: Any, header_first: bool) -> list[DatInfo]:
    """Every ``<name>.dat`` in ``folder``, sorted by name. The version is the manifest's or the header's (``pattern``);
    ``header_first`` says which of the two wins."""
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
        stored = entry.get("version", "") if isinstance(entry, dict) else ""
        version = (header_version(p, pattern) or stored) if header_first else (stored or header_version(p, pattern))
        out.append(DatInfo(name=name, version=version, path=p))
    return sorted(out, key=lambda d: d.name.casefold())


def find_dat(name: str, dats: Iterable[DatInfo]) -> Optional[DatInfo]:
    for info in dats:
        if info.name == name:
            return info
    return None


def request(url: str, method: Optional[str] = None) -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})


def open_url(req: urllib.request.Request, open_fn: Opener, name: str, url: str, error: type,
             not_modified: Optional[Callable[[], Any]] = None) -> Any:
    """The response to ``req``; HTTP and network problems raise ``error``. ``not_modified()`` answers a 304 (the result is
    returned as ``("unchanged", value)``)."""
    try:
        return open_fn(req)
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and not_modified is not None:
            return ("unchanged", not_modified())
        raise error(f"{name}: HTTP {exc.code} from {url}") from exc
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise error(f"{name}: could not download ({reason})") from exc


def close(resp: Any) -> None:
    try:
        resp.close()
    except Exception:  # noqa: BLE001 - closing a broken response must not mask the error
        pass


def receive(resp: Any, part: Path, name: str, error: type, progress: Optional[ProgressFn] = None,
            cancel: Optional[CancelToken] = None, sha: Any = None) -> tuple[int, int]:
    """Write the body of ``resp`` to ``part``; ``(bytes written, Content-Length or 0)``. A cancel, or a failure, removes ``part``."""
    headers = getattr(resp, "headers", None)
    try:
        total = int((headers.get("Content-Length") if headers is not None else 0) or 0)
    except ValueError:
        total = 0
    done = 0
    try:
        with part.open("wb") as out:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                if sha is not None:
                    sha.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total, f"Downloading {name}")
    except (Cancelled, KeyboardInterrupt):
        unlink(part)
        raise
    except (OSError, http.client.HTTPException) as exc:
        unlink(part)
        raise error(f"{name}: download failed ({exc})") from exc
    return done, total


def download_etag(name: str, folder: Path, url: str, error: type, pattern: Any, header_first: bool,
                  progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None, force: bool = False,
                  opener: Optional[Opener] = None, commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Download a clrmamepro ``<name>.dat`` from ``url`` unless the stored ETag says it is unchanged.

    Returns ``{"name", "version", "status": "downloaded" | "unchanged"}``. Network, HTTP and validation problems raise
    ``error`` and leave an existing file untouched; a cancel raises :class:`tosec.Cancelled`. The file is written to
    ``<name>.dat.part``, parsed (validated) and only then atomically replaced.
    """
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.dat"
    part = folder / f"{name}.dat.part"
    manifest = read_manifest(folder)
    entry = manifest.get(name) if isinstance(manifest.get(name), dict) else {}
    req = request(url)
    etag = entry.get("etag") if entry else None
    if target.is_file() and not force and etag:
        req.add_header("If-None-Match", etag)
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=TIMEOUT))

    def unchanged() -> dict[str, Any]:
        stored, head = (entry or {}).get("version"), header_version(target, pattern)
        if progress:
            progress(1, 1, f"{name} is up to date")
        return {"name": name, "version": ((head or stored) if header_first else (stored or head)) or "", "status": "unchanged"}

    if progress:
        progress(0, 0, f"Downloading {name}")
    resp = open_url(req, open_fn, name, url, error, unchanged if target.is_file() else None)
    if isinstance(resp, tuple):
        return resp[1]
    try:
        status = getattr(resp, "status", 200) or 200
        if status == 304 and target.is_file():
            return unchanged()
        if status >= 400 or status == 304:
            raise error(f"{name}: HTTP {status} from {url}")
        headers = getattr(resp, "headers", None)
        new_etag = headers.get("ETag") if headers is not None else None
        sha = hashlib.sha1()
        done, total = receive(resp, part, name, error, progress, cancel, sha)
    finally:
        close(resp)

    if total and done != total:
        unlink(part)
        raise error(f"{name}: incomplete download (got {done} of {total} bytes)")
    from .datfile import parse_dat
    try:
        dat = parse_dat(part, set_names=True)
    except (ValueError, OSError) as exc:
        unlink(part)
        raise error(f"{name}: downloaded file is not a valid DAT ({exc})") from exc
    if not dat.roms:
        unlink(part)
        raise error(f"{name}: downloaded DAT contains no roms")
    if dat.name != name:
        unlink(part)
        raise error(f"{name}: downloaded DAT is named {dat.name!r}")
    digest = sha.hexdigest()
    version = dat.version or header_version(part, pattern)
    same = target.is_file() and entry and entry.get("sha1") == digest
    with (commit_lock if commit_lock is not None else contextlib.nullcontext()):
        os.replace(part, target)
    manifest = read_manifest(folder)
    manifest[name] = {"version": version, "etag": new_etag or "", "sha1": digest, "size": done,
                      "url": url, "downloaded_at": now()}
    write_manifest(folder, manifest)
    if progress:
        progress(done, total or done, f"Downloaded {name}")
    return {"name": name, "version": version, "status": "unchanged" if same else "downloaded"}


def check_etag(names: Iterable[str], folder: Path, url_of: Callable[[str], str], find: Callable[[str, Path], Optional[DatInfo]],
               opener: Optional[Opener] = None, timeout: float = 10) -> list[dict[str, Any]]:
    """Check-only: compare each DAT's remote ETag (HEAD) with the stored manifest.

    One row per name: ``{"name", "installed": version|None, "status", "error"?}`` with status
    ``up_to_date`` | ``update_available`` | ``missing`` (no local file) | ``unknown`` (local file but
    no stored ETag - treated as an update) | ``error`` (network/HTTP problem; never raises).
    """
    manifest = read_manifest(folder)
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=timeout))
    rows: list[dict[str, Any]] = []
    for name in names:
        local = find(name, folder)
        entry = manifest.get(name) if isinstance(manifest.get(name), dict) else {}
        row: dict[str, Any] = {"name": name, "installed": (local.version or None) if local else None,
                               "status": "unknown"}
        try:
            resp = open_fn(request(url_of(name), method="HEAD"))
            try:
                status = getattr(resp, "status", 200) or 200
                remote = norm_etag(resp.headers.get("ETag") if resp.headers is not None else "")
            finally:
                close(resp)
            if status >= 400:
                row.update(status="error", error=f"HTTP {status}")
            else:
                stored = norm_etag((entry or {}).get("etag"))
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


def update_all(wanted: list[str], folder: Path, download: Callable[..., dict[str, Any]],
               find: Callable[[str, Path], Optional[DatInfo]], present: Callable[[Path], list[DatInfo]], error: type,
               source: str, site: str, label: str, progress: Optional[ProgressFn] = None,
               cancel: Optional[CancelToken] = None, numbered: bool = False, **options: Any) -> dict[str, Any]:
    """Download / refresh every DAT of ``wanted`` with the source's own ``download``; one broken DAT does not skip the others.

    Returns ``{"source", "dats": [...], "downloaded", "unchanged", "failed", "count"}``. Raises ``error`` only when every
    DAT failed and none is present locally. ``numbered``: the progress messages say ``[2/12]``.
    """
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    first_exc: Optional[Exception] = None
    for i, name in enumerate(wanted):
        if cancel is not None and cancel.is_set():
            raise Cancelled()

        def report(done: int, total: int, msg: str, _i: int = i) -> None:
            if progress:
                progress(done, total, f"[{_i + 1}/{len(wanted)}] {msg}" if numbered else msg)

        try:
            rows.append(download(name, folder, progress=report if numbered else progress, cancel=cancel, **options))
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - one broken DAT must not skip the others
            if not isinstance(exc, error):
                wrapped = error(f"{name}: {exc}")
                wrapped.__cause__ = exc
                exc = wrapped
            if first_exc is None:
                first_exc = exc
            errors.append(str(exc))
            local = find(name, folder)
            rows.append({"name": name, "version": local.version if local else None,
                         "status": "error", "error": str(exc)})
    have = {d.name for d in present(folder)}
    failed = sum(1 for r in rows if r["status"] == "error")
    if wanted and failed == len(wanted) and not any(n in have for n in wanted):
        raise error(f"Could not reach {site} (offline?): " + "; ".join(errors[:2])) \
            from (first_exc.__cause__ if first_exc is not None else None)
    result = {
        "source": source,
        "dats": rows,
        "downloaded": sum(1 for r in rows if r["status"] == "downloaded"),
        "unchanged": sum(1 for r in rows if r["status"] == "unchanged"),
        "failed": failed,
        "count": sum(1 for n in wanted if n in have),
    }
    if progress:
        progress(len(wanted), len(wanted),
                 f"{label}: {result['downloaded']} downloaded, {result['unchanged']} "
                 f"unchanged, {failed} failed")
    return result
