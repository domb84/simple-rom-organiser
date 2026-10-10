"""PCSX2 (PlayStation 2): where it keeps things, the disc's serial, and the saves.

* **Where**: its settings are ``PCSX2.ini`` in ``<root>/inis`` (root: ``~/.var/app/net.pcsx2.PCSX2/config/PCSX2`` for the flatpak,
  ``~/.config/PCSX2`` natively, ``Documents/PCSX2`` on Windows). ``[Folders]`` names the memory card and save state folders (relative to
  the root, or a path; the flatpak's portal paths ``/run/user/.../doc/...`` work from outside the sandbox too), ``[MemoryCards]``
  the files in the slots and ``[GameList]`` the game folders.
* **Serial**: a PS2 disc has ``SYSTEM.CNF`` in its ISO 9660 file system with ``BOOT2 = cdrom0:\\SLUS_209.46;1``: the serial is
  ``SLUS-20946``. It is read from an ``.iso`` or a ``.chd`` (CD or DVD) without extracting anything.
* **Memory cards**: a *folder card* is a folder with one folder per save (``BASLUS-20946GTA50000``: ``BA`` = America, ``BE`` = Europe,
  ``BI`` = Japan, then the serial, then the game's own name for the save); a *card image* (``Mcd001.ps2``, 8 MB) holds the same
  directories in the PS2 memory card file system, which is read for its root directory and the sizes of the saves.
* **Save states** (``sstates``): ``SLUS-20946 (CRC).NN.p2s``; the ``.backup`` copies are not counted.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import winproc

__all__ = ["Ps2Save", "detect_pcsx2", "read_serial", "serial_from_cnf", "save_serial", "find_cards", "find_states", "read_card_image",
           "parse_game_index", "ini_value", "PS2_DISC_EXTS"]

PS2_DISC_EXTS = (".iso", ".chd")
_SERIAL = re.compile(r"([A-Z]{4})[-_]?(\d{3})\.?(\d{2})")
_SAVE_DIR = re.compile(r"^B[A-Z](S[A-Z]{3}-\d{5})")
_STATE = re.compile(r"^([A-Z]{4}-\d{5}) \(([0-9A-Fa-f]{8})\)\.(\d+)\.p2s$")
SECTOR = 2048


@dataclass
class Ps2Save:
    kind: str                           # save (a folder in a card) | state | card (an image PCSX2 cards could not be read)
    serial: str                         # SLUS-20946 ("" when the name has none)
    name: str                           # the save's folder name / the state's file name
    path: Path
    card: str = ""                      # the memory card it is in
    files: int = 0
    bytes: int = 0
    mtime: float = 0.0

    def public(self) -> Dict[str, object]:
        return {"kind": self.kind, "serial": self.serial, "name": self.name, "path": str(self.path), "card": self.card, "files": self.files,
                "bytes": self.bytes, "mtime": self.mtime}


# --------------------------------------------------------------------------- the disc's serial
def serial_from_cnf(text: str) -> str:
    """``SLUS-20946`` from the text of SYSTEM.CNF (``BOOT2 = cdrom0:\\SLUS_209.46;1``); "" when it has none."""
    for line in text.replace("\r", "\n").split("\n"):
        key, sep, value = line.partition("=")
        if sep and key.strip().upper() == "BOOT2":
            m = _SERIAL.search(value.strip().upper())
            if m:
                return f"{m.group(1)}-{m.group(2)}{m.group(3)}"
    return ""


def _iso_serial(sector) -> str:
    """The serial from a disc whose 2048-byte sectors ``sector(lba)`` gives (ISO 9660: PVD at 16, root directory, SYSTEM.CNF)."""
    pvd = sector(16)
    if pvd[1:6] != b"CD001":
        return ""
    root_lba, root_len = struct.unpack("<I", pvd[158:162])[0], struct.unpack("<I", pvd[166:170])[0]
    data = b"".join(sector(root_lba + i) for i in range((min(root_len, 64 * SECTOR) + SECTOR - 1) // SECTOR))
    pos = 0
    while pos < len(data):
        n = data[pos]
        if n == 0:
            pos = (pos // SECTOR + 1) * SECTOR                                   # (records do not cross a sector)
            continue
        name_len = data[pos + 32]
        name = data[pos + 33:pos + 33 + name_len].decode("ascii", errors="replace").upper()
        if name.startswith("SYSTEM.CNF"):
            lba, size = struct.unpack("<I", data[pos + 2:pos + 6])[0], struct.unpack("<I", data[pos + 10:pos + 14])[0]
            body = b"".join(sector(lba + i) for i in range((min(size, 4096) + SECTOR - 1) // SECTOR))[:min(size, 4096)]
            return serial_from_cnf(body.decode("ascii", errors="replace"))
        pos += n
    return ""


def read_serial(path: Path) -> str:
    """The serial of a PS2 disc image (``.iso`` / ``.chd``); "" when it cannot be read."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".iso":
            with open(path, "rb") as f:
                def sector(lba: int) -> bytes:
                    f.seek(lba * SECTOR)
                    return f.read(SECTOR).ljust(SECTOR, b"\0")
                return _iso_serial(sector)
        if path.suffix.lower() == ".chd":
            from . import chd as chdlib
            info = chdlib.Chd(path, load_map=False)
            try:
                if info.is_dvd:
                    return _iso_serial(lambda lba: info.read_bytes(lba * SECTOR, SECTOR))
                track = next((t for t in info.tracks if not t.is_audio), None)
                if track is None:
                    return ""
                lead = {"MODE1_RAW": 16, "MODE2_RAW": 24, "MODE2": 8}.get(track.type, 0)

                def csector(lba: int) -> bytes:
                    raw = info.read_track_range(track, lba, 1)
                    return raw[lead:lead + SECTOR].ljust(SECTOR, b"\0")
                return _iso_serial(csector)
            finally:
                info.close()
    except Exception:  # noqa: BLE001 - an unreadable disc is a disc without a serial
        return ""
    return ""


