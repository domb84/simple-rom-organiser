"""A small pure-Python reader for ``.7z`` archives (listing + streaming one member), stdlib only.

Why: starting ``7z.exe`` costs ~80 ms on Windows, so listing a library of thousands of ``.7z`` files with it
takes minutes, and a machine without 7-Zip cannot read them at all. The listing (names, sizes, CRC-32) only
needs the archive's header, and the common case (LZMA / LZMA2 / stored data, which is what 7-Zip, No-Intro and
TOSEC packs use) is handled by :mod:`lzma`. Anything else (encryption, BCJ and other filters, PPMd, BZip2,
multi-volume ...) raises :class:`Unsupported`, and the caller falls back to the real ``7z``.
"""

from __future__ import annotations

import lzma
import struct
import zlib
from pathlib import Path
from typing import BinaryIO, Optional

SIGNATURE = b"7z\xbc\xaf\x27\x1c"
CHUNK = 1 << 20

K_END, K_HEADER, K_ARCHIVE_PROPS, K_ADDITIONAL, K_MAIN_STREAMS, K_FILES = 0, 1, 2, 3, 4, 5
K_PACK_INFO, K_UNPACK_INFO, K_SUBSTREAMS, K_SIZE, K_CRC, K_FOLDER = 6, 7, 8, 9, 10, 11
K_CODERS_UNPACK_SIZE, K_NUM_UNPACK_STREAM, K_EMPTY_STREAM, K_EMPTY_FILE, K_ANTI, K_NAMES = 12, 13, 14, 15, 16, 17
K_ENCODED_HEADER = 0x17

COPY, LZMA, LZMA2 = b"\x00", b"\x03\x01\x01", b"\x21"


class Unsupported(Exception):
    """Valid 7z, but not something this reader handles (the caller uses 7-Zip instead)."""


class _Buf:
    def __init__(self, data: bytes) -> None:
        self.b = data
        self.p = 0

    def byte(self) -> int:
        if self.p >= len(self.b):
            raise ValueError("truncated 7z header")
        v = self.b[self.p]
        self.p += 1
        return v

    def take(self, n: int) -> bytes:
        if n < 0 or self.p + n > len(self.b):
            raise ValueError("truncated 7z header")
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def number(self) -> int:
        first = self.byte()
        mask, value = 0x80, 0
        for i in range(8):
            if not first & mask:
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def bits(self, n: int) -> list[bool]:
        out: list[bool] = []
        cur = mask = 0
        for _ in range(n):
            if not mask:
                cur, mask = self.byte(), 0x80
            out.append(bool(cur & mask))
            mask >>= 1
        return out

    def optional_bits(self, n: int) -> list[bool]:
        return [True] * n if self.byte() else self.bits(n)

    def digests(self, n: int) -> list[Optional[int]]:
        defined = self.optional_bits(n)
        return [struct.unpack("<I", self.take(4))[0] if d else None for d in defined]


class _Folder:
    def __init__(self) -> None:
        self.coders: list[tuple[bytes, bytes]] = []  # (method id, properties)
        self.unpack_sizes: list[int] = []
        self.pack_index = 0     # first pack stream of this folder
        self.pack_count = 0
        self.crc: Optional[int] = None
        self.num_streams = 1
        self.out_size = 0

    @property
    def simple(self) -> bool:
        return len(self.coders) == 1


class _Streams:
    def __init__(self) -> None:
        self.pack_pos = 0
        self.pack_sizes: list[int] = []
        self.folders: list[_Folder] = []
        self.sub_sizes: list[list[int]] = []       # per folder
        self.sub_crcs: list[list[Optional[int]]] = []


def _read_pack_info(r: _Buf, s: _Streams) -> None:
    s.pack_pos = r.number()
    n = r.number()
    while True:
        t = r.number()
        if t == K_END:
            return
        if t == K_SIZE:
            s.pack_sizes = [r.number() for _ in range(n)]
        elif t == K_CRC:
            r.digests(n)
        else:
            raise ValueError("unexpected property in PackInfo")


