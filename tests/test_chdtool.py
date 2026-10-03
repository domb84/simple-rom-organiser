"""chdman wrapper tests with a fake chdman shell script (the real chdman is not available here)."""

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
from romorg import chd, chdtool  # noqa: E402


class ChdtoolBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.fake = T.install_fake_chdman(self.bin)
        self.disc = T.Disc("Disc (USA)", 3)
        self.raw = T.prepare_fake_raw(self.work / "fakeraw", self.disc)
        self.chd = self.work / "fixture.chd"
        self.disc.write_chd(self.chd)
        env = {"FAKE_CHD": str(self.chd), "FAKE_RAW": str(self.raw), "FAKE_LOG": str(self.work / "log.txt")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.chdman = chdtool.Chdman([str(self.fake)], "configured", str(self.fake))
        self.roms = self.work / "roms"
        self.roms.mkdir()
        self.scratch = T.isolate_temp(self, self.work)


class DetectTest(ChdtoolBase):
    def test_env_override_and_config_key(self) -> None:
        with mock.patch.dict(os.environ, {chdtool.ENV_VAR: str(self.fake)}):
            found = chdtool.detect({})
            self.assertEqual((found.kind, found.argv), ("configured", [str(self.fake)]))
        found = chdtool.detect({chdtool.CONFIG_KEY: str(self.fake)})
        self.assertEqual(found.label, str(self.fake))

    def test_found_on_path(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}):
            os.environ.pop(chdtool.ENV_VAR, None)
            found = chdtool.detect({})
        self.assertEqual(found.kind, "path")
        self.assertEqual(Path(found.argv[0]).resolve(), self.fake.resolve())

    def test_not_found_gives_install_hint(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent", "HOME": str(self.work / "home")}), \
                mock.patch("romorg.chdtool.Path.home", return_value=self.work / "home"), \
                mock.patch("romorg.chdtool.shutil.which", return_value=None), \
                mock.patch("romorg.chdtool._candidates", return_value=[]):
            os.environ.pop(chdtool.ENV_VAR, None)
            self.assertIsNone(chdtool.detect({}))
            info = chdtool.info({})
        self.assertFalse(info["found"])
        self.assertIn("MAME", info["hint"])
        self.assertIn("org.mamedev.MAME", info["hint"])
        self.assertTrue(info["steps"])

    def test_a_program_that_is_not_chdman_is_rejected(self) -> None:
        notit = self.bin / "other"
        notit.write_text("#!/bin/sh\necho hello\n")
        notit.chmod(0o755)
        self.assertFalse(chdtool._probe([str(notit)]))
        self.assertFalse(chdtool._probe([str(self.work / "does-not-exist")]))
        self.assertTrue(chdtool._probe([str(self.fake)]))

    def test_flatpak_permission_logic(self) -> None:
        home = Path.home()
        self.assertTrue(chdtool.flatpak_covers(["host"], Path("/run/media/deck/SD/roms")))
        self.assertTrue(chdtool.flatpak_covers(["home"], home / "Emulation"))
        self.assertFalse(chdtool.flatpak_covers(["home"], Path("/run/media/deck/SD")))
        self.assertTrue(chdtool.flatpak_covers(["/run/media/deck:rw"], Path("/run/media/deck/SD/roms")))
        self.assertFalse(chdtool.flatpak_covers(["xdg-documents"], home / "Emulation"))
        hint = chdtool.flatpak_override_hint("org.mamedev.MAME", Path("/run/media/deck/SD/roms"))
        self.assertIn("flatpak override --user --filesystem=", hint)
        self.assertIn("org.mamedev.MAME", hint)

    def test_flatpak_chdman_without_access_explains_the_override(self) -> None:
        fp = chdtool.Chdman(["flatpak", "run", "--command=chdman", "org.mamedev.MAME"], "flatpak", "x",
                            "org.mamedev.MAME")
        with mock.patch("romorg.chdtool.flatpak_permissions", return_value=["xdg-documents"]):
            with self.assertRaises(chdtool.ChdmanError) as cm:
                chdtool.check_access(fp, Path("/run/media/deck/SD/roms"))
        self.assertIn("flatpak override --user --filesystem=", str(cm.exception))
        with mock.patch("romorg.chdtool.flatpak_permissions", return_value=["host"]):
            chdtool.check_access(fp, Path("/run/media/deck/SD/roms"))     # fine


class RunTest(ChdtoolBase):
    def test_extract_gdi_and_hash(self) -> None:
        wd = chdtool.acquire_workdir(self.chdman, 1000, [self.roms])
        w = wd.path
        self.assertEqual(w.parent, self.scratch)            # the app's scratch folder, never next to the ROMs
        ticks: list = []
        ex = chdtool.extract_cd(self.chdman, self.chd, w, "gdrom", progress=lambda d, t, m: ticks.append((d, t, m)))
        self.assertEqual([t.number for t in ex.tracks], [1, 2, 3])
        self.assertEqual([t.audio for t in ex.tracks], [False, True, False])
        for t, data in zip(ex.tracks, self.disc.bins):
            self.assertEqual(t.size, len(data))
            crc, md5, sha1 = chdtool.hash_range(t.path, t.offset, t.size)
            import hashlib
            self.assertEqual(sha1, hashlib.sha1(data).hexdigest())
        self.assertTrue(any("%" in m for _d, _t, m in ticks))
        chdtool.remove_workdir(wd)
        self.assertFalse(w.exists())

    def test_extract_cue_splits_one_bin_by_track_sizes(self) -> None:
        raw = self.work / "cueraw"
        raw.mkdir()
        (raw / "disc.cue").write_text("FILE disc.bin BINARY\n")
        (raw / "disc.bin").write_bytes(b"".join(self.disc.bins))
        wd = chdtool.acquire_workdir(self.chdman, 1000, [self.roms])
        w = wd.path
        with mock.patch.dict(os.environ, {"FAKE_RAW": str(raw)}):
            ex = chdtool.extract_cd(self.chdman, self.chd, w, "cd", [len(b) for b in self.disc.bins])
        self.assertEqual([t.offset for t in ex.tracks], [0, len(self.disc.t1), len(self.disc.t1) + len(self.disc.t2)])
        crc, md5, sha1 = chdtool.hash_range(ex.tracks[1].path, ex.tracks[1].offset, ex.tracks[1].size)
        import hashlib
        self.assertEqual(sha1, hashlib.sha1(self.disc.t2).hexdigest())
        with self.assertRaises(chdtool.ChdmanError):        # layout that does not add up
            chdtool.extract_cd(self.chdman, self.chd, w, "cd", [1, 2, 3])
        chdtool.remove_workdir(wd)

    def test_create_cd_and_refuse_to_overwrite(self) -> None:
        out = self.roms / "out.chd"
        sheet = self.disc.write_raw(self.work / "rawset")
        ticks = []
        chdtool.create_cd(self.chdman, sheet, out, progress=lambda d, t, m: ticks.append(m))
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_bytes(), self.chd.read_bytes())
        self.assertTrue(any("Compressing" in m for m in ticks))
        with self.assertRaises(chdtool.ChdmanError):
            chdtool.create_cd(self.chdman, sheet, out)

    def test_failure_message_from_chdman(self) -> None:
        out = self.roms / "x.chd"
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "createcd"}):
            with self.assertRaises(chdtool.ChdmanError) as cm:
                chdtool.create_cd(self.chdman, self.disc.write_raw(self.work / "r2"), out)
        self.assertIn("simulated createcd failure", str(cm.exception))
        with self.assertRaises(chdtool.ChdmanError):
            chdtool.create_cd(self.chdman, self.work / "missing.gdi", out)
        self.assertFalse(out.exists())

    def test_cancel_stops_chdman_promptly(self) -> None:
        out = self.roms / "slow.chd"
        sheet = self.disc.write_raw(self.work / "r3")
        ev = threading.Event()
        threading.Timer(0.6, ev.set).start()
        t0 = time.time()
        with mock.patch.dict(os.environ, {"FAKE_SLOW": "1"}):
            with self.assertRaises(chdtool.ChdmanError) as cm:
                chdtool.create_cd(self.chdman, sheet, out, cancel=ev)
        self.assertTrue(cm.exception.cancelled)
        self.assertLess(time.time() - t0, 5)      # the fake would take 11 s
        self.assertFalse(out.exists())

    def test_space_check(self) -> None:
        chdtool.check_space(self.roms, 1024)
        with self.assertRaises(chdtool.ChdmanError) as cm:
            chdtool.check_space(self.roms, 1 << 60)
        self.assertIn("not enough free space", str(cm.exception))

    def test_sweep_stale_removes_only_dead_runs(self) -> None:
        dead = self.roms / f"{chdtool.TEMP_PREFIX}dead"
        dead.mkdir()
        (dead / "pid").write_text("999999999")
        (dead / "big.bin").write_bytes(b"x" * 100)
        mine = self.roms / f"{chdtool.TEMP_PREFIX}mine"
        mine.mkdir()
        (mine / "pid").write_text(str(os.getpid()))
        nopid = self.roms / f"{chdtool.TEMP_PREFIX}nopid"      # no marker: never deleted
        nopid.mkdir()
        other = self.roms / f"{chdtool.TEMP_PREFIX}foreign"
        other.mkdir()
        (other / "pid").write_text(str(os.getppid()))           # a live process of another run
        keep = self.roms / "Game"
        keep.mkdir()
        removed = chdtool.sweep_stale(self.roms)
        self.assertEqual(removed, [dead.name])
        self.assertTrue(mine.exists() and other.exists() and keep.exists() and nopid.exists())
        # an old folder of a live pid is still removed after max_age
        os.utime(other, (1, 1))
        self.assertEqual(chdtool.sweep_stale(self.roms), [other.name])

    def test_parse_gdi_errors(self) -> None:
        bad = self.work / "bad.gdi"
        bad.write_text("nonsense\n")
        with self.assertRaises(chdtool.ChdmanError):
            chdtool.parse_gdi(bad)
        bad.write_text("3\n1 0 4 2352 a.bin 0\n")
        with self.assertRaises(chdtool.ChdmanError):
            chdtool.parse_gdi(bad)


if __name__ == "__main__":
    unittest.main()
