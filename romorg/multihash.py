"""crc32 + md5 + sha1 of a byte stream in one pass, optionally with the three digests running in parallel threads.

``hashlib`` and ``zlib`` release the GIL for buffers above ~2 KiB, so the three digests of one chunk can run on three
cores: the pass costs about the slowest digest (md5) instead of the sum. Whether that helps depends on the machine
(see ``tools/bench_chd.py``); :data:`PARALLEL_DEFAULT` is the measured choice for machines with 4+ CPU threads.
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Tuple

PARALLEL_MIN = 256 * 1024        # smaller chunks are not worth a thread hand-off

_pool: Optional[ThreadPoolExecutor] = None
_pool_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=max(2, min(8, (os.cpu_count() or 2))), thread_name_prefix="romorg-hash")
        return _pool


class MultiHash:
    """``update(chunk)`` ... ``result() -> (crc32 hex, md5 hex, sha1 hex)``; not thread safe (one stream).
    ``md5=False`` skips the md5 (its result is then the empty string)."""

    def __init__(self, parallel: bool = False, md5: bool = True) -> None:
        self.parallel = parallel
        self.crc = 0
        self.md5 = hashlib.md5() if md5 else None
        self.sha1 = hashlib.sha1()
        self.size = 0

    def update(self, data) -> None:
        n = len(data)
        self.size += n
        if self.parallel and n >= PARALLEL_MIN:
            ex = _executor()
            f1 = ex.submit(self.md5.update, data) if self.md5 is not None else None
            f2 = ex.submit(self.sha1.update, data)
            self.crc = zlib.crc32(data, self.crc)
            if f1 is not None:
                f1.result()
            f2.result()
        else:
            self.crc = zlib.crc32(data, self.crc)
            if self.md5 is not None:
                self.md5.update(data)
            self.sha1.update(data)

    def result(self) -> Tuple[str, str, str]:
        return ("%08x" % (self.crc & 0xFFFFFFFF), self.md5.hexdigest() if self.md5 is not None else "",
                self.sha1.hexdigest())


# Measured on the Steam Deck (4 cores / 8 threads): sequential 277 MB/s, three threads 457 MB/s on 4 MiB blocks
# (tools/bench_chd.py prints both). Below four CPU threads the hand-off is not worth it.
PARALLEL_DEFAULT = (os.cpu_count() or 1) >= 4


def make(parallel: Optional[bool] = None, md5: bool = True) -> MultiHash:
    return MultiHash(PARALLEL_DEFAULT if parallel is None else parallel, md5)


FILE_CHUNK = 4 << 20             # 1 MiB chunks cost 12 % (measured 1456 vs 1656 MB/s on a 2 GiB file)
PIPELINE_MIN = 8 << 20           # below this a plain loop is as fast (and starts no thread)


class Cancelled(Exception):
    pass


def hash_file(path, md5: bool = True, progress: Optional[Callable[[int], None]] = None,
              cancel: Optional[Callable[[], bool]] = None, parallel: Optional[bool] = None,
              chunk: int = FILE_CHUNK) -> Tuple[str, str, str, int]:
    """``(crc32, md5 | "", sha1, size)`` of a file in one pass.

    Big files are read by a prefetch thread (the next chunk is read while the previous one is hashed) and the
    digests of a chunk run in parallel threads: measured 850 -> 1650 MB/s for crc32 + sha1 of a 2 GiB file (warm
    cache) on the Steam Deck. ``progress(nbytes)`` is called per chunk; ``cancel()`` is polled per chunk
    (:class:`Cancelled`)."""
    h = make(parallel, md5)
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size < PIPELINE_MIN:
            while True:
                if cancel is not None and cancel():
                    raise Cancelled()
                block = f.read(1 << 20)
                if not block:
                    break
                h.update(block)
                if progress is not None:
                    progress(len(block))
        else:
            q: "queue.Queue" = queue.Queue(maxsize=4)
            stop = threading.Event()

            def reader() -> None:
                try:
                    while not stop.is_set():
                        block = f.read(chunk)
                        while not stop.is_set():
                            try:
                                q.put(block, timeout=0.2)
                                break
                            except queue.Full:
                                continue
                        if not block:
                            return
                except BaseException as exc:  # noqa: BLE001 - re-raised in the consumer
                    try:
                        q.put(exc, timeout=5)
                    except queue.Full:
                        pass

            t = threading.Thread(target=reader, name="romorg-read", daemon=True)
            t.start()
            try:
                while True:
                    if cancel is not None and cancel():
                        raise Cancelled()
                    try:
                        block = q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if isinstance(block, BaseException):
                        raise block
                    if not block:
                        break
                    h.update(block)
                    if progress is not None:
                        progress(len(block))
            finally:
                stop.set()
                t.join(timeout=5)
    crc, md5_hex, sha1 = h.result()
    return crc, md5_hex, sha1, h.size