def _read_unpack_info(r: _Buf, s: _Streams) -> None:
    if r.number() != K_FOLDER:
        raise ValueError("expected Folder")
    nf = r.number()
    if r.byte():
        raise Unsupported("external folder data")
    pack_index = 0
    for _ in range(nf):
        f = _Folder()
        total_in = total_out = 0
        for _c in range(r.number()):
            flag = r.byte()
            if flag & 0x80:
                raise Unsupported("alternative coder methods")
            cid = r.take(flag & 0x0F)
            n_in, n_out = (r.number(), r.number()) if flag & 0x10 else (1, 1)
            props = r.take(r.number()) if flag & 0x20 else b""
            f.coders.append((cid, props))
            total_in += n_in
            total_out += n_out
        bound_out = set()
        for _b in range(total_out - 1):
            r.number()
            bound_out.add(r.number())
        n_packed = total_in - (total_out - 1)
        if n_packed > 1:
            for _p in range(n_packed):
                r.number()
        f.pack_index, f.pack_count = pack_index, n_packed
        pack_index += n_packed
        f.main_out = next((i for i in range(total_out) if i not in bound_out), 0)  # type: ignore[attr-defined]
        s.folders.append(f)
    if r.number() != K_CODERS_UNPACK_SIZE:
        raise ValueError("expected CodersUnpackSize")
    for f in s.folders:
        f.unpack_sizes = [r.number() for _ in range(len(f.coders))]  # one output per coder here
        f.out_size = f.unpack_sizes[min(f.main_out, len(f.unpack_sizes) - 1)]  # type: ignore[attr-defined]
    while True:
        t = r.number()
        if t == K_END:
            return
        if t == K_CRC:
            for f, c in zip(s.folders, r.digests(len(s.folders))):
                f.crc = c
        else:
            raise ValueError("unexpected property in UnpackInfo")


def _read_substreams(r: _Buf, s: _Streams) -> None:
    t = r.number()
    if t == K_NUM_UNPACK_STREAM:
        for f in s.folders:
            f.num_streams = r.number()
        t = r.number()
    s.sub_sizes = []
    if t == K_SIZE:
        for f in s.folders:
            if f.num_streams == 0:
                s.sub_sizes.append([])
                continue
            sizes = [r.number() for _ in range(f.num_streams - 1)]
            s.sub_sizes.append(sizes + [f.out_size - sum(sizes)])
        t = r.number()
    else:
        for f in s.folders:
            s.sub_sizes.append([f.out_size] if f.num_streams == 1 else [])
            if f.num_streams > 1:
                raise ValueError("missing substream sizes")
    unknown = sum(f.num_streams for f in s.folders if not (f.num_streams == 1 and f.crc is not None))
    s.sub_crcs = []
    got: list[Optional[int]] = []
    while t != K_END:
        if t == K_CRC:
            got = r.digests(unknown)
        else:
            raise ValueError("unexpected property in SubStreamsInfo")
        t = r.number()
    it = iter(got)
    for f in s.folders:
        if f.num_streams == 1 and f.crc is not None:
            s.sub_crcs.append([f.crc])
        else:
            s.sub_crcs.append([next(it, None) for _ in range(f.num_streams)])


def _read_streams(r: _Buf) -> _Streams:
    s = _Streams()
    have_sub = False
    while True:
        t = r.number()
        if t == K_END:
            break
        if t == K_PACK_INFO:
            _read_pack_info(r, s)
        elif t == K_UNPACK_INFO:
            _read_unpack_info(r, s)
        elif t == K_SUBSTREAMS:
            _read_substreams(r, s)
            have_sub = True
        else:
            raise ValueError("unexpected property in StreamsInfo")
    if not have_sub:
        s.sub_sizes = [[f.out_size] for f in s.folders]
        s.sub_crcs = [[f.crc] for f in s.folders]
    return s


