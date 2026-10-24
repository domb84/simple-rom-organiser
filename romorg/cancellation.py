"""Asking whether a job was cancelled. Every long operation takes a ``cancel`` that is either a ``threading.Event`` (the job's) or a
function returning True; this is the one place that tells the two apart."""

from __future__ import annotations

from typing import Any

__all__ = ["is_cancelled", "check"]


def is_cancelled(cancel: Any) -> bool:
    """``cancel`` (None, an Event or a function) says stop."""
    if cancel is None:
        return False
    if hasattr(cancel, "is_set"):
        return bool(cancel.is_set())
    return bool(cancel())


def check(cancel: Any) -> None:
    """Raise ``InterruptedError("cancelled")`` when ``cancel`` says stop."""
    if is_cancelled(cancel):
        raise InterruptedError("cancelled")
