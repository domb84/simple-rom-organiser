"""Locate, download and extract the TOSEC DAT pack; find the latest DAT by name."""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional, Protocol, Union

from . import __version__, paths

DOWNLOADS_URL = "https://www.tosecdev.org/downloads"
DEFAULT_DAT_NAME = "Commodore Amiga - Games - [ADF]"
USER_AGENT = f"simple-rom-organiser/{__version__} (+https://www.tosecdev.org)"
TIMEOUT = 30  # seconds, per socket operation
CHUNK = 256 * 1024
DAT_FOLDERS = ("TOSEC", "TOSEC-ISO", "TOSEC-PIX")
RELEASE_FILE = "release.json"

# Category links like /downloads/category/59-2025-03-13 (no trailing slug).
_CATEGORY_RE = re.compile(r"/downloads/category/(\d+)-(\d{4}-\d{2}-\d{2})/?$")
# DAT names like "Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat"
_DAT_RE = re.compile(r"^(?P<name>.*?) \(TOSEC-v(?P<version>\d{4}-\d{2}-\d{2})[^)]*\)\.dat$",
                     re.IGNORECASE)

ProgressFn = Callable[[int, int], None]
Fetch = Callable[[str], str]


class CancelToken(Protocol):
    def is_set(self) -> bool: ...


class TosecError(Exception):
    """Problem finding or downloading the TOSEC pack."""


class Cancelled(Exception):
    """Raised when a cancel token is set during a long operation."""


class Busy(TosecError):
    """Another process holds the DAT update lock."""


_held = threading.local()


@contextlib.contextmanager
def update_lock(blocking: bool = False, directory: Optional[Path] = None):
    """Exclusive cross-process lock (``update.lock`` in the data dir) for DAT updates.

    Re-entrant within a thread. Without ``fcntl`` (Windows) it is a no-op. Raises
    :class:`Busy` when another process holds it (unless ``blocking``).
    """
    depth = getattr(_held, "depth", 0)
    if depth:
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        fcntl = None  # type: ignore[assignment]
    fd = None
    if fcntl is not None:
        try:
            fd = os.open(str((Path(directory) if directory is not None else paths.data_dir()) / "update.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            fd = None  # unwritable data dir: the update itself reports the real error
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except OSError as exc:
                os.close(fd)
                raise Busy("Another copy of the app is updating the DATs") from exc
    _held.depth = 1
    try:
        yield
    finally:
        _held.depth = 0
        if fd is not None:
            os.close(fd)  # closing releases the flock


@dataclass
class ReleaseInfo:
    date: str
    category_url: str
    download_url: str


@dataclass
class DatInfo:
    name: str
    version: str
    path: Path


# --------------------------------------------------------------------------- HTTP

def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})


def default_fetch(url: str, timeout: float = TIMEOUT) -> str:
    """GET a page and decode it as text."""
    with urllib.request.urlopen(_request(url), timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value.strip())


def _links(html: str) -> list[str]:
    parser = _LinkParser()
    parser.feed(html)
    parser.close()
    return parser.links


def parse_release_categories(html: str, base_url: str = DOWNLOADS_URL) -> list[tuple[str, str]]:
    """Return (date, absolute category url) for every dated release, newest first."""
    found: dict[str, str] = {}
    for href in _links(html):
        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.query or parts.fragment:
            continue
        m = _CATEGORY_RE.search(parts.path)
        if not m:
            continue
        date = m.group(2)
        try:
            _dt.date.fromisoformat(date)
        except ValueError:
            continue
        found.setdefault(date, absolute)
    return sorted(found.items(), key=lambda kv: kv[0], reverse=True)


def parse_pack_link(html: str, base_url: str) -> Optional[str]:
    """Find the absolute URL of the 'DAT pack complete' download on a category page."""
    for href in _links(html):
        low = href.lower()
        if "?download=" in low and "dat-pack-complete" in low:
            return urllib.parse.urljoin(base_url, href)
    return None


