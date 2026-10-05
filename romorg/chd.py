"""Pure-Python CHD reader (stdlib only; C libraries are loaded, never required).

Reads every CHD that ``chdman`` reads: versions 1 to 5, all codecs (``zlib``, ``lzma``, ``zstd``, ``huff``, ``flac``,
the CD codecs ``cdlz`` / ``cdzl`` / ``cdfl`` / ``cdzs`` and the laserdisc codec ``avhu``), compressed and
uncompressed maps, and CHDs that need a parent file (found by its SHA-1 next to the child, or passed in). It exposes
the *tracks* of a CD / GD-ROM the way ``chdman extractcd`` writes them (raw 2352-byte sectors, subcode dropped,
GD-ROM pad frames dropped, audio in CD little-endian byte order), so the bytes can be hashed and compared with Redump;
any other CHD (hard disk, raw, laserdisc) is read as its logical bytes (:meth:`Chd.iter_raw`, what ``extracthd`` /
``extractraw`` write).

Track types: ``MODE1_RAW`` / ``MODE2_RAW`` / ``AUDIO`` are 2352-byte sectors; the cooked types
(``MODE1`` = 2048 bytes, ``MODE2_FORM1`` 2048, ``MODE2_FORM2`` 2324, ``MODE2`` 2336) are stored at the
START of each 2448-byte frame and extracted without padding / subcode (that is how ``chdman createcd`` keeps a
PlayStation 2 DVD ISO: a ``MODE1`` track, 2048 bytes per frame). DVD CHDs of ``chdman createdvd`` (metadata
``DVD ``, 2048-byte units, header raw SHA-1 = the ISO's SHA-1) are exposed as one synthetic ``DVD`` track.

Speed: FLAC goes through libFLAC and Zstandard through libzstd when they load (:mod:`romorg.flacnative`,
:mod:`romorg.zstdnative`); both have pure-Python fallbacks, so nothing is ever "unsupported" for want of a library.
Outside the scheduler's worker processes a reader decodes several hunks at once on threads (the C codecs release the
GIL): see :attr:`Chd.threads`.

Nothing is ever loaded as a whole: hunks are read, decoded and handed out one
at a time.

Codec notes (as implemented by libchdr / MAME):

``cdlz``  ``[ecc bitmap: ceil(frames/8)] [len of the LZMA part: 2 bytes]
          [raw LZMA1 of frames*2352 sector bytes] [raw deflate of frames*96 subcode]``;
          frames whose ECC bit is set had sync + P/Q parity removed
          (``cdecc.generate`` rebuilds them).
``cdzl``  the same with raw deflate for the sector part.
``cdfl``  ``[FLAC frames of frames*588 stereo samples] [raw deflate of the subcode]``.
``cdzs``  like ``cdlz`` with one Zstandard frame for the sectors and one for the subcode.
``flac``  ``['L' | 'B' = byte order of the samples] [FLAC frames of hunk/4 stereo samples]``.
``huff`` / ``avhu``  see :mod:`romorg.chdhuff`.

Old versions: v1 / v2 (hard disks: 8-byte map entries, zlib, MD5 only), v3 / v4 (16-byte map entries with the types
compressed / uncompressed / "mini" (an 8-byte pattern) / self / parent; zlib or A/V; CD track list as ``CHCD`` binary
or ``CHTR`` text metadata).

CD audio is stored big-endian inside hunks; the extraction swaps it back.
"""

from __future__ import annotations

import binascii
from array import array
import hashlib
import lzma
import os
import re
import struct
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Dict, Iterator, List, Optional, Tuple

from . import cdecc, chdhuff, flacnative, zstdnative
from .chdhuff import BitReader as _BitReader, Huffman as _HuffmanBase

__all__ = ["Chd", "ChdError", "ChdUnsupported", "Track", "TrackHash", "hash_track", "plan_chunks",
           "open_chd", "is_chd", "frames_to_bytes", "find_parent"]

CD_FRAME = 2448
CD_SECTOR = 2352
CD_SUBCODE = 96
CD_TRACK_PADDING = 4

# compression types of a v5 map entry
_T_NONE, _T_SELF, _T_PARENT, _T_RLE_SMALL, _T_RLE_LARGE = 4, 5, 6, 7, 8
_T_SELF0, _T_SELF1, _T_PARENT_SELF, _T_PARENT0, _T_PARENT1 = 9, 10, 11, 12, 13
# the reader's own types (never in a file): an 8-byte pattern repeated (v3 / v4 "mini"), a hunk of the parent by its
# number (v3 / v4), a byte offset into the parent (uncompressed v5), a hunk that was never written (zeros), unknown
_T_MINI, _T_PARENT_HUNK, _T_PARENT_BYTES, _T_ZERO, _T_BAD = 14, 15, 16, 17, 18
_PARENT_TYPES = (_T_PARENT, _T_PARENT_HUNK, _T_PARENT_BYTES)
ENV_THREADS = "ROMORG_CHD_THREADS"
MAX_THREADS = 4
THREAD_MIN_HUNK = 16384         # decode threads only pay off for hunks of this size (a CD hunk is 19584 bytes)


class ChdError(Exception):
    """Corrupt / unreadable CHD."""


class ChdUnsupported(ChdError):
    """A valid CHD that cannot be read as it is: its parent file is not there, or it uses something no chdman
    writes.

    ``needs_chdman``: a chdman might decode it. The reader has every codec chdman 0.289 has, so this is only set
    for a compression name it has never heard of (a later MAME); the callers then try the chdman they find."""

    needs_chdman = False


CD_CODECS = ("cdlz", "cdzl", "cdfl", "cdzs")      # CD hunks: sectors + ECC bitmap, subcode stored separately

