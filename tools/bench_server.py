#!/usr/bin/env python3
"""Time the server's hot paths on a real folder: scan (warm hash cache), first result pages, search, sorting, the library
plan and a re-plan after one rule changed.

    python3 tools/bench_server.py "Nintendo Game Boy Advance" /path/to/gba [--cold]

Runs the real server in this process on a COPY of the data directory (DATs are symlinked, the hash cache and config are
copied), so your own cache and settings are never touched."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REAL = Path(os.environ.get("ROMORG_REAL_DATA", Path.home() / ".local/share/simple-rom-organiser"))


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    platform, folder = sys.argv[1], sys.argv[2]
    tmp = Path(tempfile.mkdtemp(prefix="romorg-bench-"))
    for child in REAL.iterdir():
        if child.name in ("cache",):
            if "--cold" not in sys.argv:
                shutil.copytree(child, tmp / child.name)
        elif child.name in ("config.json",):
            shutil.copy(child, tmp / child.name)
        elif child.name not in ("instance.json", "update.lock"):
            (tmp / child.name).symlink_to(child)
    os.environ["ROMORG_DATA_DIR"] = str(tmp)
    os.environ["ROMORG_OFFLINE"] = "1"
    sys.path.insert(0, str(HERE))
    from romorg import server

    srv = server.make_server("127.0.0.1", 0, auto_update=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.port}"
    token = srv.app.token

    def call(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(base + path, method=method, headers={"X-Romorg-Token": token,
                                     "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read())

    def timed(label: str, fn):
        t = time.perf_counter()
        out = fn()
        print(f"{label:46s} {time.perf_counter() - t:8.2f} s", flush=True)
        return out

    def scan():
        job = call("POST", "/api/scan", {"path": folder, "platform": platform})["job"]
        while True:
            job = call("GET", "/api/job") or job
            if job["status"] not in ("running", "queued", "pending"):
                return job
            time.sleep(0.2)

    q = urllib.parse.urlencode
    if "--profile" in sys.argv:
        import cProfile
        import pstats
        app = srv.app

        def prof(label, fn):
            pr = cProfile.Profile()
            pr.enable()
            fn()
            pr.disable()
            print(f"\n=== {label}")
            pstats.Stats(pr).sort_stats(os.environ.get("BENCH_SORT", "tottime")).print_stats(int(os.environ.get("BENCH_N", "22")))

        timed("scan", scan)
        prof("games page 1", lambda: app.scan_results({"kind": "games", "limit": "50"}, None))
        prof("library plan", lambda: app.library_plan({}, {"limit": 50}))
        flip = not call("GET", "/api/library/profile?" + urllib.parse.urlencode({"platform": platform}))["profile"].get("best_variant")
        call("POST", "/api/library/profile", {"platform": platform, "best_variant": flip})
        prof("library plan after rule", lambda: app.library_plan({}, {"limit": 50}))
        import types
        job = types.SimpleNamespace(report=lambda *a, **k: None, cancel=threading.Event())
        prof("scan (warm)", lambda: app._run_scan(job, Path(folder), app._resolve_platform(platform)))
        return 0
    timed("scan", scan)
    timed("scan again (warm)", scan)
    timed("games page 1", lambda: call("GET", "/api/scan/results?" + q({"kind": "games", "limit": 50})))
    timed("games page 1 again", lambda: call("GET", "/api/scan/results?" + q({"kind": "games", "limit": 50})))
    for sort in ("name_asc", "name_desc", "rating_desc", "rating_asc"):
        timed(f"games sort {sort}", lambda s=sort: call("GET", "/api/scan/results?" + q({"kind": "games", "limit": 50, "sort": s})))
        timed(f"games sort {sort} page 2", lambda s=sort: call("GET", "/api/scan/results?" + q({"kind": "games", "limit": 50, "offset": 50, "sort": s})))
    for term in ("mario", "zelda", "a"):
        timed(f"games search '{term}'", lambda t=term: call("GET", "/api/scan/results?" + q({"kind": "games", "limit": 50, "q": t})))
    timed("library plan (first)", lambda: call("POST", "/api/library/plan", {"limit": 50}))
    timed("library plan again", lambda: call("POST", "/api/library/plan", {"limit": 50}))
    timed("library plan sort name", lambda: call("POST", "/api/library/plan", {"limit": 50, "sort": "name_asc"}))
    timed("library plan search 'a'", lambda: call("POST", "/api/library/plan", {"limit": 50, "q": "a"}))
    prof = call("GET", "/api/library/profile?" + q({"platform": platform}))
    flip = not prof["profile"].get("best_variant")
    timed("save a changed rule", lambda: call("POST", "/api/library/profile", {"platform": platform, "best_variant": flip}))
    timed("library plan after rule change", lambda: call("POST", "/api/library/plan", {"limit": 50}))
    timed("library totals", lambda: call("GET", "/api/library/totals?" + q({"platform": platform})))
    import resource
    print(f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MiB")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
