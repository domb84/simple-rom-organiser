"""The Redump DATs (``http://redump.org/datfile/<system>/``): Sega Dreamcast, Sony PlayStation, Sony PlayStation 2,
Nintendo GameCube.

Redump serves one zip per system (``Sega - Dreamcast - Datfile (1516) (2026-06-14 18-25-41).zip``
holding a single Logiqx ``.dat``) over plain **HTTP only** (the HTTPS port refuses connections).
The newest version is read from the ``Content-Disposition`` filename of a HEAD request (a streamed GET
that only reads the headers is the fallback), so a check costs one tiny request; the zip is downloaded
only when that date is newer than the installed DAT's ``<version>``. The DAT lives in
``paths.redump_dir()`` - its own folder, never touched by the TOSEC / No-Intro / WHDLoad updaters.

The API mirrors :mod:`romorg.whdload` (``check_updates`` / ``download_dat`` / ``update_dats`` /
``list_dats`` / ``find_dat``), so :mod:`romorg.autoupdate` drives it the same way.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import os
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Iterable, Optional

from . import datsource, paths
from .datsource import CHUNK, MANIFEST, TIMEOUT, USER_AGENT, Opener, PathLike, ProgressFn   # noqa: F401  (this module's names for them)
from .tosec import CancelToken, DatInfo

DAT_NAME = "Sega - Dreamcast"            # header name == file stem == the platform's DAT name (the default DAT)
PSX_DAT_NAME = "Sony - PlayStation"
PS2_DAT_NAME = "Sony - PlayStation 2"
SYSTEMS = {DAT_NAME: "dc", PSX_DAT_NAME: "psx", PS2_DAT_NAME: "ps2"}   # DAT name -> redump.org system slug
BASE_URL = "http://redump.org/datfile/"  # HTTP only: https://redump.org refuses the connection
REDUMP_DATS = (DAT_NAME, PSX_DAT_NAME, PS2_DAT_NAME)        # (the GameCube and the Wii come from libretro's mirror: see nointro)
# manifest.json: {name: {"version", "sha1", "size", "url", "filename", "downloaded_at"}}
MAX_DAT_BYTES = 256 * 1024 * 1024        # sanity limit for the extracted DAT

_VERSION_RE = re.compile(rb"<version>([^<]*)</version>")
_FILE_VERSION_RE = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2})\)")
_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


class RedumpError(Exception):
    """Problem downloading or validating a Redump DAT."""


def _dir(directory: Optional[PathLike]) -> Path:
    return Path(directory) if directory is not None else paths.redump_dir()


def dat_url(name: str = DAT_NAME) -> str:
    try:
        return f"{BASE_URL}{SYSTEMS[name]}/"
    except KeyError:
        raise RedumpError(f"unknown Redump DAT: {name!r}") from None


def header_version(path: PathLike) -> str:
    """``<version>`` of the DAT header ("" if absent / unreadable), e.g. ``2026-06-14 18-25-41``."""
    return datsource.header_version(path, _VERSION_RE)


def read_manifest(directory: Optional[PathLike] = None) -> dict[str, Any]:
    return datsource.read_manifest(_dir(directory))


def list_dats(directory: Optional[PathLike] = None) -> list[DatInfo]:
    """Every ``<name>.dat`` in the Redump folder, sorted by name (version = header ``<version>``)."""
    return datsource.list_dats(_dir(directory), _VERSION_RE, header_first=True)


def find_dat(name: str = DAT_NAME, directory: Optional[PathLike] = None) -> Optional[DatInfo]:
    return datsource.find_dat(name, list_dats(directory))


def remote_version(headers: Any) -> tuple[str, str]:
    """``(version, filename)`` from a response's ``Content-Disposition`` ("" when it has none)."""
    cd = headers.get("Content-Disposition") if headers is not None else None
    if not cd:
        return "", ""
    m = _FILENAME_RE.search(cd)
    filename = m.group(1).strip() if m else ""
    v = _FILE_VERSION_RE.findall(filename)
    return (v[-1] if v else ""), filename


def _open_headers(url: str, open_fn: Opener) -> Any:
    """HEAD first; a server that refuses HEAD gets a streamed GET of which only the headers are read."""
    try:
        return open_fn(datsource.request(url, method="HEAD"))
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 405, 501):
            raise
    return open_fn(datsource.request(url))