def _decoder(folder: _Folder):
    """A fresh decompressor (``.decompress(data, max_length)``) for a single-coder folder, or None for stored."""
    if not folder.simple:
        raise Unsupported("filter chain (BCJ/delta/...)")
    cid, props = folder.coders[0]
    if cid == COPY:
        return None
    if cid == LZMA and len(props) == 5:
        d = props[0]
        if d >= 9 * 5 * 5:
            raise ValueError("bad LZMA properties")
        lc, lp, pb = d % 9, (d // 9) % 5, d // 45
        flt = {"id": lzma.FILTER_LZMA1, "dict_size": max(4096, struct.unpack("<I", props[1:])[0]),
               "lc": lc, "lp": lp, "pb": pb}
    elif cid == LZMA2 and len(props) == 1:
        b = props[0]
        if b > 40:
            raise ValueError("bad LZMA2 properties")
        flt = {"id": lzma.FILTER_LZMA2, "dict_size": 0xFFFFFFFF if b == 40 else (2 | (b & 1)) << (b // 2 + 11)}
    else:
        raise Unsupported(f"compression method {cid.hex()}")
    return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[flt])


def _folder_bytes(f: BinaryIO, base: int, s: _Streams, folder: _Folder, limit: Optional[int] = None):
    """Yield the decoded bytes of one folder (at most ``limit`` of them)."""
    if folder.pack_count != 1:
        raise Unsupported("folder with several packed streams")
    start = base + s.pack_pos + sum(s.pack_sizes[:folder.pack_index])
    left = s.pack_sizes[folder.pack_index]
    want = folder.out_size if limit is None else min(limit, folder.out_size)
    dec = _decoder(folder)
    f.seek(start)
    done = 0
    while done < want:
        if dec is None:
            chunk = f.read(min(CHUNK, left, want - done))
            if not chunk:
                raise ValueError("unexpected end of 7z data")
            left -= len(chunk)
            yield chunk
            done += len(chunk)
            continue
        if dec.needs_input:
            if left <= 0:
                raise ValueError("unexpected end of 7z data")
            data = f.read(min(CHUNK, left))
            if not data:
                raise ValueError("unexpected end of 7z data")
            left -= len(data)
        else:
            data = b""
        out = dec.decompress(data, min(CHUNK, want - done))
        if out:
            done += len(out)
            yield out
        elif dec.eof:
            break


def _open_header(f: BinaryIO) -> tuple[bytes, int]:
    sig = f.read(32)
    if len(sig) < 32 or sig[:6] != SIGNATURE:
        raise ValueError("not a 7z archive")
    if zlib.crc32(sig[12:32]) & 0xFFFFFFFF != struct.unpack("<I", sig[8:12])[0]:
        raise ValueError("7z start header CRC mismatch")
    off, size, crc = struct.unpack("<QQI", sig[12:32])
    if size == 0:
        return b"", 32
    f.seek(32 + off)
    data = f.read(size)
    if len(data) != size or zlib.crc32(data) & 0xFFFFFFFF != crc:
        raise ValueError("7z header CRC mismatch")
    return data, 32


class Archive:
    """The members of one 7z archive: ``.files`` = [(name, size, crc_or_None)] (folders skipped)."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.files: list[tuple[str, int, Optional[int]]] = []
        self._where: dict[str, tuple[int, int]] = {}   # name -> (folder index, offset inside the folder)
        self._streams = _Streams()
        with open(self.path, "rb") as f:
            data, base = _open_header(f)
            self._base = base
            while data:
                r = _Buf(data)
                t = r.number()
                if t == K_ENCODED_HEADER:
                    s = _read_streams(r)
                    if not s.folders:
                        raise ValueError("empty encoded header")
                    folder = s.folders[0]
                    data = b"".join(_folder_bytes(f, base, s, folder))
                    if folder.crc is not None and zlib.crc32(data) & 0xFFFFFFFF != folder.crc:
                        raise ValueError("7z header CRC mismatch")
                    continue
                if t != K_HEADER:
                    raise ValueError("unexpected 7z header")
                self._parse_header(r)
                break

    def _parse_header(self, r: _Buf) -> None:
        s = self._streams
        names: list[str] = []
        empty_stream: list[bool] = []
        empty_file: list[bool] = []
        anti: list[bool] = []
        n_files = 0
        while True:
            t = r.number()
            if t == K_END:
                break
            if t == K_ARCHIVE_PROPS:
                while r.number() != K_END:
                    r.take(r.number())
            elif t == K_ADDITIONAL:
                raise Unsupported("additional streams")
            elif t == K_MAIN_STREAMS:
                self._streams = s = _read_streams(r)
            elif t == K_FILES:
                n_files = r.number()
                while True:
                    pid = r.number()
                    if pid == K_END:
                        break
                    size = r.number()
                    sub = _Buf(r.take(size))
                    if pid == K_EMPTY_STREAM:
                        empty_stream = sub.bits(n_files)
                    elif pid == K_EMPTY_FILE:
                        empty_file = sub.bits(sum(empty_stream))
                    elif pid == K_ANTI:
                        anti = sub.bits(sum(empty_stream))
                    elif pid == K_NAMES:
                        if sub.byte():
                            raise Unsupported("external names")
                        raw = sub.b[sub.p:]
                        names = raw.decode("utf-16-le", "surrogatepass").split("\x00")[:n_files]
            else:
                raise ValueError("unexpected property in Header")
        if len(names) != n_files:
            raise ValueError("7z names do not match the file count")
        empty_stream = empty_stream or [False] * n_files
        sizes = [(sz, crc) for fsz, fcrc in zip(s.sub_sizes, s.sub_crcs) for sz, crc in zip(fsz, fcrc)]
        folder_of: list[tuple[int, int]] = []  # per non-empty stream: (folder, offset)
        for fi, fsz in enumerate(s.sub_sizes):
            off = 0
            for sz in fsz:
                folder_of.append((fi, off))
                off += sz
        stream_i = empty_i = 0
        for i, name in enumerate(names):
            if empty_stream[i]:
                is_file = empty_i < len(empty_file) and empty_file[empty_i]
                is_anti = empty_i < len(anti) and anti[empty_i]
                empty_i += 1
                if is_file and not is_anti:
                    self.files.append((name, 0, 0))
                    self._where[name] = (-1, 0)
                continue
            if stream_i >= len(sizes):
                raise ValueError("7z has more files than data streams")
            sz, crc = sizes[stream_i]
            self.files.append((name, sz, crc))
            self._where[name] = folder_of[stream_i]
            stream_i += 1

    def listing(self) -> list[tuple[str, int, str]]:
        """Like :func:`romorg.scanner.list_7z`: (path, size, crc as 8 hex digits)."""
        return [(n, sz, "%08x" % (crc & 0xFFFFFFFF) if crc is not None else "") for n, sz, crc in self.files]

    def iter_member(self, name: str):
        """Yield the bytes of one member (decoding the solid block up to it); verifies the member's CRC-32."""
        try:
            fi, off = self._where[name]
        except KeyError:
            raise KeyError(f"{name} is not in {self.path.name}") from None
        if fi < 0:
            return
        s = self._streams
        size = next(sz for n, sz, _c in self.files if n == name)
        crc_want = next(c for n, _s, c in self.files if n == name)
        skip, left, crc = off, size, 0
        with open(self.path, "rb") as f:
            for chunk in _folder_bytes(f, self._base, s, s.folders[fi], limit=off + size):
                if skip:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk, skip = chunk[skip:], 0
                chunk = chunk[:left]
                left -= len(chunk)
                crc = zlib.crc32(chunk, crc)
                yield chunk
                if left <= 0:
                    break
        if left > 0:
            raise ValueError(f"7z data for {name} ended early")
        if crc_want is not None and crc & 0xFFFFFFFF != crc_want:
            raise ValueError(f"CRC mismatch in {name}")


class MemberReader:
    """A read(n) file-like over one member (for code written against a stream)."""

    def __init__(self, archive: Archive, name: str) -> None:
        self._it = archive.iter_member(name)
        self._buf = b""

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            parts = [self._buf, *self._it]
            self._buf = b""
            return b"".join(parts)
        while len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self) -> None:
        self._it.close()
