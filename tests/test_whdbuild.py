"""WHDLoad games built from the user's disks (``romorg.whdbuild``): which version, where, the archive and what the next build does."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from romorg import amigainstall as ai, library, platforms, scanner, whdbuild, whdready
from romorg.datfile import Rom
from romorg.organiser import MatchedUnit, RenameOp
from tests.test_amigainstall import make_adf

AMIGA = platforms.get_platform("Commodore Amiga")
GAMES = "Commodore Amiga - Games - [ADF]"
SPS = "Commodore Amiga - Games - SPS"
NAME = "Zap (1993)(Acme)[cr FLT].adf"


def install_package(tmp: Path) -> bytes:
    """An install that copies its slave and the disk's "game" file into WORK:Zap."""
    files = {"ZapInstall/Zap.slave": b"\x70\xff\x4e\x75WHDLOADS" + bytes(20),
             "ZapInstall/Install": b'(copyfiles (source "Zap.slave") (dest "WORK:Zap")) (copyfiles (source "Zap:game") (dest "WORK:Zap"))'}
    ai.write_lha(tmp / "pkg.lha", files)
    return (tmp / "pkg.lha").read_bytes()


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "amiga"
        (self.root / "Games").mkdir(parents=True)
        self.disk = self.root / "Games" / NAME
        self.disk.write_bytes(make_adf("Zap", {"game": b"the game"}))
        self.pkg = install_package(self.tmp)
        patch = mock.patch.multiple(whdready, level=mock.Mock(return_value=whdready.BOOTS), archive=mock.Mock(return_value="Zap.lha"))
        patch.start()
        self.addCleanup(patch.stop)

    def plan(self) -> SimpleNamespace:
        rom = Rom(name=NAME, size=1, crc="", md5="", sha1="", game=NAME[:-4], dat=GAMES)
        op = RenameOp(self.disk, self.disk, "ok")
        unit = MatchedUnit(self.disk, op, rom, frozenset(), "", GAMES, True, False, False)
        sel = library.Selection(decisions={1: library.Decision(key=1, action=library.KEEP)})
        return SimpleNamespace(selection=sel, owners={1: (op, unit)})

    def build(self, todo: list) -> dict:
        return whdbuild.build(todo, self.root, [{"archive": "Zap.lha", "title": "Zap", "date": "2024-01-01"}], fetch=lambda inst: self.pkg,
                              decruncher=lambda: None)

    def test_a_kept_game_becomes_a_whdload_archive_beside_its_disks(self) -> None:
        todo = whdbuild.targets(self.plan(), AMIGA)
        self.assertEqual([(t.title, t.archive, t.dest) for t in todo], [("Zap", "Zap.lha", self.root / "Games" / "Zap (1993)(Acme)[cr FLT].lha")])
        out = self.build(todo)
        self.assertEqual(len(out["built"]), 1)
        lha = todo[0].dest
        self.assertTrue(scanner.is_built_whdload(lha))                                  # (the marker comes first)
        from romorg.lha import LhaFile
        arc = LhaFile(lha.read_bytes())
        self.assertEqual(arc.read("Zap/game"), b"the game")
        self.assertEqual(whdbuild.marker_of(lha)["source"], [NAME])
        self.assertNotIn(lha, scanner.collect_files(self.root))                        # (no file of a DAT: never "unmatched")
        self.assertEqual(self.build(todo)["kept"], 1)                                  # (the same again: left as it is)

    def test_the_archive_follows_its_disks_and_goes_with_its_game(self) -> None:
        self.build(whdbuild.targets(self.plan(), AMIGA))
        (self.root / "Elsewhere").mkdir()
        new = self.root / "Elsewhere" / NAME
        self.disk.rename(new)
        self.disk = new
        out = self.build(whdbuild.targets(self.plan(), AMIGA))
        self.assertEqual((len(out["moved"]), len(out["built"])), (1, 0))                 # (moved, not made again)
        self.assertTrue((self.root / "Elsewhere" / "Zap (1993)(Acme)[cr FLT].lha").exists())
        out = self.build([])                                                           # (the game is no longer kept)
        self.assertEqual(len(out["removed"]), 1)
        self.assertFalse((self.root / "Elsewhere" / "Zap (1993)(Acme)[cr FLT].lha").exists())

    def test_never_over_a_file_of_another_s_and_never_what_does_not_install(self) -> None:
        todo = whdbuild.targets(self.plan(), AMIGA)
        todo[0].dest.write_bytes(b"the user's own archive")
        out = self.build(todo)
        self.assertEqual(len(out["failed"]), 1)
        self.assertEqual(todo[0].dest.read_bytes(), b"the user's own archive")
        with mock.patch.object(whdready, "level", return_value=whdready.NONE):
            self.assertEqual(whdbuild.targets(self.plan(), AMIGA), [])
        with mock.patch.object(whdready, "archive", return_value="ZapArcadia.lha"):           # (an Arcadia board's: never floppies)
            self.assertEqual(whdbuild.targets(self.plan(), AMIGA), [])
        with mock.patch.object(whdready, "archive", return_value="ZapDemo.lha"):             # (a demo's install: not the full game's ...
            self.assertEqual(len(whdbuild.targets(self.plan(), AMIGA)), 1)                      #  unless the list knows it boots from them)
            with mock.patch.object(whdready, "level", return_value=whdready.IMAGES):
                self.assertEqual(whdbuild.targets(self.plan(), AMIGA), [])

    def test_of_two_versions_as_good_the_best_platform_is_built(self) -> None:
        ocs, aga = "Zap (1993)(Acme)[0721]", "Zap (1994)(Acme)(AGA)[1624]"
        owners, decisions = {}, {}
        for key, rel in enumerate((ocs, aga), 1):
            path = self.root / "Games" / f"{rel}.zip"
            path.write_bytes(b"PK")
            rom = Rom(name=f"{rel} - Zap_Disk1.ipf", size=1, crc="", md5="", sha1="", game=rel, dat=SPS)
            op = RenameOp(path, path, "ok")
            owners[key] = (op, MatchedUnit(path, op, rom, frozenset(), "", SPS, True, True, False, member="Zap_Disk1.ipf"))
            decisions[key] = library.Decision(key=key, action=library.KEEP)
        plan = SimpleNamespace(selection=library.Selection(decisions=decisions), owners=owners)
        levels = {ocs: whdready.BOOTS, aga: whdready.BOOTS}
        with mock.patch.object(whdready, "release_of", side_effect=lambda rom: [levels[rom.game], "Zap.lha"]):
            self.assertEqual([t.names for t in whdbuild.targets(plan, AMIGA)], [[aga]])        # (Second Samurai: the AGA release)
            levels[aga] = whdready.FILES                                                     # (... unless only the OCS one is known to boot)
            self.assertEqual([t.names for t in whdbuild.targets(plan, AMIGA)], [[ocs]])

    def test_a_rebuild_that_fails_keeps_the_archive_its_disks_had(self) -> None:
        self.build(whdbuild.targets(self.plan(), AMIGA))
        (self.root / "Elsewhere").mkdir()
        self.disk.rename(self.root / "Elsewhere" / NAME)
        self.disk = self.root / "Elsewhere" / NAME

        def offline(inst):
            raise OSError("network is unreachable")
        for installs in ([{"archive": "Zap.lha", "title": "Zap", "date": "2025-06-01"}],      # (a new date: made again, offline)
                         [{"archive": "ZapNew.lha", "title": "Zap", "date": "2025-06-01"}]):  # (its install gone from the list)
            out = whdbuild.build(whdbuild.targets(self.plan(), AMIGA), self.root, installs, fetch=offline, decruncher=lambda: None)
            self.assertEqual((len(out["failed"]), out["removed"]), (1, []))
            self.assertTrue((self.root / "Elsewhere" / "Zap (1993)(Acme)[cr FLT].lha").exists())

    def test_a_write_that_fails_leaves_nothing_half_written(self) -> None:
        import errno
        real = ai.write_lha

        def full_disk(path, files, dirs=None, when=None):
            real(path, files, dirs, when)
            Path(path).write_bytes(Path(path).read_bytes()[:100])
            raise OSError(errno.ENOSPC, "No space left on device")
        todo = whdbuild.targets(self.plan(), AMIGA)
        with mock.patch.object(ai, "write_lha", full_disk):
            self.assertEqual(len(self.build(todo)["failed"]), 1)
        self.assertEqual(sorted(p.name for p in (self.root / "Games").iterdir()), [NAME])
        # one an interrupted build left: the scan says it can go, and the next build removes it
        tmp = self.root / "Games" / f"Zap (1993)(Acme)[cr FLT].lha{scanner.WHDLOAD_BUILD_TEMP}"
        tmp.write_bytes(b"half")
        problems: list = []
        scanner.collect_files(self.root, True, problems)
        self.assertIn("safe to delete", problems[0][1])
        self.assertEqual(len(self.build(todo)["built"]), 1)
        self.assertFalse(tmp.exists())

    def test_one_of_ours_that_cannot_be_read_is_made_again(self) -> None:
        todo = whdbuild.targets(self.plan(), AMIGA)
        self.build(todo)
        lha = todo[0].dest
        lha.write_bytes(lha.read_bytes()[:200])                                        # (our header, the rest lost)
        self.assertTrue(scanner.is_built_whdload(lha))
        self.assertEqual(len(self.build(todo)["built"]), 1)
        self.assertIsNotNone(whdbuild.marker_of(lha))

    def test_each_game_kept_on_its_own_gets_its_archive_when_titles_are_not_combined(self) -> None:
        other = self.root / "Games" / "Zap (1994)(Other Soft)[cr TRSI].adf"
        other.write_bytes(self.disk.read_bytes())
        plan = self.plan()
        rom = Rom(name=other.name, size=1, crc="", md5="", sha1="", game=other.stem, dat=GAMES)
        op = RenameOp(other, other, "ok")
        plan.owners[2] = (op, MatchedUnit(other, op, rom, frozenset(), "", GAMES, True, False, False))
        plan.selection.decisions[2] = library.Decision(key=2, action=library.KEEP)
        plan.selection.combine_alt = False
        self.assertEqual(len(whdbuild.targets(plan, AMIGA)), 2)
        plan.selection.combine_alt = True
        self.assertEqual(len(whdbuild.targets(plan, AMIGA)), 1)

    def test_a_set_kept_whole_without_a_playlist_is_one_game(self) -> None:
        plan = SimpleNamespace(selection=library.Selection(), owners={})
        for k, n in ((1, 1), (2, 2)):                                                    # (kept for its saves: no ChosenSet)
            name = f"Zap (1993)(Acme)(Disk {n} of 2)[cr FLT].adf"
            path = self.root / "Games" / name
            path.write_bytes(self.disk.read_bytes())
            rom = Rom(name=name, size=1, crc="", md5="", sha1="", game=name[:-4], dat=GAMES)
            op = RenameOp(path, path, "ok")
            plan.owners[k] = (op, MatchedUnit(path, op, rom, frozenset(), "", GAMES, True, False, False))
            plan.selection.decisions[k] = library.Decision(key=k, action=library.KEEP, codes=(library.SAVED_KEEP,))
        todo = whdbuild.targets(plan, AMIGA)
        self.assertEqual([(t.dest.name, [p.name for p, _m in t.disks]) for t in todo],
                         [("Zap (1993)(Acme)[cr FLT].lha", ["Zap (1993)(Acme)(Disk 1 of 2)[cr FLT].adf", "Zap (1993)(Acme)(Disk 2 of 2)[cr FLT].adf"])])

    def test_a_built_game_is_no_unmatched_file_in_the_tree(self) -> None:
        from romorg import treeview
        todo = whdbuild.targets(self.plan(), AMIGA)
        self.build(todo)
        plan = SimpleNamespace(ops=[RenameOp(self.disk, self.disk, "ok")], playlists=[])
        entries = treeview.collect(self.root, plan)
        self.assertEqual({e.parts[-1]: e.state for e in entries}[todo[0].dest.name], treeview.WHDLOAD)


