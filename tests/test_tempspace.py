"""Amendment 12: scratch space policy (RAM when safe, else the app cache, never the ROM folder)."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
from romorg import chdtool, dreamcast, scanner, tempspace  # noqa: E402
from test_dreamcast import World, tree  # noqa: E402

GB = 1024 ** 3
MB = 1024 ** 2


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.ram = self.base / "shm"
        self.ram.mkdir()
        self.disk = self.base / "cache" / "tmp"
        self.library = self.base / "roms"
        self.library.mkdir()
        p = mock.patch.dict(os.environ, {tempspace.ENV_DIR: str(self.disk)})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(tempspace.ENV_RESERVE, None)
        tempspace.configure({})

    def patch(self, ram_free=10 * GB, mem=16 * GB, disk_free=50 * GB, ram_roots=True):
        def free(path):
            return ram_free if Path(path).is_relative_to(self.ram) else disk_free
        ps = [mock.patch.object(tempspace, "ram_roots", return_value=[self.ram] if ram_roots else []),
              mock.patch.object(tempspace, "mem_available", return_value=mem),
              mock.patch.object(tempspace, "free_bytes", side_effect=free)]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

    def test_required_size_is_tracks_plus_five_percent_and_64_mib(self) -> None:
        self.assertEqual(tempspace.required_bytes(0), 64 * MB)
        self.assertEqual(tempspace.required_bytes(1000 * MB), 1000 * MB + 50 * MB + 64 * MB)

    def test_ram_is_chosen_when_there_is_plenty(self) -> None:
        self.patch()
        plan = tempspace.choose(tempspace.required_bytes(700 * MB), [self.library])
        self.assertEqual((plan.kind, plan.root), ("ram", self.ram))
        self.assertEqual(plan.message, "decoding in RAM")
        self.assertIn("RAM available", plan.reason)

    def test_disk_is_chosen_when_ram_is_low(self) -> None:
        self.patch(mem=2 * GB + 300 * MB)            # needs 764 MB + 2 GiB reserve
        plan = tempspace.choose(tempspace.required_bytes(700 * MB), [self.library])
        self.assertEqual((plan.kind, plan.root), ("disk", self.disk))
        self.assertIn("RAM not used", plan.reason)
        self.assertEqual(plan.message, f"decoding on disk: {self.disk}")

    def test_disk_is_chosen_when_the_tmpfs_is_too_small(self) -> None:
        self.patch(ram_free=100 * MB)
        plan = tempspace.choose(tempspace.required_bytes(700 * MB), [self.library])
        self.assertEqual(plan.kind, "disk")
        self.assertIn("only", plan.reason)

    def test_reserve_is_configurable(self) -> None:
        self.patch(mem=3 * GB)
        need = tempspace.required_bytes(700 * MB)
        self.assertEqual(tempspace.choose(need, []).kind, "ram")           # 0.76 GB + 2 GiB reserve < 3 GB
        with mock.patch.dict(os.environ, {tempspace.ENV_RESERVE: "4096"}):
            self.assertEqual(tempspace.choose(need, []).kind, "disk")
        with mock.patch.dict(os.environ, {tempspace.ENV_RESERVE: "0"}):
            self.assertEqual(tempspace.choose(need, []).kind, "ram")
        tempspace.configure({"temp_ram_reserve_mb": 8192})
        self.addCleanup(tempspace.configure, {})
        self.assertEqual(tempspace.choose(need, []).kind, "disk")

    def test_nothing_fits_means_no_temp_space(self) -> None:
        self.patch(mem=1 * GB, disk_free=100 * MB)
        plan = tempspace.choose(tempspace.required_bytes(700 * MB), [self.library])
        self.assertEqual(plan.kind, "none")
        self.assertIn("RAM not used", plan.reason)
        self.assertIn("only", plan.reason)
        with self.assertRaises(tempspace.TempUnavailable):
            tempspace.acquire(tempspace.required_bytes(700 * MB), [self.library])
        self.assertFalse(self.disk.exists() and any(self.disk.iterdir()))
        with self.assertRaises(chdtool.NoTempSpace):
            chdtool.acquire_workdir(None, 700 * MB, [self.library])

    def test_unknown_available_ram_never_uses_ram(self) -> None:
        self.patch(mem=None)
        self.assertEqual(tempspace.choose(tempspace.required_bytes(MB), []).kind, "disk")

    def test_never_inside_the_library_or_a_reserved_folder(self) -> None:
        self.patch(ram_roots=False)
        inside = self.library / "cache" / "tmp"
        with mock.patch.dict(os.environ, {tempspace.ENV_DIR: str(inside)}):
            plan = tempspace.choose(tempspace.required_bytes(MB), [self.library])
        self.assertEqual(plan.kind, "none")
        self.assertIn("inside the library folder", plan.reason)
        self.assertFalse(inside.exists())
        with mock.patch.dict(os.environ, {tempspace.ENV_DIR: str(self.base / "_unmatched" / "t")}):
            plan = tempspace.choose(tempspace.required_bytes(MB), [])
        self.assertEqual(plan.kind, "none")
        self.assertIn("reserved folder", plan.reason)

    def test_default_disk_folder_is_cache_tmp_of_the_data_dir(self) -> None:
        with mock.patch.dict(os.environ, {tempspace.ENV_DIR: "", "ROMORG_DATA_DIR": str(self.base / "data")}):
            self.assertEqual(tempspace.disk_root(), self.base / "data" / "cache" / "tmp")
        tempspace.configure({"temp_dir": str(self.base / "cfg")})
        self.addCleanup(tempspace.configure, {})
        with mock.patch.dict(os.environ, {tempspace.ENV_DIR: ""}):
            self.assertEqual(tempspace.disk_root(), self.base / "cfg")

    def test_tmpfs_detection_reads_proc_mounts(self) -> None:
        mounts = self.base / "mounts"
        mounts.write_text("tmpfs /dev/shm tmpfs rw 0 0\n/dev/sda1 / ext4 rw 0 0\n"
                          "/dev/sdb1 /tmp ext4 rw 0 0\ntmpfs /run/user/1000 tmpfs rw 0 0\n")
        with mock.patch.object(tempspace, "PROC_MOUNTS", str(mounts)):
            m = tempspace.tmpfs_mounts()
            self.assertEqual(m, ["/dev/shm", "/run/user/1000"])
            self.assertTrue(tempspace._on_tmpfs(Path("/dev/shm/x"), m))
            self.assertFalse(tempspace._on_tmpfs(Path("/tmp/x"), m))       # /tmp is ext4 here
            self.assertFalse(tempspace._on_tmpfs(Path("/home/x"), m))
        meminfo = self.base / "meminfo"
        meminfo.write_text("MemTotal: 100 kB\nMemAvailable:    2048 kB\n")
        with mock.patch.object(tempspace, "PROC_MEMINFO", str(meminfo)):
            self.assertEqual(tempspace.mem_available(), 2048 * 1024)

    def test_job_folders_are_unique_marked_and_removed(self) -> None:
        self.patch(ram_roots=False)
        a = tempspace.acquire(MB, [self.library])
        b = tempspace.acquire(MB, [self.library])
        self.assertNotEqual(a.path, b.path)
        self.assertTrue((a.path / tempspace.MARKER).is_file())
        (a.path / "big.bin").write_bytes(b"x" * 10)
        a.remove()
        self.assertFalse(a.path.exists())
        tempspace.cleanup_active()                       # what atexit does
        self.assertFalse(b.path.exists())

    def test_sweep_only_removes_marked_dead_folders(self) -> None:
        root = self.base / "scratch"
        root.mkdir()
        import json

        def job(name, pid, marked=True):
            d = root / name
            d.mkdir()
            (d / "x.bin").write_bytes(b"1")
            if marked:
                (d / tempspace.MARKER).write_text(json.dumps({"app": tempspace.MARKER_APP, "pid": pid}))
            return d
        dead = job(f"{tempspace.JOB_PREFIX}dead", 999999999)
        unmarked = job(f"{tempspace.JOB_PREFIX}unmarked", 999999999, marked=False)
        alien = job("somebody-elses", 999999999)
        foreign_marker = job(f"{tempspace.JOB_PREFIX}alien", 1)
        (foreign_marker / tempspace.MARKER).write_text(json.dumps({"app": "other-app", "pid": 999999999}))
        live = job(f"{tempspace.JOB_PREFIX}live", os.getppid())
        mine = job(f"{tempspace.JOB_PREFIX}mine", os.getpid())
        tempspace._active.add(mine)
        self.addCleanup(tempspace._active.discard, mine)
        removed = tempspace.sweep_stale([root])
        self.assertEqual(removed, [str(dead)])
        for keep in (unmarked, alien, foreign_marker, live, mine):
            self.assertTrue(keep.exists(), keep)
        os.utime(live, (1, 1))                           # old folder of a live pid is swept after max_age
        self.assertEqual(tempspace.sweep_stale([root]), [str(live)])


class DecodeTest(unittest.TestCase):
    """The fake chdman decodes where the policy says, and nothing is ever created in the library."""

    def setUp(self) -> None:
        self.w = World(self)
        w = self.w
        self.bin = w.base / "bin"
        self.bin.mkdir()
        self.fake = T.install_fake_chdman(self.bin)
        disc = w.discs["Beta (Europe)"]
        raw = T.prepare_fake_raw(w.base / "fakeraw", disc)
        env = {"FAKE_CHD": str(w.base / "unused.chd"), "FAKE_RAW": str(raw), "FAKE_LOG": str(w.base / "log.txt")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.chdman = chdtool.Chdman([str(self.fake)], "configured", str(self.fake))
        self.ram = w.base / "shm"
        self.ram.mkdir()
        self.disk = w.base / "diskscratch"
        self.mem = 16 * GB
        for p in (mock.patch.dict(os.environ, {tempspace.ENV_DIR: str(self.disk)}),
                  mock.patch.object(tempspace, "ram_roots", return_value=[self.ram]),
                  mock.patch.object(tempspace, "mem_available", side_effect=lambda: self.mem)):
            p.start()
            self.addCleanup(p.stop)
        w.chd("Beta (Europe)", "Beta/Beta (Europe).chd")
        self.seen: list[tuple[str, dict]] = []

    def watch(self):
        """Record the library tree and the scratch locations whenever chdman extracts."""
        real = chdtool.extract_cd

        def spy(chdman, chd, workdir, *a, **kw):
            self.seen.append((str(Path(workdir).parent), tree(self.w.root)))
            return real(chdman, chd, workdir, *a, **kw)
        return mock.patch.object(chdtool, "extract_cd", spy)

    def test_scan_decodes_in_ram_and_leaves_the_library_alone(self) -> None:
        before = tree(self.w.root)
        with self.watch():
            r = self.w.scan(chdman=self.chdman, engine="chdman")
        self.assertEqual(r.matched[0].level, "verified")
        self.assertEqual(self.seen[0][0], str(self.ram))
        self.assertEqual(self.seen[0][1], before)            # not even a hidden folder in the library during the decode
        self.assertEqual(tree(self.w.root), before)
        self.assertEqual(r.temp["ram"], 1)
        self.assertEqual(r.temp["last"]["where"], "ram")
        self.assertEqual(r.summary()["temp"]["ram"], 1)
        self.assertIn("in RAM", r.summary()["temp_text"])
        self.assertEqual(list(self.ram.iterdir()), [])        # cleaned
        self.assertFalse(self.disk.exists() and any(self.disk.iterdir()))

    def test_scan_decodes_on_disk_when_ram_is_low(self) -> None:
        self.mem = 1 * GB
        before = tree(self.w.root)
        with self.watch():
            r = self.w.scan(chdman=self.chdman, engine="chdman")
        self.assertEqual(r.matched[0].level, "verified")
        self.assertEqual(self.seen[0][0], str(self.disk))
        self.assertEqual(tree(self.w.root), before)
        self.assertEqual((r.temp["disk"], r.temp["last"]["where"]), (1, "disk"))
        self.assertIn(str(self.disk), r.summary()["temp_text"])
        self.assertEqual(list(self.disk.iterdir()), [])

    def test_scan_falls_back_to_python_when_nothing_fits(self) -> None:
        self.mem = 1 * GB
        before = tree(self.w.root)
        with mock.patch.object(tempspace, "free_bytes", return_value=1000), self.watch():
            r = self.w.scan(chdman=self.chdman, engine="chdman")
        self.assertEqual((r.matched[0].level, r.matched[0].unit.via), ("identified", "python"))
        self.assertEqual(self.seen, [])                       # chdman never ran
        self.assertFalse((self.w.base / "log.txt").exists())
        self.assertEqual(tree(self.w.root), before)
        self.assertEqual(r.temp["python"], 1)
        self.assertIn("not enough temporary space", r.temp["last"]["reason"])

    def test_a_scan_sweeps_stale_job_folders_of_every_root(self) -> None:
        import json
        stale = []
        for root in (self.ram, self.disk):
            root.mkdir(exist_ok=True)
            d = root / f"{tempspace.JOB_PREFIX}old"
            d.mkdir()
            (d / tempspace.MARKER).write_text(json.dumps({"app": tempspace.MARKER_APP, "pid": 999999999}))
            (d / "big.bin").write_bytes(b"x" * 100)
            stale.append(d)
        stranger = self.ram / "other-app-file"
        stranger.write_bytes(b"keep")
        r = self.w.scan(chdman=self.chdman, engine="chdman")
        self.assertEqual([d.exists() for d in stale], [False, False])
        self.assertTrue(stranger.exists())
        self.assertEqual(len(r.swept), 2)

    def test_cancel_removes_the_scratch_folder_promptly(self) -> None:
        ev = threading.Event()
        threading.Timer(0.6, ev.set).start()
        t0 = time.time()
        before = tree(self.w.root)
        with mock.patch.dict(os.environ, {"FAKE_SLOW_EXTRACT": "1"}):
            with self.assertRaises(scanner.ScanCancelled):
                self.w.scan(chdman=self.chdman, engine="chdman", cancel=ev)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(list(self.ram.iterdir()), [])
        self.assertEqual(tree(self.w.root), before)

    def test_verify_fully_uses_the_policy(self) -> None:
        r = self.w.scan(engine="python")
        before = tree(self.w.root)
        with self.watch():
            res = dreamcast.verify_units(r, self.chdman, cache_path=self.w.cache, engine="chdman")
        self.assertEqual((res["verified"], res["failed"]), (1, []))
        self.assertEqual(res["temp"]["ram"], 1)
        self.assertEqual(self.seen[0][0], str(self.ram))
        self.assertEqual(tree(self.w.root), before)


class ConvertTempTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)
        w = self.w
        bindir = w.base / "bin"
        bindir.mkdir()
        self.fake = T.install_fake_chdman(bindir)
        self.disc = w.discs["Epsilon (USA)"]
        self.fixture = w.base / "fixture.chd"
        self.disc.write_chd(self.fixture)
        raw = T.prepare_fake_raw(w.base / "fakeraw", self.disc)
        env = {"FAKE_CHD": str(self.fixture), "FAKE_RAW": str(raw), "FAKE_LOG": str(w.base / "log.txt")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.chdman = chdtool.Chdman([str(self.fake)], "configured", str(self.fake))
        self.scratch = T.isolate_temp(self, w.base)
        w_raw = self.disc.write_raw(w.root / "raw dump", "Epsilon")
        self.sheet = w_raw

    def test_new_chd_is_a_part_file_next_to_its_destination(self) -> None:
        r = self.w.scan()
        ops = dreamcast.plan_convert(r, True)
        seen = []
        real = chdtool.create_cd

        def spy(chdman, source, out, *a, **kw):
            seen.append(Path(out))
            res = real(chdman, source, out, *a, **kw)
            seen.append(sorted(p.name for p in Path(out).parent.iterdir()))
            return res
        with mock.patch.object(chdtool, "create_cd", spy):
            res = dreamcast.apply_conversions(ops, self.w.root, self.chdman, r.index, writer="chdman")
        self.assertEqual(res["converted"], 1)
        dst = self.w.root / "Epsilon (USA)" / "Epsilon (USA).chd"
        self.assertEqual(seen[0], dst.with_name(dst.name + dreamcast.PART_SUFFIX))
        self.assertEqual(seen[0].parent, dst.parent)
        self.assertEqual(sorted(os.listdir(dst.parent)), ["Epsilon (USA).chd"])        # .part renamed into place
        self.assertFalse([p for p in os.listdir(self.w.root) if p.startswith(".romorg") and "undo" not in p])
        self.assertEqual(list(self.scratch.iterdir()) if self.scratch.exists() else [], [])

    def test_conversion_verifies_with_the_python_reader_when_no_scratch_space_fits(self) -> None:
        r = self.w.scan()
        ops = dreamcast.plan_convert(r, True)
        with mock.patch.object(tempspace, "free_bytes", return_value=1000):     # only the scratch policy sees this
            res = dreamcast.apply_conversions(ops, self.w.root, self.chdman, r.index, writer="chdman")
        self.assertEqual((res["converted"], res["failed"]), (1, []))
        self.assertTrue((self.w.root / "Epsilon (USA)" / "Epsilon (USA).chd").is_file())
        self.assertEqual(sum(1 for ln in (self.w.base / "log.txt").read_text().splitlines() if ln.startswith("extractcd")), 0)

    def test_a_leftover_part_file_of_a_crashed_run_is_replaced(self) -> None:
        r = self.w.scan()
        ops = dreamcast.plan_convert(r, True)
        dst = self.w.root / "Epsilon (USA)" / "Epsilon (USA).chd"
        dst.parent.mkdir()
        dst.with_name(dst.name + dreamcast.PART_SUFFIX).write_bytes(b"half")
        r = self.w.scan()
        ops = dreamcast.plan_convert(r, True)
        res = dreamcast.apply_conversions(ops, self.w.root, self.chdman, r.index, writer="chdman")
        self.assertEqual(res["converted"], 1)
        self.assertEqual(sorted(os.listdir(dst.parent)), ["Epsilon (USA).chd"])


if __name__ == "__main__":
    unittest.main()
