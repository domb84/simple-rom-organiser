"""Pure-Python FLAC frame decoder (stdlib only) used for CHD 'cdfl' hunks.

A CHD ``cdfl`` hunk is a run of FLAC *frames* (no ``fLaC`` marker, no STREAMINFO),
2 channels, 16 bit, followed by the zlib-compressed subcode.  ``decode_frames``
decodes frames back to back until the requested number of samples per channel
has been produced and reports how many bytes it consumed, so the caller knows
where the next section starts.

Speed: the Rice residual is parsed from a ``'0'/'1'`` string (``str.find`` does
the unary part in C) and the predictors use ``sum(map(mul, ...))``; expect
roughly 1 MB/s of PCM per core.  The frame CRCs are *not* checked (the callers
verify the decoded audio against the CHD / DAT hashes).
"""

from __future__ import annotations

from array import array
from operator import add, sub
import math
import re

_sumprod = getattr(math, 'sumprod', None)
_RICE: dict = {}        # (rice parameter, codes in the partition) -> pattern matching the whole partition
_CODES: dict = {}       # rice parameter -> (pattern of one code, code string -> value)


class _RiceTable(dict):
    """``"0001" + low bits`` -> zigzag-decoded residual, filled on demand."""

    def __init__(self, k: int) -> None:
        super().__init__()
        self.k = k

    def __missing__(self, code: str) -> int:
        k = self.k
        zeros = len(code) - 1 - k
        u = (zeros << k) | (int(code[zeros + 1:], 2) if k else 0)
        v = self[code] = (u >> 1) ^ -(u & 1)
        return v
_LPC: dict = {}         # predictor order -> generated function


def _lpc_fn(order: int):
    """``f(warm, res, coefs, shift)`` for one LPC order, with the history kept in local variables (no slicing)."""
    fn = _LPC.get(order)
    if fn is None:
        hist = ["h%d" % i for i in range(order)]          # h0 = newest sample
        src = ["def f(out, res, c, shift):",
               "    %s = c" % (", ".join("c%d" % i for i in range(order)) + ","),
               "    %s = out[::-1]" % (", ".join(hist) + ","),
               "    append = out.append",
               "    for r in res:",
               "        v = r + ((%s) >> shift)" % " + ".join("c%d * h%d" % (i, i) for i in range(order))]
        if order > 1:
            src.append("        %s = %s" % (", ".join(reversed(hist)), ", ".join(reversed(["v"] + hist[:-1]))))
        else:
            src.append("        h0 = v")
        src += ["        append(v)", "    return out"]
        ns: dict = {}
        exec(chr(10).join(src), ns)                       # noqa: S102 - fixed template, integer order only
        fn = _LPC[order] = ns["f"]
    return fn

__all__ = ["FlacError", "decode_frames"]


class FlacError(Exception):
    pass


_BLOCK_SIZES = {1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608,
                8: 256, 9: 512, 10: 1024, 11: 2048, 12: 4096, 13: 8192,
                14: 16384, 15: 32768}
_SAMPLE_BITS = {0: 0, 1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32}


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value >> (bits - 1) else value


def _residual(bits: str, pos: int, blocksize: int, order: int):
    """Decode the partitioned Rice residual; returns (list, new bit position)."""
    method = int(bits[pos:pos + 2], 2)
    if method > 1:
        raise FlacError("reserved residual coding method")
    pbits = 4 if method == 0 else 5
    escape = (1 << pbits) - 1
    porder = int(bits[pos + 2:pos + 6], 2)
    pos += 6
    out: list = []
    append = out.append
    find = bits.find
    parts = 1 << porder
    per = blocksize >> porder
    if per < order or per == 0 and parts > 1:
        raise FlacError("bad partition order")
    for p in range(parts):
        count = per - order if p == 0 else per
        k = int(bits[pos:pos + pbits], 2)
        pos += pbits
        if k == escape:
            n = int(bits[pos:pos + 5], 2)
            pos += 5
            if n == 0:
                out.extend([0] * count)
            else:
                for _ in range(count):
                    append(_signed(int(bits[pos:pos + n], 2), n))
                    pos += n
            continue
        if not count:
            continue
        # One regex match finds where the partition ends, findall splits it into whole Rice codes and a
        # memoising table turns each code string into its value: the per-sample loop runs in C.
        key = (k, count)
        whole = _RICE.get(key)
        if whole is None:
            if len(_RICE) > 4096:
                _RICE.clear()
            whole = _RICE[key] = re.compile("(?:0*1[01]{%d}){%d}" % (k, count))
        m = whole.match(bits, pos)
        if m is None:
            raise FlacError("truncated residual")
        end = m.end()
        codes = _CODES.get(k)
        if codes is None:
            codes = _CODES[k] = (re.compile("0*1[01]{%d}" % k), _RiceTable(k))
        table = codes[1]
        if len(table) > 1 << 18:
            table.clear()
        out += map(table.__getitem__, codes[0].findall(bits, pos, end))
        pos = end
    return out, pos


