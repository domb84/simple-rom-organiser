"""No-Intro DATs from the libretro-database mirror on GitHub (clrmamepro format).

The DATs are small (one file per console), stored in ``paths.nointro_dir()`` -
deliberately *not* in the TOSEC ``dats/`` folder, which is swapped atomically
whenever a TOSEC pack is extracted. ``manifest.json`` there remembers each
file's ETag so unchanged DATs are not downloaded again.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Optional

from . import datsource, paths
from .datsource import CHUNK, MANIFEST, TIMEOUT, USER_AGENT, Opener, PathLike, ProgressFn   # noqa: F401  (this module's names for them)
from .tosec import CancelToken, DatInfo

BASE_URL = "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/no-intro/"
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
# Redump's GameCube and Wii lists as libretro's mirror has them (clrmamepro text, with each disc's serial; no betas, prototypes or demos
# the mirror left out): downloaded here, with the No-Intro DATs, from the mirror's own folder. The platforms' source stays "redump".
MIRRORED_REDUMP = ("Nintendo - GameCube", "Nintendo - Wii")
REDUMP_MIRROR_URL = "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/redump/"
# manifest.json: {name: {"version", "etag", "sha1", "size", "url", "downloaded_at"}}

_VERSION_RE = re.compile(rb'\bversion\s+"([^"]*)"')


class NoIntroError(Exception):
    """Problem downloading or validating a No-Intro DAT."""


def _dir(directory: Optional[PathLike]) -> Path:
    return Path(directory) if directory is not None else paths.nointro_dir()


def dat_url(name: str) -> str:
    return (REDUMP_MIRROR_URL if name in MIRRORED_REDUMP else BASE_URL) + urllib.parse.quote(name + ".dat")


def all_dat_names() -> tuple:
    """Every DAT this module keeps current: the No-Intro ones and the mirrored Redump ones."""
    return tuple(NOINTRO_DATS) + MIRRORED_REDUMP


def header_version(path: PathLike) -> str:
    """``version "..."`` from the first 4 KiB of a clrmamepro DAT ("" if absent/unreadable)."""
    return datsource.header_version(path, _VERSION_RE)


def read_manifest(directory: Optional[PathLike] = None) -> dict[str, Any]:
    return datsource.read_manifest(_dir(directory))


def list_dats(directory: Optional[PathLike] = None) -> list[DatInfo]:
    """Every ``<name>.dat`` in the No-Intro folder, sorted by name (version from manifest/header)."""
    return datsource.list_dats(_dir(directory), _VERSION_RE, header_first=False)


def find_dat(name: str, directory: Optional[PathLike] = None) -> Optional[DatInfo]:
    return datsource.find_dat(name, list_dats(directory))


def download_dat(name: str, directory: Optional[PathLike] = None,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 force: bool = False, opener: Optional[Opener] = None,
                 commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Download ``<name>.dat`` from the libretro mirror (conditional on the stored ETag).

    Returns ``{"name", "version", "status": "downloaded" | "unchanged"}``. Network, HTTP and
    validation problems raise :class:`NoIntroError` and leave an existing file untouched;
    a cancel raises :class:`tosec.Cancelled`.
    """
    return datsource.download_etag(name, _dir(directory), dat_url(name), NoIntroError, _VERSION_RE, False,
                                   progress, cancel, force, opener, commit_lock)


def check_updates(names: Optional[Iterable[str]] = None, opener: Optional[Opener] = None,
                  directory: Optional[PathLike] = None, timeout: float = 10) -> list[dict[str, Any]]:
    """Check-only: compare each DAT's remote ETag (HEAD) with the stored manifest (see ``datsource.check_etag``)."""
    return datsource.check_etag(list(names) if names is not None else list(NOINTRO_DATS), _dir(directory), dat_url, find_dat,
                                opener, timeout)


def update_dats(names: Optional[Iterable[str]] = None, progress: Optional[ProgressFn] = None,
                cancel: Optional[CancelToken] = None, force: bool = False,
                opener: Optional[Opener] = None,
                directory: Optional[PathLike] = None,
                commit_lock: Optional[Any] = None) -> dict[str, Any]:
    """Download/refresh the No-Intro DATs (default: all of :data:`NOINTRO_DATS`).

    Returns ``{"source": "nointro", "dats": [...], "downloaded", "unchanged", "failed", "count"}``.
    Raises :class:`NoIntroError` only when every DAT failed and none is present locally.
    """
    return datsource.update_all(list(names) if names is not None else list(NOINTRO_DATS), _dir(directory), download_dat,
                                find_dat, list_dats, NoIntroError, "nointro", "GitHub", "No-Intro DATs", progress, cancel,
                                numbered=True, force=force, opener=opener, commit_lock=commit_lock)
