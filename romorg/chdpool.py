"""A small pool of worker processes that hash CHD tracks in parallel (pure-Python decode is CPU bound).

The pure-Python FLAC decoder runs at about 1 MB/s per core, so hashing a disc with 400 MB of audio takes
minutes on one core; its tracks are independent, so they are handed to up to ``workers`` processes (several
CHDs of a scan are hashed concurrently as well). Workers are plain ``python -m romorg.chdworker`` children
(no ``multiprocessing``: that is fragile inside a threaded server, a zipapp and an AppImage). If a worker
cannot be started or dies, :class:`PoolError` is raised and the caller hashes in-process instead.

POSIX only (it waits on pipes with ``select``); elsewhere :func:`make_pool` returns None.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from . import winproc
from pathlib import Path
from typing import Any, Callable, Optional

ENV_WORKERS = "ROMORG_CHD_WORKERS"
MAX_AUTO = 8
SEGMENT_BYTES = 5 << 20          # one decode request to a worker (hash_track_split)


class PoolError(Exception):
    """A worker could not run the request (the caller falls back to in-process hashing)."""


class Cancelled(Exception):
    pass


def default_workers(configured: Any = 0) -> int:
    """0 / None = auto (up to 8, leaving one CPU free); 1 = no pool."""
    try:
        n = int(os.environ.get(ENV_WORKERS) or configured or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = min(MAX_AUTO, max(1, (os.cpu_count() or 1) - 1))
    return max(1, min(n, 16))


def _package_parent() -> str:
    import romorg
    return str(Path(romorg.__file__).resolve().parent.parent)


class _Worker:
    def __init__(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = _package_parent()          # a directory, a .pyz or an AppImage's site-packages
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTHONSTARTUP", None)
        try:
            if getattr(sys, "frozen", False):  # PyInstaller exe: sys.executable is the app itself, so it has a worker mode
                cmd = [sys.executable, "--chd-worker"]
            else:
                cmd = [sys.executable, "-B", "-u", "-m", "romorg.chdworker"]
            self.proc = subprocess.Popen(cmd, env=env,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         **winproc.popen_kwargs(new_session=True))
        except OSError as exc:
            raise PoolError(f"cannot start a worker: {exc}") from exc
        self.buf = b""
        self.dead = False
        self._chunks: "Optional[queue.Queue[bytes]]" = None
        if winproc.IS_WINDOWS:  # select() only works on sockets there: a reader thread feeds a queue instead
            self._chunks = queue.Queue()
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        fd = self.proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 65536)
                self._chunks.put(chunk)
                if not chunk:
                    return
        except (OSError, ValueError):
            self._chunks.put(b"")

    def kill(self) -> None:
        self.dead = True
        if winproc.IS_WINDOWS:
            winproc.kill_tree(self.proc)
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                self.proc.kill()
            except OSError:
                pass
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass

    def readline(self, cancel: Optional[Callable[[], bool]]) -> dict:
        fd = None if self._chunks is not None else self.proc.stdout.fileno()
        while b"\n" not in self.buf:
            if cancel is not None and cancel():
                raise Cancelled()
            if self._chunks is not None:
                try:
                    chunk = self._chunks.get(timeout=0.2)
                except queue.Empty:
                    if self.proc.poll() is not None and self._chunks.empty():
                        raise PoolError("a worker exited unexpectedly")
                    continue
            else:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if not ready:
                    if self.proc.poll() is not None:
                        raise PoolError("a worker exited unexpectedly")
                    continue
                chunk = os.read(fd, 65536)
            if not chunk:
                raise PoolError("a worker closed its pipe")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError as exc:
            raise PoolError("garbled worker reply") from exc

    def _more(self, cancel: Optional[Callable[[], bool]]) -> bytes:
        """The next bytes of the worker's output (waits; raises on cancel / a dead worker)."""
        fd = None if self._chunks is not None else self.proc.stdout.fileno()
        while True:
            if cancel is not None and cancel():
                raise Cancelled()
            if self._chunks is not None:
                try:
                    chunk = self._chunks.get(timeout=0.2)
                except queue.Empty:
                    if self.proc.poll() is not None and self._chunks.empty():
                        raise PoolError("a worker exited unexpectedly")
                    continue
            else:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if not ready:
                    if self.proc.poll() is not None:
                        raise PoolError("a worker exited unexpectedly")
                    continue
                chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise PoolError("a worker closed its pipe")
            return chunk

    def read_exact(self, n: int, cancel: Optional[Callable[[], bool]]) -> bytes:
        """Exactly ``n`` raw bytes that follow a ``{"d": n}`` line."""
        parts = [self.buf[:n]]
        have = len(parts[0])
        self.buf = self.buf[n:]
        while have < n:
            chunk = self._more(cancel)
            if have + len(chunk) > n:
                self.buf = chunk[n - have:]
                chunk = chunk[:n - have]
            parts.append(chunk)
            have += len(chunk)
        return b"".join(parts)


