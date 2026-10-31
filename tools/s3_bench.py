#!/usr/bin/env python3
"""Benchmark of the S3 upload path (romorg.s3 / romorg.s3archive.upload_moves) against a REAL bucket, with real files.

SAFE BY CONSTRUCTION: every object goes below a throw-away prefix ``<configured prefix>_romorg-perftest-<timestamp>/``; a guard
around ``Client._request`` refuses any request outside it (but one ``max-keys=1`` call that asks the service for its region); at
the end (also after an error) every object below the prefix is deleted and every unfinished multipart upload aborted, and the
prefix is listed to show it is empty. The source files are scratch copies (``--src``), uploaded as copies (never deleted).
The keys are read from the app's data folder READ-ONLY (``--data`` = the folder holding config.json and s3-secret) and are never
printed or written anywhere. The app itself is not started, ``ROMORG_DATA_DIR`` is not touched.

    python tools/s3_bench.py --data ~/.local/share/simple-rom-organiser --src ~/.cache/romorg-speed/s3perf \
        --run small:4 --run medium:8 --run large:1 --out results.jsonl

Workloads are the folders ``small`` / ``medium`` / ``large`` of ``--src`` (``small:8:100`` = 8 workers, the first 100 files).
``latency`` and ``stream`` are extra runs (round trip of a HEAD of a missing key; one 64 MiB part PUT).
"""

from __future__ import annotations

import argparse
import datetime
import http.client
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from romorg import s3, s3archive, sortroot  # noqa: E402


