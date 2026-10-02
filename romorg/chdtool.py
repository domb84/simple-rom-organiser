"""chdman wrapper: detect it, ``extractcd`` / ``createcd`` with progress + prompt cancel, temp-space rules.

chdman (from MAME) is the preferred way to read and write CHDs: it is much faster than the pure-Python reader
(:mod:`romorg.chd`), above all for FLAC audio. Nothing here is required: without chdman the app still scans
and verifies CHDs with the pure-Python reader; only *Convert to CHD* needs it.

Detection order (first hit wins): ``$ROMORG_CHDMAN`` / the ``chdman_path`` setting in config.json, ``chdman`` on
``PATH``, the Flatpak ``org.mamedev.MAME`` (``flatpak run --command=chdman``), common tool folders
(``~/.local/bin``, ``~/Emulation/tools``, EmuDeck / RetroDECK), ``/usr/bin`` and ``/usr/local/bin``.

Temp files (an extracted disc is about its full raw size, a created CHD up to that) always go to a hidden
folder **inside the ROM folder** (same filesystem as the ROMs; never ``/tmp``, which may be a small RAM disk):
``<root>/.romorg-chd-<id>/`` with a ``pid`` file. :func:`sweep_stale` removes the folders of dead runs.
"""

from __future__ import annotations

import hashlib
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

TEMP_PREFIX = ".romorg-chd-"
ENV_VAR = "ROMORG_CHDMAN"
CONFIG_KEY = "chdman_path"
FLATPAK_APPS = ("org.mamedev.MAME", "net.retrodeck.retrodeck")
SPACE_MARGIN = 256 * 1024 * 1024
PROBE_TIMEOUT = 30
STALE_AGE = 24 * 3600          # a temp folder without a live owner older than this is always swept
_PCT = re.compile(rb"(\d+(?:\.\d+)?)%")

ProgressFn = Callable[[int, int, str], None]

INSTALL_HINT = (
    "chdman was not found. It ships with MAME: open Discover (the app store in Desktop Mode), search for "
    "\"MAME\" (org.mamedev.MAME) and install it, or install any chdman and put it on PATH, or set its path "
    "in the app (chdman path). The pure-Python reader still scans and verifies your CHDs; only converting "
    "raw Redump sets to CHD needs chdman."
)


class ChdmanError(Exception):
    """chdman could not be run / failed / was cancelled (``cancelled`` set)."""

    def __init__(self, message: str, cancelled: bool = False) -> None:
        super().__init__(message)
        self.cancelled = cancelled


@dataclass
class Chdman:
    argv: list[str]                 # command prefix, e.g. ["/usr/bin/chdman"] or ["flatpak", "run", "--command=chdman", "org.mamedev.MAME"]
    kind: str                       # configured | path | flatpak | folder
    label: str                      # shown in the UI
    flatpak_app: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"found": True, "kind": self.kind, "label": self.label, "command": " ".join(self.argv),
                "flatpak_app": self.flatpak_app}


# --------------------------------------------------------------------------- detection

