"""Disc images as CHD input: ``.cue`` + ``.bin``, ``.gdi`` and ``.iso`` laid out the way ``chdman createcd`` /
``createdvd`` lay them out, so the CHD written from them has chdman's data SHA-1, metadata and header SHA-1.

What the layout is (checked against chdman 0.289, see ``tests/fixtures/chdwrite``):

* every frame of a CD is 2448 bytes: the sector at the start (2352 bytes raw, or 2048 / 2336 / 2324 "cooked"
  bytes followed by zeros) and 96 bytes of subcode, which no cue / gdi / iso has (zeros);
* audio is stored big-endian (the files are little-endian);
* every track is followed by zero frames up to a multiple of 4;
* cue: a track is the frames of its file from its ``INDEX 00`` (or ``INDEX 01`` without one) to the next track of
  the same file or the end of the file; ``INDEX 00`` makes the pregap part of the track (``PGTYPE`` with a ``V``),
  the ``PREGAP`` / ``POSTGAP`` commands only go into the metadata;
* gdi: a track runs to the start of the next one; what its file does not fill is zero frames (``PAD``);
* a DVD ISO is stored as it is (2048-byte units, no frames).

Anything this module is not sure about raises :class:`ImageError` - a wrong guess here would write a wrong CHD.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

__all__ = ["ImageError", "CdTrack", "CdImage", "DvdImage", "open_image"]

FRAME = 2448
SECTOR = 2352
TRACK_PADDING = 4
CD_HUNK_FRAMES = 8
DVD_UNIT = 2048
DVD_HUNK = 4096

# cue mode -> (CHD type, bytes of a sector in the file)
CUE_MODES = {
    "MODE1/2048": ("MODE1", 2048), "MODE1/2352": ("MODE1_RAW", 2352),
    "MODE2/2336": ("MODE2", 2336), "MODE2/2048": ("MODE2_FORM1", 2048), "MODE2/2324": ("MODE2_FORM2", 2324),
    "MODE2/2352": ("MODE2_RAW", 2352), "AUDIO": ("AUDIO", 2352),
}
KIND_ZERO, KIND_DATA, KIND_AUDIO = 0, 1, 2


class ImageError(Exception):
    """The image cannot be read, or has a layout this module does not reproduce exactly."""


@dataclass
class CdTrack:
    number: int
    type: str
    sector: int                     # bytes of one sector in the file
    path: Optional[Path] = None
    offset: int = 0                 # byte offset of the first sector in the file
    file_frames: int = 0            # frames taken from the file
    frames: int = 0                 # frames of the track in the CHD (gdi: with the pad frames)
    pad: int = 0
    pregap: int = 0
    pgtype: str = "MODE1"
    postgap: int = 0

    @property
    def audio(self) -> bool:
        return self.type == "AUDIO"


def _msf(text: str) -> int:
    m = re.fullmatch(r"(\d+):(\d+):(\d+)", text.strip())
    if not m:
        raise ImageError(f"bad time {text!r}")
    mm, ss, ff = (int(x) for x in m.groups())
    if ss >= 60 or ff >= 75:
        raise ImageError(f"bad time {text!r}")
    return (mm * 60 + ss) * 75 + ff


def _swap16(block: bytes) -> bytes:
    b = bytearray(block)
    b[0::2] = block[1::2]
    b[1::2] = block[0::2]
    return bytes(b)


def _as_frames(block: bytes, sector: int, swap: bool) -> bytes:
    """Sectors of ``sector`` bytes as 2448-byte frames."""
    if swap:
        block = _swap16(block)
    fill = bytes(FRAME - sector)
    return fill.join([block[i:i + sector] for i in range(0, len(block), sector)]) + fill


def _size(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except OSError as exc:
        raise ImageError(f"cannot read {Path(path).name}: {exc}") from exc


def parse_cue(path) -> List[CdTrack]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ImageError(f"cannot read {path.name}: {exc}") from exc
    base = path.parent
    tracks: List[CdTrack] = []
    starts: List[Tuple[Path, Optional[int], Optional[int]]] = []      # per track: file, INDEX 00, INDEX 01
    current: Optional[Path] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        word = line.split(None, 1)[0].upper()
        if word == "FILE":
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                raise ImageError(f"bad FILE line in {path.name}") from exc
            if len(parts) < 3 or parts[-1].upper() != "BINARY":
                raise ImageError(f"{path.name}: only BINARY track files can be converted "
                                 f"({parts[-1] if parts else '?'} is not)")
            current = base / " ".join(parts[1:-1])
        elif word == "TRACK":
            parts = line.split()
            if current is None or len(parts) < 3 or not parts[1].isdigit():
                raise ImageError(f"bad TRACK line in {path.name}")
            mode = CUE_MODES.get(parts[2].upper())
            if mode is None:
                raise ImageError(f"{path.name}: track mode {parts[2]} is not supported")
            if int(parts[1]) != len(tracks) + 1:
                raise ImageError(f"{path.name}: tracks are not numbered 1, 2, 3 ...")
            tracks.append(CdTrack(number=len(tracks) + 1, type=mode[0], sector=mode[1], path=current))
            starts.append((current, None, None))
        elif word == "INDEX":
            parts = line.split()
            if not tracks or len(parts) < 3 or not parts[1].isdigit():
                raise ImageError(f"bad INDEX line in {path.name}")
            f, i0, i1 = starts[-1]
            if tracks[-1].path != current:
                raise ImageError(f"{path.name}: a track that continues in another file is not supported")
            if int(parts[1]) == 0:
                starts[-1] = (f, _msf(parts[2]), i1)
            elif int(parts[1]) == 1:
                starts[-1] = (f, i0, _msf(parts[2]))
        elif word in ("PREGAP", "POSTGAP"):
            parts = line.split()
            if not tracks or len(parts) < 2:
                raise ImageError(f"bad {word} line in {path.name}")
            if word == "PREGAP":
                tracks[-1].pregap = _msf(parts[1])
            else:
                tracks[-1].postgap = _msf(parts[1])
        elif word in ("REM", "CATALOG", "FLAGS", "ISRC", "PERFORMER", "TITLE", "SONGWRITER", "CDTEXTFILE"):
            continue
        else:
            raise ImageError(f"{path.name}: unknown cue command {word}")
    if not tracks:
        raise ImageError(f"{path.name} lists no tracks")
    for n, (t, (f, i0, i1)) in enumerate(zip(tracks, starts)):
        if i1 is None:
            raise ImageError(f"{path.name}: track {t.number} has no INDEX 01")
        if i0 is not None:
            if t.pregap or i0 > i1:
                raise ImageError(f"{path.name}: track {t.number} has an unusual pregap")
            t.pregap = i1 - i0
            t.pgtype = "V" + t.type
        start = i1 if i0 is None else i0
        size = _size(f)
        if size % t.sector:
            raise ImageError(f"{f.name} is not a whole number of {t.sector}-byte sectors")
        total = size // t.sector
        first_of_file = n == 0 or tracks[n - 1].path != f
        if first_of_file and start != 0:
            raise ImageError(f"{path.name}: track {t.number} does not start at the beginning of its file")
        if n + 1 < len(tracks) and tracks[n + 1].path == f:
            nf, n0, n1 = starts[n + 1]
            if n1 is None:
                raise ImageError(f"{path.name}: track {t.number + 1} has no INDEX 01")
            if tracks[n + 1].sector != t.sector:
                raise ImageError(f"{path.name}: tracks with different sector sizes share {f.name}")
            end = n1 if n0 is None else n0
        else:
            end = total
        if not start < end <= total:
            raise ImageError(f"{path.name}: track {t.number} lies outside {f.name}")
        t.offset = start * t.sector
        t.file_frames = t.frames = end - start
    return tracks


def parse_gdi(path, text: Optional[str] = None, files: Optional[List[Path]] = None) -> List[CdTrack]:
    """The tracks of a ``.gdi``. ``text``: the sheet's content when it does not exist as a file (generated from a
    Redump ``.cue``); ``files``: the track files to use instead of the names in the sheet, in track order."""
    path = Path(path)
    if text is None:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise ImageError(f"cannot read {path.name}: {exc}") from exc
    lines = [ln for ln in text.splitlines() if ln.strip()]
    try:
        count = int(lines[0].strip())
        rows = [shlex.split(ln) for ln in lines[1:]]
    except (ValueError, IndexError) as exc:
        raise ImageError(f"{path.name} is not a GDI sheet") from exc
    if count != len(rows) or not rows:
        raise ImageError(f"{path.name}: the track count does not match the tracks listed")
    tracks: List[CdTrack] = []
    lbas: List[int] = []
    for n, row in enumerate(rows, 1):
        try:
            num, lba, kind, sector, name, offset = int(row[0]), int(row[1]), int(row[2]), int(row[3]), row[4], int(row[5])
        except (ValueError, IndexError) as exc:
            raise ImageError(f"{path.name}: bad track line {n}") from exc
        if num != n or kind not in (0, 4) or sector not in ((2352,) if kind == 0 else (2352, 2048)):
            raise ImageError(f"{path.name}: track {n} has a layout that is not supported")
        f = Path(files[n - 1]) if files is not None and n <= len(files) else path.parent / name
        size = _size(f) - offset
        if size <= 0 or size % sector:
            raise ImageError(f"{f.name} is not a whole number of {sector}-byte sectors")
        ttype = "AUDIO" if kind == 0 else ("MODE1_RAW" if sector == 2352 else "MODE1")
        tracks.append(CdTrack(number=n, type=ttype, sector=sector, path=f, offset=offset, file_frames=size // sector))
        lbas.append(lba)
    for n, t in enumerate(tracks):
        if n + 1 < len(tracks):
            t.frames = lbas[n + 1] - lbas[n]
            if t.frames < t.file_frames:
                raise ImageError(f"{path.name}: track {t.number} is longer than the gap to track {t.number + 1}")
            t.pad = t.frames - t.file_frames
        else:
            t.frames = t.file_frames
    return tracks


@dataclass
class CdImage:
    """A CD / GD-ROM as chdman stores it."""

    tracks: List[CdTrack]
    gd: bool = False
    cd: bool = True
    hunk_bytes: int = CD_HUNK_FRAMES * FRAME
    unit_bytes: int = FRAME
    read_frames: int = 512              # frames read from a file at a time
    metadata: List[Tuple[bytes, bytes]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.total_frames = sum(t.frames + (-t.frames) % TRACK_PADDING for t in self.tracks)
        self.logical_bytes = self.total_frames * FRAME
        self.source_bytes = sum(t.file_frames * t.sector for t in self.tracks)
        meta = []
        for t in self.tracks:
            if self.gd:
                text = (f"TRACK:{t.number} TYPE:{t.type} SUBTYPE:NONE FRAMES:{t.frames} PAD:{t.pad} "
                        f"PREGAP:{t.pregap} PGTYPE:{t.pgtype} PGSUB:NONE POSTGAP:{t.postgap}")
            else:
                text = (f"TRACK:{t.number} TYPE:{t.type} SUBTYPE:NONE FRAMES:{t.frames} PREGAP:{t.pregap} "
                        f"PGTYPE:{t.pgtype} PGSUB:NONE POSTGAP:{t.postgap}")
            meta.append((b"CHGD" if self.gd else b"CHT2", text.encode("ascii") + b"\0"))
        self.metadata = meta

    def _pieces(self) -> Iterator[Tuple[bytes, int, int]]:
        """``(frames as bytes, how many, kind)`` in disc order."""
        for t in self.tracks:
            kind = KIND_AUDIO if t.audio else KIND_DATA
            left = t.file_frames
            if left:
                try:
                    with open(t.path, "rb") as f:
                        f.seek(t.offset)
                        while left:
                            n = min(left, self.read_frames)
                            block = f.read(n * t.sector)
                            if len(block) != n * t.sector:
                                raise ImageError(f"{t.path.name} is shorter than its track")
                            yield _as_frames(block, t.sector, t.audio), n, kind
                            left -= n
                except OSError as exc:
                    raise ImageError(f"cannot read {t.path.name}: {exc}") from exc
            zeros = t.frames - t.file_frames + (-t.frames) % TRACK_PADDING
            while zeros:
                n = min(zeros, 4096)
                yield bytes(n * FRAME), n, KIND_ZERO
                zeros -= n

    def batches(self, hunks: int) -> Iterator[Tuple[bytes, List[str]]]:
        """``(the bytes of up to ``hunks`` hunks, a hint per hunk)``; the last hunk is filled up with zeros. The
        hint is ``audio`` for a hunk of audio frames only, ``zero`` for one without any sector, else ``data``."""
        per = self.hunk_bytes // FRAME
        want = hunks * per
        parts: List[bytes] = []
        kinds = bytearray()
        have = 0
        for data, n, kind in self._pieces():
            at = 0
            while at < n:
                take = min(n - at, want - have)
                parts.append(data if take == n else data[at * FRAME:(at + take) * FRAME])
                kinds += bytes((kind,)) * take
                have += take
                at += take
                if have == want:
                    yield b"".join(parts), self._hints(kinds, per)
                    parts, kinds, have = [], bytearray(), 0
        if have:
            fill = (-have) % per
            parts.append(bytes(fill * FRAME))
            kinds += bytes(fill)
            yield b"".join(parts), self._hints(kinds, per)

    @staticmethod
    def _hints(kinds: bytearray, per: int) -> List[str]:
        out = []
        for i in range(0, len(kinds), per):
            k = kinds[i:i + per]
            if KIND_DATA in k:
                out.append("data")
            elif KIND_AUDIO in k:
                out.append("audio")
            else:
                out.append("zero")
        return out


@dataclass
class DvdImage:
    """A DVD ISO as ``chdman createdvd`` stores it: the file itself, in 2048-byte units."""

    path: Path
    cd: bool = False
    gd: bool = False
    hunk_bytes: int = DVD_HUNK
    unit_bytes: int = DVD_UNIT

    def __post_init__(self) -> None:
        size = _size(self.path)
        if size == 0 or size % DVD_UNIT:
            raise ImageError(f"{Path(self.path).name} is not a whole number of 2048-byte sectors")
        self.logical_bytes = self.source_bytes = size
        self.metadata = [(b"DVD ", b"\0")]
        self.tracks: List[CdTrack] = []

    def batches(self, hunks: int) -> Iterator[Tuple[bytes, List[str]]]:
        hb = self.hunk_bytes
        try:
            with open(self.path, "rb") as f:
                left = self.logical_bytes
                while left:
                    block = f.read(min(left, hunks * hb))
                    if not block:
                        raise ImageError(f"{Path(self.path).name} is shorter than it was")
                    left -= len(block)
                    block += bytes((-len(block)) % hb)
                    yield block, ["data"] * (len(block) // hb)
        except OSError as exc:
            raise ImageError(f"cannot read {Path(self.path).name}: {exc}") from exc


def open_image(path, mode: str = "createcd", gdi_text: Optional[str] = None, gdi_files: Optional[List[Path]] = None):
    """The image behind ``path`` for ``mode`` = ``createcd`` (``.cue`` / ``.gdi`` / ``.iso``) or ``createdvd``
    (``.iso``). ``gdi_text`` (+ ``gdi_files``): read ``path``'s set as the GD-ROM this generated GDI describes."""
    path = Path(path)
    ext = path.suffix.lower()
    if mode == "createdvd":
        return DvdImage(path)
    if mode != "createcd":
        raise ImageError(f"unknown mode {mode}")
    if gdi_text is not None:
        return CdImage(parse_gdi(path, gdi_text, gdi_files), gd=True)
    if ext == ".cue":
        return CdImage(parse_cue(path))
    if ext == ".gdi":
        return CdImage(parse_gdi(path), gd=True)
    if ext == ".iso":
        size = _size(path)
        if size == 0 or size % 2048:
            raise ImageError(f"{path.name} is not a whole number of 2048-byte sectors")
        return CdImage([CdTrack(number=1, type="MODE1", sector=2048, path=path, file_frames=size // 2048,
                                frames=size // 2048)])
    raise ImageError(f"{path.name}: not a .cue, .gdi or .iso")
