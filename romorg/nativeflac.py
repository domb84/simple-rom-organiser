"""Optional native FLAC decoding for CHD ``cdfl`` hunks, through libsndfile (which contains libFLAC) and ctypes.

The pure-Python decoder in :mod:`romorg.flacdec` manages ~4 MB/s per process; libFLAC is ~140 MB/s per thread and
the calls release the GIL, so the CHD reader's decode threads scale with the cores. Nothing is required: if no
libsndfile can be loaded, or a hunk is not decoded exactly, the caller uses the Python decoder.

Where the library comes from (first hit wins): ``ROMORG_SNDFILE`` (a path), ``libsndfile*`` next to the app / in a
``native`` folder beside it, then the system's (``libsndfile.so.1`` on Linux, ``libsndfile-1.dll`` on Windows).
``ROMORG_NATIVE_FLAC=0`` turns it off. libsndfile is LGPL-2.1+ and is only ever loaded dynamically.

CHD stores each ``cdfl`` hunk as bare FLAC frames (no ``fLaC`` marker or STREAMINFO), so a minimal stream header is
put in front. libsndfile does not report where the last frame ended, so this path serves the sector data only; the
subcode that follows the FLAC frames in a hunk still goes through the Python decoder.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import itertools
import os
import struct
import sys
import threading
from array import array
from pathlib import Path
from typing import Optional

ENV_LIB = "ROMORG_SNDFILE"
ENV_ENABLE = "ROMORG_NATIVE_FLAC"

_i64 = ctypes.c_int64
_GETLEN = ctypes.CFUNCTYPE(_i64, ctypes.c_void_p)
_SEEK = ctypes.CFUNCTYPE(_i64, _i64, ctypes.c_int, ctypes.c_void_p)
_READ = ctypes.CFUNCTYPE(_i64, ctypes.c_void_p, _i64, ctypes.c_void_p)
_WRITE = ctypes.CFUNCTYPE(_i64, ctypes.c_void_p, _i64, ctypes.c_void_p)
_TELL = ctypes.CFUNCTYPE(_i64, ctypes.c_void_p)
SFM_READ = 0x10


class _VirtualIO(ctypes.Structure):
    _fields_ = [("get_filelen", _GETLEN), ("seek", _SEEK), ("read", _READ), ("write", _WRITE), ("tell", _TELL)]


class _SfInfo(ctypes.Structure):
    _fields_ = [("frames", _i64), ("samplerate", ctypes.c_int), ("channels", ctypes.c_int),
                ("format", ctypes.c_int), ("sections", ctypes.c_int), ("seekable", ctypes.c_int)]


class _Source:
    """The bytes libsndfile reads for one decode (kept in :data:`_SOURCES` under a key passed as user_data)."""
    __slots__ = ("buf", "addr", "size", "pos")

    def __init__(self, data: bytes) -> None:
        self.buf = ctypes.create_string_buffer(data, len(data))
        self.addr = ctypes.addressof(self.buf)
        self.size = len(data)
        self.pos = 0


_SOURCES: dict = {}
_keys = itertools.count(1)


def _getlen(key) -> int:
    return _SOURCES[key].size


def _seek(offset, whence, key) -> int:
    s = _SOURCES[key]
    s.pos = offset if whence == 0 else s.pos + offset if whence == 1 else s.size + offset
    s.pos = max(0, min(s.pos, s.size))
    return s.pos


def _read(ptr, count, key) -> int:
    s = _SOURCES[key]
    n = max(0, min(count, s.size - s.pos))
    if n:
        ctypes.memmove(ptr, s.addr + s.pos, n)
        s.pos += n
    return n


def _write(ptr, count, key) -> int:
    return 0


def _tell(key) -> int:
    return _SOURCES[key].pos


# the callbacks are created once (creating them per hunk would cost more than the decode)
_CALLBACKS = _VirtualIO(_GETLEN(_getlen), _SEEK(_seek), _READ(_read), _WRITE(_write), _TELL(_tell))

_lib: Optional[ctypes.CDLL] = None
_tried = False
_lock = threading.Lock()


def _candidates():
    env = os.environ.get(ENV_LIB)
    if env:
        yield env
    names = ("libsndfile-1.dll", "libsndfile_x64.dll", "libsndfile.dll", "libsndfile.so.1", "libsndfile.so",
             "libsndfile.dylib")
    folders = []
    if getattr(sys, "frozen", False):
        if getattr(sys, "_MEIPASS", None):       # a PyInstaller onefile exe unpacks bundled binaries here
            folders.append(Path(sys._MEIPASS))
        folders.append(Path(sys.executable).resolve().parent)
    folders += [Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd(),
                Path(__file__).resolve().parent.parent]
    for f in folders:
        for sub in ("", "native", "_soundfile_data"):
            for n in names:
                p = f / sub / n if sub else f / n
                if p.is_file():
                    yield str(p)
    found = ctypes.util.find_library("sndfile")
    if found:
        yield found


def _load() -> Optional[ctypes.CDLL]:
    for cand in _candidates():
        try:
            lib = ctypes.CDLL(cand)
            lib.sf_open_virtual.restype = ctypes.c_void_p
            lib.sf_open_virtual.argtypes = [ctypes.POINTER(_VirtualIO), ctypes.c_int, ctypes.POINTER(_SfInfo),
                                            ctypes.c_void_p]
            lib.sf_readf_short.restype = _i64
            lib.sf_readf_short.argtypes = [ctypes.c_void_p, ctypes.c_void_p, _i64]
            lib.sf_close.argtypes = [ctypes.c_void_p]
            lib.sf_close.restype = ctypes.c_int
            try:
                lib.sf_open_fd.restype = ctypes.c_void_p
                lib.sf_open_fd.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_SfInfo), ctypes.c_int]
            except AttributeError:
                pass
            return lib
        except (OSError, AttributeError):
            continue
    return None


def _get() -> Optional[ctypes.CDLL]:
    global _lib, _tried
    if not _tried:
        with _lock:
            if not _tried:
                if os.environ.get(ENV_ENABLE, "1") != "0":
                    _lib = _load()
                _tried = True
    return _lib


def available() -> bool:
    return _get() is not None


def _stream_header(samples_per_channel: int) -> bytes:
    info = struct.pack(">HH", 16, 65535) + bytes(6)                       # block sizes; frame sizes unknown
    info += ((44100 << 44) | (1 << 41) | (15 << 36) | samples_per_channel).to_bytes(8, "big") + bytes(16)
    return b"fLaC\x80" + (34).to_bytes(3, "big") + info                   # one STREAMINFO block, the last


class NativeFlacError(Exception):
    pass


def decode_frames(data: bytes, samples_per_channel: int) -> array:
    """The first ``samples_per_channel`` stereo 16-bit samples of the FLAC frames in ``data`` (as ``array('h')``,
    interleaved, native byte order - the same values :func:`romorg.flacdec.decode_frames` returns)."""
    lib = _get()
    if lib is None:
        raise NativeFlacError("libsndfile is not available")
    key = next(_keys)
    _SOURCES[key] = _Source(_stream_header(samples_per_channel) + bytes(data))
    handle = None
    try:
        info = _SfInfo()
        handle = lib.sf_open_virtual(ctypes.byref(_CALLBACKS), SFM_READ, ctypes.byref(info), ctypes.c_void_p(key))
        if not handle:
            raise NativeFlacError("libsndfile could not open the hunk")
        out = array("h", bytes(samples_per_channel * 4))
        got = lib.sf_readf_short(handle, out.buffer_info()[0], samples_per_channel)
        if got != samples_per_channel or info.channels != 2:
            raise NativeFlacError(f"libsndfile decoded {got} of {samples_per_channel} samples")
        return out
    finally:
        if handle:
            lib.sf_close(handle)
        _SOURCES.pop(key, None)


# --------------------------------------------------------------------------- without callbacks (Linux)
# sf_open_virtual calls back into Python for every read, which holds the GIL: decode threads then queue up behind
# each other. Handing libsndfile a file descriptor instead keeps the whole decode in C. The "file" is an anonymous
# memory file (memfd), one per thread, rewritten for every hunk - nothing touches a disk.
_local = threading.local()


def fd_available() -> bool:
    """True when hunks can be decoded without any callback (libsndfile with ``sf_open_fd`` and Linux memfds)."""
    lib = _get()
    return lib is not None and hasattr(os, "memfd_create") and hasattr(lib, "sf_open_fd") \
        and getattr(lib.sf_open_fd, "argtypes", None) is not None


def decode_frames_fd(data: bytes, samples_per_channel: int) -> array:
    """Like :func:`decode_frames`, through a memory file: no Python runs while libsndfile decodes, so several
    threads really decode at once."""
    lib = _get()
    if lib is None or not fd_available():
        raise NativeFlacError("libsndfile cannot read from a memory file here")
    st = _local.__dict__
    fd = st.get("fd")
    if fd is None:
        try:
            fd = st["fd"] = os.memfd_create("romorg-flac")
        except OSError as exc:
            raise NativeFlacError(f"no memory file: {exc}") from exc
        st["info"] = _SfInfo()
    stream = _stream_header(samples_per_channel) + bytes(data)
    try:
        os.pwrite(fd, stream, 0)
        os.ftruncate(fd, len(stream))
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise NativeFlacError(f"memory file: {exc}") from exc
    info = st["info"]
    info.format = 0
    handle = lib.sf_open_fd(fd, SFM_READ, ctypes.byref(info), 0)
    if not handle:
        raise NativeFlacError("libsndfile could not open the hunk")
    try:
        out = array("h", bytes(samples_per_channel * 4))
        got = lib.sf_readf_short(handle, out.buffer_info()[0], samples_per_channel)
    finally:
        lib.sf_close(handle)
    if got != samples_per_channel or info.channels != 2:
        raise NativeFlacError(f"libsndfile decoded {got} of {samples_per_channel} samples")
    return out
