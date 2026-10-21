"""RawDIC in Python: runs a WHDLoad install's own imager (``.islave``, 68000 code) on a game's original disks - read from IPF files
(``ipf``) or, for disks in the standard format, from ADFs - and returns the files it writes.

It follows RawDIC's source (github.com/wepl/RawDIC, public domain) step by step: the tracks are read as raw MFM (about two and a
half turns of the disk), the sync word is searched bit by bit and the rest shifted to line up behind it, the imager's track decoders
are called with A0 = MFM, A1 = track buffer, A5 = the RawDIC library, then its disk code, then the files of its file list are saved
from the disk image (the tracks of the track list, one after another).

    files = Imager(islave_bytes).run({1: AdfDisk(adf1), 2: IpfDisk(ipf2)})     # {"Disk.1": b"...", ...}
"""

from __future__ import annotations

import re
import struct
from typing import Callable, Optional, Protocol

from .m68k import Cpu, CpuError

__all__ = ["AdfDisk", "Imager", "ImagerError", "dos_sectors", "dos_track_mfm", "is_imager", "load_hunks", "mfm_encode",
           "standard_image"]


class ImagerError(Exception):
    """The imager stopped: the disk is not the one it reads, it failed, or it does something this RawDIC does not."""


class Disk(Protocol):
    def track(self, n: int) -> Optional[tuple[bytes, int]]:
        """One turn of the disk's track ``n`` (cylinder * 2 + side) as MFM: (bytes, number of bits), or None (no track)."""

    def dos(self, n: int) -> Optional[bytes]:
        """The track's 11 standard sectors when the disk knows them directly (an ADF), else None."""


# --- RawDIC's numbers (RawDIC.i)
IERR_OK, IERR_CHECKSUM, IERR_NOSYNC, IERR_NOSECTOR, IERR_NOWFILE = 0, -1, -2, -3, -4
IERR_DISKRANGE, IERR_NOTRACK, IERR_UNDEFINED, IERR_NODISK, IERR_CRCFAIL = -6, -7, -8, -9, -10
IERR_TRACKLIST, IERR_DSKVERSION, IERR_FLAGS, IERR_NOFUNCTION, IERR_SLAVEVER = -11, -14, -15, -16, -37
_ERRORS = {-1: "a checksum is wrong", -2: "no sync (the track is not in the format the imager reads)", -3: "a sector is missing",
           -4: "a file could not be written", -5: "out of memory", -6: "a file reaches past the end of the disk",
           -7: "a track is not in the track list", -8: "the imager's own error", -9: "no disk", -10: "a track's checksum (CRC) is not "
           "one the imager knows: a different version of the disk", -11: "the track list is invalid", -12: "the file list is invalid",
           -13: "the imager needs a newer RawDIC", -14: "the disk structure has an unknown version", -15: "unknown flags",
           -16: "a RawDIC call made where it may not be", -37: "the imager needs a newer RawDIC"}
DFLG_SINGLESIDE, DFLG_SWAPSIDES, DFLG_ERRORSWAP, DFLG_ERRORS, DFLG_RAWREADONLY, DFLG_NORESTRICTIONS = 1, 2, 4, 8, 16, 32
DFLG_DOUBLEINC2 = 64
SYNC_INDEX, SYNC_STD = 0, 0x4489
DMFM_NULL, DMFM_STD = 0, 1
VERSION = 6                                                  # the RawDIC version this follows
RAWLEN = 0x7C04                                              # bytes of a raw read (xx_RawLength)
_DECODER_STEPS = 40_000_000                                  # instructions a track decoder may take (Hybris': 37 million)

# --- where things are in the 68000's memory
_LIB = 0x1000                                                # the library's jump table (12 entries of 4 bytes)
_EXEC = 0x3000                                               # exec.library base (its calls are below it)
_DOS = 0x5000                                                # dos.library base
_MFM = 0x8000                                                # MFM buffer (4 sync words just before it)
_TRACK = 0x10000                                             # track buffer (64 KB)
_SLAVE = 0x20000                                             # the imager's hunks
_HEAP = 0x400000                                             # AllocMem
_IMAGE = 0x800000                                            # the disk image
_STACK = 0xFF0000


def _sx16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _sx32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


# ---------------------------------------------------------------------- hunk files
def load_hunks(data: bytes, cpu: Cpu, base: int, seglist: bool = False) -> list[int]:
    """Load an AmigaDOS executable into the 68000's memory from ``base`` up, relocated; returns the address of each hunk.
    ``seglist``: as LoadSeg lays it out - each hunk after a longword of its size and one BPTR to the next hunk's (the first's
    BPTR is ``(addrs[0] - 4) >> 2``)."""
    u32 = lambda o: struct.unpack_from(">I", data, o)[0]
    if len(data) < 24 or u32(0) != 0x3F3:
        raise ImagerError("not an Amiga program (no hunk header)")
    o = 4
    while u32(o):                                            # resident library names
        o += 4 + 4 * u32(o)
    o += 4
    first, last = u32(o + 4), u32(o + 8)
    o += 12
    sizes = []
    for _ in range(last - first + 1):
        v = u32(o); o += 4
        if v >> 30 == 3:
            o += 4
        sizes.append((v & 0x3FFFFFFF) * 4)
    addrs, at = [], base
    for s in sizes:
        if seglist:
            at += 8
        addrs.append(at)
        at = (at + s + 0x100 + 15) & ~15
    if seglist:
        for i, a in enumerate(addrs):
            cpu.w32(a - 8, sizes[i] + 8)
            cpu.w32(a - 4, ((addrs[i + 1] - 4) >> 2) if i + 1 < len(addrs) else 0)
    cur = -1
    pending: list[tuple[int, int, int]] = []                 # (hunk, offset, target hunk)
    while o + 4 <= len(data):
        t = u32(o) & 0x3FFFFFFF
        o += 4
        if t in (0x3E9, 0x3EA):                              # CODE, DATA
            n = u32(o) * 4; o += 4
            cur += 1
            cpu.load(addrs[cur], data[o:o + n])
            o += n
        elif t == 0x3EB:                                     # BSS
            o += 4
            cur += 1
        elif t in (0x3EC, 0x3F8):                            # RELOC32 (and its 0x3F8 twin)
            while u32(o):
                c, tgt = u32(o), u32(o + 4)
                pending += [(cur, u32(o + 8 + 4 * i), tgt) for i in range(c)]
                o += 8 + 4 * c
            o += 4
        elif t in (0x3F7, 0x3FC):                            # DREL32 / RELOC32SHORT: 16-bit counts and offsets
            while True:
                c = struct.unpack_from(">H", data, o)[0]; o += 2
                if c == 0:
                    break
                tgt = struct.unpack_from(">H", data, o)[0]; o += 2
                pending += [(cur, struct.unpack_from(">H", data, o + 2 * i)[0], tgt) for i in range(c)]
                o += 2 * c
            o = (o + 3) & ~3
        elif t == 0x3F2:                                     # END
            pass
        elif t == 0x3F0:                                     # SYMBOL
            while u32(o):
                o += 4 + 4 * u32(o) + 4
            o += 4
        elif t in (0x3F1, 0x3E7, 0x3E8):                     # DEBUG, UNIT, NAME
            o += 4 + 4 * u32(o)
        else:
            raise ImagerError(f"unknown hunk type ${t:x}")
    for h, off, tgt in pending:
        a = addrs[h] + off
        cpu.w32(a, cpu.r32(a) + addrs[tgt])
    return addrs


