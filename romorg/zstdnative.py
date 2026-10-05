"""Zstandard decompression for CHD ``zstd`` / ``cdzs`` hunks, without a compiled dependency.

The standard library only gained Zstandard in Python 3.14 (``compression.zstd``); before that the shared library
that practically every system has is *loaded* through ctypes, in this order

1. ``compression.zstd`` (Python 3.14+),
2. ``$ROMORG_LIBZSTD`` (a path; tests / development),
3. the copy bundled in the AppImage (``tools/lib``, see :mod:`romorg.bundle`),
4. on Windows a ``libzstd.dll`` / ``zstd.dll`` next to the app or in its ``native`` folder,
5. the system's ``libzstd`` (ctypes' normal search and the usual library folders).

When none is found the pure-Python decoder of :mod:`romorg.zstddec` does the work: correct, but about a megabyte per
second, so :func:`native` is False and the self-check says so. ``ROMORG_NO_ZSTD=1`` forces that fallback.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading
from pathlib import Path
from typing import Callable, List, Optional

from . import bundle, zstddec

__all__ = ["ZstdError", "available", "native", "decompress", "status", "reload"]

ENV_LIB = "ROMORG_LIBZSTD"
ENV_OFF = "ROMORG_NO_ZSTD"


class ZstdError(Exception):
    """The data is not a valid Zstandard frame of the expected size."""


_lock = threading.Lock()
_tried = False
_decode: Optional[Callable[[bytes, int], bytes]] = None
_source: Optional[str] = None
_note = ""


def _candidates() -> List[str]:
    out: List[str] = []
    env = os.environ.get(ENV_LIB)
    if env:
        out.append(env)
    d = bundle.lib_dir()
    if d is not None:
        try:
            out += sorted(str(p) for p in d.glob("libzstd.so*") if p.is_file() or p.is_symlink())
        except OSError:
            pass
    if sys.platform.startswith("win"):
        folders = []
        if getattr(sys, "frozen", False):
            if getattr(sys, "_MEIPASS", None):
                folders.append(Path(sys._MEIPASS))
            folders.append(Path(sys.executable).resolve().parent)
        folders += [Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd(),
                    Path(__file__).resolve().parent.parent]
        for folder in folders:
            for sub in ("", "native"):
                for name in ("libzstd.dll", "zstd.dll"):
                    cand = folder / sub / name if sub else folder / name
                    try:
                        if cand.is_file():
                            out.append(str(cand))
                    except OSError:
                        pass
        out += ["libzstd.dll", "zstd.dll"]
    found = ctypes.util.find_library("zstd")
    if found:
        out.append(found)
    out += ["libzstd.so.1", "libzstd.so", "libzstd.1.dylib"]
    for folder in ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/lib64", "/usr/local/lib",
                   "/run/host/usr/lib", "/run/host/usr/lib64"):
        try:
            out += sorted(str(p) for p in Path(folder).glob("libzstd.so.*"))
        except OSError:
            pass
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _stdlib() -> Optional[Callable[[bytes, int], bytes]]:
    try:
        from compression import zstd as _z          # Python 3.14+
    except ImportError:
        return None

    def decode(data: bytes, size: int) -> bytes:
        try:
            return _z.decompress(data)
        except Exception as exc:  # noqa: BLE001 - ZstdError and friends
            raise ZstdError(str(exc)) from exc

    return decode


def _ctypes(path: str) -> Callable[[bytes, int], bytes]:
    lib = ctypes.CDLL(path)
    lib.ZSTD_decompress.restype = ctypes.c_size_t
    lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_getErrorName.restype = ctypes.c_char_p
    lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]

    def decode(data: bytes, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        n = lib.ZSTD_decompress(buf, size, bytes(data), len(data))      # the call releases the GIL
        if lib.ZSTD_isError(n):
            raise ZstdError((lib.ZSTD_getErrorName(n) or b"error").decode("ascii", "replace"))
        return buf.raw[:n]

    return decode


def _python(data: bytes, size: int) -> bytes:
    try:
        return zstddec.decompress(data, size)
    except zstddec.ZstdDecodeError as exc:
        raise ZstdError(str(exc)) from exc


def _find() -> tuple:
    """``(decoder, where it comes from, note)`` of the best library; ``(None, None, why)`` without one."""
    if os.environ.get(ENV_OFF):
        return None, None, f"disabled ({ENV_OFF})"
    fn = _stdlib()
    if fn is not None:
        return fn, "compression.zstd", ""
    errors = []
    for cand in _candidates():
        try:
            return _ctypes(cand), cand, ""
        except (OSError, AttributeError) as exc:
            errors.append(f"{cand}: {exc}")
    return None, None, "libzstd not found" + (f" ({errors[0]})" if errors else "")


def _load() -> Optional[Callable[[bytes, int], bytes]]:
    global _tried, _decode, _source, _note
    if _tried:
        return _decode
    with _lock:
        if not _tried:
            _decode, _source, _note = _find()
            _tried = True               # last: another thread must never see "tried" before the result is there
        return _decode


def native() -> bool:
    """True when a Zstandard library does the decoding (fast); False = the pure-Python decoder."""
    return _load() is not None


def available() -> bool:
    """Zstandard hunks can always be decoded (see :func:`native` for how fast)."""
    return True


def status() -> dict:
    """``{"available": True, "native": bool, "library": "compression.zstd" | path | None, "note": why not
    native}``."""
    _load()
    return {"available": True, "native": _decode is not None, "library": _source, "note": _note}


def reload() -> None:
    """Forget what was loaded (tests that change the environment)."""
    global _tried, _decode, _source, _note
    with _lock:
        _tried, _decode, _source, _note = False, None, None, ""


def decompress(data: bytes, size: int) -> bytes:
    """The ``size`` bytes of one Zstandard frame (:class:`ZstdError` when it is damaged or of another size)."""
    out = (_load() or _python)(data, size)
    if len(out) != size:
        raise ZstdError(f"decoded {len(out)} bytes, expected {size}")
    return out
