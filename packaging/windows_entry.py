"""PyInstaller entry point: windowed .exe (no console), so stdout/stderr go to app.log."""

import os
import sys

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