def is_imager(data: bytes) -> bool:
    try:
        cpu = Cpu(1 << 24)
        a = load_hunks(data, cpu, _SLAVE)[0]
    except (ImagerError, struct.error, IndexError):
        return False
    return bytes(cpu.m[a + 4:a + 10]) == b"RAWDIC"


# ---------------------------------------------------------------------- MFM
_CLOCKS: dict[int, int] = {}


def mfm_encode(words: list[tuple[int, bool]], prev: int = 0) -> bytes:
    """MFM for a run of 16-bit words: (value, raw). A raw word (a sync) goes on the disk as it is; the others' clock bits (the odd
    bits) are made from their data bits (the even ones, as in the Amiga's odd/even longs)."""
    n = len(words)
    s = 0
    raw = 0
    for v, r in words:
        s = (s << 16) | (v & 0xFFFF if r else v & 0x5555)
        raw = (raw << 16) | (0xFFFF if r else 0)
    bits = 16 * n
    cm = _CLOCKS.get(bits)
    if cm is None:
        cm = _CLOCKS[bits] = int.from_bytes(b"\xaa" * (2 * n), "big")
    full = (1 << bits) - 1
    data = s & ~cm & full                                    # every word's data bits (a sync's too: its neighbours' clocks need them)
    above = (data >> 1) | (prev << (bits - 1) if prev else 0)  # the data bit before each clock (a higher bit: earlier on the disk)
    below = data << 1
    clocks = ~(above | below) & cm & ~raw & full
    return (((s & ~raw) | clocks | (s & raw)) & full).to_bytes(2 * n, "big")


def _oddeven(data: bytes) -> list[tuple[int, bool]]:
    """Amiga odd/even MFM data: first the odd bits of every long, then the even ones, as 16-bit words."""
    out = []
    longs = [int.from_bytes(data[i:i + 4], "big") for i in range(0, len(data), 4)]
    for part in ([(v >> 1) & 0x55555555 for v in longs], [v & 0x55555555 for v in longs]):
        for v in part:
            out += [(v >> 16, False), (v & 0xFFFF, False)]
    return out


def _csum(words: list[tuple[int, bool]]) -> int:
    c = 0
    for i in range(0, len(words), 2):
        c ^= (words[i][0] << 16) | words[i + 1][0]
    return c & 0x55555555


def dos_track_mfm(data: bytes, track: int, sectors: int = 11) -> tuple[bytes, int]:
    """One turn of a standard AmigaDOS track holding ``data`` (512 * sectors bytes), as the Amiga writes it."""
    words: list[tuple[int, bool]] = []
    for s in range(sectors):
        info = struct.pack(">BBBB", 0xFF, track, s, sectors - s)
        hdr = _oddeven(info) + _oddeven(bytes(16))
        body = _oddeven(data[512 * s:512 * (s + 1)])
        words += [(0, False), (0, False), (0x4489, True), (0x4489, True)]
        words += hdr + _oddeven(struct.pack(">I", _csum(hdr))) + _oddeven(struct.pack(">I", _csum(body))) + body
    words += [(0, False)] * (6334 - len(words))               # the gap up to a whole turn (6334 words, as a DD drive writes it)
    return mfm_encode(words), 16 * len(words)


