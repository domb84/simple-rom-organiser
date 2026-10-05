"""Parallel CHD scheduler: ONE work queue over every track of every CHD, decoded by N worker processes.

Pure-Python decoding is CPU bound and Python threads do not scale (the decode path holds the GIL), so the work is
spread over *processes*. A job (a scan, a Verify fully, the verification after a conversion) shares one
:class:`Scheduler`:

* every track that has to be hashed is split into *chunks* (about :data:`CHUNK_BYTES` of extracted data, on hunk /
  ECC-group boundaries, see :func:`romorg.chd.plan_chunks`), so a 10 GB PS2 image keeps all cores busy and nothing
  waits for one huge file;
* the chunks of all requested tracks form one FIFO; every worker takes the next chunk, decodes it (a worker keeps its
  opened CHDs and parsed hunk maps between chunks) and sends the extracted bytes back through its pipe;
* md5 / sha1 cannot be merged from parts, so an *ordered hasher* per track consumes the chunks in order (crc32 + md5 +
  sha1 of the same chunk run in parallel threads, ``hashlib`` / ``zlib`` release the GIL);
* back-pressure: the bytes of chunks that were handed out but not yet hashed never exceed a budget (<= 256 MB, less
  when the machine is short of memory), and at most two requests are in flight per worker;
* a crashed worker's chunks are retried on another worker (a few respawns are allowed); when the pool cannot be
  kept alive :class:`PoolError` is raised and the caller hashes in-process instead;
* cancellation: :class:`Cancelled`; :meth:`Scheduler.close` kills the workers (process groups) and reaps them.

Results are identical to the sequential :func:`romorg.chd.hash_track` (property-tested). ``workers <= 1`` (config
``chd_workers`` / ``$ROMORG_CHD_WORKERS`` = 1) selects the old sequential path inside the calling thread.

Linux and Windows run the same pool: each worker is drained by its own thread with blocking pipe reads (no
``select``), a frozen Windows exe starts itself with ``--chd-worker`` (it has no separate interpreter), no console
window flashes, and a worker is killed with its process group (POSIX) or its process tree (Windows).
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

from . import chd as chdlib
from . import multihash, winproc

try:                             # POSIX only: used to enlarge the workers' pipes
    import fcntl
except ImportError:              # Windows
    fcntl = None  # type: ignore[assignment]

ENV_WORKERS = "ROMORG_CHD_WORKERS"
CHUNK_BYTES = 4 << 20            # extracted bytes per work unit (2-8 MB measured best, see tools/bench_chd.py)
MAX_BUDGET = 256 << 20           # hunks handed out but not yet hashed
PER_WORKER_BYTES = 80 << 20      # what one worker process costs in RAM (python + a chunk + temporaries)
MAX_WORKERS = 32
WINDOWS_MAX_AUTO = 12            # automatic worker count on Windows (not measured there beyond 8)
INFLIGHT = 2                     # requests in flight per worker (hides the pipe / hashing latency)
MAX_RESPAWNS = 3
_F_SETPIPE_SZ = 1031


class PoolError(Exception):
    """The worker pool could not run the request (the caller hashes in-process instead)."""


class Cancelled(Exception):
    pass


def mem_available() -> Optional[int]:
    """Bytes of RAM available without swapping (``MemAvailable`` / Windows' available physical memory), None when
    unknown."""
    if winproc.IS_WINDOWS:
        try:
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _Status()
            st.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
                return int(st.ullAvailPhys)
        except Exception:  # noqa: BLE001 - only a hint for the worker count
            pass
        return None
    try:
        with open("/proc/meminfo", "rb") as f:
            for line in f:
                if line.startswith(b"MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def default_workers(configured: Any = 0) -> int:
    """Worker processes: ``$ROMORG_CHD_WORKERS`` / the ``chd_workers`` setting (>= 1), else one per CPU thread,
    limited by the memory that is available (a worker costs ~80 MB). 1 = no pool (sequential, in-process)."""
    try:
        n = int(os.environ.get(ENV_WORKERS) or configured or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return max(1, min(n, MAX_WORKERS))
    n = os.cpu_count() or 1
    if winproc.IS_WINDOWS:      # starting a process costs more there (a onefile exe unpacks itself per worker)
        n = min(WINDOWS_MAX_AUTO, max(1, n - 1))
    mem = mem_available()
    if mem is not None:
        n = min(n, max(2, (mem - (256 << 20)) // PER_WORKER_BYTES))
    return max(1, min(n, MAX_WORKERS))


def _package_parent() -> str:
    import romorg
    return str(Path(romorg.__file__).resolve().parent.parent)


# --------------------------------------------------------------------------- bookkeeping
@dataclass(eq=False)
class _Run:
    """One track being hashed."""
    path: str
    sig: tuple
    index: int
    chunks: List[tuple]                    # (first, count, bytes)
    digest: Any = None
    next_dispatch: int = 0
    next_hash: int = 0
    outstanding: int = 0
    results: Dict[int, bytes] = field(default_factory=dict)
    hashing: bool = False
    error: Optional[tuple] = None          # (kind, message, needs_chdman)
    result: Optional[dict] = None
    done: threading.Event = field(default_factory=threading.Event)
    progress: Optional[Callable[[int], None]] = None
    size: int = 0

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass(eq=False)
class _Chunk:
    run: _Run
    idx: int
    first: int
    count: int
    size: int
    tries: int = 0


class _Worker:
    def __init__(self, sched: "Scheduler", wid: int) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = _package_parent()          # a directory, a .pyz or an AppImage's site-packages
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTHONSTARTUP", None)
        self.sched, self.wid = sched, wid
        try:
            if getattr(sys, "frozen", False):   # PyInstaller exe: sys.executable is the app itself (its worker mode)
                cmd = [sys.executable, "--chd-worker"]
            else:
                cmd = [sys.executable, "-B", "-u", "-m", "romorg.chdworker"]
            self.proc = subprocess.Popen(cmd, env=env,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         bufsize=0, **winproc.popen_kwargs(new_session=True))
        except OSError as exc:
            raise PoolError(f"cannot start a worker: {exc}") from exc
        if fcntl is not None:
            try:
                fcntl.fcntl(self.proc.stdout.fileno(), _F_SETPIPE_SZ, 1 << 20)
            except (OSError, AttributeError):
                pass
        self.rd = io.BufferedReader(self.proc.stdout, buffer_size=1 << 16)
        self.dead = False
        self.thread = threading.Thread(target=self._loop, name=f"chd-worker-{wid}", daemon=True)

    # -- parent-side thread that feeds / drains one worker
    def _loop(self) -> None:
        sched = self.sched
        outstanding: Deque[_Chunk] = deque()
        try:
            while True:
                while len(outstanding) < INFLIGHT:
                    chunk = sched._take(block=not outstanding)
                    if chunk is None:
                        break
                    outstanding.append(chunk)         # before sending: a failed send must hand the chunk back
                    self._send(chunk)
                if not outstanding:
                    if sched._closed:
                        return
                    continue
                chunk = outstanding[0]
                reply = self._read_reply(chunk)
                outstanding.popleft()
                sched._deliver(chunk, reply)
        except BaseException as exc:  # noqa: BLE001 - crash / broken pipe: the scheduler decides what happens
            if not sched._closed:
                sched._worker_failed(self, list(outstanding), exc)

    def _send(self, chunk: _Chunk) -> None:
        run = chunk.run
        req = {"id": chunk.idx, "path": run.path, "sig": list(run.sig), "track": run.index,
               "first": chunk.first, "count": chunk.count}
        try:
            self.proc.stdin.write(json.dumps(req, separators=(",", ":")).encode() + b"\n")
        except (OSError, ValueError) as exc:
            raise PoolError(f"cannot talk to a worker: {exc}") from exc

    def _read_reply(self, chunk: _Chunk) -> tuple:
        line = self.rd.readline()
        if not line:
            raise PoolError("a worker closed its pipe")
        try:
            head = json.loads(line)
        except ValueError as exc:
            raise PoolError("garbled worker reply") from exc
        if "err" in head:
            e = head["err"]
            return ("err", (str(e.get("kind")), str(e.get("msg")), bool(e.get("needs_chdman"))))
        n = int(head["n"])
        buf = bytearray(n)
        mv = memoryview(buf)
        got = 0
        while got < n:
            k = self.rd.readinto(mv[got:])
            if not k:
                raise PoolError("a worker closed its pipe")
            got += k
        return ("ok", buf)

    def kill(self) -> None:
        self.dead = True
        if winproc.IS_WINDOWS:
            winproc.kill_tree(self.proc)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                try:
                    self.proc.kill()
                except OSError:
                    pass
        for f in (self.proc.stdin, self.rd):
            try:
                if f:
                    f.close()
            except (OSError, ValueError):
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- the scheduler
class Scheduler:
    """Hashes tracks of CHDs; :meth:`hash_tracks` may be called from several threads at once (the chunks of all
    callers share the same workers)."""

    def __init__(self, workers: int, chunk_bytes: int = CHUNK_BYTES, budget_bytes: Optional[int] = None,
                 parallel_hash: Optional[bool] = None) -> None:
        self.workers = max(1, int(workers))
        mem = mem_available()
        if budget_bytes is None:
            budget_bytes = MAX_BUDGET if mem is None else max(32 << 20, min(MAX_BUDGET, mem // 6))
        self.budget = max(1, int(budget_bytes))
        # chunks must be small enough for every worker to hold INFLIGHT of them inside the budget
        self.chunk_bytes = max(256 << 10, min(int(chunk_bytes), self.budget // max(1, self.workers * INFLIGHT)))
        self.parallel_hash = (multihash.PARALLEL_DEFAULT if parallel_hash is None else parallel_hash)
        self._cv = threading.Condition()
        self._runs: Deque[_Run] = deque()
        self._active: set = set()
        self._retry: Deque[_Chunk] = deque()
        self._workers: List[_Worker] = []
        self._reserved = 0
        self._respawns = 0
        self._closed = False
        self.broken = False
        self._hash_pool: Optional[ThreadPoolExecutor] = None
        self.stats = {"bytes": 0, "seconds": 0.0, "chunks": 0, "peak_reserved": 0}
        self._t0: Optional[float] = None
        self._peak = 0

    @property
    def pooled(self) -> bool:
        return self.workers > 1 and not self.broken

    # -- public
    def hash_tracks(self, info: Any, indexes: Sequence[int], progress: Optional[Callable[[int], None]] = None,
                    cancel: Optional[Callable[[], bool]] = None) -> Dict[int, dict]:
        """``{track index: {"crc32", "md5", "sha1", "size"}}`` of the tracks of the opened CHD ``info``.

        Raises :class:`Cancelled`, :class:`PoolError` (the caller hashes in-process) or the reader's own
        :class:`~romorg.chd.ChdError` / :class:`~romorg.chd.ChdUnsupported` (the file cannot be decoded)."""
        indexes = list(indexes)
        if not indexes:
            return {}
        if not self.pooled:
            return self._sequential(info, indexes, progress, cancel)
        if self._closed:
            raise Cancelled()
        try:
            st = os.stat(info.path)
            sig = (st.st_size, st.st_mtime_ns)
        except OSError as exc:
            raise chdlib.ChdError(f"cannot read {info.path}: {exc}") from exc
        runs: List[_Run] = []
        for i in indexes:
            tr = info.tracks[i]
            plan = chdlib.plan_chunks(info, tr, self.chunk_bytes)
            chunks = [(first, count, count * tr.sector_size) for first, count in plan]
            run = _Run(os.fspath(info.path), sig, i, chunks, progress=progress, size=tr.size)
            run.digest = multihash.make(self.parallel_hash)
            if not chunks:
                run.result = self._finish(run)
                run.done.set()
            runs.append(run)
        with self._cv:
            if self._t0 is None:
                self._t0 = time.monotonic()
            for run in runs:
                if not run.done.is_set():
                    self._runs.append(run)
                    self._active.add(run)
            self._cv.notify_all()
        try:
            self._ensure_workers(sum(len(r.chunks) for r in runs))
            self._wait(runs, cancel)
        except BaseException:
            self._abandon(runs)
            raise
        out: Dict[int, dict] = {}
        first_error = None
        for run in runs:
            if run.error and first_error is None:
                first_error = run.error
            elif run.result is not None:
                out[run.index] = run.result
        if first_error is not None:
            self._raise(first_error)
        return out

    def close(self) -> None:
        """Kill and reap the workers (idempotent)."""
        with self._cv:
            self._closed = True
            workers, self._workers = list(self._workers), []
            for run in list(self._active):
                run.done.set()
            self._cv.notify_all()
        for w in workers:
            w.kill()
        for w in workers:
            w.thread.join(timeout=2)
        pool, self._hash_pool = self._hash_pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if self._t0 is not None:
            self.stats["seconds"] = time.monotonic() - self._t0
        self.stats["peak_reserved"] = self._peak

    def __enter__(self) -> "Scheduler":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def rate(self) -> float:
        """Average bytes per second of everything hashed so far (0 when nothing was)."""
        if self._t0 is None:
            return 0.0
        dt = time.monotonic() - self._t0
        return self.stats["bytes"] / dt if dt > 0 else 0.0

    # -- sequential path (workers = 1 / no POSIX / pool broken): exactly the old in-process behaviour
    def _sequential(self, info, indexes, progress, cancel) -> Dict[int, dict]:
        out: Dict[int, dict] = {}
        t0 = time.monotonic()
        for i in indexes:
            if cancel is not None and cancel():
                raise Cancelled()
            try:
                h = chdlib.hash_track(info, info.tracks[i], progress=progress, cancel=cancel)
            except InterruptedError:
                raise Cancelled() from None
            out[i] = {"crc32": h.crc32, "md5": h.md5, "sha1": h.sha1, "size": h.size}
            self.stats["bytes"] += h.size
        self._t0 = self._t0 or t0
        return out

    # -- workers
    def _ensure_workers(self, chunks: int) -> None:
        with self._cv:
            if self._closed or self.broken:
                return
            want = min(self.workers, max(1, chunks))
            while len(self._workers) < want:
                try:
                    w = _Worker(self, len(self._workers) + self._respawns)
                except PoolError:
                    if not self._workers:
                        self.broken = True
                        self._fail_all_locked("cannot start a worker")
                        raise
                    break
                self._workers.append(w)
                w.thread.start()

    def _worker_failed(self, w: _Worker, chunks: List[_Chunk], exc: BaseException) -> None:
        """A worker died / garbled its pipe: retry its chunks elsewhere, respawn it (a few times), else give up."""
        with self._cv:
            if w in self._workers:
                self._workers.remove(w)
            for c in reversed(chunks):
                c.tries += 1
                if c.tries > 2:
                    self.broken = True
                else:
                    c.run.outstanding -= 1
                    self._reserved -= c.size
                    if c.run.failed:
                        self._finish_failed_locked(c.run)
                    else:
                        self._retry.appendleft(c)
            self._cv.notify_all()
        w.kill()
        with self._cv:
            if self._closed or self.broken:
                self.broken = True
                self._fail_all_locked(f"a worker failed ({exc})")
                return
            if self._respawns >= MAX_RESPAWNS:
                if not self._workers:
                    self.broken = True
                    self._fail_all_locked(f"the workers keep failing ({exc})")
                return
            self._respawns += 1
            try:
                nw = _Worker(self, 100 + self._respawns)
            except PoolError as err:
                if not self._workers:
                    self.broken = True
                    self._fail_all_locked(str(err))
                return
            self._workers.append(nw)
            nw.thread.start()

    def _fail_all_locked(self, why: str) -> None:
        for run in list(self._active):
            if run.error is None and not run.done.is_set():
                run.error = ("pool", why, False)
            run.done.set()
        self._active.clear()
        self._runs.clear()
        self._retry.clear()
        self._cv.notify_all()

    # -- dispatch
    def _peek_locked(self) -> Optional[_Chunk]:
        if self._retry:
            return self._retry[0]
        runs = self._runs
        while runs:
            run = runs[0]
            if run.failed or run.next_dispatch >= len(run.chunks):
                runs.popleft()
                continue
            first, count, size = run.chunks[run.next_dispatch]
            return _Chunk(run, run.next_dispatch, first, count, size)
        return None

    def _take(self, block: bool) -> Optional[_Chunk]:
        with self._cv:
            while True:
                if self._closed or self.broken:
                    return None
                c = self._peek_locked()
                if c is not None and (self._reserved == 0 or self._reserved + c.size <= self.budget):
                    if self._retry and self._retry[0] is c:
                        self._retry.popleft()
                    else:
                        c.run.next_dispatch += 1
                    if c.run.failed:                        # became pointless meanwhile
                        self._finish_failed_locked(c.run)
                        continue
                    self._reserved += c.size
                    self._peak = max(self._peak, self._reserved)
                    c.run.outstanding += 1
                    return c
                if not block:
                    return None
                self._cv.wait(0.5)

    def _deliver(self, chunk: _Chunk, reply: tuple) -> None:
        run = chunk.run
        submit = False
        with self._cv:
            run.outstanding -= 1
            kind, payload = reply
            if kind == "err":
                if run.error is None:
                    run.error = payload
                self._reserved -= chunk.size
                run.results.clear()
                self._finish_failed_locked(run)
                self._cv.notify_all()
                return
            if run.failed or self._closed:
                self._reserved -= chunk.size
                self._finish_failed_locked(run)
                self._cv.notify_all()
                return
            if len(payload) != chunk.size:
                run.error = ("corrupt", f"a worker returned {len(payload)} bytes instead of {chunk.size}", False)
                self._reserved -= chunk.size
                self._finish_failed_locked(run)
                self._cv.notify_all()
                return
            run.results[chunk.idx] = payload
            if not run.hashing and run.next_hash in run.results:
                run.hashing = True
                submit = True
        if submit:
            self._submit_hash(run)

    def _finish_failed_locked(self, run: _Run) -> None:
        if run.outstanding <= 0 and not run.hashing:
            run.done.set()

    def _submit_hash(self, run: _Run) -> None:
        with self._cv:
            if self._hash_pool is None and not self._closed:
                self._hash_pool = ThreadPoolExecutor(max_workers=max(2, min(4, self.workers)),
                                                     thread_name_prefix="chd-hash")
            pool = self._hash_pool
        if pool is None:
            return
        try:
            pool.submit(self._hash_run, run)
        except RuntimeError:                                # pool already shut down
            pass

    def _hash_run(self, run: _Run) -> None:
        """The ordered hasher of one track: consumes its chunks strictly in order."""
        while True:
            with self._cv:
                if run.failed or self._closed:
                    run.hashing = False
                    for d in run.results.values():
                        self._reserved -= len(d)
                    run.results.clear()
                    self._finish_failed_locked(run)
                    self._cv.notify_all()
                    return
                data = run.results.pop(run.next_hash, None)
                if data is None:
                    run.hashing = False
                    return
                run.next_hash += 1
            try:
                run.digest.update(data)
            except BaseException as exc:  # noqa: BLE001
                with self._cv:
                    run.error = ("other", f"hashing failed: {exc}", False)
                    self._reserved -= len(data)
                    run.hashing = False
                    self._finish_failed_locked(run)
                    self._cv.notify_all()
                return
            n = len(data)
            with self._cv:
                self._reserved -= n
                self.stats["bytes"] += n
                self.stats["chunks"] += 1
                last = run.next_hash >= len(run.chunks)
                self._cv.notify_all()
            if run.progress is not None:
                try:
                    run.progress(n)
                except Exception:  # noqa: BLE001 - an observer must never stop the job
                    pass
            if last:
                with self._cv:
                    run.result = self._finish(run)
                    run.hashing = False
                    run.done.set()
                    self._cv.notify_all()
                return

    @staticmethod
    def _finish(run: _Run) -> dict:
        crc, md5, sha1 = run.digest.result()
        return {"crc32": crc, "md5": md5, "sha1": sha1, "size": run.digest.size}

    # -- waiting / cancel
    def _wait(self, runs: List[_Run], cancel: Optional[Callable[[], bool]]) -> None:
        try:
            for run in runs:
                while not run.done.wait(0.1):
                    if cancel is not None and cancel():
                        raise Cancelled()
                    if self._closed:
                        raise Cancelled()
                if self._closed and run.result is None and run.error is None:
                    raise Cancelled()
        finally:
            with self._cv:
                for run in runs:
                    if run.done.is_set():
                        self._active.discard(run)

    def _abandon(self, runs: Sequence[_Run]) -> None:
        """The caller gave up (cancel / error): drop what was not started, let the in-flight chunks drain."""
        with self._cv:
            for run in runs:
                if run.error is None and not run.done.is_set():
                    run.error = ("cancelled", "cancelled", False)
                for d in run.results.values():
                    self._reserved -= len(d)
                run.results.clear()
            self._cv.notify_all()

    @staticmethod
    def _raise(err: tuple) -> None:
        kind, msg, needs = err
        if kind == "pool":
            raise PoolError(msg)
        if kind == "cancelled":
            raise Cancelled()
        if kind == "unsupported":
            exc = chdlib.ChdUnsupported(_strip(msg))
            exc.needs_chdman = needs
            raise exc
        raise chdlib.ChdError(_strip(msg))


def _strip(msg: str) -> str:
    """``"ChdError: text"`` -> ``"text"`` (the worker prefixes the exception class)."""
    head, sep, rest = msg.partition(": ")
    return rest if sep and head.isidentifier() else msg


def make_scheduler(workers: int, **kw: Any) -> Scheduler:
    """A scheduler for ``workers`` processes (1 = sequential, in-process)."""
    return Scheduler(workers, **kw)
