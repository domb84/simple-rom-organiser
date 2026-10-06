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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

__all__ = ["ImageError", "CdTrack", "CdImage", "DvdImage", "open_image", "read_sheet", "sheet_lines", "tokenize",
           "sheet_quote", "atoi"]

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


# --------------------------------------------------------------------------- reading sheets the way chdman does
# chdman 0.289 (MAME's chdcd.cpp) reads cue and gdi sheets with its own tokenizer, and the built-in writer must
# read every sheet exactly like it: a sheet read differently would give a CHD that is not chdman's. What chdman
# does was established with crafted sheets against chdman 0.289 itself (tests/test_chdwrite.py, SheetSyntaxTest):
#
# * a line ends at "\n" only; tokens are separated by ASCII white space (space, tab, CR, VT, FF);
# * '"' and "'" both quote (the other kind is literal inside), the quote characters are dropped, there are no
#   escapes: ``FILE "Tony's Game.bin"`` is fine, ``FILE Tony's.bin`` is not (the quote never closes);
# * keywords are case-sensitive (``file`` / ``pregap`` are not commands) and unknown commands are ignored;
# * numbers are read with C's ``atoi`` ("01x" is 1) and times with ``sscanf("%d:%d:%d")`` (a single number is
#   frames, "0:0" is 0, minutes / seconds / frames are not range-checked).
#
# Two deliberate differences, both where chdman 0.289 refuses the sheet (so there is no chdman CHD to differ from):
# a UTF-8 byte order mark is skipped (chdman reads it as part of the first word), and a sheet that is not valid
# UTF-8 is read in the Windows ANSI code page (chdman cannot open such names on Windows). On POSIX such a sheet's
# names stay the raw bytes, as chdman uses them there.
_WS = " \t\n\v\f\r"                 # C isspace() in the "C" locale


def read_sheet(path) -> str:
    """The text of a cue / gdi sheet (see above for the encoding rules). Raises OSError."""
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        if os.name == "nt":
            return raw.decode("mbcs", errors="replace")         # the ANSI code page (CP_ACP)
        return raw.decode("utf-8", errors="surrogateescape")     # = os.fsdecode of the bytes chdman opens


def sheet_lines(text: str) -> List[str]:
    """The lines of a sheet as chdman's ``fgets`` sees them (not :meth:`str.splitlines`, which also breaks at
    form feeds, U+0085, U+2028 ...)."""
    return text.split("\n")


def _token(line: str, i: int) -> Tuple[str, int]:
    """chdman's ``tokenize()``: the token starting at ``line[i:]`` (after white space) and where it ends."""
    n = len(line)
    while i < n and line[i] in _WS:
        i += 1
    out = []
    single = double = False
    while i < n:
        c = line[i]
        if not single and c == '"':
            double = not double
        elif not double and c == "'":
            single = not single
        elif not single and not double and c in _WS:
            break
        else:
            out.append(c)
        i += 1
    return "".join(out), i


def tokenize(line: str) -> List[str]:
    """All tokens of a sheet line, as chdman splits it (quotes group and are dropped; ``""`` is an empty token)."""
    out: List[str] = []
    i, n = 0, len(line)
    while True:
        while i < n and line[i] in _WS:
            i += 1
        if i >= n:
            return out
        tok, i = _token(line, i)
        out.append(tok)


def sheet_quote(name: str) -> Optional[str]:
    """``name`` as one token of a cue / gdi line for chdman's tokenizer: in double quotes, or in single quotes when
    the name has a double quote (POSIX allows one; inside ``'...'`` it is literal). None when the name has both
    kinds of quote (chdman cannot read such a name from a sheet at all) or a line break."""
    if "\n" in name or "\r" in name:
        return None
    if '"' not in name:
        return f'"{name}"'
    if "'" not in name:
        return f"'{name}'"
    return None


def _c_int(text: str, i: int = 0) -> Tuple[Optional[int], int]:
    """C ``%d`` at ``text[i:]``: white space, a sign, decimal digits. ``(None, i)`` when there is no number."""
    n = len(text)
    j = i
    while j < n and text[j] in _WS:
        j += 1
    k = j + 1 if j < n and text[j] in "+-" else j
    e = k
    while e < n and "0" <= text[e] <= "9":
        e += 1
    if e == k:
        return None, i
    return int(text[j:e]), e


def atoi(text: str) -> int:
    """C ``atoi``: the leading number of ``text``, 0 when there is none."""
    v, _ = _c_int(text)
    return v or 0


def msf_frames(text: str) -> int:
    """chdman's ``msf_to_frames``: ``sscanf("%d:%d:%d")``; one number alone is frames, missing parts are 0."""
    vals: List[int] = []
    i = 0
    for k in range(3):
        if k:
            if i < len(text) and text[i] == ":":
                i += 1
            else:
                break
        v, i2 = _c_int(text, i)
        if v is None:
            break
        vals.append(v)
        i = i2
    if len(vals) == 1:
        return vals[0]
    m, s, f = (vals + [0, 0, 0])[:3]
    return (m * 60 + s) * 75 + f


def _msf(text: str) -> int:
    frames = msf_frames(text)
    if frames < 0:
        raise ImageError(f"bad time {text!r}")
    return frames


