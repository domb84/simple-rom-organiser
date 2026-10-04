"""crc32 / md5 / sha1 in one pass (romorg.multihash) and the scanner's thread pool: same results as the plain loop."""

from __future__ import annotations

import hashlib
import os
import random
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from romorg import multihash, scanner  # noqa: E402


def plain(data: bytes):
    return "%08x" % (zlib.crc32(data) & 0xFFFFFFFF), hashlib.md5(data).hexdigest(), hashlib.sha1(data).hexdigest()


class MultiHashTest(unittest.TestCase):
    def test_parallel_equals_sequential_for_random_chunking(self) -> None:
        rnd = random.Random(5)
        data = rnd.randbytes(3 << 20)
        want = plain(data)
        for parallel in (False, True):
            for md5 in (True, False):
                h = multihash.MultiHash(parallel, md5)
                pos = 0
                while pos < len(data):
                    n = rnd.choice((1, 100, 5000, 300_000, 1 << 20))
                    h.update(data[pos:pos + n])
                    pos += n
                crc, m, sha = h.result()
                self.assertEqual((crc, sha), (want[0], want[2]))
                self.assertEqual(m, want[1] if md5 else "")
                self.assertEqual(h.size, len(data))


class HashFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name: str, size: int) -> Path:
        p = self.dir / name
        p.write_bytes(random.Random(size).randbytes(size))
        return p

    def test_sizes_around_the_pipeline_threshold(self) -> None:
        for size in (0, 1, 4095, 1 << 20, multihash.PIPELINE_MIN - 1, multihash.PIPELINE_MIN, multihash.PIPELINE_MIN * 2 + 12345):
            p = self.write(f"f{size}", size)
            want = plain(p.read_bytes())
            for parallel in (False, True):
                crc, md5, sha1, n = multihash.hash_file(p, parallel=parallel)
                self.assertEqual(((crc, md5, sha1), n), (want, size), (size, parallel))
            crc, md5, sha1, n = multihash.hash_file(p, md5=False)
            self.assertEqual((crc, md5, sha1), (want[0], "", want[2]))
            self.assertEqual(scanner.hash_file(p), (want[0], want[2]))

    def test_progress_and_cancel(self) -> None:
        p = self.write("big", multihash.PIPELINE_MIN * 2 + 7)
        got = []
        multihash.hash_file(p, progress=got.append)
        self.assertEqual(sum(got), p.stat().st_size)
        seen = []
        with self.assertRaises(multihash.Cancelled):
            multihash.hash_file(p, progress=seen.append, cancel=lambda: len(seen) >= 1)
        self.assertLess(sum(seen), p.stat().st_size)

    def test_a_read_error_in_the_prefetch_thread_surfaces(self) -> None:
        p = self.write("big", multihash.PIPELINE_MIN + 1)
        real_open = open

        class Flaky:
            def __init__(self, f):
                self.f, self.n = f, 0

            def read(self, k):
                self.n += 1
                if self.n == 2:
                    raise OSError("disk error")
                return self.f.read(k)

            def fileno(self):
                return self.f.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()
        with mock.patch("romorg.multihash.open", lambda *a, **k: Flaky(real_open(*a, **k)), create=True):
            with self.assertRaises(OSError):
                multihash.hash_file(p)


class ScannerPoolTest(unittest.TestCase):
    def test_pool_and_no_pool_give_the_same_scan(self) -> None:
        from test_scanner import ScanTests
        t = ScanTests("test_hash_cache_hits_and_invalidation")
        t.setUp()
        self.addCleanup(t.tearDown) if hasattr(t, "tearDown") else None
        with mock.patch.dict(os.environ, {scanner.ENV_SCAN_THREADS: "1"}):
            a = t._scan(use_cache=False)
        with mock.patch.dict(os.environ, {scanner.ENV_SCAN_THREADS: "4"}):
            b = t._scan(use_cache=False)
        key = lambda r: ([(m.entry.rel, m.entry.crc, m.entry.sha1) for m in r.matched],  # noqa: E731
                         [(e.rel, e.crc, e.sha1) for e in r.unmatched], r.errors)
        self.assertEqual(key(a), key(b))
        self.assertGreater(len(a.matched), 3)


if __name__ == "__main__":
    unittest.main()
