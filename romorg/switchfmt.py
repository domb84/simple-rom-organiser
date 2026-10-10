"""Nintendo Switch game files, read without any key: what is in an ``.nsp`` / ``.nsz`` (a PFS0 container) or an ``.xci`` /
``.xcz`` (a game card image with an HFS0 partition tree), and the title ID, version and kind that follow from it.

Nothing here decrypts anything. The container headers are plain, and they are enough for most files:

* a **ticket** (``<rights id>.tik``) is in the file's name, and the first 16 hex digits of a rights ID are the title ID;
* a **``.cnmt.xml``** (NSPs made by common tools carry one) says ID, type and version outright;
* the **names of the NCAs** are their content IDs, and a title database (``switchdb``) knows which title and version each one
  belongs to (that is how an eShop NSP is recognised even when it is called ``game.nsp``);
* the **file name** of a dump usually has ``[0100F2C0115B6000]`` and ``[v655360]`` in it.

A game card image (XCI) is a different build of the same title: its NCAs are not in any database, so it is told by the name,
or, when the user's ``prod.keys`` are at hand, by the title ID in the header of its CNMT NCA (``switchkeys``).

Title IDs: an application ends in ``000``, its update is the same ID plus ``0x800``, an add-on (DLC) has the application's ID plus
``0x1000`` and a counter (``...7001``). ``base_id`` goes from any of them to the application's.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__all__ = ["CONTAINER_EXTS", "Container", "read_container", "kind_of_id", "base_id", "tags_from_name", "parse_cnmt_xml",
           "SwitchFormatError", "APPLICATION", "UPDATE", "ADDON"]

CONTAINER_EXTS = (".nsp", ".nsz", ".xci", ".xcz")
APPLICATION, UPDATE, ADDON = "application", "update", "addon"
_HEX16 = re.compile(r"^[0-9A-Fa-f]{16}$")
_ID_TAG = re.compile(r"\[(0100[0-9A-Fa-f]{12})\]")
_VERSION_TAG = re.compile(r"\[v(\d+)\]", re.IGNORECASE)
_BARE_VERSION = re.compile(r"\[(\d{4,9})\]")
_MAX_HEADER = 64 * 1024 * 1024        # a header larger than this is not a game file
_MAX_XML = 1 << 20


class SwitchFormatError(ValueError):
    """The file is not a Switch container (or is damaged)."""


@dataclass
class Container:
    """What the headers of a container say."""
    kind: str                                   # "nsp" | "xci"
    files: List[Tuple[str, int]] = field(default_factory=list)       # (name, size) of every file (XCI: secure partition)
    rights_ids: List[str] = field(default_factory=list)              # from the tickets, 32 hex digits, upper case
    cnmt: List[Dict[str, object]] = field(default_factory=list)      # parsed .cnmt.xml files: id, type, version
    cnmt_nca: List[Tuple[str, int, int]] = field(default_factory=list)   # (name, absolute offset, size) of the CNMT NCAs
    entries: List[Tuple[str, int, int]] = field(default_factory=list)    # (name, absolute offset, size) of every file

    @property
    def nca_ids(self) -> List[str]:
        """The content IDs (lower case hex) of all NCAs in the container."""
        out = []
        for name, _size in self.files:
            low = name.lower()
            stem = low.split(".")[0]
            if low.endswith((".nca", ".ncz")) and len(stem) == 32 and _is_hex(stem):
                out.append(stem)
        return out

    @property
    def ticket_title_ids(self) -> List[str]:
        return [r[:16] for r in self.rights_ids]


def _is_hex(text: str) -> bool:
    try:
        int(text, 16)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- IDs and names
def kind_of_id(title_id: str) -> str:
    """``application`` | ``update`` | ``addon`` for a title ID (16 hex digits)."""
    low = int(title_id, 16) & 0xFFF
    return APPLICATION if low == 0 else UPDATE if low == 0x800 else ADDON


def base_id(title_id: str) -> str:
    """The application's title ID (upper case) that an update or add-on belongs to."""
    n = int(title_id, 16)
    kind = kind_of_id(title_id)
    if kind == ADDON:
        n = (n & ~0xFFF) - 0x1000
    else:
        n &= ~0xFFF
    return f"{n:016X}"


def tags_from_name(name: str) -> Tuple[Optional[str], Optional[int]]:
    """``(title ID, version)`` as the name of a dump spells them: ``[0100F2C0115B6000]`` and ``[v655360]`` (or a bare
    ``[655360]``). None for what is not there."""
    m = _ID_TAG.search(name)
    title = m.group(1).upper() if m else None
    v = _VERSION_TAG.search(name) or _BARE_VERSION.search(name)
    return title, int(v.group(1)) if v else None


