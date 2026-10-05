"""Pure-Python Zstandard decompressor (RFC 8878), the fallback of :mod:`romorg.zstdnative`.

Used only when no Zstandard library can be loaded (Python before 3.14 on a system without libzstd - in practice the
Windows build). It decodes whole frames into memory, which is how CHD hunks come: one frame of a few kilobytes each.
It is slow (about a megabyte per second) but complete: raw, RLE and compressed blocks, Huffman literals (one or four
streams, direct or FSE-coded weights, "treeless" reuse), FSE sequences in all four modes and repeat offsets.
Dictionaries are not supported (chdman uses none) and the optional content checksum is not checked - the caller
compares the size, and the CHD's own hashes cover the content.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

__all__ = ["ZstdDecodeError", "decompress"]

MAGIC = 0xFD2FB528


class ZstdDecodeError(ValueError):
    """Not a valid Zstandard frame."""


# code -> (baseline, extra bits)
_LL = [(i, 0) for i in range(16)] + [(16, 1), (18, 1), (20, 1), (22, 1), (24, 2), (28, 2), (32, 3), (40, 3), (48, 4),
                                     (64, 6), (128, 7), (256, 8), (512, 9), (1024, 10), (2048, 11), (4096, 12),
                                     (8192, 13), (16384, 14), (32768, 15), (65536, 16)]
_ML = [(i + 3, 0) for i in range(32)] + [(35, 1), (37, 1), (39, 1), (41, 1), (43, 2), (47, 2), (51, 3), (59, 3),
                                         (67, 4), (83, 4), (99, 5), (131, 7), (259, 8), (515, 9), (1027, 10),
                                         (2051, 11), (4099, 12), (8195, 13), (16387, 14), (32771, 15), (65539, 16)]
_LL_DEFAULT = (6, [4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2, 1, 1, 1, 1, 1,
                   -1, -1, -1, -1])
_ML_DEFAULT = (6, [1, 4, 3, 2, 2, 2, 2, 2, 2] + [1] * 37 + [-1] * 7)
_OF_DEFAULT = (5, [1, 1, 1, 1, 1, 1, 2, 2, 2] + [1] * 15 + [-1] * 5)


class _Forward:
    """Little-endian bit reader going forward (FSE table descriptions)."""

    def __init__(self, data: bytes, start: int):
        self.value = int.from_bytes(data[start:], "little")
        self.start = start
        self.pos = 0
        self.limit = (len(data) - start) * 8

    def read(self, n: int) -> int:
        v = (self.value >> self.pos) & ((1 << n) - 1)
        self.pos += n
        return v

    def end(self) -> int:
        """The byte offset after the last (partly) used byte."""
        if self.pos > self.limit:
            raise ZstdDecodeError("truncated table description")
        return self.start + (self.pos + 7) // 8


class _Backward:
    """A Zstandard bit stream: read from its end, the top set bit of the last byte marks where the data stops."""

    def __init__(self, data: bytes):
        if not data or not data[-1]:
            raise ZstdDecodeError("bad bit stream")
        self.value = int.from_bytes(data, "little")
        self.pos = len(data) * 8 - (9 - data[-1].bit_length())

    def read(self, n: int) -> int:
        self.pos -= n
        pos = self.pos
        if pos >= 0:
            return (self.value >> pos) & ((1 << n) - 1)
        return (self.value << -pos) & ((1 << n) - 1)          # past the start: zero bits


def _fse_read(data: bytes, start: int, max_log: int, max_symbols: int) -> Tuple[tuple, int]:
    """A normalised distribution -> ``(table, next byte)``."""
    br = _Forward(data, start)
    log = br.read(4) + 5
    if log > max_log:
        raise ZstdDecodeError("FSE accuracy too large")
    remaining = 1 << log
    freqs: List[int] = []
    while remaining > 0 and len(freqs) < max_symbols:
        bits = (remaining + 1).bit_length()
        val = br.read(bits)
        lower = (1 << (bits - 1)) - 1
        threshold = (1 << bits) - 1 - (remaining + 1)
        if (val & lower) < threshold:
            br.pos -= 1
            val &= lower
        elif val > lower:
            val -= threshold
        proba = val - 1
        remaining -= -proba if proba < 0 else proba
        freqs.append(proba)
        if proba == 0:
            repeat = br.read(2)
            while True:
                freqs += [0] * repeat
                if repeat != 3:
                    break
                repeat = br.read(2)
    if remaining != 0 or len(freqs) > max_symbols:
        raise ZstdDecodeError("bad FSE distribution")
    return _fse_table(log, freqs), br.end()


def _fse_table(log: int, freqs: List[int]) -> tuple:
    """``(accuracy log, symbol, bits to read, baseline)`` per state."""
    size = 1 << log
    symbols = [0] * size
    desc = [0] * len(freqs)
    high = size
    for s, f in enumerate(freqs):
        if f == -1:
            high -= 1
            symbols[high] = s
            desc[s] = 1
    step = (size >> 1) + (size >> 3) + 3
    mask = size - 1
    pos = 0
    for s, f in enumerate(freqs):
        if f <= 0:
            continue
        desc[s] = f
        for _ in range(f):
            symbols[pos] = s
            pos = (pos + step) & mask
            while pos >= high:
                pos = (pos + step) & mask
    if pos != 0:
        raise ZstdDecodeError("bad FSE distribution")
    nbits = [0] * size
    base = [0] * size
    for i in range(size):
        s = symbols[i]
        d = desc[s]
        desc[s] = d + 1
        nb = log - (d.bit_length() - 1)
        nbits[i] = nb
        base[i] = (d << nb) - size
    return log, symbols, nbits, base


def _rle_table(symbol: int) -> tuple:
    return 0, [symbol], [0], [0]


# --------------------------------------------------------------------------- Huffman literals
def _huf_weights(data: bytes, pos: int) -> Tuple[List[int], int]:
    if pos >= len(data):
        raise ZstdDecodeError("truncated Huffman tree")
    head = data[pos]
    pos += 1
    if head >= 128:
        count = head - 127
        nbytes = (count + 1) // 2
        raw = data[pos:pos + nbytes]
        if len(raw) < nbytes:
            raise ZstdDecodeError("truncated Huffman tree")
        weights = []
        for b in raw:
            weights += (b >> 4, b & 15)
        return weights[:count], pos + nbytes
    if pos + head > len(data):
        raise ZstdDecodeError("truncated Huffman tree")
    (log, symbols, nbits, base), at = _fse_read(data[pos:pos + head], 0, 6, 256)
    br = _Backward(data[pos + at:pos + head])
    s1 = br.read(log)
    s2 = br.read(log)
    weights = []
    while True:
        weights.append(symbols[s1])
        s1 = base[s1] + br.read(nbits[s1])
        if br.pos < 0:
            weights.append(symbols[s2])
            break
        weights.append(symbols[s2])
        s2 = base[s2] + br.read(nbits[s2])
        if br.pos < 0:
            weights.append(symbols[s1])
            break
        if len(weights) > 255:
            raise ZstdDecodeError("bad Huffman tree")
    return weights, pos + head


def _huf_table(weights: List[int]) -> tuple:
    """``(max bits, symbol per window, code length per window)``; the last weight is implied."""
    total = sum((1 << (w - 1)) for w in weights if w)
    if total == 0 or any(w > 11 for w in weights):
        raise ZstdDecodeError("bad Huffman tree")
    max_bits = total.bit_length()
    left = (1 << max_bits) - total
    if left & (left - 1):
        raise ZstdDecodeError("bad Huffman tree")
    weights = weights + [left.bit_length()]
    if max_bits > 11:
        raise ZstdDecodeError("bad Huffman tree")
    size = 1 << max_bits
    syms = bytearray(size)
    lens = bytearray(size)
    at = 0
    for w in range(1, max_bits + 1):            # the longest codes (weight 1) come first
        span = 1 << (w - 1)
        nb = max_bits + 1 - w
        for s, sw in enumerate(weights):
            if sw == w:
                syms[at:at + span] = bytes((s,)) * span
                lens[at:at + span] = bytes((nb,)) * span
                at += span
    if at != size:
        raise ZstdDecodeError("bad Huffman tree")
    return max_bits, bytes(syms), bytes(lens)


def _huf_stream(table: tuple, data: bytes, count: int, out: bytearray) -> None:
    max_bits, syms, lens = table
    if not data or not data[-1]:
        raise ZstdDecodeError("bad literal stream")
    value = int.from_bytes(data, "little") << max_bits        # zero bits below the start for the last look-ups
    pos = len(data) * 8 - (9 - data[-1].bit_length())
    mask = (1 << max_bits) - 1
    try:
        for _ in range(count):
            w = (value >> pos) & mask
            out.append(syms[w])
            pos -= lens[w]
    except ValueError:                  # a negative shift: codes before the start of the stream (damaged data)
        raise ZstdDecodeError("bad literal stream") from None
    if pos != 0:                        # libzstd: the stream must be used up exactly (BIT_endOfDStream)
        raise ZstdDecodeError("bad literal stream")


def _literals(data: bytes, pos: int, end: int, state: dict) -> Tuple[bytes, int]:
    if pos >= end:
        raise ZstdDecodeError("truncated block")
    b0 = data[pos]
    kind = b0 & 3
    fmt = (b0 >> 2) & 3
    if kind < 2:                        # raw / RLE
        if fmt in (0, 2):
            size, pos = b0 >> 3, pos + 1
        elif fmt == 1:
            size, pos = int.from_bytes(data[pos:pos + 2], "little") >> 4, pos + 2
        else:
            size, pos = int.from_bytes(data[pos:pos + 3], "little") >> 4, pos + 3
        if kind == 0:
            if pos + size > end:
                raise ZstdDecodeError("truncated literals")
            return data[pos:pos + size], pos + size
        if pos >= end:
            raise ZstdDecodeError("truncated literals")
        return data[pos:pos + 1] * size, pos + 1
    hlen = (3, 3, 4, 5)[fmt]
    head = int.from_bytes(data[pos:pos + hlen], "little") >> 4
    nb = (10, 10, 14, 18)[fmt]
    regen = head & ((1 << nb) - 1)
    comp = head >> nb
    pos += hlen
    stop = pos + comp
    if stop > end:
        raise ZstdDecodeError("truncated literals")
    if kind == 2:
        weights, pos = _huf_weights(data[:stop], pos)
        state["huf"] = _huf_table(weights)
    table = state.get("huf")
    if table is None:
        raise ZstdDecodeError("literals reuse a Huffman tree that was never sent")
    out = bytearray()
    if fmt == 0:
        _huf_stream(table, data[pos:stop], regen, out)
    else:
        if pos + 6 > stop:
            raise ZstdDecodeError("truncated literals")
        s1, s2, s3 = (int.from_bytes(data[pos + 2 * i:pos + 2 * i + 2], "little") for i in range(3))
        pos += 6
        per = (regen + 3) // 4
        if pos + s1 + s2 + s3 > stop or 3 * per > regen:
            raise ZstdDecodeError("bad literal streams")
        _huf_stream(table, data[pos:pos + s1], per, out)
        _huf_stream(table, data[pos + s1:pos + s1 + s2], per, out)
        _huf_stream(table, data[pos + s1 + s2:pos + s1 + s2 + s3], per, out)
        _huf_stream(table, data[pos + s1 + s2 + s3:stop], regen - 3 * per, out)
    return bytes(out), stop


# --------------------------------------------------------------------------- sequences
def _seq_table(mode: int, data: bytes, pos: int, end: int, default: tuple, max_log: int, max_symbols: int,
               previous: Optional[tuple]) -> Tuple[tuple, int]:
    if mode == 0:
        return _fse_table(*default), pos
    if mode == 1:
        if pos >= end:
            raise ZstdDecodeError("truncated sequences")
        return _rle_table(data[pos]), pos + 1
    if mode == 2:
        table, at = _fse_read(data[:end], pos, max_log, max_symbols)
        return table, at
    if previous is None:
        raise ZstdDecodeError("sequences reuse a table that was never sent")
    return previous, pos


def _sequences(data: bytes, pos: int, end: int, literals: bytes, out: bytearray, state: dict) -> None:
    if pos >= end:
        raise ZstdDecodeError("truncated block")
    b0 = data[pos]
    if b0 == 0:
        out += literals
        return
    if b0 < 128:
        nseq, pos = b0, pos + 1
    elif b0 < 255:
        nseq, pos = ((b0 - 128) << 8) + data[pos + 1], pos + 2
    else:
        nseq, pos = data[pos + 1] + (data[pos + 2] << 8) + 0x7F00, pos + 3
    if pos >= end:
        raise ZstdDecodeError("truncated sequences")
    modes = data[pos]
    pos += 1
    ll, pos = _seq_table(modes >> 6, data, pos, end, _LL_DEFAULT, 9, 36, state.get("ll"))
    of, pos = _seq_table((modes >> 4) & 3, data, pos, end, _OF_DEFAULT, 8, 32, state.get("of"))
    ml, pos = _seq_table((modes >> 2) & 3, data, pos, end, _ML_DEFAULT, 9, 53, state.get("ml"))
    state["ll"], state["of"], state["ml"] = ll, of, ml
    br = _Backward(data[pos:end])
    read = br.read
    ll_log, ll_sym, ll_nb, ll_base = ll
    of_log, of_sym, of_nb, of_base = of
    ml_log, ml_sym, ml_nb, ml_base = ml
    ll_state = read(ll_log)
    of_state = read(of_log)
    ml_state = read(ml_log)
    rep = state["rep"]
    lit_pos = 0
    last = nseq - 1
    for i in range(nseq):
        of_code = of_sym[of_state]
        ml_code = ml_sym[ml_state]
        ll_code = ll_sym[ll_state]
        if ll_code > 35 or ml_code > 52 or of_code > 31:
            raise ZstdDecodeError("bad sequence code")
        value = (1 << of_code) + read(of_code)
        mbase, mextra = _ML[ml_code]
        mlen = mbase + read(mextra)
        lbase, lextra = _LL[ll_code]
        llen = lbase + read(lextra)
        if i != last:
            ll_state = ll_base[ll_state] + read(ll_nb[ll_state])
            ml_state = ml_base[ml_state] + read(ml_nb[ml_state])
            of_state = of_base[of_state] + read(of_nb[of_state])
        if br.pos < 0:
            raise ZstdDecodeError("truncated sequences")
        if value > 3:
            offset = value - 3
            rep[2], rep[1], rep[0] = rep[1], rep[0], offset
        else:
            idx = value - 1 + (1 if llen == 0 else 0)
            if idx == 0:
                offset = rep[0]
            else:
                offset = rep[idx] if idx < 3 else rep[0] - 1
                if offset <= 0:
                    raise ZstdDecodeError("bad repeat offset")
                if idx > 1:
                    rep[2] = rep[1]
                rep[1], rep[0] = rep[0], offset
        if llen:
            out += literals[lit_pos:lit_pos + llen]
            lit_pos += llen
        if offset > len(out):
            raise ZstdDecodeError("match before the start of the data")
        if offset >= mlen:
            start = len(out) - offset
            out += out[start:start + mlen]
        else:
            seg = bytes(out[-offset:])
            out += (seg * (mlen // offset + 1))[:mlen]
    if br.pos != 0 or lit_pos > len(literals):
        raise ZstdDecodeError("bad sequence stream")
    out += literals[lit_pos:]


# --------------------------------------------------------------------------- frames
def decompress(data: bytes, max_size: int = 1 << 31) -> bytes:
    """The content of the Zstandard frame at the start of ``data``."""
    data = bytes(data)
    if len(data) < 6 or int.from_bytes(data[:4], "little") != MAGIC:
        raise ZstdDecodeError("not a Zstandard frame")
    fhd = data[4]
    pos = 5
    single = bool(fhd & 0x20)
    if fhd & 0x08:
        raise ZstdDecodeError("reserved bit set in the frame header")
    if not single:
        pos += 1                                    # window descriptor: everything is kept in memory anyway
    did = (0, 1, 2, 4)[fhd & 3]
    if did and any(data[pos:pos + did]):
        raise ZstdDecodeError("the frame needs a dictionary")
    pos += did
    fcs = (1 if single else 0, 2, 4, 8)[fhd >> 6]
    pos += fcs
    out = bytearray()
    state: dict = {"rep": [1, 4, 8]}
    while True:
        if pos + 3 > len(data):
            raise ZstdDecodeError("truncated frame")
        head = int.from_bytes(data[pos:pos + 3], "little")
        pos += 3
        last, kind, size = head & 1, (head >> 1) & 3, head >> 3
        if kind == 0:
            if pos + size > len(data):
                raise ZstdDecodeError("truncated block")
            out += data[pos:pos + size]
            pos += size
        elif kind == 1:
            if pos >= len(data):
                raise ZstdDecodeError("truncated block")
            out += data[pos:pos + 1] * size
            pos += 1
        elif kind == 2:
            end = pos + size
            if end > len(data):
                raise ZstdDecodeError("truncated block")
            literals, at = _literals(data, pos, end, state)
            _sequences(data, at, end, literals, out, state)
            pos = end
        else:
            raise ZstdDecodeError("reserved block type")
        if len(out) > max_size:
            raise ZstdDecodeError("the frame is larger than expected")
        if last:
            break
    return bytes(out)
