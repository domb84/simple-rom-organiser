"""Worker process of :mod:`romorg.chdpool`: hashes CHD tracks on request (JSON lines over stdin / stdout).

Request  : ``{"path": "...", "track": 3}``  (0-based track index)
Replies  : ``{"p": n}`` progress (bytes since the last one), then ``{"ok": {"crc32", "md5", "sha1", "size"}}``
           or ``{"error": "text"}``.
Run with ``python -m romorg.chdworker``; never started by anything but the pool.
"""

from __future__ import annotations

import json
import sys

PROGRESS_STEP = 8 << 20


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve(stdin=None) -> None:
    from . import chd as chdlib
    cache: dict = {}
    for line in (stdin or sys.stdin):
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            path = req["path"]
            info = cache.get(path)
            if info is None:
                for old in cache.values():
                    old.close()
                cache.clear()
                info = cache[path] = chdlib.Chd(path)
            pending = [0]

            def progress(n: int) -> None:
                pending[0] += n
                if pending[0] >= PROGRESS_STEP:
                    _reply({"p": pending[0]})
                    pending[0] = 0

            h = chdlib.hash_track(info, info.tracks[int(req["track"])], progress=progress)
            if pending[0]:
                _reply({"p": pending[0]})
            _reply({"ok": {"crc32": h.crc32, "md5": h.md5, "sha1": h.sha1, "size": h.size}})
        except Exception as exc:  # noqa: BLE001 - reported to the parent, which falls back to hashing itself
            _reply({"error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
