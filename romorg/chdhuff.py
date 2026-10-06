"""MAME's Huffman coding as CHD files use it (stdlib only): the bit reader, the canonical decoder with its two tree
encodings, and the two codecs built on it.

``huff``  (``chd_huffman_decompressor``): one 256-symbol tree (codes up to 16 bits, stored Huffman-coded), then one
          code per byte of the hunk.
``avhu``  (``avhuff_decoder``, laserdisc CHDs): a frame of ``metadata``, ``channels`` audio streams (16-bit deltas,
          two Huffman trees for the high / low bytes, or raw, or FLAC) and a YUY2 picture whose Y / Cb / Cr bytes are
          delta + run-length Huffman coded. The decoded hunk starts with a ``chav`` header.

Everything here is plain Python and runs once per hunk, so it is slow next to the C codecs (a few MB/s). That is
acceptable because chdman only picks ``huff`` for the hunks nothing else compresses better - a small share of a file.
"""

from __future__ import annotations

import sys
import zlib
from struct import Struct, unpack_from
from typing import List

__all__ = ["HuffError", "BitReader", "Huffman", "huff_decode", "avhuff_decode"]

_PAD = bytes(16)


class HuffError(ValueError):
    """Damaged Huffman data."""


class BitReader:
    """MSB-first bit reader. Reading past the end yields zero bits (like MAME's ``bitstream_in``); the caller checks
    :attr:`overflow` afterwards."""

    def __init__(self, data, start: int = 0, end=None):
        body = bytes(data[start:end])
        self.nbits = len(body) * 8
        self.data = body + _PAD
        self.pos = 0

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        pos = self.pos
        first = pos >> 3
        last = (pos + n + 7) >> 3
        chunk = self.data[first:last]
        if len(chunk) < last - first:
            chunk += bytes(last - first - len(chunk))
        val = int.from_bytes(chunk, "big") >> ((last - first) * 8 - (pos & 7) - n)
        self.pos = pos + n
        return val & ((1 << n) - 1)

    def align(self) -> int:
        """Skip to the next byte boundary; the byte offset reached (``bitstream_in::flush``)."""
        self.pos = (self.pos + 7) & ~7
        return self.pos >> 3

    @property
    def overflow(self) -> bool:
        return self.pos > self.nbits


