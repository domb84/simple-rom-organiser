"""A small RVZ writer for the tests (GameCube images only), and an independent reference of the padding generator.

``reference_junk`` is written straight from the text of Dolphin's ``docs/WiaAndRvz.md`` (plain loops over a list of
words), so it can check the fast implementation of :mod:`romorg.rvz` without sharing any code with it.
"""

from __future__ import annotations

import hashlib
import random
import struct
import zlib
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from romorg import zstdnative

NONE, ZSTD = 0, 5
JUNK_BLOCK = 0x8000
MASK = 0xFFFFFFFF

Region = Tuple  # ("data", bytes) | ("zero", n) | ("junk", seed, n)


def reference_junk(seed: bytes, offset: int, size: int) -> bytes:
    """The Lagged Fibonacci generator exactly as the spec describes it."""
    buf = list(struct.unpack(">17I", seed)) + [0] * (521 - 17)
    for i in range(17, 521):
        buf[i] = ((buf[i - 17] << 23) & MASK) ^ (buf[i - 16] >> 9) ^ buf[i - 1]

    def forward() -> None:
        for i in range(32):
            buf[i] ^= buf[i + 521 - 32]
        for i in range(32, 521):
            buf[i] ^= buf[i - 32]
    for _ in range(4):
        forward()
    out = bytearray()
    pos = 0                      # index of the next word
    skip = offset % JUNK_BLOCK
    produced = 0
    need = skip + size
    while produced < need:
        if pos == 521:
            forward()
            pos = 0
        w = buf[pos]
        pos += 1
        out += bytes(((w >> 24) & 0xFF, (w >> 18) & 0xFF, (w >> 8) & 0xFF, w & 0xFF))
        produced += 4
    return bytes(out[skip:skip + size])


def seed_for(n: int) -> bytes:
    return random.Random(n).randbytes(68)


def make_disc(regions: Sequence[Region], header: Optional[bytes] = None) -> Tuple[bytes, List[Tuple[int, int, bytes]]]:
    """``(the ISO, [(offset, size, seed) of every padding run])``. The first 0x80 bytes are the disc header."""
    iso = bytearray()
    runs = []
    for r in regions:
        if r[0] == "data":
            iso += r[1]
        elif r[0] == "zero":
            iso += bytes(r[1])
        else:
            start, size = len(iso), r[2]
            if start // JUNK_BLOCK != (start + size - 1) // JUNK_BLOCK:
                raise ValueError("a padding run must not cross a 32 KiB boundary")
            runs.append((start, size, r[1]))
            iso += reference_junk(r[1], start, size)
    if len(iso) < 0x80:
        raise ValueError("a disc is at least 0x80 bytes")
    head = header if header is not None else b"GTST01\x00\x00" + b"\x00" * 0x18 + b"Test Disc".ljust(0x60, b"\x00")
    iso[0:0x80] = head[:0x80].ljust(0x80, b"\x00")
    return bytes(iso), runs


def _pack(chunk_off: int, chunk: bytes, runs: List[Tuple[int, int, bytes]]) -> Optional[bytes]:
    """RVZ packing of one chunk, or None when no padding run lies inside it. Runs never cross a chunk (they never
    cross a 32 KiB block: the generator is restarted for every block)."""
    parts = []
    pos = chunk_off
    end = chunk_off + len(chunk)
    for off, size, seed in runs:
        if off + size <= chunk_off or off >= end:
            continue
        assert chunk_off <= off and off + size <= end, "a padding run must lie inside one chunk"
        if off > pos:
            parts.append(struct.pack(">I", off - pos) + chunk[pos - chunk_off:off - chunk_off])
        parts.append(struct.pack(">I", size | 0x80000000) + seed)
        pos = off + size
    if not parts:
        return None
    if pos < end:
        parts.append(struct.pack(">I", end - pos) + chunk[pos - chunk_off:])
    return b"".join(parts)


def compress(method: int, data: bytes) -> bytes:
    if method == NONE:
        return data
    if method == ZSTD:
        return zstdnative.compress(data, 3)
    raise ValueError(method)


def build_rvz(path: Union[str, Path], iso: bytes, runs: Sequence[Tuple[int, int, bytes]] = (), chunk: int = 0x20000,
              compression: int = ZSTD, store_compressed: bool = True, disc_type: int = 1, magic: bytes = b"RVZ\x01"
              ) -> dict:
    """Write ``iso`` as an RVZ. Chunks that are all zeros take no space, chunks that overlap a padding run in
    ``runs`` (``(offset, size, seed)``) are packed, the others are stored whole. ``store_compressed`` False stores
    every group without compression (the group flag), whatever ``compression`` says for the tables."""
    runs = sorted(runs)
    n_groups = -(-len(iso) // chunk)
    body = bytearray()
    base = 0x48 + 0xDC
    groups = bytearray()
    stats = {"zero": 0, "packed": 0, "plain": 0}
    for g in range(n_groups):
        off = g * chunk
        piece = iso[off:off + chunk]
        if off == 0:
            piece = bytes(0x80) + piece[0x80:]        # the header bytes come from the disc struct
        if not any(piece):
            groups += struct.pack(">III", 0, 0, 0)
            stats["zero"] += 1
            continue
        packed = _pack(off, iso[off:off + chunk], runs) if off else None
        if off == 0:
            packed = None
        data = packed if packed is not None else piece
        compressed = store_compressed and compression != NONE
        blob = compress(compression, data) if compressed else data
        while (base + len(body)) % 4:
            body += b"\0"
        data_off4 = (base + len(body)) // 4
        body += blob
        size = len(blob) | (0x80000000 if compressed else 0)
        groups += struct.pack(">III", data_off4, size, len(data) if packed is not None else 0)
        stats["packed" if packed is not None else "plain"] += 1
    while (base + len(body)) % 4:
        body += b"\0"
    raw_table = struct.pack(">QQII", 0x80, len(iso) - 0x80, 0, n_groups)
    raw_blob = compress(compression, raw_table)
    group_blob = compress(compression, bytes(groups))
    raw_off = base + len(body)
    body += raw_blob
    group_off = base + len(body)
    body += group_blob
    disc = struct.pack(">IIiI", disc_type, compression, 3, chunk) + iso[:0x80]
    disc += struct.pack(">IIQ", 0, 0x30, 0) + bytes(20)
    disc += struct.pack(">IQI", 1, raw_off, len(raw_blob))
    disc += struct.pack(">IQI", n_groups, group_off, len(group_blob))
    disc += bytes([0]) + bytes(7)
    assert len(disc) == 0xDC, len(disc)
    head = magic + struct.pack(">III", 0x01000000, 0x00030000, len(disc)) + hashlib.sha1(disc).digest()
    head += struct.pack(">QQ", len(iso), base + len(body))
    head += hashlib.sha1(head).digest()
    assert len(head) == 0x48, len(head)
    Path(path).write_bytes(head + disc + bytes(body))
    stats.update(crc32="%08x" % (zlib.crc32(iso) & MASK), sha1=hashlib.sha1(iso).hexdigest(), size=len(iso))
    return stats
