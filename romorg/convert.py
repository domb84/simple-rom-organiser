"""Optional "Convert to No-Intro format" step (AMENDMENT 4).

Files that matched their DAT only through an alternate hash are rewritten in the
DAT's own format, losslessly checked against the DAT:

* SNES copier-headered dumps (``headerless`` match, DAT rom ``.sfc``): the
  512-byte header is dropped (``strip512``) -> clean ``<rom name>.sfc``.
* N64 byte-swapped dumps (``byteswapped`` match, DAT rom ``.z64``): ``.v64``
  (``swap16``) / ``.n64`` (``swap32``) -> big-endian ``<rom name>.z64``. A dump that
  matches one of the DAT's own ``.v64`` roms raw is converted to the ``.z64`` rom of
  the same set the same way (when the set has one).

NES headerless matches are never offered (emulators need the iNES header).
Loose files and single-member ``.zip`` archives are converted (a zip becomes a
new zip holding the clean rom); 7z/rar and multi-member zips are skipped.

Per file: the original is first moved to
``<root>/_converted_originals/<its rel path>``, the clean file is
written to a temporary name (``<clean name>.romorg-convert-xxxxxxxx``) and its hash compared with the DAT (sha1, else crc)
and size - re-read from disk - and only then moved to its final name. Any
mismatch or error deletes the temporary file and moves the original back.
"""

from __future__ import annotations

import contextlib
import lzma
import os
import uuid
import zipfile
import zlib
import hashlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Iterable, Iterator, Optional

from . import folders, organiser, scanner
from .datfile import archive_stem

if TYPE_CHECKING:
    from .datfile import Rom
    from .scanner import Match, ScanResult

ProgressFn = Callable[[int, int, str], None]

CONVERT = "convert"
CONFLICT = "conflict"
SKIP = "skip"
STATUSES = (CONVERT, CONFLICT, SKIP)
STRIP512 = "strip512"
SWAP16 = "swap16"
SWAP32 = "swap32"
CONVERTED_DIR = scanner.CONVERTED_DIR
CONVERT_TEMP_MARKER = scanner.CONVERT_TEMP_MARKER
CHUNK_SIZE = scanner.CHUNK_SIZE


class ConvertError(RuntimeError):
    """The converted content does not match the DAT (nothing is kept)."""


@dataclass
class ConvertOp:
    src: Path                  # loose file or single-member .zip
    member: Optional[str]      # zip member, else None
    dst: Path                  # clean file (zip: <set>.zip holding rom.name)
    original_dst: Path         # root/_converted_originals/<rel of src>
    status: str                # convert | conflict | skip
    reason: str = ""
    rom_name: str = ""
    via: str = ""              # headerless | byteswapped
    transform: str = ""        # strip512 | swap16 | swap32
    expected_sha1: str = ""
    expected_size: int = 0
    expected_crc: str = ""     # used when the DAT has no sha1
    dat: str = ""              # DAT of the rom (protects its folder from cleanup)
    container: str = ""        # what is written: "" (as the source: a loose file / a zip) | "7z" (the rule "Compress games as 7z")
    repack: bool = False       # nothing of the rom changes, only its packing: the original is deleted once the new file is checked
    deleted: bool = False      # (repack) the original was deleted
    left: str = ""             # (repack) the original could not be deleted: why


# --------------------------------------------------------------------------- transform


def transform_stream(src: BinaryIO, dst: BinaryIO, transform: str) -> tuple[str, str, int]:
    """Copy ``src`` to ``dst`` through ``transform`` (``strip512`` / ``swap16`` / ``swap32``).

    Returns ``(crc, sha1, bytes written)`` of what was written.
    """
    t = scanner.StreamTransform(transform)
    crc = 0
    sha = hashlib.sha1()
    n = 0

    def write(data: bytes) -> None:
        nonlocal crc, n
        if data:
            dst.write(data)
            crc = zlib.crc32(data, crc)
            sha.update(data)
            n += len(data)

    while True:
        chunk = src.read(CHUNK_SIZE)
        if not chunk:
            break
        write(t.feed(chunk))
    write(t.finish())
    return f"{crc & 0xFFFFFFFF:08x}", sha.hexdigest(), n


