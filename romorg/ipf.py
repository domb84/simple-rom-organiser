"""IPF (the Software Preservation Society's disk format) read into each track's MFM bit stream, for RawDIC to image.

An IPF file is records: CAPS, INFO, one IMGE per track (where it starts, how many bits, how many blocks) and one DATA per track
(32-byte block descriptors, then the data and gap streams). A block is its data stream (sync marks and raw MFM as they are on the
disk, data and gap bytes to be MFM-encoded), then its gap (described by forward / backward gap streams, or a default value). The
blocks one after another make the track; it is turned so the first block begins ``startBitPos`` bits after the index.

Following "IPF documentation" v1.6 by Jean Louis-Guerin and the CAPS / SPS encoders it describes (no SPS library code used).
"""

from __future__ import annotations

import random
import struct
import zlib
from typing import Optional

__all__ = ["IpfDisk", "IpfError", "is_ipf"]


class IpfError(Exception):
    """Not an IPF file, or a damaged one."""


_SPREAD = [0] * 256                                          # a byte's bits spread to the data cells of 16 MFM bits
for _b in range(256):
    _v = 0
    for _k in range(8):
        if _b & (0x80 >> _k):
            _v |= 1 << (14 - 2 * _k)
    _SPREAD[_b] = _v


def is_ipf(data: bytes) -> bool:
    return data[:4] == b"CAPS"


