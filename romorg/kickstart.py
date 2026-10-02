"""Install Amiga Kickstart ROMs into a RetroArch system directory for the PUAE core.

PUAE looks for Kickstarts in the frontend's system directory under fixed
filenames (``kick34005.A500`` ...). Local files are recognised by the md5 of the
DAT rom they matched (exact: the file matched that rom by sha1 or crc+size) and
are **copied** (never moved, never overwriting) to ``<dest>/<PUAE filename>``.
The bytes are verified against the expected md5 while copying.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import subprocess
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional

if TYPE_CHECKING:
    from .datfile import Rom
    from .scanner import Entry, ScanResult

PUAE_DOCS_URL = "https://docs.libretro.com/library/puae/"
TEMP_MARKER = ".romorg-tmp-"

# Transcribed from the BIOS table of https://docs.libretro.com/library/puae/ (2026-10).
# (PUAE filename, md5, description). Every md5 except the combined CD32 image is
# present in TOSEC "Commodore Amiga - Firmware (TOSEC-v2025-01-03_CM)".
PUAE_BIOS: list[tuple[str, str, str]] = [
    ("kick31034.A1000", "0b8442c311caa54fb12ec88eaaa9facf", "Kickstart v1.1 rev 31.034 NTSC"),
    ("kick32034.A1000", "1fa1f93d3d7b51271dd1356b8b2b45a9", "Kickstart v1.1 rev 32.034 PAL"),
    ("kick33180.A500", "85ad74194e87c08904327de1a9443b7a", "Kickstart v1.2 rev 33.180"),
    ("kick34005.A500", "82a21c1890cae844b3df741f2762d48d", "Kickstart v1.3 rev 34.005"),
    ("kick37175.A500", "dc10d7bdd1b6f450773dfb558477c230", "Kickstart v2.04 rev 37.175"),
    ("kick37350.A600", "465646c9b6729f77eea5314d1f057951", "Kickstart v2.05 rev 37.350"),
    ("kick40063.A600", "e40a5dfb3d017ba8779faba30cbd1c8e", "Kickstart v3.1 rev 40.063"),
    ("kick39106.A1200", "b7cc148386aa631136f510cd29e42fc3", "Kickstart v3.0 rev 39.106"),
    ("kick40068.A1200", "646773759326fbac3b2311fd8c8793ee", "Kickstart v3.1 rev 40.068"),
    ("kick39106.A4000", "9b8bdd5a3fd32c2a5a6f5b1aefc799a5", "Kickstart v3.0 rev 39.106"),
    ("kick40068.A4000", "9bdedde6a4f33555b4a270c8ca53297d", "Kickstart v3.1 rev 40.068"),
    ("kick34005.CDTV", "89da1838a24460e4b93f4f0c5d92d48d", "CDTV extended ROM v1.00"),
    ("kick40060.CD32", "5f8924d013dd57a89cf349f4cdedc6b1", "CD32 Kickstart v3.1 rev 40.060"),
    ("kick40060.CD32.ext", "bb72565701b1b6faece07d68ea5da639", "CD32 extended ROM rev 40.060"),
    ("kick40060.CD32", "f2f241bf094168cfb9e7805dc2856433", "CD32 KS + extended v3.1 rev 40.060"),
]

# Amiga Forever names from the same table (informational; PUAE accepts these too).
AMIGA_FOREVER_NAMES: dict[str, str] = {
    "0b8442c311caa54fb12ec88eaaa9facf": "amiga-os-110-ntsc.rom",
    "1fa1f93d3d7b51271dd1356b8b2b45a9": "amiga-os-110-pal.rom",
    "85ad74194e87c08904327de1a9443b7a": "amiga-os-120.rom",
    "82a21c1890cae844b3df741f2762d48d": "amiga-os-130.rom",
    "dc10d7bdd1b6f450773dfb558477c230": "amiga-os-204.rom",
    "465646c9b6729f77eea5314d1f057951": "amiga-os-205-a600.rom",
    "e40a5dfb3d017ba8779faba30cbd1c8e": "amiga-os-310-a600.rom",
    "b7cc148386aa631136f510cd29e42fc3": "amiga-os-300-a1200.rom",
    "646773759326fbac3b2311fd8c8793ee": "amiga-os-310-a1200.rom",
    "9b8bdd5a3fd32c2a5a6f5b1aefc799a5": "amiga-os-300-a4000.rom",
    "9bdedde6a4f33555b4a270c8ca53297d": "amiga-os-310-a4000.rom",
    "89da1838a24460e4b93f4f0c5d92d48d": "amiga-os-130-cdtv-ext.rom",
    "5f8924d013dd57a89cf349f4cdedc6b1": "amiga-os-310-cd32.rom",
    "bb72565701b1b6faece07d68ea5da639": "amiga-os-310-cd32-ext.rom",
}

ProgressFn = Callable[[int, int, str], None]


@dataclass
class KickOp:
    target: Path
    source: Optional["Entry"]
    status: str  # copy | ok | conflict | missing
    reason: str = ""
    description: str = ""
    md5: str = ""         # expected md5 of the source (or of the first table entry if missing)
    rom_name: str = ""    # DAT rom name the source matched


# --------------------------------------------------------------------------- system dirs


_SYSTEM_DIR_RE = re.compile(r'^\s*system_directory\s*=\s*"?([^"\n]*)"?\s*$', re.MULTILINE)


def configured_system_dir(cfg: Path, home: Path) -> Optional[Path]:
    """``system_directory`` from a retroarch.cfg (``~`` and ``:`` expanded), or None.

    ``:`` means the RetroArch folder (the folder holding the cfg); ``default``
    means RetroArch's built-in location, which callers list separately.
    """
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            text = f.read(4 << 20)
    except OSError:
        return None
    m = None
    for m in _SYSTEM_DIR_RE.finditer(text):
        pass  # the last assignment wins, as in RetroArch
    if m is None:
        return None
    value = m.group(1).strip()
    if not value or value.lower() == "default":
        return None
    base = cfg.parent
    if value == "~" or value.startswith("~/"):
        path = home / value[2:] if len(value) > 1 else home
    elif value.startswith(":"):
        path = base / value[1:].lstrip("/\\")
    else:
        path = Path(value)
        if not path.is_absolute():
            path = base / path
    return Path(os.path.normpath(path))


def _globs(base: Path, patterns: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for pattern in patterns:
        try:
            out.extend(Path(hit) for hit in sorted(glob.glob(str(base / pattern))))
        except OSError:
            continue
    return out


def detect_system_dirs(home: Optional[Path] = None,
                       media_root: Optional[Path] = None) -> list[dict[str, Any]]:
    """Candidate RetroArch/emulator BIOS directories; existing ones first.

    The ``system_directory`` configured in each RetroArch installation's
    retroarch.cfg (Steam, Flatpak, native, Steam libraries on SD cards) comes
    first - that is the folder PUAE actually reads. Returns
    ``[{"path", "label", "exists"}]``; well-known default paths that do not exist
    are listed after the existing ones (SD-card globs only when they exist).
    """
    home = Path(home) if home is not None else Path.home()
    media = Path(media_root) if media_root is not None else Path("/run/media")
    installs: list[tuple[Path, str]] = [  # (RetroArch config dir, label)
        (home / ".local/share/Steam/steamapps/common/RetroArch", "RetroArch (Steam)"),
        (home / ".var/app/org.libretro.RetroArch/config/retroarch", "RetroArch (Flatpak)"),
        (home / ".config/retroarch", "RetroArch"),
    ]
    sd_steam = _globs(media, ("*/steamapps/common/RetroArch", "*/*/steamapps/common/RetroArch",
                              "*/*/*/steamapps/common/RetroArch"))
    installs += [(p, f"RetroArch (Steam on {p.parent.parent.parent.name})") for p in sd_steam]

    configured: list[tuple[Path, str]] = []
    for ra_dir, label in installs:
        sysdir = configured_system_dir(ra_dir / "retroarch.cfg", home)
        if sysdir is not None:
            configured.append((sysdir, f"{label} - configured"))

    cands: list[tuple[Path, str]] = configured + [
        (home / "Emulation" / "bios", "EmuDeck"),
        (home / "retrodeck" / "bios", "RetroDECK"),
        (home / ".var/app/org.libretro.RetroArch/config/retroarch/system", "RetroArch (Flatpak)"),
        (home / ".local/share/Steam/steamapps/common/RetroArch/system", "RetroArch (Steam)"),
        (home / ".config/retroarch/system", "RetroArch"),
    ]
    cands += [(p / "system", label) for p, label in installs[3:]]
    # EmuDeck / RetroDECK on an SD card: /run/media/<label>/... or /run/media/<user>/<label>/...
    for hit in _globs(media, ("*/Emulation/bios", "*/*/Emulation/bios")):
        cands.append((hit, f"EmuDeck ({hit.parent.parent.name})"))
    for hit in _globs(media, ("*/retrodeck/bios", "*/*/retrodeck/bios")):
        cands.append((hit, f"RetroDECK ({hit.parent.parent.name})"))

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, label in cands:
        key = os.path.realpath(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        out.append({"path": str(path), "label": label, "exists": exists})
    out.sort(key=lambda d: not d["exists"])  # stable: existing first, configured ones lead
    return out


# --------------------------------------------------------------------------- planning


def _file_md5(path: Path) -> Optional[str]:
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _abs(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def _local_by_md5(result: "ScanResult", kickstart_dat: Optional[str]) -> dict[str, list[tuple["Entry", "Rom"]]]:
    """md5 -> [(entry, rom)], best candidates first.

    Preference: roms from ``kickstart_dat``, loose files before archive members,
    then path order.
    """
    out: dict[str, list[tuple["Entry", "Rom", tuple]]] = {}
    for m in result.matched:
        for rom in m.roms:
            if not rom.md5:
                continue
            e = m.entry
            key = (rom.dat != kickstart_dat if kickstart_dat else False, e.member is not None,
                   str(e.path), e.member or "")
            out.setdefault(rom.md5.lower(), []).append((e, rom, key))
    return {md5: [(e, r) for e, r, _ in sorted(lst, key=lambda t: t[2])] for md5, lst in out.items()}


def plan_kickstarts(result: "ScanResult", dest: Path,
                    kickstart_dat: Optional[str] = "Commodore Amiga - Firmware") -> list[KickOp]:
    """One op per PUAE filename (table order).

    ``copy``: a local file matches and the target doesn't exist. ``ok``: the target
    already exists with one of the expected md5s. ``conflict``: the target exists
    with a different md5 (never overwritten). ``missing``: no local file found.
    """
    dest = Path(dest)
    local = _local_by_md5(result, kickstart_dat)
    groups: dict[str, list[tuple[str, str]]] = {}
    for filename, md5, desc in PUAE_BIOS:
        groups.setdefault(filename, []).append((md5, desc))

    ops: list[KickOp] = []
    for filename, variants in groups.items():
        target = dest / filename
        md5s = {m for m, _ in variants}
        found: Optional[tuple["Entry", "Rom", str, str]] = None
        for md5, desc in variants:
            hits = local.get(md5)
            if hits:
                found = (hits[0][0], hits[0][1], md5, desc)
                break
        existing = _file_md5(target) if target.is_file() else None
        exists = os.path.lexists(target)

        if found is None:
            md5, desc = variants[0]
            if existing in md5s:
                d = next(dd for m, dd in variants if m == existing)
                ops.append(KickOp(target, None, "ok", "already installed", d, existing or ""))
            else:
                why = "not found in the scanned folder"
                if exists:
                    why += "; a different file exists at the target"
                ops.append(KickOp(target, None, "missing", why, " / ".join(d for _, d in variants), md5))
            continue

        entry, rom, md5, desc = found
        src = _abs(entry.path, result.root)
        label = src.name + (f"::{entry.member}" if entry.member else "")
        if existing in md5s:
            ops.append(KickOp(target, entry, "ok", "already installed", desc, md5, rom.name))
        elif exists:
            ops.append(KickOp(target, entry, "conflict", "a different file exists at the target",
                              desc, md5, rom.name))
        else:
            ops.append(KickOp(target, entry, "copy", f"from {label}", desc, md5, rom.name))
    return ops


# --------------------------------------------------------------------------- applying


def _read_source(entry: "Entry", root: Optional[Path] = None) -> bytes:
    """Bytes of a loose file or of one archive member (zip natively, 7z/rar via 7z)."""
    path = _abs(entry.path, root) if root is not None else entry.path
    if entry.member is None:
        return path.read_bytes()
    ext = path.suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(path) as zf:
            return zf.read(entry.member)
    from .scanner import find_7z

    exe = find_7z()
    if exe is None:
        raise RuntimeError("7z is needed to extract from " + path.name)
    proc = subprocess.run([exe, "e", "-so", "-p", "--", str(path), entry.member],
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f"7z exited with code {proc.returncode}")
    return proc.stdout


def _write_no_overwrite(target: Path, data: bytes) -> None:
    """Atomic create: temp file in the same directory, then link/rename without overwriting."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}{TEMP_MARKER}{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, target)  # atomic, fails if target exists
        except FileExistsError:
            raise
        except (OSError, NotImplementedError, AttributeError):
            # FAT/exFAT have no hard links: check-then-rename.
            if os.path.lexists(target):
                raise FileExistsError(f"target exists: {target}") from None
            os.rename(tmp, target)
    finally:  # also removes a partial temp file after ENOSPC / EIO
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def apply_kickstarts(ops: Iterable[KickOp], root: Optional[Path] = None,
                     progress: Optional[ProgressFn] = None) -> dict[str, Any]:
    """Copy the ``copy`` ops. Returns ``{"copied", "failed": [{target, error}], "skipped"}``.

    ``root`` resolves relative source paths (scan root); absolute paths need none.
    """
    all_ops = list(ops)
    todo = [op for op in all_ops if op.status == "copy" and op.source is not None]
    copied = 0
    failed: list[dict[str, str]] = []
    for i, op in enumerate(todo):
        if progress:
            progress(i, len(todo), op.target.name)
        try:
            if os.path.lexists(op.target):
                raise FileExistsError(f"target exists: {op.target}")
            assert op.source is not None
            data = _read_source(op.source, root)
            got = hashlib.md5(data).hexdigest()
            if op.md5 and got != op.md5:
                raise ValueError(f"md5 mismatch for {op.target.name}: got {got}, expected {op.md5}")
            _write_no_overwrite(op.target, data)
            copied += 1
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, KeyError,
                subprocess.SubprocessError) as exc:
            failed.append({"target": str(op.target), "error": str(exc)})
    if progress:
        progress(len(todo), len(todo), "")
    return {"copied": copied, "failed": failed, "skipped": len(all_ops) - len(todo)}


def to_json(ops: Iterable[KickOp], root: Optional[Path] = None) -> list[dict[str, Any]]:
    """JSON-friendly list for the API."""
    out = []
    for op in ops:
        src = None
        if op.source is not None:
            p = op.source.path
            if root is not None:
                try:
                    p = p.relative_to(root)
                except ValueError:
                    pass
            src = p.as_posix() + (f"::{op.source.member}" if op.source.member else "")
        out.append({"target": str(op.target), "filename": op.target.name, "source": src,
                    "status": op.status, "reason": op.reason, "description": op.description,
                    "md5": op.md5, "rom_name": op.rom_name})
    return out