class AdfDisk:
    """A standard disk image (ADF): every track in the AmigaDOS format."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self._mfm: dict[int, tuple[bytes, int]] = {}

    def dos(self, n: int) -> Optional[bytes]:
        if n < 0 or (n + 1) * 0x1600 > len(self.data):
            return None
        return self.data[n * 0x1600:(n + 1) * 0x1600]

    def track(self, n: int) -> Optional[tuple[bytes, int]]:
        if n not in self._mfm:
            d = self.dos(n)
            if d is None:
                return None
            self._mfm[n] = dos_track_mfm(d, n)
        return self._mfm[n]


class _Raw:
    """A raw read: RAWLEN bytes of the track's MFM from where the read starts, and the bit-by-bit sync search over it."""

    def __init__(self, turn: bytes, nbits: int, start: int = 0) -> None:
        total = 8 * (RAWLEN + 8)
        rev = int.from_bytes(turn, "big") >> (8 * len(turn) - nbits) if nbits else 0
        start %= max(nbits, 1)
        # the turn from ``start`` round to ``start`` again, as often as fits (RawDIC reads with no index sync)
        first = rev & ((1 << (nbits - start)) - 1) if nbits else 0
        s, have = first, nbits - start
        while have < total and nbits:
            s = (s << nbits) | rev
            have += nbits
        if not nbits:
            s, have = 0, total
        s >>= have - total
        s &= ~((1 << 64) - 1)                                # (the 8 bytes past the read are not read: zero)
        self.s = s
        self.shifted: dict[int, bytes] = {}
        self.ptr = 0
        self.bit = 0

    def view(self, b: int) -> bytes:
        v = self.shifted.get(b)
        if v is None:
            v = self.shifted[b] = ((self.s << b) & ((1 << (8 * (RAWLEN + 8))) - 1)).to_bytes(RAWLEN + 8, "big")
        return v

    def search(self, word: int) -> bool:
        """_SearchMFMword_plain: the next place the 16-bit pattern is, then past every repeat of it."""
        pat = struct.pack(">H", word & 0xFFFF)
        limit = RAWLEN - 4
        best = None
        for b in range(16):
            v = self.view(b)
            p = self.ptr if b >= self.bit else self.ptr + 2
            while p <= limit:
                p = v.find(pat, p, limit + 2)
                if p < 0:
                    break
                if p & 1:
                    p += 1
                    continue
                break
            else:
                p = -1
            if p >= 0 and p <= limit and (best is None or (p, b) < best):
                best = (p, b)
        if best is None:
            return False
        p, b = best
        v = self.view(b)
        while v[p:p + 2] == pat:
            p += 2
            if p > limit:
                return False
        self.ptr, self.bit = p, b
        return True

    def aligned(self, length: int) -> bytes:
        """_BitShiftMFM: the stream from the search position, lined up on words, ``length`` bytes (zeros past the read)."""
        v = self.view(self.bit)
        out = v[self.ptr:RAWLEN + 2]
        return (out + bytes(max(0, length - len(out))))[:length]


def _dos_block(mfm: bytes, sectors: int) -> tuple[int, Optional[int], Optional[bytes]]:
    """slave.asm _DecodeDOSblock on the MFM after a sector's sync: (error, sector, its 512 bytes)."""
    m = 0x55555555
    longs = struct.unpack_from(">270I", mfm)
    hsum = 0
    for v in longs[:10]:
        hsum ^= v
    hsum &= m
    get = lambda i: (((longs[i] & m) << 1) + (longs[i + 1] & m)) & 0xFFFFFFFF
    sec = (get(0) >> 8) & 0xFF
    if sec >= sectors:
        return IERR_UNDEFINED, None, None
    if get(10) != hsum:
        return IERR_CHECKSUM, sec, None
    dsum = get(12)
    s = 0
    body = bytearray(512)
    for i in range(128):
        o, e = longs[14 + i] & m, longs[142 + i] & m
        s ^= o ^ e
        struct.pack_into(">I", body, 4 * i, ((o << 1) + e) & 0xFFFFFFFF)
    return (IERR_OK if s == dsum else IERR_CHECKSUM), sec, bytes(body)


def dos_sectors(turn: bytes, nbits: int, sectors: int = 11) -> Optional[bytes]:
    """A track's standard AmigaDOS sectors from its MFM, or None when it is not a whole, good standard track."""
    raw = _Raw(turn, nbits)
    out = bytearray(512 * sectors)
    seen: set[int] = set()
    for _ in range(4 * sectors):
        if not raw.search(SYNC_STD):
            break
        err, sec, data = _dos_block(raw.aligned(0x440), sectors)
        if err == IERR_OK and sec is not None and data is not None:
            out[512 * sec:512 * sec + 512] = data
            seen.add(sec)
            if len(seen) == sectors:
                return bytes(out)
    return None


def standard_image(disk: Disk, tracks: int = 160) -> tuple[bytes, list[int]]:
    """The disk as an ADF: each track's standard sectors (zeros where a track is in another format) and the tracks that are."""
    out = bytearray(tracks * 0x1600)
    bad = []
    for t in range(tracks):
        data = disk.dos(t)
        if data is None:
            got = disk.track(t)
            data = dos_sectors(*got) if got else None
        if data is None:
            bad.append(t)
        else:
            out[t * 0x1600:(t + 1) * 0x1600] = data
    return bytes(out), bad


def track_crc16(data: bytes) -> int:
    """RawDIC's CRC16 of a track (the values in the imagers' CRC lists)."""
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else c << 1
        c &= 0xFFFF
    return c