def _transform_for(match: "Match") -> str:
    via = getattr(match, "matched_via", scanner.VIA_RAW)
    if via == scanner.VIA_HEADERLESS and getattr(match, "header", 0) == scanner.SNES_HEADER_SIZE:
        return STRIP512
    if via == scanner.VIA_BYTESWAPPED:
        return {"v64": SWAP16, "n64": SWAP32}.get(getattr(match, "byte_order", ""), "")
    return ""


_TARGET_EXT = {STRIP512: ".sfc", SWAP16: ".z64", SWAP32: ".z64"}
_RAW_SWAP = {".v64": SWAP16, ".n64": SWAP32}  # DAT roms that are byte-swapped copies of the .z64


def _temp_name(dst: Path) -> Path:
    """Temporary name of a clean file being written (never taken for a moved original)."""
    return dst.with_name(f"{dst.name}{CONVERT_TEMP_MARKER}{uuid.uuid4().hex[:8]}")


def _set_index(result: "ScanResult") -> dict[tuple[str, str], list["Rom"]]:
    """(dat, set name) -> roms of that set, over the loaded DATs."""
    out: dict[tuple[str, str], list["Rom"]] = {}
    for d in getattr(result, "dats", None) or ():
        for r in d.roms:
            name = getattr(r, "set_name", "")
            if name:
                out.setdefault((d.name, name), []).append(r)
    return out


def _raw_swapped_target(match: "Match", sets: dict[tuple[str, str], list["Rom"]],
                        ) -> Optional[tuple["Rom", str]]:
    """Raw match to a DAT ``.v64`` / ``.n64`` rom: (the set's ``.z64`` rom, transform)."""
    for r in match.primary:
        transform = _RAW_SWAP.get(Path(r.name).suffix.lower())
        name = getattr(r, "set_name", "")
        if not transform or not name:
            continue
        for z in sets.get((r.dat, name), ()):
            if Path(z.name).suffix.lower() == ".z64":
                return z, transform
    return None


# --------------------------------------------------------------------------- planning


def _original_dst(src: Path, root: Path) -> Path:
    """``root/_converted_originals/<rel>`` (a leading reserved folder is dropped)."""
    rel = src.relative_to(root)
    return root.joinpath(CONVERTED_DIR, *folders.core_parts(rel.parts))


def _superseded_names(result: "ScanResult") -> dict[tuple[str, str], str]:
    """(dat, rom name) -> newer rom name, over the local matches (latest-only)."""
    try:
        from . import tags
    except ImportError:  # tag parser unavailable: nothing is superseded
        return {}
    by_dat: dict[str, dict[str, bool]] = {}
    for m in result.matched:
        if scanner.is_converted_original(m.entry.path, result.root):
            continue
        for r in m.primary:
            by_dat.setdefault(r.dat, {})[r.name] = bool(getattr(r, "set_name", ""))
    out: dict[tuple[str, str], str] = {}
    for dat, names in by_dat.items():
        style = "nointro" if any(names.values()) else "tosec"
        for old, new in tags.superseded(list(names), style).items():
            out[(dat, old)] = new
    return out


def orphan_originals(result: "ScanResult") -> list[tuple[Path, Path]]:
    """``(original, where it came from)`` for each file in ``_converted_originals`` none of whose roms is matched anywhere else in the
    folder: a conversion that was cut short (the original is moved away first, the clean file was never put in place). Moving it
    back lets the next build convert it again."""
    root = Path(result.root)
    elsewhere: set[tuple[str, str]] = set()
    kept: list["Match"] = []
    for m in result.matched:
        path = m.entry.path if m.entry.path.is_absolute() else root / m.entry.path
        if scanner.is_converted_original(path, root):
            kept.append(m)
        else:
            elsewhere.update((r.dat, r.name) for r in m.primary)
    out: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for m in kept:
        path = m.entry.path if m.entry.path.is_absolute() else root / m.entry.path
        if path in seen or not m.primary or any((r.dat, r.name) in elsewhere for r in m.primary):
            continue
        seen.add(path)
        rel = path.relative_to(root)
        out.append((path, root.joinpath(*folders.core_parts(rel.parts))))
    return out


