"""A process-wide count of the data the running job has dealt with (files hashed, discs read, files copied or moved).

Jobs run one at a time, so one counter is enough: the job manager resets it when a job starts and the job's status reports it
(``bytes``), which the UI shows as "12.3 GB processed"."""

from __future__ import annotations

import threading

__all__ = ["reset", "add", "value"]

_lock = threading.Lock()
_bytes = 0


def reset() -> None:
    global _bytes
    with _lock:
        _bytes = 0


def add(n: int) -> None:
    global _bytes
    if n > 0:
        with _lock:
            _bytes += n


def value() -> int:
    with _lock:
        return _bytes