class Huffman:
    """Canonical Huffman decoder for ``numcodes`` symbols of at most ``maxbits`` bits."""

    def __init__(self, numcodes: int, maxbits: int):
        self.numcodes = numcodes
        self.maxbits = maxbits
        self.bits = 1           # width of the lookup window (the longest code in use)
        self.table: List[int] = [0, 0]      # window -> symbol | code length << 16 (0 = no such code)

    # -- trees
    def import_tree_rle(self, br: BitReader) -> None:
        """Code lengths stored directly, runs as ``1, length, count - 3`` (the CHD map and the A/V codec)."""
        numbits = 5 if self.maxbits >= 16 else 4 if self.maxbits >= 8 else 3
        n = self.numcodes
        lengths: List[int] = []
        while len(lengths) < n:
            nb = br.read(numbits)
            if nb != 1:
                lengths.append(nb)
                continue
            nb = br.read(numbits)
            if nb == 1:
                lengths.append(1)
                continue
            rep = br.read(numbits) + 3
            if len(lengths) + rep > n:
                raise HuffError("bad Huffman tree")
            lengths.extend([nb] * rep)
        self._build(lengths)

    def import_tree_huffman(self, br: BitReader) -> None:
        """Code lengths that are themselves Huffman coded with a small 24-symbol tree (the ``huff`` codec)."""
        small = Huffman(24, 6)
        lengths = [br.read(3)]
        start = br.read(3) + 1
        count = 0
        for index in range(1, 24):
            if index < start or count == 7:
                lengths.append(0)
            else:
                count = br.read(3)
                lengths.append(0 if count == 7 else count)
        small._build(lengths)
        rlefullbits = (self.numcodes - 9).bit_length()
        n = self.numcodes
        out: List[int] = []
        last = 0
        while len(out) < n:
            value = small.decode_one(br)
            if value != 0:
                last = value - 1
                out.append(last)
            else:
                count = br.read(3) + 2
                if count == 7 + 2:
                    count += br.read(rlefullbits)
                out.extend([last] * min(count, n - len(out)))
            if br.overflow:
                raise HuffError("truncated Huffman tree")
        self._build(out)

    def _build(self, lengths: List[int]) -> None:
        maxbits = self.maxbits
        histo = [0] * 33
        for b in lengths:
            if b > maxbits:
                raise HuffError("bad Huffman tree")
            histo[b] += 1
        start = 0
        for ln in range(32, 0, -1):
            both = start + histo[ln]
            if ln != 1 and both & 1:
                raise HuffError("bad Huffman tree")
            histo[ln] = start
            start = both >> 1
        used = max(lengths) if lengths else 0
        used = max(used, 1)
        table = [0] * (1 << used)
        for sym, b in enumerate(lengths):
            if b == 0:
                continue
            code = histo[b]
            histo[b] += 1
            span = 1 << (used - b)
            lo = code << (used - b)
            if lo + span > len(table):
                raise HuffError("bad Huffman tree")
            table[lo:lo + span] = [sym | (b << 16)] * span
        self.bits = used
        self.table = table

    # -- decoding
    def decode_one(self, br: BitReader) -> int:
        pos = br.pos
        bits = self.bits
        at = pos >> 3
        val = unpack_from(">I", br.data, at)[0] if at + 4 <= len(br.data) else 0
        ent = self.table[(val >> (32 - (pos & 7) - bits)) & ((1 << bits) - 1)]
        br.pos = pos + (ent >> 16)
        return ent & 0xFFFF

    def decode_bytes(self, br: BitReader, count: int) -> bytearray:
        """``count`` symbols as bytes (the hot loop of the ``huff`` codec: one table lookup per byte)."""
        table = self.table
        bits = self.bits
        mask = (1 << bits) - 1
        data = br.data
        limit = len(data) - 4
        byte = br.pos >> 3
        have = 8 - (br.pos & 7)
        acc = data[byte] & ((1 << have) - 1) if byte < len(data) else 0
        byte += 1
        out = bytearray(count)
        for i in range(count):
            if have < bits:
                acc = ((acc & ((1 << have) - 1)) << 32) | (unpack_from(">I", data, byte)[0] if byte <= limit else 0)
                byte += 4
                have += 32
            ent = table[(acc >> (have - bits)) & mask]
            out[i] = ent & 0xFF
            have -= ent >> 16
        br.pos = byte * 8 - have
        return out


# --------------------------------------------------------------------------- huff
# A hunk is 4096+ one-byte codes; a Python loop manages about 3 per microsecond, far too slow for the share of hunks
# chdman gives to this codec on barely compressible data (1 in 6 on a PlayStation 2 DVD). So the codes are handed to
# zlib: a deflate "dynamic" block is nothing but canonical Huffman codes for the byte values, and MAME's codes are
# deflate's mirrored (MAME numbers the LONGEST codes from zero, deflate the shortest), i.e. the same tree read on the
# complemented bit stream, with the symbols of each code length in reverse order. Two differences are worked around:
#   * deflate fills bytes from the low bit: every byte is bit-reversed (one ``translate``);
#   * deflate needs a code for "end of block" and MAME's tree leaves no room. The longest all-zero MAME code (its
#     symbol is ``x``, the rarest kind) is lengthened by one bit and shares its place with the end mark; whenever
#     ``x`` turns up, zlib has read one bit too many, so decoding restarts just behind it.
# Trees deflate cannot express (a code of 15 or 16 bits, an incomplete tree) take the Python loop.
_REV = bytes(int(format(i, "08b")[::-1], 2) for i in range(256))
_NIB = [format(i, "04b") for i in range(16)]
# last block, dynamic codes, 257 literal codes, 1 distance code, 19 code-length codes: 4 bits for the lengths 0..15
_DEFLATE_HEAD = "101" + "00000" + "00000" + "1111" + "".join(
    "001" if sym < 16 else "000" for sym in (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15))
_Q = Struct(">Q").unpack_from
_MAX_RESTARTS = 24