def _pick_rom(match: "Match", transform: str) -> Optional["Rom"]:
    want = _TARGET_EXT.get(transform)
    roms = [r for r in match.primary if Path(r.name).suffix.lower() == want]
    if not roms:
        return None
    if len(roms) == 1:
        return roms[0]
    stem = match.entry.path.stem
    rom, _ = organiser.choose_rom(stem, roms, key=lambda r: Path(r.name).stem)
    return rom


def plan_conversions(result: "ScanResult", layout: Optional[str] = None,
                     latest_only: bool = False, compress_7z: bool = False,
                     keep: Optional[Callable[[Path], bool]] = None, cleanups: bool = True) -> list[ConvertOp]:
    """One op per convertible match (see the module doc); conflicts / skips explained. ``compress_7z`` (the rule "Compress games as
    7z"): what is written is a ``.7z``, and every other game - a loose file or a zip / 7z of one rom, already the DAT's - gets a repack
    op (``repack``). ``keep(path)``: False for a file the library rules set aside (not converted; None = every file). ``cleanups``
    False: no clean-up of a dump (the rule "Convert when building" off), only the repacks."""
    root = Path(result.root)
    layout = layout or getattr(result, "layout", scanner.LAYOUT_PER_DAT)
    member_counts = scanner._archive_member_counts(result.matched, result.unmatched)
    older = _superseded_names(result) if latest_only else {}
    sets: Optional[dict[tuple[str, str], list["Rom"]]] = None
    ops: list[ConvertOp] = []
    claimed: dict[str, ConvertOp] = {}
    originals: dict[str, ConvertOp] = {}

    for m in result.matched:
        if scanner.is_converted_original(m.entry.path, root):
            continue
        apath = m.entry.path if m.entry.path.is_absolute() else root / m.entry.path
        if scanner.is_in_reason_dir(apath, root):
            continue  # set aside by Build library (excluded / superseded / incomplete / duplicate)
        if keep is not None and not keep(apath):
            continue  # (the library rules set it aside: not worth converting)
        repack = False
        if scanner.is_convertible(m) and not cleanups:
            continue
        if not scanner.is_convertible(m):
            if not compress_7z or not m.primary or apath.suffix.lower() in (".7z", ".rar"):
                continue                                         # (already a 7z; a rar is not read here)
            rom, transform, repack = m.primary[0], "", True          # (already the DAT's rom: only its packing changes)
        elif getattr(m, "matched_via", scanner.VIA_RAW) == scanner.VIA_RAW:
            if sets is None:
                sets = _set_index(result)
            target = _raw_swapped_target(m, sets)
            if target is None:
                continue
            rom, transform = target
        else:
            transform = _transform_for(m)
            rom = _pick_rom(m, transform) if transform else None
            if rom is None:
                continue
        e = m.entry
        src = e.path if e.path.is_absolute() else root / e.path
        folder = organiser.canonical_dir(root, rom.dat, layout)
        if compress_7z:
            dst = folder / organiser.safe_filename(archive_stem(rom) + ".7z")
        elif e.member is None:
            dst = folder / organiser.safe_filename(rom.name)
        else:
            dst = folder / organiser.safe_filename(archive_stem(rom) + ".zip")
        op = ConvertOp(src=src, member=e.member, dst=dst, original_dst=_original_dst(src, root),
                       status=CONVERT, rom_name=rom.name,
                       via=getattr(m, "matched_via", "") or scanner.VIA_RAW,
                       transform=transform, expected_sha1=rom.sha1, expected_size=rom.size,
                       expected_crc=rom.crc, dat=rom.dat, container="7z" if compress_7z else "", repack=repack)
        ops.append(op)

        if e.member is not None:
            n = member_counts.get(e.path, 1)
            if e.path.suffix.lower() not in scanner.ZIP_EXTS and e.path.suffix.lower() != ".7z":
                op.status, op.reason = SKIP, "extract the archive first (only .zip and .7z archives are converted)"
                continue
            if n != 1:
                op.status, op.reason = SKIP, f"archive has {n} members - extract it first"
                continue
        newer = older.get((rom.dat, rom.name)) or next(
            (older[(r.dat, r.name)] for r in m.primary if (r.dat, r.name) in older), None)
        if newer:  # (a raw .v64 match is superseded under its own .v64 name)
            op.status, op.reason = SKIP, f"older version - superseded by {newer}"
            continue
        if not rom.sha1 and not rom.crc:
            op.status, op.reason = SKIP, "the DAT has no hash to verify the result against"
            continue
        key = organiser._fold(dst)
        if key in claimed:
            op.status, op.reason = CONFLICT, f"another file converts to the same name ({claimed[key].src.name})"
            continue
        if organiser._exists(dst) and not organiser._same_file(src, dst):
            op.status, op.reason = CONFLICT, "target exists"
            continue
        okey = organiser._fold(op.original_dst)
        if not repack and (organiser._exists(op.original_dst) or okey in originals):
            op.status, op.reason = CONFLICT, "a converted original with this name is already kept"
            continue
        claimed[key] = op
        if not repack:
            originals[okey] = op
    return ops


