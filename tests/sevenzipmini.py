"""A minimal ``.7z`` writer for tests (stdlib: LZMA2 through :mod:`lzma`, one folder per file, a plain header), so the archives of
the disc tests do not depend on a 7-Zip being installed. :mod:`romorg.sevenzip` reads what it writes."""

from __future__ import annotations

import lzma
import struct
import zlib
from pathlib import Path


def num(n: int) -> bytes:
    """A 7z variable-length number."""
    if n < 0x80:
        return bytes([n])
    for i in range(1, 9):
        if n < (1 << (7 * (i + 1))):
            first = ((0xFF << (8 - i)) & 0xFF) | (n >> (8 * i))
            return bytes([first]) + (n & ((1 << (8 * i)) - 1)).to_bytes(i, "little")
    return b"\xff" + n.to_bytes(8, "little")


def write_7z(path: Path, files: dict[str, bytes]) -> Path:
    packs = [lzma.compress(data, format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}]) for data in files.values()]
    n = len(files)
    h = bytearray(b"\x01\x04")                                                  # Header, MainStreamsInfo
    h += b"\x06" + num(0) + num(n) + b"\x09" + b"".join(num(len(p)) for p in packs) + b"\x00"       # PackInfo
    h += b"\x07\x0b" + num(n) + b"\x00"                                         # UnpackInfo: Folder, not external
    h += b"".join(num(1) + b"\x21" + b"\x21" + num(1) + bytes([16]) for _ in files)               # a folder: one LZMA2 coder, 1 MiB dictionary
    h += b"\x0c" + b"".join(num(len(d)) for d in files.values())                # CodersUnpackSize
    h += b"\x0a\x01" + b"".join(struct.pack("<I", zlib.crc32(d) & 0xFFFFFFFF) for d in files.values()) + b"\x00"   # CRCs, end of UnpackInfo
    h += b"\x00"                                                                # end of StreamsInfo
    names = b"".join(name.encode("utf-16-le") + b"\x00\x00" for name in files)
    h += b"\x05" + num(n) + b"\x11" + num(len(names) + 1) + b"\x00" + names + b"\x00"   # FilesInfo: the names
    h += b"\x00"                                                                # end of Header
    body = b"".join(packs)
    tail = struct.pack("<QQI", len(body), len(h), zlib.crc32(bytes(h)) & 0xFFFFFFFF)
    head = b"7z\xbc\xaf\x27\x1c" + b"\x00\x04" + struct.pack("<I", zlib.crc32(tail) & 0xFFFFFFFF) + tail
    Path(path).write_bytes(head + body + bytes(h))
    return Path(path)
