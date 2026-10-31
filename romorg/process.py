"""Running other programs (chdman, 7-Zip): the ``Popen`` keywords they share, finding a tool, and whether a process is alive."""

from __future__ import annotations

import os
import shutil


def popen_kwargs(new_session: bool = False) -> dict:
    """Extra ``Popen`` / ``run`` keywords: its own session when asked (then it can be killed as a group)."""
    return {"start_new_session": True} if new_session else {}


def find_tool(names: tuple[str, ...]) -> str | None:
    """First of ``names`` on PATH."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def pid_alive(pid: int) -> bool:
    """Whether a process with id ``pid`` is running. Never signals it: ``os.kill(pid, 0)`` (``EPERM`` = exists, someone else's).

    Only a definite "no such process" answer counts as dead: the callers sweep a dead owner's scratch folder or
    replace its instance file, so a probe that fails for any other reason counts as alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return False        # larger than pid_t: names no process
    except OSError:
        return True         # EPERM (someone else's process) or an unexpected errno: let the caller keep it
    return True
