"""WHDLoad games built from the user's disks (``romorg.whdbuild``): which version, where, the archive and what the next build does."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from romorg import amigainstall as ai, library, platforms, scanner, whdbuild, whdready
from romorg.datfile import Rom
from romorg.organiser import MatchedUnit, RenameOp
from tests.test_amigainstall import make_adf

AMIGA = platforms.get_platform("Commodore Amiga")
GAMES = "Commodore Amiga - Games - [ADF]"
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


if __name__ == "__main__":
    unittest.main()
