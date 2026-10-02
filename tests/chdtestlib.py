"""Test-only helpers: a minimal CHD v5 *writer*, synthetic Dreamcast-style discs, a tiny FLAC encoder,
a Redump-style DAT generator and a fake ``chdman`` shell script.

The writer exists only here (the app only ever reads CHDs; creating them is chdman's job).  It can write
an uncompressed map or a compressed (Huffman coded, with RLE and self references) map, and ``cdlz`` /
``cdzl`` / ``cdfl`` / ``zlib`` / ``lzma`` / uncompressed hunks.
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
    b.put(0, 8)                  # header CRC-8 (not checked by the reader)
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
    b.put(0, 16)                 # frame CRC-16 (not checked)
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


def _compress_hunk(codec: str, raw_sectors: bytes, subcode: bytes, frames: int, audio_flac: Optional[bytes],
                   hunk_bytes: int) -> bytes:
    """Compress one CD hunk (``raw_sectors`` = frames*2352 bytes in hunk byte order)."""
    if codec in ("cdlz", "cdzl"):
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

    ``tracks``: dicts ``{"type": "MODE1_RAW"|"AUDIO"|..., "data": bytes (frames*2352, audio little-endian),
    "pad": int = 0}``; ``pad`` zero frames are appended inside ``FRAMES`` (GD-ROM) and every track is padded
    with extra frames to a multiple of 4 like chdman does.  ``generic`` ("zlib" | "lzma" | "none") stores
    plain 2448-byte-frame hunks instead of the CD codecs.  Returns ``{"raw_sha1", "frames", ...}``.
    """
    hunk_bytes = hunk_frames * FRAME
    frames_out: list[bytes] = []
    meta: list[bytes] = []
    track_kinds: list[str] = []
    for n, t in enumerate(tracks, 1):
        data = t["data"]
        assert len(data) % SECTOR == 0
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
        nframes = len(t["data"]) // SECTOR + t.get("pad", 0)
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
    header += struct.pack(">QQQII", logical, map_off, meta_off, hunk_bytes, FRAME)
    header += raw_sha1 + hashlib.sha1(raw_sha1 + bytes(meta_blob)).digest() + bytes(20)
    assert len(header) == 124
    out[:124] = header
    Path(path).write_bytes(bytes(out))
    return {"raw_sha1": raw_sha1.hex(), "hunks": hunks_n, "frames": total_frames}


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

    def write_raw(self, folder: Path, stem: Optional[str] = None, gdi_style: bool = True) -> Path:
        """A raw Redump-style set: ``stem.gdi`` + ``stemNN.bin/.raw`` (or .cue + one bin per track)."""
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
                lines += [f'FILE "{f.name}" BINARY', f"  TRACK {i:02d} {'AUDIO' if i == 2 else 'MODE1/2352'}",
                          "    INDEX 01 00:00:00"]
            sheet = folder / f"{stem}.cue"
            sheet.write_text("\n".join(lines) + "\n")
        return sheet


def _digests(data: bytes) -> tuple[str, str, str]:
    return "%08x" % (zlib.crc32(data) & 0xFFFFFFFF), hashlib.md5(data).hexdigest(), hashlib.sha1(data).hexdigest()


def game_xml(name: str, category: str, bins: Sequence[bytes], two_digit: bool = False) -> str:
    cue = f"FILE x BINARY\n{name}".encode()
    crc, md5, sha1 = _digests(cue)
    rows = [f'\t\t<rom name="{escape(name)}.cue" size="{len(cue)}" crc="{crc}" md5="{md5}" sha1="{sha1}"/>']
    for i, b in enumerate(bins, 1):
        crc, md5, sha1 = _digests(b)
        tn = f"{i:02d}" if two_digit else str(i)
        rows.append(f'\t\t<rom name="{escape(name)} (Track {tn}).bin" size="{len(b)}" crc="{crc}" '
                    f'md5="{md5}" sha1="{sha1}"/>')
    return (f'\t<game name="{escape(name)}">\n\t\t<category>{category}</category>\n'
            f'\t\t<description>{escape(name)}</description>\n' + "\n".join(rows) + "\n\t</game>\n")


def write_dat(path: Path, games: Sequence[tuple[str, str, Sequence[bytes]]], version: str = "2026-06-14 18-25-41",
              name: str = "Sega - Dreamcast") -> Path:
    body = "".join(game_xml(n, c, b, two_digit=(i % 2 == 1)) for i, (n, c, b) in enumerate(games))
    path.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE datafile PUBLIC "-//Logiqx//DTD ROM Management Datafile//EN" '
        '"http://www.logiqx.com/Dats/datafile.dtd">\n<datafile>\n\t<header>\n'
        f"\t\t<name>{name}</name>\n\t\t<description>{name} - Discs</description>\n"
        f"\t\t<version>{version}</version>\n\t\t<date>{version}</date>\n\t\t<author>redump.org</author>\n"
        "\t\t<homepage>redump.org</homepage>\n\t\t<url>http://redump.org/</url>\n\t</header>\n"
        + body + "</datafile>\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- fake chdman (shell script)

FAKE_CHDMAN = r"""#!/bin/sh
# fake chdman for the tests: createcd copies $FAKE_CHD, extractcd copies the raw files in $FAKE_RAW
cmd="$1"; shift
case "$cmd" in
  help|-help|--help)
    echo "chdman - MAME Compressed Hunks of Data (CHD) manager 0.fake"
    echo "Usage: chdman <command> [options]"
    echo "  createcd extractcd info verify"
    exit 1 ;;
esac
in=""; out=""; outbin=""
while [ $# -gt 0 ]; do
  case "$1" in
    -i) in="$2"; shift 2 ;;
    -o) out="$2"; shift 2 ;;
    -ob) outbin="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$FAKE_LOG" ] && echo "$cmd $in $out" >> "$FAKE_LOG"
if [ "$FAKE_FAIL" = "$cmd" ]; then echo "Error: simulated $cmd failure" >&2; exit 1; fi
case "$cmd" in
  createcd)
    [ -f "$in" ] || { echo "Error opening input: No such file or directory" >&2; exit 1; }
    [ -e "$out" ] && { echo "Error: output file already exists" >&2; exit 1; }
    i=0
    while [ $i -le 10 ]; do
      printf 'Compressing, %d.0%% complete... (ratio=50.0%%)\r' $((i*10))
      [ -n "$FAKE_SLOW" ] && sleep 1
      i=$((i+1))
    done
    echo
    cp "$FAKE_CHD" "$out" ;;
  extractcd)
    [ -f "$in" ] || { echo "Error opening input: No such file or directory" >&2; exit 1; }
    dir=$(dirname "$out")
    printf 'Extracting, 50.0%% complete...\r'
    case "$out" in
      *.gdi) cp "$FAKE_RAW"/disc.gdi "$out"; for f in "$FAKE_RAW"/disc[0-9]*; do cp "$f" "$dir"/; done ;;
      *) cp "$FAKE_RAW"/disc.cue "$out"; cp "$FAKE_RAW"/disc.bin "$outbin" ;;
    esac
    echo ;;
  *) echo "unknown command" >&2; exit 2 ;;
esac
exit 0
"""


def install_fake_chdman(folder: Path) -> Path:
    p = folder / "chdman"
    p.write_text(FAKE_CHDMAN)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


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
