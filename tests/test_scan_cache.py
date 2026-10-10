"""Scans are remembered: a scan that was stopped carries on, a rescan reads only new and changed files (also when files were only
moved or renamed), "recalculate" reads everything again, and coming back to a system needs no new scan."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from tests.servercase import ServerCase, GBA, SNES, put  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)

from romorg import scanner, server


class Counting(ServerCase):
    def count_hashes(self):
        n = [0]
        real1, real2 = scanner.hash_file, scanner.hash_file_variants

        def one(*a, **k):
            n[0] += 1
            return real1(*a, **k)

        def two(*a, **k):
            n[0] += 1
            return real2(*a, **k)
        patches = [mock.patch.object(scanner, "hash_file", one), mock.patch.object(scanner, "hash_file_variants", two)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return n


class RescanReadsOnlyWhatChanged(Counting):
    def make(self) -> Path:
        root = self.tmp / "roms"
        put(root / "Dump" / "a.sfc", self.rom["Alpha (USA)"])
        put(root / "Dump" / "b.sfc", self.rom["Beta (USA)"])
        return root






    def test_a_system_scan_rescan_and_recalculate(self) -> None:
        root = self.make()
        n = self.count_hashes()
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        self.assertEqual(n[0], 2)
        n[0] = 0
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        self.assertEqual(n[0], 0)
        self.job("/api/scan", {"path": str(root), "platform": SNES, "force": True})
        self.assertEqual(n[0], 2)

    def test_a_stopped_scan_carries_on_from_where_it_was(self) -> None:
        root = self.tmp / "roms"
        for i, name in enumerate(["Alpha (USA)", "Beta (USA)", "Gamma (USA)", "Delta (USA)"]):
            put(root / f"{i}.sfc", self.rom[name])
        real = scanner.hash_file_variants
        seen: list[Path] = []

        def read_then_stop(path, *a, **k):
            seen.append(path)
            out = real(path, *a, **k)
            if len(seen) == 2:                                                          # after the second file: the user presses Cancel
                self.call("POST", "/api/job/cancel")
            return out
        with mock.patch.object(scanner, "hash_file_variants", read_then_stop), mock.patch.object(scanner, "scan_threads", lambda: 1):
            self.call("POST", "/api/scan", {"path": str(root), "platform": SNES})
            first = self.wait()
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(len(seen), 2)
        n = self.count_hashes()
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        self.assertEqual(n[0], 2)                                                       # only the two it had not got to

class ComingBack(ServerCase):
    def test_a_system_keeps_its_scan_while_you_look_at_another(self) -> None:
        snes, gba = self.tmp / "snes", self.tmp / "gba"
        put(snes / "a.sfc", self.rom["Alpha (USA)"])
        put(gba / "r.gba", self.rom["Run (USA)"])
        self.job("/api/scan", {"path": str(snes), "platform": SNES})
        self.job("/api/scan", {"path": str(gba), "platform": GBA})
        self.assertEqual(self.call("GET", "/api/status")["scan"]["platform"], GBA)
        calls = []
        real = scanner.scan
        with mock.patch.object(scanner, "scan", lambda *a, **k: calls.append(1) or real(*a, **k)):
            back = self.call("POST", "/api/scan/select", {"platform": SNES})
        self.assertEqual((back["selected"], calls), (True, []))                          # no scan was made
        status = self.call("GET", "/api/status")["scan"]
        self.assertEqual((status["platform"], status["summary"]["matched_files"]), (SNES, 1))

    def test_a_system_never_scanned_or_with_another_folder_is_not_selected(self) -> None:
        snes = self.tmp / "snes"
        put(snes / "a.sfc", self.rom["Alpha (USA)"])
        self.assertFalse(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])
        self.job("/api/scan", {"path": str(snes), "platform": SNES})
        (self.tmp / "snes2").mkdir()
        self.call("POST", "/api/folders", {"platform": SNES, "path": str(self.tmp / "snes2")})
        self.assertFalse(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])



class KeptOnDisk(Counting):
    """A scan is saved, and is there when the app is started again - while nothing it was made from has changed."""

    def make(self) -> Path:
        root = self.tmp / "roms"
        put(root / "a.sfc", self.rom["Alpha (USA)"])
        put(root / "b.sfc", self.rom["Beta (USA)"])
        return root

    def restart(self) -> None:
        """A new app on the same data folder: nothing is in memory."""
        self.srv.shutdown()
        self.srv.server_close()
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.port = self.srv.port
        import threading
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def scan_and_restart(self) -> Path:
        root = self.make()
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        self.restart()
        self.assertIsNone(self.call("GET", "/api/status")["scan"])               # (nothing in memory yet)
        return root

    def test_the_scan_is_there_after_a_restart_without_scanning_again(self) -> None:
        self.scan_and_restart()
        n = self.count_hashes()
        got = self.call("POST", "/api/scan/select", {"platform": SNES})
        self.assertEqual((got["selected"], n[0]), (True, 0))
        status = self.call("GET", "/api/status")["scan"]
        self.assertEqual((status["platform"], status["summary"]["matched_files"]), (SNES, 2))
        rows = self.call("GET", "/api/scan/results?kind=matched")
        self.assertEqual(rows["total"], 2)
        plan = self.call("POST", "/api/library/plan", {})                                  # and what is built from it works
        self.assertTrue(plan["items"])

    def test_a_changed_folder_means_a_new_scan(self) -> None:
        root = self.scan_and_restart()
        put(root / "c.sfc", self.rom["Gamma (USA)"])
        self.assertFalse(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])

    def test_a_changed_file_with_the_same_size_means_a_new_scan(self) -> None:
        root = self.scan_and_restart()
        later = (root / "a.sfc").stat().st_mtime_ns + 5_000_000_000
        os.utime(root / "a.sfc", ns=(later, later))
        self.assertFalse(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])

    def test_a_new_database_means_a_new_scan(self) -> None:
        self.scan_and_restart()
        dat = next((self.tmp / "data" / "nointro").glob("*Super*.dat"))
        later = dat.stat().st_mtime_ns + 5_000_000_000
        os.utime(dat, ns=(later, later))
        self.assertFalse(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])

    def test_the_scan_options_are_part_of_it(self) -> None:
        self.scan_and_restart()
        self.call("POST", "/api/switch/config", {"verify_scan": True})
        self.assertFalse(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])

    def test_a_file_that_cannot_be_read_is_ignored_and_removed(self) -> None:
        self.scan_and_restart()
        saved = next((self.tmp / "data" / "scans").glob("*.scan"))
        saved.write_bytes(b"not a pickle")
        self.assertFalse(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])
        self.assertFalse(saved.exists())

    def test_another_version_or_other_classes_are_not_used(self) -> None:
        from romorg import scancache
        self.scan_and_restart()
        with mock.patch.object(scancache, "_shape", return_value="something else"):
            self.assertFalse(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])

    def test_a_rescan_after_a_build_replaces_it(self) -> None:
        root = self.make()
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        put(root / "c.sfc", self.rom["Gamma (USA)"])
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        self.restart()
        self.assertTrue(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])
        self.assertEqual(self.call("GET", "/api/status")["scan"]["summary"]["matched_files"], 3)


class ArchiveFolder(ServerCase):
    """One archive folder for every system (Settings), a system's own when it has one, and none at all unless one is chosen."""
    ARCHIVE = False


    def test_a_global_folder_and_a_folder_for_one_system(self) -> None:
        mine, other = str(self.tmp / "my-archive"), str(self.tmp / "gba-archive")
        self.assertEqual(self.call("POST", "/api/settings/archive", {"dir": mine}), {"dir": mine, "overrides": {}})
        out = self.call("POST", "/api/settings/archive", {"dir": other, "platform": GBA})
        self.assertEqual(out, {"dir": mine, "overrides": {GBA: other}})
        self.assertEqual(self.call("GET", "/api/status")["archive"], out)                       # kept
        self.assertEqual(self.call("POST", "/api/settings/archive", {"dir": "", "platform": GBA})["overrides"], {})
        self.assertEqual(self.call("POST", "/api/settings/archive", {"dir": ""})["dir"], "")

    def test_a_file_is_not_a_folder(self) -> None:
        put(self.tmp / "afile", b"x")
        code, _ = self.http("POST", "/api/settings/archive", {"dir": str(self.tmp / "afile")})
        self.assertEqual(code, 400)