class SpsReleaseTests(unittest.TestCase):
    """An SPS release is built from wherever its images are: loose (as the organiser leaves them), or in a zip / 7z of its own."""

    REL = "Zap (1993)(Acme)[0042]"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def plan(self, paths: list[tuple[Path, str]]) -> SimpleNamespace:
        owners, decisions = {}, {}
        for n, (path, member) in enumerate(paths, 1):
            rom = Rom(name=f"{self.REL} - Zap_Disk{n}.ipf", size=1, crc="", md5="", sha1="", game=self.REL, dat=SPS)
            op = RenameOp(path, path, "ok")
            owners[n] = (op, MatchedUnit(path, op, rom, frozenset(), "", SPS, True, member is not None, False, member))
            decisions[n] = library.Decision(key=n, action=library.KEEP)
        return SimpleNamespace(selection=library.Selection(decisions=decisions), owners=owners)

    def targets(self, plan: SimpleNamespace) -> list:
        with mock.patch.object(whdready, "release", return_value=[whdready.BOOTS, "Zap.lha"]):
            return whdbuild.targets(plan, AMIGA)

    def test_loose_images_are_every_disk_of_the_release(self) -> None:
        paths = []
        for n in (1, 2):
            p = self.tmp / f"{self.REL} - Zap_Disk{n}.ipf"
            p.write_bytes(bytes([n]) * 10)
            paths.append((p, None))
        todo = self.targets(self.plan(paths))
        self.assertEqual((len(todo), todo[0].sps), (1, True))
        self.assertEqual(whdbuild._disk_data(todo[0]), [b"\x01" * 10, b"\x02" * 10])

    def test_a_zip_of_the_release_is_read_whole(self) -> None:
        z = self.tmp / f"{self.REL}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Zap_Disk2.ipf", b"\x02" * 10)
            zf.writestr("Zap_Disk1.ipf", b"\x01" * 10)
            zf.writestr("scan.jpg", b"jpg")
        todo = self.targets(self.plan([(z, "Zap_Disk1.ipf")]))
        self.assertEqual(whdbuild._disk_data(todo[0]), [b"\x01" * 10, b"\x02" * 10])
        self.assertEqual(todo[0].dest, self.tmp / f"{self.REL}.lha")


