"""The "Commodore - Amiga - WHDLoad" DAT (MrV2K's community database, clrmamepro format).

Every entry of that DAT is one Retroplay pre-installed ``.lha`` archive with the size / crc / md5 /
sha1 of the WHOLE archive file. Only this single DAT (a ~1 MB text file with hashes) is ever
downloaded - never any game file. The repository states no licence.

Source: https://github.com/MrV2K/WHDLoad-Database. The DAT lives in ``paths.whdload_dir()`` - its own
folder, deliberately separate from the TOSEC ``dats/`` folder (swapped atomically on every pack
update) and from ``nointro/``: nothing here is shared with, or cross-matched against, the TOSEC
Amiga system. ``manifest.json`` remembers the ETag so an unchanged DAT is not downloaded again; the
version shown to users is the DAT header ``date`` (e.g. ``2026-07-05``).

The API mirrors :mod:`romorg.nointro` (``check_updates`` / ``download_dat`` / ``update_dats`` /
``list_dats`` / ``find_dat``), so :mod:`romorg.autoupdate` drives it the same way.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Optional

from . import datsource, paths
from .datsource import MANIFEST, USER_AGENT, Opener, PathLike, ProgressFn   # noqa: F401  (this module's names for them)
from .tosec import CancelToken, DatInfo

DAT_NAME = "Commodore - Amiga - WHDLoad"          # header name == file stem == the platform's DAT name
BASE_URL = "https://raw.githubusercontent.com/MrV2K/WHDLoad-Database/main/"
WHDLOAD_DATS = (DAT_NAME,)
# manifest.json: {name: {"version", "etag", "sha1", "size", "url", "downloaded_at"}}

_DATE_RE = re.compile(rb'\b(?:date|version)\s+"([^"]*)"')


class WhdloadError(Exception):
    """Problem downloading or validating the WHDLoad DAT."""


def _dir(directory: Optional[PathLike]) -> Path:
    return Path(directory) if directory is not None else paths.whdload_dir()


def dat_url(name: str = DAT_NAME) -> str:
    return BASE_URL + urllib.parse.quote(name + ".dat")


def header_version(path: PathLike) -> str:
    """The header ``date "..."`` (or ``version``) of the DAT ("" if absent/unreadable)."""
    return datsource.header_version(path, _DATE_RE)


def read_manifest(directory: Optional[PathLike] = None) -> dict[str, Any]:
    return datsource.read_manifest(_dir(directory))


def list_dats(directory: Optional[PathLike] = None) -> list[DatInfo]:
    """Every ``<name>.dat`` in the WHDLoad folder, sorted by name (version = header date)."""
    return datsource.list_dats(_dir(directory), _DATE_RE, header_first=True)


def find_dat(name: str = DAT_NAME, directory: Optional[PathLike] = None) -> Optional[DatInfo]:
    return datsource.find_dat(name, list_dats(directory))


def download_dat(name: str = DAT_NAME, directory: Optional[PathLike] = None,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 force: bool = False, opener: Optional[Opener] = None,
                 commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Download ``<name>.dat`` (conditional on the stored ETag).

    Returns ``{"name", "version", "status": "downloaded" | "unchanged"}``. Network, HTTP and validation
    problems raise :class:`WhdloadError` and leave an existing file untouched; a cancel raises
    :class:`tosec.Cancelled`. The file is written to ``<name>.dat.part``, parsed (validated) and
    only then atomically replaced.
    """
    return datsource.download_etag(name, _dir(directory), dat_url(name), WhdloadError, _DATE_RE, True,
                                   progress, cancel, force, opener, commit_lock)


def check_updates(names: Optional[Iterable[str]] = None, opener: Optional[Opener] = None,
                  directory: Optional[PathLike] = None, timeout: float = 10) -> list[dict[str, Any]]:
    """Check-only: compare the remote ETag (HEAD) with the stored manifest (see ``datsource.check_etag``)."""
    return datsource.check_etag(list(names) if names is not None else list(WHDLOAD_DATS), _dir(directory), dat_url, find_dat,
                                opener, timeout)


def update_dats(names: Optional[Iterable[str]] = None, progress: Optional[ProgressFn] = None,
                cancel: Optional[CancelToken] = None, force: bool = False,
                opener: Optional[Opener] = None,
                directory: Optional[PathLike] = None,
                commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Download/refresh the WHDLoad DAT (default: :data:`WHDLOAD_DATS`).

    Returns ``{"source": "whdload", "dats": [...], "downloaded", "unchanged", "failed", "count"}``.
    Raises :class:`WhdloadError` only when every DAT failed and none is present locally.
    """
    return datsource.update_all(list(names) if names is not None else list(WHDLOAD_DATS), _dir(directory), download_dat,
                                find_dat, list_dats, WhdloadError, "whdload", "GitHub", "WHDLoad DAT", progress, cancel,
                                force=force, opener=opener, commit_lock=commit_lock)