# --------------------------------------------------------------------------- saves
def save_serial(dirname: str) -> str:
    """The serial in a memory card folder's name (``BASLUS-20946GTA50000`` -> ``SLUS-20946``); "" for system data and the like."""
    m = _SAVE_DIR.match(dirname)
    return m.group(1) if m else ""


def _measure(folder: Path) -> Tuple[int, int, float]:
    files = total = 0
    newest = 0.0
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            if name.startswith("_pcsx2_"):                                       # (PCSX2's own bookkeeping, not part of the save)
                continue
            try:
                st = (Path(dirpath) / name).stat()
            except OSError:
                continue
            files += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return files, total, newest


def find_cards(cards: Iterable[Path]) -> List[Ps2Save]:
    """The saves of the given memory cards: folder cards (a save = a folder in it) and card images."""
    out: List[Ps2Save] = []
    for card in cards:
        card = Path(card)
        if card.is_dir():
            try:
                saves = sorted(p for p in card.iterdir() if p.is_dir() and not p.name.startswith("_pcsx2_"))
            except OSError:
                continue
            for folder in saves:
                files, total, newest = _measure(folder)
                out.append(Ps2Save("save", save_serial(folder.name), folder.name, folder, card.name, files, total, newest))
        elif card.is_file():
            out.extend(read_card_image(card))
    return out


def find_states(folder: Path) -> List[Ps2Save]:
    """The save states in a PCSX2 ``sstates`` folder (one entry per file; ``.backup`` copies are left out)."""
    out: List[Ps2Save] = []
    try:
        files = sorted(p for p in Path(folder).iterdir() if p.is_file())
    except OSError:
        return out
    for p in files:
        m = _STATE.match(p.name)
        if m:
            st = p.stat()
            out.append(Ps2Save("state", m.group(1), p.name, p, "", 1, st.st_size, st.st_mtime))
    return out


# --------------------------------------------------------------------------- memory card images (PS2 memory card file system)
def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _stamp(b: bytes, o: int) -> float:
    """Seconds since the epoch of an 8-byte card timestamp (unused, sec, min, hour, day, month, year u16); 0 when it makes no sense."""
    import calendar
    sec, minute, hour, day, month = b[o + 1], b[o + 2], b[o + 3], b[o + 4], b[o + 5]
    year = _u16(b, o + 6)
    try:
        return float(calendar.timegm((year, month, day, hour, minute, sec)))
    except (ValueError, OverflowError):
        return 0.0


