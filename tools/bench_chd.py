#!/usr/bin/env python3
"""Benchmark the CHD engine (read-only): old vs new strategies, wall time, MB/s, CPU use, peak RSS, identical hashes.

NOT run by the test suite. Everything is read-only with respect to the files you give it; scratch data (only the
``chdman`` strategy writes some) goes under ``--scratch`` (default ``~/.cache/romorg-fast-test``), and every scan
uses a fresh temporary data directory (no hash cache, no touching the app's own data).

Examples::

    tools/bench_chd.py disc "/roms/dreamcast/Toy Commander (USA)/Toy Commander (USA).chd" --strategies seq-python,seq-native,sched
    tools/bench_chd.py disc big.chd --workers 8 --chdman ~/.cache/chdman-try/usr/bin/chdman
    tools/bench_chd.py scan /roms/dreamcast --dat redump/dc.dat --workers 8
    tools/bench_chd.py scan /roms/dreamcast --dat redump/dc.dat --old-tree /path/to/old/checkout   # the previous code
    tools/bench_chd.py hash big.iso          # loose files: plain loop vs prefetch + parallel digests
    tools/bench_chd.py small folder-of-roms  # many small files: sequential vs thread pool

Strategies for ``disc``: ``seq-python`` (the old path: one process, pure-Python FLAC), ``seq-native`` (one process,
libFLAC), ``sched`` (the new scheduler, ``--workers`` processes + libFLAC), ``old-pool4`` (needs ``--old-tree``: the
previous 4 file-level processes), ``chdman`` (``--chdman PATH``: extractcd + hashing).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
MIB = 1 << 20


# ------------------------------------------------------------------ child side (one measured job)
def _job_disc(spec: dict) -> dict:
    sys.path.insert(0, spec.get("tree") or str(REPO))
    from romorg import chd
    path, strategy = spec["path"], spec["strategy"]
    t0 = time.perf_counter()
    out: dict[int, dict] = {}
    if strategy in ("seq-python", "seq-native"):
        with chd.Chd(path) as c:
            for i, t in enumerate(c.tracks):
                h = chd.hash_track(c, t)
                out[i] = {"size": h.size, "crc32": h.crc32, "md5": h.md5, "sha1": h.sha1}
    elif strategy == "sched":
        from romorg import chdsched
        with chd.Chd(path, load_map=False) as c, chdsched.Scheduler(spec["workers"]) as s:
            out = s.hash_tracks(c, range(len(c.tracks)))
            out = {i: dict(v) for i, v in out.items()}
    elif strategy == "old-pool4":
        from romorg import chdpool
        with chd.Chd(path, load_map=False) as c:
            pool = chdpool.HashPool(4)
            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(4) as ex:
                    res = list(ex.map(lambda i: pool.hash_track(path, i), range(len(c.tracks))))
            finally:
                pool.close()
            out = {i: {"size": r["size"], "crc32": r["crc32"], "md5": r["md5"], "sha1": r["sha1"]} for i, r in enumerate(res)}
    elif strategy == "chdman":
        chdman = spec["chdman"]
        work = Path(tempfile.mkdtemp(prefix="bench-", dir=spec["scratch"]))
        try:
            with chd.Chd(path, load_map=False) as c:
                gd = c.is_gd
                sizes = [t.size for t in c.tracks]
            env = dict(os.environ)
            lib = Path(chdman).resolve().parent.parent.parent / "lib"
            if lib.is_dir():
                env["LD_LIBRARY_PATH"] = f"{lib}:{env.get('LD_LIBRARY_PATH', '')}"
            sheet = work / ("disc.gdi" if gd else "disc.cue")
            cmd = [chdman, "extractcd", "-i", path, "-o", str(sheet)] + ([] if gd else ["-ob", str(work / "disc.bin")])
            if spec.get("host"):
                cmd = ["flatpak-spawn", "--host", "env", f"LD_LIBRARY_PATH={env.get('LD_LIBRARY_PATH', '')}"] + cmd
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            files = sorted(p for p in work.iterdir() if p.suffix in (".bin", ".raw"))
            if gd:
                import shlex
                files = [work / shlex.split(line)[4] for line in sheet.read_text().splitlines()[1:] if line.strip()]
            blob_sizes = sizes
            off = 0
            for i, f in enumerate(files if gd else [work / "disc.bin"] * len(sizes)):
                crc, md5, sha1 = 0, hashlib.md5(), hashlib.sha1()
                with open(f, "rb") as fh:
                    if not gd:
                        fh.seek(off)
                    left = blob_sizes[i] if not gd else f.stat().st_size
                    n = 0
                    while left > 0:
                        b = fh.read(min(4 * MIB, left))
                        left -= len(b)
                        n += len(b)
                        crc = zlib.crc32(b, crc)
                        md5.update(b)
                        sha1.update(b)
                off += blob_sizes[i]
                out[i] = {"size": n, "crc32": "%08x" % (crc & 0xFFFFFFFF), "md5": md5.hexdigest(), "sha1": sha1.hexdigest()}
        finally:
            subprocess.run(["rm", "-rf", str(work)])
    else:
        raise SystemExit(f"unknown strategy {strategy}")
    wall = time.perf_counter() - t0
    return {"wall": wall, "bytes": sum(v["size"] for v in out.values()), "tracks": out}


def _job_scan(spec: dict) -> dict:
    sys.path.insert(0, spec.get("tree") or str(REPO))
    os.environ["ROMORG_DATA_DIR"] = spec["data_dir"]
    from romorg import datfile, discsys
    dat = datfile.parse_redump(spec["dat"])
    t0 = time.perf_counter()
    kw = dict(use_cache=False, cache_path=Path(spec["data_dir"]) / "hashes.sqlite")
    if spec.get("workers"):
        kw["workers"] = spec["workers"]
    if spec.get("engine"):
        kw["engine"] = spec["engine"]
    r = discsys.scan(spec["root"], dat, **kw)
    wall = time.perf_counter() - t0
    rows = sorted((m.unit.game.name, m.level, tuple(t["sha1"] or "" for t in m.unit.tracks)) for m in r.matched)
    return {"wall": wall, "matched": len(r.matched), "unmatched": len(r.unmatched),
            "levels": {lv: sum(1 for x in r.matched if x.level == lv) for lv in sorted({m.level for m in r.matched})},
            "engine": getattr(r, "engine", ""), "engine_info": getattr(r, "engine_info", {}),
            "digest": hashlib.sha1(json.dumps(rows).encode()).hexdigest(),
            "names": sorted(m.unit.game.name for m in r.matched)}


def _job_hash(spec: dict) -> dict:
    sys.path.insert(0, spec.get("tree") or str(REPO))
    from romorg import multihash
    p = spec["path"]
    out = {}
    size = os.path.getsize(p)
    for name in ("plain-loop", "prefetch-seq", "prefetch-parallel"):
        t = time.perf_counter()
        if name == "plain-loop":
            crc = 0
            sha = hashlib.sha1()
            with open(p, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    crc = zlib.crc32(b, crc)
                    sha.update(b)
            res = ("%08x" % (crc & 0xFFFFFFFF), sha.hexdigest())
        else:
            r = multihash.hash_file(p, md5=False, parallel=(name == "prefetch-parallel"))
            res = (r[0], r[2])
        dt = time.perf_counter() - t
        out[name] = {"wall": dt, "mb_s": size / dt / MIB, "result": res}
    # three digests (crc32 + md5 + sha1), as the CHD scheduler needs them
    for par in (False, True):
        t = time.perf_counter()
        r = multihash.hash_file(p, md5=True, parallel=par)
        dt = time.perf_counter() - t
        out[f"3digest-{'parallel' if par else 'seq'}"] = {"wall": dt, "mb_s": size / dt / MIB, "result": (r[0], r[2])}
    return out


def _job_small(spec: dict) -> dict:
    sys.path.insert(0, spec.get("tree") or str(REPO))
    from concurrent.futures import ThreadPoolExecutor
    from romorg import scanner
    files = sorted(p for p in Path(spec["path"]).rglob("*") if p.is_file())
    total = sum(f.stat().st_size for f in files)
    out = {"files": len(files), "bytes": total}
    for name, n in (("sequential", 1), ("pool2", 2), ("pool4", 4), ("pool8", 8)):
        t = time.perf_counter()
        if n == 1:
            res = [scanner.hash_file(f) for f in files]
        else:
            with ThreadPoolExecutor(n) as ex:
                res = list(ex.map(scanner.hash_file, files))
        dt = time.perf_counter() - t
        out[name] = {"wall": dt, "mb_s": total / dt / MIB, "files_s": len(files) / dt,
                     "digest": hashlib.sha1(json.dumps(res).encode()).hexdigest()[:12]}
    return out


JOBS = {"disc": _job_disc, "scan": _job_scan, "hash": _job_hash, "small": _job_small}


# ------------------------------------------------------------------ parent side: run a job in a child, sample CPU / RSS
def _tree_rss_kb(root_pid: int) -> int:
    """VmRSS of the process and all its descendants (kB)."""
    kids: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", "rb") as f:
                st = f.read().decode("utf-8", "replace")
            ppid = int(st.rsplit(")", 1)[1].split()[1])
            kids.setdefault(ppid, []).append(int(d))
            with open(f"/proc/{d}/statm", "rb") as f:
                rss[int(d)] = int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
        except (OSError, ValueError, IndexError):
            continue
    total, stack = 0, [root_pid]
    while stack:
        p = stack.pop()
        total += rss.get(p, 0)
        stack.extend(kids.get(p, []))
    return total


def run_child(kind: str, spec: dict, env_extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update(env_extra or {})
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--child", kind, json.dumps(spec)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, _tree_rss_kb(proc.pid))
            stop.wait(0.15)
    th = threading.Thread(target=sample, daemon=True)
    th.start()
    out, err = proc.communicate()
    stop.set()
    th.join()
    wall = time.perf_counter() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if proc.returncode != 0:
        raise SystemExit(f"job failed ({kind}): {err[-800:]}")
    res = json.loads(out.strip().splitlines()[-1])
    res["_cpu"] = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    res["_wall_total"] = wall
    res["_rss_mb"] = peak / 1024
    return res


def table(rows: list[list[str]], header: list[str]) -> str:
    cols = list(zip(*([header] + rows)))
    widths = [max(len(str(c)) for c in col) for col in cols]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    return "\n".join([fmt.format(*header), fmt.format(*["-" * w for w in widths])] + [fmt.format(*[str(c) for c in r]) for r in rows])


def cmd_disc(a: argparse.Namespace) -> None:
    rows, reference = [], {}
    strategies = a.strategies.split(",")
    for path in a.paths:
        base = os.path.basename(path)
        ref = None
        for strat in strategies:
            if strat == "old-pool4" and not a.old_tree:
                continue
            if strat == "chdman" and not a.chdman:
                continue
            spec = {"path": path, "strategy": strat, "workers": a.workers, "chdman": a.chdman, "scratch": a.scratch,
                    "tree": a.old_tree if strat == "old-pool4" else None, "host": a.host}
            env = {"ROMORG_NO_NATIVE_FLAC": "1"} if strat in ("seq-python", "old-pool4") else {}
            if a.libflac and strat not in ("seq-python", "old-pool4"):
                env["ROMORG_LIBFLAC"] = a.libflac
            r = run_child("disc", spec, env)
            sig = {int(i): (v["size"], v["crc32"], v["md5"], v["sha1"]) for i, v in r["tracks"].items()}
            if ref is None:
                ref = sig
            same = "yes" if sig == ref else "NO"
            cores = r["_cpu"] / r["_wall_total"]
            rows.append([base[:38], f"{r['bytes'] / MIB:.0f}", strat, f"{r['wall']:.1f}", f"{r['bytes'] / MIB / r['wall']:.0f}",
                         f"{r['_cpu']:.0f}", f"{cores:.1f}", f"{r['_rss_mb']:.0f}", same])
            print(table(rows[-1:], ["disc", "MB", "strategy", "wall s", "MB/s", "cpu s", "cores", "peak RSS MB", "same"]).splitlines()[-1],
                  flush=True)
        reference[path] = ref
    print()
    print(table(rows, ["disc", "MB", "strategy", "wall s", "MB/s", "cpu s", "cores", "peak RSS MB", "same hashes"]))


def cmd_scan(a: argparse.Namespace) -> None:
    rows, ref = [], None
    for label, tree, workers, engine, env in a.runs:
        data = tempfile.mkdtemp(prefix="bench-data-", dir=a.scratch)
        try:
            spec = {"root": a.folder, "dat": a.dat, "data_dir": data, "workers": workers, "engine": engine, "tree": tree}
            r = run_child("scan", spec, env)
        finally:
            subprocess.run(["rm", "-rf", data])
        if ref is None:
            ref = r
        same = "yes" if (r["names"] == ref["names"] and r["levels"] == ref["levels"]) else "NO"
        rows.append([label, f"{r['wall']:.1f}", r["matched"], r["unmatched"], json.dumps(r["levels"]),
                     f"{r['_cpu']:.0f}", f"{r['_cpu'] / r['_wall_total']:.1f}", f"{r['_rss_mb']:.0f}", same])
        print(rows[-1], flush=True)
        if r.get("engine_info", {}).get("text"):
            print("   ", r["engine_info"]["text"])
    print()
    print(table(rows, ["run", "wall s", "matched", "unmatched", "levels", "cpu s", "cores", "peak RSS MB", "same results"]))


def main() -> None:
    if len(sys.argv) > 3 and sys.argv[1] == "--child":
        print(json.dumps(JOBS[sys.argv[2]](json.loads(sys.argv[3]))))
        return
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    common.add_argument("--scratch", default=str(Path.home() / ".cache" / "romorg-fast-test"))
    common.add_argument("--old-tree", help="checkout of the previous code (e.g. `git archive <rev> | tar -x -C DIR`)")
    common.add_argument("--libflac", help="libFLAC.so path for the native strategies (when the system has none visible)")
    d = sub.add_parser("disc", parents=[common], help="time hashing every track of one or more CHDs")
    d.add_argument("paths", nargs="+")
    d.add_argument("--strategies", default="seq-python,seq-native,sched")
    d.add_argument("--chdman", help="path of a chdman binary for the 'chdman' strategy")
    d.add_argument("--host", action="store_true", help="run chdman through `flatpak-spawn --host` (Flatpak sandbox)")
    s = sub.add_parser("scan", parents=[common], help="time a folder scan (fresh data dir, no cache)")
    s.add_argument("folder")
    s.add_argument("--dat", required=True, help="the Redump .dat of the system")
    s.add_argument("--runs", default="new", help="comma list: new, new1 (1 worker), old (needs --old-tree), chdman-forced")
    h = sub.add_parser("hash", parents=[common], help="loose big file: plain loop vs prefetch + parallel digests")
    h.add_argument("path")
    m = sub.add_parser("small", parents=[common], help="many small files: sequential vs thread pool")
    m.add_argument("path")
    a = ap.parse_args()
    if a.cmd == "disc":
        cmd_disc(a)
    elif a.cmd == "scan":
        runs = []
        for name in a.runs.split(","):
            env = {"ROMORG_LIBFLAC": a.libflac} if a.libflac else {}
            if name == "new":
                runs.append((f"new ({a.workers} workers)", None, a.workers, None, env))
            elif name == "new1":
                runs.append(("new, 1 worker", None, 1, None, env))
            elif name == "old":
                if not a.old_tree:
                    raise SystemExit("--runs old needs --old-tree")
                runs.append(("old (4 file-level processes)", a.old_tree, 4, None, {"ROMORG_NO_NATIVE_FLAC": "1"}))
        a.runs = runs
        cmd_scan(a)
    elif a.cmd == "hash":
        r = run_child("hash", {"path": a.path})
        base = None
        for k, v in r.items():
            if k.startswith("_"):
                continue
            base = base or v["result"]
            print(f"{k:20} {v['wall']:6.2f} s  {v['mb_s']:7.0f} MB/s  {'same' if v['result'] == base else 'DIFFERENT'}")
    elif a.cmd == "small":
        r = run_child("small", {"path": a.path})
        print(f"{r['files']} files, {r['bytes'] / MIB:.0f} MB")
        for k in ("sequential", "pool2", "pool4", "pool8"):
            v = r[k]
            print(f"{k:12} {v['wall']:6.2f} s  {v['mb_s']:7.0f} MB/s  {v['files_s']:8.0f} files/s  digest {v['digest']}")


if __name__ == "__main__":
    main()