def convert_counts(ops: Iterable[ConvertOp]) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for op in ops:
        out[op.status] = out.get(op.status, 0) + 1
    return out


# --------------------------------------------------------------------------- applying


def _check(op: ConvertOp, crc: str, sha1: str, size: int, what: str) -> None:
    if size != op.expected_size:
        raise ConvertError(f"{what}: {size} bytes, the DAT says {op.expected_size} - nothing converted")
    if op.expected_sha1:
        if sha1 != op.expected_sha1:
            raise ConvertError(f"{what}: sha1 {sha1} does not match the DAT ({op.expected_sha1})"
                               " - nothing converted")
    elif crc != op.expected_crc:
        raise ConvertError(f"{what}: crc {crc} does not match the DAT ({op.expected_crc})"
                           " - nothing converted")


def _write_loose(op: ConvertOp, original: Path, tmp: Path) -> str:
    """Write the clean file; returns its sha1 (verified by re-reading it from disk)."""
    with open(original, "rb") as f, open(tmp, "xb") as out:
        crc, sha1, n = transform_stream(f, out, op.transform)
        out.flush()
        os.fsync(out.fileno())
    _check(op, crc, sha1, n, "converted content")
    crc2, sha2 = scanner.hash_file(tmp)  # re-read what is on disk
    _check(op, crc2, sha2, tmp.stat().st_size, "written file")
    return sha2


def _write_zip(op: ConvertOp, original: Path, tmp: Path) -> str:
    """Write the clean single-member zip; returns the sha1 of the zip file itself."""
    with _source(op, original) as f:                         # (the one member of its zip / 7z)
        with zipfile.ZipFile(tmp, "x", zipfile.ZIP_DEFLATED) as zout:
            zi = zipfile.ZipInfo(op.rom_name, date_time=datetime.now().timetuple()[:6])
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            with zout.open(zi, "w", force_zip64=op.expected_size >= 0x7FFFFFFF) as w:
                crc, sha1, n = transform_stream(f, w, op.transform)
    _check(op, crc, sha1, n, "converted content")
    with zipfile.ZipFile(tmp) as zchk:  # re-read what is on disk
        infos = zchk.infolist()
        if len(infos) != 1 or infos[0].filename != op.rom_name:
            raise ConvertError("written archive has unexpected contents")
        with zchk.open(infos[0]) as f:
            raw, _ = scanner.hash_stream(f, infos[0].file_size)
        assert raw is not None
        _check(op, raw[0], raw[1], infos[0].file_size, "written archive")
    return scanner.hash_file(tmp)[1]


@contextlib.contextmanager
def _source(op: ConvertOp, path: Path) -> Iterator[BinaryIO]:
    """The rom of ``op`` as a stream: the loose file, or the one member of its zip / 7z (checked: the archive holds nothing else)."""
    if op.member is None:
        with open(path, "rb") as f:
            yield f
        return
    from . import discarchive
    names = [m.name for m in discarchive.list_members(path)]
    if names != [op.member]:
        raise ConvertError("the archive changed since the scan - scan again")
    with discarchive.open_member(path, op.member) as f:
        yield f


