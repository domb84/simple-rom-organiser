"""CD-ROM sector ECC (P/Q parity) regeneration, vectorised with big integers.

CHD ``cdlz`` / ``cdzl`` hunks drop the 12-byte sync header and the 276 bytes of
P/Q parity of every sector whose parity is valid (an "ECC bitmap" says which);
the reader has to regenerate them to reproduce the raw 2352-byte sector.  The
algorithm is the one of ECMA-130 annex A (as in ecm / libchdr / MAME):

* P: 86 columns x 24 rows over bytes ``12..2075``  -> parity at ``2076..2247``
* Q: 52 diagonals x 43 over bytes ``12..2247``      -> parity at ``2248..2351``

All GF(2^8) work is done on whole batches of sectors at once: a row of a batch
is one Python ``int`` and ``xtime`` is a SWAR shift/mask, so the cost is a few
hundred big-int operations per hunk instead of ~4000 byte operations per sector.
"""

from __future__ import annotations

from operator import itemgetter
from typing import List, Sequence

SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
SECTOR = 2352
_P_OFF = 2076
_Q_OFF = 2248

# GF(2^8) helpers (polynomial 0x11D) --------------------------------------------------
_F = bytes(((i << 1) ^ (0x11D if i & 0x80 else 0)) & 0xFF for i in range(256))
_BINV = bytearray(256)
for _i in range(256):
    _BINV[_i ^ _F[_i]] = _i
_BINV = bytes(_BINV)

_MASKS: dict = {}


def _masks(nbytes: int):
    m = _MASKS.get(nbytes)
    if m is None:
        m = (int.from_bytes(b"\x7f" * nbytes, "big"), int.from_bytes(b"\x01" * nbytes, "big"))
        _MASKS[nbytes] = m
    return m


def _xtime(x: int, m7f: int, m01: int) -> int:
    return ((x & m7f) << 1) ^ (((x >> 7) & m01) * 0x1D)


def _horner(rows: Sequence[bytes], lanes: int):
    """rows: byte strings of ``lanes`` bytes; returns the (first, second) parity vectors."""
    m7f, m01 = _masks(lanes)
    h = 0
    x = 0
    for row in rows:
        t = int.from_bytes(row, "big")
        x ^= t
        h = _xtime(h ^ t, m7f, m01)
    r = _xtime(h, m7f, m01) ^ x
    r = int.from_bytes(r.to_bytes(lanes, "big").translate(_BINV), "big")
    return r.to_bytes(lanes, "big"), (r ^ x).to_bytes(lanes, "big")


# Q block geometry: 2236 bytes = 26 x 86; lane (k, e) at step m reads
#   byte  86 * ((k + m) % 26) + e + 2 * m   i.e. column (e + 2m) of the 26 x 86 matrix,
# so a *strided slice* of the concatenated batch gives a whole row of lanes and the
# per-step "rotate by one lane" is two masked shifts.
_ROT: dict = {}


def _rot_masks(blocks: int):
    r = _ROT.get(blocks)
    if r is None:
        first = int.from_bytes((b"\xff" + b"\x00" * 25) * blocks, "big")
        last = int.from_bytes((b"\x00" * 25 + b"\xff") * blocks, "big")
        r = (~first & ((1 << (blocks * 26 * 8)) - 1), last)
        _ROT[blocks] = r
    return r


_PGET: dict = {}


def _p_getters(n: int):
    g = _PGET.get(n)
    if g is None:
        g = []
        for r in range(24):
            sl = [slice(i * 2064 + 86 * r, i * 2064 + 86 * r + 86) for i in range(n)]
            g.append(itemgetter(*sl) if n > 1 else (lambda b, a=sl[0]: (b[a],)))
        _PGET[n] = g
    return g


_ZERO4 = b"\x00\x00\x00\x00"


def _body(s, end: int) -> bytes:
    """The bytes the parity covers: ``12..end``; in a MODE 2 sector (mode byte 2: PlayStation, CD-i,
    Video CD) the 4 header bytes count as zeros (ECMA-130 / MAME ``ecc_source_byte``)."""
    if s[15] == 2:
        return _ZERO4 + bytes(s[16:end])
    return bytes(s[12:end])


def _p_parity(sectors: Sequence):
    """``(first, second)``: the two P parity rows (86 bytes per sector each) of the sectors' data."""
    n = len(sectors)
    blkp = b"".join([_body(s, 2076) for s in sectors])
    rows = [b"".join(g(blkp)) for g in _p_getters(n)]
    return _horner(rows, 86 * n)


