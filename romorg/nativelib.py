"""Finding and loading an optional native library (libFLAC, libsndfile, libzstd) with ctypes: the search every one of them does, and
the "try once, on first use, from any thread" that goes with it.

Where a library may come from, first hit wins: an environment variable naming it, the folder the app ships its libraries in
(:func:`bundle.lib_dir`), the system's own (``ctypes.util.find_library``, well-known names, the usual library folders,
including the host's inside a flatpak). Nothing is required: a caller that finds none uses its pure-Python fallback.
"""

from __future__ import annotations

import ctypes.util
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from . import bundle

__all__ = ["app_folders", "candidates", "Once", "SYSTEM_LIB_DIRS"]

SYSTEM_LIB_DIRS = ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/lib64", "/usr/local/lib",
                   "/run/host/usr/lib", "/run/host/usr/lib64")


def app_folders() -> List[Path]:
    """Where a library shipped with the app may sit: the start script's folder (the current one when there is none), and the folder
    above the package."""
    return [Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd(), Path(__file__).resolve().parent.parent]


def candidates(soname: str, find: str, env: Optional[str] = None, names: Sequence[str] = ()) -> List[str]:
    """What to try loading, in order, without duplicates. ``soname``: the library's file stem (``libFLAC``): ``<soname>.so*`` is looked
    for in the bundled folder and ``<soname>.so.*`` in the system's. ``find``: the name ``ctypes.util.find_library`` takes (``FLAC``).
    ``env``: the variable that names a library. ``names``: well-known names to hand to the loader (``libFLAC.so.14`` ...)."""
    out: List[str] = []
    named = os.environ.get(env) if env else None
    if named:
        out.append(named)
    shipped = bundle.lib_dir()
    if shipped is not None:
        try:
            out += sorted(str(p) for p in shipped.glob(f"{soname}.so*") if p.is_file() or p.is_symlink())
        except OSError:
            pass
    found = ctypes.util.find_library(find)
    if found:
        out.append(found)
    out += list(names)
    for folder in SYSTEM_LIB_DIRS:
        try:
            out += sorted(str(p) for p in Path(folder).glob(f"{soname}.so.*"))
        except OSError:
            pass
    return list(dict.fromkeys(out))


class Once:
    """``find()`` run once, on first use, whichever thread asks first; its result is kept until :meth:`reset`. The result is set
    before ``tried``: another thread never sees "tried" with no result."""

    def __init__(self, find: Callable[[], Any]) -> None:
        self._find = find
        self._lock = threading.Lock()
        self._tried = False
        self._result: Any = None

    def get(self) -> Any:
        if self._tried:
            return self._result
        with self._lock:
            if not self._tried:
                self._result = self._find()
                self._tried = True
            return self._result

    def reset(self) -> None:
        with self._lock:
            self._tried, self._result = False, None
