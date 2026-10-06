"""Worker process of :mod:`romorg.chdsched`: decodes ranges of CHD tracks on request.

Protocol (binary over stdin / stdout; ``python -m romorg.chdworker``, never started by anything but the scheduler)::

    request  (one JSON line): {"id": 7, "path": "...", "sig": [size, mtime_ns], "track": 2, "first": 4096, "count": 1792}
    reply    (JSON line + raw bytes): {"id": 7, "n": 4214784}\\n<n bytes of extracted track data>
    or       {"id": 7, "err": {"kind": "unsupported" | "corrupt" | "other", "msg": "...", "needs_chdman": false}}

The CHD writer (:mod:`romorg.chdwrite`) uses the same processes to compress::

    request  {"id": 7, "op": "compress", "codecs": ["cdlz", "cdzl", "cdfl", ""], "hunk_bytes": 19584, "cd": true,
              "hints": ["data", "audio", ...], "n": <bytes that follow: the hunks, one after the other>}
    reply    {"id": 7, "n": <bytes that follow>, "items": [[slot, length, crc16], ...]}\n<the compressed hunks>

``first`` / ``count`` are frames of the track (2048-byte units for a DVD CHD) and the reply is exactly what
``chdman extractcd`` / ``extractdvd`` would write for them. The worker keeps its opened CHDs (and their parsed hunk
maps) between requests, so a file's map is parsed once per worker, not once per chunk. It exits at EOF of stdin.

Closing a file it keeps open (Windows cannot rename or delete a file another process has open)::

    request  {"id": -1, "op": "release", "path": "..."}
    reply    {"id": -1, "n": 0}
"""

from __future__ import annotations

import json
import os
import sys
from collections import OrderedDict

MAX_OPEN = 4
ENV_JITTER = "ROMORG_CHDWORKER_JITTER_MS"      # test hooks: random delay per request (out-of-order delivery) ...
ENV_CRASH = "ROMORG_CHDWORKER_CRASH"            # ... and "always" / "once:<marker file>" = die like a segfault


def _same(path: str) -> str:
    """``path`` in a form that compares equal for the same file (Windows: any case, either slash)."""
    return os.path.normcase(os.path.abspath(path))


def _err(rid, kind: str, exc: BaseException) -> dict:
    return {"id": rid, "err": {"kind": kind, "msg": f"{type(exc).__name__}: {exc}",
                               "needs_chdman": bool(getattr(exc, "needs_chdman", False))}}


def serve(stdin=None, stdout=None) -> None:
    # The reader and the writer are imported on the first request that needs them: a worker that only compresses
    # (the writer's) or only decodes (the scheduler's) starts sooner - a dozen of them start at once per job.
    chdlib = None
    inp = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    cache: "OrderedDict[str, tuple]" = OrderedDict()
    compressors: dict = {}
    jitter = float(os.environ.get(ENV_JITTER) or 0) / 1000.0
    crash = os.environ.get(ENV_CRASH, "")

    def write_all(data) -> None:
        # with ``python -u`` sys.stdout.buffer is a raw FileIO: one write() may take only part of a big reply
        view = memoryview(data)
        while view:
            n = out.write(view)
            if n is None or n <= 0:
                raise BrokenPipeError("the scheduler stopped reading")
            view = view[n:]

    def reply(head: dict, payload: bytes = b"") -> None:
        write_all(json.dumps(head, separators=(",", ":")).encode() + b"\n")
        if payload:
            write_all(payload)
        out.flush()

    for line in inp:
        line = line.strip()
        if not line:
            continue
        rid = None
        if crash == "always":
            os._exit(139)
        if crash.startswith("once:") and not os.path.exists(crash[5:]):
            open(crash[5:], "w").close()
            os._exit(139)
        if jitter:
            import random
            import time
            time.sleep(random.random() * jitter)
        try:
            req = json.loads(line)
            rid = req.get("id")
            if req.get("op") == "compress":
                from . import chdwrite
                n = int(req["n"])
                data = inp.read(n)
                if len(data) != n:
                    return
                key = (tuple(req["codecs"]), int(req["hunk_bytes"]), bool(req["cd"]))
                if key not in compressors:
                    compressors[key] = chdwrite._Compressor(*key)
                items, blob = chdwrite.compress_many(compressors[key], data, req["hints"])
                reply({"id": rid, "n": len(blob), "items": items}, blob)
                continue
            if req.get("op") == "release":
                want = _same(req["path"])
                for key in [k for k in cache if _same(k) == want]:
                    cache.pop(key)[1].close()
                reply({"id": rid, "n": 0})
                continue
            if chdlib is None:
                from . import chd as chdlib
                chdlib.Chd.threads = 1  # the worker processes are the parallelism: no decode threads inside them
            path = req["path"]
            sig = tuple(req.get("sig") or ())
            hit = cache.get(path)
            if hit is not None and hit[0] != sig:
                hit[1].close()
                del cache[path]
                hit = None
            if hit is None:
                while len(cache) >= MAX_OPEN:
                    _p, (_s, old) = cache.popitem(last=False)
                    old.close()
                hit = cache[path] = (sig, chdlib.Chd(path))
            else:
                cache.move_to_end(path)
            info = hit[1]
            data = info.read_track_range(info.tracks[int(req["track"])], int(req["first"]), int(req["count"]))
            reply({"id": rid, "n": len(data)}, data)
        except (BrokenPipeError, KeyboardInterrupt):
            return
        except Exception as exc:  # noqa: BLE001 - reported to the parent
            kind = "other"
            if chdlib is not None and isinstance(exc, chdlib.ChdUnsupported):
                kind = "unsupported"
            elif chdlib is not None and isinstance(exc, chdlib.ChdError):
                kind = "corrupt"
            try:
                reply(_err(rid, kind, exc))
            except OSError:
                return


def main() -> int:
    try:
        os.nice(5)          # the app's UI and the server stay responsive while every core decodes
    except (OSError, AttributeError):
        pass
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