def _probe(argv: Sequence[str]) -> bool:
    """True when ``argv`` runs and prints chdman's usage (its exit status for ``help`` is not 0)."""
    try:
        proc = subprocess.run([*argv, "help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    text = proc.stdout.decode("utf-8", "replace").lower()
    return "chdman" in text and ("createcd" in text or "extractcd" in text or "usage" in text)


def _candidates(config: Optional[dict], extra_dirs: Sequence[str] = ()) -> list[tuple[str, list[str], str, str]]:
    """``[(kind, argv, label, flatpak app)]`` in detection order (not probed)."""
    out: list[tuple[str, list[str], str, str]] = []
    for raw, kind in ((os.environ.get(ENV_VAR, ""), "configured"),
                      ((config or {}).get(CONFIG_KEY, "") if isinstance(config, dict) else "", "configured")):
        if isinstance(raw, str) and raw.strip():
            p = os.path.expanduser(raw.strip())
            out.append((kind, [p], p, ""))
    found = shutil.which("chdman")
    if found:
        out.append(("path", [found], found, ""))
    if shutil.which("flatpak"):
        for app in FLATPAK_APPS:
            out.append(("flatpak", ["flatpak", "run", "--command=chdman", app], f"{app} (Flatpak)", app))
    home = Path.home()
    folders = [home / ".local" / "bin", home / "Emulation" / "tools", home / "Emulation" / "tools" / "chdconv",
               home / "Emulation" / "tools" / "MAME", home / "retrodeck" / "tools", home / "bin",
               Path("/usr/bin"), Path("/usr/local/bin"), *(Path(d) for d in extra_dirs)]
    for d in folders:
        p = d / "chdman"
        try:
            if p.is_file() and os.access(p, os.X_OK):
                out.append(("folder", [str(p)], str(p), ""))
        except OSError:
            pass
    return out


def detect(config: Optional[dict] = None, extra_dirs: Sequence[str] = ()) -> Optional[Chdman]:
    """The first working chdman, or None."""
    seen: set[tuple] = set()
    for kind, argv, label, app in _candidates(config, extra_dirs):
        key = tuple(argv)
        if key in seen:
            continue
        seen.add(key)
        if kind == "flatpak":
            try:
                ok = subprocess.run(["flatpak", "info", app], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    timeout=PROBE_TIMEOUT).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if not ok:
                continue
        if _probe(argv):
            return Chdman(argv=argv, kind=kind, label=label, flatpak_app=app)
    return None


def info(config: Optional[dict] = None) -> dict[str, Any]:
    """JSON for the UI: ``{"found", "label", "kind", ...}`` or ``{"found": False, "hint": ...}``."""
    found = detect(config)
    if found is None:
        return {"found": False, "kind": "", "label": "", "hint": INSTALL_HINT,
                "steps": ["Open Discover (Desktop Mode) and install \"MAME\" (org.mamedev.MAME) - it ships chdman",
                          "or install any chdman and put it on PATH",
                          f"or set the chdman path in the app (config key \"{CONFIG_KEY}\" / environment {ENV_VAR})"]}
    out = found.to_dict()
    out["hint"] = ""
    return out


# --------------------------------------------------------------------------- flatpak access

def flatpak_permissions(app: str) -> Optional[list[str]]:
    """The ``filesystems`` the Flatpak ``app`` may access, or None when they cannot be read."""
    try:
        proc = subprocess.run(["flatpak", "info", "--show-permissions", app], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("filesystems="):
            return [x for x in line[len("filesystems="):].split(";") if x]
    return []


def flatpak_covers(perms: Sequence[str], path: Path) -> bool:
    """Whether a Flatpak with filesystem permissions ``perms`` can see ``path``."""
    home = Path.home()
    for perm in perms:
        base = perm.split(":", 1)[0].lstrip("!")
        if perm.startswith("!"):
            continue
        if base in ("host", "host-os"):
            return True
        if base == "home" and (path == home or home in path.parents):
            return True
        if base.startswith("~/") or base.startswith("/"):
            full = Path(os.path.expanduser(base)) if base.startswith("~/") else Path(base)
            if path == full or full in path.parents:
                return True
    return False


def flatpak_override_hint(app: str, path: Path) -> str:
    return (f"The Flatpak {app} cannot see {path}. Allow it once with:\n"
            f"  flatpak override --user --filesystem=\"{path}\" {app}\n"
            f"(or --filesystem=host / --filesystem=home), then try again.")


def check_access(chdman: Chdman, folder: Path) -> None:
    """Raise :class:`ChdmanError` with the ``flatpak override`` fix when a Flatpak chdman cannot see ``folder``."""
    if chdman.kind != "flatpak":
        return
    perms = flatpak_permissions(chdman.flatpak_app)
    if perms is not None and not flatpak_covers(perms, Path(folder).resolve()):
        raise ChdmanError(flatpak_override_hint(chdman.flatpak_app, Path(folder)))


# --------------------------------------------------------------------------- temp dirs / space

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def make_workdir(root: Path) -> Path:
    """A fresh hidden work folder inside ``root`` (the ROM filesystem), owned by this process."""
    work = Path(root) / f"{TEMP_PREFIX}{uuid.uuid4().hex[:10]}"
    work.mkdir()
    (work / "pid").write_text(str(os.getpid()), encoding="ascii")
    return work


def remove_workdir(work: Optional[Path]) -> None:
    if work is not None:
        shutil.rmtree(work, ignore_errors=True)


def sweep_stale(root: Path, max_age: float = STALE_AGE) -> list[str]:
    """Delete leftover ``.romorg-chd-*`` folders of dead runs directly inside ``root``; returns the names."""
    removed: list[str] = []
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return removed
    for p in entries:
        if not p.name.startswith(TEMP_PREFIX) or not p.is_dir() or p.is_symlink():
            continue
        try:
            pid = int((p / "pid").read_text(encoding="ascii").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        try:
            age = time.time() - p.stat().st_mtime
        except OSError:
            age = 0
        if pid and pid != os.getpid() and _pid_alive(pid) and age < max_age:
            continue            # another running copy of the app still works there
        if pid == os.getpid() and age < max_age:
            continue            # ours, in use
        shutil.rmtree(p, ignore_errors=True)
        if not p.exists():
            removed.append(p.name)
    return removed


def check_space(folder: Path, needed: int, margin: int = SPACE_MARGIN) -> None:
    """Raise :class:`ChdmanError` unless ``folder``'s filesystem has ``needed + margin`` bytes free."""
    try:
        free = shutil.disk_usage(folder).free
    except OSError as exc:
        raise ChdmanError(f"cannot read the free space of {folder}: {exc}") from exc
    if free < needed + margin:
        raise ChdmanError(f"not enough free space in {folder}: {free // (1 << 20)} MB free, about "
                          f"{(needed + margin) // (1 << 20)} MB needed for the temporary files")


# --------------------------------------------------------------------------- running chdman

def run(chdman: Chdman, args: Sequence[str], progress: Optional[ProgressFn] = None, label: str = "",
        cancel: Any = None, cwd: Optional[Path] = None, poll: float = 0.2) -> str:
    """Run chdman, forward its ``NN.N%`` progress and stop it promptly on cancel. Returns its output text."""
    argv = [*chdman.argv, *args]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=str(cwd) if cwd else None,
                                start_new_session=True)
    except OSError as exc:
        raise ChdmanError(f"cannot run chdman: {exc}") from exc
    chunks: "queue.Queue[Optional[bytes]]" = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        try:
            while True:
                b = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not b:
                    break
                chunks.put(b)
        finally:
            chunks.put(None)

    t = threading.Thread(target=reader, name="chdman-out", daemon=True)
    t.start()
    out = bytearray()
    cancelled = False
    done_reading = False
    last_pct = -1.0
    while not done_reading:
        if cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
            cancelled = True
            _terminate(proc)
            break
        try:
            b = chunks.get(timeout=poll)
        except queue.Empty:
            continue
        if b is None:
            done_reading = True
            break
        out += b
        if len(out) > 1 << 20:
            del out[:len(out) - (1 << 19)]
        if progress is not None:
            m = _PCT.findall(b)
            if m:
                pct = float(m[-1])
                if pct != last_pct:
                    last_pct = pct
                    try:
                        progress(int(pct * 10), 1000, f"{label} {pct:.0f}%".strip())
                    except Exception:  # noqa: BLE001 - a broken observer must not stop chdman
                        pass
    if cancelled:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        t.join(timeout=5)
        proc.stdout.close()
        raise ChdmanError("cancelled", cancelled=True)
    rc = proc.wait()
    t.join(timeout=5)
    proc.stdout.close()
    text = out.decode("utf-8", "replace").replace("\r", "\n")
    if rc != 0:
        tail = [ln.strip() for ln in text.splitlines() if ln.strip() and "% complete" not in ln][-3:]
        msg = "; ".join(tail) or f"exit status {rc}"
        if chdman.kind == "flatpak" and re.search(r"no such file|not found|unable to open|cannot open|permission", text, re.I):
            msg += "\n" + flatpak_override_hint(chdman.flatpak_app, Path(args[args.index("-i") + 1]).parent
                                                if "-i" in args else Path("."))
        raise ChdmanError(f"chdman {args[0] if args else ''} failed: {msg}")
    return text


def _terminate(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            proc.kill()


# --------------------------------------------------------------------------- extractcd / createcd

@dataclass
class ExtractedTrack:
    number: int
    path: Path
    offset: int
    size: int
    audio: bool = False


@dataclass
class Extraction:
    sheet: Path
    tracks: list[ExtractedTrack] = field(default_factory=list)


def parse_gdi(path: Path) -> list[dict[str, Any]]:
    """Rows ``{number, lba, type, sector, file, offset}`` of a ``.gdi`` file."""
    text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    try:
        count = int(text[0].strip())
    except (IndexError, ValueError) as exc:
        raise ChdmanError(f"{path.name} is not a GDI file") from exc
    rows = []
    for line in text[1:]:
        if not line.strip():
            continue
        parts = shlex.split(line)
        if len(parts) < 6:
            raise ChdmanError(f"bad GDI line: {line!r}")
        rows.append({"number": int(parts[0]), "lba": int(parts[1]), "type": int(parts[2]),
                     "sector": int(parts[3]), "file": parts[4], "offset": int(parts[5])})
    if len(rows) != count:
        raise ChdmanError(f"{path.name}: expected {count} tracks, found {len(rows)}")
    return rows


def extract_cd(chdman: Chdman, chd: Path, workdir: Path, kind: str, track_sizes: Sequence[int] = (),
               progress: Optional[ProgressFn] = None, cancel: Any = None) -> Extraction:
    """``chdman extractcd`` into ``workdir``. GD-ROM CHDs go to ``disc.gdi`` + one file per track; CD CHDs to
    ``disc.cue`` + ``disc.bin`` (split with ``track_sizes`` = bytes of each track, in order)."""
    check_access(chdman, Path(chd).parent)
    check_access(chdman, workdir)
    if kind == "gdrom":
        sheet = workdir / "disc.gdi"
        run(chdman, ["extractcd", "-i", str(chd), "-o", str(sheet)], progress, "Extracting", cancel)
        tracks = []
        for row in parse_gdi(sheet):
            f = workdir / row["file"]
            if not f.is_file():
                raise ChdmanError(f"chdman did not write {row['file']}")
            tracks.append(ExtractedTrack(row["number"], f, row["offset"], f.stat().st_size - row["offset"],
                                         audio=row["type"] == 0))
        return Extraction(sheet, tracks)
    sheet, binf = workdir / "disc.cue", workdir / "disc.bin"
    run(chdman, ["extractcd", "-i", str(chd), "-o", str(sheet), "-ob", str(binf)], progress, "Extracting", cancel)
    if not binf.is_file():
        raise ChdmanError("chdman did not write disc.bin")
    tracks = []
    off = 0
    for n, size in enumerate(track_sizes, 1):
        tracks.append(ExtractedTrack(n, binf, off, size))
        off += size
    if off != binf.stat().st_size:
        raise ChdmanError("the extracted disc size does not match the CHD's track layout")
    return Extraction(sheet, tracks)


def create_cd(chdman: Chdman, source: Path, out_chd: Path, progress: Optional[ProgressFn] = None,
              cancel: Any = None) -> None:
    """``chdman createcd -i <gdi|cue> -o out.chd`` (``out_chd`` must not exist)."""
    check_access(chdman, Path(source).parent)
    check_access(chdman, Path(out_chd).parent)
    if out_chd.exists():
        raise ChdmanError(f"refusing to overwrite {out_chd}")
    run(chdman, ["createcd", "-i", str(source), "-o", str(out_chd)], progress, "Compressing", cancel)
    if not out_chd.is_file():
        raise ChdmanError("chdman did not write the CHD")


def hash_range(path: Path, offset: int, size: int, progress: Optional[Callable[[int], None]] = None,
               cancel: Any = None) -> tuple[str, str, str]:
    """``(crc32, md5, sha1)`` of ``size`` bytes of ``path`` from ``offset``."""
    crc = 0
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    left = size
    with open(path, "rb") as f:
        f.seek(offset)
        while left > 0:
            if cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
                raise ChdmanError("cancelled", cancelled=True)
            block = f.read(min(1 << 20, left))
            if not block:
                raise ChdmanError(f"{Path(path).name} is shorter than expected")
            left -= len(block)
            crc = zlib.crc32(block, crc)
            md5.update(block)
            sha1.update(block)
            if progress:
                progress(len(block))
    return "%08x" % (crc & 0xFFFFFFFF), md5.hexdigest(), sha1.hexdigest()