def gd_area_marker(line: str) -> str:
    """``"sd"`` / ``"hd"`` when chdman 0.289 reads ``line`` as a Redump GD-ROM area marker (``REM SINGLE-DENSITY
    AREA`` / ``REM HIGH-DENSITY AREA``: case-sensitive, a prefix of what follows ``REM``), else ``""``."""
    word, i = _token(line, 0)
    if word != "REM":
        return ""
    rest = line[i:].lstrip(_WS)
    if rest.startswith("SINGLE-DENSITY AREA"):
        return "sd"
    if rest.startswith("HIGH-DENSITY AREA"):
        return "hd"
    return ""


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
    """The tracks of a ``.cue``, read as chdman 0.289 reads it (see :func:`read_sheet` / :func:`tokenize`)."""
    path = Path(path)
    try:
        text = read_sheet(path)
    except OSError as exc:
        raise ImageError(f"cannot read {path.name}: {exc}") from exc
    base = path.parent
    tracks: List[CdTrack] = []
    starts: List[Tuple[Path, Optional[int], Optional[int]]] = []      # per track: file, INDEX 00, INDEX 01
    current: Optional[Path] = None
    for line in sheet_lines(text):
        parts = tokenize(line)
        if not parts:
            continue
        word = parts[0]                     # case-sensitive, as in chdman: "file" / "pregap" are not commands
        if word == "FILE":
            kind = parts[2] if len(parts) > 2 else ""
            if kind != "BINARY":
                raise ImageError(f"{path.name}: only BINARY track files can be converted "
                                 f"({kind or 'no type'} is not)")
            if not parts[1]:
                raise ImageError(f"bad FILE line in {path.name}")
            current = base / parts[1]
        elif word == "TRACK":
            if current is None or len(parts) < 2:
                raise ImageError(f"bad TRACK line in {path.name}")
            mode = CUE_MODES.get(parts[2] if len(parts) > 2 else "")
            if mode is None:
                raise ImageError(f"{path.name}: track mode {parts[2] if len(parts) > 2 else '(none)'} "
                                 f"is not supported")
            if atoi(parts[1]) != len(tracks) + 1:
                raise ImageError(f"{path.name}: tracks are not numbered 1, 2, 3 ...")
            tracks.append(CdTrack(number=len(tracks) + 1, type=mode[0], sector=mode[1], path=current))
            starts.append((current, None, None))
        elif word == "INDEX":
            if not tracks or len(parts) < 2:
                raise ImageError(f"bad INDEX line in {path.name}")
            f, i0, i1 = starts[-1]
            if tracks[-1].path != current:
                raise ImageError(f"{path.name}: a track that continues in another file is not supported")
            num, at = atoi(parts[1]), _msf(parts[2] if len(parts) > 2 else "")
            if num == 0:
                starts[-1] = (f, at, i1)
            elif num == 1:
                starts[-1] = (f, i0, at)
        elif word in ("PREGAP", "POSTGAP"):
            if not tracks:
                raise ImageError(f"bad {word} line in {path.name}")
            if word == "PREGAP":
                tracks[-1].pregap = _msf(parts[1] if len(parts) > 1 else "")
            else:
                tracks[-1].postgap = _msf(parts[1] if len(parts) > 1 else "")
        elif word == "REM" and gd_area_marker(line):
            # chdman 0.289 makes a GD-ROM of a Redump Dreamcast cue, moving each pregap to the end of the track
            # before it - a layout this module does not reproduce (the app converts such a cue through a .gdi)
            raise ImageError(f"{path.name} is a GD-ROM cue sheet (REM SINGLE-DENSITY / HIGH-DENSITY AREA); "
                             f"it needs a .gdi")
        # anything else (REM, FLAGS, CATALOG, TITLE, lowercase words ...) is ignored, as chdman ignores it
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
    """The tracks of a ``.gdi``, read as chdman 0.289 reads it: the first line's number is the track count, every
    other non-blank line has exactly six fields (number, LBA, type, sector size, file, offset), and the offset is
    ignored (chdman reads every track file from its start). Tracks listed out of order are refused (chdman takes
    them, with a different layout). ``text``: the sheet's content when it does not exist as a file (generated from
    a Redump ``.cue``); ``files``: the track files to use instead of the names in the sheet, in track order."""
    path = Path(path)
    if text is None:
        try:
            text = read_sheet(path)
        except OSError as exc:
            raise ImageError(f"cannot read {path.name}: {exc}") from exc
    lines = sheet_lines(text)
    count = atoi(lines[0])
    if count <= 0:
        raise ImageError(f"{path.name} is not a GDI sheet (no track count on its first line)")
    rows = [r for r in (tokenize(ln) for ln in lines[1:]) if r]
    if count != len(rows):
        raise ImageError(f"{path.name}: the track count does not match the tracks listed")
    tracks: List[CdTrack] = []
    lbas: List[int] = []
    for n, row in enumerate(rows, 1):
        if len(row) != 6:
            raise ImageError(f"{path.name}: track line {n} has {len(row)} fields, not 6 "
                             f"(a file name with spaces must be in quotes)")
        num, lba, kind, sector, name = atoi(row[0]), atoi(row[1]), atoi(row[2]), atoi(row[3]), row[4]
        if num != n or kind not in (0, 4) or sector not in ((2352,) if kind == 0 else (2352, 2048)):
            raise ImageError(f"{path.name}: track {n} has a layout that is not supported")
        if not name and files is None:
            raise ImageError(f"{path.name}: track {n} names no file")
        f = Path(files[n - 1]) if files is not None and n <= len(files) else path.parent / name
        offset = 0
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
