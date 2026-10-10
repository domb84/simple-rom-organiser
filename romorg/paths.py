"""Per-user data / config locations (portable across Linux, Windows, macOS).

Set ``ROMORG_DATA_DIR`` to override the data directory (used by tests and for
portable installs).
"""

from __future__ import annotations

import json
import os
import sys
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

APP_NAME = "simple-rom-organiser"


def _base_data_dir() -> Path:
    override = os.environ.get("ROMORG_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Local"
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "share"
    return base / APP_NAME


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    """Application data directory, created on demand."""
    return _ensure(_base_data_dir())


def dats_dir() -> Path:
    """Directory holding the extracted (flat) .dat files."""
    return _ensure(data_dir() / "dats")


def whdload_dir() -> Path:
    """Directory holding the WHDLoad DAT (MrV2K's community database) + manifest.json.

    Its own sibling of ``dats/`` and ``nointro/``: neither the TOSEC pack's atomic swap nor a
    No-Intro update ever touches it (and it never touches them).
    """
    return _ensure(data_dir() / "whdload")


def redump_dir() -> Path:
    """Directory holding the Redump DATs (Sega Dreamcast) + manifest.json.

    Its own sibling of ``dats/``, ``nointro/`` and ``whdload/``: no other updater ever touches it.
    """
    return _ensure(data_dir() / "redump")


def ratings_dir() -> Path:
    """Directory holding the LaunchBox ratings index (``ratings.sqlite`` + manifest.json).

    Its own sibling of ``dats/``, ``nointro/``, ``whdload/`` and ``redump/``: no DAT updater ever touches it.
    """
    return _ensure(data_dir() / "ratings")


def nointro_dir() -> Path:
    """Directory holding the No-Intro DATs (libretro mirror) + manifest.json.

    A sibling of ``dats/`` so the TOSEC pack's atomic swap of that folder never touches it.
    """
    return _ensure(data_dir() / "nointro")


def cache_dir() -> Path:
    """Directory for the downloaded pack zip and the hash cache."""
    return _ensure(data_dir() / "cache")


def updates_path() -> Path:
    """JSON file remembering the last DAT update check (``checked_at``, per-source ``latest``)."""
    return data_dir() / "updates.json"


def offline_forced() -> bool:
    """True when ``ROMORG_OFFLINE=1`` (tests, smoke runs): never touch the network."""
    return os.environ.get("ROMORG_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def config_path() -> Path:
    return data_dir() / "config.json"


# Every write to config.json goes through update_config(): one process-wide lock, and the
# modification is always applied to the LATEST file content (read-modify-write inside the lock),
# so two threads (a scan finishing while the user saves a folder or a library profile) can never
# overwrite each other's keys with a stale snapshot.
_CONFIG_LOCK = threading.RLock()


def _read_config_file() -> tuple[dict[str, Any], bool]:
    """``(content, damaged)``: damaged = the file exists but is not a JSON object."""
    try:
        with config_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return {}, True
    if isinstance(data, dict):
        return data, False
    return {}, True


def load_config() -> dict[str, Any]:
    """Load config.json; returns {} if missing or unreadable."""
    with _CONFIG_LOCK:
        return _read_config_file()[0]


def _write_config(config: dict[str, Any]) -> None:
    target = config_path()
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_config(mutate: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    """Atomically ``mutate(latest config)`` and save it; returns the saved config.

    ``mutate`` edits the dict in place (its return value is ignored). The lock is held for the
    whole read-modify-write. A config.json that exists but cannot be parsed is first copied to
    ``config.json.damaged`` so a write never silently destroys what the user had.
    """
    with _CONFIG_LOCK:
        cfg, damaged = _read_config_file()
        if damaged:
            try:
                shutil.copy2(config_path(), config_path().with_name("config.json.damaged"))
            except OSError:
                pass
        mutate(cfg)
        _write_config(cfg)
        return cfg


def save_config(config: dict[str, Any]) -> None:
    """Atomically replace config.json with ``config`` (prefer :func:`update_config`: this overwrites
    keys other writers may have set since ``config`` was loaded)."""
    with _CONFIG_LOCK:
        _write_config(config)


def scans_dir() -> Path:
    """Directory holding the saved scan of each system (``scancache``)."""
    d = data_dir() / "scans"
    d.mkdir(parents=True, exist_ok=True)
    return d