class _Card:
    """A raw PS2 memory card image (with or without the 16 ECC bytes after every 512-byte page)."""

    def __init__(self, data: bytes) -> None:
        if data[:28] != b"Sony PS2 Memory Card Format ":
            raise ValueError("not a PS2 memory card")
        self.data = data
        self.page = _u16(data, 0x28)
        self.pages_per_cluster = _u16(data, 0x2A)
        self.clusters = _u32(data, 0x30)
        self.alloc_offset = _u32(data, 0x34)
        self.rootdir = _u32(data, 0x3C)
        self.ifc = [_u32(data, 0x50 + 4 * i) for i in range(32)]
        if self.page not in (512, 1024) or self.pages_per_cluster == 0:
            raise ValueError("odd card geometry")
        total_pages = self.clusters * self.pages_per_cluster
        self.stride = self.page + (16 if len(data) >= total_pages * (self.page + 16) else 0)       # (ECC bytes after every page)
        self.cluster_bytes = self.page * self.pages_per_cluster
        self._fat: Dict[int, int] = {}

    def cluster(self, n: int) -> bytes:
        if not 0 <= n < self.clusters:
            raise ValueError("cluster out of range")
        out = b""
        for p in range(self.pages_per_cluster):
            start = (n * self.pages_per_cluster + p) * self.stride
            out += self.data[start:start + self.page]
        return out

    def fat_entry(self, index: int) -> int:
        per = self.cluster_bytes // 4
        if index not in self._fat:
            fat_cluster_no, off = divmod(index, per)
            ifc_per = self.cluster_bytes // 4
            ifc_index, in_ifc = divmod(fat_cluster_no, ifc_per)
            ifc_cluster = self.ifc[ifc_index]
            fat_cluster = _u32(self.cluster(ifc_cluster), 4 * in_ifc)
            self._fat[index] = _u32(self.cluster(fat_cluster), 4 * off)
        return self._fat[index]

    def chain(self, first: int) -> bytes:
        """The data of a cluster chain (``first`` is an allocatable-cluster index)."""
        out = b""
        seen = set()
        cur = first
        while cur not in seen and len(seen) < self.clusters:
            seen.add(cur)
            out += self.cluster(self.alloc_offset + cur)
            nxt = self.fat_entry(cur)
            if not nxt & 0x80000000:
                break
            nxt &= 0x7FFFFFFF
            if nxt == 0x7FFFFFFF:
                break
            cur = nxt
        return out

    def entries(self, first: int, count: int) -> List[Tuple[int, int, int, float, str]]:
        """``(mode, length, cluster, modified, name)`` of the first ``count`` directory entries of a directory."""
        raw = self.chain(first)
        out = []
        for i in range(count):
            e = raw[i * 512:(i + 1) * 512]
            if len(e) < 0x60:
                break
            mode, length, cluster = _u16(e, 0), _u32(e, 4), _u32(e, 0x10)
            name = e[0x40:0x60].split(b"\0")[0].decode("ascii", errors="replace")
            out.append((mode, length, cluster, _stamp(e, 0x18), name))
        return out

    def tree(self, first: int, count: int, depth: int = 0) -> Tuple[int, int, float]:
        """``(files, bytes, newest)`` of a directory, below it."""
        files = total = 0
        newest = 0.0
        for mode, length, cluster, mod, name in self.entries(first, count):
            if name in (".", "..") or not mode & 0x8000:
                continue
            if mode & 0x20 and depth < 4:
                f, b, n = self.tree(cluster, length, depth + 1)
                files, total, newest = files + f, total + b, max(newest, n)
            elif mode & 0x10:
                files, total, newest = files + 1, total + length, max(newest, mod)
        return files, total, newest