class WhdloadCheckTests(unittest.TestCase):
    """"Check which versions WHDLoad can install" (``POST /api/whdload/check``)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1", "ROMORG_RETROARCH_DETECT": "0"})
        env.start()
        self.addCleanup(env.stop)
        whdready.clear_cache()
        self.addCleanup(whdready.clear_cache)

    def test_an_xfd_package_that_cannot_be_fetched_leaves_the_versions_unchecked(self) -> None:
        import time
        import urllib.error
        from romorg import whdinstalls
        from romorg.server import App
        names = ["Zap (1993)(Acme)[cr FLT].adf", "Zap (1993)(Acme)[cr SKR].adf"]
        items = []
        for n, name in enumerate(names):
            path = self.tmp / name
            path.write_bytes(make_adf("Zap", {"game": b"the game %d" % n}))
            rom = Rom(name=name, size=1, crc="", md5="", sha1="", game=name[:-4], dat=GAMES)
            items.append(library.Item(key=n, dat=GAMES, rom=rom, style="tosec", path=path, member=None))
        files = {"ZapInstall/Zap.slave": b"\x70\xff\x4e\x75WHDLOADS" + bytes(20),
                 "ZapInstall/Install": b'(copyfiles (source "Zap.slave") (dest "WORK:Zap")) (copyfiles (source "Zap:game") (dest "WORK:Zap"))'
                                       b' (run "xfdDecrunch WORK:Zap/game")'}
        ai.write_lha(self.tmp / "pkg.lha", files)
        installs = [{"archive": "Zap.lha", "title": "Zap", "date": "2024-01-01", "hol": ""}]
        app = App(token=None, auto_update=False)
        state = SimpleNamespace(result=None, root=self.tmp, platform=AMIGA, library_plans={})

        def no_net(*a, **k):
            raise urllib.error.URLError("[Errno -3] Temporary failure in name resolution")
        with mock.patch.object(app, "_state_of", return_value=state), \
                mock.patch.object(app, "_whd_matcher", return_value=({"installs": installs}, whdinstalls.Matcher(installs))), \
                mock.patch.object(app, "_profile", return_value=library.default_profile(AMIGA)), \
                mock.patch("romorg.totals.user_items", return_value=items), mock.patch.object(whdready, "known", return_value={}), \
                mock.patch.object(whdinstalls, "package", return_value=((self.tmp / "pkg.lha").read_bytes(), False)), \
                mock.patch.object(whdinstalls, "xfd_package", side_effect=no_net):
            app.whdload_check({}, {"platform": AMIGA.name})
            deadline = time.time() + 30
            while time.time() < deadline and (app.jobs.current is None or app.jobs.current.status == "running"):
                time.sleep(0.05)
            result = app.jobs.current.result
        self.assertEqual((result["checked"], len(result["failed"])), (0, 2))
        whdready.clear_cache()
        self.assertEqual([whdready.is_ready([n]) for n in names], [None, None])           # (not "does not install": checked again later)



def _wait(app) -> dict:
    """The result of the app's job (it runs in a thread of its own)."""
    import time
    deadline = time.time() + 60
    while time.time() < deadline and (app.jobs.current is None or app.jobs.current.status == "running"):
        time.sleep(0.05)
    return app.jobs.current.result