class _Bits:
    """A growing bit stream (most significant bit first) that knows its last bit, for the next MFM clock."""

    def __init__(self) -> None:
        self.v = 0
        self.n = 0

    def raw(self, value: int, nbits: int) -> None:
        if nbits <= 0:
            return
        self.v = (self.v << nbits) | (value & ((1 << nbits) - 1))
        self.n += nbits

    def mfm(self, data: bytes, nbits: int) -> None:
        """``nbits`` data bits (from the start of ``data``) as MFM: 2 * nbits cells, the clocks worked out."""
        if nbits <= 0:
            return
        nbytes = (nbits + 7) // 8
        spread = 0
        for b in data[:nbytes]:
            spread = (spread << 16) | _SPREAD[b]
        spread = (spread >> (16 * nbytes - 2 * nbits)) if nbytes * 8 > nbits else spread
        width = 2 * nbits
        prev = self.v & 1 if self.n else 0
        full = (1 << width) - 1
        clockmask = int.from_bytes(b"\xaa" * ((width + 7) // 8), "big") >> ((-width) % 8)
        above = (spread >> 1) | (prev << (width - 1))
        clocks = ~(above | (spread << 1)) & clockmask & full
        self.raw(spread | clocks, width)

    def bytes(self) -> bytes:
        pad = (-self.n) % 8
        return (self.v << pad).to_bytes((self.n + pad) // 8, "big")


class _Track:
    __slots__ = ("cyl", "side", "density", "start", "data_bits", "gap_bits", "track_bits", "blocks", "flags", "key")


class IpfDisk:
    """An IPF file's tracks as MFM (``track(cylinder * 2 + side)``), for :class:`romorg.rawdic.Imager`."""

    def __init__(self, data: bytes, check: bool = True) -> None:
        if not is_ipf(data):
            raise IpfError("not an IPF file")
        self.data = data
        self.encoder = 0
        self.info: dict = {}
        self.tracks: dict[int, _Track] = {}
        datas: dict[int, tuple[int, int]] = {}               # dataKey -> (offset of the extra block, its length)
        o = 0
        while o + 12 <= len(data):
            kind, length, crc = data[o:o + 4], *struct.unpack_from(">II", data, o + 4)
            if length < 12 or o + length > len(data):
                raise IpfError(f"a damaged record at {o}")
            if check:
                rec = bytearray(data[o:o + length])
                rec[8:12] = bytes(4)
                if zlib.crc32(rec) & 0xFFFFFFFF != crc:
                    raise IpfError(f"the {kind.decode('latin-1')} record at {o} fails its checksum")
            body = data[o + 12:o + length]
            nxt = o + length
            if kind == b"INFO":
                f = struct.unpack_from(">21I", body)
                self.encoder = f[1]
                self.info = {"media": f[0], "encoder": f[1], "encoder_rev": f[2], "file_key": f[3], "file_rev": f[4],
                             "origin": f[5], "min_track": f[6], "max_track": f[7], "min_side": f[8], "max_side": f[9],
                             "date": f[10], "time": f[11], "platforms": [p for p in f[12:16] if p], "disk": f[16],
                             "creator": f[17]}
            elif kind == b"IMGE":
                f = struct.unpack_from(">14I", body)
                t = _Track()
                t.cyl, t.side, t.density = f[0], f[1], f[2]
                t.start, t.data_bits, t.gap_bits, t.track_bits = f[6], f[7], f[8], f[9]
                t.blocks, t.flags, t.key = f[10], f[12], f[13]
                self.tracks[t.cyl * 2 + t.side] = t
            elif kind == b"DATA":
                elen, _bits, ecrc, key = struct.unpack_from(">IIII", body)
                if nxt + elen > len(data):
                    raise IpfError("a DATA record runs past the end of the file")
                if check and elen and zlib.crc32(data[nxt:nxt + elen]) & 0xFFFFFFFF != ecrc:
                    raise IpfError(f"the data of track key {key} fails its checksum")
                datas[key] = (nxt, elen)
                nxt += elen
            o = nxt
        if not self.tracks:
            raise IpfError("an IPF file with no tracks")
        self._datas = datas
        self._cache: dict[int, Optional[tuple[bytes, int]]] = {}

    @property
    def disk_number(self) -> int:
        return self.info.get("disk", 0)

    def dos(self, n: int) -> Optional[bytes]:
        return None                                          # (an IPF has no sectors to hand over: RawDIC reads its MFM)

    def track(self, n: int) -> Optional[tuple[bytes, int]]:
        if n not in self._cache:
            self._cache[n] = self._build(n)
        return self._cache[n]

    # --- one track
    def _build(self, n: int) -> Optional[tuple[bytes, int]]:
        t = self.tracks.get(n)
        if t is None:
            return None
        rnd = random.Random(n)
        if t.blocks == 0 or t.key not in self._datas:
            if t.density == 1 or t.track_bits:               # unformatted: noise
                nb = t.track_bits or 100000
                return rnd.getrandbits(nb).to_bytes((nb + 7) // 8, "big"), nb
            return None
        base, elen = self._datas[t.key]
        ext = self.data[base:base + elen]
        out = _Bits()
        descs = [struct.unpack_from(">8I", ext, 32 * i) for i in range(t.blocks)]
        for i, d in enumerate(descs):
            data_bits, gap_bits = d[0], d[1]
            enc_type, flags, gap_default, data_off = d[4], d[5], d[6], d[7]
            in_bits = self.encoder == 2 and flags & 4
            before = out.n
            if data_bits:
                self._data_stream(ext, data_off, bool(in_bits), out, rnd)
            got = out.n - before
            if got != data_bits:                             # (keep the track's geometry even if a stream is short or long)
                if got > data_bits:
                    out.v >>= got - data_bits
                    out.n -= got - data_bits
                else:
                    out.raw(0, data_bits - got)
            if gap_bits:
                self._gap(ext, d, out, gap_bits)
        nbits = out.n
        v = out.v
        start = t.start % nbits if nbits else 0
        if start:                                            # the first block begins ``start`` bits after the index
            v = ((v >> start) | (v << (nbits - start))) & ((1 << nbits) - 1)
        pad = (-nbits) % 8
        return (v << pad).to_bytes((nbits + pad) // 8, "big"), nbits

    def _data_stream(self, ext: bytes, o: int, in_bits: bool, out: _Bits, rnd: random.Random) -> None:
        for _ in range(100000):
            if o >= len(ext):
                raise IpfError("a data stream runs past its record")
            head = ext[o]
            o += 1
            if head == 0:
                return
            width, kind = head >> 5, head & 31
            size = int.from_bytes(ext[o:o + width], "big")
            o += width
            nbits = size if in_bits else 8 * size
            if kind == 5:                                    # fuzzy: no sample, random bits (weak cells)
                out.mfm(rnd.getrandbits(8 * ((nbits + 7) // 8)).to_bytes((nbits + 7) // 8, "big"), nbits)
                continue
            nbytes = (nbits + 7) // 8
            sample = ext[o:o + nbytes]
            o += nbytes
            if kind in (1, 4):                               # sync / raw: MFM cells as they are on the disk
                out.raw(int.from_bytes(sample, "big") >> (8 * nbytes - nbits), nbits)
            elif kind in (2, 3):                             # data / gap bytes: MFM-encoded
                out.mfm(sample, nbits)
            else:
                raise IpfError(f"an unknown data stream element type {kind}")

    def _gap(self, ext: bytes, d: tuple, out: _Bits, gap_bits: int) -> None:
        """The gap after a block: ``gap_bits`` MFM cells (gap_bits / 2 data bits) from its gap streams or its default value."""
        cells = gap_bits // 2
        flags, gap_default = d[5], d[6]
        fwd: list[tuple[Optional[int], int, int]] = []       # (data bits or None: fill, sample, sample bits)
        bwd: list[tuple[Optional[int], int, int]] = []
        if self.encoder == 2 and flags & 3:
            o = d[2]
            for which in (fwd, bwd):
                if which is fwd and not flags & 1 or which is bwd and not flags & 2:
                    continue
                o = self._gap_stream(ext, o, which)
        if not fwd and not bwd:
            fwd = [(None, gap_default & 0xFF, 8)]
        f_bits = self._fill(fwd, cells, False)
        b_bits = self._fill(bwd, cells, True) if bwd else None
        if b_bits is None:
            bits, n = f_bits
        elif not fwd:
            bits, n = b_bits
        else:                                                # both: forward from the block, backward from the next, meeting in the gap
            fv, fn = f_bits
            bv, bn = b_bits
            fl = self._defined(fwd)
            bl = self._defined(bwd)
            if fl is not None and bl is not None and fl + bl <= cells:
                mid_f = cells - bl
            elif fl is not None:
                mid_f = min(fl, cells)
            elif bl is not None:
                mid_f = max(cells - bl, 0)
            else:
                mid_f = cells // 2
            head = fv >> (fn - mid_f) if mid_f else 0
            tail = bv & ((1 << (cells - mid_f)) - 1) if cells - mid_f else 0
            bits, n = (head << (cells - mid_f)) | tail, cells
        nbytes = (n + 7) // 8
        out.mfm((bits << (8 * nbytes - n)).to_bytes(nbytes, "big") if n else b"", n)
        if gap_bits & 1:
            out.raw(0, 1)

    @staticmethod
    def _gap_stream(ext: bytes, o: int, into: list) -> int:
        length: Optional[int] = None
        for _ in range(100000):
            head = ext[o]
            o += 1
            if head == 0:
                return o
            width, kind = head >> 5, head & 31
            size = int.from_bytes(ext[o:o + width], "big")
            o += width
            if kind == 1:                                    # gap length (data bits) for the sample that follows
                length = size
            elif kind == 2:                                  # sample of ``size`` bits
                nbytes = (size + 7) // 8
                sample = int.from_bytes(ext[o:o + nbytes], "big") >> (8 * nbytes - size) if size else 0
                o += nbytes
                into.append((length, sample, size))
                length = None
            else:
                raise IpfError(f"an unknown gap stream element type {kind}")
        return o

    @staticmethod
    def _defined(elems: list) -> Optional[int]:
        if any(n is None for n, _s, _b in elems):
            return None
        return sum(n for n, _s, _b in elems)

    @staticmethod
    def _fill(elems: list, cells: int, backward: bool) -> tuple[int, int]:
        """The gap's ``cells`` data bits from a gap stream (a sample without a length repeats as far as needed)."""
        v, n = 0, 0
        seq = list(reversed(elems)) if backward else elems
        pieces: list[tuple[int, int]] = []
        total = 0
        for length, sample, sbits in seq:
            if sbits == 0:
                continue
            want = cells - total if length is None else min(length, cells - total)
            if want <= 0:
                break
            reps = -(-want // sbits)
            rep = 0
            for _ in range(reps):
                rep = (rep << sbits) | sample
            rbits = reps * sbits
            if backward:                                     # (a backward sample lines up on the far end)
                rep &= (1 << want) - 1
            else:
                rep >>= rbits - want
            pieces.append((rep, want))
            total += want
            if length is None:
                break
        last = next(((s, b) for _l, s, b in reversed(seq) if b), None) if total < cells else None
        if last is not None:                                 # (lengths that fall short: the last sample loops on, as the
            sample, sbits = last                             # documentation's example has it)
            want = cells - total
            reps = -(-want // sbits)
            rep = 0
            for _ in range(reps):
                rep = (rep << sbits) | sample
            rep = rep & ((1 << want) - 1) if backward else rep >> (reps * sbits - want)
            pieces.append((rep, want))
            total = cells
        if backward:
            pieces.reverse()
        for p, pb in pieces:
            v = (v << pb) | p
            n += pb
        if n < cells:                                        # (the rest: zero bits, MFM clocks only)
            if backward:
                n = cells
            else:
                v <<= cells - n
                n = cells
        return v, n