def find_latest_release(fetch: Fetch = default_fetch) -> ReleaseInfo:
    """Find the newest dated release category and its complete DAT pack link."""
    categories = parse_release_categories(fetch(DOWNLOADS_URL), DOWNLOADS_URL)
    if not categories:
        raise TosecError("No dated release categories found on the TOSEC downloads page")
    # Normally the newest category has the pack; fall back to older ones just in case.
    for date, url in categories[:3]:
        link = parse_pack_link(fetch(url), url)
        if link:
            return ReleaseInfo(date=date, category_url=url, download_url=link)
    raise TosecError(f"No 'DAT Pack - Complete' download found for release {categories[0][0]}")


def check_latest(fetch: Fetch = default_fetch, timeout: Optional[float] = None) -> ReleaseInfo:
    """Check-only: newest release + pack link (a few small page fetches, no pack download).

    ``timeout`` (seconds) applies to the default fetcher; a custom ``fetch`` is used as is.
    """
    if timeout is not None and fetch is default_fetch:
        def fetch(url: str, _t: float = timeout) -> str:  # noqa: F811
            return default_fetch(url, _t)
    return find_latest_release(fetch)


def installed_release(directory: Optional[Path] = None) -> Optional[str]:
    """The installed release date (release.json) - only when at least one .dat exists."""
    folder = Path(directory) if directory is not None else paths.dats_dir()
    data = read_release(folder)
    rel = data.get("release") if data else None
    if not isinstance(rel, str) or not rel:
        return None
    try:
        has_dat = any(p.is_file() and p.name.lower().endswith(".dat") for p in folder.iterdir())
    except OSError:
        return None
    return rel if has_dat else None


