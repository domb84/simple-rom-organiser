"""Scans are remembered: a scan that was stopped carries on, a rescan reads only new and changed files (also when files were only
moved or renamed), "recalculate" reads everything again, and coming back to a system needs no new scan."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from tests.test_collection_api import CollectionCase, GBA, SNES, put  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)

from romorg import scanner, server


class Counting(CollectionCase):
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

    def test_a_collection_rescan_reads_nothing_again(self) -> None:
        root = self.make()
        n = self.count_hashes()
        self.call("POST", "/api/collection/save", {"root": str(root)})
        self.job("/api/collection/scan")
        self.assertEqual(n[0], 2)
        n[0] = 0
        self.job("/api/collection/scan")
        self.assertEqual(n[0], 0)

    def test_a_new_file_is_the_only_one_read(self) -> None:
        root = self.make()
        n = self.count_hashes()
        self.call("POST", "/api/collection/save", {"root": str(root)})
        self.job("/api/collection/scan")
        n[0] = 0
        put(root / "Dump" / "c.sfc", self.rom["Gamma (USA)"])
        self.job("/api/collection/scan")
        self.assertEqual(n[0], 1)

    def test_files_that_were_only_moved_or_renamed_are_not_read_again(self) -> None:
        root = self.make()
        n = self.count_hashes()
        self.call("POST", "/api/collection/save", {"root": str(root)})
        self.job("/api/collection/scan")
        n[0] = 0
        (root / "Dump" / "a.sfc").rename(root / "Dump" / "renamed.sfc")
        (root / "elsewhere").mkdir()
        (root / "Dump" / "b.sfc").rename(root / "elsewhere" / "b.sfc")
        scan = self.job("/api/collection/scan")["scan"]
        self.assertEqual(n[0], 0)
        self.assertEqual(sum(s["games"] for s in scan["systems"]), 2)                   # (and they are still recognised)

    def test_a_build_and_the_scan_after_it_read_nothing_again(self) -> None:
        root = self.make()
        n = self.count_hashes()
        self.call("POST", "/api/collection/save", {"root": str(root)})
        self.job("/api/collection/scan")
        n[0] = 0
        self.job("/api/collection/apply")
        self.assertEqual(n[0], 0)                                                       # (the files moved: same files, new places)

    def test_recalculate_reads_everything_again(self) -> None:
        root = self.make()
        n = self.count_hashes()
        self.call("POST", "/api/collection/save", {"root": str(root)})
        self.job("/api/collection/scan")
        n[0] = 0
        self.job("/api/collection/scan", {"force": True})
        self.assertEqual(n[0], 2)
        n[0] = 0
        self.job("/api/collection/scan")                                                # and the normal rescan after it uses what was written
        self.assertEqual(n[0], 0)

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

class ComingBack(CollectionCase):
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

    def test_a_collection_build_forgets_the_scans_of_single_systems(self) -> None:
        root = self.tmp / "roms"
        put(root / "Dump" / "a.sfc", self.rom["Alpha (USA)"])
        self.job("/api/scan", {"path": str(root / "Dump"), "platform": SNES})
        self.call("POST", "/api/collection/save", {"root": str(root)})
        self.job("/api/collection/scan")
        self.job("/api/collection/apply")
        self.assertFalse(self.call("POST", "/api/scan/select", {"platform": SNES})["selected"])


class ArchiveFolder(CollectionCase):
    """One archive folder for every system (Settings), a system's own when it has one, and none at all unless one is chosen."""
    ARCHIVE = False

    def test_there_is_no_default_archive_folder(self) -> None:
        self.assertEqual(self.call("GET", "/api/status")["archive"], {"dir": "", "overrides": {}})
        root = self.tmp / "roms"
        put(root / "Dump" / "a.sfc", self.rom["Alpha (USA)"])
        put(root / "Dump" / "note.txt", b"hello")
        self.call("POST", "/api/collection/save", {"root": str(root)})
        info = self.call("GET", "/api/collection")
        self.assertEqual((info["aside_default"], info["aside_is_global"]), ("", False))
        self.job("/api/collection/scan")
        plan = self.job("/api/collection/plan")
        self.assertEqual(plan["sort"]["aside"], "")
        self.assertNotIn("_other", plan["sort"]["counts"])                                    # nothing would be archived
        self.job("/api/collection/apply")
        self.assertTrue((root / "Dump" / "note.txt").is_file())                               # the note stays where it was
        self.assertTrue((root / "snes" / "Alpha (USA).sfc").is_file())
        self.assertFalse((self.tmp / "rom-archive").exists())
        code, _ = self.http("POST", "/api/collection/aside/restore", {})
        self.assertEqual(code, 409)

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

    def test_a_collection_uses_the_global_folder_unless_it_has_its_own(self) -> None:
        root = self.tmp / "roms"
        put(root / "Dump" / "a.sfc", self.rom["Alpha (USA)"])
        put(root / "Dump" / "note.txt", b"hello")
        self.call("POST", "/api/collection/save", {"root": str(root)})
        glob = self.tmp / "everything-archive"
        self.call("POST", "/api/settings/archive", {"dir": str(glob)})
        info = self.call("GET", "/api/collection")
        self.assertEqual((info["aside_default"], info["aside_is_global"]), (str(glob), True))
        self.job("/api/collection/scan")
        self.job("/api/collection/apply")
        self.assertTrue((glob / "_other" / "Dump" / "note.txt").is_file())
