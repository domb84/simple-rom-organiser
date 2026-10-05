"""Pure-Python CHD v5 reader for CD / GD-ROM images (stdlib only).

Reads the container (header, compressed or uncompressed map, metadata), decodes
hunks (``cdlz``, ``cdzl``, ``cdfl`` plus plain ``zlib``, ``lzma`` and
uncompressed) and exposes the *tracks* the way ``chdman extractcd`` writes them
(raw 2352-byte sectors, subcode dropped, GD-ROM pad frames dropped, audio in CD
little-endian byte order), so the bytes can be hashed and compared with Redump.

Track types: ``MODE1_RAW`` / ``MODE2_RAW`` / ``AUDIO`` are 2352-byte sectors; the cooked types
(``MODE1`` = 2048 bytes, ``MODE2_FORM1`` 2048, ``MODE2_FORM2`` 2324, ``MODE2`` 2336) are stored at the
START of each 2448-byte frame and extracted without padding / subcode (that is how ``chdman createcd`` keeps a
PlayStation 2 DVD ISO: a ``MODE1`` track, 2048 bytes per frame). DVD CHDs of ``chdman createdvd`` (metadata
``DVD ``, 2048-byte units, header raw SHA-1 = the ISO's SHA-1) are exposed as one synthetic ``DVD`` track.
Codecs the built-in reader cannot decode (``zstd`` / ``cdzs``, ``huff``, ``flac`` data) raise
:class:`ChdUnsupported` with ``needs_chdman`` set.

Nothing is ever loaded as a whole: hunks are read, decoded and handed out one
at a time.

Codec notes (as implemented by libchdr / MAME):

``cdlz``  ``[ecc bitmap: ceil(frames/8)] [len of the LZMA part: 2 bytes]
          [raw LZMA1 of frames*2352 sector bytes] [raw deflate of frames*96 subcode]``;
          frames whose ECC bit is set had sync + P/Q parity removed
          (``cdecc.generate`` rebuilds them).
``cdzl``  the same with raw deflate for the sector part.
``cdfl``  ``[FLAC frames of frames*588 stereo samples] [raw deflate of the subcode]``.

CD audio is stored big-endian inside hunks; the extraction swaps it back.
"""

from __future__ import annotations

import hashlib
import lzma
import os
import threading
import re
import struct
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import cdecc, flacdec

READER_CODECS = frozenset({"cdlz", "cdzl", "cdfl", "zlib", "lzma"})  # what the built-in reader can decode

__all__ = ["Chd", "ChdError", "ChdUnsupported", "Track", "TrackHash", "hash_track",
           "open_chd", "is_chd", "frames_to_bytes"]

DECODE_THREADS = max(1, min(8, (os.cpu_count() or 2) - 1))   # hunk decode threads per open CHD (1 = off)

CD_FRAME = 2448
CD_SECTOR = 2352
CD_SUBCODE = 96
CD_TRACK_PADDING = 4

# compression types of a v5 map entry
_T_NONE, _T_SELF, _T_PARENT, _T_RLE_SMALL, _T_RLE_LARGE = 4, 5, 6, 7, 8
_T_SELF0, _T_SELF1, _T_PARENT_SELF, _T_PARENT0, _T_PARENT1 = 9, 10, 11, 12, 13


class ChdError(Exception):
    """Corrupt / unreadable CHD."""


class ChdUnsupported(ChdError):
    """A valid CHD that this reader cannot decode (old version, zstd, parent, ...).

    ``needs_chdman``: chdman could decode it (a codec the built-in reader lacks)."""

    needs_chdman = False


_CODEC_NAMES = {"zstd": "Zstandard", "cdzs": "Zstandard (CD)", "huff": "Huffman", "flac": "FLAC data",
                "avhu": "AV Huffman"}


def _unsupported_codec(codec: str) -> ChdUnsupported:
    name = _CODEC_NAMES.get(codec)
    exc = ChdUnsupported(f"needs chdman: the CHD uses the '{codec}' compression"
                         + (f" ({name})" if name else "") + ", which the built-in reader cannot decode")
    exc.needs_chdman = True
    return exc