# ---------------------------------------------------------------------- the imager
class Imager:
    def __init__(self, islave: bytes) -> None:
        self.cpu = cpu = Cpu(1 << 24)
        self.hunks = load_hunks(islave, cpu, _SLAVE)
        code = self.hunks[0]
        if bytes(cpu.m[code + 4:code + 10]) != b"RAWDIC":
            raise ImagerError("not a RawDIC imager")
        self.slv = code + 10
        self.version = cpu.r8(self.slv)
        self.flags = cpu.r8(self.slv + 1)
        if self.version > VERSION:
            raise ImagerError(_ERRORS[-13])
        if self.flags & ~1:
            raise ImagerError(_ERRORS[-15])
        self.debug = bool(self.flags & 1)
        self.files: dict[str, bytearray] = {}
        self.names: dict[str, str] = {}                      # lower case -> as first written
        self.log: list[str] = []
        self.disks: dict[int, Disk] = {}
        self._heap = _HEAP
        self.max_steps = 400_000_000
        self.read_file: Optional[Callable[[str], Optional[bytes]]] = None
        self.handles: dict[int, list] = {}
        self.locks: dict[int, str] = {}
        self.dirs: set[str] = set()
        self.made_dirs: list[str] = []
        self.curdir = ""
        self._cur_lock = 0
        self._fh = 0
        self._setup()

    # --- the library, exec and dos
    def _setup(self) -> None:
        cpu = self.cpu
        for i, f in enumerate((self._lib_readtrack, self._lib_nextsync, self._lib_nextmfmword, self._lib_savefile,
                               self._lib_savediskfile, self._lib_appendfile, self._lib_appenddiskfile, self._lib_dmfm_standard,
                               self._lib_print, self._lib_setsync, self._lib_reserved, self._lib_reserved)):
            cpu.hook(_LIB + 4 * i, f)
        cpu.w32(4, _EXEC)
        for lvo in range(6, 0x400, 6):
            cpu.hook(_EXEC - lvo, self._make_lvo("exec", lvo))
            cpu.hook(_DOS - lvo, self._make_lvo("dos", lvo))
        cpu.w16(_EXEC + 20, 37)                              # lib_Version: Kickstart 2.04
        cpu.w16(_DOS + 20, 37)

    def _make_lvo(self, lib: str, lvo: int) -> Callable[[Cpu], None]:
        def f(cpu: Cpu) -> None:
            h = getattr(self, f"_{lib}_{lvo}", None)
            if h is None:
                raise ImagerError(f"the imager calls {lib}.library -{lvo}, which this RawDIC does not have")
            h(cpu)
        return f

    def _alloc(self, size: int) -> int:
        a = (self._heap + 15) & ~15
        self._heap = a + max(size, 0)
        if self._heap >= _IMAGE:
            raise ImagerError("the imager asks for more memory than there is")
        self.cpu.m[a:a + size] = bytes(size)
        return a

    def _exec_198(self, cpu: Cpu) -> None:                   # AllocMem(d0 size, d1 flags)
        cpu.d[0] = self._alloc(cpu.d[0])

    def _exec_684(self, cpu: Cpu) -> None:                   # AllocVec
        cpu.d[0] = self._alloc(cpu.d[0])

    def _exec_210(self, cpu: Cpu) -> None:                   # FreeMem
        pass

    _exec_690 = _exec_210                                    # FreeVec
    _exec_132 = _exec_138 = _exec_414 = _exec_636 = _exec_642 = _exec_210   # Forbid Permit CloseLibrary CacheClearU CacheClearE

    def _exec_624(self, cpu: Cpu) -> None:                   # CopyMem(a0, a1, d0)
        n = cpu.d[0]
        cpu.m[cpu.a[1]:cpu.a[1] + n] = cpu.m[cpu.a[0]:cpu.a[0] + n]

    _exec_630 = _exec_624                                    # CopyMemQuick

    def _exec_552(self, cpu: Cpu) -> None:                   # OpenLibrary(a1 name, d0 version)
        name = cpu.cstring(cpu.a[1]).lower()
        cpu.d[0] = _DOS if name == "dos.library" else _EXEC if name == "exec.library" else 0

    _exec_408 = _exec_552                                    # OldOpenLibrary

    # dos.library: files are the imager's own (and, to read, the install drawer's), drawers made as it asks, a current drawer
    def _path(self, at: int) -> str:
        name = self.cpu.cstring(at).replace("\\", "/")
        if ":" in name:
            name = name.split(":", 1)[1]
        parts = [] if name.startswith("/") else [p for p in self.curdir.split("/") if p]
        for p in name.split("/"):
            if p == "" or p == ".":
                continue
            parts.append(p)
        return "/".join(parts)

    def _find(self, path: str) -> Optional[bytearray]:
        key = path.lower()
        if key in self.names:
            return self.files[self.names[key]]
        if self.read_file is not None:
            data = self.read_file(path)
            if data is not None:
                return bytearray(data)
        return None

    def _dos_30(self, cpu: Cpu) -> None:                     # Open(d1 name, d2 mode)
        path = self._path(cpu.d[1])
        mode = cpu.d[2]
        if mode == 1006:                                     # MODE_NEWFILE
            self._write(path, b"", False)
            data = self._file(path)
        else:
            data = self._find(path)
            if data is None:
                if mode != 1004:
                    cpu.d[0] = 0
                    return
                data = self._file(path)
        self._fh += 1
        self.handles[self._fh] = [path, data, 0]
        cpu.d[0] = self._fh << 2

    def _handle(self, v: int) -> list:
        h = self.handles.get(v >> 2)
        if h is None:
            raise ImagerError("the imager uses a file it did not open")
        return h

    def _dos_36(self, cpu: Cpu) -> None:                     # Close(d1)
        self.handles.pop(cpu.d[1] >> 2, None)
        cpu.d[0] = 0xFFFFFFFF

    def _dos_42(self, cpu: Cpu) -> None:                     # Read(d1, d2 buffer, d3 length)
        h = self._handle(cpu.d[1])
        chunk = bytes(h[1][h[2]:h[2] + cpu.d[3]])
        cpu.m[cpu.d[2]:cpu.d[2] + len(chunk)] = chunk
        h[2] += len(chunk)
        cpu.d[0] = len(chunk)

    def _dos_48(self, cpu: Cpu) -> None:                     # Write(d1, d2 buffer, d3 length)
        h = self._handle(cpu.d[1])
        if h[0].lower() not in self.names:                   # (a file of the drawer, written to: the imager's own from now)
            self._write(h[0], bytes(h[1]), False)
            h[1] = self._file(h[0])
        data = bytes(cpu.m[cpu.d[2]:cpu.d[2] + cpu.d[3]])
        h[1][h[2]:h[2] + len(data)] = data
        h[2] += len(data)
        cpu.d[0] = len(data)

    def _dos_66(self, cpu: Cpu) -> None:                     # Seek(d1, d2 position, d3 mode): the old position
        h = self._handle(cpu.d[1])
        old, pos, mode = h[2], _sx32(cpu.d[2]), _sx32(cpu.d[3])
        h[2] = max(0, (pos if mode == -1 else old + pos if mode == 0 else len(h[1]) + pos))
        cpu.d[0] = old

    def _lock(self, path: str) -> int:
        self._fh += 1
        self.locks[self._fh] = path
        return self._fh << 2

    def _dos_84(self, cpu: Cpu) -> None:                     # Lock(d1 name, d2 mode)
        path = self._path(cpu.d[1])
        ok = path == "" or path.lower() in self.dirs or self._find(path) is not None
        cpu.d[0] = self._lock(path) if ok else 0

    def _dos_120(self, cpu: Cpu) -> None:                    # CreateDir(d1 name)
        path = self._path(cpu.d[1])
        self.dirs.add(path.lower())
        self.made_dirs.append(path)
        cpu.d[0] = self._lock(path)

    def _dos_126(self, cpu: Cpu) -> None:                    # CurrentDir(d1 lock): the old one
        old = self._cur_lock
        self._cur_lock = cpu.d[1]
        self.curdir = self.locks.get(cpu.d[1] >> 2, "")
        cpu.d[0] = old

    def _dos_90(self, cpu: Cpu) -> None:                     # UnLock
        pass

    def _dos_132(self, cpu: Cpu) -> None:                    # IoErr
        cpu.d[0] = 0

    def _dos_72(self, cpu: Cpu) -> None:                     # DeleteFile(d1)
        key = self._path(cpu.d[1]).lower()
        if key in self.names:
            del self.files[self.names.pop(key)]
        cpu.d[0] = 0xFFFFFFFF

    def _dos_948(self, cpu: Cpu) -> None:                    # PutStr(d1)
        self.log.append(cpu.cstring(cpu.d[1]))
        cpu.d[0] = 0

    def _dos_954(self, cpu: Cpu) -> None:                    # VPrintf(d1 format, d2 args)
        self.log.append(self._format(cpu.d[1], cpu.d[2]))
        cpu.d[0] = 0

    # --- the state RawDIC keeps (xx_...)
    def _flags_d0(self) -> None:
        cpu = self.cpu
        v = cpu.d[0]
        cpu.n = 1 if v & 0x80000000 else 0
        cpu.z = 1 if v == 0 else 0
        cpu.v = cpu.c = 0

    def _ret(self, err: int, handler: bool = True) -> None:
        """A library call's result in D0, through lib_ErrorHandler: an error ends the imaging unless the disk has DFLG_ERRORS."""
        self.cpu.d[0] = err & 0xFFFFFFFF
        if handler and err < 0 and not (self.dflags & DFLG_ERRORS):
            raise ImagerError(self._why(err))
        self._flags_d0()

    def _why(self, err: int) -> str:
        where = f" (disk {self.disk_no}, track {self.cur_track})"
        return f"the imager stopped: {_ERRORS.get(err, f'error {err}')}{where}"

    @property
    def dflags(self) -> int:
        return self.cpu.r16(self.disk + 6)

    # --- track lists (slave.asm _GetTrackInfo, _TrackListLength, _DiskImageLength)
    def _entries(self) -> list[tuple[int, int, int, int, int, int]]:
        cpu = self.cpu
        out = []
        a = cpu.r32(self.disk + 8)
        for _ in range(1000):
            first = _sx16(cpu.r16(a))
            if first < 0:
                break
            out.append((first, _sx16(cpu.r16(a + 2)), cpu.r16(a + 4), cpu.r16(a + 6), cpu.r32(a + 8), a))
            a += 12
        return out

    def _count(self, first: int, last: int) -> int:
        n = abs(last - first)
        if self.dflags & DFLG_DOUBLEINC2:
            n >>= 1
        return n + 1

    def _test_tracklist(self) -> tuple[int, int]:
        """_TrackListLength: (number of tracks, the largest block) - (0, 0) when the list is not valid."""
        free = self.dflags & DFLG_NORESTRICTIONS
        es = self._entries()
        if not es:
            return 0, 0
        n = big = 0
        for i, (first, last, length, _s, _d, _a) in enumerate(es):
            if last < 0 or (last < first and not free) or length == 0:
                return 0, 0
            n += self._count(first, last)
            big = max(big, length)
            if i + 1 < len(es) and not free and not last < es[i + 1][0]:
                return 0, 0
        return n, big

    def _image_length(self) -> int:
        return sum(self._count(f, l) * ln for f, l, ln, _s, _d, _a in self._entries())

    def _track_info(self, track: int) -> int:
        """_GetTrackInfo: the track's offset in the disk image (and its sync, decoder, length become current), or -1."""
        dbl = self.dflags & DFLG_DOUBLEINC2
        off = 0
        for first, last, length, sync, dec, _a in self._entries():
            if dbl and (track + first) & 1:
                inside = False
            elif last >= first:
                inside = first <= track <= last
            else:
                inside = last <= track <= first
            if inside:
                k = abs(track - first)
                if dbl:
                    k >>= 1
                self.cur_sync, self.cur_decoder, self.cur_len = sync, dec, length
                return off + k * length
            off += self._count(first, last) * length
        return -1

    def _test_disk(self) -> None:
        if not self._test_tracklist()[0]:
            raise ImagerError(self._why(IERR_TRACKLIST))
        v = self.cpu.r16(self.disk + 4)
        if v == 0 or v > 2:
            raise ImagerError(self._why(IERR_DSKVERSION))
        if self.dflags & ~0x7F:
            raise ImagerError(self._why(IERR_FLAGS))

    def _alloc_trackbuffer(self) -> None:
        n, big = self._test_tracklist()
        if not n:
            raise ImagerError(self._why(IERR_TRACKLIST))
        self.track_len = max((big + 7) & ~7, 0x1800)
        if self.track_len > 0x10000:
            raise ImagerError("the imager's tracks are longer than 64 KB")

    def _clear_track(self) -> None:
        self.cpu.m[_TRACK:_TRACK + self.track_len] = bytes(self.track_len)

    # --- reading
    def _physical(self, retry: int) -> int:
        """GetAbsTrackNum: the drive's track (cylinder * 2 + side) for the current track."""
        t, f = self.cur_track, self.dflags
        if f & DFLG_SINGLESIDE:
            t += t
            if f & DFLG_ERRORSWAP:
                t |= retry & 1
        if f & DFLG_SWAPSIDES:
            t ^= 1
        return t

    def _raw_read(self, retry: int) -> int:
        """DriveRawRead: a raw read of the current track into the raw and MFM buffers."""
        src = self.disks.get(self.disk_no)
        if src is None:
            return IERR_NODISK
        got = src.track(self._physical(retry))
        if got is None:
            turn, nbits = b"", 0
        else:
            turn, nbits = got
        # the read starts where the disk happens to be; a retry starts somewhere else, as a real one would
        self.raw = _Raw(turn, nbits, start=(5 - retry) * (nbits // 3 + 977) if nbits else 0)
        self.cpu.m[_MFM:_MFM + RAWLEN] = self.raw.view(0)[:RAWLEN]
        return IERR_OK

    def _search(self, word: int, length: int) -> int:
        """_SearchMFMword (length RAWLEN) / _SearchMFMword_fast (length $440)."""
        cpu = self.cpu
        sync = self.cur_sync or SYNC_STD
        for i in range(1, 5):
            cpu.w16(_MFM - 2 * i, sync)                      # _AddSyncToBuffer
        if self.raw is None or not self.raw.search(word):
            return IERR_NOSYNC
        cpu.m[_MFM:_MFM + length] = self.raw.aligned(length)
        return IERR_OK

    def _decoder_call(self, dec: int) -> int:
        """A track decoder: a track takes it some 10,000 to 100,000 instructions; one that goes on and on is lost in a track
        that is not in its format (searching past the buffer for a pattern that never comes, as it would hang an Amiga)."""
        cpu = self.cpu
        cpu.d[0] = self.cur_track & 0xFFFF
        cpu.a[0], cpu.a[1], cpu.a[5] = _MFM, _TRACK, _LIB
        try:
            cpu.call(dec, max_steps=min(self._budget(), _DECODER_STEPS))
        except CpuError as exc:
            if "runs too long" not in str(exc):
                raise
            raise ImagerError(f"the imager's track decoder never finishes (disk {self.disk_no}, track {self.cur_track}): "
                              "the track is not in the format it reads") from None
        return _sx32(cpu.d[0])

    def _read_track_core(self) -> int:
        """_ReadTrackCore: read the current track (with retries) and decode it into the track buffer."""
        err = IERR_OK
        for retry in range(5, 0, -1):
            src = self.disks.get(self.disk_no)
            if (retry == 5 and self.cur_decoder == DMFM_STD and self.cur_sync == SYNC_STD and self.cur_len == 0x1600
                    and not self.dflags & DFLG_RAWREADONLY and src is not None and src.dos(self._physical(retry)) is not None):
                # (RawDIC reads such a track with trackdisk's own reader first)
                data = src.dos(self._physical(retry))
                self.cpu.m[_TRACK:_TRACK + 0x1600] = data
                self.raw = None
                return IERR_OK
            err = self._raw_read(retry)
            if err == IERR_OK and self.cur_sync != SYNC_INDEX:
                err = self._search(self.cur_sync, RAWLEN)
            if err == IERR_OK:
                if self.cur_decoder == DMFM_STD:
                    saved = list(self.cpu.d), list(self.cpu.a)
                    self.cpu.a[0], self.cpu.a[1] = _MFM, _TRACK
                    err = self._dos_track((self.cur_len >> 9) & 0xFF)
                    self.cpu.d[:], self.cpu.a[:] = saved
                else:
                    err = self._decoder_call(self.cur_decoder)
            if err >= 0:
                return err
        return err

    def _read_track(self, track: int) -> int:
        """_ReadTrack (the library's ReadTrack): a track by number - from the disk, or from the disk image once it is read."""
        self.cur_track = track
        off = self._track_info(track)
        if off < 0:
            return IERR_NOTRACK
        if self.finished:
            n = self.cur_len
            if not off < self.image_len or n > self.image_len - off:
                return IERR_DISKRANGE
            self.cpu.m[_TRACK:_TRACK + n] = self.cpu.m[_IMAGE + off:_IMAGE + off + n]
            return IERR_OK
        self._clear_track()
        if self.cur_decoder == DMFM_NULL:
            return IERR_OK
        return self._read_track_core()

    # --- the standard format (slave.asm _DecodeDOSTrack)
    def _dos_track(self, sectors: int) -> int:
        cpu = self.cpu
        a0, a1 = cpu.a[0], cpu.a[1]
        sectors &= 0xFF
        if sectors == 0:
            return IERR_OK
        sectors = min(sectors, 32)
        seen = [False] * sectors
        error = 0
        left = sectors
        while left:
            err = self._search(self.cur_sync or SYNC_STD, 0x440)
            if err:
                return err
            err, sec = self._dos_block(a0, a1, sectors)
            if err == IERR_UNDEFINED:
                continue
            if sec is not None:
                seen[sec] = True
            if err == IERR_CHECKSUM:
                error = err
            elif err:
                return err
            left -= 1
        if not all(seen):
            return IERR_NOSECTOR
        return error

    def _dos_block(self, a0: int, a1: int, sectors: int) -> tuple[int, Optional[int]]:
        err, sec, data = _dos_block(bytes(self.cpu.m[a0:a0 + 0x440]), sectors)
        if data is not None:
            self.cpu.m[a1 + 512 * sec:a1 + 512 * sec + 512] = data
        return err, sec

    # --- library calls (the hooks: registers kept but for the results)
    def _lib_readtrack(self, cpu: Cpu) -> None:
        if self.calling == "decoder":
            raise ImagerError(self._why(IERR_NOFUNCTION))
        saved = list(cpu.d), list(cpu.a)
        err = self._read_track(_sx16(cpu.d[0]))           # (RawDIC's current track, sync, decoder stay changed, as there)
        cpu.d[:], cpu.a[:] = saved
        cpu.a[1] = _TRACK
        self._ret(err)

    def _lib_nextsync(self, cpu: Cpu) -> None:
        cpu.d[0] = self.cur_sync
        self._lib_nextmfmword(cpu)

    def _lib_nextmfmword(self, cpu: Cpu) -> None:
        err = self._search(cpu.d[0] & 0xFFFF, RAWLEN)
        cpu.a[0] = _MFM
        self._ret(err)

    def _file(self, name: str) -> bytearray:
        if self.curdir and "/" not in name and ":" not in name:
            name = f"{self.curdir}/{name}"                   # (RawDIC writes in the current drawer)
        key = name.lower()
        if key not in self.names:
            self.names[key] = name
            self.files[name] = bytearray()
        return self.files[self.names[key]]

    def _write(self, name: str, data: bytes, append: bool) -> None:
        f = self._file(name)
        if not append:
            f[:] = b""
        f += data

    def _lib_savefile(self, cpu: Cpu, append: bool = False) -> None:
        name = cpu.cstring(cpu.a[0])
        n = cpu.d[0]
        self._write(name, bytes(cpu.m[cpu.a[1]:cpu.a[1] + n]), append)
        self._ret(IERR_OK)

    def _lib_appendfile(self, cpu: Cpu) -> None:
        self._lib_savefile(cpu, True)

    def _disk_file(self, name: str, off: int, length: int, append: bool) -> int:
        if off < 0 or off >= self.image_len or length < 0 or off + length > self.image_len:
            return IERR_DISKRANGE
        self._write(name, bytes(self.cpu.m[_IMAGE + off:_IMAGE + off + length]), append)
        return IERR_OK

    def _lib_savediskfile(self, cpu: Cpu, append: bool = False) -> None:
        if self.calling in ("init", "decoder"):
            raise ImagerError(self._why(IERR_NOFUNCTION))
        self._ret(self._disk_file(cpu.cstring(cpu.a[0]), _sx32(cpu.d[0]), _sx32(cpu.d[1]), append))

    def _lib_appenddiskfile(self, cpu: Cpu) -> None:
        self._lib_savediskfile(cpu, True)

    def _lib_dmfm_standard(self, cpu: Cpu) -> None:
        saved = list(cpu.d), list(cpu.a)
        err = self._dos_track(cpu.d[0] & 0xFF)
        cpu.d[:], cpu.a[:] = saved
        cpu.d[0] = err & 0xFFFFFFFF
        self._flags_d0()

    def _lib_print(self, cpu: Cpu) -> None:
        self.log.append(self._format(cpu.a[0], cpu.a[1]))

    def _lib_setsync(self, cpu: Cpu) -> None:
        if self.version < 5:
            raise ImagerError(self._why(IERR_SLAVEVER))
        self.cur_sync = cpu.d[0] & 0xFFFF

    def _lib_reserved(self, cpu: Cpu) -> None:
        pass

    def _format(self, fmt_at: int, args: int) -> str:
        """RawFmt / VPrintf: %d %ld %u %x %lx %s %c, with the arguments one after another (words unless 'l')."""
        cpu = self.cpu
        fmt = cpu.cstring(fmt_at)
        out, i, at = [], 0, args
        while i < len(fmt):
            ch = fmt[i]
            if ch != "%":
                out.append(ch); i += 1
                continue
            j = i + 1
            while j < len(fmt) and fmt[j] in "-0123456789.":
                j += 1
            long = j < len(fmt) and fmt[j] == "l"
            if long:
                j += 1
            kind = fmt[j] if j < len(fmt) else "%"
            if kind == "%":
                out.append("%")
            elif kind == "s":
                out.append(cpu.cstring(cpu.r32(at))); at += 4
            else:
                v = cpu.r32(at) if long else cpu.r16(at)
                at += 4 if long else 2
                out.append(f"{v:x}" if kind in "xX" else chr(v & 0xFF) if kind == "c" else str(_sx32(v) if long else _sx16(v))
                           if kind == "d" else str(v))
            i = j + 1
        return "".join(out)

    # --- the state machine (RawDIC.asm SM_Start, ReadDisk)
    def _call_user(self, kind: str, at: int) -> int:
        if not at:
            return IERR_OK
        cpu = self.cpu
        cpu.d[0] = self.disk_no
        cpu.a[0] = self.disk
        if self.version >= 5:
            cpu.a[1] = _IMAGE if self.image_len else 0
        else:
            cpu.a[1] = self.slv
        cpu.a[5] = _LIB
        self.calling = kind
        try:
            cpu.call(at, max_steps=self._budget())
        finally:
            self.calling = ""
        return _sx32(cpu.d[0])

    def _crc16(self) -> int:
        """slave.asm _TrackCRC16: each byte goes into the low byte (``eor.b``), then 8 shifts with $1021 - not the CRC-CCITT
        of the books (which takes the byte into the high byte), so the values the imagers know come out only this way."""
        return track_crc16(bytes(self.cpu.m[_TRACK:_TRACK + self.cur_len]))

    def _test_crc(self) -> int:
        """_TestCRC: the tracks of the CRC list must have the CRC16 values the imager knows."""
        self._alloc_trackbuffer()
        at = self.cpu.r32(self.disk + 20)
        if not at:
            return IERR_OK
        for _ in range(1000):
            t = _sx16(self.cpu.r16(at))
            if t == -1:
                break
            err = self._read_track(t)
            if err:
                return err
            if self._crc16() != self.cpu.r16(at + 2):
                return IERR_CRCFAIL
            at += 4
        return IERR_OK

    def _read_disk(self) -> None:
        pos = 0
        for first, last, length, sync, dec, _a in self._entries():
            step = (1 if last >= first else -1) * (2 if self.dflags & DFLG_DOUBLEINC2 else 1)
            self.cur_len, self.cur_sync, self.cur_decoder = length, sync, dec
            t = first
            while (t <= last) if step > 0 else (t >= last):
                self.cur_track = t
                self._clear_track()
                if dec != DMFM_NULL:
                    err = self._read_track_core()
                    if err < 0:
                        self.log.append(self._why(err))
                        if not (self.debug or self.ignore):
                            raise ImagerError(self._why(err))
                n = min(length, self.image_len - pos)
                if n > 0:
                    self.cpu.m[_IMAGE + pos:_IMAGE + pos + n] = self.cpu.m[_TRACK:_TRACK + n]
                    pos += n
                t += step
        self.finished = True

    def _save_files(self) -> None:
        cpu = self.cpu
        fl = cpu.r32(self.disk + 16)
        name = f"Disk.{self.disk_no}"
        if fl == 0:
            entries = [(None, 0, -1)]
        elif fl == 0xFFFFFFFF:
            entries = []
        else:
            entries = []
            for _ in range(10000):
                n = cpu.r32(fl)
                if n == 0xFFFFFFFF:
                    break
                entries.append((n, _sx32(cpu.r32(fl + 4)), _sx32(cpu.r32(fl + 8))))
                fl += 12
        for n, off, length in entries:
            fname = name if not n else cpu.cstring(n)
            if length == -1:
                length = self.image_len
            err = self._disk_file(fname, off, length, False)
            if err:
                raise ImagerError(self._why(err))

    def run(self, disks: dict[int, Disk], max_steps: int = 1_500_000_000, ignore_errors: bool = False,
            read_file: Optional[Callable[[str], Optional[bytes]]] = None) -> dict[str, bytes]:
        """Image every disk the imager reads (disk 1, 2 ... as ``disks`` gives them); returns the files written, in order (in
        drawers the imager made: "GG/G00"). ``ignore_errors``: RawDIC's IGNOREERRORS - a track that does not decode is kept as it
        came out. ``read_file``: files of the drawer RawDIC runs in, for an imager that reads one."""
        cpu = self.cpu
        self.disks = disks = dict(disks)              # (a copy: a disk read twice is added to it)
        self.read_file = read_file
        self.ignore = ignore_errors
        self.max_steps = max_steps
        self._start_steps = cpu.steps
        self._cur_lock = self._lock("")                    # (RawDIC runs in the drawer the shell CD'd to: a lock, never 0 -
        self.curdir = ""                                     # imagers that go back to it test for 0)
        self.disk = cpu.r32(self.slv + 2)
        self.disk_no = 1
        self.calling = ""
        self.raw: Optional[_Raw] = None
        self.cur_track = 0
        cpu.a[7] = _STACK
        try:
            while self.disk:
                if self.disk_no not in disks:
                    self._again()
                self.finished = False
                self.image_len = 0
                while True:                                  # the CRC check picks the disk structure (or an alternative)
                    err = self._test_crc()
                    if err == IERR_OK:
                        break
                    alt = cpu.r32(self.disk + 24)
                    if err != IERR_CRCFAIL or not alt:
                        raise ImagerError(self._why(err))
                    self.disk = alt
                self._test_disk()
                self._alloc_trackbuffer()
                err = self._call_user("init", cpu.r32(self.disk + 28))
                if err < 0:
                    raise ImagerError(self._why(err))
                self._test_disk()
                self._alloc_trackbuffer()
                self.image_len = self._image_length()
                if not self.image_len:
                    raise ImagerError(self._why(IERR_TRACKLIST))
                if self.image_len > _STACK - 0x10000 - _IMAGE:
                    raise ImagerError("the disk image is too big")
                cpu.m[_IMAGE:_IMAGE + self.image_len] = bytes(self.image_len)
                self.raw = None
                self._read_disk()
                err = self._call_user("disk", cpu.r32(self.disk + 32))
                if err < 0:
                    raise ImagerError(self._why(err))
                self._save_files()
                self.disk = cpu.r32(self.disk)
                self.disk_no += 1
        except CpuError as exc:
            raise ImagerError(f"the imager failed on disk {self.disk_no}: {exc}") from None
        return {k: bytes(v) for k, v in self.files.items()}

    def _budget(self) -> int:
        """What is left of the run's instructions (``max_steps`` for all its calls together: an imager lost in data that is not
        its disk's - a crack's - stops in good time)."""
        left = self.max_steps - (self.cpu.steps - self._start_steps)
        if left <= 0:
            raise ImagerError(f"the imager runs too long (over {self.max_steps:,} instructions): not the disk it reads")
        return left

    def _again(self) -> None:
        """A disk past the release's last: imagers that read a disk twice ("insert the same disk twice": Elite reads its side 1
        as disk 2). The disk its name (a version 2 structure's) gives the number of, else the only disk of a one-disk release."""
        cpu, disks = self.cpu, self.disks
        name = cpu.cstring(cpu.r32(self.disk + 36)) if cpu.r16(self.disk + 4) >= 2 and cpu.r32(self.disk + 36) else ""
        m = re.search(r"\b(\d+)\b", name)
        if m and int(m.group(1)) in disks and int(m.group(1)) != self.disk_no:
            disks[self.disk_no] = disks[int(m.group(1))]
        elif len(disks) == 1:
            disks[self.disk_no] = next(iter(disks.values()))
        else:
            raise ImagerError(f"the imager reads disk {self.disk_no}{f' ({name})' if name else ''}, which is not there")

    def disk_count(self) -> int:
        """How many disks the imager reads (following the first disk structures, not the alternatives)."""
        n, d = 0, self.cpu.r32(self.slv + 2)
        while d and n < 100:
            n += 1
            d = self.cpu.r32(d)
        return n
