"""The hash worker pool (parallel pure-Python decode) and its fallbacks."""

from __future__ import annotations

import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
from romorg import chd, chdpool, dreamcast, scanner  # noqa: E402


class PoolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.disc = T.Disc("x", 1)
        self.path = str(Path(self.tmp.name) / "a.chd")
        self.disc.write_chd(self.path)
        self.pool = chdpool.HashPool(2)
        self.addCleanup(self.pool.close)

    def test_same_hashes_as_in_process(self) -> None:
        with chd.Chd(self.path) as c:
            for i, t in enumerate(c.tracks):
                h = chd.hash_track(c, t)
                got = self.pool.hash_track(self.path, i)
                self.assertEqual((got["crc32"], got["md5"], got["sha1"], got["size"]), (h.crc32, h.md5, h.sha1, h.size))

    def test_parallel_requests_from_threads(self) -> None:
        with ThreadPoolExecutor(4) as ex:
            res = list(ex.map(lambda i: self.pool.hash_track(self.path, i)["sha1"], [0, 1, 2] * 4))
        self.assertEqual(len(set(res)), 3)
        self.assertLessEqual(len(self.pool._all), 2)          # never more workers than asked for

    def test_progress_and_errors(self) -> None:
        got = []
        self.pool.hash_track(self.path, 2, progress=got.append)
        self.assertEqual(sum(got), len(self.disc.t3))
        with self.assertRaises(chdpool.PoolError):
            self.pool.hash_track(str(Path(self.tmp.name) / "missing.chd"), 0)
        with self.assertRaises(chdpool.PoolError):
            self.pool.hash_track(self.path, 99)
        self.assertEqual(self.pool.hash_track(self.path, 0)["size"], len(self.disc.t1))   # still usable

    def test_cancel_kills_the_worker_promptly(self) -> None:
        big = Path(self.tmp.name) / "big.chd"
        T.build_chd(big, [{"type": "AUDIO", "data": T.make_audio_track(1200, 5)}])      # ~1 MB of FLAC: seconds
        flag = threading.Event()
        threading.Timer(0.4, flag.set).start()
        t0 = time.time()
        with self.assertRaises(chdpool.Cancelled):
            self.pool.hash_track(str(big), 0, cancel=flag.is_set)
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(self.pool._started, 0)                  # the worker is gone
        self.assertEqual(self.pool.hash_track(self.path, 0)["size"], len(self.disc.t1))   # a new one is started

    def test_a_dying_worker_is_a_pool_error_and_hash_tracks_falls_back(self) -> None:
        self.pool.hash_track(self.path, 0)
        for w in self.pool._all:
            w.proc.kill()
        time.sleep(0.2)
        with chd.Chd(self.path) as c:
            got = dreamcast.hash_tracks_python(c, [0, 1, 2], None, self.pool)
            ref = {i: chd.hash_track(c, t) for i, t in enumerate(c.tracks)}
        self.assertEqual({i: g["sha1"] for i, g in got.items()}, {i: r.sha1 for i, r in ref.items()})

    def test_unstartable_worker_means_fallback(self) -> None:
        pool = chdpool.HashPool(2)
        with mock.patch("romorg.chdpool.subprocess.Popen", side_effect=OSError("no exec")):
            with self.assertRaises(chdpool.PoolError):
                pool.hash_track(self.path, 0)
            self.assertTrue(pool.broken)
            with chd.Chd(self.path) as c:
                got = dreamcast.hash_tracks_python(c, [0], None, pool)
        self.assertEqual(got[0]["size"] if "size" in got[0] else 1, 1)
        self.assertEqual(len(got[0]["sha1"]), 40)

    def test_worker_count_rules(self) -> None:
        with mock.patch.dict(os.environ, {chdpool.ENV_WORKERS: "3"}):
            self.assertEqual(chdpool.default_workers(0), 3)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(chdpool.ENV_WORKERS, None)
            self.assertEqual(chdpool.default_workers(1), 1)
            self.assertEqual(chdpool.default_workers(0), min(4, os.cpu_count() or 1))
            self.assertEqual(chdpool.default_workers(99), 16)
        self.assertIsNone(chdpool.make_pool(1))


class ScanWithPoolTest(unittest.TestCase):
    def test_parallel_scan_equals_sequential_scan(self) -> None:
        sys.path.insert(0, os.path.dirname(__file__))
        from test_dreamcast import World, tree
        w = World(self)
        games = ["Beta (Europe)", "Gamma (Japan)", "Alpha (USA) (En,Fr)", "Delta (USA) (Demo)"]
        for g in games:
            w.chd(g, f"{g}/{g}.chd")
        seq = w.scan(cache_path=w.base / "a.sqlite")
        par = w.scan(cache_path=w.base / "b.sqlite", workers=3)
        key = lambda r: sorted((m.unit.game.name, m.level, tuple(t["sha1"] for t in m.unit.tracks)) for m in r.matched)  # noqa: E731
        self.assertEqual(key(seq), key(par))
        self.assertEqual(len(par.matched), 4)

    def test_parallel_verify_and_cancel(self) -> None:
        from test_dreamcast import World
        w = World(self)
        for g in ("Beta (Europe)", "Gamma (Japan)"):
            w.chd(g, f"{g}/{g}.chd")
        r = w.scan()
        res = dreamcast.verify_units(r, cache_path=w.cache, workers=3)
        self.assertEqual((res["verified"], res["failed"]), (2, []))
        r = w.scan(cache_path=w.base / "other.sqlite")
        with self.assertRaises(scanner.ScanCancelled):
            dreamcast.verify_units(r, cache_path=w.base / "other.sqlite", workers=3, cancel=lambda: True)


if __name__ == "__main__":
    unittest.main()