def _unsupported_codec(codec: str) -> ChdUnsupported:
    exc = ChdUnsupported(f"needs chdman: the CHD uses the compression '{codec}', which the built-in reader does "
                         "not know (a format newer than this app?)")
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
    """CRC-16/CCITT as the CHD map uses it (init 0xFFFF, no final xor) = ``binascii.crc_hqx`` (C speed)."""
    return binascii.crc_hqx(data, crc)


def crc16_reference(data: bytes, crc: int = 0xFFFF) -> int:
    t = _CRC16
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ t[(crc >> 8) ^ b]
    return crc


# --------------------------------------------------------------------------- map decode
def _Huffman(br: _BitReader, numcodes: int = 16, maxbits: int = 8) -> _HuffmanBase:
    """The Huffman tree of a v5 compressed map (16 symbols, 8 bits, lengths run-length coded)."""
    h = _HuffmanBase(numcodes, maxbits)
    try:
        h.import_tree_rle(br)
    except chdhuff.HuffError as exc:
        raise ChdError("bad Huffman tree in the CHD map") from exc
    return h


# --------------------------------------------------------------------------- metadata
_TRACK_TAGS = {b"CHT2", b"CHTR", b"CHGD", b"CHGT"}
_DVD_TAG = b"DVD "
_HD_TAG = b"GDDD"
_LD_TAG = b"AVAV"
_OLD_CD_TAG = b"CHCD"
_OLD_CD_TYPES = ("MODE1", "MODE1_RAW", "MODE2", "MODE2_FORM1", "MODE2_FORM2", "MODE2_FORM_MIX", "MODE2_RAW", "AUDIO")
_OLD_CD_TRACKS = 99
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


def parse_old_cd_metadata(body: bytes) -> List[Track]:
    """The binary ``CHCD`` track list of early v3 CD CHDs: a track count and 99 records of six 32-bit numbers
    (type, subtype, sector size, subcode size, frames, pad frames) in the byte order of the machine that made it."""
    if len(body) < 4 + 24 * _OLD_CD_TRACKS:
        raise ChdError("old CD metadata is truncated")
    order = "<"
    count = struct.unpack("<I", body[:4])[0]
    if count > _OLD_CD_TRACKS:
        order = ">"
        count = struct.unpack(">I", body[:4])[0]
    if not 0 < count <= _OLD_CD_TRACKS:
        raise ChdError("bad old CD metadata")
    tracks = []
    for i in range(count):
        ttype, sub, _dsize, _ssize, frames, _extra = struct.unpack(order + "6I", body[4 + 24 * i:28 + 24 * i])
        if ttype >= len(_OLD_CD_TYPES):
            raise ChdError("bad old CD metadata")
        tracks.append(Track(number=i + 1, type=_OLD_CD_TYPES[ttype], subtype=("RW", "RW_RAW", "NONE")[min(sub, 2)],
                            frames=frames))
    return tracks


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


def _zstd_raw(data: bytes, size: int, codec: str) -> bytes:
    """One Zstandard frame of ``size`` bytes (libzstd when it loads, else the pure-Python decoder)."""
    try:
        return zstdnative.decompress(data, size)
    except zstdnative.ZstdError as exc:
        raise ChdError(f"corrupt Zstandard data: {exc}") from exc


def _lzma_raw(data: bytes, size: int, filters) -> bytes:
    d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filters)
    try:
        out = d.decompress(data, size)
    except lzma.LZMAError as exc:
        raise ChdError(f"corrupt LZMA data: {exc}") from exc
    return out


