"""Per-user data / config locations (portable across Linux, Windows, macOS).

Set ``ROMORG_DATA_DIR`` to override the data directory (used by tests and for
portable installs).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

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


def load_config() -> dict[str, Any]:
    """Load config.json; returns {} if missing or unreadable."""
    try:
        with config_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(config: dict[str, Any]) -> None:
    """Atomically write config.json."""
    target = config_path()
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