def _q_parity(sectors: Sequence) -> List[bytes]:
    """The 104 Q parity bytes of every sector (they cover header, data and the P parity that is in the sector)."""
    n = len(sectors)
    blk = b"".join([_body(s, 2248) for s in sectors])
    lanes = 52 * n
    m7f, m01 = _masks(lanes)
    keep, lastmask = _rot_masks(2 * n)
    fb = int.from_bytes
    h = 0
    x = 0
    for m in range(43):
        t = fb(blk[2 * m::86] + blk[2 * m + 1::86], "big")
        x ^= t
        h = ((h ^ t) & m7f) << 1 ^ ((((h ^ t) >> 7) & m01) * 0x1D)
        h = ((h >> 8) & keep) | ((h & lastmask) << 200)
        x = ((x >> 8) & keep) | ((x & lastmask) << 200)
    # h was rotated after the last step: undo nothing, lanes are read at (k + 43) % 26
    r = ((h & m7f) << 1 ^ (((h >> 7) & m01) * 0x1D)) ^ x
    r = fb(r.to_bytes(lanes, "big").translate(_BINV), "big")
    ra = r.to_bytes(lanes, "big")
    rb = (r ^ x).to_bytes(lanes, "big")
    out = []
    for i in range(n):
        o0 = i * 26
        o1 = (n + i) * 26
        q = bytearray(104)
        for base, vec in ((0, ra), (52, rb)):
            e0 = vec[o0:o0 + 26]
            e1 = vec[o1:o1 + 26]
            q[base:base + 52:2] = e0[17:] + e0[:17]
            q[base + 1:base + 52:2] = e1[17:] + e1[:17]
        out.append(bytes(q))
    return out


def generate(sectors: Sequence) -> None:
    """Fill sync header + P/Q parity (in place) of every 2352-byte sector in the list.

    The items may be ``bytearray`` or writable ``memoryview`` objects of length 2352.
    """
    if not sectors:
        return
    first, second = _p_parity(sectors)
    for i, s in enumerate(sectors):
        s[2076:2248] = first[i * 86:(i + 1) * 86] + second[i * 86:(i + 1) * 86]
    for s, q in zip(sectors, _q_parity(sectors)):
        s[2248:2352] = q
        s[0:12] = SYNC


def valid(sectors: Sequence) -> List[bool]:
    """Per sector: does it carry exactly the sync header and P/Q parity that :func:`generate` would write? (What a
    CHD writer needs to know before it drops them.) Nothing is modified. P is checked first and Q only for the
    sectors that pass, so sectors without error correction (audio, MODE 2 form 2) cost little."""
    n = len(sectors)
    ok = [bytes(s[0:12]) == SYNC for s in sectors]
    idx = [i for i in range(n) if ok[i]]
    if idx:
        cand = [sectors[i] for i in idx]
        first, second = _p_parity(cand)
        keep = []
        for k, i in enumerate(idx):
            s = cand[k]
            if bytes(s[2076:2162]) == first[k * 86:(k + 1) * 86] and bytes(s[2162:2248]) == second[k * 86:(k + 1) * 86]:
                keep.append(i)
            else:
                ok[i] = False
        if keep:
            cand = [sectors[i] for i in keep]
            for i, s, q in zip(keep, cand, _q_parity(cand)):
                if bytes(s[2248:2352]) != q:
                    ok[i] = False
    return ok


def generate_reference(sector: bytearray) -> None:
    """Straightforward per-sector implementation (used by the tests as an oracle)."""
    def block(src_off, major_count, minor_count, major_mult, minor_inc, dest_off):
        size = major_count * minor_count
        for major in range(major_count):
            index = (major >> 1) * major_mult + (major & 1)
            a = 0
            b = 0
            for _ in range(minor_count):
                t = sector[src_off + index]
                index += minor_inc
                if index >= size:
                    index -= size
                b ^= t
                a ^= t
                a = _F[a]
            a = _BINV[_F[a] ^ b]
            sector[dest_off + major] = a
            sector[dest_off + major + major_count] = a ^ b
    # careful: Horner variable is ``a`` here, plain xor is ``b`` (see _horner)
    header = bytes(sector[12:16])
    if sector[15] == 2:          # MODE 2: the header counts as zeros
        sector[12:16] = _ZERO4
    block(12, 86, 24, 2, 86, _P_OFF)
    block(12, 52, 43, 86, 88, _Q_OFF)
    sector[12:16] = header
    sector[0:12] = SYNC