def _subframe(bits: str, pos: int, blocksize: int, bps: int):
    if bits[pos] != "0":
        raise FlacError("bad subframe header")
    stype = int(bits[pos + 1:pos + 7], 2)
    pos += 7
    wasted = 0
    if bits[pos] == "1":
        i = bits.find("1", pos + 1)
        if i < 0:
            raise FlacError("truncated subframe")
        wasted = i - pos
        pos = i + 1
    else:
        pos += 1
    bps -= wasted
    if stype == 0:                                   # constant
        v = _signed(int(bits[pos:pos + bps], 2), bps)
        pos += bps
        samples = [v] * blocksize
    elif stype == 1:                                 # verbatim
        samples = []
        for _ in range(blocksize):
            samples.append(_signed(int(bits[pos:pos + bps], 2), bps))
            pos += bps
    elif 8 <= stype <= 12:                           # fixed predictor
        order = stype - 8
        samples = []
        for _ in range(order):
            samples.append(_signed(int(bits[pos:pos + bps], 2), bps))
            pos += bps
        res, pos = _residual(bits, pos, blocksize, order)
        out = samples
        append = out.append
        if order == 0:
            out = res
        elif order == 1:
            prev = out[0]
            for r in res:
                prev = r + prev
                append(prev)
        elif order == 2:
            a, b = out[1], out[0]
            for r in res:
                a, b = r + 2 * a - b, a
                append(a)
        elif order == 3:
            a, b, c = out[2], out[1], out[0]
            for r in res:
                a, b, c = r + 3 * a - 3 * b + c, a, b
                append(a)
        else:
            a, b, c, d = out[3], out[2], out[1], out[0]
            for r in res:
                a, b, c, d = r + 4 * a - 6 * b + 4 * c - d, a, b, c
                append(a)
        samples = out
    elif stype >= 32:                                # LPC
        order = (stype & 31) + 1
        warm = []
        for _ in range(order):
            warm.append(_signed(int(bits[pos:pos + bps], 2), bps))
            pos += bps
        prec = int(bits[pos:pos + 4], 2) + 1
        if prec == 16:
            raise FlacError("invalid LPC precision")
        shift = _signed(int(bits[pos + 4:pos + 9], 2), 5)
        pos += 9
        coefs = []
        for _ in range(order):
            coefs.append(_signed(int(bits[pos:pos + prec], 2), prec))
            pos += prec
        if shift < 0:
            raise FlacError("negative LPC shift")
        res, pos = _residual(bits, pos, blocksize, order)
        samples = _lpc_fn(order)(warm, res, coefs, shift)   # coefs[0] applies to the newest sample
    else:
        raise FlacError("reserved subframe type")
    if len(samples) != blocksize:
        raise FlacError("subframe length mismatch")
    if wasted:
        samples = [s << wasted for s in samples]
    return samples, pos


