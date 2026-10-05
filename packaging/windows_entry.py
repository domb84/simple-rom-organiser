"""PyInstaller entry point: windowed .exe (no console), so stdout/stderr go to app.log.

``--chd-worker`` makes the exe act as a CHD hashing worker (see romorg/chdpool.py): the frozen app has no
separate Python interpreter to start ``python -m romorg.chdworker`` with.
"""

import os
import sys

if len(sys.argv) > 1 and sys.argv[1] == "--chd-worker":
    # A windowed exe has no console: sys.stdin / sys.stdout are None, but the pipe handles are fds 0 and 1.
    sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
    sys.stdout = open(1, "w", encoding="utf-8", closefd=False, newline="\n")
    from romorg.chdworker import main as worker_main

    sys.exit(worker_main())

if sys.stdout is None or sys.stderr is None or os.environ.get("ROMORG_LOG") == "1":
    from romorg import paths

    log = paths.data_dir() / "app.log"
    try:
        if log.exists() and log.stat().st_size > 1 << 20:
            log.replace(log.with_suffix(".log.1"))
    except OSError:
        pass
    stream = open(log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = stream

from romorg.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