def _tree_lengths(br: BitReader) -> bytes:
    """The 256 code lengths of a ``huff`` hunk: ``import_tree_huffman`` written for speed (it runs per hunk). The
    lengths are coded with a small tree of 24 symbols whose own lengths open the hunk (3 bits each); symbol 0
    repeats the last length."""
    data = br.data
    v = int.from_bytes(data[:10], "big")
    lengths = [v >> 77]
    start = ((v >> 74) & 7) + 1
    sh = 74
    count = 0
    lengths += [0] * (start - 1)
    for index in range(start, 24):
        if count == 7:
            lengths += [0] * (24 - index)
            break
        sh -= 3
        count = (v >> sh) & 7
        lengths.append(0 if count == 7 else count)
    pos = 80 - sh
    # canonical codes, the longest first (MAME's order); tv = symbol, tl = code length per window of ``bits`` bits
    bits = max(lengths) or 1
    tv = [0] * (1 << bits)
    tl = [0] * (1 << bits)
    code = 0
    for ln in range(bits, 0, -1):
        span = 1 << (bits - ln)
        for sym in range(24):
            if lengths[sym] == ln:
                lo = code * span
                tv[lo:lo + span] = [sym] * span
                tl[lo:lo + span] = [ln] * span
                code += 1
        if ln != 1 and code & 1:
            raise HuffError("bad Huffman tree")
        code >>= 1
    shift = 64 - bits
    mask = (1 << bits) - 1
    top = len(data) - 8
    out = bytearray()
    app = out.append
    last = 0
    n = 0
    while n < 256:
        at = pos >> 3
        if at > top:
            raise HuffError("truncated Huffman tree")
        w = _Q(data, at)[0] << (pos & 7)
        i = (w >> shift) & mask
        value = tv[i]
        used = tl[i]
        if value:
            last = value - 1
            app(last)
            n += 1
            if n < 256:                 # up to two more plain codes from the same 64-bit window
                i = (w >> (shift - used)) & mask
                value = tv[i]
                if value:
                    last = value - 1
                    app(last)
                    n += 1
                    used += tl[i]
                    if n < 256:
                        i = (w >> (shift - used)) & mask
                        value = tv[i]
                        if value:
                            last = value - 1
                            app(last)
                            n += 1
                            used += tl[i]
        else:
            count = ((w >> (61 - used)) & 7) + 2
            used += 3
            if count == 9:
                count += (w >> (56 - used)) & 0xFF
                used += 8
            out += bytes((last,)) * count
            n += count
        pos += used
    br.pos = pos
    if br.overflow:
        raise HuffError("truncated Huffman tree")
    return bytes(out[:256])