class HashPool:
    """``hash_track(path, index)`` is thread-safe; at most ``workers`` run at the same time."""

    def __init__(self, workers: int) -> None:
        self.workers = max(1, workers)
        self._idle: "queue.LifoQueue[_Worker]" = queue.LifoQueue()
        self._lock = threading.Lock()
        self._started = 0
        self._all: list[_Worker] = []
        self.broken = False
        self.closed = False

    def _take(self, cancel: Optional[Callable[[], bool]]) -> _Worker:
        while True:
            if cancel is not None and cancel():
                raise Cancelled()
            try:
                w = self._idle.get_nowait()
                if w.dead or w.proc.poll() is not None:
                    continue
                return w
            except queue.Empty:
                pass
            with self._lock:
                if self.closed or self.broken:
                    raise PoolError("the pool is not available")
                if self._started < self.workers:
                    self._started += 1
                    try:
                        w = _Worker()
                    except PoolError:
                        self._started -= 1
                        self.broken = True
                        raise
                    self._all.append(w)
                    return w
            try:
                w = self._idle.get(timeout=0.2)
            except queue.Empty:
                continue
            if w.dead or w.proc.poll() is not None:
                continue
            return w

    def _drop(self, w: _Worker) -> None:
        w.kill()
        with self._lock:
            self._started -= 1
            if w in self._all:
                self._all.remove(w)

    def read_segment(self, path: str, index: int, first: int, count: int,
                     cancel: Optional[Callable[[], bool]] = None) -> bytes:
        """The extracted bytes of ``count`` frames of a track from frame ``first``, decoded by one worker."""
        w = self._take(cancel)
        try:
            try:
                req = {"path": str(path), "track": index, "first": first, "count": count, "raw": True}
                w.proc.stdin.write((json.dumps(req) + "\n").encode())
                w.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise PoolError(f"cannot talk to a worker: {exc}") from exc
            parts = []
            while True:
                msg = w.readline(cancel)
                if "d" in msg:
                    parts.append(w.read_exact(int(msg["d"]), cancel))
                    continue
                if "ok" in msg:
                    self._idle.put(w)
                    return b"".join(parts)
                raise PoolError(str(msg.get("error") or "worker failed"))
        except (Cancelled, PoolError):
            self._drop(w)
            raise

    def hash_track_split(self, path: str, index: int, frames: int, frame_bytes: int,
                         progress: Optional[Callable[[int], None]] = None,
                         cancel: Optional[Callable[[], bool]] = None) -> dict:
        """crc32 / md5 / sha1 of one track: the workers decode it in segments (in parallel, so even a single big
        track uses every worker) and this thread hashes the bytes in order."""
        seg = max(1, SEGMENT_BYTES // max(1, frame_bytes))
        crc = 0
        md5 = hashlib.md5()
        sha1 = hashlib.sha1()
        size = 0
        failed = [False]

        def stop() -> bool:
            return failed[0] or (cancel is not None and cancel())

        window: "deque" = deque()
        ex = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="romorg-seg")
        try:
            starts = iter(range(0, frames, seg))
            while True:
                while len(window) < self.workers + 2:
                    s = next(starts, None)
                    if s is None:
                        break
                    window.append(ex.submit(self.read_segment, path, index, s, min(seg, frames - s), stop))
                if not window:
                    break
                data = window.popleft().result()
                crc = zlib.crc32(data, crc)
                md5.update(data)
                sha1.update(data)
                size += len(data)
                if progress:
                    progress(len(data))
        except BaseException:
            failed[0] = True
            if cancel is not None and cancel():
                raise Cancelled() from None
            raise
        finally:
            ex.shutdown(wait=True, cancel_futures=True)
        return {"crc32": "%08x" % (crc & 0xFFFFFFFF), "md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "size": size}

    def hash_track(self, path: str, index: int, progress: Optional[Callable[[int], None]] = None,
                   cancel: Optional[Callable[[], bool]] = None) -> dict:
        w = self._take(cancel)
        try:
            try:
                w.proc.stdin.write((json.dumps({"path": str(path), "track": index}) + "\n").encode())
                w.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise PoolError(f"cannot talk to a worker: {exc}") from exc
            while True:
                msg = w.readline(cancel)
                if "p" in msg:
                    if progress:
                        progress(int(msg["p"]))
                    continue
                if "ok" in msg:
                    self._idle.put(w)
                    w = None
                    return msg["ok"]
                raise PoolError(str(msg.get("error") or "worker failed"))
        except Cancelled:
            if w is not None:
                w.kill()
                with self._lock:
                    self._started -= 1
                    if w in self._all:
                        self._all.remove(w)
                w = None
            raise
        except PoolError:
            if w is not None:
                w.kill()
                with self._lock:
                    self._started -= 1
                    if w in self._all:
                        self._all.remove(w)
                w = None
            raise
        finally:
            if w is not None and not w.dead:
                self._idle.put(w)

    def close(self) -> None:
        self.closed = True
        with self._lock:
            workers, self._all = list(self._all), []
        for w in workers:
            w.kill()


def make_pool(workers: int) -> Optional[HashPool]:
    """A pool for ``workers`` > 1, else None (hash in-process)."""
    if workers <= 1:
        return None
    return HashPool(workers)