def _filename_from_disposition(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # RFC 5987: filename*=UTF-8''...
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.IGNORECASE)
    if m:
        return urllib.parse.unquote(m.group(2).strip().strip('"'), encoding=m.group(1) or "utf-8")
    m = re.search(r'filename\s*=\s*"([^"]+)"', value, re.IGNORECASE) or \
        re.search(r"filename\s*=\s*([^;]+)", value, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _safe_basename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name


def _content_range(resp: Any) -> Optional[tuple[int, int]]:
    """(start, total) from a 206 response's ``Content-Range: bytes start-end/total``."""
    m = re.match(r"\s*bytes\s+(\d+)-\d+/(\d+)\s*$", resp.headers.get("Content-Range") or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def download_pack(info: ReleaseInfo, dest_dir: Union[str, os.PathLike[str]],
                  progress: Optional[ProgressFn] = None,
                  cancel: Optional[CancelToken] = None,
                  opener: Optional[Callable[[urllib.request.Request], Any]] = None) -> Path:
    """Stream the pack zip into dest_dir; return its path.

    Writes to ``<name>.part`` and renames when complete. If a file with the
    final name and the advertised size already exists it is reused. A ``.part``
    left by an interrupted download (network error, app closed) is resumed with
    an HTTP ``Range`` request when the server supports it; it is only deleted on
    cancel. ``opener`` (request -> response) is injectable for tests.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    open_fn = opener or (lambda req: urllib.request.urlopen(req, timeout=TIMEOUT))
    resp = open_fn(_request(info.download_url))
    try:
        status = getattr(resp, "status", 200)
        if status and status >= 400:
            raise TosecError(f"Download failed: HTTP {status}")
        name = _filename_from_disposition(resp.headers.get("Content-Disposition"))
        name = _safe_basename(name or "") or f"TOSEC - DAT Pack - Complete (TOSEC-v{info.date}).zip"
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        target = dest / name
        if total and target.is_file() and target.stat().st_size == total and \
                _has_zip_magic(target):
            if progress:
                progress(total, total)
            return target
        part = target.with_name(target.name + ".part")
        done, mode = 0, "wb"
        have = part.stat().st_size if part.is_file() else 0
        if total and 0 < have < total:
            req = _request(info.download_url)
            req.add_header("Range", f"bytes={have}-")
            ranged = open_fn(req)
            rng = _content_range(ranged) if getattr(ranged, "status", 200) == 206 else None
            if rng == (have, total):
                resp.close()
                resp, done, mode = ranged, have, "ab"
            else:  # server ignored the range: start again with the plain response
                ranged.close()
        elif have > total > 0:
            part.unlink()
        try:
            with part.open(mode) as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            if total and done != total:
                raise TosecError(f"Incomplete download: got {done} of {total} bytes - "
                                 "try again to resume")
            if not _has_zip_magic(part):
                _unlink(part)
                raise TosecError("The download is not a zip file (a login or error page?) - "
                                 "try again later")
            os.replace(part, target)
        except (Cancelled, KeyboardInterrupt):
            _unlink(part)
            raise
    finally:
        resp.close()
    return target


def _has_zip_magic(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- extract

def _dat_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    """(member, flat safe basename) for every .dat under the wanted folders."""
    out: list[tuple[zipfile.ZipInfo, str]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = PurePosixPath(info.filename.replace("\\", "/")).parts
        if len(parts) < 2 or parts[0] not in DAT_FOLDERS or ".." in parts:
            continue
        base = parts[-1]
        if not base.lower().endswith(".dat"):
            continue
        base = _safe_basename(base)
        if base and base not in (".", ".."):
            out.append((info, base))
    return out


def recover_dats_dir(out: Path) -> None:
    """Clean up after an extraction that was killed half-way.

    Removes stale ``.dats-new-*`` temp dirs; if ``out`` is missing because the
    process died between the two renames of the swap, the old DATs are put back.
    """
    try:
        with update_lock(directory=Path(out).parent):
            _recover(Path(out))
    except Busy:
        return  # another process is mid-update: its temp dirs are live


def _recover(out: Path) -> None:
    try:
        siblings = list(out.parent.iterdir())
    except OSError:
        return
    olds = sorted((p for p in siblings if p.name.startswith(".dats-old-") and p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not out.exists() and olds:
        try:
            os.replace(olds.pop(0), out)
        except OSError:
            pass
    for p in siblings:
        if p.name.startswith(".dats-new-") and p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    for p in olds:
        shutil.rmtree(p, ignore_errors=True)


def extract_dats(zip_path: Union[str, os.PathLike[str]], out_dir: Union[str, os.PathLike[str]],
                 progress: Optional[ProgressFn] = None,
                 cancel: Optional[CancelToken] = None,
                 release: Optional[dict[str, Any]] = None,
                 commit_lock: Optional[Any] = None) -> int:
    """Extract all DATs flat into out_dir, replacing its previous contents.

    Extraction goes to a sibling temp dir which is then swapped in, so a
    failure leaves the old DATs intact. ``release`` (if given) is written as
    release.json into the temp dir *before* the swap, so DATs and their release
    marker change atomically. Returns the number of DATs written.
    """
    out = Path(out_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    with update_lock(directory=out.parent):
        return _extract_locked(zip_path, out, progress, cancel, release, commit_lock)


def _extract_locked(zip_path: Any, out: Path, progress: Optional[ProgressFn],
                    cancel: Optional[CancelToken], release: Optional[dict[str, Any]],
                    commit_lock: Optional[Any]) -> int:
    recover_dats_dir(out)
    tmp = Path(tempfile.mkdtemp(prefix=".dats-new-", dir=str(out.parent)))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = _dat_members(zf)
            total = len(members)
            for i, (info, base) in enumerate(members, 1):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                target = tmp / base  # basename only: no zip-slip possible
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                if progress:
                    progress(i, total)
        count = len(list(tmp.glob("*.dat")))
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if count == 0:
            raise TosecError("The downloaded pack contains no TOSEC DATs (layout changed?) - "
                             "the installed DATs were left untouched")
        if release is not None:
            (tmp / RELEASE_FILE).write_text(json.dumps(release, indent=2), encoding="utf-8")
        old: Optional[Path] = None
        with (commit_lock if commit_lock is not None else contextlib.nullcontext()):
            if out.exists():
                old = out.with_name(
                    f".dats-old-{os.getpid()}-{_dt.datetime.now().strftime('%H%M%S%f')}")
                os.replace(out, old)
            os.replace(tmp, out)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return count
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# --------------------------------------------------------------------------- pipeline

def read_release(directory: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Contents of release.json in the dats dir, or None."""
    path = (directory or paths.dats_dir()) / RELEASE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def update_dats(progress: Optional[Callable[[int, int, str], None]] = None,
                cancel: Optional[CancelToken] = None, force: bool = False,
                fetch: Fetch = default_fetch,
                opener: Optional[Callable[[urllib.request.Request], Any]] = None,
                keep_pack: bool = False, info: Optional[ReleaseInfo] = None,
                commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Find, download and extract the latest pack into paths.dats_dir().

    ``info`` (from :func:`check_latest`) skips discovery; the pack is only
    downloaded when ``info.date`` differs from :func:`installed_release` (or
    ``force``). ``commit_lock`` (a context manager) is held only around the
    final directory swap.

    progress(done, total, message). Returns {"release", "count", "skipped"}.
    The downloaded zip is deleted after extraction unless keep_pack is True.
    """
    with update_lock(directory=paths.dats_dir().parent):
        return _update_locked(progress, cancel, force, fetch, opener, keep_pack, info, commit_lock)


def _update_locked(progress: Optional[Callable[[int, int, str], None]],
                   cancel: Optional[CancelToken], force: bool, fetch: Fetch,
                   opener: Optional[Callable[[urllib.request.Request], Any]],
                   keep_pack: bool, info: Optional[ReleaseInfo],
                   commit_lock: Optional[Any]) -> dict[str, Any]:
    def report(done: int, total: int, msg: str) -> None:
        if progress:
            progress(done, total, msg)

    if info is None:
        report(0, 0, "Checking tosecdev.org for the latest release")
        info = find_latest_release(fetch)
    out = paths.dats_dir()
    if not force and installed_release(out) == info.date:
        count = len(list(out.glob("*.dat")))
        report(count, count, f"Release {info.date} already installed")
        return {"release": info.date, "count": count, "skipped": True}

    cache = paths.cache_dir()
    zip_path = download_pack(
        info, cache,
        progress=lambda d, t: report(d, t, f"Downloading TOSEC {info.date}"),
        cancel=cancel, opener=opener)
    release = {"release": info.date, "pack": zip_path.name,
               "downloaded_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    try:
        count = extract_dats(zip_path, out, progress=lambda d, t: report(d, t, "Extracting DATs"),
                             cancel=cancel, release=release, commit_lock=commit_lock)
    except zipfile.BadZipFile as exc:
        _unlink(zip_path)
        raise TosecError("The downloaded pack is corrupt - it will be downloaded again "
                         "on the next update") from exc
    if not keep_pack:
        try:
            zip_path.unlink()
        except OSError:
            pass
    report(count, count, f"Installed {count} DATs from {info.date}")
    return {"release": info.date, "count": count, "skipped": False}


# --------------------------------------------------------------------------- local dats

def parse_dat_filename(filename: str) -> tuple[str, str]:
    """Split a TOSEC DAT filename into (name, version date).

    Files not following the convention give (stem, "").
    """
    m = _DAT_RE.match(filename)
    if m:
        return m.group("name"), m.group("version")
    stem = filename[:-4] if filename.lower().endswith(".dat") else filename
    return stem, ""


def list_dats(directory: Optional[Union[str, os.PathLike[str]]] = None) -> list[DatInfo]:
    """Newest DAT per name in directory (default: dats_dir()), sorted by name."""
    folder = Path(directory) if directory is not None else paths.dats_dir()
    best: dict[str, DatInfo] = {}
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for p in entries:
        if not p.is_file() or not p.name.lower().endswith(".dat"):
            continue
        name, version = parse_dat_filename(p.name)
        cur = best.get(name)
        if cur is None or (version, p.name) > (cur.version, cur.path.name):
            best[name] = DatInfo(name=name, version=version, path=p)
    return sorted(best.values(), key=lambda d: d.name.casefold())


def find_latest_dat(name: str = DEFAULT_DAT_NAME,
                    directory: Optional[Union[str, os.PathLike[str]]] = None) -> Optional[DatInfo]:
    """Newest DAT whose name (part before ' (TOSEC-v') equals name exactly."""
    for info in list_dats(directory):
        if info.name == name:
            return info
    return None
