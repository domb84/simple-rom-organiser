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


def app_dirs() -> list[Path]:
    """The folders a user means by "next to the app": the single-file exe's folder (frozen), the top folder of the
    Windows .zip package (``<top>\\python\\python.exe`` + ``<top>\\app\\romorg``, see ``build_windows.ps1``), and the
    folders of ``sys.executable`` and ``argv[0]``."""
    out: list[Path] = []
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).resolve().parent)
    else:
        here = Path(__file__).resolve().parent              # ...\app\romorg in the zip
        top = here.parent.parent
        if here.parent.name.lower() == "app" and (top / "python").is_dir():
            out.append(top)
    out.append(Path(sys.executable).parent)
    if sys.argv and sys.argv[0]:
        try:
            out.append(Path(sys.argv[0]).resolve().parent)
        except OSError:
            pass
    return out


def program_dirs() -> list[Path]:
    """Folders worth checking for tools on Windows (empty elsewhere): next to the app, Program Files, Scoop, ..."""
    if not IS_WINDOWS:
        return []
    env = os.environ
    out: list[Path] = app_dirs()
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


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _win_pid_alive(pid: int) -> bool:
    """``OpenProcess`` + ``GetExitCodeProcess``: alive while the exit code is ``STILL_ACTIVE``. A process we may not
    open (``ERROR_ACCESS_DENIED``: another user's, or a protected one) exists, so it counts as alive."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True     # the process exists (we hold a handle); its state is just unreadable
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Whether a process with id ``pid`` is running. Never signals it.

    POSIX: ``os.kill(pid, 0)`` (``EPERM`` = exists, someone else's). Windows: ``os.kill(pid, 0)`` would send
    ``CTRL_C_EVENT`` to the console instead of probing, so the process is opened and its exit code read instead
    (a process that exited but whose handle someone still holds reports its real exit code, so it counts as dead).

    Only a definite "no such process" answer counts as dead: the callers sweep a dead owner's scratch folder or
    replace its instance file, so a probe that fails for any other reason (ctypes unavailable, an unexpected errno)
    counts as alive. Windows process ids are 32-bit; a larger value (a corrupted marker) names no process."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        if pid > 0xFFFFFFFF:
            return False    # not a Windows pid; the DWORD argument would silently truncate it to another one
        try:
            return _win_pid_alive(pid)
        except Exception:   # OSError, AttributeError, ctypes.ArgumentError, ...: unknown, so keep the owner's files
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return False        # larger than pid_t: names no process
    except OSError:
        return True         # EPERM (someone else's process) or an unexpected errno: let the caller keep it
    return True


def kill_tree(proc: "subprocess.Popen") -> None:
    """Kill ``proc`` and everything it started (a .cmd/.bat wrapper's child included). Windows has no process groups
    to signal, so ``taskkill /T`` walks the tree; elsewhere the caller uses ``os.killpg``."""
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
