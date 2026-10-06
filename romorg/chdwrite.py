"""CHD writer (version 5): what ``chdman createcd`` / ``createdvd`` do, without chdman.

For the same input the file has chdman's data SHA-1, metadata and header SHA-1 (the header SHA-1 covers data and
metadata, not how they are compressed), ``chdman verify`` accepts it and emulators read it like chdman's. The
compressed bytes are not chdman's, and need not be.

File layout: header (124 bytes), the metadata entries, the hunks in order, the compressed map.

Where the time goes, and what is done about it. LZMA costs the same per core here as in chdman, and chdman
compresses every hunk with every codec of its list to keep the smallest. Here

* a hunk that is a copy of an earlier one is found by its SHA-1 *before* anything is compressed;
* audio hunks go to FLAC only;
* a data hunk is probed first (deflate level 1 on a sample): data that will not compress gets deflate and no LZMA at
  all, the rest gets LZMA and no deflate. Measured on PlayStation, PlayStation 2 and Dreamcast discs this costs
  0.1 to 0.5 % in size against trying both;
* the work is spread over worker *processes* (the ones of :mod:`romorg.chdsched`): checking which sectors carry
  standard error correction is Python code, and threads would queue for the interpreter lock behind it. Small
  images, and systems where no worker starts, use threads in this process.

Codecs: ``cdlz`` / ``cdzl`` / ``cdfl`` for CDs and ``lzma`` / ``zlib`` for DVDs (chdman's defaults, minus the two
DVD codecs that never pay on a game disc); with ``preset="zstd"`` the LZMA slot becomes Zstandard (``cdzs`` /
``zstd``): much faster to write and to read, but only emulators from 2024 on read it.
"""

from __future__ import annotations

import binascii
import hashlib
import io
import json
import lzma
import os
import re
import struct
import sys
import threading
import zlib
from array import array
from collections import deque
from itertools import compress, repeat
from operator import lshift as _shl, or_ as _or
from typing import Callable, List, Optional, Sequence, Tuple

from . import cdecc, flacenc, zstdnative
from .cdimage import FRAME, SECTOR

__all__ = ["ChdWriteError", "Cancelled", "write_chd", "default_codecs", "default_threads", "zstd_available"]

HEADER = 124
SUBCODE = FRAME - SECTOR
_T_NONE, _T_SELF, _T_RLE_SMALL, _T_RLE_LARGE, _T_SELF0, _T_SELF1 = 4, 5, 7, 8, 9, 10
ENV_THREADS = "ROMORG_CHDWRITE_THREADS"
ZSTD_LEVEL = 9              # the Zstandard preset: several times quicker than LZMA, a few percent larger
PROBE_BYTES = 2048          # a data hunk is probed with deflate level 1 on two samples of this size ...
PROBE_POOR = 0.97           # ... and counts as "will not compress" when they stay above this share
SMALL_IMAGE = 24 << 20      # below this, starting worker processes costs more than it saves
SLICE_HUNKS = 12            # hunks per request to a worker
ProgressFn = Callable[[int, int], None]


class ChdWriteError(Exception):
    """The CHD could not be written."""


class Cancelled(ChdWriteError):
    """Stopped by the caller; the partial file is removed."""


def default_threads() -> int:
    """Parallel compressors: ``$ROMORG_CHDWRITE_THREADS``, else the scheduler's worker count (one per CPU thread,
    fewer when memory is short)."""
    raw = os.environ.get(ENV_THREADS, "")
    if raw.strip().isdigit():
        return max(1, min(64, int(raw)))
    from . import chdsched
    return chdsched.default_workers()


def zstd_available() -> bool:
    """True when hunks can be *compressed* with Zstandard (a library is needed; the reader's Python fallback only
    decodes)."""
    return zstdnative.can_compress()


