"""TOSEC CD discs (Amiga CD32 / CDTV) as CHDs: identified, converted on Build library, verified.

A TOSEC CD game is the files of one DAT game: a cue sheet and its tracks - one ``.iso`` / ``.bin`` / ``.img`` per data track and a
``.wav`` per audio track ([ISO]), one ``.bin`` holding every track ([BIN]), or a CloneCD set (``.ccd`` / ``.img`` / ``.sub``, mostly
with a cue as well: [IMG]). The DAT has no entry for a CHD, so a CHD is told by what it holds:

Identification
    Every track of the CHD is decoded once (:func:`hash_chd`) and hashed three ways: as the track's own file (``.iso`` / ``.bin``:
    the extracted sectors, as ``chdman extractcd`` writes them), as a ``.wav`` (the same samples behind a standard 44-byte header) and,
    for all tracks together, as the one file of a [BIN] / [IMG] set. A DAT game whose track files are exactly those - one file the
    whole disc, or one per track - is the disc (:func:`identify`); its cue / ccd / sub entries are taken as held (the CHD carries the
    track layout; a CloneCD ``.sub`` is not kept, no emulator of these discs reads it). The hashes are cached per file
    (:func:`cached` / :func:`store`), so a rescan never decodes an unchanged CHD again.

Conversion
    A kept disc (a zip / 7z of its files, or the loose files in one folder) is written as ``<game>.chd`` with the built-in writer
    (:mod:`romorg.chdwrite`, chdman's layout) next to where it is; Build library then puts it where the game goes. The new CHD is
    identified as above against the game's own DAT entries before anything of the original is touched - the check the scan makes
    afterwards, so a converted disc is always recognised. Then the originals are deleted ("Delete the originals after converting")
    or moved to ``_converted_originals/``. Every step is journalled like the other conversions.

A disc whose files cannot all be rebuilt from a CHD (an audio track in an ``.mp3``, a ``.wav`` that is not plain CD audio, no cue
sheet) is not converted: it stays as it is.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Optional, Sequence

from . import cdimage, chdtool, chdwrite, discarchive, organiser, scanner, winproc
from . import chd as chdlib
from .folders import CONVERTED_DIR

ProgressFn = Callable[[int, int, str], None]

TRACK_EXTS = (".iso", ".bin", ".img", ".wav")
SHEET_EXTS = (".cue", ".ccd", ".mds")
SIDE_EXTS = (".sub",)                  # CloneCD subchannel data: not part of a CHD
CHD_EXT = ".chd"
CACHE_SUFFIX = "#cd"                   # hash cache rows of a CHD's tracks (not the hash of the file's own bytes)
CACHE_VERSION = "cd1"
PART_SUFFIX = ".romorg.part"
UNPACK_AHEAD = 2                       # discs unpacked ahead of the one being compressed (threads: LZMA / zlib release the GIL)


def wav_header(length: int) -> bytes:
    """The 44-byte header of a CD audio ``.wav`` of ``length`` bytes of samples."""
    return (b"RIFF" + struct.pack("<I", 36 + length) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 176400, 4, 16)
            + b"data" + struct.pack("<I", length))


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


# --------------------------------------------------------------------------- what a CHD holds

@dataclass
class DiscHashes:
    """The files a CD CHD stands for: per track ``{number, audio, size, crc, sha1, wav_crc, wav_sha1}`` and the whole disc."""
    tracks: list
    size: int = 0
    crc: str = ""
    sha1: str = ""

    def to_json(self) -> str:
        return json.dumps({"tracks": self.tracks, "size": self.size, "crc": self.crc, "sha1": self.sha1}, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Optional["DiscHashes"]:
        try:
            d = json.loads(text)
            return cls(list(d["tracks"]), int(d["size"]), str(d["crc"]), str(d["sha1"]))
        except (ValueError, KeyError, TypeError):
            return None


def is_cd_chd(path: Path) -> bool:
    """True for a CHD of a CD (not a DVD, not a hard disk); False for anything the reader cannot open."""
    try:
        with chdlib.Chd(path, load_map=False) as info:
            return bool(info.is_cd) and not info.is_gd and bool(info.tracks)
    except (chdlib.ChdError, OSError, ValueError):
        return False


def hash_chd(path: Path, cancel: Optional[Callable[[], bool]] = None, progress: Optional[Callable[[int], None]] = None) -> DiscHashes:
    """Decode every track of a CD CHD once and hash it as its own file, as a ``.wav`` (audio) and as part of the whole disc.
    Raises :class:`romorg.chd.ChdError` / OSError; ``InterruptedError`` when ``cancel()`` says so."""
    whole_crc, whole_sha1, whole_size = 0, hashlib.sha1(), 0
    tracks = []
    with chdlib.Chd(path) as info:
        if not info.is_cd or info.is_gd:
            raise chdlib.ChdError("not a CD")
        for tr in info.tracks:
            crc, sha1, size = 0, hashlib.sha1(), 0
            wcrc, wsha1 = 0, None
            if tr.is_audio:
                head = wav_header(tr.size)
                wcrc, wsha1 = zlib.crc32(head), hashlib.sha1(head)
            for chunk in info.iter_track(tr, cancel=cancel):
                crc = zlib.crc32(chunk, crc)
                sha1.update(chunk)
                whole_crc = zlib.crc32(chunk, whole_crc)
                whole_sha1.update(chunk)
                if wsha1 is not None:
                    wcrc = zlib.crc32(chunk, wcrc)
                    wsha1.update(chunk)
                size += len(chunk)
                if progress:
                    progress(len(chunk))
            whole_size += size
            row = {"number": tr.number, "audio": tr.is_audio, "size": size, "crc": f"{crc & 0xFFFFFFFF:08x}", "sha1": sha1.hexdigest()}
            if wsha1 is not None:
                row.update(wav_crc=f"{wcrc & 0xFFFFFFFF:08x}", wav_sha1=wsha1.hexdigest())
            tracks.append(row)
    return DiscHashes(tracks, whole_size, f"{whole_crc & 0xFFFFFFFF:08x}", whole_sha1.hexdigest())


def cached(cache: "scanner.HashCache", path: Path, st: os.stat_result) -> Optional[DiscHashes]:
    key = scanner._cache_key(path)
    if key is None:
        return None
    row = cache.get_alt(key + CACHE_SUFFIX, st.st_size, st.st_mtime_ns, "", CACHE_VERSION, scanner.file_ident(st))
    return DiscHashes.from_json(row[1]) if row is not None else None


def store(cache: "scanner.HashCache", path: Path, st: os.stat_result, hashes: DiscHashes) -> None:
    key = scanner._cache_key(path)
    if key is not None:                 # (one row: the hashes of every track, as JSON in the sha1 column)
        cache.put_alt(key + CACHE_SUFFIX, st.st_size, st.st_mtime_ns, "", CACHE_VERSION, "", hashes.to_json(), scanner.file_ident(st))


def _same(rom: Any, crc: str, sha1: str, size: int) -> bool:
    return rom.size == size and (rom.crc or "").lower() == crc and (not rom.sha1 or rom.sha1.lower() == sha1)


def disc_parts(roms: Sequence[Any]) -> Optional[tuple[list, list]]:
    """``(track files, the others: sheets and subchannel data)`` of a DAT game, None when it is no disc a CHD can stand for
    (no track file, or a file of another kind: an ``.mp3`` track, a manual)."""
    tracks = [r for r in roms if _ext(r.name) in TRACK_EXTS]
    rest = [r for r in roms if _ext(r.name) not in TRACK_EXTS]
    if not tracks or any(_ext(r.name) not in SHEET_EXTS + SIDE_EXTS for r in rest):
        return None
    return tracks, rest


def matches_game(h: DiscHashes, roms: Sequence[Any]) -> bool:
    """True when the CHD of ``h`` is exactly the disc of these DAT entries (one game): its one file is the whole disc, or each
    track is one of its files (``.wav`` with the standard header)."""
    parts = disc_parts(roms)
    if parts is None:
        return False
    tracks, _rest = parts
    if len(tracks) == 1 and _same(tracks[0], h.crc, h.sha1, h.size):
        return True
    if len(tracks) != len(h.tracks):
        return False
    left = list(tracks)
    for t in h.tracks:
        hit = next((r for r in left if _same(r, t["crc"], t["sha1"], t["size"])
                    or (t.get("wav_crc") and _same(r, t["wav_crc"], t["wav_sha1"], t["size"] + 44))), None)
        if hit is None:
            return False
        left.remove(hit)
    return True


def identify(h: DiscHashes, dats: Iterable[Any]) -> Optional[list]:
    """Every DAT entry of the game the CHD of ``h`` is (the first DAT in ``dats`` order that has it), or None."""
    keys = {(h.crc, h.size)}
    for t in h.tracks:
        keys.add((t["crc"], t["size"]))
        if t.get("wav_crc"):
            keys.add((t["wav_crc"], t["size"] + 44))
    for dat in dats:
        idx = dat.by_crc_size()
        games = []
        for k in keys:
            for r in idx.get(k, ()):
                if r.game not in games:
                    games.append(r.game)
        by_game = None
        for game in sorted(games):
            if by_game is None:
                by_game = dat.games()
            roms = by_game.get(game) or []
            if matches_game(h, roms):
                return list(roms)
    return None


# --------------------------------------------------------------------------- conversion

@dataclass
class CdConvertOp:
    src: Path                       # the zip / 7z of the disc, or its cue sheet (loose files)
    member: Optional[str]
    dst: Path                       # the new CHD: <folder of src>/<game>.chd
    original_dst: Path              # where the original goes (_converted_originals/...) when it is kept
    status: str                     # convert | conflict | skip
    reason: str = ""
    rom_name: str = ""              # the game
    via: str = "builtin"
    moves: list = field(default_factory=list)   # [(original file, _converted_originals/...)]
    raw_bytes: int = 0
    roms: list = field(default_factory=list)    # the game's DAT entries (what the new CHD is checked against)
    sheet: str = ""                 # the cue sheet (or the one .iso) the CHD is written from: a member name, or a path
    archive: bool = False
    members: list = field(default_factory=list)  # archive members to unpack
    deleted: list = field(default_factory=list)
    left: list = field(default_factory=list)     # originals that could not be deleted (``delete_originals``): left where they are
    delete_originals: bool = False
    mode: str = "createcd"
    note: str = ""


def convertible_reason(roms: Sequence[Any]) -> str:
    """Why this disc cannot be converted so that the CHD is checked against the DAT ("" = it can)."""
    parts = disc_parts(roms)
    if parts is None:
        kinds = sorted({_ext(r.name) for r in roms if _ext(r.name) not in TRACK_EXTS + SHEET_EXTS + SIDE_EXTS})
        return f"it has {', '.join(kinds) or 'no'} track files, which a CHD cannot be checked against"
    tracks, rest = parts
    cues = [r for r in rest if _ext(r.name) == ".cue"]
    if len(cues) > 1:
        return "it has more than one cue sheet"
    if not cues and not (len(tracks) == 1 and _ext(tracks[0].name) == ".iso"):
        return "it has no cue sheet"
    for r in tracks:
        e = _ext(r.name)
        if e == ".wav" and (r.size - 44) % cdimage.SECTOR:
            return f"{r.name} is not plain CD audio: the CHD could not be checked against it"
        if e in (".bin", ".img") and r.size % cdimage.SECTOR:
            return f"{r.name} is not a whole number of sectors"
    return ""


def plan_conversions(result: Any, disc_dats: Iterable[str], keep: Optional[Callable[[Path], bool]] = None) -> list[CdConvertOp]:
    """One op per disc of ``disc_dats`` the scan found whole - a zip / 7z holding nothing but its files, or its loose files in one
    folder. ``keep(path)``: False for a disc the library rules set aside (it is not converted; None = every disc)."""
    root = Path(result.root)
    disc_dats = set(disc_dats)
    dats = {d.name: d for d in getattr(result, "dats", ()) or ()}
    archives: dict[Path, list] = {}
    loose: dict[tuple, list] = {}
    for m in result.matched:
        if m.dat not in disc_dats or getattr(m, "container", "") == "chd":
            continue
        p = Path(m.entry.path)
        p = p if p.is_absolute() else root / p
        if scanner.is_converted_original(p, root) or scanner.is_in_reason_dir(p, root):
            continue                                        # (an original a conversion kept; a disc the rules set aside)
        if m.entry.member is not None:
            archives.setdefault(p, []).append(m)
        else:
            loose.setdefault((p.parent, m.dat, m.primary[0].game), []).append(m)
    members: dict[Path, int] = {}                           # (every member of an archive, whatever DAT matched it, or none)
    for e in [m.entry for m in result.matched] + list(result.unmatched):
        if e.member is not None:
            ap = Path(e.path) if Path(e.path).is_absolute() else root / e.path
            members[ap] = members.get(ap, 0) + 1
    ops: list[CdConvertOp] = []
    for p, ms in sorted(archives.items()):
        games = {(m.dat, r.game) for m in ms for r in m.primary}
        game_of = [g for g in games if all(any((m.dat, r.game) == g for r in m.primary) for m in ms)]
        if len(game_of) != 1 or members.get(p, 0) != len(ms) or p.suffix.lower() not in (".zip", ".7z"):
            continue                                        # (an archive that holds anything but the disc is never converted: nor deleted)
        dat, game = game_of[0]
        roms = dats[dat].games().get(game, []) if dat in dats else []
        names = {r.name for r in roms}
        held = {m.entry.member for m in ms}
        if not roms or names - held or len(held) != len(ms):
            continue                                        # (not the whole disc: nothing to convert)
        sheet = next((r.name for r in roms if _ext(r.name) == ".cue"), "") or next(r.name for r in roms if _ext(r.name) in TRACK_EXTS)
        want = [r.name for r in roms if _ext(r.name) in TRACK_EXTS or r.name == sheet]
        op = _op(root, p, [p], game, roms, keep)
        op.archive, op.sheet, op.members = True, sheet, want
        ops.append(op)
    for (folder, dat, game), ms in sorted(loose.items(), key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2])):
        roms = dats[dat].games().get(game, []) if dat in dats else []
        by_name = {}
        for m in ms:
            r = next(r for r in m.primary if r.game == game)
            by_name.setdefault(r.name, Path(m.entry.path) if Path(m.entry.path).is_absolute() else root / m.entry.path)
        if not roms or {r.name for r in roms} - set(by_name):
            continue
        if any(by_name[r.name].name != r.name for r in roms if _ext(r.name) == ".cue" or _ext(r.name) in TRACK_EXTS):
            continue                                        # (the cue names its tracks: they must have the DAT's names)
        sheet_rom = next((r for r in roms if _ext(r.name) == ".cue"), None) or next(r for r in roms if _ext(r.name) in TRACK_EXTS)
        files = [by_name[r.name] for r in roms]
        op = _op(root, by_name[sheet_rom.name], files, game, roms, keep)
        op.sheet = str(by_name[sheet_rom.name])
        ops.append(op)
    return ops


def _op(root: Path, src: Path, files: list[Path], game: str, roms: list, keep: Optional[Callable[[Path], bool]]) -> CdConvertOp:
    dst = src.parent / organiser.safe_filename(game + CHD_EXT)
    moves = [(f, root / CONVERTED_DIR / f.relative_to(root)) for f in files]
    op = CdConvertOp(src, None, dst, moves[0][1], "convert", rom_name=game, moves=moves, roms=list(roms),
                     raw_bytes=sum(r.size for r in roms))
    why = convertible_reason(roms)
    if keep is not None and not keep(src):
        op.status, op.reason = "skip", "the library rules set this disc aside: it is not converted"
    elif why:
        op.status, op.reason = "skip", f"not converted: {why}"
    elif organiser._exists(dst):
        op.status, op.reason = "conflict", f"{dst.name} is there already"
    return op


def _unpack(op: CdConvertOp, n: int, cancel: Any, progress: Optional[ProgressFn] = None) -> tuple[Path, Path]:
    """Take the disc of ``op`` out of its zip / 7z into a work folder next to where the CHD goes (the same file system): ``(the work
    folder, the cue sheet in it)``. Raises :class:`chdtool.ChdmanError`. Runs ahead of the conversions, in a thread (``n``: a work folder
    of its own) - LZMA decoding releases the GIL, so the next discs unpack while this one is compressed."""
    unpacked = op.dst.parent / (chdtool.TEMP_PREFIX + f"unpack-{n}")
    if unpacked.exists():
        shutil.rmtree(unpacked, ignore_errors=True)
    is_cancelled = lambda: organiser._is_cancelled(cancel)  # noqa: E731
    try:
        listed = discarchive.list_members(op.src)
        extra = sorted({m.name for m in listed} - {r.name for r in op.roms})
        if extra:                               # (checked again before anything is written: deleting the archive must lose nothing)
            raise chdtool.ChdmanError(f"{op.src.name} holds other files too ({extra[0]}): it is not converted")
        wanted = [m for m in listed if m.name in set(op.members)]
        done = [0]

        def took(k: int) -> None:
            done[0] += k
            if progress:
                progress(done[0] >> 20, max(1, op.raw_bytes >> 20), f"{op.rom_name}: Unpacking {op.src.name}")
        try:
            out = discarchive.extract_members(op.src, wanted, unpacked, progress=took, cancel=is_cancelled)
        except scanner.ScanCancelled:
            raise chdtool.ChdmanError("cancelled", cancelled=True) from None
        except discarchive.ArchiveError as exc:
            raise chdtool.ChdmanError(f"could not unpack {op.src.name}: {exc}") from exc
        return unpacked, Path(out[op.sheet])
    except BaseException:
        shutil.rmtree(unpacked, ignore_errors=True)
        raise


def _convert_one(op: CdConvertOp, root: Path, journal: Any, created: list[str], progress: Optional[ProgressFn], cancel: Any,
                 preset: str, workers: Optional[int], prepared: Any = None) -> None:
    """``prepared``: the future of the disc's :func:`_unpack` started ahead (None: it is unpacked here)."""
    for s, _d in op.moves:
        if not organiser._exists(s):
            raise FileNotFoundError(f"source missing: {s}")
    if organiser._exists(op.dst):
        raise FileExistsError(f"target exists: {op.dst}")
    chdtool.check_space(op.dst.parent, op.raw_bytes * (2 if op.archive else 1))
    new = op.dst.with_name(op.dst.name + PART_SUFFIX)
    unpacked: Optional[Path] = None
    placed = False
    moved: list[tuple[Path, Path]] = []
    label = op.rom_name
    is_cancelled = lambda: organiser._is_cancelled(cancel)  # noqa: E731
    try:
        if new.is_file() and not new.is_symlink():
            new.unlink()                      # a leftover of a crashed run (our own distinctive name)
        sheet = Path(op.sheet)
        if op.archive:
            if prepared is not None:
                if progress and not prepared.done():
                    progress(0, 1, f"{label}: Unpacking {op.src.name}")
                unpacked, sheet = prepared.result()
            else:
                unpacked, sheet = _unpack(op, 0, cancel, progress)

        def report(done: int, total: int) -> None:
            if progress:
                progress(done, total, f"{label}: Compressing, {100 * done // max(1, total)}% complete")
        try:
            image = cdimage.open_image(sheet, "createcd")
            if os.environ.get(chdwrite.ENV_THREADS, "").strip().isdigit():
                workers = None
            chdwrite.write_chd(new, image, preset=preset, progress=report, threads=workers, cancel=is_cancelled)
        except chdwrite.Cancelled:
            raise chdtool.ChdmanError("cancelled", cancelled=True) from None
        except cdimage.ImageError as exc:
            raise chdtool.ChdmanError(f"this disc cannot be converted: {exc}") from exc
        except chdwrite.ChdWriteError as exc:
            raise chdtool.ChdmanError(f"could not write the CHD: {exc}") from exc
        # check the new CHD against the DAT before anything of the original is touched (the test the scan makes)
        if progress:
            progress(0, 1, f"{label}: Checking the CHD")
        try:
            got = hash_chd(new, cancel=is_cancelled)
        except InterruptedError:
            raise chdtool.ChdmanError("cancelled", cancelled=True) from None
        if not matches_game(got, op.roms):
            raise chdtool.ChdmanError("verification failed: the new CHD does not rebuild the DAT's files - nothing was changed")
        sha1 = hashlib.sha1()
        with open(new, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                sha1.update(block)
        seq = journal.record({"op": "create", "path": organiser.rel_str(op.dst, root), "sha1": sha1.hexdigest(),
                              "size": new.stat().st_size}, sync=True)
        try:
            organiser.move_exclusive(new, op.dst)
        except BaseException:
            journal.failed(seq)
            raise
        placed = True
        journal.useful = True
        if op.delete_originals:
            # the originals are deleted: the CHD was checked against the DAT above. Undo could not bring them back, so it must never
            # take the CHD away either: its record is marked as not done. From here on the CHD stays whatever happens: an original that
            # cannot be deleted (open elsewhere, read-only) is left where it is and reported, never a reason to undo the conversion
            journal.failed(seq)
            for s, _d in op.moves:
                try:
                    os.unlink(s)
                    op.deleted.append(s)
                except OSError as exc:
                    op.left.append(f"{s}: {exc}")
            op.moves = []
        for s, d in op.moves:                 # keep the originals: each moves to _converted_originals/ (journalled one by one)
            organiser.make_dirs(d.parent, created, journal)
            mseq = journal.record({"op": "move", "src": organiser.rel_str(s, root), "dst": organiser.rel_str(d, root)})
            try:
                organiser.rename_no_overwrite(s, d)
            except BaseException:
                journal.failed(mseq)
                raise
            moved.append((s, d))
    except BaseException as exc:
        for s, d in list(reversed(moved)):    # roll back: originals back, new CHD removed
            try:
                rseq = journal.record({"op": "move", "src": organiser.rel_str(d, root), "dst": organiser.rel_str(s, root)})
                try:
                    organiser.rename_no_overwrite(d, s)
                except BaseException:
                    journal.failed(rseq)
                    raise
                moved.remove((s, d))
            except (OSError, ValueError):
                pass
        if placed and not moved:
            try:
                os.unlink(op.dst)
            except OSError:
                pass
        if isinstance(exc, organiser.UndoLogError):
            raise
        raise
    finally:
        if unpacked is not None:
            shutil.rmtree(unpacked, ignore_errors=True)
        try:
            if new.is_file() and not new.is_symlink():
                new.unlink()                   # failed / cancelled: no half-written CHD stays behind
        except OSError:
            pass


def apply_conversions(ops: Iterable[CdConvertOp], root: Path, progress: Optional[ProgressFn] = None, cancel: Any = None,
                      preset: str = "default", workers: Optional[int] = None) -> dict[str, Any]:
    """Convert the ``convert`` ops: write -> check against the DAT -> place -> delete or keep the originals."""
    root = Path(root)
    todo = [o for o in ops if o.status == "convert"]
    journal = organiser.Journal(root)
    converted: list[CdConvertOp] = []
    failed: list[dict[str, str]] = []
    created: list[str] = []
    removed: list[str] = []
    cancelled = False
    error: Optional[str] = None
    ahead: dict[int, Any] = {}                         # op index -> the future of its unpacking, started ahead
    pool = ThreadPoolExecutor(max_workers=UNPACK_AHEAD, thread_name_prefix="romorg-unpack")

    def start(k: int) -> None:
        if k < len(todo) and k not in ahead and todo[k].archive:
            ahead[k] = pool.submit(_unpack, todo[k], k + 1, cancel)
    try:
        chdtool.sweep_stale(root)
        for i, op in enumerate(todo):
            if organiser._is_cancelled(cancel):
                cancelled = True
                break
            if progress:
                progress(i, len(todo), op.rom_name)
            for k in range(i, i + UNPACK_AHEAD + 1):         # (this disc and the next ones unpack while this one is compressed)
                start(k)
            try:
                _convert_one(op, root, journal, created, progress, cancel, preset, workers, ahead.pop(i, None))
                converted.append(op)
            except organiser.UndoLogError:
                raise
            except chdtool.ChdmanError as exc:
                if exc.cancelled:
                    cancelled = True
                    break
                failed.append({"src": str(op.src), "dst": str(op.dst), "error": str(exc)})
            except (OSError, ValueError, chdlib.ChdError) as exc:
                failed.append({"src": str(op.src), "dst": str(op.dst), "error": winproc.long_path_hint(exc, op.src, op.dst)})
        protected = organiser.protected_dirs(root, [], set())
        sources = ([Path(s).parent for op in converted for s, _d in op.moves] + [Path(s).parent for op in converted for s in op.deleted]
                   + [Path(c) for c in created])
        mine = set(created)
        for d in organiser.remove_empty_dirs(sources, root, protected):
            if d in mine:
                created.remove(d)
                mine.discard(d)
                journal.record({"op": "unmkdir", "path": organiser.rel_str(Path(d), root)})
            else:
                journal.record({"op": "rmdir", "path": organiser.rel_str(Path(d), root)})
                journal.useful = True
                removed.append(d)
    except organiser.UndoLogError as exc:
        error = str(exc)
    finally:
        for fut in ahead.values():                     # (unpacked ahead for discs that were not converted: their work folders go)
            fut.cancel()
        pool.shutdown(wait=True)
        for fut in ahead.values():
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                shutil.rmtree(fut.result()[0], ignore_errors=True)
        log_path = journal.close()
    return {"converted": len(converted), "failed": failed, "removed_dirs": removed,
            "undo_log": str(log_path) if log_path else None, "cancelled": cancelled, "error": error,
            "deleted": sum(len(o.deleted) for o in converted), "not_deleted": [x for o in converted for x in o.left],
            "from_archives": sum(1 for o in converted if o.archive),
            "written_by": {"builtin": len(converted)} if converted else {}, "preset": preset}
