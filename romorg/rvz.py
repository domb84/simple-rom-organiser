"""GameCube images in Dolphin's RVZ format (stdlib only; Zstandard through :mod:`romorg.zstdnative`).

Redump hashes the *ISO* of a disc. A ``.rvz`` file is a lossless, compressed re-encoding of that ISO, so to compare it
with the Redump DAT the ISO is rebuilt while it is read (never stored) and hashed: :func:`hash_image`. The format is
described in Dolphin's ``docs/WiaAndRvz.md``; what matters here:

* a 0x48-byte file head and a 0xDC-byte disc struct, then tables of *raw data* ranges and of *groups* (the disc cut
  into ``chunk_size`` pieces, typically 128 KiB), each group compressed on its own (Zstandard, bzip2, LZMA, LZMA2 or
  not at all);
* the first 0x80 bytes of the disc live in the disc struct;
* a group whose compressed size is 0 is all zeros;
* "RVZ packing": the padding that GameCube mastering tools filled with a pseudo-random stream is not stored, only its
  32-bit-word seed. A packed group is a list of ``(size, literal bytes)`` and ``(size | 0x80000000, 68-byte seed)``
  entries; the seed starts a Lagged Fibonacci generator (j = 32, k = 521) that is run :func:`junk` bytes forward.

Wii discs (the same container, but with encrypted partitions) are recognised and refused with
:class:`RvzUnsupported`; so are the older WIA files (another magic).

Decoding is Python, and the padding generator is the cost: about 55 MB/s per core (1.4 GB disc: about 6 s of CPU
when half of it is padding). :func:`hash_image` therefore spreads the groups over the worker processes of
:mod:`romorg.chdsched` (``op: "rvz"`` of :mod:`romorg.chdworker`) and hashes in order in the calling process.
"""

from __future__ import annotations

import bz2
import hashlib
import io
import json
import lzma
import struct
import threading
import zlib
from typing import BinaryIO, Callable, Iterator, List, Optional, Tuple

from . import zstdnative

__all__ = ["RvzError", "RvzUnsupported", "Rvz", "is_rvz", "junk", "hash_image", "iter_image"]

MAGIC = b"RVZ\x01"
HEAD_BYTES = 0x48
DISC_BYTES = 0xDC
SEED_BYTES = 68
JUNK_BLOCK = 0x8000
RAW_ENTRY = 24                  # wia_raw_data_t: u64 offset, u64 size, u32 first group, u32 groups
GROUP_ENTRY = 12                # rvz_group_t: u32 data_off4, u32 data_size, u32 rvz_packed_size
SLICE_BYTES = 4 << 20           # decoded bytes a worker is asked for at a time
NONE, PURGE, BZIP2, LZMA, LZMA2, ZSTD = range(6)
ProgressFn = Callable[[int, int], None]


class RvzError(Exception):
    """Damaged or inconsistent RVZ file."""


class RvzUnsupported(RvzError):
    """A valid image this reader does not handle (a Wii disc, a compression it does not know)."""


