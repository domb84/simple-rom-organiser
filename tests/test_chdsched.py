"""The parallel CHD scheduler (romorg.chdsched): chunking, ordering, back-pressure, cancel, crash fallback, and that
its hashes are identical to the sequential reader (property tests over random chunk sizes)."""

from __future__ import annotations

import binascii
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
from romorg import chd, chdsched, discsys, dreamcast, scanner, winproc  # noqa: E402


def reference(path: str) -> dict[int, dict]:
    with chd.Chd(path) as c:
        return {i: {"crc32": h.crc32, "md5": h.md5, "sha1": h.sha1, "size": h.size}
                for i, h in ((i, chd.hash_track(c, t)) for i, t in enumerate(c.tracks))}


def scheduled(path: str, workers: int = 2, chunk: int = 0, **kw) -> dict[int, dict]:
    with chd.Chd(path, load_map=False) as c, chdsched.Scheduler(workers, **kw) as s:
        if chunk:
            s.chunk_bytes = chunk
        return s.hash_tracks(c, range(len(c.tracks)))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.disc = T.Disc("x", 1, frames=(40, 24, 56))
        self.path = str(self.dir / "a.chd")
        self.disc.write_chd(self.path)


class EqualityTest(Base):
    def test_same_hashes_as_the_sequential_reader(self) -> None:
        self.assertEqual(scheduled(self.path), reference(self.path))

    def test_one_worker_is_the_sequential_path(self) -> None:
        with chd.Chd(self.path, load_map=False) as c, chdsched.Scheduler(1) as s:
            self.assertFalse(s.pooled)
            got = s.hash_tracks(c, [0, 1, 2])
            self.assertEqual(s._workers, [])                     # no process was started
        self.assertEqual(got, reference(self.path))

    def test_property_random_chunk_sizes_and_layouts(self) -> None:
        """Random chunking (any size, any hunk size / codec mix, cooked + raw + audio + DVD) == sequential hashes."""
        rnd = random.Random(2026)
        cases = []
        for n in range(5):
            d = T.Disc(f"d{n}", 100 + n, frames=(rnd.randint(1, 60), rnd.randint(1, 40), rnd.randint(1, 70)), pad=rnd.randint(0, 5))
            p = str(self.dir / f"p{n}.chd")
            d.write_chd(p, hunk_frames=rnd.choice((2, 4, 8, 16)), data_codec=rnd.randint(0, 1), compressed_map=bool(n % 2 == 0),
                       **({} if n % 2 == 0 else {"generic": "none"}))
            cases.append(p)
        cooked = str(self.dir / "cooked.chd")
        T.build_chd(cooked, [{"type": "MODE1", "data": T.make_iso(50, 7)},
                             {"type": "MODE2_RAW", "data": T.make_mode2_track(30, 8)}], gd=False)
        cases.append(cooked)
        dvd = str(self.dir / "dvd.chd")
        iso = T.make_iso(777, 5)
        T.build_dvd_chd(dvd, iso, hunk_sectors=rnd.choice((2, 4, 8)))
        cases.append(dvd)
        sched = chdsched.Scheduler(3)
        self.addCleanup(sched.close)
        for p in cases:
            ref = reference(p)
            for _ in range(3):
                sched.chunk_bytes = rnd.choice((1, 700, 2352, 5000, 19584, 50000, 200000))
                with chd.Chd(p, load_map=False) as c:
                    got = sched.hash_tracks(c, range(len(c.tracks)))
                self.assertEqual(got, ref, (p, sched.chunk_bytes))

    def test_plan_chunks_partition_every_track_on_hunk_boundaries(self) -> None:
        with chd.Chd(self.path) as c:
            for t in c.tracks:
                for target in (1, 5000, 40000, 1 << 20):
                    plan = chd.plan_chunks(c, t, target)
                    self.assertEqual(sum(n for _f, n in plan), t.data_frames)
                    pos = 0
                    for first, n in plan:
                        self.assertEqual(first, pos)
                        self.assertGreater(n, 0)
                        pos += n
                    for first, _n in plan[1:]:
                        self.assertEqual((t.start + first) % c.frames_per_hunk, 0)

    def test_read_track_range_equals_the_stream(self) -> None:
        with chd.Chd(self.path) as c:
            rnd = random.Random(1)
            for t in c.tracks:
                full = b"".join(c.iter_track(t))
                ss = t.sector_size
                for _ in range(12):
                    a = rnd.randint(0, t.data_frames)
                    b = rnd.randint(a, t.data_frames)
                    self.assertEqual(c.read_track_range(t, a, b - a), full[a * ss:b * ss])
                with self.assertRaises(chd.ChdError):
                    c.read_track_range(t, 0, t.data_frames + 1)

    def test_progress_adds_up_and_several_callers_share_the_workers(self) -> None:
        seen = []
        lock = threading.Lock()

        def prog(n: int) -> None:
            with lock:
                seen.append(n)
        ref = reference(self.path)
        with chdsched.Scheduler(2) as s:
            s.chunk_bytes = 20000
            results = []

            def job() -> None:
                with chd.Chd(self.path, load_map=False) as c:
                    results.append(s.hash_tracks(c, [0, 1, 2], progress=prog))
            ts = [threading.Thread(target=job) for _ in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(60)
            self.assertEqual(len(s._workers), 2)                  # never more processes than asked for
        self.assertEqual(results, [ref, ref, ref])
        self.assertEqual(sum(seen), 3 * sum(v["size"] for v in ref.values()))


class OrderingAndMemoryTest(Base):
    def test_out_of_order_arrival_is_hashed_in_order(self) -> None:
        with mock.patch.dict(os.environ, {"ROMORG_CHDWORKER_JITTER_MS": "40"}):
            got = scheduled(self.path, workers=4, chunk=3000)
        self.assertEqual(got, reference(self.path))

    def test_buffered_bytes_stay_within_the_budget(self) -> None:
        big = str(self.dir / "big.chd")
        T.Disc("big", 9, frames=(300, 200, 400)).write_chd(big)
        with chd.Chd(big, load_map=False) as c, chdsched.Scheduler(4, budget_bytes=120_000) as s:
            s.chunk_bytes = 20000
            got = s.hash_tracks(c, range(len(c.tracks)))
            peak = s._peak
            self.assertEqual(s._reserved, 0)                      # everything released at the end
        self.assertEqual(got, reference(big))
        self.assertLessEqual(peak, 120_000 + 20000)               # budget + the one chunk that is always allowed
        self.assertGreater(peak, 0)

    def test_chunk_size_follows_the_budget(self) -> None:
        s = chdsched.Scheduler(8, chunk_bytes=8 << 20, budget_bytes=64 << 20)
        self.addCleanup(s.close)
        self.assertLessEqual(s.chunk_bytes * 8 * chdsched.INFLIGHT, 64 << 20)


class CancelAndCrashTest(Base):
    def alive(self, s) -> list:
        return [w for w in list(s._workers) if w.proc.poll() is None]

    def test_cancel_is_prompt_and_close_reaps_the_workers(self) -> None:
        big = str(self.dir / "big.chd")
        T.build_chd(big, [{"type": "AUDIO", "data": T.make_audio_track(1500, 5)}])
        flag = threading.Event()
        threading.Timer(0.5, flag.set).start()
        # the pure-Python FLAC decoder (about 1 MB/s) makes the job take seconds
        with chd.Chd(big, load_map=False) as c, mock.patch.dict(os.environ, {"ROMORG_NO_NATIVE_FLAC": "1"}):
            s = chdsched.Scheduler(2)
            s.chunk_bytes = 20000
            t0 = time.time()
            with self.assertRaises(chdsched.Cancelled):
                s.hash_tracks(c, [0], cancel=flag.is_set)
            self.assertLess(time.time() - t0, 5)
            procs = [w.proc for w in s._workers]
            s.close()
        self.assertTrue(procs)
        for p in procs:
            self.assertIsNotNone(p.poll())                         # reaped: no zombie, no orphan

    def test_a_killed_worker_is_replaced_and_the_chunk_retried(self) -> None:
        with chd.Chd(self.path, load_map=False) as c, chdsched.Scheduler(2) as s:
            s.chunk_bytes = 4000
            first = s.hash_tracks(c, [0])
            for w in list(s._workers):
                w.proc.kill()                                      # SIGKILL / TerminateProcess
            time.sleep(0.2)
            second = s.hash_tracks(c, [0, 1, 2])
        ref = reference(self.path)
        self.assertEqual(first[0], ref[0])
        self.assertEqual(second, ref)

    def test_a_crash_in_the_middle_of_a_job_is_survived(self) -> None:
        marker = str(self.dir / "crashed")
        with mock.patch.dict(os.environ, {"ROMORG_CHDWORKER_CRASH": f"once:{marker}"}):
            got = scheduled(self.path, workers=2, chunk=5000)
        self.assertTrue(os.path.exists(marker))                    # a worker really died ...
        self.assertEqual(got, reference(self.path))                # ... and the result is still right

    def test_workers_that_always_crash_mean_pool_error_and_the_caller_hashes_in_process(self) -> None:
        with mock.patch.dict(os.environ, {"ROMORG_CHDWORKER_CRASH": "always"}):
            with chd.Chd(self.path, load_map=False) as c, chdsched.Scheduler(2) as s:
                with self.assertRaises(chdsched.PoolError):
                    s.hash_tracks(c, [0, 1, 2])
                self.assertTrue(s.broken)
                got = discsys.hash_tracks_python(c, [0, 1, 2], None, s)    # falls back to the in-process path
        ref = reference(self.path)
        self.assertEqual({i: g["sha1"] for i, g in got.items()}, {i: r["sha1"] for i, r in ref.items()})

    def test_unstartable_worker_means_fallback(self) -> None:
        with mock.patch("romorg.chdsched.subprocess.Popen", side_effect=OSError("no exec")):
            with chd.Chd(self.path, load_map=False) as c, chdsched.Scheduler(2) as s:
                with self.assertRaises(chdsched.PoolError):
                    s.hash_tracks(c, [0])
                self.assertTrue(s.broken)
                got = discsys.hash_tracks_python(c, [0], None, s)
        self.assertEqual(got[0]["sha1"], reference(self.path)[0]["sha1"])


class WorkerProcessTest(Base):
    """What the pool needs from the platform (written for Windows, run everywhere): real worker processes, binary
    pipes, no console window, no inherited file handles, and files the workers had open can be moved afterwards."""

    def test_the_pool_really_runs_worker_processes(self) -> None:
        with chd.Chd(self.path, load_map=False) as c, chdsched.Scheduler(2, chunk_bytes=4000) as s:
            got = s.hash_tracks(c, range(len(c.tracks)))
            self.assertTrue(s.pooled)
            self.assertGreater(s.stats["chunks"], 3)
            self.assertTrue(s._workers)
            self.assertTrue(all(w.proc.poll() is None for w in s._workers))
        self.assertEqual(got, reference(self.path))

    def test_workers_get_binary_unbuffered_pipes_and_no_console_window(self) -> None:
        real = subprocess.Popen
        seen = []

        def spy(cmd, **kw):
            seen.append((cmd, kw))
            return real(cmd, **kw)
        with mock.patch("romorg.chdsched.subprocess.Popen", side_effect=spy):
            proc = chdsched.spawn_worker()
        try:
            cmd, kw = seen[0]
            self.assertEqual((kw["bufsize"], kw.get("text"), kw.get("universal_newlines")), (0, None, None))
            self.assertEqual((kw["stdin"], kw["stdout"]), (subprocess.PIPE, subprocess.PIPE))
            if winproc.IS_WINDOWS:
                self.assertTrue(kw["creationflags"] & subprocess.DETACHED_PROCESS)    # no console, no window
                self.assertNotEqual(kw.get("close_fds"), False)       # only the pipes are inherited
            else:
                self.assertTrue(kw["start_new_session"])
            # a request whose bytes include CR LF and a lone LF must come back untranslated
            payload = b"\r\n\n\x1a\x00" * 1000
            head = {"id": 1, "op": "compress", "codecs": ["zlib", "", "", ""], "hunk_bytes": len(payload), "cd": False,
                    "hints": ["data"], "n": len(payload)}
            proc.stdin.write(json.dumps(head).encode() + b"\n" + payload)
            rd = io.BufferedReader(proc.stdout)
            reply = json.loads(rd.readline())
            blob = rd.read(reply["n"])
            self.assertEqual(len(blob), reply["n"])
            self.assertEqual(reply["items"][0][2], binascii.crc_hqx(payload, 0xFFFF))
        finally:
            chdsched.kill_worker(proc)

    def test_a_file_open_in_the_parent_is_not_inherited_by_the_workers(self) -> None:
        held = self.dir / "held.bin"
        f = open(held, "wb")
        try:
            procs = [chdsched.spawn_worker() for _ in range(2)]
        finally:
            f.close()
        try:
            os.replace(held, self.dir / "moved.bin")     # WinError 32 if a worker had inherited the handle
            os.remove(self.dir / "moved.bin")
        finally:
            for proc in procs:
                chdsched.kill_worker(proc)

    def test_release_closes_the_file_in_every_worker(self) -> None:
        moved = self.dir / "moved.chd"
        ref = reference(self.path)
        with chdsched.Scheduler(3, chunk_bytes=4000) as s:
            with chd.Chd(self.path, load_map=False) as c:
                first = s.hash_tracks(c, range(len(c.tracks)))
            self.assertTrue(s.pooled)
            if winproc.IS_WINDOWS:                        # the workers keep it open for the next chunks ...
                with self.assertRaises(PermissionError):
                    os.replace(self.path, moved)
            self.assertTrue(s.release(Path(self.path)))
            os.replace(self.path, moved)                  # ... until it is released
            with chd.Chd(moved, load_map=False) as c:     # the pool goes on working
                self.assertEqual(s.hash_tracks(c, range(len(c.tracks))), first)
            self.assertTrue(s.pooled)
            self.assertTrue(s.release(moved))
            os.remove(moved)
        self.assertEqual(first, ref)

    def test_release_on_a_broken_pool_kills_the_survivors_so_the_file_can_move(self) -> None:
        moved = self.dir / "moved.chd"
        with chdsched.Scheduler(3, chunk_bytes=4000) as s:
            with chd.Chd(self.path, load_map=False) as c:
                s.hash_tracks(c, range(len(c.tracks)))
            procs = [w.proc for w in s._workers]
            threads = [w.thread for w in s._workers]
            s.broken = True                               # as after a worker failed for good
            self.assertTrue(s.release(self.path))
            os.replace(self.path, moved)                  # WinError 32 if a survivor still had it open
            self.assertTrue(all(p.poll() is not None for p in procs))
            for t in threads:
                t.join(5)
                self.assertFalse(t.is_alive())            # no worker thread spins on the broken pool
        os.remove(moved)

    def test_release_waits_for_a_failed_worker_to_exit(self) -> None:
        moved = self.dir / "moved.chd"
        with chdsched.Scheduler(2, chunk_bytes=4000) as s:
            with chd.Chd(self.path, load_map=False) as c:
                s.hash_tracks(c, range(len(c.tracks)))
            w = s._workers[0]
            with s._cv:                                   # failed and taken out of the pool, process not reaped yet
                s._workers.remove(w)
                s._gone.append(w)
            killer = threading.Timer(0.3, w.kill)
            killer.start()
            t0 = time.monotonic()
            self.assertTrue(s.release(self.path))
            self.assertGreaterEqual(time.monotonic() - t0, 0.2)   # it waited for that process ...
            self.assertIsNotNone(w.proc.poll())
            killer.join()
            os.replace(self.path, moved)                  # ... so nothing holds the file any more
        os.remove(moved)

    def test_restart_workers_frees_every_file_and_the_pool_goes_on(self) -> None:
        moved = self.dir / "moved.chd"
        ref = reference(self.path)
        with chdsched.Scheduler(2, chunk_bytes=4000) as s:
            with chd.Chd(self.path, load_map=False) as c:
                s.hash_tracks(c, range(len(c.tracks)))
            old = [w.proc for w in s._workers]
            s.restart_workers()
            self.assertTrue(all(p.poll() is not None for p in old))
            os.replace(self.path, moved)
            with chd.Chd(moved, load_map=False) as c:
                self.assertEqual(s.hash_tracks(c, range(len(c.tracks))), ref)
            self.assertTrue(s.pooled)
            self.assertTrue(s.release(moved))
        os.remove(moved)

    def test_killing_workers_needs_no_taskkill_unless_frozen(self) -> None:
        with mock.patch("romorg.winproc.kill_tree") as tree:
            live = chdsched.spawn_worker()
            chdsched.kill_worker(live)                     # an interpreter's worker has no children
            self.assertIsNotNone(live.poll())
            done = chdsched.spawn_worker()
            done.stdin.close()
            done.wait(10)
            frozen = chdsched.spawn_worker()
            with mock.patch.object(sys, "frozen", True, create=True):
                chdsched.kill_worker(done)                 # it has exited: nothing to kill
                try:
                    chdsched.kill_worker(frozen)
                finally:
                    frozen.kill()
                    frozen.wait(10)
        self.assertEqual(tree.call_count, 1 if winproc.IS_WINDOWS else 0)   # the one-file exe: its whole tree

    def test_release_without_workers_or_after_close_returns_at_once(self) -> None:
        self.assertTrue(chdsched.Scheduler(1).release(self.path))
        s = chdsched.Scheduler(2)
        self.assertTrue(s.release(self.path))             # no worker started yet
        s.close()
        self.assertTrue(s.release(self.path))


class ErrorsTest(Base):
    def test_corrupt_and_unsupported_files_raise_the_readers_errors(self) -> None:
        bad = Path(self.dir / "bad.chd")
        data = bytearray(Path(self.path).read_bytes())
        for i in range(2000, 2600):                                # inside the compressed hunks
            data[i] ^= 0xA5
        bad.write_bytes(bytes(data))
        with chd.Chd(bad, load_map=False) as c, chdsched.Scheduler(2) as s:
            with self.assertRaises(chd.ChdError):
                s.hash_tracks(c, [0, 1, 2])
        z = str(self.dir / "zstd.chd")
        T.build_dvd_chd(z, T.make_iso(40, 3), pick=lambda h: "wxyz", codecs=("lzma", "zlib", "wxyz", "zstd"))
        with chd.Chd(z, load_map=False) as c, chdsched.Scheduler(2) as s:
            with self.assertRaises(chd.ChdUnsupported) as cm:
                s.hash_tracks(c, [0])
            self.assertTrue(cm.exception.needs_chdman)
            self.assertTrue(s.pooled)                              # a bad FILE does not break the pool
            with chd.Chd(self.path, load_map=False) as good:
                self.assertEqual(len(s.hash_tracks(good, [0])), 1)

    def test_worker_count_rules(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != chdsched.ENV_WORKERS}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("romorg.chdsched.mem_available", return_value=64 << 30):
                cpus = os.cpu_count() or 1
                auto = min(chdsched.WINDOWS_MAX_AUTO, max(1, cpus - 1)) if winproc.IS_WINDOWS else cpus
                self.assertEqual(chdsched.default_workers(0), min(chdsched.MAX_WORKERS, auto))
                self.assertEqual(chdsched.default_workers(3), 3)           # the config key
                self.assertEqual(chdsched.default_workers(1), 1)
                self.assertEqual(chdsched.default_workers(999), chdsched.MAX_WORKERS)
            with mock.patch("romorg.chdsched.mem_available", return_value=300 << 20):   # little RAM: fewer processes
                self.assertLessEqual(chdsched.default_workers(0), 2)
        with mock.patch.dict(os.environ, {chdsched.ENV_WORKERS: "5"}):
            self.assertEqual(chdsched.default_workers(0), 5)
            self.assertEqual(chdsched.default_workers(2), 5)               # the environment wins
        with mock.patch.dict(os.environ, {chdsched.ENV_WORKERS: "1"}):
            self.assertEqual(chdsched.default_workers(8), 1)
            self.assertFalse(chdsched.make_scheduler(1).pooled)


class ScanWithSchedulerTest(unittest.TestCase):
    def test_parallel_scan_equals_sequential_scan(self) -> None:
        from test_dreamcast import World
        w = World(self)
        for g in ["Beta (Europe)", "Gamma (Japan)", "Alpha (USA) (En,Fr)", "Delta (USA) (Demo)"]:
            w.chd(g, f"{g}/{g}.chd")
        seq = w.scan(cache_path=w.base / "a.sqlite", workers=1)
        par = w.scan(cache_path=w.base / "b.sqlite", workers=3)
        key = lambda r: sorted((m.unit.game.name, m.level, tuple(t["sha1"] for t in m.unit.tracks)) for m in r.matched)  # noqa: E731
        self.assertEqual(key(seq), key(par))
        self.assertEqual(len(par.matched), 4)
        self.assertEqual(par.engine, "python")
        self.assertGreater(par.engine_info["python_bytes"], 0)
        self.assertEqual(par.engine_info["workers"], 3)
        self.assertIn("Built-in reader (3 processes", par.engine_info["text"])

    def test_parallel_verify_and_cancel(self) -> None:
        from test_dreamcast import World
        w = World(self)
        for g in ("Beta (Europe)", "Gamma (Japan)"):
            w.chd(g, f"{g}/{g}.chd")
        r = w.scan()
        res = dreamcast.verify_units(r, cache_path=w.cache, workers=3)
        self.assertEqual((res["verified"], res["failed"]), (2, []))
        self.assertIn("Built-in reader", res["engine_text"])
        r = w.scan(cache_path=w.base / "other.sqlite")
        with self.assertRaises(scanner.ScanCancelled):
            dreamcast.verify_units(r, cache_path=w.base / "other.sqlite", workers=3, cancel=lambda: True)


if __name__ == "__main__":
    unittest.main()
