"""FLAC *frames* for CHD ``cdfl`` hunks: a ctypes binding to libFLAC's stream encoder (the writing side of
:mod:`romorg.flacnative`, which finds and loads the library).

A hunk is an independent FLAC stream without its header: 44.1 kHz, 2 channels, 16 bit, a fixed block size derived
from the hunk size, exactly as MAME's ``flac_encoder`` configures libFLAC (compression level 8, not restricted to the
streamable subset). MAME's decoder is strict about that block size, so the frames of any other encoder are not
safe to store. Without libFLAC :func:`available` is False and the writer keeps audio in ``cdlz`` / ``cdzl`` hunks.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from array import array
from typing import Optional

from . import flacnative

__all__ = ["available", "block_size", "encode", "FlacEncodeError"]

_VOID = ctypes.c_void_p
_WRITE = ctypes.CFUNCTYPE(ctypes.c_int, _VOID, _VOID, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32, _VOID)
MAX_BLOCK = 2048
CD_BLOCK = 2352
_lock = threading.Lock()
_bound: dict = {}               # id of a loaded library object -> the encoder functions are declared on it
_local = threading.local()


class FlacEncodeError(Exception):
    """libFLAC refused to encode (the caller stores the hunk with another codec)."""


def _lib():
    lib = flacnative._load()
    if lib is None:
        return None
    ok = _bound.get(id(lib))
    if ok is None:
        with _lock:
            ok = _bound.get(id(lib))
            if ok is None:
                # declared on THIS library object: after flacnative.reload() there is a new one, and calling an
                # undeclared function would pass pointers as 32-bit integers
                try:
                    lib.FLAC__stream_encoder_new.restype = _VOID
                    lib.FLAC__stream_encoder_new.argtypes = []
                    lib.FLAC__stream_encoder_delete.argtypes = [_VOID]
                    lib.FLAC__stream_encoder_delete.restype = None
                    for name in ("verify", "streamable_subset"):
                        f = getattr(lib, "FLAC__stream_encoder_set_" + name)
                        f.argtypes, f.restype = [_VOID, ctypes.c_int], ctypes.c_int
                    for name in ("compression_level", "channels", "bits_per_sample", "sample_rate", "blocksize"):
                        f = getattr(lib, "FLAC__stream_encoder_set_" + name)
                        f.argtypes, f.restype = [_VOID, ctypes.c_uint32], ctypes.c_int
                    f = lib.FLAC__stream_encoder_set_total_samples_estimate
                    f.argtypes, f.restype = [_VOID, ctypes.c_uint64], ctypes.c_int
                    f = lib.FLAC__stream_encoder_init_stream
                    f.argtypes, f.restype = [_VOID, _WRITE, _VOID, _VOID, _VOID, _VOID], ctypes.c_int
                    f = lib.FLAC__stream_encoder_process_interleaved
                    f.argtypes, f.restype = [_VOID, _VOID, ctypes.c_uint32], ctypes.c_int
                    f = lib.FLAC__stream_encoder_finish
                    f.argtypes, f.restype = [_VOID], ctypes.c_int
                    ok = True
                except AttributeError:
                    ok = False
                _bound.clear()
                _bound[id(lib)] = ok
                _keep[:] = [lib]
    return lib if ok else None


_keep: list = []                # the bound library object stays alive, so its id is not reused


def available() -> bool:
    return _lib() is not None


def block_size(nbytes: int, cd: bool = True) -> int:
    """MAME's block size for ``nbytes`` of 16-bit stereo audio: all samples, halved until at most one CD sector's
    worth of bytes (2352, the ``cdfl`` codec) or 2048 (the plain ``flac`` codec)."""
    block = nbytes // 4
    limit = CD_BLOCK if cd else MAX_BLOCK
    while block > limit:
        block //= 2
    return block


class _Encoder:
    def __init__(self, lib) -> None:
        self.lib = lib
        self.enc = lib.FLAC__stream_encoder_new()
        if not self.enc:
            raise FlacEncodeError("libFLAC could not allocate an encoder")
        self.parts: list = []

        def write_cb(_e, buf, nbytes, samples, _frame, _cd):
            if samples:                         # samples == 0: the stream header / metadata, which CHD leaves out
                self.parts.append(ctypes.string_at(buf, nbytes))
            return 0

        self._cb = _WRITE(write_cb)

    def encode(self, samples: "array", count: int, block: int) -> bytes:
        lib, enc = self.lib, self.enc
        # finish() puts every setting back to its default, so they are set for each hunk (MAME does the same)
        lib.FLAC__stream_encoder_set_verify(enc, 0)
        lib.FLAC__stream_encoder_set_compression_level(enc, 8)
        lib.FLAC__stream_encoder_set_channels(enc, 2)
        lib.FLAC__stream_encoder_set_bits_per_sample(enc, 16)
        lib.FLAC__stream_encoder_set_sample_rate(enc, 44100)
        lib.FLAC__stream_encoder_set_total_samples_estimate(enc, 0)
        lib.FLAC__stream_encoder_set_streamable_subset(enc, 0)
        lib.FLAC__stream_encoder_set_blocksize(enc, block)
        self.parts = []
        rc = lib.FLAC__stream_encoder_init_stream(enc, self._cb, None, None, None, None)
        if rc != 0:
            raise FlacEncodeError(f"libFLAC encoder did not start (status {rc})")
        ok = lib.FLAC__stream_encoder_process_interleaved(enc, samples.buffer_info()[0], count)
        ok = lib.FLAC__stream_encoder_finish(enc) and ok
        if not ok:
            raise FlacEncodeError("libFLAC failed to encode the hunk")
        return b"".join(self.parts)


def encode(pcm: bytes, big_endian: bool = True, cd: bool = True) -> bytes:
    """The FLAC frames of interleaved 16-bit stereo ``pcm`` (big-endian samples as CHD hunks hold CD audio)."""
    lib = _lib()
    if lib is None:
        raise FlacEncodeError("libFLAC is not available")
    enc = getattr(_local, "enc", None)
    if enc is None or enc.lib is not lib:
        enc = _local.enc = _Encoder(lib)
    words = array("h")
    words.frombytes(pcm)
    if big_endian != (sys.byteorder == "big"):
        words.byteswap()
    wide = array("i", words)                    # libFLAC takes 32-bit samples
    return enc.encode(wide, len(words) // 2, block_size(len(pcm), cd))