def is_chd(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"MComprHD"
    except OSError:
        return False


# --------------------------------------------------------------------------- CRC16
def _crc16_table():
    t = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        t.append(c)
    return t


_CRC16 = _crc16_table()


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    t = _CRC16
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ t[(crc >> 8) ^ b]
    return crc


# --------------------------------------------------------------------------- map decode
class _BitReader:
    """MSB-first bit reader over a bytes object."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        pos = self.pos
        first = pos >> 3
        last = (pos + n + 7) >> 3
        chunk = self.data[first:last]
        if len(chunk) < last - first:
            raise ChdError("compressed map is truncated")
        val = int.from_bytes(chunk, "big")
        total = (last - first) * 8
        val >>= total - (pos & 7) - n
        self.pos = pos + n
        return val & ((1 << n) - 1)


class _Huffman:
    """Canonical Huffman decoder (libchdr style RLE-coded tree, 16 symbols, 8 bits)."""

    def __init__(self, br: _BitReader, numcodes: int = 16, maxbits: int = 8):
        numbits = 5 if maxbits >= 16 else 4 if maxbits >= 8 else 3
        lengths: List[int] = []
        while len(lengths) < numcodes:
            nb = br.read(numbits)
            if nb != 1:
                lengths.append(nb)
            else:
                nb = br.read(numbits)
                if nb == 1:
                    lengths.append(1)
                else:
                    rep = br.read(numbits) + 3
                    if len(lengths) + rep > numcodes:
                        raise ChdError("bad Huffman tree in the CHD map")
                    lengths.extend([nb] * rep)
        histo = [0] * 33
        for b in lengths:
            if b > maxbits:
                raise ChdError("bad Huffman tree in the CHD map")
            histo[b] += 1
        start = 0
        for ln in range(32, 0, -1):
            nxt = (start + histo[ln]) >> 1
            histo[ln] = start
            start = nxt
        self.maxbits = maxbits
        table = [None] * (1 << maxbits)
        for sym, b in enumerate(lengths):
            if b == 0:
                continue
            code = histo[b]
            histo[b] += 1
            lo = code << (maxbits - b)
            for i in range(lo, lo + (1 << (maxbits - b))):
                table[i] = (sym, b)
        self.table = table

    def decode(self, br: _BitReader) -> int:
        pos = br.pos
        first = pos >> 3
        chunk = br.data[first:first + 3]
        chunk = chunk + b"\x00" * (3 - len(chunk))
        val = int.from_bytes(chunk, "big")
        window = (val >> (24 - (pos & 7) - self.maxbits)) & ((1 << self.maxbits) - 1)
        ent = self.table[window]
        if ent is None:
            raise ChdError("bad Huffman code in the CHD map")
        br.pos = pos + ent[1]
        return ent[0]


# --------------------------------------------------------------------------- metadata
_TRACK_TAGS = {b"CHT2", b"CHTR", b"CHGD", b"CHGT"}
_DVD_TAG = b"DVD "
DVD_SECTOR = 2048
_TYPES = {
    # name: (bytes of one sector in the extracted file, offset inside the frame's 2352 bytes). A cooked type is
    # stored by chdman at the start of the frame (offset 0), the raw types fill all 2352 bytes.
    "MODE1": (2048, 0), "MODE1/2048": (2048, 0),
    "MODE1_RAW": (2352, 0), "MODE1/2352": (2352, 0),
    "MODE2": (2336, 0), "MODE2/2336": (2336, 0),
    "MODE2_FORM1": (2048, 0), "MODE2/2048": (2048, 0),
    "MODE2_FORM2": (2324, 0), "MODE2/2324": (2324, 0),
    "MODE2_FORM_MIX": (2336, 0),
    "MODE2_RAW": (2352, 0), "MODE2/2352": (2352, 0),
    "AUDIO": (2352, 0),
    "DVD": (2048, 0),          # the synthetic track of a createdvd CHD
}


@dataclass
class Track:
    number: int
    type: str
    subtype: str
    frames: int                    # frames recorded for the track (incl. PAD frames)
    pad: int = 0                   # GD-ROM: padding frames at the end of ``frames``
    pregap: int = 0
    pgtype: str = ""
    pgsub: str = ""
    postgap: int = 0
    start: int = 0                 # first frame of the track inside the CHD
    gd: bool = False

    @property
    def is_audio(self) -> bool:
        return self.type == "AUDIO"

    @property
    def sector_size(self) -> int:
        return _TYPES.get(self.type, (2352, 0))[0]

    @property
    def sector_offset(self) -> int:
        return _TYPES.get(self.type, (2352, 0))[1]

    @property
    def data_frames(self) -> int:
        """Frames that end up in the extracted file (pad frames dropped)."""
        return max(0, self.frames - self.pad)

    @property
    def size(self) -> int:
        return self.data_frames * self.sector_size

    @property
    def virtual_pregap(self) -> bool:
        return self.pregap > 0 and self.pgtype.startswith("V")

    def to_dict(self) -> dict:
        return {"number": self.number, "type": self.type, "frames": self.frames, "pad": self.pad,
                "size": self.size, "audio": self.is_audio}


def frames_to_bytes(track: Track) -> int:
    return track.size


_KV = re.compile(r"(\w+):(\S+)")


def parse_track_metadata(tag: bytes, text: str) -> Track:
    kv = dict(_KV.findall(text))
    try:
        num = int(kv["TRACK"])
        ttype = kv["TYPE"]
        frames = int(kv["FRAMES"])
    except (KeyError, ValueError) as exc:
        raise ChdError(f"unparseable track metadata: {text!r}") from exc
    return Track(number=num, type=ttype, subtype=kv.get("SUBTYPE", "NONE"), frames=frames,
                 pad=int(kv.get("PAD", 0) or 0), pregap=int(kv.get("PREGAP", 0) or 0),
                 pgtype=kv.get("PGTYPE", ""), pgsub=kv.get("PGSUB", ""),
                 postgap=int(kv.get("POSTGAP", 0) or 0), gd=tag in (b"CHGD", b"CHGT"))


# --------------------------------------------------------------------------- the reader
def _lzma_filters(hunk_bytes: int):
    # libchdr: level 9 (lc=3 lp=0 pb=2) with the dictionary reduced to the hunk size
    dict_size = 1 << 16
    while dict_size < hunk_bytes * 2:
        dict_size <<= 1
    return [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": 3, "lp": 0, "pb": 2}]


def _inflate_raw(data: bytes, size: int) -> bytes:
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(data, size)
    except zlib.error as exc:
        raise ChdError(f"corrupt deflate data: {exc}") from exc
    return out


def _lzma_raw(data: bytes, size: int, filters) -> bytes:
    d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filters)
    try:
        out = d.decompress(data, size)
    except lzma.LZMAError as exc:
        raise ChdError(f"corrupt LZMA data: {exc}") from exc
    return out


class Chd:
    """An opened CHD v5 file.  Use as a context manager or call :meth:`close`."""

    def __init__(self, path, fileobj: Optional[BinaryIO] = None, load_map: bool = True):
        self.path = str(path)
        self._f = fileobj if fileobj is not None else open(path, "rb")
        self._own = fileobj is None
        self._cache: Dict[int, bytes] = {}
        self._io_lock = threading.Lock()
        self._filters = None
        try:
            self._read_header()
            self._read_metadata()
            self._map_loaded = False
            if load_map:
                self._ensure_map()
        except BaseException:
            self.close()
            raise

    # -- context manager
    def __enter__(self) -> "Chd":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._own and self._f is not None:
            self._f.close()
        self._f = None

    # -- header
    def _read_header(self) -> None:
        f = self._f
        f.seek(0)
        h = f.read(124)
        if len(h) < 16 or h[:8] != b"MComprHD":
            raise ChdError("not a CHD file")
        length, version = struct.unpack(">II", h[8:16])
        if version != 5:
            raise ChdUnsupported(f"CHD version {version} is not supported (only v5; re-create with a current chdman)")
        if length != 124 or len(h) < 124:
            raise ChdError("bad CHD v5 header length")
        self.version = version
        self.compressors = tuple(h[16 + 4 * i:20 + 4 * i].decode("latin-1") if h[16 + 4 * i:20 + 4 * i] != b"\0\0\0\0" else ""
                                 for i in range(4))
        (self.logical_bytes, self.map_offset, self.meta_offset,
         self.hunk_bytes, self.unit_bytes) = struct.unpack(">QQQII", h[32:64])
        self.raw_sha1 = h[64:84].hex()
        self.sha1 = h[84:104].hex()
        self.parent_sha1 = h[104:124].hex()
        if self.hunk_bytes == 0 or self.unit_bytes == 0:
            raise ChdError("bad CHD geometry")
        if any(self.parent_sha1) and self.parent_sha1 != "0" * 40:
            raise ChdUnsupported("CHD needs a parent file")
        self.hunk_count = (self.logical_bytes + self.hunk_bytes - 1) // self.hunk_bytes
        self.compressed = self.compressors[0] != ""

    # -- map
    def _ensure_map(self) -> None:
        """Parse the hunk map on first use (header / metadata alone are enough to list the tracks)."""
        if not self._map_loaded:
            self._read_map()
            self._map_loaded = True

    def _read_map(self) -> None:
        f = self._f
        n = self.hunk_count
        if not self.compressed:
            f.seek(self.map_offset)
            raw = f.read(4 * n)
            if len(raw) < 4 * n:
                raise ChdError("map is truncated")
            offs = struct.unpack(">%dI" % n, raw)
            self._ctype = [_T_NONE] * n
            self._clen = [self.hunk_bytes] * n
            self._coff = [o * self.hunk_bytes for o in offs]
            self._ccrc = [0] * n
            for i, o in enumerate(offs):
                if o == 0:
                    raise ChdUnsupported("CHD references a parent hunk")
            return
        f.seek(self.map_offset)
        head = f.read(16)
        if len(head) < 16:
            raise ChdError("map is truncated")
        mapbytes = struct.unpack(">I", head[:4])[0]
        firstoffs = int.from_bytes(head[4:10], "big")
        mapcrc = struct.unpack(">H", head[10:12])[0]
        lengthbits, selfbits, parentbits = head[12], head[13], head[14]
        data = f.read(mapbytes)
        if len(data) < mapbytes:
            raise ChdError("map is truncated")
        br = _BitReader(data)
        huff = _Huffman(br)
        types = [0] * n
        rep = 0
        last = 0
        for i in range(n):
            if rep > 0:
                types[i] = last
                rep -= 1
                continue
            val = huff.decode(br)
            if val == _T_RLE_SMALL:
                types[i] = last
                rep = 2 + huff.decode(br)
            elif val == _T_RLE_LARGE:
                types[i] = last
                rep = 2 + 16 + (huff.decode(br) << 4)
                rep += huff.decode(br)
            else:
                types[i] = last = val
        lens = [0] * n
        offs = [0] * n
        crcs = [0] * n
        cur = firstoffs
        lastself = 0
        lastparent = 0
        rawmap = bytearray()
        for i in range(n):
            t = types[i]
            off = cur
            ln = 0
            crc = 0
            if t <= 3:
                ln = br.read(lengthbits)
                cur += ln
                crc = br.read(16)
            elif t == _T_NONE:
                ln = self.hunk_bytes
                cur += ln
                crc = br.read(16)
            elif t == _T_SELF:
                off = br.read(selfbits)
                lastself = off
            elif t == _T_PARENT:
                off = br.read(parentbits)
                lastparent = off
            elif t in (_T_SELF0, _T_SELF1):
                if t == _T_SELF1:
                    lastself += 1
                off = lastself
                t = _T_SELF
            elif t in (_T_PARENT0, _T_PARENT1, _T_PARENT_SELF):
                raise ChdUnsupported("CHD references a parent hunk")
            else:
                raise ChdError("bad compression type in the CHD map")
            types[i] = t
            lens[i] = ln
            offs[i] = off
            crcs[i] = crc
            rawmap += struct.pack(">BBHHIH", t, ln >> 16, ln & 0xFFFF, off >> 32, off & 0xFFFFFFFF, crc)
        if crc16(bytes(rawmap)) != mapcrc:
            raise ChdError("CHD map CRC mismatch (file damaged?)")
        self._ctype, self._clen, self._coff, self._ccrc = types, lens, offs, crcs

    # -- metadata
    def _read_metadata(self) -> None:
        f = self._f
        self.metadata: List[Tuple[bytes, bytes]] = []
        off = self.meta_offset
        seen = set()
        while off:
            if off in seen:
                raise ChdError("metadata loop")
            seen.add(off)
            f.seek(off)
            m = f.read(16)
            if len(m) < 16:
                raise ChdError("metadata is truncated")
            tag = m[:4]
            length = int.from_bytes(m[5:8], "big")
            nxt = struct.unpack(">Q", m[8:16])[0]
            body = f.read(length)
            if len(body) < length:
                raise ChdError("metadata is truncated")
            self.metadata.append((tag, body))
            off = nxt
        tracks: List[Track] = []
        for tag, body in self.metadata:
            if tag in _TRACK_TAGS:
                text = body.split(b"\0", 1)[0].decode("ascii", "replace")
                tracks.append(parse_track_metadata(tag, text))
        tracks.sort(key=lambda t: t.number)
        start = 0
        for t in tracks:
            t.start = start
            start += t.frames + (-t.frames) % CD_TRACK_PADDING
        self.is_gd = any(t.gd for t in tracks)
        self.is_cd = bool(tracks)
        self.is_dvd = (not tracks) and any(tag == _DVD_TAG for tag, _b in self.metadata)
        if self.is_dvd:
            # chdman createdvd: the ISO is the logical data (2048-byte units); header raw SHA-1 = the ISO's SHA-1
            if self.unit_bytes != DVD_SECTOR or self.logical_bytes % DVD_SECTOR or self.hunk_bytes % DVD_SECTOR:
                raise ChdUnsupported("a DVD CHD with an unusual unit / hunk size")
            tracks = [Track(number=1, type="DVD", subtype="NONE", frames=self.logical_bytes // DVD_SECTOR)]
            start = tracks[0].frames
        self.tracks = tracks
        self.total_frames = start
        self.frames_per_hunk = self.hunk_bytes // self.unit_bytes if self.unit_bytes else 0
        if self.is_cd and self.unit_bytes != CD_FRAME:
            raise ChdError("CD metadata but unit size is not 2448")

    # -- hunks
    def _codec_for(self, ctype: int) -> str:
        tag = self.compressors[ctype]
        if not tag:
            raise ChdError("hunk uses an undefined compressor slot")
        return tag

    def read_hunk_raw(self, index: int, with_subcode: bool = True) -> bytes:
        """The hunk as stored in the CHD's logical stream (frames of 2448 for a CD).

        With ``with_subcode=False`` a CD hunk comes back as bare 2352-byte sectors
        (cheaper: the subcode is neither inflated nor copied).
        """
        self._ensure_map()
        if index < 0 or index >= self.hunk_count:
            raise ChdError("hunk index out of range")
        ctype = self._ctype[index]
        if ctype == _T_SELF:
            return self.read_hunk_raw(self._coff[index], with_subcode)
        comp = self._read_at(self._coff[index], self._clen[index])
        if len(comp) < self._clen[index]:
            raise ChdError("file is truncated (hunk %d)" % index)
        if ctype == _T_NONE:
            raw = comp
            if not with_subcode and self.is_cd:
                return _strip_subcode(raw)
            return raw
        if ctype > 3:
            raise ChdUnsupported("unsupported hunk type %d" % ctype)
        codec = self._codec_for(ctype)
        if codec in ("cdlz", "cdzl", "cdfl"):
            return self._decode_cd(codec, comp, with_subcode)
        if codec == "zlib":
            raw = _inflate_raw(comp, self.hunk_bytes)
        elif codec == "lzma":
            if self._filters is None:
                self._filters = _lzma_filters(self.hunk_bytes)
            raw = _lzma_raw(comp, self.hunk_bytes, self._filters)
        else:
            raise _unsupported_codec(codec)
        if len(raw) != self.hunk_bytes:
            raise ChdError("hunk %d decoded to the wrong size" % index)
        if not with_subcode and self.is_cd:
            return _strip_subcode(raw)
        return raw

    def _cd_base(self, codec: str, comp: bytes):
        """Decode the sector part of a CD hunk: ``(base, pending, end)``.

        ``pending`` lists the sectors whose sync + P/Q parity still has to be
        regenerated (``cdecc.generate``); ``end`` is where the subcode starts.
        """
        frames = self.hunk_bytes // CD_FRAME
        nbytes = frames * CD_SECTOR
        pending: list = []
        if codec == "cdfl":
            pcm, end = flacdec.decode_frames(comp, 0, frames * (CD_SECTOR // 4))
            pcm.byteswap()                      # hunks hold CD audio big-endian
            base = bytearray(pcm.tobytes())
        else:
            ecc_bytes = (frames + 7) // 8
            clb = 2 if self.hunk_bytes < 65536 else 3
            ecc = comp[:ecc_bytes]
            complen = int.from_bytes(comp[ecc_bytes:ecc_bytes + clb], "big")
            hdr = ecc_bytes + clb
            body = comp[hdr:hdr + complen]
            if codec == "cdlz":
                if self._filters is None:
                    self._filters = _lzma_filters(nbytes)
                base = bytearray(_lzma_raw(body, nbytes, self._filters))
            else:
                base = bytearray(_inflate_raw(body, nbytes))
            end = hdr + complen
            if len(base) == nbytes and any(ecc):
                mv = memoryview(base)
                pending = [mv[fr * CD_SECTOR:(fr + 1) * CD_SECTOR] for fr in range(frames)
                           if ecc[fr >> 3] & (1 << (fr & 7))]
        if len(base) != nbytes:
            raise ChdError("CD hunk decoded to the wrong size")
        return base, pending, end

    def _decode_cd(self, codec: str, comp: bytes, with_subcode: bool) -> bytes:
        frames = self.hunk_bytes // CD_FRAME
        base, pending, end = self._cd_base(codec, comp)
        cdecc.generate(pending)
        if not with_subcode:
            return bytes(base)
        sub = _inflate_raw(comp[end:], frames * CD_SUBCODE)
        if len(sub) != frames * CD_SUBCODE:
            raise ChdError("CD subcode has the wrong size")
        out = bytearray(frames * CD_FRAME)
        for fr in range(frames):
            out[fr * CD_FRAME:fr * CD_FRAME + CD_SECTOR] = base[fr * CD_SECTOR:(fr + 1) * CD_SECTOR]
            out[fr * CD_FRAME + CD_SECTOR:(fr + 1) * CD_FRAME] = sub[fr * CD_SUBCODE:(fr + 1) * CD_SUBCODE]
        return bytes(out)

    GROUP = 16          # hunks decoded together so the ECC work is batched

    def _read_at(self, offset: int, length: int) -> bytes:
        with self._io_lock:                 # decode threads share the one file object
            self._f.seek(offset)
            return self._f.read(length)

    def _decode_sectors(self, first: int, last: int) -> Dict[int, bytes]:
        """Hunks ``first..last-1`` as bare 2352-byte sectors. Thread-safe (touches no shared state but the file)."""
        bases = []
        pending: list = []
        for i in range(first, last):
            ctype = self._ctype[i]
            codec = self.compressors[ctype] if ctype <= 3 else ""
            if codec in ("cdlz", "cdzl", "cdfl") and self.is_cd:
                comp = self._read_at(self._coff[i], self._clen[i])
                if len(comp) < self._clen[i]:
                    raise ChdError("file is truncated (hunk %d)" % i)
                base, pend, _end = self._cd_base(codec, comp)
                pending.extend(pend)
                bases.append((i, base))
            else:
                bases.append((i, self.read_hunk_raw(i, with_subcode=False)))
        cdecc.generate(pending)
        return {i: bytes(b) for i, b in bases}

    def _hunk_sectors(self, index: int) -> bytes:
        got = self._cache.get(index)
        if got is not None:
            return got
        self._ensure_map()
        self._cache = self._decode_sectors(index, min(self.hunk_count, index + self.GROUP))
        return self._cache[index]

    def _ahead(self, starts: Sequence[int], decode: Callable[[int], object]) -> Iterator[object]:
        """``decode(start)`` for every start, in order, computed a few items ahead by :data:`DECODE_THREADS` threads
        (zlib, lzma and hashlib release the GIL, so data hunks decode in parallel; FLAC audio does not gain)."""
        threads = DECODE_THREADS
        if threads <= 1 or len(starts) < 2:
            for s in starts:
                yield decode(s)
            return
        self._ensure_map()
        if self._filters is None and self.is_cd:   # lazily built: do it before the threads start
            self._filters = _lzma_filters(self.hunk_bytes // CD_FRAME * CD_SECTOR)
        window: "deque" = deque()
        it = iter(starts)
        ex = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="romorg-chd")
        try:
            for s in it:
                window.append(ex.submit(decode, s))
                if len(window) >= threads * 2:
                    yield window.popleft().result()
            while window:
                yield window.popleft().result()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    # -- tracks
    def iter_frames(self, first_frame: int, count: int) -> Iterator[bytes]:
        """Raw 2352-byte sectors (no subcode) of ``count`` frames from CHD frame ``first_frame``."""
        if not self.is_cd:
            raise ChdError("not a CD / GD-ROM CHD")
        fph = self.frames_per_hunk
        end = first_frame + count
        if count <= 0:
            return
        self._ensure_map()
        h0, h1 = first_frame // fph, (end - 1) // fph + 1
        group = self.GROUP

        def decode(start: int) -> bytes:
            got = self._decode_sectors(start, min(h1, start + group))
            return b"".join([got[i] for i in range(start, min(h1, start + group))])

        per = fph * CD_SECTOR
        for start, data in zip(range(h0, h1, group), self._ahead(range(h0, h1, group), decode)):
            lo = max(first_frame - start * fph, 0) * CD_SECTOR
            hi = min(len(data), (end - start * fph) * CD_SECTOR)
            for o in range(lo, hi, per * 4):            # hand out at most 4 hunks at a time (cancel / progress)
                yield data[o:min(hi, o + per * 4)]

    def iter_track(self, track: Track, cancel: Optional[Callable[[], bool]] = None,
                   skip_audio: bool = False, first: int = 0, count: Optional[int] = None) -> Iterator[bytes]:
        """The extracted bytes of a track, as ``chdman extractcd`` writes its bin / gdi file (a DVD CHD: the ISO,
        as ``chdman extractdvd`` writes it).

        ``first`` / ``count`` select a run of the track's frames (2048-byte sectors for a DVD): the pieces of
        consecutive runs concatenate to the whole track, which lets several processes decode one track."""
        total = self.track_frames(track)
        first = max(0, min(first, total))
        count = total - first if count is None else max(0, min(count, total - first))
        if self.is_dvd:
            yield from self._iter_dvd(cancel, first, count)
            return
        ssize, soff = track.sector_size, track.sector_offset
        audio = track.is_audio
        for chunk in self.iter_frames(track.start + first, count):
            if cancel is not None and cancel():
                raise InterruptedError("cancelled")
            if audio:
                b = bytearray(chunk)
                b[0::2] = chunk[1::2]
                b[1::2] = chunk[0::2]
                yield bytes(b)
            elif ssize == CD_SECTOR:
                yield chunk
            else:
                n = len(chunk) // CD_SECTOR
                yield b"".join(chunk[i * CD_SECTOR + soff:i * CD_SECTOR + soff + ssize] for i in range(n))

    def track_frames(self, track: Track) -> int:
        """How many frames :meth:`iter_track` covers (2048-byte sectors for a DVD)."""
        return self.logical_bytes // DVD_SECTOR if self.is_dvd else track.data_frames

    def _iter_dvd(self, cancel: Optional[Callable[[], bool]] = None, first: int = 0,
                  frames: Optional[int] = None) -> Iterator[bytes]:
        lo = first * DVD_SECTOR
        hi = self.logical_bytes if frames is None else min(self.logical_bytes, lo + frames * DVD_SECTOR)
        if hi <= lo:
            return
        hb = self.hunk_bytes
        # decoded a few hunks at a time so small hunks (2-4 KiB) do not cost one Python iteration each
        per = max(1, (1 << 20) // hb)
        h0, h1 = lo // hb, min(self.hunk_count, (hi - 1) // hb + 1)
        self._ensure_map()

        def decode(start: int) -> bytes:
            return b"".join([self.read_hunk_raw(i, with_subcode=False) for i in range(start, min(h1, start + per))])

        done = lo
        for start, data in zip(range(h0, h1, per), self._ahead(range(h0, h1, per), decode)):
            if cancel is not None and cancel():
                raise InterruptedError("cancelled")
            base = start * hb
            data = data[max(lo - base, 0):hi - base]
            done += len(data)
            yield data
        if done != hi:
            raise ChdError("the CHD holds less data than its header says")

    def verify_raw_sha1(self, progress: Optional[Callable[[int, int], None]] = None,
                        cancel: Optional[Callable[[], bool]] = None) -> bool:
        """Decode every hunk (subcode included) and compare with the header's raw SHA-1."""
        h = hashlib.sha1()
        remaining = self.logical_bytes
        for i in range(self.hunk_count):
            if cancel is not None and cancel():
                raise InterruptedError("cancelled")
            data = self.read_hunk_raw(i, with_subcode=True)
            h.update(data[:remaining])
            remaining -= len(data)
            if progress:
                progress(i + 1, self.hunk_count)
        return h.hexdigest() == self.raw_sha1

    @property
    def reader_supports(self) -> bool:
        """True when every compression the CHD uses can be decoded by this reader (else it needs chdman)."""
        return all(c in READER_CODECS for c in self.compressors if c)

    def describe(self) -> dict:
        return {"version": self.version, "compressors": [c for c in self.compressors if c],
                "logical_bytes": self.logical_bytes, "hunk_bytes": self.hunk_bytes,
                "hunks": self.hunk_count, "sha1": self.sha1, "raw_sha1": self.raw_sha1,
                "kind": "gdrom" if self.is_gd else "cd" if self.is_cd else "dvd" if self.is_dvd else "data",
                "tracks": [t.to_dict() for t in self.tracks]}


def _strip_subcode(raw: bytes) -> bytes:
    n = len(raw) // CD_FRAME
    return b"".join(raw[i * CD_FRAME:i * CD_FRAME + CD_SECTOR] for i in range(n))


def open_chd(path) -> Chd:
    return Chd(path)


# --------------------------------------------------------------------------- hashing
@dataclass
class TrackHash:
    number: int
    type: str
    size: int
    crc32: str
    md5: str
    sha1: str
    audio: bool = False

    def to_dict(self) -> dict:
        return {"number": self.number, "type": self.type, "size": self.size, "crc32": self.crc32,
                "md5": self.md5, "sha1": self.sha1, "audio": self.audio}


def hash_track(chd: Chd, track: Track, progress: Optional[Callable[[int], None]] = None,
               cancel: Optional[Callable[[], bool]] = None) -> TrackHash:
    """crc32 + md5 + sha1 of one track's extracted bytes in a single pass."""
    crc = 0
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    size = 0
    for chunk in chd.iter_track(track, cancel=cancel):
        crc = zlib.crc32(chunk, crc)
        md5.update(chunk)
        sha1.update(chunk)
        size += len(chunk)
        if progress:
            progress(len(chunk))
    return TrackHash(track.number, track.type, size, "%08x" % (crc & 0xFFFFFFFF),
                     md5.hexdigest(), sha1.hexdigest(), track.is_audio)