def _flac_raw(comp: bytes, size: int) -> bytes:
    """A ``flac`` hunk: ``L`` / ``B`` (the byte order of the 16-bit stereo samples) and FLAC frames."""
    order = comp[:1]
    if order not in (b"L", b"B") or size % 4:
        raise ChdError("corrupt FLAC hunk")
    try:
        pcm, _end = flacnative.decode_pcm(comp[1:], 0, size // 4, big_endian=order == b"B", need_end=False)
    except flacnative.FlacError as exc:
        raise ChdError(f"corrupt FLAC data: {exc}") from exc
    return bytes(pcm)


def _swap16(chunk) -> bytes:
    b = bytearray(chunk)
    b[0::2] = chunk[1::2]
    b[1::2] = chunk[0::2]
    return bytes(b)


# --------------------------------------------------------------------------- headers / parents
_ZERO20 = "0" * 40
_ZERO16 = "0" * 32


def _parse_header(f) -> dict:
    """The fields of a CHD header of any version (1 to 5) as a dict; raises :class:`ChdError`."""
    f.seek(0)
    h = f.read(124)
    if len(h) < 16 or h[:8] != b"MComprHD":
        raise ChdError("not a CHD file")
    length, version = struct.unpack(">II", h[8:16])
    want = {1: 76, 2: 80, 3: 120, 4: 108, 5: 124}.get(version)
    if want is None:
        raise ChdUnsupported(f"CHD version {version} is not supported (chdman reads versions 1 to 5)")
    if length != want or len(h) < want:
        raise ChdError(f"bad CHD v{version} header length")
    d = {"version": version, "length": length, "md5": "", "parent_md5": "", "sha1": "", "raw_sha1": "",
         "parent_sha1": "", "meta_offset": 0, "map_offset": length, "unit_bytes": 0}
    if version == 5:
        d["compressors"] = tuple(h[16 + 4 * i:20 + 4 * i].decode("latin-1")
                                 if h[16 + 4 * i:20 + 4 * i] != b"\0\0\0\0" else "" for i in range(4))
        (d["logical_bytes"], d["map_offset"], d["meta_offset"], d["hunk_bytes"],
         d["unit_bytes"]) = struct.unpack(">QQQII", h[32:64])
        d["raw_sha1"], d["sha1"], d["parent_sha1"] = h[64:84].hex(), h[84:104].hex(), h[104:124].hex()
        d["has_parent"] = d["parent_sha1"] != _ZERO20
        d["compressed"] = d["compressors"][0] != ""
        return d
    flags, compression = struct.unpack(">II", h[16:24])
    if compression > 3 or (version < 3 and compression > 1):
        raise ChdUnsupported(f"CHD v{version} with the unknown compression type {compression}")
    d["compressors"] = (("", "zlib", "zlib", "avhu")[compression], "", "", "")
    d["compressed"] = False                 # the map itself is never compressed before v5
    d["has_parent"] = bool(flags & 1)
    if version <= 2:
        hunk_sectors, d["hunk_count"], cyls, heads, secs = struct.unpack(">5I", h[24:44])
        seclen = struct.unpack(">I", h[76:80])[0] if version == 2 else 512
        d["md5"], d["parent_md5"] = h[44:60].hex(), h[60:76].hex()
        d["hunk_bytes"] = hunk_sectors * seclen
        d["unit_bytes"] = seclen
        d["logical_bytes"] = cyls * heads * secs * seclen
    elif version == 3:
        d["hunk_count"], d["logical_bytes"], d["meta_offset"] = struct.unpack(">IQQ", h[24:44])
        d["md5"], d["parent_md5"] = h[44:60].hex(), h[60:76].hex()
        d["hunk_bytes"] = struct.unpack(">I", h[76:80])[0]
        d["sha1"], d["parent_sha1"] = h[80:100].hex(), h[100:120].hex()
        d["raw_sha1"] = d["sha1"]           # v3: the header SHA-1 covers the data only
    else:
        d["hunk_count"], d["logical_bytes"], d["meta_offset"], d["hunk_bytes"] = struct.unpack(">IQQI", h[24:48])
        d["sha1"], d["parent_sha1"], d["raw_sha1"] = h[48:68].hex(), h[68:88].hex(), h[88:108].hex()
    return d


def find_parent(path, parent_sha1: str = "", parent_md5: str = "") -> Optional[str]:
    """The CHD in the folder of ``path`` whose header SHA-1 (MD5 for the oldest versions) is the one a child asks
    for, or None. Only headers are read."""
    want_sha = parent_sha1 if parent_sha1 and parent_sha1 != _ZERO20 else ""
    want_md5 = parent_md5 if parent_md5 and parent_md5 != _ZERO16 else ""
    if not (want_sha or want_md5):
        return None
    folder = os.path.dirname(os.path.abspath(str(path)))
    me = os.path.abspath(str(path))
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for name in names:
        if not name.lower().endswith(".chd"):
            continue
        cand = os.path.join(folder, name)
        if cand == me:
            continue
        try:
            with open(cand, "rb") as f:
                d = _parse_header(f)
        except (OSError, ChdError):
            continue
        if (want_sha and d["sha1"] == want_sha) or (not want_sha and want_md5 and d["md5"] == want_md5):
            return cand
    return None


# --------------------------------------------------------------------------- decode threads
_pool_lock = threading.Lock()
_pool: Optional[ThreadPoolExecutor] = None
_pool_size = 0


def default_threads() -> int:
    """Decode threads of a reader outside the scheduler: ``$ROMORG_CHD_THREADS``, else up to 4 (one per core)."""
    raw = os.environ.get(ENV_THREADS, "")
    if raw.strip().isdigit():
        return max(1, min(32, int(raw)))
    return max(1, min(MAX_THREADS, os.cpu_count() or 1))


def _executor(n: int) -> ThreadPoolExecutor:
    global _pool, _pool_size
    with _pool_lock:
        if _pool is None or _pool_size < n:
            old = _pool
            _pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="chd-decode")
            _pool_size = n
            if old is not None:
                old.shutdown(wait=False)
        return _pool