def _inflate_codes(lengths: bytes, comp: bytes, pos: int, size: int):
    """``size`` symbols of the tree ``lengths`` from bit ``pos`` of ``comp`` through zlib; None when the tree does
    not fit deflate."""
    lmax = max(lengths)
    if not 1 <= lmax <= 14:
        return None
    counts = [lengths.count(ln) for ln in range(1, lmax + 1)]
    if sum(c << (lmax - ln) for ln, c in enumerate(counts, 1)) != 1 << lmax:
        return None
    order = sorted(range(256), key=lengths.__getitem__)         # by (length, symbol)
    at = 256 - sum(counts)
    src = []
    dst = []
    x = 0
    for ln, c in enumerate(counts, 1):
        run = bytes(order[at:at + c])
        at += c
        if ln == lmax:
            x = run[0]
            run = run[1:]
        src.append(run)
        dst.append(run[::-1])
    perm = bytes.maketrans(b"".join(src), b"".join(dst))
    used_lengths = [ln for ln, c in enumerate(counts, 1) if c]
    dl = bytearray(lengths)
    dl[x] = lmax + 1
    head = _DEFLATE_HEAD + "".join(map(_NIB.__getitem__, dl)) + _NIB[lmax + 1] + "0000"
    hbits = len(head)
    hint = int(head, 2)
    # MAME reads zero bits past the end of the data (``bitstream_in``), and so must zlib: when the hunk's last code
    # is x, the data ends inside x's lengthened deflate code and zlib would wait for one bit more. One byte of
    # zeros (ones on the complemented stream) is appended; whether a code really reached into it is decided on the
    # bit count at the end, as MAME's ``overflow()`` does.
    nbits = len(comp) * 8
    sbits = nbits + 8
    stream = int.from_bytes(comp + bytes(1), "big") ^ ((1 << sbits) - 1)
    xb = bytes([x])
    parts = []
    need = size
    restarts = 0
    while need > 0:
        if restarts > _MAX_RESTARTS:    # x is a common symbol here (a flat tree): the plain loop is cheaper
            dec = Huffman(256, 16)
            dec._build(list(lengths))
            br = BitReader(comp)
            br.pos = pos
            tail = dec.decode_bytes(br, need)
            if br.overflow:
                raise HuffError("truncated Huffman data")
            return b"".join(parts).translate(perm) + bytes(tail)
        restarts += 1
        if pos >= nbits:                # every code has at least one bit: the next one lies wholly past the data
            raise HuffError("truncated Huffman data")
        rem = sbits - pos
        total = hbits + rem
        pad = -total % 8
        blob = (((hint << rem) | (stream & ((1 << rem) - 1))) << pad).to_bytes((total + pad) >> 3, "big")
        d = zlib.decompressobj(-15)
        try:
            raw = d.decompress(blob.translate(_REV), need)
        except zlib.error:
            return None
        i = raw.find(xb)
        if i < 0:
            if len(raw) == need:
                parts.append(raw)
                took = raw.translate(lengths)
                pos += sum(ln * took.count(ln) for ln in used_lengths)
                break
            if not d.eof:
                raise HuffError("truncated Huffman data")
            i = len(raw)                # the end mark: x was the next symbol
        pre = raw[:i]
        parts += (pre, xb)
        need -= i + 1
        took = pre.translate(lengths)       # the code length of every symbol decoded so far
        pos += sum(ln * took.count(ln) for ln in used_lengths) + lmax
    if pos > nbits:                     # the last codes took bits past the end (bitstream_in::overflow)
        raise HuffError("truncated Huffman data")
    return b"".join(parts).translate(perm)


def huff_decode(comp: bytes, size: int) -> bytes:
    """One ``huff`` hunk of ``size`` bytes."""
    br = BitReader(comp)
    lengths = _tree_lengths(br)
    out = _inflate_codes(lengths, comp, br.pos, size)
    if out is None:
        dec = Huffman(256, 16)
        dec._build(list(lengths))
        out = bytes(dec.decode_bytes(br, size))
        if br.overflow:
            raise HuffError("truncated Huffman data")
    return out


# --------------------------------------------------------------------------- avhu
def _rle_count(code: int) -> int:
    """How many pixels a run code (0x100 and up) stands for: 8 to 15, then 16, 32, ... 2048."""
    if code <= 0x107:
        return 8 + (code - 0x100)
    return 16 << (code - 0x108)


def _avhuff_video(src: bytes, width: int, height: int, out: bytearray, at: int) -> None:
    if not src or not src[0] & 0x80:
        raise HuffError("lossy A/V video is not a CHD format")
    br = BitReader(src)
    br.read(8)
    planes = []
    for _ in range(3):
        dec = Huffman(256 + 16, 16)
        dec.import_tree_rle(br)
        br.align()
        planes.append(dec)
    ydec, cbdec, crdec = planes
    # MAME decodes Y Cb Y Cr pixel by pixel from ONE bit stream, so the three planes cannot be read separately
    ytab, cbtab, crtab = ydec.table, cbdec.table, crdec.table
    ybits, cbbits, crbits = ydec.bits, cbdec.bits, crdec.bits
    data = br.data
    top = len(data) - 4
    pos = br.pos
    yprev = cbprev = crprev = 0
    pairs = width // 2
    order = ((0, ytab, ybits), (1, cbtab, cbbits), (0, ytab, ybits), (2, crtab, crbits))
    for dy in range(height):
        row = at + dy * width * 2
        runs = [0, 0, 0]                # RLE left per plane (Y, Cb, Cr); cleared at the end of each row
        prevs = [yprev, cbprev, crprev]
        for dx in range(pairs):
            base = row + dx * 4
            for k, (plane, table, bits) in enumerate(order):
                if runs[plane]:
                    runs[plane] -= 1
                else:
                    val = unpack_from(">I", data, pos >> 3)[0] if (pos >> 3) <= top else 0
                    ent = table[(val >> (32 - (pos & 7) - bits)) & ((1 << bits) - 1)]
                    pos += ent >> 16
                    sym = ent & 0xFFFF
                    if sym < 0x100:
                        prevs[plane] = (prevs[plane] + sym) & 0xFF
                    else:
                        runs[plane] = _rle_count(sym) - 1
                out[base + k] = prevs[plane]
        yprev, cbprev, crprev = prevs
    br.pos = pos
    if br.overflow or br.align() != len(src):
        raise HuffError("damaged A/V video data")