class MultiDiskSetTests(unittest.TestCase):
    """A kept multi-disk ADF set (``Selection.sets``) is one WHDLoad game: its disks in slot order, named after the set."""

    SET = "Zap (1993)(Acme)[cr FLT]"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "amiga"
        (self.root / "Games").mkdir(parents=True)
        self.names = [f"Zap (1993)(Acme)(Disk {n} of 2)[cr FLT].adf" for n in (1, 2)]
        self.disk1 = self.root / "Games" / self.names[0]
        self.disk1.write_bytes(make_adf("Zap", {"game": b"the game"}))
        self.zip = self.root / "Games" / f"{Path(self.names[1]).stem}.zip"      # (disk 2 zipped on its own)
        with zipfile.ZipFile(self.zip, "w") as zf:
            zf.writestr(self.names[1], make_adf("Zap2", {"more": b"more of it"}))
        self.seen: list = []
        patch = mock.patch.multiple(whdready, level=mock.Mock(side_effect=lambda names, sha="": self.seen.append(list(names)) or whdready.BOOTS),
                                    archive=mock.Mock(return_value="Zap.lha"))
        patch.start()
        self.addCleanup(patch.stop)

    def plan(self) -> SimpleNamespace:
        owners = {}
        for key, (path, member) in ((1, (self.disk1, None)), (2, (self.zip, self.names[1]))):
            name = self.names[key - 1]
            rom = Rom(name=name, size=1, crc="", md5="", sha1="", game=name[:-4], dat=GAMES)
            op = RenameOp(path, path, "ok")
            owners[key] = (op, MatchedUnit(path, op, rom, frozenset(), "", GAMES, True, member is not None, False, member))
        cs = library.ChosenSet(id=1, dat=GAMES, name=self.SET, total=2, slots={2: 2, 1: 1}, labels={}, flags=(), cracked=True)
        sel = library.Selection(decisions={k: library.Decision(key=k, action=library.KEEP) for k in (2, 1)}, sets=[cs])
        return SimpleNamespace(selection=sel, owners=owners)

    def test_the_set_s_disks_in_slot_order_named_after_the_set(self) -> None:
        todo = whdbuild.targets(self.plan(), AMIGA)
        self.assertEqual(len(todo), 1)                                                  # (its disks are no single-disk games too)
        t = todo[0]
        self.assertEqual((t.title, t.names, t.sps), ("Zap", self.names, False))
        self.assertEqual(self.seen, [self.names])                                       # (the shipped list asked with every disk)
        self.assertEqual(t.disks, [(self.disk1, None), (self.zip, self.names[1])])
        self.assertEqual(t.dest, self.root / "Games" / f"{self.SET}.lha")

    def test_a_zipped_disk_is_read_through_the_archive(self) -> None:
        t = whdbuild.targets(self.plan(), AMIGA)[0]
        data = whdbuild._disk_data(t)
        self.assertEqual(data[0], self.disk1.read_bytes())
        with zipfile.ZipFile(self.zip) as zf:
            self.assertEqual(data[1], zf.read(self.names[1]))

    def test_a_set_of_another_dat_is_no_whdload_game(self) -> None:
        plan = self.plan()
        plan.selection.sets[0].dat = "Commodore Amiga - Operating Systems - Workbench"
        self.assertEqual(whdbuild.targets(plan, AMIGA), [])                             # (its disks are not single games either)