def _slices(items: list, parts: int) -> List[list]:
    """``items`` cut into at most ``parts`` runs of neighbours (a task per run keeps the hand-over cost low)."""
    per = max(1, -(-len(items) // parts))
    return [items[i:i + per] for i in range(0, len(items), per)]


class Chd:
    """An opened CHD file (version 1 to 5).  Use as a context manager or call :meth:`close`.

    ``parent``: the path (or an opened :class:`Chd`) of the parent when the file is a delta; without it the parent is
    looked up by its SHA-1 among the CHDs of the same folder the first time one of its hunks is needed."""

    #: decode threads; None = :func:`default_threads`. The scheduler's worker processes set 1 (they are the
    #: parallelism there).
    threads: Optional[int] = None

    def __init__(self, path, fileobj: Optional[BinaryIO] = None, load_map: bool = True, parent=None):
        self.path = str(path)
        self._f = fileobj if fileobj is not None else open(path, "rb")
        self._own = fileobj is None
        self._cache: Dict[int, bytes] = {}
        self._cache_le = False
        self._swapped: frozenset = frozenset()
        self._same: Dict[tuple, object] = {}        # the last few hunks other hunks are copies of
        self._filters = None
        self._parent: Optional["Chd"] = parent if isinstance(parent, Chd) else None
        self._parent_path = None if parent is None or isinstance(parent, Chd) else str(parent)
        self._own_parent = False
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
        if self._own_parent and self._parent is not None:
            self._parent.close()
            self._parent = None

    # -- header
    def _read_header(self) -> None:
        d = _parse_header(self._f)
        self.version = d["version"]
        self.compressors = d["compressors"]
        self.logical_bytes, self.map_offset, self.meta_offset = d["logical_bytes"], d["map_offset"], d["meta_offset"]
        self.hunk_bytes, self.unit_bytes = d["hunk_bytes"], d["unit_bytes"]
        self.raw_sha1, self.sha1, self.parent_sha1 = d["raw_sha1"], d["sha1"], d["parent_sha1"] or _ZERO20
        self.md5, self.parent_md5 = d["md5"], d["parent_md5"]
        self.has_parent = d["has_parent"]
        self.compressed = d["compressed"]
        if self.hunk_bytes == 0 or (self.version == 5 and self.unit_bytes == 0):
            raise ChdError("bad CHD geometry")
        self.hunk_count = (self.logical_bytes + self.hunk_bytes - 1) // self.hunk_bytes
        if self.version < 5:
            if d["hunk_count"] < self.hunk_count:
                raise ChdError("the CHD's hunk count does not cover its size")
            self.hunk_count = d["hunk_count"]

    # -- parent
    def _has_parent(self) -> bool:
        """The header names a parent, or the caller gave one (the only way for a parent without checksums)."""
        return self.has_parent or self._parent is not None or bool(self._parent_path)

    def _parent_chd(self) -> "Chd":
        if self._parent is None:
            found = self._parent_path or find_parent(self.path, self.parent_sha1, self.parent_md5)
            if not found:
                ident = self.parent_sha1 if self.parent_sha1 != _ZERO20 else self.parent_md5
                raise ChdUnsupported(f"this CHD is a delta of a parent CHD ({ident}) that is not in its folder")
            try:
                p = Chd(found)
            except OSError as exc:
                raise ChdError(f"cannot open the parent CHD {found}: {exc}") from exc
            ok = (p.sha1 == self.parent_sha1) if self.parent_sha1 != _ZERO20 else (
                not self.parent_md5.strip("0") or p.md5 == self.parent_md5)      # no checksum: nothing to compare
            if not ok:
                p.close()
                raise ChdError(f"{found} is not the parent of this CHD (its SHA-1 differs)")
            self._parent, self._own_parent = p, True
        return self._parent

    # -- map
    def _ensure_map(self) -> None:
        """Parse the hunk map on first use (header / metadata alone are enough to list the tracks)."""
        if not self._map_loaded:
            if self.version == 5:
                self._read_map()
            else:
                self._read_old_map()
            self._map_loaded = True

    def _read_old_map(self) -> None:
        """v1 / v2: 8 bytes per hunk (offset 44 bits, length 20 bits); v3 / v4: 16 bytes (offset, CRC-32, length 24
        bits, flags) with the entry types compressed / uncompressed / mini / self / parent."""
        f = self._f
        n = self.hunk_count
        hb = self.hunk_bytes
        size = 8 if self.version <= 2 else 16
        f.seek(self.map_offset)
        raw = f.read(size * n)
        if len(raw) < size * n:
            raise ChdError("map is truncated")
        types = bytearray(n)
        lens = array("I", bytes(4 * n))
        offs = array("Q", bytes(8 * n))
        if self.version <= 2:
            for i, (v,) in enumerate(struct.iter_unpack(">Q", raw)):
                ln = v >> 44
                offs[i] = v & 0xFFFFFFFFFFF
                if ln == hb or ln == 0:
                    types[i], lens[i] = _T_NONE, hb
                else:
                    types[i], lens[i] = 0, ln
        else:
            kinds = {1: 0, 2: _T_NONE, 3: _T_MINI, 4: _T_SELF, 5: _T_PARENT_HUNK}
            for i, (off, _crc, lo, hi, flags) in enumerate(struct.iter_unpack(">QIHBB", raw)):
                t = kinds.get(flags & 0x0F, _T_BAD)
                types[i] = t
                offs[i] = off
                lens[i] = hb if t == _T_NONE else (lo | (hi << 16)) if t == 0 else 0
        self._ctype, self._clen, self._coff, self._ccrc = array("B", bytes(types)), lens, offs, array("H", bytes(2 * n))

    def _read_map(self) -> None:
        f = self._f
        n = self.hunk_count
        if not self.compressed:
            f.seek(self.map_offset)
            raw = f.read(4 * n)
            if len(raw) < 4 * n:
                raise ChdError("map is truncated")
            offs = struct.unpack(">%dI" % n, raw)
            hb = self.hunk_bytes
            # offset 0 = the hunk was never written: it is the parent's when there is one, else zeros
            absent = _T_PARENT_BYTES if self._has_parent() else _T_ZERO
            self._ctype = array("B", [_T_NONE if o else absent for o in offs])
            self._clen = array("I", [hb if o else 0 for o in offs])
            self._coff = array("Q", [o * hb if o else i * hb for i, o in enumerate(offs)])
            self._ccrc = array("H", bytes(2 * n))
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
        decode = huff.decode_one
        types = [0] * n
        rep = 0
        last = 0
        for i in range(n):
            if rep > 0:
                types[i] = last
                rep -= 1
                continue
            val = decode(br)
            if val == _T_RLE_SMALL:
                types[i] = last
                rep = 2 + decode(br)
            elif val == _T_RLE_LARGE:
                types[i] = last
                rep = 2 + 16 + (decode(br) << 4)
                rep += decode(br)
            else:
                types[i] = last = val
        if br.overflow:
            raise ChdError("compressed map is truncated")
        lens = [0] * n
        offs = [0] * n
        crcs = [0] * n
        cur = firstoffs
        lastself = 0
        lastparent = 0
        pack = struct.Struct(">BBHHIH").pack
        rawmap = bytearray()
        # the fixed-width fields are read with one 64-bit window each (length + crc <= 56 bits); the stream is padded
        padded = data + b"\0" * 8
        window = struct.Struct(">Q").unpack_from
        pos = br.pos
        fast_bits = lengthbits + 16
        if fast_bits > 56 or selfbits > 48 or parentbits > 48:
            raise ChdError("unsupported CHD map field widths")
        fast_mask = (1 << fast_bits) - 1
        total_bits = len(data) * 8
        hunk_units = self.hunk_bytes // self.unit_bytes
        for i in range(n):
            t = types[i]
            off = cur
            ln = 0
            crc = 0
            if t <= 3 or t == _T_NONE:
                if pos > total_bits:
                    raise ChdError("compressed map is truncated")
                if t <= 3:
                    v = (window(padded, pos >> 3)[0] >> (64 - (pos & 7) - fast_bits)) & fast_mask
                    pos += fast_bits
                    ln = v >> 16
                    crc = v & 0xFFFF
                else:
                    ln = self.hunk_bytes
                    crc = (window(padded, pos >> 3)[0] >> (64 - (pos & 7) - 16)) & 0xFFFF
                    pos += 16
                cur += ln
            elif t == _T_SELF:
                if pos > total_bits:
                    raise ChdError("compressed map is truncated")
                off = (window(padded, pos >> 3)[0] >> (64 - (pos & 7) - selfbits)) & ((1 << selfbits) - 1)
                pos += selfbits
                lastself = off
            elif t == _T_PARENT:
                if pos > total_bits:
                    raise ChdError("compressed map is truncated")
                off = (window(padded, pos >> 3)[0] >> (64 - (pos & 7) - parentbits)) & ((1 << parentbits) - 1)
                pos += parentbits
                lastparent = off
            elif t in (_T_SELF0, _T_SELF1):
                if t == _T_SELF1:
                    lastself += 1
                off = lastself
                t = _T_SELF
            elif t == _T_PARENT_SELF:
                off = lastparent = i * hunk_units
                t = _T_PARENT
            elif t in (_T_PARENT0, _T_PARENT1):
                if t == _T_PARENT1:
                    lastparent += hunk_units
                off = lastparent
                t = _T_PARENT
            else:
                raise ChdError("bad compression type in the CHD map")
            types[i] = t
            lens[i] = ln
            offs[i] = off
            crcs[i] = crc
            rawmap += pack(t, ln >> 16, ln & 0xFFFF, off >> 32, off & 0xFFFFFFFF, crc)
        if pos > total_bits:
            raise ChdError("compressed map is truncated")
        if crc16(bytes(rawmap)) != mapcrc:
            raise ChdError("CHD map CRC mismatch (file damaged?)")
        # compact arrays: a 6 GB image has 520,000 hunks and every worker process keeps the map of the CHDs it works on
        self._ctype, self._clen, self._coff, self._ccrc = (array("B", types), array("I", lens), array("Q", offs),
                                                           array("H", crcs))

    # -- metadata
    def _read_metadata(self) -> None:
        f = self._f
        self.metadata: List[Tuple[bytes, bytes]] = []
        self._meta_flags: List[int] = []
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
            self._meta_flags.append(m[4])
            off = nxt
        tracks: List[Track] = []
        for tag, body in self.metadata:
            if tag in _TRACK_TAGS:
                text = body.split(b"\0", 1)[0].decode("ascii", "replace")
                tracks.append(parse_track_metadata(tag, text))
            elif tag == _OLD_CD_TAG and not tracks:
                tracks = parse_old_cd_metadata(body)
        tracks.sort(key=lambda t: t.number)
        start = 0
        for t in tracks:
            t.start = start
            start += t.frames + (-t.frames) % CD_TRACK_PADDING
        self.is_gd = any(t.gd for t in tracks)
        self.is_cd = bool(tracks)
        self.is_hd = False
        if self.version < 5 and not self.unit_bytes:
            # v3 / v4 headers carry no unit size: 2448 for a CD, the sector size of a hard disk, else the hunk
            self.unit_bytes = CD_FRAME if tracks else self.hunk_bytes
        for tag, body in self.metadata:
            if tag == _HD_TAG:
                self.is_hd = True
                m = re.search(rb"BPS:(\d+)", body)
                if m and self.version in (3, 4) and int(m.group(1)) and not tracks:
                    self.unit_bytes = int(m.group(1))
        if self.version <= 2:
            self.is_hd = True
        self.is_ld = any(tag == _LD_TAG for tag, _b in self.metadata)
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
        if self.is_cd and (self.unit_bytes != CD_FRAME or self.hunk_bytes % CD_FRAME):
            raise ChdError("CD metadata but unit size is not 2448")

    # -- hunks
    def _codec_for(self, ctype: int) -> str:
        tag = self.compressors[ctype]
        if not tag:
            raise ChdError("hunk uses an undefined compressor slot")
        return tag

    def _fetch(self, index: int):
        """Everything of hunk ``index`` that needs the file: ``(codec, compressed bytes, ref)``, or ``(None, hunk,
        ref)`` when the bytes are already the hunk (stored, a pattern, zeros, or taken from the parent).
        Self-references are followed: ``ref`` is then the hunk the data really belongs to (else -1), so that a
        run of identical hunks is decoded once. Not thread safe (one file position)."""
        self._ensure_map()
        if index < 0 or index >= self.hunk_count:
            raise ChdError("hunk index out of range")
        ctype = self._ctype[index]
        hops = 0
        while ctype == _T_SELF:
            index = self._coff[index]
            hops += 1
            if index >= self.hunk_count or hops > 64:
                raise ChdError("bad self-reference in the CHD map")
            ctype = self._ctype[index]
        ref = index if hops else -1
        hb = self.hunk_bytes
        off = self._coff[index]
        if ctype <= 3 or ctype == _T_NONE:
            f = self._f
            f.seek(off)
            comp = f.read(self._clen[index])
            if len(comp) < self._clen[index]:
                raise ChdError("file is truncated (hunk %d)" % index)
            return (None, comp, ref) if ctype == _T_NONE else (self._codec_for(ctype), comp, ref)
        if ctype == _T_MINI:
            return None, (off.to_bytes(8, "big") * (hb // 8 + 1))[:hb], ref
        if ctype == _T_ZERO:
            return None, bytes(hb), ref
        if ctype in _PARENT_TYPES:
            if not self._has_parent():
                # chdman writes this for a child of a parent without checksums (an uncompressed CHD)
                raise ChdUnsupported("this CHD is a delta of a parent CHD that has no checksum, so the parent "
                                     "cannot be found by itself")
            p = self._parent_chd()
            if ctype == _T_PARENT_HUNK:
                raw = p.read_hunk_raw(off)
            else:
                raw = p.read_bytes(off * p.unit_bytes if ctype == _T_PARENT else off, hb, pad=True)
            if len(raw) != hb:
                raise ChdError("the parent CHD has another hunk size")
            return None, raw, ref
        raise ChdUnsupported("unsupported hunk type in the CHD map")

    def _decode(self, codec: Optional[str], comp: bytes, with_subcode: bool = True) -> bytes:
        """The hunk of ``_fetch``'s result. Pure computation: safe on several threads at once."""
        strip = not with_subcode and self.is_cd
        if codec is None:
            return _strip_subcode(comp) if strip else comp
        if codec in CD_CODECS:
            return self._decode_cd(codec, comp, with_subcode)
        hb = self.hunk_bytes
        try:
            if codec == "zlib":
                raw = _inflate_raw(comp, hb)
            elif codec == "lzma":
                if self._filters is None:
                    self._filters = _lzma_filters(hb)
                raw = _lzma_raw(comp, hb, self._filters)
            elif codec == "zstd":
                raw = _zstd_raw(comp, hb, codec)
            elif codec == "flac":
                raw = _flac_raw(comp, hb)
            elif codec == "huff":
                raw = chdhuff.huff_decode(comp, hb)
            elif codec == "avhu":
                raw = chdhuff.avhuff_decode(comp, hb)
            else:
                raise _unsupported_codec(codec)
        except chdhuff.HuffError as exc:
            raise ChdError(f"corrupt {codec} data: {exc}") from exc
        if len(raw) != hb:
            raise ChdError("a hunk decoded to the wrong size")
        return _strip_subcode(raw) if strip else raw

    def read_hunk_raw(self, index: int, with_subcode: bool = True) -> bytes:
        """The hunk as stored in the CHD's logical stream (frames of 2448 for a CD).

        With ``with_subcode=False`` a CD hunk comes back as bare 2352-byte sectors
        (cheaper: the subcode is neither inflated nor copied).
        """
        return self.read_hunks(index, 1, with_subcode)[0]

    def _threads(self) -> int:
        """Decode threads for this file: none for small hunks (the hand-over costs more than their decoding)."""
        if self.hunk_bytes < THREAD_MIN_HUNK:
            return 1
        n = self.threads
        return default_threads() if n is None else max(1, n)

    def _remember(self, key, data) -> None:
        cache = self._same
        if len(cache) >= 8:
            cache.pop(next(iter(cache)))
        cache[key] = data

    def read_hunks(self, first: int, count: int, with_subcode: bool = True) -> List[bytes]:
        """``count`` hunks from ``first``. The file is read here; the decoding is spread over the decode threads.
        Hunks that are copies of another one (self-references) are decoded once."""
        jobs = [self._fetch(i) for i in range(first, first + count)]
        out: List[Optional[bytes]] = [None] * len(jobs)
        same = self._same
        groups: Dict[object, List[int]] = {}
        for k, (codec, comp, ref) in enumerate(jobs):
            if codec is None:
                out[k] = self._decode(None, comp, with_subcode)
            elif ref >= 0 and (ref, with_subcode) in same:
                out[k] = same[(ref, with_subcode)]
            else:
                groups.setdefault(ref if ref >= 0 else -1 - k, []).append(k)
        todo = [ks[0] for ks in groups.values()]
        threads = self._threads()
        if threads > 1 and len(todo) > 1:
            def run(part):
                return [self._decode(jobs[k][0], jobs[k][1], with_subcode) for k in part]
            done = [x for part in _executor(threads).map(run, _slices(todo, threads)) for x in part]
        else:
            done = [self._decode(jobs[k][0], jobs[k][1], with_subcode) for k in todo]
        for (key, ks), data in zip(groups.items(), done):
            for k in ks:
                out[k] = data
            if isinstance(key, int) and key >= 0:
                self._remember((key, with_subcode), data)
        return out          # type: ignore[return-value]

    def read_bytes(self, offset: int, length: int, pad: bool = False) -> bytes:
        """``length`` logical bytes from ``offset`` (subcode included for a CD). ``pad``: bytes past the end of
        the data read as zeros (a child that is larger than its parent)."""
        if offset < 0 or length < 0:
            raise ChdError("byte range out of bounds")
        hb = self.hunk_bytes
        end = offset + length
        limit = self.hunk_count * hb
        if end > limit and not pad:
            raise ChdError("byte range out of bounds")
        stop = min(end, limit)
        if stop <= offset:
            return bytes(length)
        first = offset // hb
        last = (stop - 1) // hb
        if first == last:
            data = self.read_hunk_raw(first)
        else:
            data = b"".join(self.read_hunks(first, last - first + 1))
        lo = offset - first * hb
        out = data[lo:lo + (stop - offset)] if (lo or stop - offset != len(data)) else data
        return out + bytes(end - stop) if end > stop else out

    def _cd_base(self, codec: str, comp: bytes, need_end: bool = True, audio_le: bool = False):
        """Decode the sector part of a CD hunk: ``(base, pending, end)``.

        ``pending`` lists the sectors whose sync + P/Q parity still has to be
        regenerated (``cdecc.generate``); ``end`` is where the subcode starts. ``audio_le``: FLAC audio comes back
        in CD (little-endian) byte order instead of the big-endian order hunks are stored in.
        """
        frames = self.hunk_bytes // CD_FRAME
        nbytes = frames * CD_SECTOR
        pending: list = []
        if codec == "cdfl":
            # libFLAC through ctypes when it loads (100x faster), else the pure-Python decoder; hunks hold CD audio
            # big-endian
            try:
                base, end = flacnative.decode_pcm(comp, 0, frames * (CD_SECTOR // 4), big_endian=not audio_le,
                                                 need_end=need_end)
            except flacnative.FlacError as exc:
                raise ChdError(f"corrupt FLAC audio: {exc}") from exc
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
            elif codec == "cdzs":
                base = bytearray(_zstd_raw(body, nbytes, codec))
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
        base, pending, end = self._cd_base(codec, comp, need_end=with_subcode)
        cdecc.generate(pending)
        if not with_subcode:
            return bytes(base)
        # the subcode follows: deflate for cdlz / cdzl / cdfl, Zstandard for cdzs
        if codec == "cdzs":
            sub = _zstd_raw(comp[end:], frames * CD_SUBCODE, codec)
        else:
            sub = _inflate_raw(comp[end:], frames * CD_SUBCODE)
        if len(sub) != frames * CD_SUBCODE:
            raise ChdError("CD subcode has the wrong size")
        out = bytearray(frames * CD_FRAME)
        for fr in range(frames):
            out[fr * CD_FRAME:fr * CD_FRAME + CD_SECTOR] = base[fr * CD_SECTOR:(fr + 1) * CD_SECTOR]
            out[fr * CD_FRAME + CD_SECTOR:(fr + 1) * CD_FRAME] = sub[fr * CD_SUBCODE:(fr + 1) * CD_SUBCODE]
        return bytes(out)

    GROUP = 16          # hunks decoded together so the ECC work is batched

    def _hunk_sectors(self, index: int, audio_le: bool = False) -> bytes:
        """The 2352-byte sectors of hunk ``index`` (its whole group is decoded and kept). ``audio_le``: FLAC hunks
        are decoded straight to CD byte order; :attr:`_swapped` then names them."""
        got = self._cache.get(index)
        if got is not None and self._cache_le == audio_le:
            return got
        self._cache = {}
        threads = self._threads()
        # with decode threads a group per thread is read at once: each thread gets a run worth its hand-over
        last = min(self.hunk_count, index + self.GROUP * threads)
        jobs = [self._fetch(i) for i in range(index, last)]
        same = self._same
        ready: Dict[int, tuple] = {}        # copies of a hunk decoded a moment ago (runs of silence, of zeros)
        for k, (codec, _c, ref) in enumerate(jobs):
            if codec in CD_CODECS and ref >= 0 and (ref, "sectors", audio_le) in same:
                ready[k] = same[(ref, "sectors", audio_le)]
        cd = [k for k, (codec, _c, _r) in enumerate(jobs) if codec in CD_CODECS and k not in ready]

        def run(part):
            return [self._cd_base(jobs[k][0], jobs[k][1], False, audio_le) for k in part]
        if threads > 1 and len(cd) > 1:
            decoded = [x for part in _executor(threads).map(run, _slices(cd, threads)) for x in part]
        else:
            decoded = run(cd)
        bases: Dict[int, object] = {}
        pending: list = []
        for k, (base, pend, _end) in zip(cd, decoded):
            bases[k] = base
            pending.extend(pend)
        cdecc.generate(pending)
        cache: Dict[int, bytes] = {}
        swapped = set()
        for k, (codec, comp, ref) in enumerate(jobs):
            if k in ready:
                data, was_swapped = ready[k]
            elif k in bases:
                data, was_swapped = bytes(bases[k]), audio_le and codec == "cdfl"
                if ref >= 0:
                    self._remember((ref, "sectors", audio_le), (data, was_swapped))
            else:
                data, was_swapped = self._decode(codec, comp, False), False
            cache[index + k] = data
            if was_swapped:
                swapped.add(index + k)
        self._cache = cache
        self._cache_le = audio_le
        self._swapped = frozenset(swapped)
        return self._cache[index]

    # -- tracks
    def _frames(self, first_frame: int, count: int, audio_le: bool = False) -> Iterator[Tuple[bytes, bool]]:
        """``(sectors, swapped)`` runs of ``count`` frames from CHD frame ``first_frame``; ``swapped`` = the run
        is FLAC audio already in CD byte order (only with ``audio_le``)."""
        if not self.is_cd:
            raise ChdError("not a CD / GD-ROM CHD")
        fph = self.frames_per_hunk
        f = first_frame
        end = first_frame + count
        while f < end:
            h, off = divmod(f, fph)
            data = self._hunk_sectors(h, audio_le)
            take = min(end - f, fph - off)
            yield data[off * CD_SECTOR:(off + take) * CD_SECTOR], h in self._swapped
            f += take

    def iter_frames(self, first_frame: int, count: int) -> Iterator[bytes]:
        """Raw 2352-byte sectors (no subcode) of ``count`` frames from CHD frame ``first_frame``."""
        for chunk, _swapped in self._frames(first_frame, count):
            yield chunk

    def _extracted(self, track: Track, chunk: bytes, swapped: bool = False) -> bytes:
        """Whole sectors of ``chunk`` (raw 2352-byte sectors of one track) as ``chdman extractcd`` writes them."""
        ssize, soff = track.sector_size, track.sector_offset
        if track.is_audio:
            return chunk if swapped else _swap16(chunk)
        if swapped:
            chunk = _swap16(chunk)
        if ssize == CD_SECTOR:
            return chunk
        n = len(chunk) // CD_SECTOR
        return b"".join(chunk[i * CD_SECTOR + soff:i * CD_SECTOR + soff + ssize] for i in range(n))

    def iter_track(self, track: Track, cancel: Optional[Callable[[], bool]] = None,
                   skip_audio: bool = False) -> Iterator[bytes]:
        """The extracted bytes of a track, as ``chdman extractcd`` writes its bin / gdi file (a DVD CHD: the ISO,
        as ``chdman extractdvd`` writes it)."""
        if self.is_dvd:
            yield from self.iter_raw(cancel)
            return
        for chunk, swapped in self._frames(track.start, track.data_frames, track.is_audio):
            if cancel is not None and cancel():
                raise InterruptedError("cancelled")
            yield self._extracted(track, chunk, swapped)

    def read_track_range(self, track: Track, first: int, count: int) -> bytes:
        """``count`` frames of the extracted track bytes from frame ``first`` of the track (a DVD CHD: 2048-byte
        units of the ISO) - what the parallel scheduler hands to a worker."""
        if first < 0 or count < 0 or first + count > track.data_frames:
            raise ChdError("track range out of bounds")
        if self.is_dvd:
            return self._read_dvd_units(first, count)
        return b"".join(self._extracted(track, c, sw)
                        for c, sw in self._frames(track.start + first, count, track.is_audio))

    def _read_dvd_units(self, first: int, count: int) -> bytes:
        start = first * DVD_SECTOR
        end = min(self.logical_bytes, (first + count) * DVD_SECTOR)
        if end <= start:
            return b""
        out = self.read_bytes(start, end - start)
        if len(out) != end - start:
            raise ChdError("the CHD holds less data than its header says")
        return out

    def iter_raw(self, cancel: Optional[Callable[[], bool]] = None, with_subcode: bool = True) -> Iterator[bytes]:
        """The logical bytes of the CHD in order - what ``chdman extractraw`` / ``extracthd`` / ``extractdvd``
        write (for a CD: the 2448-byte frames, subcode included)."""
        remaining = self.logical_bytes
        # decoded about a megabyte of hunks at a time: small hunks (2-4 KiB) do not cost one iteration each, and
        # the decode threads get a batch to share
        per = max(self._threads(), (1 << 20) // self.hunk_bytes, 1)
        i = 0
        n = self.hunk_count
        while remaining > 0 and i < n:
            if cancel is not None and cancel():
                raise InterruptedError("cancelled")
            take = min(per, n - i)
            data = b"".join(self.read_hunks(i, take, with_subcode))
            i += take
            if len(data) > remaining:
                data = data[:remaining]
            remaining -= len(data)
            yield data
        if remaining:
            raise ChdError("the CHD holds less data than its header says")

    def _iter_dvd(self, cancel: Optional[Callable[[], bool]] = None) -> Iterator[bytes]:
        return self.iter_raw(cancel)

    # -- verification (chdman verify)
    def overall_sha1(self, raw_digest: bytes) -> str:
        """The header SHA-1 that belongs to the data SHA-1 ``raw_digest``: data + the hashes of the metadata
        entries flagged for it, sorted (v4 / v5); before v4 it is the data SHA-1 itself."""
        if self.version < 4:
            return raw_digest.hex()
        h = hashlib.sha1(raw_digest)
        for item in sorted(tag + hashlib.sha1(body).digest()
                           for (tag, body), flags in zip(self.metadata, self._meta_flags) if flags & 1):
            h.update(item)
        return h.hexdigest()

    def verify(self, progress: Optional[Callable[[int, int], None]] = None,
               cancel: Optional[Callable[[], bool]] = None) -> dict:
        """Decode everything and compare with the header, like ``chdman verify``: ``{"raw": bool, "overall": bool,
        "raw_sha1": the data's SHA-1 (MD5 for v1 / v2)}``. A CHD without checksums (chdman leaves them out of
        uncompressed files) gives ``None`` for both: there is nothing to compare with."""
        md5_only = not self.raw_sha1
        h = hashlib.md5() if md5_only else hashlib.sha1()
        done = 0
        for data in self.iter_raw(cancel):
            h.update(data)
            done += len(data)
            if progress:
                progress(min(self.hunk_count, done // self.hunk_bytes), self.hunk_count)
        if md5_only:
            ok = h.hexdigest() == self.md5 if self.md5.strip("0") else None
            return {"raw": ok, "overall": ok, "raw_sha1": h.hexdigest()}
        if self.raw_sha1 == _ZERO20:
            return {"raw": None, "overall": None, "raw_sha1": h.hexdigest()}
        raw_ok = h.hexdigest() == self.raw_sha1
        return {"raw": raw_ok, "overall": self.overall_sha1(h.digest()) == self.sha1, "raw_sha1": h.hexdigest()}

    def verify_raw_sha1(self, progress: Optional[Callable[[int, int], None]] = None,
                        cancel: Optional[Callable[[], bool]] = None) -> bool:
        """Decode every hunk (subcode included) and compare with the header's raw SHA-1."""
        return bool(self.verify(progress, cancel)["raw"])

    def describe(self) -> dict:
        return {"version": self.version, "compressors": [c for c in self.compressors if c],
                "logical_bytes": self.logical_bytes, "hunk_bytes": self.hunk_bytes,
                "hunks": self.hunk_count, "sha1": self.sha1, "raw_sha1": self.raw_sha1,
                "parent_sha1": self.parent_sha1 if self.has_parent else "",
                "kind": ("gdrom" if self.is_gd else "cd" if self.is_cd else "dvd" if self.is_dvd
                         else "hd" if self.is_hd else "laserdisc" if self.is_ld else "data"),
                "tracks": [t.to_dict() for t in self.tracks]}


def plan_chunks(chd: "Chd", track: Track, target_bytes: int) -> List[Tuple[int, int]]:
    """Split a track into ``(first_frame, frames)`` ranges of about ``target_bytes`` of extracted data each.

    The boundaries sit on hunk (and ECC-group) boundaries of the CHD so that no hunk is decoded twice except at a
    track edge; ranges are in track frames (a DVD CHD: 2048-byte units)."""
    total = track.data_frames
    if total <= 0:
        return []
    fph = max(1, chd.frames_per_hunk if not chd.is_dvd else chd.hunk_bytes // DVD_SECTOR)
    per_hunk = fph * max(1, track.sector_size)
    hunks = max(1, target_bytes // per_hunk)
    if hunks >= Chd.GROUP:
        hunks -= hunks % Chd.GROUP
    align = hunks * fph
    base = 0 if chd.is_dvd else track.start
    out: List[Tuple[int, int]] = []
    pos = 0
    while pos < total:
        edge = ((base + pos) // align + 1) * align - base
        n = min(edge, total) - pos
        out.append((pos, n))
        pos += n
    return out


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
