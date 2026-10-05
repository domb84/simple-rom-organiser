"""Windows helpers for running external tools (chdman, 7-Zip) from a windowed app.

``NO_WINDOW`` keeps a console window from flashing for every child process. On POSIX it is 0, so
``creationflags=winproc.NO_WINDOW`` is always safe to pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def popen_kwargs(new_session: bool = False) -> dict:
    """Extra ``Popen`` / ``run`` keywords: no console window on Windows; own session on POSIX when asked."""
    if IS_WINDOWS:
        return {"creationflags": NO_WINDOW}
    return {"start_new_session": True} if new_session else {}


def program_dirs() -> list[Path]:
    """Folders worth checking for tools on Windows (empty elsewhere): next to the app, Program Files, Scoop, ..."""
    if not IS_WINDOWS:
        return []
    env = os.environ
    out: list[Path] = [Path(sys.executable).parent, Path(sys.argv[0]).resolve().parent]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "SystemDrive"):
        root = env.get(var)
        if not root:
            continue
        base = Path(root + "\\") if var == "SystemDrive" else Path(root)
        out += [base / "7-Zip", base / "MAME", base / "Programs" / "MAME", base / "RetroArch" / "tools",
                base / "scoop" / "shims", base / "Emulation" / "tools", base / "Emulation" / "tools" / "chdconv",
                base / "tools" / "chdman", base / "tools"]
    out += [Path(r"C:\RetroArch-Win64"), Path(r"C:\mame"), Path(r"C:\tools\mame")]
    seen: set[str] = set()
    return [d for d in out if not (str(d).lower() in seen or seen.add(str(d).lower()))]


def find_tool(names: tuple[str, ...]) -> str | None:
    """First of ``names`` on PATH (``shutil.which`` honours PATHEXT, so ``chdman`` finds ``chdman.exe``), else in
    :func:`program_dirs`."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for d in program_dirs():
        for name in names:
            for ext in ("", ".exe"):
                p = d / (name + ext)
                try:
                    if p.is_file():
                        return str(p)
                except OSError:
                    pass
    return None
