"""What the readers of an emulator's own files (``nintendosaves``, ``pcsx2``, ``switchsaves``) share."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

__all__ = ["measure", "read_lines"]


def measure(folder: Path, skip_prefix: str = "", listing: bool = False) -> Tuple[int, int, float, Tuple[Tuple[str, int], ...]]:
    """``(files, bytes, newest modification time, listing)`` of everything below ``folder``; a file whose name starts with
    ``skip_prefix`` (an emulator's own bookkeeping) is not counted. ``listing``: also every ``(path below folder, size)``, sorted."""
    files = total = 0
    newest = 0.0
    rows: List[Tuple[str, int]] = []
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            if skip_prefix and name.startswith(skip_prefix):
                continue
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            files += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
            if listing:
                rows.append((p.relative_to(folder).as_posix(), st.st_size))
    rows.sort()
    return files, total, newest, tuple(rows)


def read_lines(path: Path) -> List[str]:
    """The lines of a text file (bytes that are not UTF-8 replaced); none when it cannot be read."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