class ShippedSpsTargetTests(unittest.TestCase):
    """An SPS release is looked up in the shipped list by its release name: one that boots is built, one that did not install never."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data")})
        env.start()
        self.addCleanup(env.stop)
        whdready.clear_cache()
        self.addCleanup(whdready.clear_cache)

    def targets(self, release: str, files: list[str]) -> list:
        owners, decisions = {}, {}
        for n, f in enumerate(files, 1):
            path = self.tmp / f"{release} - {f}"
            path.write_bytes(bytes([n]) * 10)
            rom = Rom(name=path.name, size=1, crc="", md5="", sha1="", game=release, dat=SPS)
            op = RenameOp(path, path, "ok")
            owners[n] = (op, MatchedUnit(path, op, rom, frozenset(), "", SPS, True, False, False))
            decisions[n] = library.Decision(key=n, action=library.KEEP)
        return whdbuild.targets(SimpleNamespace(selection=library.Selection(decisions=decisions), owners=owners), AMIGA)

    def test_a_release_the_shipped_list_boots(self) -> None:
        rel = "'Allo 'Allo! Cartoon Fun! (1993)(Alternative)[1005]"
        todo = self.targets(rel, ["AlloAlloCartoonFun_Disk1of2.ipf", "AlloAlloCartoonFun_Disk2of2.ipf"])
        self.assertEqual([(t.archive, t.level, t.sps, t.names, t.dest.name) for t in todo],
                         [("AlloAllo.lha", whdready.BOOTS, True, [rel], f"{rel}.lha")])
        self.assertEqual(len(todo[0].disks), 2)

    def test_a_release_whose_install_failed_is_not_built(self) -> None:
        self.assertEqual(self.targets("A320 Airbus Vol. 2 v1.0 (1995)(Games 4 Europe)[1592]", ["A320AirbusVol.2.ipf"]), [])
        self.assertEqual(self.targets("Zap (1993)(Acme)[0042]", ["Zap_Disk1.ipf"]), [])        # (not in the list at all)


class WhdloadBuildEndpointTests(unittest.TestCase):
    """"Build WHDLoad games" (``POST /api/whdload/build``): driven on the App with its scan and the list of installs mocked."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1", "ROMORG_RETROARCH_DETECT": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.tmp / "amiga"
        (self.root / "Games").mkdir(parents=True)
        self.disk = self.root / "Games" / NAME
        self.disk.write_bytes(make_adf("Zap", {"game": b"the game"}))
        self.pkg = install_package(self.tmp)
        self.installs = [{"archive": "Zap.lha", "title": "Zap", "date": "2024-01-01"}]
        patch = mock.patch.multiple(whdready, level=mock.Mock(return_value=whdready.BOOTS), archive=mock.Mock(return_value="Zap.lha"))
        patch.start()
        self.addCleanup(patch.stop)

    def plan(self) -> SimpleNamespace:
        rom = Rom(name=NAME, size=1, crc="", md5="", sha1="", game=NAME[:-4], dat=GAMES)
        op = RenameOp(self.disk, self.disk, "ok")
        unit = MatchedUnit(self.disk, op, rom, frozenset(), "", GAMES, True, False, False)
        sel = library.Selection(decisions={1: library.Decision(key=1, action=library.KEEP)})
        return SimpleNamespace(selection=sel, owners={1: (op, unit)})

    def run_build(self, state, cat, package=None) -> tuple:
        from romorg import whdinstalls
        from romorg.server import App
        app = App(token=None, auto_update=False)
        package = package or (lambda inst, *a, **k: (self.pkg, False))
        with mock.patch.object(app, "_state_of", return_value=state), mock.patch.object(app, "_library_plan", return_value=self.plan()), \
                mock.patch.object(whdinstalls, "load", return_value=cat), mock.patch.object(whdinstalls, "package", side_effect=package):
            out = app.whdload_build({}, {"platform": AMIGA.name})
            return app, out, _wait(app)

    def state(self) -> SimpleNamespace:
        return SimpleNamespace(result=None, root=self.root, platform=AMIGA, library_plans={})

    def test_without_a_scan_or_the_list_of_installs_it_is_refused(self) -> None:
        from romorg.server import ApiError, App
        from romorg import whdinstalls
        app = App(token=None, auto_update=False)
        with mock.patch.object(app, "_state_of", return_value=None), mock.patch.object(whdinstalls, "load", return_value={"installs": []}):
            with self.assertRaises(ApiError) as cm:
                app.whdload_build({}, {"platform": AMIGA.name})
        self.assertEqual((cm.exception.status, cm.exception.error_code), (409, "scan_needed"))
        with mock.patch.object(app, "_state_of", return_value=self.state()), mock.patch.object(whdinstalls, "load", return_value=None):
            with self.assertRaises(ApiError) as cm:
                app.whdload_build({}, {"platform": AMIGA.name})
        self.assertEqual((cm.exception.status, cm.exception.error_code), (409, "whdinstalls_missing"))
        self.assertIsNone(app.jobs.current)                                             # (no job started)

    def test_the_job_counts_what_it_did(self) -> None:
        cat = {"installs": self.installs}
        _app, out, result = self.run_build(self.state(), cat)
        self.assertEqual((out["job"]["kind"], out["job"]["platform"]), ("whdbuild", AMIGA.name))
        self.assertEqual(result, {"action": "whdbuild", "games": 1, "built": 1, "moved": 0, "kept": 0, "removed": 0, "failed": [],
                                  "needs_newer_whdload": []})
        lha = self.root / "Games" / "Zap (1993)(Acme)[cr FLT].lha"
        self.assertIsNotNone(whdbuild.marker_of(lha))
        _app, _out, result = self.run_build(self.state(), cat)                          # (the same again: kept as it is)
        self.assertEqual((result["built"], result["kept"]), (0, 1))
        _app, _out, result = self.run_build(self.state(), {"installs": []})             # (its install gone from the list)
        self.assertEqual((result["built"], len(result["failed"]), result["removed"]), (0, 1, 0))
        self.assertIn("not in the list of installs", result["failed"][0]["error"])
        self.assertTrue(lha.exists())

    def test_a_cancelled_build_removes_nothing(self) -> None:
        import json
        cat = {"installs": self.installs}
        old = self.root / "Games" / "Gone (1990)(Acme).lha"                             # (one of ours whose game is no longer kept)
        ai.write_lha(old, {whdbuild.MARKER: json.dumps({"source": ["Gone (1990)(Acme).adf"], "install": "Gone.lha",
                                                        "install_date": "2024-01-01"}).encode(), "Gone/game": b"gone"})
        holder: dict = {}

        def cancelling(inst, *a, **k):
            holder["app"].jobs.current.cancel.set()                                     # (Cancel pressed while the first game builds)
            return self.pkg, False
        from romorg import whdinstalls
        from romorg.server import App
        app = holder["app"] = App(token=None, auto_update=False)
        with mock.patch.object(app, "_state_of", return_value=self.state()), mock.patch.object(app, "_library_plan", return_value=self.plan()), \
                mock.patch.object(whdinstalls, "load", return_value=cat), mock.patch.object(whdinstalls, "package", side_effect=cancelling):
            app.whdload_build({}, {"platform": AMIGA.name})
            result = _wait(app)
        self.assertEqual((result["built"], result["removed"]), (1, 0))
        self.assertTrue(old.exists())                                                   # (left: a build that was stopped removes nothing)
        _app, _out, result = self.run_build(self.state(), cat)                          # (the next whole build does)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(old.exists())


