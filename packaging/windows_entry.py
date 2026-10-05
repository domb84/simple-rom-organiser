"""PyInstaller entry point: windowed .exe (no console), so stdout/stderr go to app.log.

``--chd-worker`` makes the exe act as a CHD hashing worker (see romorg/chdsched.py): the frozen app has no
separate Python interpreter to start ``python -m romorg.chdworker`` with.
"""

import os
import sys

if len(sys.argv) > 1 and sys.argv[1] == "--chd-worker":
    # A windowed exe has no console: sys.stdin / sys.stdout are None, but the pipe handles are fds 0 and 1. The
    # worker protocol is binary (a JSON line, then raw track bytes), so the fds are opened unbuffered and untranslated.
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(0, os.O_BINARY)
        msvcrt.setmode(1, os.O_BINARY)
    from romorg.chdworker import serve

    serve(open(0, "rb", closefd=False), open(1, "wb", closefd=False))
    sys.exit(0)

if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
    # Build check: the build passes the names of all romorg modules (the server imports them by name, which
    # PyInstaller cannot see); each must import from inside the exe. The exit status is the number that fail.
    import importlib
    bad = 0
    for name in (sys.argv[2].split(",") if len(sys.argv) > 2 else []):
        try:
            importlib.import_module("romorg." + name)
        except Exception:  # noqa: BLE001 - report any failure
            bad += 1
    sys.exit(bad)

if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
    # CHD engine check of THIS exe (romorg/selfcheck.py): libFLAC, Zstandard, worker processes, the writer.
    # A windowed exe has no console, so the report goes to the file named by "--report FILE" (default: stdout when
    # the caller redirected it, else selfcheck.log in the data folder). The exit status is 0 when it passed.
    args = sys.argv[2:]
    report = None
    if "--report" in args:
        i = args.index("--report")
        report = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    if report is None and sys.stdout is None:
        from romorg import paths

        report = str(paths.data_dir() / "selfcheck.log")
    stream = open(report, "w", encoding="utf-8", buffering=1) if report else sys.stdout
    sys.stdout = sys.stderr = stream
    from romorg import selfcheck

    try:
        code = selfcheck.main(args)
    except Exception as exc:  # noqa: BLE001 - the report must say why
        print(f"FAIL  self-check crashed: {type(exc).__name__}: {exc}")
        code = 1
    stream.flush()
    sys.exit(code)

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