def _write_7z(op: ConvertOp, original: Path, tmp: Path, cancel: Any = None) -> str:
    """Write a ``.7z`` holding the (clean) rom as ``rom_name``; checked against the DAT as written and again read back from disk.
    Returns the sha1 of the .7z file."""
    from . import sevenzip
    t = scanner.StreamTransform(op.transform) if op.transform else None
    crc_sha = [0, hashlib.sha1(), 0]

    def chunks() -> Iterator[bytes]:
        with _source(op, original) as f:
            while True:
                block = f.read(CHUNK_SIZE)
                if not block:
                    break
                out = t.feed(block) if t else block
                if out:
                    yield out
            tail = t.finish() if t else b""
            if tail:
                yield tail

    def counted() -> Iterator[bytes]:
        for block in chunks():
            crc_sha[0] = zlib.crc32(block, crc_sha[0])
            crc_sha[1].update(block)
            crc_sha[2] += len(block)
            yield block
    with open(tmp, "xb"):
        pass                                                   # (claimed: never over anything)
    sevenzip.write_one(tmp, op.rom_name, counted(), op.expected_size, cancel=lambda: organiser._is_cancelled(cancel))
    _check(op, f"{crc_sha[0] & 0xFFFFFFFF:08x}", crc_sha[1].hexdigest(), crc_sha[2], "converted content")
    arc = sevenzip.Archive(tmp)                                # re-read what is on disk (the 7z's own CRC checked as it is read)
    if [n for n, _s, _c in arc.files] != [op.rom_name]:
        raise ConvertError("written archive has unexpected contents")
    crc, sha, n = 0, hashlib.sha1(), 0
    for block in arc.iter_member(op.rom_name):
        crc = zlib.crc32(block, crc)
        sha.update(block)
        n += len(block)
    _check(op, f"{crc & 0xFFFFFFFF:08x}", sha.hexdigest(), n, "written archive")
    return scanner.hash_file(tmp)[1]


def _repack_one(op: ConvertOp, root: Path, created: list[str], tmp: Path) -> None:
    """Put the checked ``.7z`` of a repack op in place and delete the original (main thread)."""
    if organiser._exists(op.dst) and not organiser._same_file(op.src, op.dst):
        raise FileExistsError(f"target exists: {op.dst}")
    organiser.make_dirs(op.dst.parent, created)
    organiser.move_exclusive(tmp, op.dst)
    try:
        os.unlink(op.src)
        op.deleted = True
    except OSError as exc:                                     # (open elsewhere, read-only: left where it is, reported; the 7z stays)
        op.left = f"{op.src}: {exc}"


def _convert_one(op: ConvertOp, root: Path, created: list[str]) -> None:
    src, dst, orig = op.src, op.dst, op.original_dst
    if not organiser._exists(src):
        raise FileNotFoundError(f"source missing: {src}")
    if organiser._exists(orig):
        raise FileExistsError(f"target exists: {orig}")
    if organiser._exists(dst) and not organiser._same_file(src, dst):
        raise FileExistsError(f"target exists: {dst}")

    # 1. keep the original
    organiser.make_dirs(orig.parent, created)
    organiser.rename_no_overwrite(src, orig)

    tmp: Optional[Path] = None
    try:
        # 2. write the clean file under a temporary name and verify it against the DAT
        organiser.make_dirs(dst.parent, created)
        tmp = _temp_name(dst)
        if op.container == "7z":
            _write_7z(op, orig, tmp)
        elif op.member is None:
            _write_loose(op, orig, tmp)
        else:
            _write_zip(op, orig, tmp)
        # 3. move it into place (never overwriting)
        organiser.move_exclusive(tmp, dst)
        tmp = None
    except BaseException as exc:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        try:
            organiser.rename_no_overwrite(orig, src)
        except (OSError, ValueError) as back:
            raise ConvertError(f"{exc}; the original could not be moved back ({back}) - it is at {orig}") from exc
        raise


