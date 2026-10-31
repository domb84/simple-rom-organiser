"""Zstandard decompression for CHD ``zstd`` / ``cdzs`` hunks, without a compiled dependency.

The standard library only gained Zstandard in Python 3.14 (``compression.zstd``); before that the shared library
that practically every system has is *loaded* through ctypes, in this order

1. ``compression.zstd`` (Python 3.14+),
2. ``$ROMORG_LIBZSTD`` (a path; tests / development),
3. the copy bundled in the AppImage (``tools/lib``, see :mod:`romorg.bundle`),
4. the system's ``libzstd`` (ctypes' normal search and the usual library folders).

When none is found the pure-Python decoder of :mod:`romorg.zstddec` does the work: correct, but about a megabyte per
second, so :func:`native` is False and the self-check says so. ``ROMORG_NO_ZSTD=1`` forces that fallback.
"""

from __future__ import annotations

import ctypes
import os
from typing import Callable, List, Optional

from . import nativelib, zstddec

__all__ = ["ZstdError", "available", "native", "decompress", "compress", "can_compress", "status", "reload"]

ENV_LIB = "ROMORG_LIBZSTD"
ENV_OFF = "ROMORG_NO_ZSTD"


class ZstdError(Exception):
    """The data is not a valid Zstandard frame of the expected size."""


def _candidates() -> List[str]:
    return nativelib.candidates("libzstd", "zstd", ENV_LIB, names=("libzstd.so.1", "libzstd.so", "libzstd.1.dylib"))


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

    decode.compress = lambda data, level: _z.compress(data, level)      # type: ignore[attr-defined]
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

    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compress.restype = ctypes.c_size_t
    lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]

    def compress(data: bytes, level: int) -> bytes:
        cap = lib.ZSTD_compressBound(len(data))
        buf = ctypes.create_string_buffer(cap)
        n = lib.ZSTD_compress(buf, cap, bytes(data), len(data), level)   # releases the GIL
        if lib.ZSTD_isError(n):
            raise ZstdError((lib.ZSTD_getErrorName(n) or b"error").decode("ascii", "replace"))
        return buf.raw[:n]

    decode.compress = compress      # type: ignore[attr-defined]
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


_once = nativelib.Once(lambda: _find())          # (decoder, where it comes from, note)


def _load() -> Optional[Callable[[bytes, int], bytes]]:
    return _once.get()[0]


def native() -> bool:
    """True when a Zstandard library does the decoding (fast); False = the pure-Python decoder."""
    return _load() is not None


def available() -> bool:
    """Zstandard hunks can always be decoded (see :func:`native` for how fast)."""
    return True


def status() -> dict:
    """``{"available": True, "native": bool, "library": "compression.zstd" | path | None, "note": why not
    native}``."""
    decode, source, note = _once.get()
    return {"available": True, "native": decode is not None, "library": source, "note": note}


def reload() -> None:
    """Forget what was loaded (tests that change the environment)."""
    _once.reset()


def can_compress() -> bool:
    """Compressing needs a library (there is no pure-Python Zstandard *encoder*)."""
    return _load() is not None


def compress(data: bytes, level: int = 3) -> bytes:
    """One Zstandard frame holding ``data`` (what chdman's ``zstd`` / ``cdzs`` codecs store)."""
    fn = _load()
    if fn is None:
        raise ZstdError(_once.get()[2] or "no Zstandard library")
    return fn.compress(data, level)     # type: ignore[attr-defined]


def decompress(data: bytes, size: int) -> bytes:
    """The ``size`` bytes of one Zstandard frame (:class:`ZstdError` when it is damaged or of another size)."""
    out = (_load() or _python)(data, size)
    if len(out) != size:
        raise ZstdError(f"decoded {len(out)} bytes, expected {size}")
    return out
