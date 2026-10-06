"""Test-only helpers: a minimal CHD v5 *writer*, synthetic Dreamcast / PlayStation / PlayStation 2 discs, a tiny
FLAC encoder, a Redump-style DAT generator and a fake ``chdman`` shell script.

The writer exists only here (the app only ever reads CHDs; creating them is chdman's job).  It can write
an uncompressed map or a compressed (Huffman coded, with RLE and self references) map, and ``cdlz`` /
``cdzl`` / ``cdfl`` / ``zlib`` / ``lzma`` / uncompressed hunks, cooked (``MODE1`` 2048-byte, as ``chdman createcd``
keeps a PlayStation 2 ISO) and ``MODE2_RAW`` tracks, and DVD CHDs (``DVD `` metadata, 2048-byte units).
"""

from __future__ import annotations

import hashlib
import lzma
import math
import os
import random
import stat
import struct
import zlib
from pathlib import Path
from typing import Optional, Sequence
from xml.sax.saxutils import escape as _escape


def escape(text: str) -> str:
    return _escape(text, {'"': "&quot;"})


from romorg import cdecc
from romorg import chd as chdlib

FRAME = 2448
SECTOR = 2352


# --------------------------------------------------------------------------- sectors

def mode1_sector(lba: int, data: bytes) -> bytes:
    """A valid MODE1 raw sector: sync, header, 2048 data bytes, (zero) EDC, P/Q parity."""
    assert len(data) == 2048
    m, rest = divmod(lba + 150, 75 * 60)
    s, f = divmod(rest, 75)
    bcd = lambda v: (v // 10) << 4 | v % 10  # noqa: E731
    sec = bytearray(2352)
    sec[12:16] = bytes((bcd(m), bcd(s), bcd(f), 1))
    sec[16:2064] = data
    cdecc.generate_reference(sec)
    return bytes(sec)


def _msf(lba: int) -> bytes:
    m, rest = divmod(lba + 150, 75 * 60)
    s, f = divmod(rest, 75)
    bcd = lambda v: (v // 10) << 4 | v % 10  # noqa: E731
    return bytes((bcd(m), bcd(s), bcd(f)))


def mode2_form1_sector(lba: int, data: bytes) -> bytes:
    """A MODE 2 form 1 raw sector (PlayStation): sync, header (mode 2), subheader, 2048 data bytes, EDC, P/Q parity
    (computed with the header counted as zeros, as the real ECC of mode 2 does)."""
    assert len(data) == 2048
    sec = bytearray(2352)
    sec[12:16] = _msf(lba) + b"\x02"
    sec[16:24] = bytes((0, 0, 0x08, 0, 0, 0, 0x08, 0))
    sec[24:2072] = data
    sec[2072:2076] = (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "little")       # placeholder EDC
    cdecc.generate_reference(sec)
    return bytes(sec)


def mode2_form2_sector(lba: int, data: bytes) -> bytes:
    """A MODE 2 form 2 raw sector: 2324 data bytes + 4 EDC bytes, no parity (stored raw by the CHD codecs)."""
    assert len(data) == 2324
    sec = bytearray(2352)
    sec[0:12] = cdecc.SYNC
    sec[12:16] = _msf(lba) + b"\x02"
    sec[16:24] = bytes((0, 0, 0x20, 0, 0, 0, 0x20, 0))
    sec[24:2348] = data
    sec[2348:2352] = (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(sec)


def make_mode2_track(frames: int, seed: int, start_lba: int = 0) -> bytes:
    """A PlayStation-style data track: mostly form 1 sectors with some form 2 (XA audio / video) sectors."""
    rnd = random.Random(seed)
    out = []
    for i in range(frames):
        if i % 5 == 3:
            out.append(mode2_form2_sector(start_lba + i, bytes(rnd.choice(b"WXYZ\x01") for _ in range(2324))))
        else:
            block = bytes(rnd.choice(b"ABCDEFGH \x00") for _ in range(256)) * 8
            out.append(mode2_form1_sector(start_lba + i, block))
    return b"".join(out)


def make_iso(sectors: int, seed: int) -> bytes:
    """A PlayStation 2 style ISO: ``sectors`` x 2048 compressible bytes (a few zero runs and a text-like body)."""
    rnd = random.Random(seed)
    out = []
    for i in range(sectors):
        if i % 7 == 6:
            out.append(bytes(2048))
        else:
            out.append(bytes(rnd.choice(b"ABCDEFGHIJ \x00") for _ in range(128)) * 16)
    return b"".join(out)


def make_data_track(frames: int, seed: int, start_lba: int = 0) -> bytes:
    rnd = random.Random(seed)
    out = []
    for i in range(frames):
        # compressible but not trivial payload
        block = bytes(rnd.choice(b"ABCDEFGH \x00") for _ in range(256)) * 8
        out.append(mode1_sector(start_lba + i, block))
    return b"".join(out)


def make_audio_track(frames: int, seed: int) -> bytes:
    """``frames`` * 2352 bytes of little-endian stereo 16-bit audio (a mix of tones and a little noise)."""
    rnd = random.Random(seed)
    n = frames * 588
    out = bytearray()
    for i in range(n):
        l = int(9000 * math.sin(i / 17.0) + 3000 * math.sin(i / 3.1) + rnd.randint(-40, 40))
        r = int(8000 * math.sin(i / 23.0 + 1) + rnd.randint(-40, 40))
        out += struct.pack("<hh", l, r)
    return bytes(out)


# --------------------------------------------------------------------------- FLAC (tiny encoder)

class _Bits:
    def __init__(self) -> None:
        self.s: list[str] = []

    def put(self, value: int, n: int) -> None:
        if n:
            self.s.append(format(value & ((1 << n) - 1), f"0{n}b"))

    def signed(self, value: int, n: int) -> None:
        self.put(value & ((1 << n) - 1), n)

    def unary(self, q: int) -> None:
        self.s.append("0" * q + "1")

    def align(self) -> None:
        total = sum(len(x) for x in self.s)
        self.put(0, (-total) % 8)

    def tobytes(self) -> bytes:
        bits = "".join(self.s)
        assert len(bits) % 8 == 0
        return int(bits, 2).to_bytes(len(bits) // 8, "big") if bits else b""


def _rice(b: _Bits, residual: Sequence[int], k: int) -> None:
    for r in residual:
        u = (-2 * r - 1) if r < 0 else 2 * r
        b.unary(u >> k)
        b.put(u & ((1 << k) - 1), k)


def _subframe(b: _Bits, samples: Sequence[int], kind: str, bps: int = 16) -> None:
    b.put(0, 1)
    if kind == "verbatim":
        b.put(1, 6)
        b.put(0, 1)
        for v in samples:
            b.signed(v, bps)
    elif kind == "constant":
        b.put(0, 6)
        b.put(0, 1)
        b.signed(samples[0], bps)
    else:   # fixed order 2 (or 1)
        order = 2 if kind == "fixed2" else 1
        b.put(8 + order, 6)
        b.put(0, 1)
        for v in samples[:order]:
            b.signed(v, bps)
        res = []
        for i in range(order, len(samples)):
            if order == 2:
                res.append(samples[i] - 2 * samples[i - 1] + samples[i - 2])
            else:
                res.append(samples[i] - samples[i - 1])
        k = 6
        b.put(0, 2)          # rice, 4 bit parameters
        b.put(0, 4)          # one partition
        b.put(k, 4)
        _rice(b, res, k)


def _flac_crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _flac_crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def flac_frame(left: Sequence[int], right: Sequence[int], number: int, kinds=("fixed2", "fixed2"),
               stereo: str = "indep") -> bytes:
    """One FLAC frame (the codec of a CHD ``cdfl`` hunk is a run of these)."""
    n = len(left)
    b = _Bits()
    b.put(0b11111111111110, 14)
    b.put(0, 1)
    b.put(0, 1)                  # fixed block size
    b.put(7, 4)                  # block size: 16 bit value follows
    b.put(9, 4)                  # 44.1 kHz
    assign = {"indep": 1, "left_side": 8, "side_right": 9, "mid_side": 10}[stereo]
    b.put(assign, 4)
    b.put(4, 3)                  # 16 bit
    b.put(0, 1)
    assert number < 128
    b.put(number, 8)             # UTF-8 coded frame number (< 128: one byte)
    b.put(n - 1, 16)
    b.put(_flac_crc8(b.tobytes()), 8)   # header CRC-8 (libFLAC checks it)
    if stereo == "indep":
        chans = [(left, 16), (right, 16)]
    elif stereo == "left_side":
        chans = [(left, 16), ([l - r for l, r in zip(left, right)], 17)]
    elif stereo == "side_right":
        chans = [([l - r for l, r in zip(left, right)], 17), (right, 16)]
    else:
        mid = [(l + r) >> 1 for l, r in zip(left, right)]
        side = [l - r for l, r in zip(left, right)]
        chans = [(mid, 16), (side, 17)]
    for (samples, bps), kind in zip(chans, kinds):
        _subframe(b, samples, kind, bps)
    b.align()
    b.put(_flac_crc16(b.tobytes()), 16)   # frame CRC-16 (libFLAC checks it)
    return b.tobytes()


def flac_stream(pcm_le: bytes, block: int = 1176, kinds=("fixed2", "fixed2"), stereo: str = "indep") -> bytes:
    """FLAC frames for little-endian stereo 16-bit PCM."""
    vals = struct.unpack("<%dh" % (len(pcm_le) // 2), pcm_le)
    left, right = list(vals[0::2]), list(vals[1::2])
    out = bytearray()
    for i, start in enumerate(range(0, len(left), block)):
        out += flac_frame(left[start:start + block], right[start:start + block], i % 128, kinds, stereo)
    return bytes(out)


# --------------------------------------------------------------------------- CHD writer

def _crc16(data: bytes) -> int:
    return chdlib.crc16(data)


def _huff_bits(values: Sequence[int], widths: Sequence[int]) -> str:
    return "".join(format(v, f"0{w}b") for v, w in zip(values, widths))


def zstd_compress(data: bytes) -> Optional[bytes]:
    """One Zstandard frame of ``data`` (what chdman's ``zstd`` / ``cdzs`` codecs store), or None when this machine has
    no Zstandard library - the tests that need it skip."""
    from romorg import zstdnative
    lib = zstdnative.status()["library"]
    if not lib:
        return None
    if lib == "compression.zstd":
        from compression import zstd
        return zstd.compress(data)
    import ctypes
    z = ctypes.CDLL(lib)
    z.ZSTD_compressBound.restype = ctypes.c_size_t
    z.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    z.ZSTD_compress.restype = ctypes.c_size_t
    z.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    cap = z.ZSTD_compressBound(len(data))
    buf = ctypes.create_string_buffer(cap)
    n = z.ZSTD_compress(buf, cap, data, len(data), 3)
    return buf.raw[:n]


def _compress_hunk(codec: str, raw_sectors: bytes, subcode: bytes, frames: int, audio_flac: Optional[bytes],
                   hunk_bytes: int) -> bytes:
    """Compress one CD hunk (``raw_sectors`` = frames*2352 bytes in hunk byte order)."""
    if codec in ("cdlz", "cdzl", "cdzs"):
        ecc = bytearray((frames + 7) // 8)
        base = bytearray(raw_sectors)
        for fr in range(frames):
            sec = base[fr * SECTOR:(fr + 1) * SECTOR]
            if sec[:12] == cdecc.SYNC:
                check = bytearray(sec)
                cdecc.generate_reference(check)
                if check == sec:                      # valid parity: drop sync + parity, flag it
                    ecc[fr >> 3] |= 1 << (fr & 7)
                    base[fr * SECTOR:fr * SECTOR + 12] = bytes(12)
                    base[fr * SECTOR + 2076:(fr + 1) * SECTOR] = bytes(276)
        if codec == "cdlz":
            comp = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=chdlib._lzma_filters(frames * SECTOR))
            body = comp.compress(bytes(base)) + comp.flush()
        elif codec == "cdzs":                         # sectors AND subcode are Zstandard frames
            clb = 2 if hunk_bytes < 65536 else 3
            return bytes(ecc) + len(zstd_compress(bytes(base))).to_bytes(clb, "big") + zstd_compress(bytes(base)) \
                + zstd_compress(subcode)
        else:
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            body = c.compress(bytes(base)) + c.flush()
        clb = 2 if hunk_bytes < 65536 else 3
        sub = zlib.compressobj(9, zlib.DEFLATED, -15)
        subc = sub.compress(subcode) + sub.flush()
        return bytes(ecc) + len(body).to_bytes(clb, "big") + body + subc
    if codec == "cdfl":
        sub = zlib.compressobj(9, zlib.DEFLATED, -15)
        subc = sub.compress(subcode) + sub.flush()
        return (audio_flac or b"") + subc
    raise ValueError(codec)


def build_chd(path, tracks: Sequence[dict], codecs: Sequence[str] = ("cdlz", "cdzl", "cdfl", ""),
              data_codec: int = 0, audio_codec: int = 2, gd: bool = True, hunk_frames: int = 8,
              compressed_map: bool = True, stereo: str = "indep", flac_kinds=("fixed2", "fixed2"),
              generic: Optional[str] = None, metadata_tag: Optional[bytes] = None) -> dict:
    """Write a CHD v5.

    ``tracks``: dicts ``{"type": "MODE1_RAW"|"MODE2_RAW"|"AUDIO"|"MODE1"|..., "data": bytes (frames*2352, audio
    little-endian; a cooked type such as ``MODE1`` has frames*2048 bytes, stored at the start of each frame like
    ``chdman createcd`` does), "pad": int = 0}``; ``pad`` zero frames are appended inside ``FRAMES`` (GD-ROM) and
    every track is padded with extra frames to a multiple of 4 like chdman does.  ``generic`` ("zlib" | "lzma" |
    "none") stores plain 2448-byte-frame hunks instead of the CD codecs.  Returns ``{"raw_sha1", "frames", ...}``.
    """
    hunk_bytes = hunk_frames * FRAME
    frames_out: list[bytes] = []
    meta: list[bytes] = []
    track_kinds: list[str] = []
    sizes = []
    for n, t in enumerate(tracks, 1):
        data = t["data"]
        ssize = chdlib._TYPES[t["type"]][0]
        sizes.append(ssize)
        assert len(data) % ssize == 0
        if ssize != SECTOR:         # cooked: every sector sits at the start of a zero padded 2352-byte frame
            data = b"".join(data[i:i + ssize] + bytes(SECTOR - ssize) for i in range(0, len(data), ssize))
        nframes = len(data) // SECTOR + t.get("pad", 0)
        body = data + bytes(t.get("pad", 0) * SECTOR)
        extra = (-nframes) % 4
        body += bytes(extra * SECTOR)
        audio = t["type"] == "AUDIO"
        track_kinds.append("A" if audio else "D")
        for i in range(nframes + extra):
            sec = body[i * SECTOR:(i + 1) * SECTOR]
            if audio:   # CHD hunks hold CD audio big-endian
                b = bytearray(sec)
                b[0::2], b[1::2] = sec[1::2], sec[0::2]
                sec = bytes(b)
            frames_out.append(sec + bytes(96))
        if gd:
            text = (f"TRACK:{n} TYPE:{t['type']} SUBTYPE:NONE FRAMES:{nframes} PAD:{t.get('pad', 0)} "
                    f"PREGAP:0 PGTYPE:MODE1 PGSUB:NONE POSTGAP:0\0")
            tag = b"CHGD"
        else:
            text = (f"TRACK:{n} TYPE:{t['type']} SUBTYPE:NONE FRAMES:{nframes} PREGAP:0 PGTYPE:MODE1 "
                    f"PGSUB:NONE POSTGAP:0\0")
            tag = b"CHT2"
        meta.append((metadata_tag or tag, text.encode("ascii")))
    total_frames = len(frames_out)
    hunks_n = (total_frames + hunk_frames - 1) // hunk_frames
    while len(frames_out) < hunks_n * hunk_frames:
        frames_out.append(bytes(FRAME))
    logical = total_frames * FRAME
    raw = b"".join(frames_out)
    # frame -> track kind (for the codec choice)
    kind_of_frame: list[str] = []
    for n, t in enumerate(tracks):
        nframes = len(t["data"]) // sizes[n] + t.get("pad", 0)
        kind_of_frame += [track_kinds[n]] * (nframes + (-nframes) % 4)
    kind_of_frame += ["D"] * (hunks_n * hunk_frames - len(kind_of_frame))

    out = bytearray(124)
    out += b""
    # metadata chain
    meta_off = 124
    meta_blob = bytearray()
    pos = meta_off
    for i, (tag, body) in enumerate(meta):
        size = 16 + len(body)
        nxt = pos + size if i + 1 < len(meta) else 0
        meta_blob += tag + bytes([1]) + len(body).to_bytes(3, "big") + struct.pack(">Q", nxt) + body
        pos += size
    out += meta_blob
    hunk_entries = []     # (type, length, offset, crc)
    seen: dict[bytes, int] = {}
    comp_slot = {c: i for i, c in enumerate(codecs) if c}
    for h in range(hunks_n):
        raw_h = raw[h * hunk_bytes:(h + 1) * hunk_bytes]
        if compressed_map and raw_h in seen and generic is None:
            hunk_entries.append((5, 0, seen[raw_h], 0))       # SELF reference
            continue
        seen.setdefault(raw_h, h)
        kinds = set(kind_of_frame[h * hunk_frames:(h + 1) * hunk_frames])
        crc = _crc16(raw_h)
        if generic is not None:
            if generic == "none" or not compressed_map:
                comp, ctype = raw_h, 4
            elif generic == "zlib":
                c = zlib.compressobj(9, zlib.DEFLATED, -15)
                comp, ctype = c.compress(raw_h) + c.flush(), comp_slot["zlib"]
            elif generic == "zstd":
                comp, ctype = zstd_compress(raw_h), comp_slot["zstd"]
            else:
                c = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=chdlib._lzma_filters(hunk_bytes))
                comp, ctype = c.compress(raw_h) + c.flush(), comp_slot["lzma"]
        else:
            frames_raw = [raw_h[i * FRAME:(i + 1) * FRAME] for i in range(hunk_frames)]
            sectors = b"".join(f[:SECTOR] for f in frames_raw)
            subcode = b"".join(f[SECTOR:] for f in frames_raw)
            if kinds == {"A"}:
                codec = codecs[audio_codec]
                flac = None
                if codec == "cdfl":
                    pcm_le = bytearray(sectors)
                    pcm_le[0::2], pcm_le[1::2] = sectors[1::2], sectors[0::2]
                    flac = flac_stream(bytes(pcm_le), block=hunk_frames * 588 // 4, kinds=flac_kinds, stereo=stereo)
                comp = _compress_hunk(codec, sectors, subcode, hunk_frames, flac, hunk_bytes)
                ctype = comp_slot[codec]
            else:
                codec = codecs[data_codec]
                comp = _compress_hunk(codec, sectors, subcode, hunk_frames, None, hunk_bytes)
                ctype = comp_slot[codec]
        hunk_entries.append((ctype, len(comp), len(out), crc))
        out += comp
    return _finish_chd(path, out, hunk_entries, hunks_n, meta_blob, hunk_bytes, FRAME, raw, logical, compressed_map,
                       generic, codecs, 124, extra={"frames": total_frames})


def _finish_chd(path, out: bytearray, hunk_entries, hunks_n: int, meta_blob, hunk_bytes: int, unit: int, raw: bytes,
                logical: int, compressed_map: bool, generic, codecs, meta_off: int, extra=None) -> dict:
    """Write the map (compressed or not) and the header of a CHD whose metadata and hunks are already in ``out``."""
    map_off = len(out)
    if not compressed_map:
        assert generic == "none"
        # uncompressed v5: hunks aligned to the hunk size, map = u32 per hunk (in hunk units)
        out = bytearray(out[:124 + len(meta_blob)])
        while len(out) % hunk_bytes:
            out += b"\0"
        offs = []
        for h in range(hunks_n):
            offs.append(len(out) // hunk_bytes)
            out += raw[h * hunk_bytes:(h + 1) * hunk_bytes]
        map_off = len(out)
        out += struct.pack(">%dI" % hunks_n, *offs)
        hdr_comp = [b"\0\0\0\0"] * 4
    else:
        # --- compressed map: flat 4-bit Huffman code (code == symbol), RLE for runs, self refs
        types = [e[0] for e in hunk_entries]
        sym_bits: list[tuple[int, int]] = []     # (value, bit width)
        i = 0
        first_off = 124 + len(meta_blob)
        while i < len(types):
            t = types[i]
            run = 1
            while i + run < len(types) and types[i + run] == t:
                run += 1
            sym_bits.append((t, 4))
            rest = run - 1
            while rest >= 3:
                if rest >= 19:
                    n = min(rest - 19, 255)
                    sym_bits += [(8, 4), ((n >> 4) & 15, 4), (n & 15, 4)]
                    rest -= 19 + n          # RLE_LARGE: this hunk + 2 + 16 + n repeats
                else:
                    n = rest - 3
                    sym_bits += [(7, 4), (n, 4)]
                    rest -= 3 + n           # RLE_SMALL: this hunk + 2 + n repeats
            for _ in range(rest):
                sym_bits.append((t, 4))
            i += run
        lengthbits = max(1, max((e[1] for e in hunk_entries), default=1).bit_length())
        selfbits = max(1, hunks_n.bit_length())
        bits = [format(4, "04b")] * 16
        bits_s = "".join(bits) + "".join(format(v, f"0{w}b") for v, w in sym_bits)
        # second pass fields
        rawmap = bytearray()
        for t, ln, off, crc in hunk_entries:
            if t <= 3:
                bits_s += format(ln, f"0{lengthbits}b") + format(crc, "016b")
            elif t == 4:
                bits_s += format(crc, "016b")
            elif t == 5:
                bits_s += format(off, f"0{selfbits}b")
            rawmap += struct.pack(">BBHHIH", t, ln >> 16, ln & 0xFFFF, off >> 32, off & 0xFFFFFFFF, crc if t != 5 else 0)
        bits_s += "0" * ((-len(bits_s)) % 8)
        mapdata = int(bits_s, 2).to_bytes(len(bits_s) // 8, "big")
        out += struct.pack(">I", len(mapdata)) + first_off.to_bytes(6, "big") + struct.pack(
            ">H", _crc16(bytes(rawmap))) + bytes([lengthbits, selfbits, 0, 0]) + mapdata
        hdr_comp = [c.encode("ascii") if c else b"\0\0\0\0" for c in codecs]
    raw_sha1 = hashlib.sha1(raw[:logical]).digest()
    header = bytearray()
    header += b"MComprHD" + struct.pack(">II", 124, 5) + b"".join(hdr_comp)
    header += struct.pack(">QQQII", logical, map_off, meta_off, hunk_bytes, unit)
    header += raw_sha1 + hashlib.sha1(raw_sha1 + bytes(meta_blob)).digest() + bytes(20)
    assert len(header) == 124
    out[:124] = header
    Path(path).write_bytes(bytes(out))
    info = {"raw_sha1": raw_sha1.hex(), "hunks": hunks_n}
    info.update(extra or {})
    return info


def build_dvd_chd(path, iso: bytes, codecs: Sequence[str] = ("lzma", "zlib", "huff", "flac"), hunk_sectors: int = 2,
                  pick=None, compressed_map: bool = True) -> dict:
    """Write a DVD CHD like ``chdman createdvd``: metadata ``DVD ``, 2048-byte units, the ISO as the logical data,
    header raw SHA-1 = the ISO's SHA-1. ``pick(hunk_index)`` chooses the codec of a hunk (``lzma`` | ``zlib`` |
    ``huff`` | ``flac`` | ``zstd`` | ``none``, or any other name from ``codecs``: a hunk of that codec with garbage
    data, to test the reader's refusal of a codec it does not know, e.g. ``wxyz``). Identical hunks become SELF
    references."""
    unit = 2048
    assert len(iso) % unit == 0
    hunk_bytes = hunk_sectors * unit
    hunks_n = (len(iso) + hunk_bytes - 1) // hunk_bytes
    raw = iso + bytes(hunks_n * hunk_bytes - len(iso))
    meta_blob = b"DVD " + bytes([1]) + (1).to_bytes(3, "big") + (0).to_bytes(8, "big") + b"\0"
    out = bytearray(124) + meta_blob
    entries = []
    seen: dict[bytes, int] = {}
    for h in range(hunks_n):
        raw_h = raw[h * hunk_bytes:(h + 1) * hunk_bytes]
        crc = _crc16(raw_h)
        if compressed_map and raw_h in seen:
            entries.append((5, 0, seen[raw_h], 0))
            continue
        seen.setdefault(raw_h, h)
        codec = (pick(h) if pick else ("lzma" if h % 2 == 0 else "zlib")) if compressed_map else "none"
        if codec == "none":
            comp, ctype = raw_h, 4
        elif codec == "zlib":
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            comp, ctype = c.compress(raw_h) + c.flush(), list(codecs).index("zlib")
        elif codec == "lzma":
            c = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=chdlib._lzma_filters(hunk_bytes))
            comp, ctype = c.compress(raw_h) + c.flush(), list(codecs).index("lzma")
        elif codec == "zstd" and zstd_compress(b"x") is not None:
            comp, ctype = zstd_compress(raw_h), list(codecs).index("zstd")
        elif codec == "huff":
            comp, ctype = huff_compress(raw_h), list(codecs).index("huff")
        elif codec == "flac":
            comp, ctype = b"L" + flac_stream(raw_h, block=len(raw_h) // 4), list(codecs).index("flac")
        else:
            comp, ctype = b"not really " + codec.encode(), list(codecs).index(codec)
        entries.append((ctype, len(comp), len(out), crc))
        out += comp
    return _finish_chd(path, out, entries, hunks_n, meta_blob, hunk_bytes, unit, raw, len(iso), compressed_map,
                       "none" if not compressed_map else None, codecs, 124,
                       extra={"sha1": hashlib.sha1(iso).hexdigest()})


# --------------------------------------------------------------------------- Redump-style discs + DAT

class Disc:
    """A synthetic GD-ROM-like disc: data track 1, audio track 2 (with GD pad), data track 3."""

    def __init__(self, name: str, seed: int, frames=(8, 6, 12), pad: int = 4) -> None:
        self.name = name
        self.t1 = make_data_track(frames[0], seed)
        self.t2 = make_audio_track(frames[1], seed + 1)
        self.t3 = make_data_track(frames[2], seed + 2, 4000)
        self.pad = pad
        self.tracks = [{"type": "MODE1_RAW", "data": self.t1},
                       {"type": "AUDIO", "data": self.t2, "pad": pad},
                       {"type": "MODE1_RAW", "data": self.t3}]

    @property
    def bins(self) -> list[bytes]:
        return [self.t1, self.t2, self.t3]

    def write_chd(self, path, **kw) -> dict:
        return build_chd(path, self.tracks, **kw)

    def write_raw(self, folder: Path, stem: Optional[str] = None, gdi_style: bool = True, markers: bool = False) -> Path:
        """A raw Redump-style set: ``stem.gdi`` + ``stemNN.bin/.raw`` (or .cue + one bin per track; ``markers``: the
        ``REM SINGLE-DENSITY AREA`` / ``REM HIGH-DENSITY AREA`` lines of Redump's Dreamcast cues)."""
        folder.mkdir(parents=True, exist_ok=True)
        stem = stem or self.name
        files = []
        for i, data in enumerate(self.bins, 1):
            ext = "raw" if i == 2 else "bin"
            f = folder / f"track{i:02d}.{ext}"
            f.write_bytes(data)
            files.append(f)
        if gdi_style:
            lines = [str(len(files))]
            lba = 0
            for i, (f, d) in enumerate(zip(files, self.bins), 1):
                lines.append(f'{i} {lba} {0 if i == 2 else 4} 2352 "{f.name}" 0')
                lba += len(d) // SECTOR
            sheet = folder / f"{stem}.gdi"
            sheet.write_text("\n".join(lines) + "\n")
        else:
            lines = []
            for i, f in enumerate(files, 1):
                if markers and i in (1, 3):
                    lines.append("REM SINGLE-DENSITY AREA" if i == 1 else "REM HIGH-DENSITY AREA")
                lines += [f'FILE "{f.name}" BINARY', f"  TRACK {i:02d} {'AUDIO' if i == 2 else 'MODE1/2352'}",
                          "    INDEX 01 00:00:00"]
            sheet = folder / f"{stem}.cue"
            sheet.write_text("\n".join(lines) + "\n")
        return sheet


def _digests(data: bytes) -> tuple[str, str, str]:
    return "%08x" % (zlib.crc32(data) & 0xFFFFFFFF), hashlib.md5(data).hexdigest(), hashlib.sha1(data).hexdigest()


def game_xml(name: str, category: str, bins: Sequence[bytes], two_digit: bool = False, style: str = "tracks") -> str:
    """``style``: ``tracks`` (``.cue`` + ``(Track N).bin``), ``bin`` (``.cue`` + ONE ``<name>.bin``: PlayStation 2
    CD games, single-track discs) or ``iso`` (ONE ``<name>.iso``: PlayStation 2 DVD games)."""
    cue = f"FILE x BINARY\n{name}".encode()
    crc, md5, sha1 = _digests(cue)
    rows = [] if style == "iso" else [
        f'\t\t<rom name="{escape(name)}.cue" size="{len(cue)}" crc="{crc}" md5="{md5}" sha1="{sha1}"/>']
    for i, b in enumerate(bins, 1):
        crc, md5, sha1 = _digests(b)
        tn = f"{i:02d}" if two_digit else str(i)
        fn = (f"{name}.iso" if style == "iso" else f"{name}.bin" if style == "bin"
              else f"{name} (Track {tn}).bin")
        rows.append(f'\t\t<rom name="{escape(fn)}" size="{len(b)}" crc="{crc}" '
                    f'md5="{md5}" sha1="{sha1}"/>')
    return (f'\t<game name="{escape(name)}">\n\t\t<category>{category}</category>\n'
            f'\t\t<description>{escape(name)}</description>\n' + "\n".join(rows) + "\n\t</game>\n")


def write_dat(path: Path, games: Sequence[tuple], version: str = "2026-06-14 18-25-41",
              name: str = "Sega - Dreamcast") -> Path:
    """``games``: ``(name, category, [track bytes])`` or ``(name, category, [bytes], style)`` (see :func:`game_xml`)."""
    body = "".join(game_xml(g[0], g[1], g[2], two_digit=(i % 2 == 1), style=(g[3] if len(g) > 3 else "tracks"))
                   for i, g in enumerate(games))
    path.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE datafile PUBLIC "-//Logiqx//DTD ROM Management Datafile//EN" '
        '"http://www.logiqx.com/Dats/datafile.dtd">\n<datafile>\n\t<header>\n'
        f"\t\t<name>{name}</name>\n\t\t<description>{name} - Discs</description>\n"
        f"\t\t<version>{version}</version>\n\t\t<date>{version}</date>\n\t\t<author>redump.org</author>\n"
        "\t\t<homepage>redump.org</homepage>\n\t\t<url>http://redump.org/</url>\n\t</header>\n"
        + body + "</datafile>\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- fake chdman (shell script)

FAKE_CHDMAN = r"""
# fake chdman for the tests: createcd copies $FAKE_CHD, extractcd copies the raw files in $FAKE_RAW
import glob, os, shutil, sys, time

def fail(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)

def progress(text):
    sys.stdout.write(text)
    sys.stdout.flush()

args = sys.argv[1:]
cmd = args.pop(0) if args else ""
if cmd in ("help", "-help", "--help"):
    print("chdman - MAME Compressed Hunks of Data (CHD) manager 0.fake")
    print("Usage: chdman <command> [options]")
    print("  createcd createdvd extractcd extractdvd info verify")
    sys.exit(1)
inp = out = outbin = ""
while args:
    if args[0] in ("-i", "-o", "-ob") and len(args) > 1:
        v = args[1]
        if args[0] == "-i": inp = v
        elif args[0] == "-o": out = v
        else: outbin = v
        del args[:2]
    else:
        del args[:1]
env = os.environ.get
if env("FAKE_LOG"):
    with open(env("FAKE_LOG"), "a") as f:
        f.write("%s %s %s\n" % (cmd, inp, out))
if env("FAKE_FAIL") == cmd:
    fail("Error: simulated %s failure" % cmd)
NOINPUT = "Error opening input: No such file or directory"
if cmd == "createcd":
    if not os.path.isfile(inp): fail(NOINPUT)
    if os.path.exists(out): fail("Error: output file already exists")
    for i in range(11):
        progress("Compressing, %d.0%% complete... (ratio=50.0%%)\r" % (i * 10))
        if env("FAKE_SLOW"): time.sleep(1)
    print()
    if env("FAKE_GDI_COPY"): shutil.copyfile(inp, env("FAKE_GDI_COPY"))
    src = env("FAKE_CHD")
    if inp.endswith(".cue") and env("FAKE_CHD_CUE"): src = env("FAKE_CHD_CUE")
    shutil.copyfile(src, out)
elif cmd == "extractcd":
    if not os.path.isfile(inp): fail(NOINPUT)
    d = os.path.dirname(out)
    progress("Extracting, 50.0% complete...\r")
    if env("FAKE_SLOW_EXTRACT"): time.sleep(20)
    raw = env("FAKE_RAW")
    if out.endswith(".gdi"):
        shutil.copyfile(os.path.join(raw, "disc.gdi"), out)
        for f in glob.glob(os.path.join(raw, "disc[0-9]*")):
            shutil.copyfile(f, os.path.join(d, os.path.basename(f)))
    else:
        shutil.copyfile(os.path.join(raw, "disc.cue"), out)
        shutil.copyfile(os.path.join(raw, "disc.bin"), outbin)
    print()
elif cmd == "createdvd":
    if not os.path.isfile(inp): fail(NOINPUT)
    if os.path.exists(out): fail("Error: output file already exists")
    progress("Compressing, 50.0% complete... (ratio=50.0%)\r")
    print()
    shutil.copyfile(env("FAKE_DVD_CHD") or env("FAKE_CHD"), out)
elif cmd == "extractdvd":
    if not os.path.isfile(inp): fail(NOINPUT)
    progress("Extracting, 50.0% complete...\r")
    if env("FAKE_SLOW_EXTRACT"): time.sleep(20)
    print()
    shutil.copyfile(env("FAKE_ISO"), out)
else:
    fail("unknown command", 2)
"""


def install_script(path: Path, source: str) -> Path:
    """An executable stand-in for a command-line tool, written in Python: ``<path>.py`` plus a launcher (a
    ``#!`` script on POSIX, a ``.cmd`` on Windows). Returns the launcher; calling it again replaces the script."""
    import sys
    path = Path(path)
    script = path.with_name(path.name + ".py")
    script.write_text(source, encoding="utf-8", newline="\n")
    if os.name == "nt":
        launcher = path.with_name(path.name + ".cmd")
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        launcher = path
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


def install_fake_chdman(folder: Path) -> Path:
    return install_script(folder / "chdman", FAKE_CHDMAN)


def prepare_fake_raw(folder: Path, disc: Disc) -> Path:
    """The files the fake ``extractcd`` hands out: ``disc.gdi`` + ``disc01.bin`` / ``disc02.raw`` / ``disc03.bin``."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = [str(len(disc.bins))]
    lba = 0
    for i, d in enumerate(disc.bins, 1):
        ext = "raw" if i == 2 else "bin"
        (folder / f"disc{i:02d}.{ext}").write_bytes(d)
        lines.append(f'{i} {lba} {0 if i == 2 else 4} 2352 "disc{i:02d}.{ext}" 0')
        lba += len(d) // SECTOR
    (folder / "disc.gdi").write_text("\n".join(lines) + "\n")
    return folder


def prepare_fake_raw_cd(folder: Path, tracks: Sequence[bytes]) -> Path:
    """The files the fake ``extractcd`` hands out for a (non-GD) CD CHD: ``disc.cue`` + ONE ``disc.bin``."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "disc.bin").write_bytes(b"".join(tracks))
    (folder / "disc.cue").write_text('FILE "disc.bin" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n')
    return folder


def isolate_temp(testcase, base: Path, ram: bool = False) -> Path:
    """Send romorg.tempspace scratch files to ``base/scratch`` (disk mode; RAM candidates off unless ``ram``)."""
    from unittest import mock
    from romorg import tempspace
    scratch = Path(base) / "scratch"
    patches = [mock.patch.dict(os.environ, {tempspace.ENV_DIR: str(scratch)})]
    if not ram:
        patches.append(mock.patch.object(tempspace, "ram_roots", return_value=[]))
    for p in patches:
        p.start()
        testcase.addCleanup(p.stop)
    return scratch


def make_symlink(link, target) -> None:
    """``link`` -> ``target``; skips the test where symlinks are not allowed (Windows without developer mode)."""
    import unittest
    try:
        Path(link).symlink_to(target)
    except (OSError, NotImplementedError):
        raise unittest.SkipTest("symlinks unavailable")


def find_bash():
    """A bash that really runs. On Windows Git for Windows' bash comes first: PATH can hold WSL's System32
    bash.exe ahead of it (it sees Linux paths, not ours), which is kept only as a last resort. Elsewhere: PATH."""
    import shutil
    import subprocess
    found = shutil.which("bash")
    git_bash = []
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent
        git_bash = [str(root / "usr" / "bin" / "bash.exe"), str(root / "bin" / "bash.exe")]
    parts = os.path.normcase(found or "").replace("/", "\\").split("\\")
    if os.name == "nt" and "system32" in parts:          # WSL launcher (or the Store stub)
        cands = git_bash + [found]
    else:
        cands = [found] + git_bash
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        try:
            if subprocess.run([c, "-c", "exit 0"], capture_output=True, timeout=20).returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def disable_native_flac(testcase) -> None:
    """Tests that count on the pure-Python decoder (slow enough to cancel, or the chdman policy) must not depend on
    whether this machine happens to have a libsndfile: switch it off here and in any worker process."""
    from unittest import mock
    from romorg import flacnative, nativeflac
    patches = [mock.patch.object(nativeflac, "_get", return_value=None),
               mock.patch.dict(os.environ, {nativeflac.ENV_ENABLE: "0", "ROMORG_NO_NATIVE_FLAC": "1"})]
    for p in patches:
        p.start()
        testcase.addCleanup(p.stop)
    flacnative.reload()                      # libFLAC too: forget a library loaded earlier, and load again afterwards
    testcase.addCleanup(flacnative.reload)


# --------------------------------------------------------------------------- MAME Huffman (test-side encoders)

def huff_lengths(counts: Sequence[int], maxbits: int = 16) -> list[int]:
    """Huffman code lengths for a histogram (0 for unused symbols). A lone symbol gets the length 1; a tree that
    would be deeper than ``maxbits`` is flattened (the rare symbols are counted up) until it fits."""
    import heapq
    counts = list(counts)
    while True:
        heap = [(c, i, (i,)) for i, c in enumerate(counts) if c]
        lengths = [0] * len(counts)
        if len(heap) == 1:
            lengths[heap[0][1]] = 1
            return lengths
        heapq.heapify(heap)
        while len(heap) > 1:
            a = heapq.heappop(heap)
            b = heapq.heappop(heap)
            for sym in a[2] + b[2]:
                lengths[sym] += 1
            heapq.heappush(heap, (a[0] + b[0], min(a[1], b[1]), a[2] + b[2]))
        if max(lengths) <= maxbits:
            return lengths
        counts = [(c + 1) // 2 + 1 if c else 0 for c in counts]


def mame_codes(lengths: Sequence[int]) -> dict[int, str]:
    """MAME's canonical codes as bit strings: the LONGEST codes are numbered from zero."""
    out: dict[int, str] = {}
    code = 0
    for ln in range(max(lengths), 0, -1):
        for sym, b in enumerate(lengths):
            if b == ln:
                out[sym] = format(code, f"0{ln}b")
                code += 1
        code >>= 1
    return out


def _bits_to_bytes(bits: str) -> bytes:
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big") if bits else b""


def huff_compress(data: bytes, lengths: Optional[Sequence[int]] = None, rle: bool = True) -> bytes:
    """A ``huff`` hunk as chdman writes it: the 24 lengths of a small tree, the 256 code lengths coded with it
    (symbol 0 = repeat the last length), then one code per byte."""
    return _bits_to_bytes(huff_bits(data, lengths, rle))


def huff_bits(data: bytes, lengths: Optional[Sequence[int]] = None, rle: bool = True) -> str:
    """:func:`huff_compress` as a bit string, before the zero padding to whole bytes."""
    if lengths is None:
        counts = [0] * 256
        for b in data:
            counts[b] += 1
        lengths = huff_lengths(counts)
    # tokens of the tree: ("len", L) or ("run", count)
    tokens: list[tuple[str, int]] = []
    i = 0
    last = None
    while i < 256:
        ln = lengths[i]
        run = 1
        while i + run < 256 and lengths[i + run] == ln:
            run += 1
        if rle and ln == last and run >= 2:
            take = min(run, 2 + 7 + 255)
            tokens.append(("run", take))
            i += take
            continue
        tokens.append(("len", ln))
        last = ln
        i += 1
    small_counts = [0] * 24
    for kind, v in tokens:
        small_counts[0 if kind == "run" else v + 1] += 1
    small = huff_lengths(small_counts, 6)
    if sum(1 for x in small if x) == 1:            # a lone symbol: give it a partner so the tree is complete
        small[[k for k in range(24) if not small[k]][0]] = 1
    codes = mame_codes(small)
    bits = format(small[0], "03b") + "000" + "".join(format(small[k], "03b") for k in range(1, 24))
    for kind, v in tokens:
        if kind == "len":
            bits += codes[v + 1]
        else:
            bits += codes[0] + (format(v - 2, "03b") if v - 2 < 7 else "111" + format(v - 9, "08b"))
    data_codes = mame_codes(lengths)
    bits += "".join(data_codes[b] for b in data)
    return bits


def _tree_rle(lengths: Sequence[int], numbits: int) -> str:
    """``export_tree_rle`` without the runs: a length of 1 is escaped as ``1, 1``."""
    return "".join(format(1, f"0{numbits}b") * 2 if ln == 1 else format(ln, f"0{numbits}b") for ln in lengths)


def avhuff_compress(meta: bytes, channels: Sequence[Sequence[int]], width: int, height: int, yuy2: bytes,
                    audio: str = "huffman") -> bytes:
    """An ``avhu`` hunk: ``channels`` are lists of signed 16-bit samples; ``audio`` = ``huffman`` | ``raw``
    (FLAC audio is covered by the real chdman fixtures)."""
    samples = len(channels[0]) if channels else 0
    deltas = []
    for ch in channels:
        prev = 0
        row = []
        for v in ch:
            row.append((v - prev) & 0xFFFF)
            prev = v
        deltas.append(row)
    tree = b""
    streams = []
    if audio == "raw" or not channels:
        streams = [b"".join(d.to_bytes(2, "big") for d in row) for row in deltas]
    else:
        hi_counts, lo_counts = [0] * 256, [0] * 256
        for row in deltas:
            for d in row:
                hi_counts[d >> 8] += 1
                lo_counts[d & 0xFF] += 1
        hi, lo = huff_lengths(hi_counts), huff_lengths(lo_counts)
        for t in (hi, lo):
            if sum(1 for x in t if x) == 1:
                t[[k for k in range(256) if not t[k]][0]] = 1
        tree = _bits_to_bytes(_tree_rle(hi, 5)) + _bits_to_bytes(_tree_rle(lo, 5))
        hc, lc = mame_codes(hi), mame_codes(lo)
        streams = [_bits_to_bytes("".join(hc[d >> 8] + lc[d & 0xFF] for d in row)) for row in deltas]
    head = bytes([len(meta), len(channels), samples >> 8, samples & 0xFF, width >> 8, width & 0xFF,
                  height >> 8, height & 0xFF]) + len(tree).to_bytes(2, "big")
    head += b"".join(len(s).to_bytes(2, "big") for s in streams)
    video = b""
    if width and height:
        # delta + run-length symbols per plane, in the order the decoder asks for them (Y Cb Y Cr)
        planes = {"y": [], "cb": [], "cr": []}
        order = []
        prev = {"y": 0, "cb": 0, "cr": 0}
        for row in range(height):
            line = yuy2[row * width * 2:(row + 1) * width * 2]
            pending = {"y": [], "cb": [], "cr": []}
            for x in range(0, len(line), 4):
                for name, v in (("y", line[x]), ("cb", line[x + 1]), ("y", line[x + 2]), ("cr", line[x + 3])):
                    pending[name].append(v)
            for name, values in pending.items():
                syms = []
                k = 0
                while k < len(values):
                    run = 0
                    while k + run < len(values) and values[k + run] == prev[name]:
                        run += 1
                    if run >= 8:            # a run code: 0x100 + (n - 8) for 8..15, 0x108.. for 16 << k
                        n = min(run, 15) if run < 16 else 1 << (run.bit_length() - 1)
                        syms.append((0x100 + n - 8) if n < 16 else 0x108 + (n.bit_length() - 5))
                        k += n
                    else:
                        syms.append((values[k] - prev[name]) & 0xFF)
                        prev[name] = values[k]
                        k += 1
                planes[name].append(syms)
        trees = {}
        for name in planes:
            counts = [0] * 272
            for syms in planes[name]:
                for sy in syms:
                    counts[sy] += 1
            ln = huff_lengths(counts)
            if sum(1 for x in ln if x) == 1:
                ln[[k for k in range(272) if not ln[k]][0]] = 1
            trees[name] = (ln, mame_codes(ln))
        bits = "10000000"
        for name in ("y", "cb", "cr"):
            t = _tree_rle(trees[name][0], 5)
            bits += t + "0" * (-(len(bits) + len(t)) % 8)
        # interleave: walk the pixels again and emit a plane's next symbol whenever its run is used up
        for row in range(height):
            cursor = {"y": 0, "cb": 0, "cr": 0}
            left = {"y": 0, "cb": 0, "cr": 0}
            for _x in range(width // 2):
                for name in ("y", "cb", "y", "cr"):
                    if left[name]:
                        left[name] -= 1
                        continue
                    sy = planes[name][row][cursor[name]]
                    cursor[name] += 1
                    bits += trees[name][1][sy]
                    if sy >= 0x100:
                        left[name] = ((8 + sy - 0x100) if sy <= 0x107 else 16 << (sy - 0x108)) - 1
        video = _bits_to_bytes(bits)
    return head + meta + tree + b"".join(streams) + video


def avhuff_raw(meta: bytes, channels: Sequence[Sequence[int]], width: int, height: int, yuy2: bytes,
               hunk_bytes: int) -> bytes:
    """The decoded form of an A/V hunk (``chav`` header, metadata, big-endian audio, picture, zero padding)."""
    samples = len(channels[0]) if channels else 0
    out = b"chav" + bytes([len(meta), len(channels), samples >> 8, samples & 0xFF, width >> 8, width & 0xFF,
                          height >> 8, height & 0xFF]) + meta
    for ch in channels:
        out += b"".join((v & 0xFFFF).to_bytes(2, "big") for v in ch)
    out += yuy2
    return out + bytes(hunk_bytes - len(out))


# --------------------------------------------------------------------------- old CHD versions (1 to 4)

def meta_entry(tag: bytes, body: bytes, next_offset: int, flags: int = 1) -> bytes:
    return tag + bytes([flags]) + len(body).to_bytes(3, "big") + next_offset.to_bytes(8, "big") + body


def overall_sha1(raw_sha1: bytes, metadata: Sequence[tuple]) -> bytes:
    """The v4 / v5 header SHA-1: the data SHA-1 and the hashes of the (flagged) metadata entries, sorted."""
    h = hashlib.sha1(raw_sha1)
    for item in sorted(tag + hashlib.sha1(body).digest() for tag, body in metadata):
        h.update(item)
    return h.digest()


def build_old_chd(path, version: int, data: bytes, hunk_bytes: int, kinds=None, metadata: Sequence[tuple] = (),
                  parent: Optional[dict] = None, compression: int = 1, avhu=None, geometry=None) -> dict:
    """Write a CHD of version 1 to 4 the way the chdman of its time did.

    ``kinds(hunk) -> "zlib" | "raw" | "mini" | "self" | "parent"`` picks the map entry type (default: zlib, and
    ``self`` for a hunk seen before); ``mini`` needs a hunk that is one 8-byte pattern, ``parent`` a hunk equal to
    the parent's hunk of the same number (``parent`` = the dict another build returned). ``avhu(hunk) -> bytes``
    gives the compressed form for ``compression`` 3. v1 / v2 are hard disks: ``geometry`` =
    ``(cylinders, heads, sectors)`` and 512-byte sectors."""
    hunks_n = (len(data) + hunk_bytes - 1) // hunk_bytes
    raw = data + bytes(hunks_n * hunk_bytes - len(data))
    head_len = {1: 76, 2: 80, 3: 120, 4: 108}[version]
    entry = 8 if version <= 2 else 16
    out = bytearray(head_len + entry * hunks_n)
    if version >= 3:
        out += b"EndOfListCookie\0"
    meta_off = 0
    if metadata:
        assert version >= 3
        meta_off = len(out)
        pos = meta_off
        for k, (tag, body) in enumerate(metadata):
            nxt = pos + 16 + len(body) if k + 1 < len(metadata) else 0
            out += meta_entry(tag, body, nxt)
            pos += 16 + len(body)
    seen: dict[bytes, int] = {}
    table = bytearray()
    for h in range(hunks_n):
        raw_h = raw[h * hunk_bytes:(h + 1) * hunk_bytes]
        kind = kinds(h) if kinds else ("self" if raw_h in seen else "zlib")
        crc = zlib.crc32(raw_h) & 0xFFFFFFFF
        if version <= 2:
            if kind == "zlib":
                c = zlib.compressobj(9, zlib.DEFLATED, -15)
                comp = c.compress(raw_h) + c.flush()
                if len(comp) >= hunk_bytes:
                    comp = raw_h
            else:
                comp = raw_h
            table += ((len(comp) << 44) | len(out)).to_bytes(8, "big")
            out += comp
            continue
        if kind == "self":
            table += struct.pack(">QIHBB", seen[raw_h], crc, 0, 0, 4)
            continue
        seen.setdefault(raw_h, h)
        if kind == "mini":
            assert raw_h == raw_h[:8] * (hunk_bytes // 8)
            table += struct.pack(">QIHBB", int.from_bytes(raw_h[:8], "big"), crc, 0, 0, 3)
        elif kind == "parent":
            table += struct.pack(">QIHBB", h, crc, 0, 0, 5)
        elif kind == "raw":
            table += struct.pack(">QIHBB", len(out), crc, hunk_bytes & 0xFFFF, hunk_bytes >> 16, 2)
            out += raw_h
        else:
            if compression == 3:
                comp = avhu(h)
            else:
                c = zlib.compressobj(9, zlib.DEFLATED, -15)
                comp = c.compress(raw_h) + c.flush()
            table += struct.pack(">QIHBB", len(out), crc, len(comp) & 0xFFFF, len(comp) >> 16, 1)
            out += comp
    out[head_len:head_len + len(table)] = table
    raw_sha1 = hashlib.sha1(raw[:len(data)]).digest()
    md5 = hashlib.md5(raw[:len(data)]).digest()
    flags = 1 if parent else 0
    header = bytearray(b"MComprHD" + struct.pack(">III", head_len, version, flags) + struct.pack(">I", compression))
    info = {"raw_sha1": raw_sha1.hex(), "md5": md5.hex(), "hunks": hunks_n}
    if version <= 2:
        cyls, heads, secs = geometry
        assert cyls * heads * secs * 512 == len(data) and hunk_bytes % 512 == 0
        header += struct.pack(">5I", hunk_bytes // 512, hunks_n, cyls, heads, secs) + md5
        header += bytes.fromhex(parent["md5"]) if parent else bytes(16)
        if version == 2:
            header += struct.pack(">I", 512)
        info["sha1"] = ""
    elif version == 3:
        header += struct.pack(">IQQ", hunks_n, len(data), meta_off) + md5
        header += (bytes.fromhex(parent["md5"]) if parent else bytes(16)) + struct.pack(">I", hunk_bytes)
        header += raw_sha1 + (bytes.fromhex(parent["sha1"]) if parent else bytes(20))
        info["sha1"] = raw_sha1.hex()
    else:
        sha1 = overall_sha1(raw_sha1, metadata)
        header += struct.pack(">IQQI", hunks_n, len(data), meta_off, hunk_bytes)
        header += sha1 + (bytes.fromhex(parent["sha1"]) if parent else bytes(20)) + raw_sha1
        info["sha1"] = sha1.hex()
    assert len(header) == head_len, (len(header), head_len)
    out[:head_len] = header
    Path(path).write_bytes(bytes(out))
    return info


def old_cd_metadata(tracks: Sequence[tuple], order: str = "<") -> bytes:
    """The binary ``CHCD`` track list of the first CD CHDs: ``tracks`` = ``(type number, frames)``."""
    body = struct.pack(order + "I", len(tracks))
    for ttype, frames in tracks:
        body += struct.pack(order + "6I", ttype, 0, 2352 if ttype in (1, 6, 7) else 2048, 0, frames, (-frames) % 4)
    return body + bytes(4 + 24 * 99 - len(body))


def bash_env(bash: str) -> dict:
    """The environment to run `bash` scripts in. Git for Windows' bash.exe started from PowerShell / cmd has no
    coreutils (dirname, mktemp ...) on PATH unless its own usr\bin is added; harmless elsewhere."""
    env = dict(os.environ)
    if os.name == "nt":
        d = Path(bash).resolve().parent
        extra = [str(d)] + ([str(d.parent / "usr" / "bin")] if d.name == "bin" else [])
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env