def apply_conversions(ops: Iterable[ConvertOp], root: Path, progress: Optional[ProgressFn] = None,
                      cancel: Any = None) -> dict[str, Any]:
    """Perform the ``convert`` ops (see the module doc).

    Returns ``{"converted", "failed": [{src, dst, error}], "removed_dirs", "cancelled", "error", "repacked", "deleted",
    "not_deleted"}``. ``cancel`` (callable or Event) stops between files.
    """
    root = Path(root)
    todo = [op for op in ops if op.status == CONVERT]
    total = len(todo)
    converted: list[ConvertOp] = []
    failed: list[dict[str, str]] = []
    created: list[str] = []
    removed: list[str] = []
    cancelled = False
    errors = (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error, lzma.LZMAError)
    done = 0
    cleanups = [op for op in todo if not op.repack]
    repacks = [op for op in todo if op.repack]
    for op in cleanups:
        if organiser._is_cancelled(cancel):
            cancelled = True
            break
        if progress is not None:
            progress(done, total, op.src.name)
        done += 1
        try:
            _convert_one(op, root, created)
            converted.append(op)
        except errors as exc:
            failed.append({"src": str(op.src), "dst": str(op.dst), "error": str(exc)})
    # repacks: compressed in threads (LZMA releases the GIL: ~3 MB/s a core), put in place one by one here
    workers = max(1, min(len(repacks), os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="romorg-7z") as pool:
        running: dict[Any, tuple[ConvertOp, Path]] = {}
        queue = list(repacks)
        while (queue or running) and not cancelled:
            while queue and len(running) < workers * 2:
                if organiser._is_cancelled(cancel):
                    cancelled = True
                    break
                op = queue.pop(0)
                tmp = _temp_name(op.dst)
                organiser.make_dirs(op.dst.parent, created)
                running[pool.submit(_write_7z, op, op.src, tmp, cancel)] = (op, tmp)
            if not running:
                break
            finished, _rest = wait(list(running), return_when=FIRST_COMPLETED)
            for fut in finished:
                op, tmp = running.pop(fut)
                done += 1
                if progress is not None:
                    progress(done, total, op.src.name)
                try:
                    fut.result()
                    _repack_one(op, root, created, tmp)
                    converted.append(op)
                except InterruptedError:
                    cancelled = True
                except errors as exc:
                    failed.append({"src": str(op.src), "dst": str(op.dst), "error": str(exc)})
                finally:
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
        for fut, (op, tmp) in running.items():          # (cancelled: the ones still running end, their part files go)
            fut.cancel()
        for fut, (op, tmp) in running.items():
            try:
                fut.result()
            except BaseException:  # noqa: BLE001
                pass
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    if progress is not None and not cancelled:
        progress(total, total, "")

    removed += organiser.tidy_after(root, [op.src.parent for op in converted], created,
                                    organiser.protected_dirs(root, [], {op.dat for op in todo if op.dat}))
    return {"converted": len(converted), "failed": failed, "removed_dirs": removed, "cancelled": cancelled, "error": None,
            "repacked": sum(1 for op in converted if op.repack), "deleted": sum(1 for op in converted if op.deleted),
            "not_deleted": [op.left for op in converted if op.left]}


# --------------------------------------------------------------------------- zips that hold a game next to other files

@dataclass
class SplitOp:
    """A zip that holds one game's ROM(s) next to other files (a readme, scans, a manual): the game stays in the zip, the rest goes to
    ``_unmatched`` as files of their own (they match nothing in any database)."""
    src: Path
    keep: list[str]            # the members the DAT knows (they stay in the zip, as they are)
    extras: list[str]          # the members it does not know
    extras_dir: Path           # <root>/_unmatched/<the zip's folder>/<zip name without .zip>/
    status: str = "split"      # split | skip
    reason: str = ""


def _safe_member(name: str) -> Optional[Path]:
    """A member name as a relative path that stays inside its folder, or None (absolute, ``..``, a drive)."""
    text = name.replace("\\", "/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or text.startswith("/") or any(p == ".." or ":" in p for p in parts):
        return None
    return Path(*parts)


def plan_splits(result: "ScanResult") -> list[SplitOp]:
    """One op per ``.zip`` of the scan that holds one game next to files no database knows (:func:`organiser.partial_archives`)."""
    root = Path(result.root)
    _lm, _lu, archives = organiser._collect(result, root)
    out: list[SplitOp] = []
    for path, members in sorted(archives.items(), key=lambda kv: str(kv[0]).casefold()):
        if path.suffix.lower() != ".zip" or os.path.islink(path) or scanner.is_converted_original(path, root):
            continue
        if folders.reserved_of(scanner._rel_parts(path, root)) is not None:
            continue                                            # (already set aside: the archive step takes it as it is)
        matches = [m for _e, m in members if m is not None]
        if not matches or len(matches) == len(members):
            continue
        dat = matches[0].dat
        games: Optional[set[str]] = None
        for m in matches:
            g = {organiser._archive_stem(r) for r in m.primary} if m.dat == dat else set()
            games = g if games is None else games & g
        if not games:
            continue                                            # (members of different games: no one game's zip)
        keep = [e.member for e, m in members if m is not None and e.member]
        extras = [e.member for e, m in members if m is None and e.member]
        rel = organiser._rel_to(path, root)
        base = (rel.parent if rel is not None else Path()) / path.stem
        op = SplitOp(path, keep, extras, root / folders.UNMATCHED_DIR / base)
        bad = [x for x in extras if _safe_member(x) is None]
        if bad:
            op.status, op.reason = "skip", f"a member has a name that cannot be unpacked safely: {bad[0]}"
        out.append(op)
    return out


def _crc_of(stream: BinaryIO) -> tuple[int, int]:
    crc = size = 0
    while chunk := stream.read(1 << 20):
        crc = zlib.crc32(chunk, crc)
        size += len(chunk)
    return crc & 0xFFFFFFFF, size


def _split_one(op: SplitOp, root: Path, created: list[str]) -> list[Path]:
    """Unpack the extras, write a zip of the known members, check both, then put the new zip in place of the old one. Nothing of the old
    zip is lost: every member is in the new zip or in a file of its own. Returns the files made."""
    made: list[Path] = []
    tmp = _temp_name(op.src)
    try:
        with zipfile.ZipFile(op.src) as zf:
            infos = {i.filename: i for i in zf.infolist() if not i.is_dir()}
            for name in op.extras:                                   # 1. the extras, each to a file of its own (never over another)
                info = infos[name]
                target = op.extras_dir / _safe_member(name)          # type: ignore[operator]
                n = 1
                while organiser._exists(target):
                    n += 1
                    target = target.with_name(f"{target.stem} ({n}){target.suffix}")
                os.makedirs(target.parent, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                made.append(target)
                with open(target, "rb") as check:
                    if _crc_of(check) != (info.CRC, info.file_size):
                        raise ConvertError(f"{name}: the unpacked file does not match the one in the zip")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:   # 2. the known members, as they are
                for name in op.keep:
                    info = infos[name]
                    with zf.open(info) as src, out.open(zipfile.ZipInfo(name, info.date_time), "w") as dst:
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
            with zipfile.ZipFile(tmp) as new:                        # 3. check them against the old ones
                got = {i.filename: (i.CRC, i.file_size) for i in new.infolist()}
                want = {n: (infos[n].CRC, infos[n].file_size) for n in op.keep}
                if got != want:
                    raise ConvertError("the new zip does not hold the same members")
                if new.testzip() is not None:
                    raise ConvertError("the new zip is damaged")
        os.replace(tmp, op.src)                                      # 4. only now is the old zip replaced
        return made
    except BaseException:
        for f in made:                                               # (nothing changed: what was unpacked goes again)
            try:
                f.unlink()
            except OSError:
                pass
        raise
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def apply_splits(ops: Iterable[SplitOp], root: Path, progress: Optional[ProgressFn] = None, cancel: Any = None) -> dict[str, Any]:
    """Perform the ``split`` ops: ``{"split", "extras", "failed": [{src, error}], "cancelled"}``."""
    root = Path(root)
    todo = [op for op in ops if op.status == "split"]
    failed: list[dict[str, str]] = []
    created: list[str] = []
    split = extras = 0
    cancelled = False
    errors = (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error, ConvertError, KeyError)
    for i, op in enumerate(todo):
        if organiser._is_cancelled(cancel):
            cancelled = True
            break
        if progress is not None:
            progress(i, len(todo), op.src.name)
        try:
            made = _split_one(op, root, created)
            split += 1
            extras += len(made)
        except errors as exc:
            failed.append({"src": str(op.src), "error": str(exc)})
    return {"split": split, "extras": extras, "failed": failed, "cancelled": cancelled}
