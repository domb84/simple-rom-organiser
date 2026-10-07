#!/usr/bin/env python3
"""Benchmark the built-in CHD writer (romorg.chdwrite) against chdman: seconds, size, SHA-1, engine, workers.

NOT run by the test suite. The inputs are only read. Every conversion runs in a fresh interpreter (as in the app),
and the CHDs are written to ``--out`` (default: a new temporary folder) and deleted after each run unless
``--keep`` is given.

Each input is ``PATH[@cd|@dvd][=REFERENCE.chd]``: a ``.cue``, ``.gdi`` or ``.iso`` (an ``.iso`` is written as a DVD
unless ``@cd``), and optionally a CHD made by chdman from the same input whose SHA-1 the output must have (handy
when chdman is slow or not at hand). With ``--chdman PATH`` chdman itself converts the same inputs, timed the same
way, and its SHA-1 becomes the reference.

Examples::

    tools/bench_chdwrite.py game.cue disc.gdi ps2.iso --chdman /usr/bin/chdman --repeat 3
    tools/bench_chdwrite.py game.cue=chdman/game.chd --preset default,zstd --json results.json
    tools/bench_chdwrite.py ps2.iso@cd --workers 8 --libflac /path/to/libFLAC.so --verify

Columns: ``s`` seconds of the conversion itself (``write_chd``, or the whole chdman run), ``total s`` with the
interpreter start, ``cpu s`` / ``procs`` / ``peak MB`` of the run and every process it started (``procs`` and an
all-processes peak on Windows only), ``size`` and ``x ref`` against the reference, ``sha1`` = equal to the reference,
``engine`` / ``workers`` / ``fallbacks`` as reported by the writer. ``--verify`` also times what a Convert does next:
hashing every track of the new CHD with the app's scheduler (``verify s``).

chdman always runs its default preset (here and in the table), so with ``--preset zstd`` the ``x ref`` of the
built-in zstd rows compares against default-preset chdman output: not like-for-like (``sha1`` is still valid, the
header SHA-1 covers the raw data).

chdman on Windows: some builds fail on absolute paths, so it is started in the folder that holds both the input
and ``--out``, with relative paths (both must be on one drive).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import benchproc  # noqa: E402

MIB = 1 << 20


# ------------------------------------------------------------------ child side: one conversion with the built-in writer
def _child(spec: dict) -> dict:
    sys.path.insert(0, spec["repo"])
    from romorg import cdimage, chd, chdsched, chdwrite, flacenc
    image = cdimage.open_image(spec["input"], spec["mode"])
    t0 = time.perf_counter()
    info = chdwrite.write_chd(spec["out"], image, preset=spec["preset"], threads=spec.get("workers") or None)
    seconds = time.perf_counter() - t0
    out = {"seconds": seconds, "info": info, "flac_encoder": flacenc.available()}
    if spec.get("verify"):
        workers = spec.get("workers") or chdsched.default_workers()
        t0 = time.perf_counter()
        with chd.Chd(spec["out"], load_map=False) as c, chdsched.Scheduler(workers) as s:
            got = s.hash_tracks(c, range(len(c.tracks)))
            out["verify_pooled"] = s.pooled
            s.release(spec["out"])
        out["verify_seconds"] = time.perf_counter() - t0
        out["track_sha1"] = [got[i]["sha1"] for i in sorted(got)]
    return out


# ------------------------------------------------------------------ parent side
def parse_input(text: str) -> dict:
    ref = None
    if "=" in text:
        text, ref = text.rsplit("=", 1)
    mode = None
    for tag in ("@cd", "@dvd"):
        if text.lower().endswith(tag):
            text, mode = text[:-len(tag)], "createcd" if tag == "@cd" else "createdvd"
    path = Path(text).resolve()
    if not path.is_file():
        raise SystemExit(f"no such input: {path}")
    ext = path.suffix.lower()
    if ext not in (".cue", ".gdi", ".iso"):
        raise SystemExit(f"{path.name}: not a .cue, .gdi or .iso")
    if mode is None:
        mode = "createdvd" if ext == ".iso" else "createcd"
    if mode == "createdvd" and ext != ".iso":
        raise SystemExit(f"{path.name}: only an .iso can be written as a DVD")
    label = path.stem + ("_dvd" if mode == "createdvd" else "_cd" if ext == ".iso" else "")
    return {"path": path, "mode": mode, "ref": Path(ref).resolve() if ref else None, "label": label}


def header_sha1(path: Path) -> str:
    sys.path.insert(0, str(REPO))
    from romorg import chd
    with chd.Chd(path, load_map=False) as c:
        return c.sha1


def remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def run_builtin(item: dict, preset: str, a: argparse.Namespace, out: Path) -> dict:
    spec = {"repo": str(REPO), "input": str(item["path"]), "mode": item["mode"], "out": str(out),
            "preset": preset, "workers": a.workers, "verify": a.verify}
    env = dict(os.environ)
    if a.libflac:
        env["ROMORG_LIBFLAC"] = a.libflac
    m = benchproc.run([sys.executable, "-B", str(Path(__file__).resolve()), "--child", json.dumps(spec)], env=env)
    if m.returncode != 0:
        raise SystemExit(f"the built-in writer failed on {item['path'].name}: {m.stderr[-1500:]}")
    res = json.loads(m.stdout.strip().splitlines()[-1])
    info = res["info"]
    row = {"tool": "builtin", "seconds": res["seconds"], "total": m.wall, "cpu": m.cpu, "procs": m.processes,
           "peak_mb": m.peak_mb, "size": info["size"], "sha1": info["sha1"], "engine": info["engine"],
           "workers": info["workers"], "fallbacks": info["fallbacks"], "stored": info["stored"],
           "flac_encoder": res["flac_encoder"]}
    if a.verify:
        row.update(verify_seconds=res["verify_seconds"], verify_pooled=res["verify_pooled"])
    return row


def run_chdman(item: dict, a: argparse.Namespace, out: Path) -> dict:
    src = item["path"]
    try:
        cwd = os.path.commonpath([str(src.parent), str(out.parent)])
    except ValueError:
        raise SystemExit(f"chdman needs the input and --out on one drive ({src}, {out.parent})") from None
    cmd = [a.chdman, item["mode"], "-i", os.path.relpath(src, cwd), "-o", os.path.relpath(out, cwd), "-f"]
    m = benchproc.run(cmd, cwd=cwd)
    if m.returncode != 0 or not out.is_file():
        raise SystemExit(f"chdman failed on {src.name}: {(m.stderr or m.stdout)[-1500:]}")
    return {"tool": "chdman", "seconds": m.wall, "total": m.wall, "cpu": m.cpu, "procs": m.processes,
            "peak_mb": m.peak_mb, "size": out.stat().st_size, "sha1": header_sha1(out), "engine": "chdman",
            "workers": None, "fallbacks": None}


def fmt(v, spec: str = "", dash: str = "-") -> str:
    return dash if v is None else format(v, spec)


def table(rows: list, header: list) -> str:
    cols = list(zip(*([header] + rows)))
    widths = [max(len(str(c)) for c in col) for col in cols]
    line = "  ".join("{:<%d}" % w for w in widths)
    return "\n".join([line.format(*header), line.format(*["-" * w for w in widths])]
                     + [line.format(*[str(c) for c in r]) for r in rows])


HEAD = ["input", "preset", "tool", "s", "total s", "cpu s", "procs", "peak MB", "size MB", "x ref", "sha1",
        "engine", "workers", "fallbacks"]


def row_cells(r: dict) -> list:
    return [r["input"], r["preset"], r["tool"], fmt(r["seconds"], ".2f"), fmt(r["total"], ".2f"), fmt(r["cpu"], ".0f"),
            fmt(r["procs"]), fmt(r["peak_mb"], ".0f"), f"{r['size'] / MIB:.1f}", fmt(r.get("ratio"), ".4f"),
            {True: "same", False: "DIFFERENT", None: "-"}[r.get("same")], r["engine"], fmt(r["workers"]),
            fmt(r["fallbacks"])] + ([fmt(r.get("verify_seconds"), ".2f")] if "verify_seconds" in r else [])


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(_child(json.loads(sys.argv[2]))))
        return
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", metavar="PATH[@cd|@dvd][=REFERENCE.chd]")
    ap.add_argument("--preset", default="default", help="comma list of writer presets: default, zstd")
    ap.add_argument("--chdman", help="also convert with this chdman (its SHA-1 becomes the reference)")
    ap.add_argument("--repeat", type=int, default=1, help="runs of each conversion (the summary shows medians)")
    ap.add_argument("--workers", type=int, default=0, help="writer threads / worker processes (default: the app's)")
    ap.add_argument("--libflac", help="libFLAC library for the writer's FLAC encoder ($ROMORG_LIBFLAC)")
    ap.add_argument("--verify", action="store_true", help="also time hashing the new CHD with the scheduler")
    ap.add_argument("--out", help="folder for the CHDs (default: a new temporary folder, removed at the end)")
    ap.add_argument("--keep", action="store_true", help="keep the last CHD of each conversion")
    ap.add_argument("--json", help="write every run and the summary to this file")
    a = ap.parse_args()
    presets = [p for p in a.preset.split(",") if p]
    for p in presets:
        if p not in ("default", "zstd"):
            ap.error(f"unknown preset {p!r} (default, zstd)")
    for opt in ("chdman", "libflac"):
        if getattr(a, opt) and not Path(getattr(a, opt)).is_file():
            ap.error(f"--{opt}: no such file: {getattr(a, opt)}")
    items = [parse_input(t) for t in a.inputs]
    tmp = None
    if a.out:
        out_dir = Path(a.out).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = Path(tmp := tempfile.mkdtemp(prefix="bench-chdwrite-"))
    runs: list = []
    head = HEAD + (["verify s"] if a.verify else [])
    print(table([], head).splitlines()[0], flush=True)
    try:
        for item in items:
            ref_sha1 = header_sha1(item["ref"]) if item["ref"] else None
            ref_size = item["ref"].stat().st_size if item["ref"] else None
            plan = ([("chdman", "default")] if a.chdman else []) + [("builtin", p) for p in presets]
            for rep in range(a.repeat):
                for tool, preset in plan:
                    out = out_dir / f"{item['label']}.{tool}.{preset}.chd"
                    remove(out)
                    r = run_chdman(item, a, out) if tool == "chdman" else run_builtin(item, preset, a, out)
                    if tool == "chdman" and rep == 0:
                        ref_sha1, ref_size = r["sha1"], r["size"]
                    r.update(input=item["label"], preset=preset, repeat=rep, ref_sha1=ref_sha1,
                             same=None if ref_sha1 is None else r["sha1"] == ref_sha1,
                             ratio=None if not ref_size else r["size"] / ref_size)
                    runs.append(r)
                    print(table([row_cells(r)], head).splitlines()[-1], flush=True)
                    if not (a.keep and rep == a.repeat - 1):
                        remove(out)
    finally:
        if tmp and not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    summary = []
    for item in items:
        for tool, preset in ([("chdman", "default")] if a.chdman else []) + [("builtin", p) for p in presets]:
            mine = [r for r in runs if r["input"] == item["label"] and r["tool"] == tool and r["preset"] == preset]
            if not mine:
                continue
            s = dict(mine[-1])
            for k in ("seconds", "total", "cpu", "peak_mb", "verify_seconds"):
                vals = [r[k] for r in mine if r.get(k) is not None]
                s[k] = statistics.median(vals) if vals else None
            s["all_seconds"] = [round(r["seconds"], 2) for r in mine]
            s["same"] = None if any(r["same"] is None for r in mine) else all(r["same"] for r in mine)
            summary.append(s)
    print()
    print(f"medians of {a.repeat} run(s):" if a.repeat > 1 else "summary:")
    print(table([row_cells(s) for s in summary], head))
    if a.json:
        Path(a.json).write_text(json.dumps({"runs": runs, "summary": summary}, indent=1, default=str),
                                encoding="utf-8")


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    from romorg.cdimage import ImageError
    from romorg.chdwrite import ChdWriteError

    try:
        main()
    except (OSError, ImageError, ChdWriteError) as exc:     # ChdWriteError / ImageError: bad input, not a bug
        sys.exit(f"{Path(sys.argv[0]).name}: {type(exc).__name__}: {exc}")
