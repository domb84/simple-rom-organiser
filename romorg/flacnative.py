"""Native FLAC for CHD ``cdfl`` hunks: a ctypes binding to libFLAC's stream decoder.

The pure-Python decoder (:mod:`romorg.flacdec`) runs at about 1 MB/s; libFLAC does 100+ MB/s per core, which is what
turns a CD-audio-heavy disc from minutes into seconds. Nothing is compiled: the shared library is *loaded*, in this
order

1. ``$ROMORG_LIBFLAC`` (a path; tests / development),
2. the copy bundled in the AppImage (``tools/lib``, see :mod:`romorg.bundle`),
3. the system's ``libFLAC.so.*`` (ctypes' normal search, ``ldconfig``, the usual library folders),

and when none loads, :func:`decode_frames` is simply the pure-Python decoder (same results, slow). When only the
audio of a hunk is wanted (no subcode), libsndfile is asked first if it can read from a memory file
(:func:`romorg.nativeflac.decode_frames_fd`, Linux): that path never calls back into Python. The libFLAC decoder is
fed exactly like libchdr does it: a synthesised ``fLaC`` + STREAMINFO header (44.1 kHz, 2 channels, 16 bit) followed
by the FLAC *frames* of the hunk; decoding stops after the hunk's sample count and the position tells where the
zlib-compressed subcode starts.

Results are bit-identical to :func:`romorg.flacdec.decode_frames`; unlike that one libFLAC also checks the
frame CRCs, so damaged audio raises :class:`FlacError` instead of decoding to garbage.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
import threading
from array import array
from pathlib import Path
from typing import List, Optional, Tuple

from . import bundle, flacdec, nativeflac
from .flacdec import FlacError

__all__ = ["FlacError", "available", "library_path", "decode_frames", "decode_pcm", "status", "reload"]

ENV_LIB = "ROMORG_LIBFLAC"
_BIG = sys.byteorder == "big"

_lock = threading.Lock()
_lib = None
_lib_path: Optional[str] = None
_tried = False
_note = ""

_VOID = ctypes.c_void_p
_READ = ctypes.CFUNCTYPE(ctypes.c_int, _VOID, ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_size_t), _VOID)
_TELL = ctypes.CFUNCTYPE(ctypes.c_int, _VOID, ctypes.POINTER(ctypes.c_uint64), _VOID)
_WRITE = ctypes.CFUNCTYPE(ctypes.c_int, _VOID, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.POINTER(ctypes.c_int32)), _VOID)
_ERROR = ctypes.CFUNCTYPE(None, _VOID, ctypes.c_int, _VOID)

_ST_END_OF_STREAM = 4


def _candidates() -> List[str]:
    out: List[str] = []
    env = os.environ.get(ENV_LIB)
    if env:
        out.append(env)
    d = bundle.lib_dir()
    if d is not None:
        try:
            out += sorted(str(p) for p in d.glob("libFLAC.so*") if p.is_file() or p.is_symlink())
        except OSError:
            pass
    if sys.platform.startswith("win"):      # a libFLAC DLL next to the app / in its "native" folder, then the system's
        folders = []
        if getattr(sys, "frozen", False):
            if getattr(sys, "_MEIPASS", None):
                folders.append(Path(sys._MEIPASS))
            folders.append(Path(sys.executable).resolve().parent)
        folders += [Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd(),
                    Path(__file__).resolve().parent.parent]
        for folder in folders:
            for sub in ("", "native"):
                for name in ("libFLAC.dll", "FLAC.dll", "libFLAC-14.dll", "libFLAC-12.dll", "libFLAC-8.dll"):
                    cand = folder / sub / name if sub else folder / name
                    try:
                        if cand.is_file():
                            out.append(str(cand))
                    except OSError:
                        pass
        out += ["libFLAC.dll", "FLAC.dll"]
    found = ctypes.util.find_library("FLAC")
    if found:
        out.append(found)
    out += ["libFLAC.so.14", "libFLAC.so.12", "libFLAC.so.8", "libFLAC.so"]
    for folder in ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/lib64", "/usr/local/lib",
                   "/run/host/usr/lib", "/run/host/usr/lib64"):
        try:
            out += sorted(str(p) for p in Path(folder).glob("libFLAC.so.*") if ".so." in p.name)
        except OSError:
            pass
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _load():
    global _lib, _lib_path, _tried, _note
    if _tried:
        return _lib
    with _lock:
        if not _tried:
            _lib, _lib_path, _note = _find()
            _tried = True               # last: another thread must never see "tried" before the result is there
        return _lib


def _find() -> tuple:
    """``(library, its path, note)``; ``(None, None, why)`` when libFLAC cannot be loaded."""
    if os.environ.get("ROMORG_NO_NATIVE_FLAC"):
        return None, None, "disabled (ROMORG_NO_NATIVE_FLAC)"
    errors = []
    libdir = bundle.lib_dir()
    if libdir is not None:            # libFLAC needs libogg; the bundled one is not on the loader's path
        for p in sorted(libdir.glob("libogg.so*")):
            try:
                ctypes.CDLL(str(p), mode=ctypes.RTLD_GLOBAL)
                break
            except OSError:
                continue
    for cand in _candidates():
        try:
            lib = ctypes.CDLL(cand)
            _bind(lib)
        except (OSError, AttributeError) as exc:
            errors.append(f"{cand}: {exc}")
            continue
        return lib, cand, ""
    return None, None, "libFLAC not found" + (f" ({errors[0]})" if errors else "")


def _bind(lib) -> None:
    lib.FLAC__stream_decoder_new.restype = _VOID
    lib.FLAC__stream_decoder_new.argtypes = []
    lib.FLAC__stream_decoder_delete.argtypes = [_VOID]
    lib.FLAC__stream_decoder_delete.restype = None
    lib.FLAC__stream_decoder_init_stream.argtypes = [_VOID, _READ, _VOID, _TELL, _VOID, _VOID, _WRITE, _VOID, _ERROR, _VOID]
    lib.FLAC__stream_decoder_init_stream.restype = ctypes.c_int
    for n in ("process_single", "process_until_end_of_metadata", "reset", "finish", "get_state"):
        f = getattr(lib, "FLAC__stream_decoder_" + n)
        f.argtypes = [_VOID]
        f.restype = ctypes.c_int
    lib.FLAC__stream_decoder_get_decode_position.argtypes = [_VOID, ctypes.POINTER(ctypes.c_uint64)]
    lib.FLAC__stream_decoder_get_decode_position.restype = ctypes.c_int


def _sndfile() -> bool:
    """libsndfile (what the Windows build bundles) can stand in for libFLAC when only the audio is wanted."""
    return not os.environ.get("ROMORG_NO_NATIVE_FLAC") and nativeflac.available()


def available() -> bool:
    """True when CD audio is decoded natively: libFLAC, or libsndfile as the stand-in."""
    return _load() is not None or _sndfile()


def library_path() -> Optional[str]:
    _load()
    return _lib_path


def status() -> dict:
    """``{"native": bool, "library": path | None, "note": why not}`` for the UI / self-check."""
    _load()
    if _lib is None and _sndfile():
        return {"native": True, "library": "libsndfile", "note": "libFLAC not found: CD audio is decoded through libsndfile"}
    return {"native": _lib is not None, "library": _lib_path, "note": _note}


def reload() -> None:
    """Forget the loaded library (tests that change the environment)."""
    global _lib, _lib_path, _tried, _note
    with _lock:
        _lib, _lib_path, _tried, _note = None, None, False, ""
    _local.__dict__.clear()


def _header(total_samples: int) -> bytes:
    """``fLaC`` + a last STREAMINFO block: blocksize 16..65535, 44.1 kHz, 2 channels, 16 bit (like libchdr)."""
    si = struct.pack(">HH", 16, 65535) + b"\0\0\0" + b"\0\0\0"
    si += (((44100 << 44) | (1 << 41) | (15 << 36) | (total_samples & ((1 << 36) - 1)))).to_bytes(8, "big")
    si += b"\0" * 16
    return b"fLaC" + bytes([0x80, 0, 0, 34]) + si


class _Decoder:
    """One libFLAC stream decoder with its buffers (not thread safe: one per thread)."""

    def __init__(self, lib) -> None:
        self.lib = lib
        self.dec = lib.FLAC__stream_decoder_new()
        if not self.dec:
            raise FlacError("libFLAC could not allocate a decoder")
        self._in = ctypes.create_string_buffer(1 << 16)
        self._in_addr = ctypes.addressof(self._in)
        self._in_len = 0
        self._in_pos = 0
        self._l = ctypes.create_string_buffer(4 * 8192)
        self._r = ctypes.create_string_buffer(4 * 8192)
        self._lcap = 8192
        self._hdrs: dict = {}
        self._got = 0
        self._err = 0
        self._over = False

        def read_cb(_d, buf, n, _cd):
            k = min(n[0], self._in_len - self._in_pos)
            if k <= 0:
                n[0] = 0
                return 1                       # END_OF_STREAM
            ctypes.memmove(buf, self._in_addr + self._in_pos, k)
            self._in_pos += k
            n[0] = k
            return 0

        def tell_cb(_d, pos, _cd):
            pos[0] = self._in_pos
            return 0

        def write_cb(_d, frame, bufs, _cd):
            if frame[2] != 2 or frame[4] != 16:      # header.channels, header.bits_per_sample: CD audio only; a mono
                self._err = -1                       # frame has no second buffer (a NULL read killed the process)
                return 1                             # ABORT
            bs = frame[0]                      # FLAC__Frame.header.blocksize is its first field
            got = self._got
            if got + bs > self._lcap:
                self._over = True
                return 1                       # ABORT: more samples than the hunk holds
            ctypes.memmove(self._l_addr + 4 * got, bufs[0], 4 * bs)
            ctypes.memmove(self._r_addr + 4 * got, bufs[1], 4 * bs)
            self._got = got + bs
            return 0

        def err_cb(_d, status, _cd):
            self._err = status or -1

        self._cbs = (_READ(read_cb), _TELL(tell_cb), _WRITE(write_cb), _ERROR(err_cb))
        self._l_addr = ctypes.addressof(self._l)
        self._r_addr = ctypes.addressof(self._r)
        rc = lib.FLAC__stream_decoder_init_stream(self.dec, self._cbs[0], None, self._cbs[1], None, None,
                                                  self._cbs[2], None, self._cbs[3], None)
        if rc != 0:
            raise FlacError(f"libFLAC refused to initialise (status {rc})")

    def close(self) -> None:
        if self.dec:
            try:
                self.lib.FLAC__stream_decoder_finish(self.dec)
                self.lib.FLAC__stream_decoder_delete(self.dec)
            except Exception:  # noqa: BLE001
                pass
            self.dec = None

    def decode(self, data, start: int, samples: int, big_endian: bool) -> Tuple[bytearray, int]:
        lib, dec = self.lib, self.dec
        hdr = self._hdrs.get(samples)
        if hdr is None:
            hdr = self._hdrs[samples] = _header(samples)
        need = len(hdr) + len(data) - start
        if need > len(self._in):
            self._in = ctypes.create_string_buffer(need + 4096)
            self._in_addr = ctypes.addressof(self._in)
        ctypes.memmove(self._in_addr, hdr, len(hdr))
        body = data if (start == 0 and isinstance(data, bytes)) else bytes(memoryview(data)[start:])
        ctypes.memmove(self._in_addr + len(hdr), body, len(body))
        self._in_len, self._in_pos = need, 0
        if samples > self._lcap:
            self._lcap = samples
            self._l = ctypes.create_string_buffer(4 * samples)
            self._r = ctypes.create_string_buffer(4 * samples)
            self._l_addr = ctypes.addressof(self._l)
            self._r_addr = ctypes.addressof(self._r)
        self._got, self._err, self._over = 0, 0, False
        lib.FLAC__stream_decoder_reset(dec)
        if not lib.FLAC__stream_decoder_process_until_end_of_metadata(dec) or self._err:
            raise FlacError("libFLAC rejected the stream header")
        single = lib.FLAC__stream_decoder_process_single
        while self._got < samples:
            ok = single(dec)
            if self._over:
                raise FlacError("FLAC frames overshoot the hunk")
            if not ok or self._err:
                raise FlacError(f"FLAC decode error {self._err}" if self._err else "FLAC decode failed")
            if lib.FLAC__stream_decoder_get_state(dec) == _ST_END_OF_STREAM and self._got < samples:
                raise FlacError("truncated FLAC data")
        if self._got != samples:
            raise FlacError("FLAC frames overshoot the hunk")
        pos = ctypes.c_uint64()
        if lib.FLAC__stream_decoder_get_decode_position(dec, ctypes.byref(pos)):
            end = start + int(pos.value) - len(hdr)
        else:                                  # pragma: no cover - position unknown: caller only needs it for subcode
            end = start + self._in_pos - len(hdr)
        lraw = ctypes.string_at(self._l_addr, 4 * samples)
        rraw = ctypes.string_at(self._r_addr, 4 * samples)
        out = bytearray(4 * samples)
        # int32 samples (values are 16-bit): keep the two low bytes, in the byte order asked for
        lowi, highi = (3, 2) if _BIG else (0, 1)
        first, second = (highi, lowi) if big_endian else (lowi, highi)
        out[0::4] = lraw[first::4]
        out[1::4] = lraw[second::4]
        out[2::4] = rraw[first::4]
        out[3::4] = rraw[second::4]
        return out, end


_local = threading.local()


def _decoder() -> Optional[_Decoder]:
    lib = _load()
    if lib is None:
        return None
    d = getattr(_local, "dec", None)
    if d is None:
        d = _local.dec = _Decoder(lib)
    return d


def decode_pcm(data, start: int, samples_per_channel: int, big_endian: bool = False,
               need_end: bool = True) -> Tuple[bytearray, int]:
    """Decode a hunk's FLAC frames to interleaved stereo 16-bit PCM bytes (little- or big-endian samples) and the
    offset where they end. Native when libFLAC loads; else, when the caller does not need the end offset
    (``need_end=False``: the subcode after the frames is not wanted), libsndfile if that loads (what the Windows
    build bundles; the offset returned is then 0); else the pure-Python decoder."""
    if not need_end and start == 0 and _sndfile() and nativeflac.fd_available():
        # only the audio is wanted: libsndfile reading a memory file decodes without a single callback into Python,
        # which is a little quicker on one core and, unlike the libFLAC binding below, scales over decode threads
        try:
            pcm = nativeflac.decode_frames_fd(data, samples_per_channel)
        except nativeflac.NativeFlacError:
            pcm = None                 # damaged data or no memory file: libFLAC below says what is wrong
        if pcm is not None:
            if big_endian != _BIG:
                pcm.byteswap()
            return bytearray(pcm.tobytes()), 0
    d = _decoder()
    if d is None and not need_end and start == 0 and _sndfile():
        try:
            pcm = nativeflac.decode_frames(bytes(data), samples_per_channel)
        except nativeflac.NativeFlacError:
            pcm = None                 # not available / not decoded exactly: the Python decoder below
        if pcm is not None:
            if big_endian != _BIG:
                pcm.byteswap()
            return bytearray(pcm.tobytes()), 0
    if d is None:
        pcm, end = flacdec.decode_frames(data, start, samples_per_channel)
        if big_endian != _BIG:
            pcm.byteswap()
        return bytearray(pcm.tobytes()), end
    return d.decode(data, start, samples_per_channel, big_endian)


def decode_frames(data, start: int, samples_per_channel: int, channels: int = 2) -> Tuple["array", int]:
    """Drop-in for :func:`romorg.flacdec.decode_frames` (``array('h')`` of interleaved native-endian samples)."""
    if channels != 2:
        raise FlacError("unexpected channel count")
    d = _decoder()
    if d is None:
        return flacdec.decode_frames(data, start, samples_per_channel, channels)
    raw, end = d.decode(data, start, samples_per_channel, _BIG)
    pcm = array("h")
    pcm.frombytes(bytes(raw))
    return pcm, end
