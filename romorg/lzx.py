"""Amiga LZX archives (stdlib only): the format of ``unlzx`` / ``xadUnFile``, which some WHDLoad installs unpack from the game's
disks (Dungeon Master II, Pinball BrainDamage: an archive split over the disks, joined again by xadUnFileM).

Reads the methods 0 (stored) and 2 (LZX: LZ77 with a 64 KiB window, static Huffman blocks, "aligned offset" blocks); files packed
together ("merged": the members with no packed size go with the next one that has it) are one stream, cut by their sizes. Every
file is checked against the CRC-32 of its header, as unlzx does.

    arc = LzxFile(data)
    for name, body in arc.files(): ...      # LzxError on a damaged archive, an unknown method or a bad CRC
"""

from __future__ import annotations

import struct
import zlib

__all__ = ["LzxFile", "LzxError", "is_lzx"]


class LzxError(Exception):
    """A damaged archive, an unknown method or a bad CRC."""


def is_lzx(data: bytes) -> bool:
    return data[:3] == b"LZX"


_ONE = [0, 0, 0, 0] + [i // 2 for i in range(2, 30)]                    # (extra bits of a position / length slot)
_TWO = [0, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144,
        8192, 12288, 16384, 24576, 32768, 49152]                        # (the base of a slot)


class _Bits:
    """unlzx's bit order: 16-bit big-endian words, each read from its lowest bit up."""
    __slots__ = ("data", "pos", "buf", "n")

    def __init__(self, data: bytes) -> None:
        self.data, self.pos, self.buf, self.n = data, 0, 0, 0

    def peek(self, k: int) -> int:
        while self.n < k:                                 # (past the end: zero bits, as the C reads its buffer's tail)
            w = self.data[self.pos:self.pos + 2]
            self.pos += 2
            self.buf |= int.from_bytes(w.ljust(2, b"\0"), "big") << self.n
            self.n += 16
        return self.buf & ((1 << k) - 1)

    def skip(self, k: int) -> None:
        self.buf >>= k
        self.n -= k

    def get(self, k: int) -> int:
        v = self.peek(k)
        self.skip(k)
        return v


def _table(lens: list[int]) -> tuple[list[int], int]:
    """A canonical Huffman code (codes by length, then symbol; the first bit read is the code's top bit) as a lookup of
    ``1 << longest`` entries: ``(symbol << 5) | length`` by the next ``longest`` bits. A code that does not fill its space is damage
    (unlzx's make_decode_table refuses it)."""
    top = max(lens) if lens else 0
    if not top:
        raise LzxError("an empty Huffman table")
    tbl = [0] * (1 << top)
    code = 0
    for ln in range(1, top + 1):
        for sym, l2 in enumerate(lens):
            if l2 != ln:
                continue
            if code >> ln:
                raise LzxError("a damaged Huffman table")
            rev = int(f"{code:0{ln}b}"[::-1], 2)
            for j in range(rev, 1 << top, 1 << ln):
                tbl[j] = (sym << 5) | ln
            code += 1
        code <<= 1
    if code != 1 << (top + 1):
        raise LzxError("a damaged Huffman table")
    return tbl, top


def _sym(bits: _Bits, tbl: list[int], top: int) -> int:
    e = tbl[bits.peek(top)]
    bits.skip(e & 31)
    return e >> 5


def _decode(data: bytes, size: int) -> bytes:
    """``size`` bytes of a method 2 stream (unlzx's extract_normal / read_literal_table / decrunch)."""
    bits = _Bits(data)
    out = bytearray()
    lit_len = [0] * 768
    off_len = [0] * 8
    off_tbl, off_top = None, 0
    last = 1
    while len(out) < size:
        method = bits.get(3)
        if method == 3:                                   # (aligned offsets: the low 3 bits of a far offset by their own code)
            off_len = [bits.get(3) for _ in range(8)]
            off_tbl, off_top = _table(off_len)
        length = bits.get(8) << 16
        length |= bits.get(8) << 8
        length |= bits.get(8)
        if method != 1:                                   # (method 1: the code of the block before)
            pos, fix, max_sym = 0, 1, 256
            while True:                                   # (the literals' lengths, then the matches': each by a 20-symbol code)
                pre = [bits.get(4) for _ in range(20)]
                ptbl, ptop = _table(pre)
                while pos < max_sym:
                    s = _sym(bits, ptbl, ptop)
                    if s in (17, 18):
                        k, cnt = (4, 3) if s == 17 else (6 - fix, 19)
                        cnt += bits.get(k) + fix
                        while pos < max_sym and cnt:
                            lit_len[pos] = 0
                            pos += 1
                            cnt -= 1
                    elif s == 19:
                        cnt = bits.get(1) + 3 + fix
                        s = _sym(bits, ptbl, ptop)
                        v = (lit_len[pos] - s) % 17
                        while pos < max_sym and cnt:
                            lit_len[pos] = v
                            pos += 1
                            cnt -= 1
                    else:
                        lit_len[pos] = (lit_len[pos] - s) % 17
                        pos += 1
                fix -= 1
                max_sym += 512
                if max_sym != 768:
                    break
        elif not any(lit_len):
            raise LzxError("a block that reuses no code")
        ltbl, ltop = _table(lit_len)
        if method == 3 and off_tbl is None:
            raise LzxError("an aligned block with no offset code")
        end = len(out) + length
        while len(out) < end and len(out) < size:
            s = _sym(bits, ltbl, ltop)
            if s < 256:
                out.append(s)
                continue
            s -= 256
            slot = s & 31
            cnt, k = _TWO[slot], _ONE[slot]
            if k >= 3 and method == 3:
                k -= 3
                cnt += bits.get(k) << 3
                cnt += _sym(bits, off_tbl, off_top)    # type: ignore[arg-type]
            else:
                cnt += bits.get(k)
                if not cnt:
                    cnt = last
            last = cnt
            slot = (s >> 5) & 15
            n = _TWO[slot] + 3 + bits.get(_ONE[slot])
            start = len(out) - cnt
            if start < 0:                                 # (before the start: the window's zeros)
                pad = min(-start, n)
                out += bytes(pad)
                n -= pad
                start = len(out) - cnt
            if cnt >= n:
                out += out[start:start + n]
            else:
                for i in range(n):
                    out.append(out[start + i])
    return bytes(out[:size])


class LzxFile:
    def __init__(self, data: bytes) -> None:
        if not is_lzx(data) or len(data) < 10:
            raise LzxError("not an LZX archive")
        self.data = data
        self.groups: list[tuple[list[tuple[str, int, int]], int, int, int]] = []   # ([(name, size, crc)], method, start, packed)
        pos, pending = 10, []
        while pos + 31 <= len(data):
            h = bytearray(data[pos:pos + 31])
            hcrc = struct.unpack_from("<I", h, 26)[0]
            h[26:30] = bytes(4)
            nlen, clen = h[30], h[14]
            name = data[pos + 31:pos + 31 + nlen]
            comment = data[pos + 31 + nlen:pos + 31 + nlen + clen]
            if zlib.crc32(bytes(h) + name + comment) != hcrc:
                if pending or not self.groups:
                    raise LzxError("a damaged header")
                break                                     # (unlzx stops at a bad header: what follows is not the archive's)
            pos += 31 + nlen + clen
            size, packed = struct.unpack_from("<II", h, 2)
            pending.append((name.decode("latin-1"), size, struct.unpack_from("<I", h, 22)[0]))
            if packed:
                self.groups.append((pending, h[11], pos, packed))
                pending = []
                pos += packed
        if pending:
            raise LzxError("the archive ends inside a packed group (a part missing)")

    def files(self) -> list[tuple[str, bytes]]:
        out = []
        for members, method, start, packed in self.groups:
            raw = self.data[start:start + packed]
            if len(raw) < packed:
                raise LzxError("the archive is cut short (a part missing)")
            total = sum(m[1] for m in members)
            if method == 0:
                body = raw
            elif method == 2:
                body = _decode(raw, total)
            else:
                raise LzxError(f"pack method {method} is not supported")
            at = 0
            for name, size, crc in members:
                piece = body[at:at + size]
                at += size
                if zlib.crc32(piece) != crc:
                    raise LzxError(f"{name}: bad CRC")
                out.append((name, piece))
        return out
