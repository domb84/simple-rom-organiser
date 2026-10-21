"""AmigaDOS disks in ADF images (stdlib only): the files of an OFS / FFS floppy (``DOS\\0`` .. ``DOS\\7``).

An ADF is the 880 KB (DD) or 1760 KB (HD) of a floppy's sectors, 512-byte blocks in order. The root block is in the middle; a
directory is a 72-entry hash table of header blocks (each a file or a directory, with a chain for names that hash alike); a file's data
blocks are listed in its header (and extension blocks), backwards. OFS data blocks carry a 24-byte header, FFS ones are plain data. A
disk that is not AmigaDOS (a game's own format, a cracked "NDOS" disk) has no files: :class:`AdfError`.

    disk = AdfDisk(path_or_bytes)
    disk.label, disk.files()            # [("dir/file", size), ...]
    disk.read("s/startup-sequence")      # bytes (names compare without case, as on the Amiga)
"""

from __future__ import annotations

import os
import struct
from typing import Iterable, Iterator, Optional, Union

__all__ = ["AdfDisk", "AdfError"]

BLOCK = 512
_T_HEADER, _T_DATA, _T_LIST = 2, 8, 16
_ST_ROOT, _ST_DIR, _ST_FILE, _ST_SOFTLINK, _ST_LINKDIR, _ST_LINKFILE = 1, 2, -3, 3, 4, -4


class AdfError(Exception):
    """Not an AmigaDOS disk, or a damaged one."""


def _s32(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


class AdfDisk:
    def __init__(self, source: Union[str, os.PathLike, bytes], unreadable_tracks: Iterable[int] = ()) -> None:
        """``unreadable_tracks``: tracks the image has no data of (an IPF's tracks in a game's own format - a protection track):
        a file with a block on one cannot be read."""
        self.unreadable = frozenset(unreadable_tracks)
        if isinstance(source, (bytes, bytearray)):
            self.data = bytes(source)
        else:
            with open(source, "rb") as fh:
                self.data = fh.read()
        if len(self.data) not in (901120, 1802240) and len(self.data) % BLOCK:
            raise AdfError("not an ADF image (size)")
        self.blocks = len(self.data) // BLOCK
        boot = self.data[:4]
        if boot[:3] != b"DOS" or boot[3] > 7:
            raise AdfError("not an AmigaDOS disk (no DOS boot block)")
        self.ffs = bool(boot[3] & 1)
        self.intl = boot[3] in (2, 3, 4, 5)
        self.root = self.blocks // 2
        r = self._block(self.root)
        if self._long(r, 0) != _T_HEADER or _s32(self._long(r, BLOCK - 4)) != _ST_ROOT:
            raise AdfError("no root block")
        self.label = self._name(r)
        self.damaged: set[int] = set()                    # (the blocks a directory's chain led to that are no file header)
        self._files: Optional[list[tuple[str, int]]] = None

    # ---------------------------------------------------------------- blocks
    def _block(self, n: int) -> bytes:
        if not 0 <= n < self.blocks:
            raise AdfError(f"block {n} out of range")
        if self.unreadable and n // 11 in self.unreadable:
            raise AdfError(f"block {n} is on track {n // 11}, which is in the game's own format")
        return self.data[n * BLOCK:(n + 1) * BLOCK]

    @staticmethod
    def _long(b: bytes, off: int) -> int:
        return struct.unpack_from(">I", b, off)[0]

    @staticmethod
    def _name(b: bytes) -> str:
        n = b[BLOCK - 80]
        return b[BLOCK - 79:BLOCK - 79 + min(n, 30)].decode("latin-1")

    def _entries(self, dir_block: int) -> Iterator[tuple[str, int, int]]:
        """``(name, header block, secondary type)`` of a directory's entries."""
        d = self._block(dir_block)
        seen = set()
        for i in range(72):
            n = self._long(d, 24 + 4 * i)
            while n and n not in seen:
                seen.add(n)
                if not 0 <= n < self.blocks:
                    self.damaged.add(n)
                    break
                h = self._block(n)
                if self._long(h, 0) != _T_HEADER:          # (a chain into a block that is no file's: a damaged directory, or a
                    self.damaged.add(n)                    #  cracker's - AmigaDOS shows the other entries, so do we)
                    break
                yield self._name(h), n, _s32(self._long(h, BLOCK - 4))
                n = self._long(h, BLOCK - 16)          # (the next of the same hash)

    # ---------------------------------------------------------------- files
    def walk(self) -> Iterator[tuple[str, int, int]]:
        """``(path, header block, size)`` of every file, directories first-level first."""
        stack = [("", self.root)]
        seen = {self.root}
        count = 0
        while stack:
            prefix, blk = stack.pop()
            for name, n, st in sorted(self._entries(blk)):
                path = f"{prefix}{name}"
                count += 1
                if count > self.blocks:                # (more entries than the disk has blocks: a damaged disk)
                    raise AdfError("damaged directory (more entries than blocks)")
                if st in (_ST_DIR, _ST_LINKDIR):
                    if n in seen:                      # (a directory that holds itself, or one above it: a damaged disk)
                        raise AdfError(f"damaged directory (block {n} loops)")
                    seen.add(n)
                    stack.append((path + "/", n))
                elif st == _ST_FILE:
                    yield path, n, self._long(self._block(n), BLOCK - 188)

    def files(self) -> list[tuple[str, int]]:
        if self._files is None:                       # (asked for again and again by an install script: read once)
            self._files = [(p, size) for p, _n, size in self.walk()]
        return self._files

    def _find(self, path: str) -> Optional[int]:
        blk = self.root
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        for i, part in enumerate(parts):
            low = part.casefold()
            hit = next(((n, st) for name, n, st in self._entries(blk) if name.casefold() == low), None)
            if hit is None:
                return None
            blk = hit[0]
            if i < len(parts) - 1 and hit[1] not in (_ST_DIR, _ST_LINKDIR):
                return None
        return blk

    def read(self, path: str) -> bytes:
        n = self._find(path)
        if n is None:
            raise AdfError(f"no such file: {path}")
        return self._read_header(n)

    def _read_header(self, n: int) -> bytes:
        h = self._block(n)
        size = self._long(h, BLOCK - 188)
        blocks: list[int] = []
        cur = h
        guard = 0
        while True:
            count = self._long(cur, 8)
            # the data block numbers of this header / extension, last one first
            table = [self._long(cur, BLOCK - 204 - 4 * i) for i in range(min(count, 72))]
            blocks.extend(b for b in table if b)
            ext = self._long(cur, BLOCK - 8)
            if not ext:
                break
            cur = self._block(ext)
            guard += 1
            if guard > self.blocks:
                raise AdfError("extension loop")
        out = bytearray()
        for b in blocks:
            d = self._block(b)
            if self.ffs:
                out += d
            else:
                if self._long(d, 0) != _T_DATA:
                    raise AdfError(f"bad OFS data block {b}")
                out += d[24:24 + self._long(d, 12)]
            if len(out) >= size:
                break
        if len(out) < size:
            raise AdfError("file shorter than its size")
        return bytes(out[:size])