class Counter:
    """Counts what goes over the wire without caring how the client is written (patches http.client)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.connects = 0
        self.by_method: dict[str, int] = {}
        self.sleeps = 0
        self._orig_req = http.client.HTTPConnection.request
        self._orig_conn = http.client.HTTPConnection.connect

    def install(self) -> None:
        me = self

        def request(conn, method, url, *a, **k):  # noqa: ANN001
            with me.lock:
                me.requests += 1
                me.by_method[method] = me.by_method.get(method, 0) + 1
            return me._orig_req(conn, method, url, *a, **k)

        def connect(conn):  # noqa: ANN001
            with me.lock:
                me.connects += 1
            return me._orig_conn(conn)
        http.client.HTTPConnection.request = request
        http.client.HTTPConnection.connect = connect

    def sleep(self, s: float) -> None:
        with self.lock:
            self.sleeps += 1
        time.sleep(s)

    def reset(self) -> None:
        with self.lock:
            self.requests = self.connects = self.sleeps = 0
            self.by_method = {}


def load(data: Path):
    cfg = json.loads((data / "config.json").read_text(encoding="utf-8"))
    c = cfg["s3"]
    target = s3archive.parse(cfg["archive_dir"])
    secret = (data / "s3-secret").read_text(encoding="utf-8").strip()
    return c, target, secret


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--run", action="append", default=[], help="small:W[:N] | medium:W[:N] | large:W | latency | stream")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--parts", type=int, help="parts of a multipart upload on the wire at once (the Client's ``parts``)")
    ap.add_argument("--part-size-mib", type=int, help="multipart part size (and threshold) in MiB")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    c, target, secret = load(args.data)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = f"{target.prefix}_romorg-perftest-{stamp}"            # (Config adds the "/")
    safe = prefix + "/"
    counter = Counter()
    counter.install()
    cfg = s3.Config(c["endpoint"], target.bucket, c["access_key"], secret, c.get("region", ""), prefix)
    assert cfg.prefix == safe, cfg.prefix
    probed: list = []

    def mk(**kw) -> s3.Client:
        cl = s3.Client(cfg, sleep=counter.sleep, **kw)
        if cfg.region == "" and client_region:
            cl._region = client_region[0]
        real_request = cl._request

        def guarded(method, key, query=None, *a, **k):
            q = query or {}
            if key.startswith(safe) or (key == "" and q.get("prefix", "").startswith(safe)):
                return real_request(method, key, query, *a, **k)
            if key == "" and not q.get("prefix") and q.get("max-keys") == "1" and not probed and method == "GET":
                probed.append(1)                                    # the one region probe of Client.region
                return real_request(method, key, query, *a, **k)
            if key == "" and method == "POST" and "delete" in q:      # delete_many: the keys are checked by cleanup()
                return real_request(method, key, query, *a, **k)
            raise RuntimeError(f"REFUSED request outside the test prefix: {method} {key!r} {q}")
        cl._request = guarded                                       # type: ignore[method-assign]
        return cl
    client_region: list = []
    kw = {}
    if args.parts:
        kw["parts"] = args.parts
    if args.part_size_mib:
        kw["part_size"] = kw["multipart_threshold"] = args.part_size_mib << 20
    client = mk(**kw)
    print(f"endpoint={c['endpoint']} bucket={target.bucket} test prefix={safe}", flush=True)
    out = args.out.open("a", encoding="utf-8") if args.out else None

    def cleanup() -> dict:
        keys = [o.key for o in client.list("")]
        assert all(k.startswith(safe) for k in keys)
        errs = client.delete_many(keys) if keys else []
        aborted = 0
        for u in client.list_uploads():
            if u["key"].startswith(safe):
                client.abort_upload(u["key"], u["upload_id"])
                aborted += 1
        return {"deleted": len(keys), "errors": len(errs), "aborted": aborted}

    def record(row: dict) -> None:
        row = {"label": args.label, **row} if args.label and "run" in row else row
        print(json.dumps(row), flush=True)
        if out:
            out.write(json.dumps(row) + "\n")
            out.flush()

    try:
        print("region:", client.region, flush=True)
        client_region.append(client.region)
        for spec in args.run:
            name, *rest = spec.split(":")
            counter.reset()
            if name == "latency":
                ts = []
                for i in range(20):
                    t = time.perf_counter()
                    client.head(safe + f"missing-{i}")
                    ts.append(time.perf_counter() - t)
                record({"run": "latency", "head_missing_ms_median": round(sorted(ts)[10] * 1000, 1), "min": round(min(ts) * 1000, 1),
                        "connects": counter.connects, "requests": counter.requests})
                continue
            if name == "parts":                  # parts:K[:MiB] = one multipart upload of a slice, K parts of 8 MiB on the wire at once
                k, mib = int(rest[0]), int(rest[1]) if len(rest) > 1 else 32
                tmp = args.src / "parts.bin"
                with open(args.src / "large" / "big.chd", "rb") as a, open(tmp, "wb") as b:
                    b.write(a.read(mib << 20))
                cl = mk(parts=k, part_size=8 << 20, multipart_threshold=8 << 20)
                t = time.perf_counter()
                cl.put_file(safe + "parts.bin", tmp)
                dt = time.perf_counter() - t
                tmp.unlink()
                record({"run": f"parts{k}", "MiB": mib, "s": round(dt, 2), "MB_s": round((mib << 20) / 1e6 / dt, 1), "requests": counter.requests,
                        "connects": counter.connects})
                cl.delete(safe + "parts.bin")
                cl.close()
                continue
            if name == "stream":
                n = 64 << 20
                f = args.src / "large" / "big.chd"
                tmp = args.src / "stream.bin"
                with open(f, "rb") as a, open(tmp, "wb") as b:
                    b.write(a.read(n))
                t = time.perf_counter()
                client.put_file(safe + "stream.bin", tmp)
                dt = time.perf_counter() - t
                tmp.unlink()
                record({"run": "stream64MiB_single_PUT", "s": round(dt, 2), "MB_s": round(n / 1e6 / dt, 1), "requests": counter.requests})
                client.delete(safe + "stream.bin")
                continue
            workers = int(rest[0]) if rest else 4
            files = sorted((args.src / name).iterdir())
            if len(rest) > 1:
                files = files[:int(rest[1])]
            base = Path("/nonexistent-romorg-standin") / "bucket" / prefix
            inv = s3archive.Inventory(client, target.__class__(target.bucket, safe), base)
            moves = [sortroot.SMove(f, base / name / f.name, name, "", "file", f.stat().st_size, True) for f in files]
            total = sum(f.stat().st_size for f in files)
            t = time.perf_counter()
            res = s3archive.upload_moves(client, inv, moves, workers=workers)
            dt = time.perf_counter() - t
            record({"run": name, "workers": workers, "files": len(files), "MB": round(total / 1e6, 1), "s": round(dt, 2),
                    "MB_s": round(total / 1e6 / dt, 1), "files_s": round(len(files) / dt, 1),
                    "requests": counter.requests, "req_per_file": round(counter.requests / len(files), 2),
                    "connects": counter.connects, "retries": counter.sleeps, "by_method": counter.by_method,
                    "failed": len(res["failed"]), "uploaded": res["uploaded"], "err": (res["failed"][:1] or [{}])[0].get("error", "")})
            record({"cleanup": cleanup()})
    finally:
        try:
            record({"final_cleanup": cleanup()})
            left = list(client.list(""))
            record({"prefix_empty_after": not left, "objects_left": len(left), "uploads_left": len(client.list_uploads())})
        except Exception as exc:  # noqa: BLE001
            print("CLEANUP FAILED:", type(exc).__name__, exc, "prefix:", safe, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