def default_codecs(cd: bool, preset: str = "default") -> Tuple[str, str, str, str]:
    if preset == "zstd":
        if not zstd_available():
            raise ChdWriteError("the Zstandard preset needs a Zstandard library (libzstd)")
        return ("cdzs", "cdzl", "cdfl", "") if cd else ("zstd", "zlib", "", "")
    if preset != "default":
        raise ChdWriteError(f"unknown preset {preset!r}")
    return ("cdlz", "cdzl", "cdfl", "") if cd else ("lzma", "zlib", "", "")


# --------------------------------------------------------------------------- hunk compressors
def _lzma_filters(nbytes: int):
    # what MAME's decoder derives from the hunk size (the dictionary must not be larger); lc / lp / pb fixed
    dict_size = 1 << 16
    while dict_size < nbytes * 2:
        dict_size <<= 1
    # nice_len 32 instead of liblzma's 64: about 10 % quicker on disc data for the same size (measured on
    # PlayStation, PlayStation 2 and Dreamcast hunks: -0.19 % to +0.07 %)
    return [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": 3, "lp": 0, "pb": 2, "nice_len": 32}]


def _deflate(data: bytes, level: int = 9) -> bytes:
    c = zlib.compressobj(level, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


class _Compressor:
    """Picks the codec of each hunk. Thread safe: no state changes after construction."""

    def __init__(self, codecs: Sequence[str], hunk_bytes: int, cd: bool):
        self.codecs = tuple(codecs)
        self.hunk_bytes = hunk_bytes
        self.cd = cd
        self.slot = {name: i for i, name in enumerate(self.codecs) if name}
        if cd:
            self.frames = hunk_bytes // FRAME
            self.sector_bytes = self.frames * SECTOR
            self.filters = _lzma_filters(self.sector_bytes)
            self.zero_sub = bytes(self.frames * SUBCODE)
            self.zero_sub_deflate = _deflate(self.zero_sub)
            self.zero_sub_zstd = zstdnative.compress(self.zero_sub, ZSTD_LEVEL) if "cdzs" in self.slot else b""
            self.ecc_bytes = (self.frames + 7) // 8
            self.len_bytes = 2 if hunk_bytes < 65536 else 3
            self.flac = "cdfl" in self.slot and flacenc.available()
        else:
            self.filters = _lzma_filters(hunk_bytes)

    # -- CD
    def _split(self, hunk: bytes) -> Tuple[bytes, bytes]:
        n = self.frames
        sectors = b"".join([hunk[i * FRAME:i * FRAME + SECTOR] for i in range(n)])
        sub = b"".join([hunk[i * FRAME + SECTOR:(i + 1) * FRAME] for i in range(n)])
        return sectors, sub

    def _strip_ecc(self, sectors: bytes) -> Tuple[bytes, bytes, int]:
        """``(ECC bitmap, sectors with sync and parity zeroed where they are the standard ones, how many)``."""
        n = self.frames
        bitmap = bytearray(self.ecc_bytes)
        cand = []
        for i in range(n):
            at = i * SECTOR
            # MODE 2 form 2 (bit 5 of the submode) has no error correction: not worth the arithmetic
            if sectors.startswith(cdecc.SYNC, at) and not (sectors[at + 15] == 2 and sectors[at + 18] & 0x20):
                cand.append(i)
        if not cand:
            return bytes(bitmap), sectors, 0
        good = cdecc.valid([sectors[i * SECTOR:(i + 1) * SECTOR] for i in cand])
        out = bytearray(sectors)
        count = 0
        for i, ok in zip(cand, good):
            if ok:
                at = i * SECTOR
                bitmap[i >> 3] |= 1 << (i & 7)
                out[at:at + 12] = _ZERO12
                out[at + 2076:at + SECTOR] = _ZERO276
                count += 1
        return bytes(bitmap), bytes(out), count

    def _wrap(self, bitmap: bytes, body: bytes, sub_blob: bytes) -> bytes:
        return bitmap + len(body).to_bytes(self.len_bytes, "big") + body + sub_blob

    def strip_many(self, hunks: Sequence[bytes], hints: Sequence[str]) -> list:
        """The ECC work of several hunks in one go (the arithmetic is vectorised: a batch costs less per sector).
        Per hunk ``(sectors, subcode, bitmap, bare sectors, stripped count)``; audio hunks are left for
        :meth:`_cd` to look at only if FLAC disappoints."""
        n = self.frames
        split = [self._split(h) for h in hunks]
        cand = []
        for k, (sectors, _sub) in enumerate(split):
            if hints[k] == "audio" and self.flac:
                continue
            for i in range(n):
                at = i * SECTOR
                if sectors.startswith(cdecc.SYNC, at) and not (sectors[at + 15] == 2 and sectors[at + 18] & 0x20):
                    cand.append((k, i))
        good = cdecc.valid([split[k][0][i * SECTOR:(i + 1) * SECTOR] for k, i in cand]) if cand else []
        maps = [bytearray(self.ecc_bytes) for _ in hunks]
        bare: list = [None] * len(hunks)
        counts = [0] * len(hunks)
        for (k, i), ok in zip(cand, good):
            if ok:
                if bare[k] is None:
                    bare[k] = bytearray(split[k][0])
                at = i * SECTOR
                maps[k][i >> 3] |= 1 << (i & 7)
                bare[k][at:at + 12] = _ZERO12
                bare[k][at + 2076:at + SECTOR] = _ZERO276
                counts[k] += 1
        out = []
        for k, (sectors, sub) in enumerate(split):
            if hints[k] == "audio" and self.flac:
                out.append((sectors, sub, None, None, 0))
            else:
                out.append((sectors, sub, bytes(maps[k]), bytes(bare[k]) if bare[k] is not None else sectors,
                            counts[k]))
        return out

    def _cd(self, hunk: bytes, hint: str, pre=None) -> Tuple[int, bytes]:
        hb = self.hunk_bytes
        sectors, sub = (pre[0], pre[1]) if pre is not None else self._split(hunk)
        plain_sub = sub == self.zero_sub
        sub_deflate = self.zero_sub_deflate if plain_sub else _deflate(sub)
        best: Tuple[int, bytes] = (_T_NONE, hunk)
        size = hb
        if hint == "audio" and self.flac:
            try:
                frames = flacenc.encode(sectors)
            except flacenc.FlacEncodeError:
                frames = None
            if frames is not None and len(frames) + len(sub_deflate) < size:
                best = (self.slot["cdfl"], frames + sub_deflate)
                size = len(best[1])
                if size < hb * PROBE_POOR:
                    return best                 # audio that FLAC handles: nothing else comes close
        if pre is not None and pre[2] is not None:
            bitmap, bare, stripped = pre[2], pre[3], pre[4]
        else:
            bitmap, bare, stripped = self._strip_ecc(sectors)
        strong = "cdlz" if "cdlz" in self.slot else "cdzs" if "cdzs" in self.slot else ""
        poor = bool(strong) and "cdzl" in self.slot and stripped == 0 and _probe_poor(bare, SECTOR)
        if strong and not poor:
            if strong == "cdlz":
                body = lzma.compress(bare, lzma.FORMAT_RAW, filters=self.filters)
                sub_blob = sub_deflate
            else:
                body = zstdnative.compress(bare, ZSTD_LEVEL)
                sub_blob = self.zero_sub_zstd if plain_sub else zstdnative.compress(sub, ZSTD_LEVEL)
            if len(body) < self.sector_bytes:
                comp = self._wrap(bitmap, body, sub_blob)
                if len(comp) < size:
                    best, size = (self.slot[strong], comp), len(comp)
        if "cdzl" in self.slot and (poor or not strong or best[0] == _T_NONE):
            body = _deflate(bare, 6 if poor else 9)
            if len(body) < self.sector_bytes:
                comp = self._wrap(bitmap, body, sub_deflate)
                if len(comp) < size:
                    best, size = (self.slot["cdzl"], comp), len(comp)
        return best

    # -- plain hunks
    def _plain(self, hunk: bytes) -> Tuple[int, bytes]:
        hb = self.hunk_bytes
        best: Tuple[int, bytes] = (_T_NONE, hunk)
        size = hb
        strong = "lzma" if "lzma" in self.slot else "zstd" if "zstd" in self.slot else ""
        poor = bool(strong) and "zlib" in self.slot and _probe_poor(hunk, max(PROBE_BYTES, hb // 2))
        if strong and not poor:
            comp = (lzma.compress(hunk, lzma.FORMAT_RAW, filters=self.filters) if strong == "lzma"
                    else zstdnative.compress(hunk, ZSTD_LEVEL))
            if len(comp) < size:
                best, size = (self.slot[strong], comp), len(comp)
        if "zlib" in self.slot and (poor or not strong or best[0] == _T_NONE):
            comp = _deflate(hunk, 6 if poor else 9)
            if len(comp) < size:
                best, size = (self.slot["zlib"], comp), len(comp)
        return best

    def __call__(self, hunk: bytes, hint: str) -> Tuple[int, bytes]:
        return self._cd(hunk, hint) if self.cd else self._plain(hunk)


_ZERO12 = bytes(12)
_ZERO276 = bytes(276)


def _probe_poor(data: bytes, step: int) -> bool:
    """A cheap guess that ``data`` will not compress: two samples, deflate level 1. Wrong guesses only cost size
    (a little) or time, never correctness."""
    n = len(data)
    first = data[24:24 + PROBE_BYTES]
    at = (n // 2) - (n // 2) % step + 24
    sample = first + data[at:at + PROBE_BYTES]
    return len(zlib.compress(sample, 1)) >= len(sample) * PROBE_POOR


def compress_many(comp: "_Compressor", data: bytes, hints: Sequence[str]) -> Tuple[list, bytes]:
    """Compress the hunks in ``data``: ``([[slot, length, crc16], ...], the compressed hunks joined)`` - what a
    worker process answers."""
    hb = comp.hunk_bytes
    items = []
    out = []
    hunks = [data[k * hb:(k + 1) * hb] for k in range(len(hints))]
    pre = comp.strip_many(hunks, hints) if comp.cd else None
    for k, hint in enumerate(hints):
        hunk = hunks[k]
        slot, blob = comp._cd(hunk, hint, pre[k]) if pre is not None else comp(hunk, hint)
        items.append([slot, len(blob), binascii.crc_hqx(hunk, 0xFFFF)])
        out.append(blob)
    return items, b"".join(out)


def _write_all(f, data) -> None:
    """Write all of ``data`` to the unbuffered pipe ``f`` (one ``write`` may take only part of it)."""
    view = memoryview(data)
    while view:
        n = f.write(view)
        if n is None or n <= 0:
            raise OSError("cannot write to a worker")
        view = view[n:]


class _Pool:
    """Runs :func:`compress_many` on slices of hunks, in order: in worker processes when there are several and they
    start, else on threads of this process. A worker that dies has its slice redone here."""

    def __init__(self, comp: "_Compressor", workers: int, processes: bool):
        self.comp = comp
        self.workers = max(1, workers)
        self.key = {"codecs": list(comp.codecs), "hunk_bytes": comp.hunk_bytes, "cd": comp.cd}
        self.procs: list = []
        self.local = threading.local()
        self.lock = threading.Lock()
        self.processes = processes and self.workers > 1
        self.fallbacks = 0
        from concurrent.futures import ThreadPoolExecutor     # (not needed by the worker processes)
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="chd-write") \
            if self.workers > 1 else None

    def _worker(self):
        """``(process, buffered reader of its stdout)`` of this thread's worker, None = compress here."""
        st = self.local.__dict__
        if "proc" not in st:
            st["proc"] = None
            if self.processes:
                from . import chdsched
                try:
                    proc = chdsched.spawn_worker()
                except chdsched.PoolError:
                    self.processes = False
                else:
                    # the pipe is unbuffered (bufsize=0): readline() on it would read one byte per system call.
                    # The buffered reader replaces proc.stdout, so closing the worker closes it.
                    proc.stdout = io.BufferedReader(proc.stdout, buffer_size=1 << 16)
                    with self.lock:
                        self.procs.append(proc)
                    st["proc"] = (proc, proc.stdout)
        return st["proc"]

    def _one(self, job: Tuple[bytes, Sequence[str]]) -> Tuple[list, bytes]:
        data, hints = job
        worker = self._worker()
        if worker is not None:
            proc, rd = worker
            try:
                head = dict(self.key, id=0, op="compress", hints=list(hints), n=len(data))
                _write_all(proc.stdin, json.dumps(head, separators=(",", ":")).encode() + b"\n")
                _write_all(proc.stdin, data)
                reply = json.loads(rd.readline())
                n = int(reply["n"])
                buf = bytearray(n)
                view = memoryview(buf)
                got = 0
                while got < n:
                    k = rd.readinto(view[got:])
                    if not k:
                        raise OSError("worker closed its pipe")
                    got += k
                items = reply["items"]
                if len(items) == len(hints) and sum(i[1] for i in items) == n:
                    return items, bytes(buf)
                raise ValueError("garbled worker reply")
            except (OSError, ValueError, KeyError, TypeError):
                self.local.proc = None          # this thread compresses by itself from now on
                with self.lock:                 # (several threads may fail at once)
                    self.fallbacks += 1
        return compress_many(self.comp, data, hints)

    def submit(self, jobs: list) -> list:
        """Start the jobs; a list of zero-argument callables that give the results (in the order of ``jobs``)."""
        if self.pool is None:
            done = [self._one(j) for j in jobs]
            return [lambda r=r: r for r in done]
        return [self.pool.submit(self._one, j).result for j in jobs]

    def close(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
        if self.procs:
            from . import chdsched
            for proc in self.procs:             # end of input: every worker exits by itself, all at once ...
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            for proc in self.procs:
                try:
                    proc.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    pass
                chdsched.kill_worker(proc)      # ... only one that did not is killed
            self.procs = []


# --------------------------------------------------------------------------- the map
def _huffman_lengths(counts: Sequence[int], maxbits: int) -> List[int]:
    """Code lengths for a histogram, at most ``maxbits`` long, always a complete tree."""
    import heapq
    counts = list(counts)
    used = [i for i, c in enumerate(counts) if c]
    if len(used) == 1:                  # a lone symbol: give it a partner so the tree is complete
        counts[(used[0] + 1) % len(counts)] = 1
    while True:
        heap = [(c, i, (i,)) for i, c in enumerate(counts) if c]
        lengths = [0] * len(counts)
        heapq.heapify(heap)
        while len(heap) > 1:
            a = heapq.heappop(heap)
            b = heapq.heappop(heap)
            for sym in a[2] + b[2]:
                lengths[sym] += 1
            heapq.heappush(heap, (a[0] + b[0], min(a[1], b[1]), a[2] + b[2]))
        if max(lengths) <= maxbits:
            return lengths
        counts = [(c + 1) // 2 + 1 if c else 0 for c in counts]     # too deep: flatten and retry


def _canonical(lengths: Sequence[int]) -> dict:
    """MAME's canonical codes as bit strings: the longest codes are numbered from zero."""
    out = {}
    code = 0
    for ln in range(max(lengths), 0, -1):
        for sym, b in enumerate(lengths):
            if b == ln:
                out[sym] = format(code, f"0{ln}b")
                code += 1
        code >>= 1
    return out


_RUN = re.compile(rb"(.)\1{3,}", re.S)                     # four or more entries of one kind
_IS_CODEC = bytes(1 if b <= 3 else 0 for b in range(256))  # translate tables over entry types / kinds
_HAS_FIELD = bytes(0 if b in (_T_SELF0, _T_SELF1) else 1 for b in range(256))
_HAS_FIELD_NO_SELF = bytes(0 if b in (_T_SELF, _T_SELF0, _T_SELF1) else 1 for b in range(256))


def _be_bytes(code: str, values: Sequence[int]) -> bytes:
    """``values`` as big-endian items of the array type ``code``."""
    arr = array(code, values)
    if sys.byteorder == "little":
        arr.byteswap()
    return arr.tobytes()


def _build_map(types: Sequence[int], lens: Sequence[int], offs: Sequence[int], crcs: Sequence[int],
               first_offset: int) -> bytes:
    """The compressed v5 map: a 16-byte header, the Huffman-coded entry types (runs folded), then the fields each
    type needs. ``types`` holds 0..3 (a codec slot), NONE and SELF; ``offs`` of a SELF entry is the hunk it copies.

    A DVD has close to a million entries, so the per-entry work is done by C loops (``map``, ``translate``,
    strided slices); Python code only visits the runs and the rare NONE / SELF entries."""
    n = len(types)
    tb = bytes(types)
    self_at = [m.start() for m in re.finditer(b"\x05", tb)]
    none_at = [m.start() for m in re.finditer(b"\x04", tb)]
    # SELF entries that repeat the last copied hunk, or continue right after it, need no offset at all
    kinds = bytearray(tb)
    last_self = -2
    for i in self_at:
        o = offs[i]
        if o == last_self:
            kinds[i] = _T_SELF0
        elif o == last_self + 1:
            kinds[i] = _T_SELF1
        last_self = o
    # the symbols: a run of four or more of the same kind is folded (the kind, then RLE codes for the repeats);
    # anything shorter is the kinds as they are
    symbols = bytearray()
    done = 0
    for m in _RUN.finditer(kinds):
        start, end = m.span()
        symbols += kinds[done:start]
        done = end
        t = kinds[start]
        symbols.append(t)
        rest = end - start - 1
        while rest >= 3:
            if rest >= 19:
                k = min(rest - 19, 255)
                symbols += bytes((_T_RLE_LARGE, k >> 4, k & 15))
                rest -= 19 + k
            else:
                symbols += bytes((_T_RLE_SMALL, rest - 3))
                rest = 0
        symbols += bytes((t,)) * rest
    symbols += kinds[done:]
    counts = [symbols.count(s) for s in range(16)]
    lengths = _huffman_lengths(counts, 8)
    codes = _canonical(lengths)
    bits = ["".join("00010001" if ln == 1 else format(ln, "04b") for ln in lengths)]
    bits.append("".join(map(codes.__getitem__, symbols)))
    lengthbits = max(compress(lens, tb.translate(_IS_CODEC)), default=0).bit_length()
    selfbits = max((offs[i] for i in self_at), default=0).bit_length()
    # the field of each entry: codec slot = length and CRC, NONE = CRC, SELF = the hunk it copies (none for the
    # folded SELF0 / SELF1, and none at all when every copy is of hunk 0)
    vals = list(map(_or, map(_shl, lens, repeat(16)), crcs))
    for i in none_at:
        vals[i] = crcs[i]
    for i in self_at:
        vals[i] = offs[i]
    fmts = [""] * 16
    fmts[0:4] = [f"0{lengthbits + 16}b"] * 4
    fmts[_T_NONE] = "016b"
    fmts[_T_SELF] = f"0{selfbits}b"
    mask = kinds.translate(_HAS_FIELD if selfbits else _HAS_FIELD_NO_SELF)
    bits.append("".join(map(format, compress(vals, mask), compress(map(fmts.__getitem__, kinds), mask))))
    # the uncompressed map (only its CRC is stored): 12 big-endian bytes per entry, ``>BBHHIH`` of (type,
    # length >> 16, length & 0xFFFF, offset >> 32, offset & 0xFFFFFFFF, CRC); a SELF entry has no length or CRC
    if self_at:
        lens = array("I", lens)
        crcs = array("H", crcs)
        for i in self_at:
            lens[i] = crcs[i] = 0
    lb = _be_bytes("I", lens)           # 0, length >> 16, length & 0xFFFF
    ob = _be_bytes("Q", offs)           # 0, 0, offset >> 32, offset & 0xFFFFFFFF
    cb = _be_bytes("H", crcs)
    raw = bytearray(12 * n)
    raw[0::12] = tb
    for at, src, k, step in ((1, lb, 1, 4), (2, lb, 2, 4), (3, lb, 3, 4), (4, ob, 2, 8), (5, ob, 3, 8),
                             (6, ob, 4, 8), (7, ob, 5, 8), (8, ob, 6, 8), (9, ob, 7, 8), (10, cb, 0, 2),
                             (11, cb, 1, 2)):
        raw[at::12] = src[k::step]
    stream = "".join(bits)
    stream += "0" * (-len(stream) % 8)
    body = int(stream, 2).to_bytes(len(stream) // 8, "big") if stream else b""
    head = struct.pack(">I", len(body)) + first_offset.to_bytes(6, "big") + struct.pack(
        ">H", binascii.crc_hqx(bytes(raw), 0xFFFF)) + bytes([lengthbits, selfbits, 0, 0])
    return head + body


def _metadata_blob(metadata: Sequence[Tuple[bytes, bytes]], at: int) -> bytes:
    out = bytearray()
    pos = at
    for k, (tag, body) in enumerate(metadata):
        nxt = pos + 16 + len(body) if k + 1 < len(metadata) else 0
        out += tag + b"\x01" + len(body).to_bytes(3, "big") + nxt.to_bytes(8, "big") + body
        pos += 16 + len(body)
    return bytes(out)


def _overall_sha1(raw: bytes, metadata: Sequence[Tuple[bytes, bytes]]) -> bytes:
    h = hashlib.sha1(raw)
    for item in sorted(tag + hashlib.sha1(body).digest() for tag, body in metadata):
        h.update(item)
    return h.digest()


# --------------------------------------------------------------------------- writing
def write_chd(out_path, image, preset: str = "default", codecs: Optional[Sequence[str]] = None,
              threads: Optional[int] = None, progress: Optional[ProgressFn] = None,
              cancel: Optional[Callable[[], bool]] = None, processes: Optional[bool] = None) -> dict:
    """Write ``image`` (a :class:`romorg.cdimage.CdImage` / ``DvdImage``) to the new file ``out_path``.

    ``progress(done, total)`` counts logical bytes. ``processes``: compress in worker processes (default: yes for
    images of some size). Returns ``{"sha1", "raw_sha1", "size", "hunks", "stored" (hunks per codec name, plus
    ``none`` and ``self``), ...}``. On any error or cancel the partial file is removed."""
    hb = image.hunk_bytes
    logical = image.logical_bytes
    hunk_count = (logical + hb - 1) // hb
    names = tuple(codecs) if codecs else default_codecs(image.cd, preset)
    names = tuple(names) + ("",) * (4 - len(names))
    comp = _Compressor(names, hb, image.cd)
    threads = threads or default_threads()
    if processes is None:
        processes = logical >= SMALL_IMAGE
    per = max(1, min(SLICE_HUNKS, (256 << 10) // hb or 1) if hb >= 16384 else (256 << 10) // hb)
    batch = max(per, threads * per * 3)                 # hunks in flight: a few megabytes
    metadata = list(image.metadata)
    types = array("B")
    lens = array("I")
    offs = array("Q")
    crcs = array("H")
    seen: dict = {}
    raw_sha = hashlib.sha1()
    stored = {name: 0 for name in names if name}
    stored.update({"none": 0, "self": 0})
    ok = False
    out_path = os.fspath(out_path)
    pool = _Pool(comp, threads, processes)
    reader = iter(image.batches(batch))     # closed below: its open track file must not outlive a failed write
    f = open(out_path, "xb")
    try:
        meta = _metadata_blob(metadata, HEADER)
        f.write(bytes(HEADER) + meta)
        first = pos = HEADER + len(meta)
        index = 0
        written = 0
        sha1_of = hashlib.sha1
        waiting: deque = deque()

        def settle(item) -> None:
            """Take a batch's results and write its hunks (batches are settled in order)."""
            nonlocal pos, written
            count, where, todo, results = item
            fresh = {}
            n = 0
            for result in results:
                items, blob = result()
                at = 0
                for slot, length, crc in items:
                    fresh[todo[n]] = (slot, blob[at:at + length], crc)
                    at += length
                    n += 1
            out = []
            for k in range(count):
                if where[k] >= 0:
                    types.append(_T_SELF)
                    lens.append(0)
                    offs.append(where[k])
                    crcs.append(0)
                    stored["self"] += 1
                    continue
                slot, blob, crc = fresh[k]
                types.append(slot)
                lens.append(len(blob))
                offs.append(pos)
                crcs.append(crc)
                pos += len(blob)
                out.append(blob)
                stored["none" if slot == _T_NONE else names[slot]] += 1
            f.write(b"".join(out))
            written += count
            if progress:
                progress(min(logical, written * hb), logical)

        for data, hints in reader:
            if cancel is not None and cancel():
                raise Cancelled("cancelled")
            count = len(hints)
            if len(data) != count * hb or index + count > hunk_count:
                raise ChdWriteError("the image changed while it was read")
            left = logical - index * hb
            raw_sha.update(data if len(data) <= left else data[:left])
            # copies of an earlier hunk are found first, so they are never compressed
            where = [0] * count
            todo = []
            for k in range(count):
                sha = sha1_of(data[k * hb:(k + 1) * hb]).digest()
                hit = seen.get(sha)
                if hit is None:
                    seen[sha] = index + k
                    where[k] = -1
                    todo.append(k)
                else:
                    where[k] = hit
            jobs = []
            for i in range(0, len(todo), per):
                part = todo[i:i + per]
                if part[-1] - part[0] == len(part) - 1:         # neighbours: one slice of the batch
                    blob = data[part[0] * hb:(part[-1] + 1) * hb]
                else:
                    blob = b"".join([data[k * hb:(k + 1) * hb] for k in part])
                jobs.append((blob, [hints[k] for k in part]))
            waiting.append((count, where, todo, pool.submit(jobs)))
            index += count
            # the next batch is read and hashed while the workers are busy with this one; two batches in flight
            while len(waiting) > 2:
                settle(waiting.popleft())
        while waiting:
            if cancel is not None and cancel():
                raise Cancelled("cancelled")
            settle(waiting.popleft())
        if index != hunk_count:
            raise ChdWriteError("the image is shorter than its layout says")
        map_offset = pos
        f.write(_build_map(types, lens, offs, crcs, first))
        raw = raw_sha.digest()
        sha1 = _overall_sha1(raw, metadata)
        header = b"MComprHD" + struct.pack(">II", HEADER, 5)
        header += b"".join(n.encode("ascii") if n else bytes(4) for n in names)
        header += struct.pack(">QQQII", logical, map_offset, HEADER if metadata else 0, hb, image.unit_bytes)
        header += raw + sha1 + bytes(20)
        size = f.tell()
        f.seek(0)
        f.write(header)
        f.flush()
        os.fsync(f.fileno())
        ok = True
    finally:
        f.close()
        getattr(reader, "close", lambda: None)()
        pool.close()
        if not ok:
            try:
                os.unlink(out_path)
            except OSError:
                pass
    return {"sha1": sha1.hex(), "raw_sha1": raw.hex(), "size": size, "hunks": hunk_count, "stored": stored,
            "codecs": [n for n in names if n], "workers": threads,
            # "processes" only when every slice was compressed by a worker: a worker that failed (its slices were
            # redone here) shows up as "threads", so a broken pool is visible, not just slow
            "engine": "processes" if pool.processes and threads > 1 and not pool.fallbacks else "threads",
            "fallbacks": pool.fallbacks}