def is_rvz(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == MAGIC
    except OSError:
        return False


# --------------------------------------------------------------------------- the padding generator
_M03 = int.from_bytes(b"\x03" * 521, "big")
_M3F = int.from_bytes(b"\x3f" * 521, "big")
_FB = int.from_bytes


def _forward(state: bytes) -> bytes:
    """One full advance of the 521-word state (big-endian words): ``b[i] ^= b[i + 489]`` for the first 32 words, then
    ``b[i] ^= b[i - 32]`` for the rest. The second loop reads values it has just written, 32 words back, so it runs
    in blocks of 32 words (128 bytes), each XORed with the finished block before it."""
    cur = _FB(state[0:128], "big") ^ _FB(state[1956:2084], "big")
    parts = [cur.to_bytes(128, "big")]
    for k in range(1, 16):
        cur = _FB(state[128 * k:128 * k + 128], "big") ^ cur
        parts.append(cur.to_bytes(128, "big"))
    tail = _FB(state[2048:2084], "big") ^ (cur >> 736)      # words 512..520 ^ words 480..488 (the top 36 bytes)
    parts.append(tail.to_bytes(36, "big"))
    return b"".join(parts)


def _words(state: bytes) -> bytes:
    """The 2084 output bytes of a state: each word contributes ``w >> 24``, ``w >> 18`` (sic), ``w >> 8`` and ``w``."""
    b0, b1 = state[0::4], state[1::4]
    c1 = (((_FB(b0, "big") & _M03) << 6) | ((_FB(b1, "big") >> 2) & _M3F)).to_bytes(521, "big")
    out = bytearray(2084)
    out[0::4] = b0
    out[1::4] = c1
    out[2::4] = state[2::4]
    out[3::4] = state[3::4]
    return bytes(out)


def _seeded(seed: bytes) -> bytes:
    """The state after the seed (17 big-endian words) is stretched to 521 words and advanced four times."""
    buf = list(struct.unpack(">17I", seed)) + [0] * (521 - 17)
    for i in range(17, 521):
        buf[i] = ((buf[i - 17] << 23) & 0xFFFFFFFF) ^ (buf[i - 16] >> 9) ^ buf[i - 1]
    state = struct.pack(">521I", *buf)
    for _ in range(4):
        state = _forward(state)
    return state


def junk(seed: bytes, offset: int, size: int) -> bytes:
    """``size`` bytes of padding for the disc offset ``offset``: the generator is started from ``seed`` at the 32 KiB
    boundary below ``offset`` and advanced by the rest."""
    if len(seed) != SEED_BYTES:
        raise RvzError("a padding seed must be 68 bytes")
    skip = offset % JUNK_BLOCK
    state = _seeded(seed)
    need = skip + size
    out = []
    got = 0
    while got < need:
        out.append(_words(state))
        got += 2084
        if got < need:
            state = _forward(state)
    data = b"".join(out)
    return data[skip:skip + size]


def unpack(data: bytes, offset: int, want: int) -> bytes:
    """Decode RVZ packing: ``data`` is a list of literal runs and padding seeds; ``offset`` is where the group starts
    on the disc (the padding depends on it)."""
    out: List[bytes] = []
    pos = 0
    have = 0
    n = len(data)
    while pos < n:
        if pos + 4 > n:
            raise RvzError("truncated packed group")
        size = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if size & 0x80000000:
            size &= 0x7FFFFFFF
            if have + size > want:              # before junk() builds the run: a 4-byte field must not cost GiB
                raise RvzError(f"a packed group decodes to more than the {want} bytes expected")
            if pos + SEED_BYTES > n:
                raise RvzError("truncated padding seed")
            out.append(junk(data[pos:pos + SEED_BYTES], offset + have, size))
            pos += SEED_BYTES
        else:
            if pos + size > n:
                raise RvzError("truncated literal run")
            out.append(data[pos:pos + size])
            pos += size
        have += size
    if have != want:
        raise RvzError(f"a packed group decoded to {have} bytes, expected {want}")
    return b"".join(out)


# --------------------------------------------------------------------------- the container
def _decompress(method: int, blob: bytes, size: int, props: bytes) -> bytes:
    try:
        if method == NONE:
            out = blob
        elif method == ZSTD:
            out = zstdnative.decompress(blob, size)
        elif method == BZIP2:
            out = bz2.BZ2Decompressor().decompress(blob, size)
        elif method == LZMA:
            if len(props) < 5:
                raise RvzError("missing LZMA properties")
            d = props[0]
            if d >= 9 * 5 * 5:
                raise RvzUnsupported("bad LZMA properties")
            lc, lp, pb = d % 9, (d // 9) % 5, d // 45
            dic = max(4096, struct.unpack("<I", props[1:5])[0])
            out = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb,
                                                                   "dict_size": dic}]).decompress(blob, size)
        elif method == LZMA2:
            p = props[0] if props else 0
            if p > 40:
                raise RvzUnsupported("bad LZMA2 properties")
            dic = 0xFFFFFFFF if p == 40 else (2 | (p & 1)) << (p // 2 + 11)
            out = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2,
                                                                   "dict_size": min(dic, 1 << 30)}]).decompress(blob, size)
        else:
            raise RvzUnsupported(f"compression method {method} is not supported")
    except (zstdnative.ZstdError, OSError, EOFError, ValueError, lzma.LZMAError) as exc:
        raise RvzError(f"corrupt compressed data: {exc}") from exc
    if len(out) != size:
        raise RvzError(f"compressed data decoded to {len(out)} bytes, expected {size}")
    return out