def read_card_image(path: Path) -> List[Ps2Save]:
    """The saves of a memory card image (each directory of its root). An image that cannot be read is one ``card`` entry."""
    path = Path(path)
    try:
        st = path.stat()
        card = _Card(path.read_bytes())
        root = card.entries(card.rootdir, 1)
        count = root[0][1] if root else 0
        out: List[Ps2Save] = []
        for mode, length, cluster, mod, name in card.entries(card.rootdir, min(count, 512)):
            if name in (".", "..") or not mode & 0x8000 or not mode & 0x20:
                continue
            files, total, newest = card.tree(cluster, length)
            out.append(Ps2Save("save", save_serial(name), name, path, path.name, files, total, newest or mod or st.st_mtime))
        return out
    except (OSError, ValueError, struct.error, IndexError):
        try:
            st = path.stat()
            return [Ps2Save("card", "", path.name, path, path.name, 1, st.st_size, st.st_mtime)]
        except OSError:
            return []


# --------------------------------------------------------------------------- where PCSX2 is
def ini_value(ini: Path, section: str, key: str) -> str:
    """``key`` of ``[section]`` in a PCSX2 ini ("" when absent)."""
    current = ""
    try:
        for line in Path(ini).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
            elif current == section and "=" in line:
                k, _sep, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()
    except OSError:
        pass
    return ""


def _resolve(root: Path, text: str, default: str) -> Path:
    p = Path(text or default)
    return Path(os.path.normpath(p if p.is_absolute() else root / p))


def detect_pcsx2(home: Optional[Path] = None, root: Optional[Path] = None) -> Dict[str, object]:
    """PCSX2's folders: ``root`` (the one with ``inis``), ``cards`` (the memory cards in its slots), ``states`` and ``games``; {} when
    it is not installed. With ``root`` only that folder is looked at (the user chose it)."""
    home = Path(home) if home else Path.home()
    cands = [home / ".var" / "app" / "net.pcsx2.PCSX2" / "config" / "PCSX2", home / ".config" / "PCSX2"]
    if os.environ.get("APPDATA"):
        cands.append(Path(os.environ["APPDATA"]) / "PCSX2")
    cands += [docs / "PCSX2" for docs in winproc.documents_dirs()]          # Windows: Documents\PCSX2 (wherever Documents is)
    cands.append(home / "Documents" / "PCSX2")
    if root is not None:
        cands = [Path(root)]
    for root in cands:
        ini = root / "inis" / "PCSX2.ini"
        if not ini.is_file() and not (root / "memcards").is_dir():
            continue
        card_dir = _resolve(root, ini_value(ini, "Folders", "MemoryCards"), "memcards")
        states = _resolve(root, ini_value(ini, "Folders", "SaveStates"), "sstates")
        cards: List[str] = []
        for slot in ("Slot1", "Slot2"):
            if ini_value(ini, "MemoryCards", f"{slot}_Enable").lower() != "false":
                name = ini_value(ini, "MemoryCards", f"{slot}_Filename")
                if name:
                    cards.append(str(card_dir / name))
        if not cards and card_dir.is_dir():                                      # (no slot named: every card in the folder)
            cards = [str(p) for p in sorted(card_dir.iterdir()) if p.suffix.lower() == ".ps2"]
        games: List[str] = []
        for key in ("RecursivePaths", "Paths"):
            for line in _ini_lines(ini, "GameList", key):
                games.append(line)
        return {"root": str(root), "ini": str(ini) if ini.is_file() else "", "cards": cards, "card_dir": str(card_dir), "states": str(states),
                "games": games}
    return {}


def _ini_lines(ini: Path, section: str, key: str) -> List[str]:
    out: List[str] = []
    current = ""
    try:
        for line in Path(ini).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
            elif current == section and "=" in line:
                k, _sep, v = line.partition("=")
                if k.strip() == key and v.strip():
                    out.append(v.strip())
    except OSError:
        pass
    return out


def parse_game_index(text: str) -> Dict[str, str]:
    """``{serial: name}`` from PCSX2's ``GameIndex.yaml`` (a ``SLUS-20946:`` line followed by ``  name: "..."``)."""
    out: Dict[str, str] = {}
    serial = ""
    for line in text.splitlines():
        if line and not line[0].isspace():
            m = re.match(r"^([A-Z]{4}-\d{5}):\s*$", line)
            serial = m.group(1) if m else ""
        elif serial and line.startswith("  name:"):
            name = line.split(":", 1)[1].strip().strip('"')
            if name:
                out.setdefault(serial, name)
            serial = ""
    return out