def check_updates(names: Optional[Iterable[str]] = None, opener: Optional[Opener] = None,
                  directory: Optional[PathLike] = None, timeout: float = 10) -> list[dict[str, Any]]:
    """Check-only: the remote version (``Content-Disposition`` date) against the installed ``<version>``.

    One row per name: ``{"name", "installed": version|None, "latest": version|None, "status", "error"?}``,
    status ``up_to_date`` | ``update_available`` (remote newer) | ``missing`` (no local file) | ``unknown``
    (the server named no version) | ``error`` (network / HTTP problem; never raises). One request per DAT.
    """
    folder = _dir(directory)
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=timeout))
    rows: list[dict[str, Any]] = []
    for name in (list(names) if names is not None else list(REDUMP_DATS)):
        local = find_dat(name, folder)
        row: dict[str, Any] = {"name": name, "installed": (local.version or None) if local else None,
                               "latest": None, "status": "unknown"}
        try:
            resp = _open_headers(dat_url(name), open_fn)
            try:
                status = getattr(resp, "status", 200) or 200
                version, _fn = remote_version(getattr(resp, "headers", None))
            finally:
                datsource.close(resp)   # a streamed GET: closing without reading the body
            if status >= 400:
                row.update(status="error", error=f"HTTP {status}")
            else:
                row["latest"] = version or None
                if local is None:
                    row["status"] = "missing"
                elif not version:
                    row["status"] = "unknown"
                elif not local.version or version > local.version:
                    row["status"] = "update_available"
                else:
                    row["status"] = "up_to_date"
        except RedumpError as exc:
            row.update(status="error", error=str(exc))
        except urllib.error.HTTPError as exc:
            row.update(status="error", error=f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            row.update(status="error", error=str(getattr(exc, "reason", exc)))
        rows.append(row)
    return rows


def _extract_dat(zip_path: Path, dest: Path, name: str) -> None:
    """Copy the (single) ``.dat`` member of the zip to ``dest`` (only the bytes are used: no path from the zip)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".dat")]
            if len(members) != 1:
                raise RedumpError(f"{name}: the downloaded zip holds {len(members)} .dat files (expected 1)")
            info = members[0]
            if info.file_size > MAX_DAT_BYTES:
                raise RedumpError(f"{name}: the DAT inside the zip is implausibly large")
            with zf.open(info) as src, dest.open("wb") as out:
                left = MAX_DAT_BYTES
                while True:
                    block = src.read(CHUNK)
                    if not block:
                        break
                    left -= len(block)
                    if left < 0:
                        raise RedumpError(f"{name}: the DAT inside the zip is implausibly large")
                    out.write(block)
    except zipfile.BadZipFile as exc:
        raise RedumpError(f"{name}: the download is not a zip file ({exc})") from exc


def download_dat(name: str = DAT_NAME, directory: Optional[PathLike] = None,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 force: bool = False, opener: Optional[Opener] = None,
                 commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Download ``<name>`` (a zip) and install its DAT.

    Returns ``{"name", "version", "status": "downloaded" | "unchanged"}`` (``unchanged``: the installed DAT
    is current - nothing is downloaded unless ``force``). Network, HTTP and validation problems raise
    :class:`RedumpError` and leave an existing DAT untouched; a cancel raises :class:`tosec.Cancelled`.
    The zip goes to ``<name>.zip.part``, its DAT is extracted to ``<name>.dat.part``, parsed (validated) and
    only then atomically replaced.
    """
    folder = _dir(directory)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.dat"
    zpart = folder / f"{name}.zip.part"
    dpart = folder / f"{name}.dat.part"
    manifest = read_manifest(folder)
    entry = manifest.get(name) if isinstance(manifest.get(name), dict) else {}
    url = dat_url(name)
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=TIMEOUT))

    def unchanged() -> dict[str, Any]:
        local = find_dat(name, folder)
        if progress:
            progress(1, 1, f"{name} is up to date")
        return {"name": name, "version": (local.version if local else "") or (entry or {}).get("version", ""),
                "status": "unchanged"}

    if progress:
        progress(0, 0, f"Downloading {name}")
    resp = datsource.open_url(datsource.request(url), open_fn, name, url, RedumpError)

    try:
        status = getattr(resp, "status", 200) or 200
        if status >= 400:
            raise RedumpError(f"{name}: HTTP {status} from {url}")
        headers = getattr(resp, "headers", None)
        version, filename = remote_version(headers)
        local = find_dat(name, folder)
        if not force and local is not None and version and local.version and version <= local.version:
            return unchanged()   # the response body is never read
        done, total = datsource.receive(resp, zpart, name, RedumpError, progress, cancel)
    finally:
        datsource.close(resp)

    try:
        if total and done != total:
            raise RedumpError(f"{name}: incomplete download (got {done} of {total} bytes)")
        _extract_dat(zpart, dpart, name)
        from .datfile import parse_redump
        try:
            dat = parse_redump(dpart)
        except Exception as exc:  # noqa: BLE001 - ElementTree.ParseError is not a ValueError
            raise RedumpError(f"{name}: downloaded file is not a valid DAT ({exc})") from exc
        if not dat.roms:
            raise RedumpError(f"{name}: downloaded DAT contains no roms")
        if dat.name != name:
            raise RedumpError(f"{name}: downloaded DAT is named {dat.name!r}")
        sha = hashlib.sha1()
        with dpart.open("rb") as fh:
            for block in iter(lambda: fh.read(CHUNK), b""):
                sha.update(block)
        digest = sha.hexdigest()
        new_version = dat.version or version or header_version(dpart)
        same = target.is_file() and entry and entry.get("sha1") == digest
        with (commit_lock if commit_lock is not None else contextlib.nullcontext()):
            os.replace(dpart, target)
    finally:
        datsource.unlink(zpart)
        datsource.unlink(dpart)
    manifest = read_manifest(folder)
    manifest[name] = {"version": new_version, "sha1": digest, "size": target.stat().st_size, "url": url,
                      "filename": filename, "downloaded_at": datsource.now()}
    datsource.write_manifest(folder, manifest)
    if progress:
        progress(done, total or done, f"Downloaded {name}")
    return {"name": name, "version": new_version, "status": "unchanged" if same else "downloaded"}


def update_dats(names: Optional[Iterable[str]] = None, progress: Optional[ProgressFn] = None,
                cancel: Optional[CancelToken] = None, force: bool = False,
                opener: Optional[Opener] = None,
                directory: Optional[PathLike] = None,
                commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Download / refresh the Redump DATs (default: :data:`REDUMP_DATS`).

    Returns ``{"source": "redump", "dats": [...], "downloaded", "unchanged", "failed", "count"}``.
    Raises :class:`RedumpError` only when every DAT failed and none is present locally.
    """
    return datsource.update_all(list(names) if names is not None else list(REDUMP_DATS), _dir(directory), download_dat,
                                find_dat, list_dats, RedumpError, "redump", "redump.org", "Redump DAT", progress, cancel,
                                force=force, opener=opener, commit_lock=commit_lock)