class Rvz:
    """An opened ``.rvz`` GameCube image. :attr:`layout` lists the pieces of the ISO in order as
    ``(disc offset, length, group index)``; :meth:`piece` decodes one of them."""

    def __init__(self, path, fileobj: Optional[BinaryIO] = None):
        self.path = str(path)
        self._f = fileobj if fileobj is not None else open(path, "rb")
        self._own = fileobj is None
        try:
            self._read()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "Rvz":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._own and self._f is not None:
            self._f.close()
        self._f = None

    def _read(self) -> None:
        f = self._f
        f.seek(0, 2)
        self._fsize = f.tell()
        f.seek(0)
        h = f.read(HEAD_BYTES + DISC_BYTES)
        if h[:4] == b"WIA\x01":
            raise RvzUnsupported("a WIA file (convert it to RVZ or ISO with Dolphin)")
        if len(h) < 4 or h[:4] != MAGIC:
            raise RvzError("not an RVZ file")
        if len(h) < HEAD_BYTES + DISC_BYTES:
            raise RvzError("truncated RVZ header")
        disc_size = struct.unpack(">I", h[12:16])[0]
        self.iso_size, self.file_size = struct.unpack(">QQ", h[0x24:0x34])
        if hashlib.sha1(h[:0x34]).digest() != h[0x34:0x48]:
            raise RvzError("the RVZ header checksum does not match (damaged file)")
        d = h[HEAD_BYTES:HEAD_BYTES + DISC_BYTES]
        if disc_size < DISC_BYTES - 8 or hashlib.sha1(d[:min(disc_size, DISC_BYTES)]).digest() != h[0x10:0x24]:
            raise RvzError("the RVZ disc header checksum does not match (damaged file)")
        self.disc_type, self.compression, self.level, self.chunk_size = struct.unpack(">IIiI", d[:16])
        self.dhead = d[16:0x90]
        n_part, _part_size, _part_off = struct.unpack(">IIQ", d[0x90:0xA0])
        n_raw, raw_off, raw_size = struct.unpack(">IQI", d[0xB4:0xC4])
        n_groups, group_off, group_size = struct.unpack(">IQI", d[0xC4:0xD4])
        props_len = d[0xD4]
        self.props = bytes(d[0xD5:0xD5 + min(props_len, 7)])
        if self.disc_type == 2:
            raise RvzUnsupported("a Wii disc: only GameCube images are read")
        if self.disc_type != 1 or n_part:
            raise RvzUnsupported("not a plain GameCube image")
        if self.chunk_size < JUNK_BLOCK or self.chunk_size & (self.chunk_size - 1) and self.chunk_size % 0x200000:
            raise RvzError("bad chunk size")
        if n_raw == 0 or n_groups == 0 or n_raw > 1 << 16 or n_groups > 1 << 24:
            raise RvzError("bad table sizes")
        self.raws = self._table(raw_off, raw_size, n_raw * RAW_ENTRY, ">QQII", RAW_ENTRY, n_raw)
        self.groups = self._table(group_off, group_size, n_groups * GROUP_ENTRY, ">III", GROUP_ENTRY, n_groups)
        self.layout: List[Tuple[int, int, int]] = []
        pos = 0
        for off, size, first, count in sorted(self.raws):
            aligned = off - off % JUNK_BLOCK        # the first range starts at 0x80 but holds the data from 0
            size += off - aligned
            if aligned != pos:
                raise RvzError("the data ranges of the image do not follow each other")
            if first + count > n_groups or count != -(-size // self.chunk_size):
                raise RvzError("a data range names the wrong number of groups")
            for g in range(count):
                self.layout.append((aligned + g * self.chunk_size, min(self.chunk_size, size - g * self.chunk_size),
                                    first + g))
            pos += size
        if pos != self.iso_size:
            raise RvzError("the data ranges do not add up to the size of the disc")

    def _blob(self, off: int, n: int, what: str) -> bytes:
        """``n`` bytes at ``off``; the numbers come from the file, so they are checked against its size first."""
        if off < 0 or n < 0 or off + n > self._fsize:
            raise RvzError(what)
        self._f.seek(off)
        blob = self._f.read(n)
        if len(blob) != n:
            raise RvzError(what)
        return blob

    def _table(self, off: int, stored: int, size: int, fmt: str, entry: int, count: int) -> list:
        blob = self._blob(off, stored, "truncated RVZ table")
        raw = _decompress(self.compression, blob, size, self.props)
        return [struct.unpack_from(fmt, raw, i * entry) for i in range(count)]

    # -- decoding
    @property
    def pieces(self) -> int:
        return len(self.layout)

    def piece(self, index: int) -> bytes:
        """The bytes of ``layout[index]`` of the ISO."""
        disc_off, length, gi = self.layout[index]
        data_off4, data_size, packed = self.groups[gi]
        n = data_size & 0x7FFFFFFF
        if n == 0:
            block = bytes(length)
        else:
            blob = self._blob(data_off4 * 4, n, "the RVZ file is truncated")
            if data_size & 0x80000000:
                if packed > length + 72 * (length // 4 + 1):     # records of 4 bytes + a 68-byte seed at most
                    raise RvzError("a group claims an impossible packed size")
                blob = _decompress(self.compression, blob, packed or length, self.props)
            block = unpack(blob, disc_off, length) if packed else blob
            if len(block) != length:
                raise RvzError("a group decoded to the wrong size")
        if disc_off == 0:
            block = self.dhead + block[0x80:]
        return block

    def read_pieces(self, first: int, count: int) -> bytes:
        return b"".join(self.piece(i) for i in range(first, first + count))

    def iter_image(self) -> Iterator[bytes]:
        for i in range(len(self.layout)):
            yield self.piece(i)


def iter_image(path) -> Iterator[bytes]:
    """The ISO a ``.rvz`` file stands for, in pieces (one process)."""
    with Rvz(path) as r:
        yield from r.iter_image()


# --------------------------------------------------------------------------- hashing, in parallel
def hash_image(path, workers: Optional[int] = None, cancel: Optional[Callable[[], bool]] = None,
               progress: Optional[ProgressFn] = None) -> Tuple[str, str, int]:
    """``(crc32 hex, sha1 hex, size)`` of the ISO a ``.rvz`` file stands for, decoded by worker processes (this
    process when there is one core, a tiny image, or no worker starts). ``progress(done bytes, total bytes)``.
    Raises :class:`RvzError` / :class:`RvzUnsupported`; :class:`InterruptedError` when ``cancel()`` turns true."""
    with Rvz(path) as r:
        total = r.iso_size
        n = r.pieces
        per = max(1, SLICE_BYTES // r.chunk_size)
        slices = [(i, min(per, n - i)) for i in range(0, n, per)]
        stat = _stat(path)
        if workers is None:
            from . import chdsched
            workers = chdsched.default_workers()
        crc = 0
        sha = hashlib.sha1()
        done = 0

        def feed(data: bytes) -> None:
            nonlocal crc, done
            crc = zlib.crc32(data, crc)
            sha.update(data)
            done += len(data)
            if progress:
                progress(done, total)

        if workers < 2 or len(slices) < 3:
            for first, count in slices:
                if cancel and cancel():
                    raise InterruptedError("cancelled")
                feed(r.read_pieces(first, count))
        else:
            _parallel(r, path, stat, slices, min(workers, len(slices)), feed, cancel)
        if done != total:
            raise RvzError("the image is shorter than its header says")
        return "%08x" % (crc & 0xFFFFFFFF), sha.hexdigest(), total


def _stat(path) -> list:
    import os
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


def _parallel(r: Rvz, path, stat: list, slices: list, workers: int, feed: Callable[[bytes], None],
              cancel: Optional[Callable[[], bool]]) -> None:
    """Decode ``slices`` in ``workers`` processes; ``feed`` gets the bytes in order. A worker that fails hands its
    slice to this process; a damaged file raises."""
    from . import chdsched
    cond = threading.Condition()
    results: dict = {}
    state = {"next": 0, "stop": False, "error": None}
    window = workers * 3
    todo = list(range(len(slices)))

    def decode_here(i: int) -> bytes:
        first, count = slices[i]
        with Rvz(path) as local:
            return local.read_pieces(first, count)

    def run() -> None:
        proc = None
        rd = None
        alive = True
        try:
            proc = chdsched.spawn_worker()
            rd = io.BufferedReader(proc.stdout, buffer_size=1 << 16)
        except chdsched.PoolError:
            proc = None
        try:
            while True:
                with cond:
                    while not state["stop"] and todo and todo[0] - state["next"] >= window:
                        cond.wait(0.2)
                    if state["stop"] or not todo:
                        return
                    i = todo.pop(0)
                first, count = slices[i]
                data: Optional[bytes] = None
                if proc is not None and alive:
                    try:
                        req = {"id": i, "op": "rvz", "path": str(path), "sig": stat, "first": first, "count": count}
                        proc.stdin.write(json.dumps(req, separators=(",", ":")).encode() + b"\n")
                        head = json.loads(rd.readline() or b"{}")
                        if "err" in head:
                            raise RvzError(str(head["err"].get("msg")))
                        got = int(head["n"])
                        buf = rd.read(got)
                        if len(buf) != got:
                            raise OSError("a worker closed its pipe")
                        data = bytes(buf)
                    except RvzError:
                        raise
                    except (OSError, ValueError, KeyError, TypeError):
                        alive = False           # this thread decodes by itself from now on (the process is reaped below)
                if data is None:
                    data = decode_here(i)
                with cond:
                    results[i] = data
                    cond.notify_all()
        except BaseException as exc:  # noqa: BLE001 - handed to the consuming thread
            with cond:
                state["error"] = exc
                state["stop"] = True
                cond.notify_all()
        finally:
            if rd is not None:
                try:
                    rd.close()
                except (OSError, ValueError):
                    pass
            if proc is not None:
                chdsched.kill_worker(proc)

    threads = [threading.Thread(target=run, name=f"rvz-{k}", daemon=True) for k in range(workers)]
    for t in threads:
        t.start()
    try:
        for i in range(len(slices)):
            with cond:
                while i not in results and state["error"] is None:
                    if cancel and cancel():
                        state["stop"] = True
                        raise InterruptedError("cancelled")
                    cond.wait(0.1)
                if state["error"] is not None and i not in results:
                    raise state["error"]
                data = results.pop(i)
                state["next"] = i + 1
                cond.notify_all()
            feed(data)
    finally:
        with cond:
            state["stop"] = True
            cond.notify_all()
        for t in threads:
            t.join(timeout=10)
