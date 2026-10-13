"""Moving a PlayStation 2 save to the other region's game.

A PS2 save is a folder on the memory card named ``B`` + a region letter + the disc's serial + the game's own tail
(``BASLUS-20946GTA50000``), usually with a file inside that carries the same name. A game opens only the folder with its own
region's serial, so a USA save is invisible to the European release. Rewriting those names for the other release's serial lets it
find the save. Two kinds of card are handled, both written from the file system's public descriptions:

* a **folder card** (PCSX2): the save's folder is copied under the new name, its files renamed the same way (PCSX2's own ``_pcsx2_*``
  bookkeeping is left out; it is rebuilt);
* a **card image** (``.ps2``, 8 MB, with or without the 16 spare bytes after every 512-byte page): the names in the directory entries are
  rewritten in place (the save is *moved*, not copied: a copy would need a free cluster chain written to the card, and a card's file
  system is not something to grow by hand). The spare bytes of a changed page carry an error-correcting code that is recomputed;
  :func:`ecc` was checked against every written page of a real 8 MB card image.

A game that keeps the region inside its save data may still refuse the save. Nothing here touches a file by itself.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import List, Optional, Tuple

from . import pcsx2
from .savecarry import region_letter

__all__ = ["ecc", "page_spare", "head_for", "folder_plan", "convert_image", "image_saves"]

_PAR = [bin(i).count("1") & 1 for i in range(256)]
_COLUMN = (0x07, 0x16, 0x25, 0x34, 0x43, 0x52, 0x61, 0x70)       # what each bit of the chunk's XOR does to the column parity byte


def ecc(chunk: bytes) -> bytes:
    """The 3 ECC bytes of a 128-byte chunk of a card page (column parity, then the two line parities)."""
    column = 0
    cp, l0, l1 = 0x77, 0x7F, 0x7F
    for i, b in enumerate(chunk):
        column ^= b
        if _PAR[b]:
            l0 ^= i ^ 0x7F
            l1 ^= i
    for k in range(8):
        if column >> k & 1:
            cp ^= _COLUMN[k]
    return bytes((cp, l0 & 0x7F, l1 & 0x7F))


def page_spare(page: bytes) -> bytes:
    """The 16 spare bytes of a 512-byte page: the ECC of its four chunks and four zero bytes."""
    return b"".join(ecc(page[i * 128:(i + 1) * 128]) for i in range(4)) + bytes(4)


def head_for(serial: str) -> str:
    """``BASLUS-20946`` for ``SLUS-20946`` (the first 12 characters of a save's name); "" when the serial says no region."""
    letter = region_letter(serial.strip().upper()) if len(serial.strip()) == 10 else ""
    return f"B{letter}{serial.strip().upper()}" if letter else ""


def _rename(name: str, old_head: str, new_head: str) -> str:
    return new_head + name[len(old_head):] if name.startswith(old_head) else name


# --------------------------------------------------------------------------- folder cards
def folder_plan(folder: Path, old: str, new: str) -> Optional[Tuple[str, List[Tuple[Path, str]]]]:
    """``(the new folder's name, [(file, its path below the new folder)])`` for copying the save ``folder`` (whose serial is ``old``) to
    the release with serial ``new``; None when a serial says no region or the folder is not a save of ``old``. Nothing is touched."""
    folder = Path(folder)
    old_head, new_head = folder.name[:12], head_for(new)
    if not new_head or pcsx2.save_serial(folder.name) != old.strip().upper():
        return None
    files: List[Tuple[Path, str]] = []
    for dirpath, dirnames, names in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("_pcsx2_"))
        for name in sorted(names):
            if name.startswith("_pcsx2_"):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(folder).parts
            files.append((path, "/".join(rel[:-1] + (_rename(rel[-1], old_head, new_head),))))
    return _rename(folder.name, old_head, new_head), files


# --------------------------------------------------------------------------- card images
def _chain(card: "pcsx2._Card", first: int) -> List[int]:
    """The allocatable-cluster numbers of a directory's cluster chain."""
    out: List[int] = []
    cur = first
    while cur not in out and len(out) < card.clusters:
        out.append(cur)
        nxt = card.fat_entry(cur)
        if not nxt & 0x80000000:
            break
        nxt &= 0x7FFFFFFF
        if nxt == 0x7FFFFFFF:
            break
        cur = nxt
    return out


def _entry_at(card: "pcsx2._Card", first: int, index: int) -> Tuple[int, int, int]:
    """``(offset in the image of the directory entry's name field, the page it is in, the offset of that page)``."""
    chain = _chain(card, first)
    pos = index * 512
    cluster = chain[pos // card.cluster_bytes]
    within = pos % card.cluster_bytes
    page_no = card.alloc_offset + cluster
    page_in_cluster, in_page = divmod(within, card.page)
    start = ((page_no * card.pages_per_cluster) + page_in_cluster) * card.stride
    return start + in_page + 0x40, page_in_cluster, start


def convert_image(data: bytes, old: str, new: str) -> Optional[Tuple[bytes, int]]:
    """``(the card image with its save of the release ``old`` renamed for the release ``new``, saves changed)``; None when ``data`` is
    no card image, a serial says no region, there is no such save, or the card already has a save of ``new``. The names of the save's
    folder and of the files inside that carry its name are rewritten, and the ECC of every changed page recomputed."""
    old_head, new_head = head_for(old), head_for(new)
    if not old_head or not new_head or old_head == new_head:
        return None
    try:
        card = pcsx2._Card(bytes(data))
        rows = card.entries(card.rootdir, 1)
        count = min(rows[0][1], 512) if rows else 0
        root = card.entries(card.rootdir, count)
    except (ValueError, struct.error, IndexError):
        return None
    names = {name for _m, _l, _c, _t, name in root}
    if any(_rename(n, old_head, new_head) in names for n in names if n.startswith(old_head)):
        return None                                              # (the card already has a save under the new name)
    out = bytearray(data)
    touched = set()
    saves = 0

    def put(first: int, index: int, name: str) -> None:
        at, _p, page_start = _entry_at(card, first, index)
        out[at:at + 32] = name.encode("ascii")[:32].ljust(32, b"\0")
        touched.add(page_start)

    try:
        for i, (mode, length, cluster, _t, name) in enumerate(root):
            if name in (".", "..") or not mode & 0x8000 or not mode & 0x20 or not name.startswith(old_head):
                continue
            for j, (m2, _l2, _c2, _t2, inner) in enumerate(card.entries(cluster, min(length, 512))):
                if inner not in (".", "..") and m2 & 0x8000 and inner.startswith(old_head):
                    put(cluster, j, _rename(inner, old_head, new_head))
            put(card.rootdir, i, _rename(name, old_head, new_head))
            saves += 1
    except (ValueError, struct.error, IndexError):
        return None
    if not saves:
        return None
    if card.stride > card.page:                                   # (the spare bytes after each page: a programmed page keeps its ECC right)
        for start in touched:
            spare = start + card.page
            if bytes(out[spare:spare + 3]) != b"\xff\xff\xff":
                out[spare:spare + 16] = page_spare(bytes(out[start:start + card.page]))
    return bytes(out), saves


def image_saves(data: bytes) -> List[str]:
    """The names of the saves (directories) of a card image, for a caller that wants to know what is on it."""
    try:
        card = pcsx2._Card(bytes(data))
        rows = card.entries(card.rootdir, 1)
        return [n for m, _l, _c, _t, n in card.entries(card.rootdir, min(rows[0][1], 512) if rows else 0)
                if n not in (".", "..") and m & 0x8000 and m & 0x20]
    except (ValueError, struct.error, IndexError):
        return []
