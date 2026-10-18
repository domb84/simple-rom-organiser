"""LhA / LZH archives (stdlib only): the format of the Amiga's archives, WHDLoad installs among them.

Reads header levels 0, 1 and 2 and the methods ``-lh0-`` (stored), ``-lh1-`` (LHarc 1.x: LZSS with an adaptive Huffman code, 4 KiB
window) and ``-lh4-`` ``-lh5-`` ``-lh6-`` ``-lh7-`` (LZSS + static Huffman blocks, 4 / 8 / 32 / 64 KiB windows); ``-lzs-`` / ``-lz5-``
are not supported (rare, old). Directories (``-lhd-``) are listed.
Every extracted file is checked against the CRC-16 of its header. About 1 MB/s of output per core in pure Python - an install is
some hundred KB.

    arc = LhaFile(path_or_bytes)
    for e in arc.entries: ... e.name, e.size, e.method, e.is_dir
    data = arc.read(e)            # bytes, LhaError on a bad CRC or an unknown method
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Optional, Union

__all__ = ["LhaFile", "LhaEntry", "LhaError", "crc16"]


class LhaError(Exception):
    """A damaged archive, an unknown method or a bad CRC."""


# --------------------------------------------------------------------------- CRC-16 (IBM / ARC, reflected 0xA001)

def _crc_table() -> list[int]:
    out = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        out.append(c)
    return out


_CRC = _crc_table()


def crc16(data: bytes, crc: int = 0) -> int:
    t = _CRC
    for b in data:
        crc = t[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc


# --------------------------------------------------------------------------- headers

@dataclass
class LhaEntry:
    name: str                 # path inside the archive, "/" separated
    method: str               # "-lh5-" ...
    size: int                 # original size
    packed: int
    crc: int
    offset: int               # where the packed data starts
    level: int
    comment: str = ""

    @property
    def is_dir(self) -> bool:
        return self.method == "-lhd-"


def _name(raw: bytes) -> tuple[str, str]:
    """``(name, comment)`` of a level 0 / 1 name field (Amiga LhA puts a comment after a NUL)."""
    name, _sep, comment = raw.partition(b"\x00")
    text = name.decode("latin-1").replace("\\", "/").replace("\xff", "/")
    return text, comment.decode("latin-1")


def _ext_headers(data: bytes, pos: int, first_size: int, size_bytes: int = 2) -> tuple[int, dict]:
    """Read the chain of extended headers starting at ``pos`` (the first one ``first_size`` bytes long): ``(end, {type: data})``."""
    found: dict[int, bytes] = {}
    size = first_size
    while size:                                  # (each: type, data, the size of the next one)
        if pos + size > len(data) or size < size_bytes + 1:
            raise LhaError("truncated extended header")
        found[data[pos]] = data[pos + 1:pos + size - size_bytes]
        nxt = int.from_bytes(data[pos + size - size_bytes:pos + size], "little")
        pos += size
        size = nxt
    return pos, found


def _parse(data: bytes) -> list[LhaEntry]:
    out: list[LhaEntry] = []
    pos = 0
    n = len(data)
    while pos < n:
        if data[pos] == 0:                       # (end of archive)
            break
        if pos + 22 > n:
            raise LhaError("truncated header")
        level = data[pos + 20]
        method = data[pos + 2:pos + 7].decode("latin-1")
        packed, size = struct.unpack_from("<II", data, pos + 7)
        if level in (0, 1):
            hsize = data[pos]
            nlen = data[pos + 21]
            name, comment = _name(data[pos + 22:pos + 22 + nlen])
            crc = int.from_bytes(data[pos + 22 + nlen:pos + 24 + nlen], "little")
            end = pos + 2 + hsize
            if level == 1:
                ext_size = int.from_bytes(data[end - 2:end], "little")
                end2, ext = _ext_headers(data, end, ext_size)
                packed -= end2 - end                 # (level 1: the packed size counts the extended headers)
                if 2 in ext:
                    name = ext[2].decode("latin-1").replace("\xff", "/") + name
                if 1 in ext:
                    name = ext[1].decode("latin-1")
                end = end2
        elif level == 2:
            hsize = int.from_bytes(data[pos:pos + 2], "little")
            crc = int.from_bytes(data[pos + 21:pos + 23], "little")
            ext_size = int.from_bytes(data[pos + 24:pos + 26], "little")
            _e, ext = _ext_headers(data, pos + 26, ext_size)
            fname = ext.get(1, b"").decode("latin-1")
            dname = ext.get(2, b"").decode("latin-1").replace("\xff", "/")
            name, comment = dname + fname, ext.get(0x3F, b"").decode("latin-1")
            end = pos + hsize
        else:
            raise LhaError(f"unknown header level {level}")
        if end + packed > n:
            raise LhaError(f"truncated data of {name}")
        out.append(LhaEntry(name=name.strip("/") if method == "-lhd-" else name, method=method, size=size, packed=packed,
                            crc=crc, offset=end, level=level, comment=comment))
        pos = end + packed
    return out


# --------------------------------------------------------------------------- -lh4- .. -lh7- decoder

_DICBIT = {"-lh4-": 12, "-lh5-": 13, "-lh6-": 15, "-lh7-": 16}
_NC = 510            # 256 literals + lengths 3..256
_NT = 19
_TBIT = 5
_CBIT = 9


class _Bits:
    __slots__ = ("data", "pos", "buf", "nbits")

    def __init__(self, data: bytes) -> None:
        self.data, self.pos, self.buf, self.nbits = data, 0, 0, 0

    def peek(self, n: int) -> int:
        while self.nbits < n:
            b = self.data[self.pos] if self.pos < len(self.data) else 0
            self.pos += 1
            self.buf = (self.buf << 8) | b
            self.nbits += 8
        return (self.buf >> (self.nbits - n)) & ((1 << n) - 1)

    def skip(self, n: int) -> None:
        if n:
            self.peek(n)
            self.nbits -= n
            self.buf &= (1 << self.nbits) - 1

    def get(self, n: int) -> int:
        if not n:
            return 0
        v = self.peek(n)
        self.nbits -= n
        self.buf &= (1 << self.nbits) - 1
        return v


class _Table:
    """A canonical Huffman code (codes by length, then symbol) as a lookup table of its longest code."""
    __slots__ = ("sym", "lens", "width", "single")

    def __init__(self, lens: list[int], single: Optional[int] = None) -> None:
        self.lens = lens
        self.single = single
        self.sym: list[int] = []
        self.width = 0
        if single is not None:
            return
        width = max(lens) if lens else 0
        if width == 0:
            raise LhaError("empty Huffman table")
        if width > 16:
            raise LhaError("bad Huffman table")
        self.width = width
        table = [-1] * (1 << width)
        code = 0
        for length in range(1, width + 1):
            for s, sl in enumerate(lens):
                if sl == length:
                    shift = width - length
                    start = code << shift
                    if start + (1 << shift) > len(table):
                        raise LhaError("bad Huffman table")
                    for k in range(start, start + (1 << shift)):
                        table[k] = s
                    code += 1
            code <<= 1
        self.sym = table

    def decode(self, bits: _Bits) -> int:
        if self.single is not None:
            return self.single
        s = self.sym[bits.peek(self.width)]
        if s < 0:
            raise LhaError("bad Huffman code")
        bits.skip(self.lens[s])
        return s


def _read_pt(bits: _Bits, nn: int, nbit: int, special: int) -> _Table:
    n = bits.get(nbit)
    if n == 0:
        return _Table([0] * nn, single=bits.get(nbit))
    if n > nn:
        raise LhaError("bad table size")
    lens = [0] * nn
    i = 0
    while i < n:
        c = bits.peek(3)
        if c == 7:
            bits.skip(3)
            while bits.get(1):
                c += 1
                if c > 16:
                    raise LhaError("bad length")
        else:
            bits.skip(3)
        lens[i] = c
        i += 1
        if i == special:
            z = bits.get(2)
            for _ in range(z):
                if i < nn:
                    lens[i] = 0
                    i += 1
    return _Table(lens)


def _read_c(bits: _Bits, pt: _Table) -> _Table:
    n = bits.get(_CBIT)
    if n == 0:
        return _Table([0] * _NC, single=bits.get(_CBIT))
    if n > _NC:
        raise LhaError("bad table size")
    lens = [0] * _NC
    i = 0
    while i < n:
        c = pt.decode(bits)
        if c <= 2:
            run = 1 if c == 0 else bits.get(4) + 3 if c == 1 else bits.get(_CBIT) + 20
            i += run
        else:
            lens[i] = c - 2
            i += 1
    return _Table(lens)


def _decode_lh(data: bytes, size: int, dicbit: int) -> bytes:
    np_ = dicbit + 1
    pbit = 4 if dicbit <= 13 else 5
    bits = _Bits(data)
    out = bytearray()
    append = out.append
    while len(out) < size:
        blocksize = bits.get(16)
        if blocksize == 0:
            raise LhaError("empty block")
        pt = _read_pt(bits, _NT, _TBIT, 3)
        ct = _read_c(bits, pt)
        pp = _read_pt(bits, np_, pbit, -1)
        for _ in range(blocksize):
            c = ct.decode(bits)
            if c < 256:
                append(c)
            else:
                length = c - 253
                p = pp.decode(bits)
                if p > 1:
                    p = (1 << (p - 1)) + bits.get(p - 1)
                start = len(out) - p - 1
                if start < 0:
                    raise LhaError("match before the start")
                if p + 1 >= length:
                    out += out[start:start + length]
                else:
                    for k in range(length):
                        append(out[start + k])
            if len(out) >= size:
                break
    return bytes(out[:size])


# --------------------------------------------------------------------------- -lh1- decoder (LHarc 1.x: LZHUF)

_N1 = 4096           # window
_F1 = 60             # longest match
_NCHAR1 = 256 - 2 + _F1
_T1 = _NCHAR1 * 2 - 1
_R1 = _T1 - 1
_MAXFREQ1 = 0x8000
# the upper 6 bits of a position: a static code (LZHUF's d_code / d_len), the lower 6 bits plain
_DCODE = bytes([0] * 32 + [v for v in range(1, 4) for _ in range(16)] + [v for v in range(4, 12) for _ in range(8)]
               + [v for v in range(12, 24) for _ in range(4)] + [v for v in range(24, 48) for _ in range(2)] + list(range(48, 64)))
_DLEN = bytes([3] * 32 + [4] * 48 + [5] * 64 + [6] * 48 + [7] * 48 + [8] * 16)


def _decode_lh1(data: bytes, size: int) -> bytes:
    """LZSS (4 KiB window, matches of 3..60) with an adaptive Huffman code for literals and lengths."""
    T, R, NC = _T1, _R1, _NCHAR1
    freq = [1] * NC + [0] * (T + 1 - NC)
    son = [0] * T
    prnt = [0] * (T + NC)
    for i in range(NC):
        son[i] = i + T
        prnt[i + T] = i
    i, j = 0, NC
    while j <= R:
        freq[j] = freq[i] + freq[i + 1]
        son[j] = i
        prnt[i] = prnt[i + 1] = j
        i += 2
        j += 1
    freq[T] = 0xFFFF
    prnt[R] = 0

    def reconst() -> None:
        j = 0
        for i in range(T):
            if son[i] >= T:
                freq[j] = (freq[i] + 1) // 2
                son[j] = son[i]
                j += 1
        i, j = 0, NC
        while j < T:
            f = freq[i] + freq[i + 1]
            freq[j] = f
            k = j - 1
            while f < freq[k]:
                k -= 1
            k += 1
            freq[k + 1:j + 1] = freq[k:j]
            freq[k] = f
            son[k + 1:j + 1] = son[k:j]
            son[k] = i
            i += 2
            j += 1
        for i in range(T):
            k = son[i]
            prnt[k] = i
            if k < T:
                prnt[k + 1] = i

    def update(c: int) -> None:
        if freq[R] == _MAXFREQ1:
            reconst()
        c = prnt[c + T]
        while True:
            freq[c] += 1
            k = freq[c]
            l = c + 1
            if k > freq[l]:
                l += 1
                while k > freq[l]:
                    l += 1
                l -= 1
                freq[c] = freq[l]
                freq[l] = k
                i = son[c]
                prnt[i] = l
                if i < T:
                    prnt[i + 1] = l
                j = son[l]
                son[l] = i
                prnt[j] = c
                if j < T:
                    prnt[j + 1] = c
                son[c] = j
                c = l
            c = prnt[c]
            if c == 0:
                break

    bits = _Bits(data)
    get = bits.get
    out = bytearray(b" " * _N1)              # (the window starts full of spaces)
    append = out.append
    end = _N1 + size
    while len(out) < end:
        c = son[R]
        while c < T:
            c = son[c + get(1)]
        c -= T
        update(c)
        if c < 256:
            append(c)
            continue
        length = c - 253
        b = get(8)
        hi = _DCODE[b] << 6
        extra = _DLEN[b] - 2
        b = (b << extra) | get(extra) if extra else b
        p = hi | (b & 0x3F)
        start = len(out) - p - 1
        if p + 1 >= length:
            out += out[start:start + length]
        else:
            for k in range(length):
                append(out[start + k])
    return bytes(out[_N1:end])


# --------------------------------------------------------------------------- archive

class LhaFile:
    """An LhA archive read into memory (installs and their archives are small)."""

    def __init__(self, source: Union[str, os.PathLike, bytes]) -> None:
        if isinstance(source, (bytes, bytearray)):
            self.data = bytes(source)
        else:
            with open(source, "rb") as fh:
                self.data = fh.read()
        self.entries = _parse(self.data)

    def names(self) -> list[str]:
        return [e.name for e in self.entries]

    def entry(self, name: str) -> Optional[LhaEntry]:
        low = name.casefold()
        for e in self.entries:
            if e.name.casefold() == low:
                return e
        return None

    def read(self, entry: Union[LhaEntry, str]) -> bytes:
        e = self.entry(entry) if isinstance(entry, str) else entry
        if e is None:
            raise LhaError(f"no such file: {entry}")
        if e.is_dir:
            return b""
        raw = self.data[e.offset:e.offset + e.packed]
        if e.method == "-lh0-":
            out = raw
        elif e.method in _DICBIT:
            out = _decode_lh(raw, e.size, _DICBIT[e.method])
        elif e.method == "-lh1-":
            out = _decode_lh1(raw, e.size)
        else:
            raise LhaError(f"{e.name}: unsupported method {e.method}")
        if len(out) != e.size or crc16(out) != e.crc:
            raise LhaError(f"{e.name}: CRC mismatch")
        return out
