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
written to a temporary name (``<clean name>.romorg-convert-xxxxxxxx``, journalled
first so Undo deletes a leftover of an interrupted run) and its hash compared with the DAT (sha1, else crc)
and size - re-read from disk - and only then moved to its final name. Any
mismatch or error deletes the temporary file and moves the original back. Every
step goes into the organiser's undo log (``move`` + ``create`` records), so the
normal :func:`organiser.undo` removes the clean file (sha1-checked) and restores
the original.
"""

from __future__ import annotations

import os
import uuid
import zipfile
import zlib
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Iterable, Optional

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
                     latest_only: bool = False) -> list[ConvertOp]:
    """One op per convertible match (see the module doc); conflicts / skips explained."""
    root = Path(result.root)
    layout = layout or getattr(result, "layout", scanner.LAYOUT_PER_DAT)
    member_counts = scanner._archive_member_counts(result.matched, result.unmatched)
    older = _superseded_names(result) if latest_only else {}
    sets: Optional[dict[tuple[str, str], list["Rom"]]] = None
    ops: list[ConvertOp] = []
    claimed: dict[str, ConvertOp] = {}
    originals: dict[str, ConvertOp] = {}

    for m in result.matched:
        if not scanner.is_convertible(m) or scanner.is_converted_original(m.entry.path, root):
            continue
        if scanner.is_in_reason_dir(m.entry.path if m.entry.path.is_absolute() else root / m.entry.path, root):
            continue  # set aside by Build library (excluded / superseded / incomplete / duplicate)
        if getattr(m, "matched_via", scanner.VIA_RAW) == scanner.VIA_RAW:
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
        if e.member is None:
            dst = folder / organiser.safe_filename(rom.name)
        else:
            dst = folder / organiser.safe_filename(archive_stem(rom) + ".zip")
        op = ConvertOp(src=src, member=e.member, dst=dst, original_dst=_original_dst(src, root),
                       status=CONVERT, rom_name=rom.name,
                       via=getattr(m, "matched_via", "") or scanner.VIA_RAW,
                       transform=transform, expected_sha1=rom.sha1, expected_size=rom.size,
                       expected_crc=rom.crc, dat=rom.dat)
        ops.append(op)

        if e.member is not None:
            n = member_counts.get(e.path, 1)
            if e.path.suffix.lower() not in scanner.ZIP_EXTS:
                op.status, op.reason = SKIP, "extract the archive first (only .zip archives are converted)"
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
        if organiser._exists(op.original_dst) or okey in originals:
            op.status, op.reason = CONFLICT, "a converted original with this name is already kept"
            continue
        claimed[key] = op
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
    with zipfile.ZipFile(original) as zin:
        infos = [i for i in zin.infolist() if not i.is_dir()]
        if len(infos) != 1 or infos[0].filename != op.member:
            raise ConvertError("the archive changed since the scan - scan again")
        with zin.open(infos[0]) as f, zipfile.ZipFile(tmp, "x", zipfile.ZIP_DEFLATED) as zout:
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


def _convert_one(op: ConvertOp, root: Path, journal: Any, created: list[str]) -> None:
    src, dst, orig = op.src, op.dst, op.original_dst
    if not organiser._exists(src):
        raise FileNotFoundError(f"source missing: {src}")
    if organiser._exists(orig):
        raise FileExistsError(f"target exists: {orig}")
    if organiser._exists(dst) and not organiser._same_file(src, dst):
        raise FileExistsError(f"target exists: {dst}")

    # 1. keep the original
    organiser.make_dirs(orig.parent, created, journal)
    seq = journal.record({"op": "move", "src": organiser.rel_str(src, root),
                          "dst": organiser.rel_str(orig, root)})
    try:
        organiser.rename_no_overwrite(src, orig)
    except BaseException:
        journal.failed(seq)
        raise
    journal.useful = True

    tmp: Optional[Path] = None
    try:
        # 2. write the clean file under a temporary name and verify it against the DAT
        organiser.make_dirs(dst.parent, created, journal)
        tmp = _temp_name(dst)
        # journalled before it exists: Undo deletes a leftover of an interrupted run
        journal.record({"op": "tmp", "path": organiser.rel_str(tmp, root)})
        if op.member is None:
            sha1 = _write_loose(op, orig, tmp)
        else:
            sha1 = _write_zip(op, orig, tmp)
        # 3. journal the file we create, then move it into place (never overwriting)
        seq = journal.record({"op": "create", "path": organiser.rel_str(dst, root),
                              "sha1": sha1, "size": tmp.stat().st_size})
        try:
            organiser.move_exclusive(tmp, dst)
        except BaseException:
            journal.failed(seq)
            raise
        tmp = None
    except BaseException as exc:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if isinstance(exc, organiser.UndoLogError):
            raise  # stop everything; the log says where the original is
        seq = journal.record({"op": "move", "src": organiser.rel_str(orig, root),
                              "dst": organiser.rel_str(src, root)})
        try:
            organiser.rename_no_overwrite(orig, src)
        except (OSError, ValueError) as back:
            journal.failed(seq)
            raise ConvertError(f"{exc}; the original could not be moved back ({back}) - "
                               f"it is at {orig} (Undo restores it)") from exc
        raise


def apply_conversions(ops: Iterable[ConvertOp], root: Path, progress: Optional[ProgressFn] = None,
                      cancel: Any = None) -> dict[str, Any]:
    """Perform the ``convert`` ops (see the module doc).

    Returns ``{"converted", "failed": [{src, dst, error}], "removed_dirs",
    "undo_log", "cancelled", "error"}``. ``cancel`` (callable or Event) stops between files.
    """
    root = Path(root)
    todo = [op for op in ops if op.status == CONVERT]
    total = len(todo)
    journal = organiser.Journal(root)
    converted: list[ConvertOp] = []
    failed: list[dict[str, str]] = []
    created: list[str] = []
    removed: list[str] = []
    cancelled = False
    error: Optional[str] = None
    try:
        for i, op in enumerate(todo):
            if organiser._is_cancelled(cancel):
                cancelled = True
                break
            if progress is not None:
                progress(i, total, op.src.name)
            try:
                _convert_one(op, root, journal, created)
                converted.append(op)
            except organiser.UndoLogError:
                raise
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile,
                    EOFError, zlib.error) as exc:
                failed.append({"src": str(op.src), "dst": str(op.dst), "error": str(exc)})
        if progress is not None and not cancelled:
            progress(total, total, "")

        protected = organiser.protected_dirs(root, [], {op.dat for op in todo if op.dat})
        sources = [op.src.parent for op in converted] + [Path(c) for c in created]
        mine = set(created)
        for d in organiser.remove_empty_dirs(sources, root, protected):
            if d in mine:  # a folder this run created (its conversion failed)
                created.remove(d)
                mine.discard(d)
                journal.record({"op": "unmkdir", "path": organiser.rel_str(Path(d), root)})
            else:
                journal.record({"op": "rmdir", "path": organiser.rel_str(Path(d), root)})
                journal.useful = True
                removed.append(d)
    except organiser.UndoLogError as exc:  # stop: every change made so far is in the log
        error = str(exc)
    finally:
        log_path = journal.close()
    return {"converted": len(converted), "failed": failed, "removed_dirs": removed,
            "undo_log": str(log_path) if log_path else None, "cancelled": cancelled, "error": error}