class WhdGamesTests(unittest.TestCase):
    """The WHDLoad installs page's games (``App._whd_games``): one row a game, its floppies before its SPS release."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1", "ROMORG_RETROARCH_DETECT": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.tmp / "amiga"

    def match(self, rel: str, dat: str, names: list[str], game: str) -> scanner.Match:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        roms = [Rom(name=n, size=1, crc="", md5="", sha1="", game=game, dat=dat) for n in names]
        return scanner.Match(entry=scanner.Entry(path=path, member=None, size=1, crc="", sha1=None), roms=roms)

    def rows(self, matched: list) -> list[dict]:
        from romorg.server import App
        app = App(token=None, auto_update=False)
        state = SimpleNamespace(result=SimpleNamespace(matched=matched), root=self.root, platform=AMIGA, library_plans={})
        with mock.patch.object(app, "_state_of", return_value=state), mock.patch.object(app, "_whd_matcher", return_value=(None, None)), \
                mock.patch("romorg.openretro.database", return_value=None):
            return app._whd_games(AMIGA)[1]

    def test_a_scan_is_needed(self) -> None:
        from romorg.server import ApiError, App
        app = App(token=None, auto_update=False)
        with mock.patch.object(app, "_state_of", return_value=None), self.assertRaises(ApiError) as cm:
            app._whd_games(AMIGA)
        self.assertEqual(cm.exception.error_code, "scan_needed")

    def test_one_row_a_game_its_floppies_before_its_sps_release(self) -> None:
        d1, d2 = "Zap (1993)(Acme)(Disk 1 of 2)[cr FLT].adf", "Zap (1993)(Acme)(Disk 2 of 2)[cr FLT].adf"
        rel = "Zap (1993)(Acme)[0042]"
        matched = [self.match(f"Games/{d1}", GAMES, [d1], d1[:-4]), self.match(f"Games/{d2}", GAMES, [d2], d2[:-4]),
                   self.match(f"SPS/{rel}.zip", SPS, [f"{rel} - Zap_Disk1.ipf", f"{rel} - Zap_Disk2.ipf", f"{rel} - Zap_Disk3.ipf"], rel),
                   self.match("Games/Zap (1993)(Acme)[cr PDX].adf", GAMES, ["Zap (1993)(Acme)[cr PDX].adf"], "Zap (1993)(Acme)[cr PDX]")]
        rows = self.rows(matched)
        self.assertEqual([(r["title"], r["disks"], r["dir"]) for r in rows], [("Zap", 2, "Games")])   # (the floppies, the set with most disks)

    def test_an_sps_release_on_its_own_is_titled_by_its_release(self) -> None:
        rel = "Alien Breed - Tower Assault (1994)(Team17)[0817]"
        matched = [self.match(f"SPS/{rel}.zip", SPS, [f"{rel} - AB_Disk1.ipf", f"{rel} - AB_Disk2.ipf"], rel),
                   self.match("Games/Alien Breed (1991)(Team17).adf", GAMES, ["Alien Breed (1991)(Team17).adf"], "Alien Breed (1991)(Team17)")]
        rows = self.rows(matched)
        self.assertEqual([(r["title"], r["disks"], r["dir"]) for r in rows],
                         [("Alien Breed", 1, "Games"), ("Alien Breed - Tower Assault", 2, "SPS")])

    def test_a_disc_or_a_game_set_aside_gets_no_row(self) -> None:
        cd32 = AMIGA.disc_dats[0]
        matched = [self.match("CD32/Zap (1994)(Acme).cue", cd32, ["Zap (1994)(Acme).cue"], "Zap (1994)(Acme)"),
                   self.match("_excluded/Zap (1993)(Acme)(DE).adf", GAMES, ["Zap (1993)(Acme)(DE).adf"], "Zap (1993)(Acme)(DE)")]
        self.assertEqual(self.rows(matched), [])


class BuiltArchiveScanTests(unittest.TestCase):
    """A WHDLoad game built here, in a whole scan of the folder: no unmatched file, nothing the counts know of."""

    def test_a_built_archive_is_not_scanned(self) -> None:
        import hashlib
        import zlib
        from romorg.datfile import DatFile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "amiga"
        (root / "Games").mkdir(parents=True)
        data = make_adf("Zap", {"game": b"the game"})
        (root / "Games" / NAME).write_bytes(data)
        rom = Rom(name=NAME, size=len(data), crc=f"{zlib.crc32(data) & 0xFFFFFFFF:08x}", md5=hashlib.md5(data).hexdigest(),
                  sha1=hashlib.sha1(data).hexdigest(), game=NAME[:-4], dat=GAMES)
        dat = DatFile(GAMES, "", "", [rom])
        with mock.patch.object(scanner, "find_7z", return_value=None):
            before = scanner.scan(root, [dat], use_cache=False)
        lha = root / "Games" / "Zap (1993)(Acme)[cr FLT].lha"
        ai.write_lha(lha, {whdbuild.MARKER: b'{"source": []}', "Zap/game": b"the game"})
        foreign = root / "Games" / "Other.lha"                                          # (an archive of the user's own: scanned)
        ai.write_lha(foreign, {"Other/game": b"x"})
        self.assertTrue(scanner.is_built_whdload(lha))
        self.assertFalse(scanner.is_built_whdload(foreign))
        with mock.patch.object(scanner, "find_7z", return_value=None):
            res = scanner.scan(root, [dat], use_cache=False)
        seen = [m.entry.path for m in res.matched] + [e.path for e in res.unmatched] + list(res.unsupported) + [p for p, _e in res.errors]
        self.assertNotIn(lha, seen)
        self.assertIn(foreign, seen)
        self.assertEqual([m.entry.path for m in res.matched], [root / "Games" / NAME])
        counts, was = res.summary(), before.summary()
        self.assertEqual({k: counts[k] for k in ("have", "matched_files", "dat_total", "missing")},
                         {k: was[k] for k in ("have", "matched_files", "dat_total", "missing")})
        self.assertEqual(counts["unmatched_files"] + counts["unsupported"] + counts["errors"],
                         was["unmatched_files"] + was["unsupported"] + was["errors"] + 1)   # (only the user's own archive)


if __name__ == "__main__":
    unittest.main()