def parse_cnmt_xml(data: bytes) -> Optional[Dict[str, object]]:
    """``{id, type, version, base}`` from a ``.cnmt.xml`` (None when it cannot be read)."""
    try:
        root = ET.fromstring(data.decode("utf-8-sig", errors="replace"))
    except ET.ParseError:
        return None
    def text(tag: str) -> str:
        node = root.find(tag)
        return (node.text or "").strip() if node is not None and node.text else ""
    raw = text("Id").lower().removeprefix("0x")
    if not _HEX16.match(raw):
        return None
    try:
        version = int(text("Version") or 0)
    except ValueError:
        version = 0
    other = text("RequiredApplicationId") or text("ApplicationId")
    other = other.lower().removeprefix("0x")
    return {"id": raw.upper(), "type": text("Type"), "version": version,
            "base": other.upper() if _HEX16.match(other) else ""}


# --------------------------------------------------------------------------- PFS0 / HFS0
def _read_table(f, base: int, magic: bytes) -> Tuple[int, List[Tuple[str, int, int]]]:
    """``(header size, [(name, offset from the end of the header, size)])`` of a PFS0 / HFS0 at ``base``."""
    f.seek(base)
    head = f.read(16)
    if len(head) < 16 or head[:4] != magic:
        raise SwitchFormatError(f"no {magic.decode()} header")
    count, strsz = struct.unpack("<II", head[4:12])
    esz = 0x18 if magic == b"PFS0" else 0x40
    if count > 100000 or strsz > _MAX_HEADER or 16 + count * esz + strsz > _MAX_HEADER:
        raise SwitchFormatError("implausible header")
    entries = f.read(count * esz)
    strings = f.read(strsz)
    if len(entries) < count * esz or len(strings) < strsz:
        raise SwitchFormatError("truncated header")
    out = []
    for i in range(count):
        off, size, name_off = struct.unpack("<QQI", entries[i * esz:i * esz + 20])
        if name_off >= len(strings):
            raise SwitchFormatError("bad name offset")
        end = strings.find(b"\0", name_off)
        name = strings[name_off:end if end >= 0 else len(strings)].decode("utf-8", errors="replace")
        out.append((name, off, size))
    return 16 + count * esz + strsz, out


def read_container(path: Path) -> Container:
    """Read the headers of an NSP / NSZ / XCI / XCZ (a few kilobytes, never the game). Raises :class:`SwitchFormatError`."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic == b"PFS0":
                return _read_nsp(f)
            f.seek(0x100)
            if f.read(4) == b"HEAD":
                return _read_xci(f)
    except OSError as exc:
        raise SwitchFormatError(str(exc)) from exc
    raise SwitchFormatError("not a Switch container")


def _read_nsp(f) -> Container:
    hdr, files = _read_table(f, 0, b"PFS0")
    box = Container("nsp", [(n, s) for n, _o, s in files])
    box.entries = [(n, hdr + o, s) for n, o, s in files]
    for name, off, size in files:
        low = name.lower()
        if low.endswith(".tik"):
            stem = name[:-4]
            if len(stem) == 32 and _is_hex(stem):
                box.rights_ids.append(stem.upper())
        elif low.endswith(".cnmt.xml") and size <= _MAX_XML:
            f.seek(hdr + off)
            info = parse_cnmt_xml(f.read(size))
            if info:
                box.cnmt.append(info)
        elif low.endswith((".cnmt.nca", ".cnmt.ncz")):
            box.cnmt_nca.append((name, hdr + off, size))
    return box


def _read_xci(f) -> Container:
    f.seek(0x130)
    root_off, _root_size = struct.unpack("<QQ", f.read(16))
    hdr, parts = _read_table(f, root_off, b"HFS0")
    box = Container("xci")
    for pname, poff, _psize in parts:
        base = root_off + hdr + poff
        if pname != "secure":                        # (update / normal / logo hold nothing the game is told by)
            continue
        phdr, files = _read_table(f, base, b"HFS0")
        for name, off, size in files:
            box.files.append((name, size))
            box.entries.append((name, base + phdr + off, size))
            low = name.lower()
            if low.endswith(".tik"):
                stem = name[:-4]
                if len(stem) == 32 and _is_hex(stem):
                    box.rights_ids.append(stem.upper())
            elif low.endswith((".cnmt.nca", ".cnmt.ncz")):
                box.cnmt_nca.append((name, base + phdr + off, size))
    return box