def _avhuff_audio(src: bytes, sizes: List[int], treesize: int, channels: int, samples: int, out: bytearray,
                  at: int) -> None:
    """Every channel is ``samples`` big-endian 16-bit values, one channel after the other."""
    off = 0
    if treesize == 0xFFFF:              # one mono FLAC stream per channel
        from . import flacdec
        for ch in range(channels):
            try:
                pcm, _end = flacdec.decode_frames(src, off, samples, channels=1)
            except flacdec.FlacError as exc:
                raise HuffError(f"damaged A/V FLAC audio: {exc}") from exc
            if sys.byteorder == "little":
                pcm.byteswap()
            out[at + ch * samples * 2:at + (ch + 1) * samples * 2] = pcm.tobytes()
            off += sizes[ch]
        return
    hi = lo = None
    if treesize:
        br = BitReader(src, 0, treesize)
        hi = Huffman(256, 16)
        hi.import_tree_rle(br)
        br.align()
        lo = Huffman(256, 16)
        lo.import_tree_rle(br)
        if br.align() != treesize:
            raise HuffError("damaged A/V audio trees")
        off = treesize
    for ch in range(channels):
        size = sizes[ch]
        dst = at + ch * samples * 2
        prev = 0
        if hi is None:
            if off + 2 * samples > len(src):
                raise HuffError("truncated A/V audio")
            for s in range(samples):
                prev = (prev + ((src[off + 2 * s] << 8) | src[off + 2 * s + 1])) & 0xFFFF
                out[dst + 2 * s] = prev >> 8
                out[dst + 2 * s + 1] = prev & 0xFF
        else:
            br = BitReader(src, off, off + size)
            for s in range(samples):
                delta = hi.decode_one(br) << 8
                delta |= lo.decode_one(br)
                prev = (prev + delta) & 0xFFFF
                out[dst + 2 * s] = prev >> 8
                out[dst + 2 * s + 1] = prev & 0xFF
            if br.overflow:
                raise HuffError("truncated A/V audio")
        off += size


def avhuff_decode(comp: bytes, size: int) -> bytes:
    """One ``avhu`` hunk: ``chav`` header, metadata, audio channels, YUY2 picture, zero padded to ``size``."""
    n = len(comp)
    if n < 8:
        raise HuffError("truncated A/V hunk")
    metasize, channels = comp[0], comp[1]
    samples = (comp[2] << 8) | comp[3]
    width = (comp[4] << 8) | comp[5]
    height = (comp[6] << 8) | comp[7]
    if n < 10 + 2 * channels:
        raise HuffError("truncated A/V hunk")
    treesize = (comp[8] << 8) | comp[9]
    sizes = [(comp[10 + 2 * c] << 8) | comp[11 + 2 * c] for c in range(channels)]
    audio_bytes = (treesize if treesize != 0xFFFF else 0) + sum(sizes)
    off = 10 + 2 * channels
    if off + audio_bytes >= n:
        raise HuffError("truncated A/V hunk")
    total = 12 + metasize + channels * samples * 2 + width * height * 2
    if total > size:
        raise HuffError("A/V frame larger than the hunk")
    out = bytearray(size)
    out[0:12] = b"chav" + bytes([metasize, channels, samples >> 8, samples & 0xFF, width >> 8, width & 0xFF,
                                 height >> 8, height & 0xFF])
    out[12:12 + metasize] = comp[off:off + metasize]
    off += metasize
    at = 12 + metasize
    if channels:
        _avhuff_audio(comp[off:off + audio_bytes], sizes, treesize, channels, samples, out, at)
        off += audio_bytes
        at += channels * samples * 2
    if width and height:
        _avhuff_video(comp[off:], width, height, out, at)
    return bytes(out)