def _frames(bits: str, nbits: int, samples_per_channel: int, channels: int):
    """The frames of ``decode_frames``: ``(left, right, bit position after the last frame, samples)``. A field
    cut off by the end of ``bits`` raises ``ValueError`` / ``IndexError`` or leaves the position past ``nbits``."""
    left_all: list = []
    right_all: list = []
    pos = 0
    total = 0
    while total < samples_per_channel:
        if pos + 32 > nbits:
            raise FlacError("truncated FLAC data")
        if bits[pos:pos + 14] != "11111111111110":
            raise FlacError("lost FLAC frame sync")
        bs_code = int(bits[pos + 16:pos + 20], 2)
        sr_code = int(bits[pos + 20:pos + 24], 2)
        ch_code = int(bits[pos + 24:pos + 28], 2)
        ss_code = int(bits[pos + 28:pos + 31], 2)
        pos += 32
        # frame / sample number: UTF-8 style coded
        first = int(bits[pos:pos + 8], 2)
        pos += 8
        if first >= 0xC0:
            extra = 1 if first < 0xE0 else 2 if first < 0xF0 else 3 if first < 0xF8 else 4 if first < 0xFC else 5 if first < 0xFE else 6
            pos += 8 * extra
        if bs_code == 6:
            blocksize = int(bits[pos:pos + 8], 2) + 1
            pos += 8
        elif bs_code == 7:
            blocksize = int(bits[pos:pos + 16], 2) + 1
            pos += 16
        elif bs_code in _BLOCK_SIZES:
            blocksize = _BLOCK_SIZES[bs_code]
        else:
            raise FlacError("reserved block size")
        if sr_code == 12:
            pos += 8
        elif sr_code in (13, 14):
            pos += 16
        pos += 8                                      # header CRC-8
        bps = _SAMPLE_BITS.get(ss_code, 0) or 16
        if bps != 16:
            raise FlacError("only 16-bit FLAC is supported")
        if ch_code <= 7:
            nch = ch_code + 1
        elif ch_code <= 10:
            nch = 2
        else:
            raise FlacError("reserved channel assignment")
        if nch != channels:
            raise FlacError("unexpected channel count")
        chans = []
        for c in range(nch):
            sbps = bps + 1 if ((ch_code == 8 and c == 1) or (ch_code == 9 and c == 0) or (ch_code == 10 and c == 1)) else bps
            s, pos = _subframe(bits, pos, blocksize, sbps)
            chans.append(s)
        if ch_code == 8:                              # left / side
            left = chans[0]
            right = list(map(sub, left, chans[1]))
        elif ch_code == 9:                            # side / right
            right = chans[1]
            left = list(map(add, chans[0], right))
        elif ch_code == 10:                           # mid / side
            side = chans[1]                           # ((2m | s&1) + s) >> 1  ==  m + ((s + (s & 1)) >> 1)
            left = [m + ((s + (s & 1)) >> 1) for m, s in zip(chans[0], side)]
            right = list(map(sub, left, side))
        elif nch == 1:
            left, right = chans[0], ()
        else:
            left, right = chans
        pos = (pos + 7) & ~7                          # byte align
        pos += 16                                     # frame CRC-16
        left_all += left
        right_all += right
        total += blocksize
    return left_all, right_all, pos, total


def decode_frames(data, start: int, samples_per_channel: int, channels: int = 2):
    """Decode FLAC frames from ``data[start:]`` until ``samples_per_channel`` samples.

    Returns ``(pcm, end)``: ``pcm`` is an ``array('h')`` of interleaved signed
    16-bit samples (native byte order) and ``end`` the offset just after the last
    consumed frame.  Only 16-bit streams are supported (CD audio).
    """
    data = bytes(data)
    n = len(data)
    nbits = (n - start) * 8
    bits = bin(int.from_bytes(data[start:], "big") | (1 << nbits))[3:]
    try:
        left_all, right_all, pos, total = _frames(bits, nbits, samples_per_channel, channels)
    except (ValueError, IndexError) as exc:           # a field cut off by the end of the data
        raise FlacError("truncated FLAC data") from exc
    if pos > nbits:                                   # the last frame (its CRC-16 at least) runs past the data
        raise FlacError("truncated FLAC data")
    if total != samples_per_channel:
        raise FlacError("FLAC frames overshoot the hunk")
    try:
        if channels == 1:
            return array("h", left_all), start + (pos + 7) // 8
        pcm = array("h", bytes(4 * total))
        pcm[0::2] = array("h", left_all)
        pcm[1::2] = array("h", right_all)
    except OverflowError as exc:                      # pragma: no cover - corrupt data
        raise FlacError("sample out of 16-bit range") from exc
    return pcm, start + (pos + 7) // 8
