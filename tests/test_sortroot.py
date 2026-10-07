"""Tests for romorg.sortroot: sorting a mixed folder into system folders, and sweeping what a build set aside."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from romorg import sortroot as sr


class World(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.root, self.aside = base / "roms", base / "roms-aside"
        self.root.mkdir()
        self.folders = {"Nintendo Game Boy Advance": self.root / "gba", "Super Nintendo Entertainment System": self.root / "snes"}

    def f(self, rel: str, data: bytes = b"x") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


class Sort(World):
    def test_files_go_to_their_system_and_the_rest_goes_aside(self) -> None:
        loose = self.f("Random/mario.sfc")
        misplaced = self.f("gba/zelda.sfc")                    # a SNES ROM in the GBA folder
        right = self.f("gba/ok.gba")
        mystery = self.f("Random/mystery.gba")
        note = self.f("Random/readme.txt")
        save = self.f("Random/mario.srm")
        moves = sr.plan_sort(self.root, self.aside, self.folders,
                             {loose: "Super Nintendo Entertainment System", misplaced: "Super Nintendo Entertainment System",
                              right: "Nintendo Game Boy Advance"}, [], [mystery], [note, save])
        by = {m.src.name: m for m in moves}
        self.assertEqual(by["mario.sfc"].dst, self.root / "snes" / "mario.sfc")
        self.assertEqual(by["zelda.sfc"].dst, self.root / "snes" / "zelda.sfc")
        self.assertNotIn("ok.gba", by)                                         # already home
        self.assertEqual(by["mystery.gba"].dst, self.aside / "_unmatched" / "Random" / "mystery.gba")
        self.assertEqual(by["readme.txt"].dst, self.aside / "_other" / "Random" / "readme.txt")
        self.assertEqual(by["mario.srm"].dst, self.root / "snes" / "mario.srm")   # a save travels with its ROM
        self.assertEqual(by["mario.srm"].bucket, "sidecar")

    def test_unmatched_inside_a_system_folder_is_left_for_its_library_build(self) -> None:
        inside = self.f("gba/unknown.gba")
        moves = sr.plan_sort(self.root, self.aside, self.folders, {}, [], [inside], [])
        self.assertEqual(moves, [])

    def test_names_never_collide(self) -> None:
        a, b = self.f("one/Game.sfc"), self.f("two/Game.sfc")
        self.f("snes/Game.sfc")
        moves = sr.plan_sort(self.root, self.aside, self.folders,
                             {a: "Super Nintendo Entertainment System", b: "Super Nintendo Entertainment System"}, [], [], [])
        self.assertEqual(sorted(m.dst.name for m in moves), ["Game (2).sfc", "Game (3).sfc"])

    def test_disc_games_move_as_a_folder_and_their_files_are_not_other(self) -> None:
        game = self.root / "Stuff" / "Crash (USA)"
        (game).mkdir(parents=True)
        (game / "Crash (USA).chd").write_bytes(b"c")
        (game / "Crash (USA).srm").write_bytes(b"s")
        folders = {**self.folders, "Sony PlayStation": self.root / "psx"}
        moves = sr.plan_sort(self.root, self.aside, folders, {},
                             [{"platform": "Sony PlayStation", "top": game, "folder": True, "files": [game / "Crash (USA).chd"]}],
                             [], [game / "Crash (USA).srm"])
        self.assertEqual([(m.src, m.dst, m.kind) for m in moves], [(game, self.root / "psx" / "Crash (USA)", "folder")])

    def test_apply_undo_and_empty_folders_go(self) -> None:
        a = self.f("Random/mario.sfc", b"mario")
        t = self.f("Random/readme.txt", b"hi")
        moves = sr.plan_sort(self.root, self.aside, self.folders, {a: "Super Nintendo Entertainment System"}, [], [], [t])
        res = sr.apply_moves(moves, self.aside.parent / "j", "sort", keep=[self.root])
        self.assertEqual((res["moved"], res["failed"]), (2, []))
        self.assertEqual((self.root / "snes" / "mario.sfc").read_bytes(), b"mario")
        self.assertTrue((self.aside / "_other" / "Random" / "readme.txt").is_file())
        self.assertFalse((self.root / "Random").exists())                       # emptied: removed
        out = sr.undo_moves(Path(res["journal"]), keep=[self.root])
        self.assertEqual((out["restored"], out["skipped"]), (2, []))
        self.assertEqual((self.root / "Random" / "mario.sfc").read_bytes(), b"mario")
        self.assertFalse((self.root / "snes").exists())

    def test_nothing_is_overwritten_when_the_target_appeared(self) -> None:
        a = self.f("x/g.sfc", b"new")
        moves = sr.plan_sort(self.root, self.aside, self.folders, {a: "Super Nintendo Entertainment System"}, [], [], [])
        self.f("snes/g.sfc", b"mine")                                            # appeared after the preview
        res = sr.apply_moves(moves, self.aside.parent / "j", "sort", keep=[self.root])
        self.assertEqual(res["moved"], 0)
        self.assertEqual((self.root / "snes" / "g.sfc").read_bytes(), b"mine")
        self.assertEqual(a.read_bytes(), b"new")


class Sweep(World):
    def test_set_aside_folders_leave_the_rom_folder_and_come_back(self) -> None:
        keep = self.f("gba/keep.gba")
        ex = self.f("gba/_excluded/beta.gba")
        sup = self.f("snes/_superseded/Sub/old.sfc")
        orig = self.f("gba/_converted_originals/raw/x.bin")              # the user's safety copies stay
        moves = sr.plan_sweep(self.folders, self.aside)
        self.assertEqual(sorted(m.src.name for m in moves), ["beta.gba", "old.sfc"])
        res = sr.apply_moves(moves, self.aside.parent / "j", "sweep", keep=[self.root, *self.folders.values()])
        self.assertEqual(res["moved"], 2)
        self.assertTrue((self.aside / "gba" / "_excluded" / "beta.gba").is_file())
        self.assertTrue((self.aside / "snes" / "_superseded" / "Sub" / "old.sfc").is_file())
        self.assertTrue(keep.is_file() and orig.is_file())
        self.assertFalse((self.root / "gba" / "_excluded").exists())
        back = sr.plan_restore(self.folders, self.aside)
        self.assertEqual(sorted(m.dst for m in back), sorted([ex, sup]))
        sr.apply_moves(back, self.aside.parent / "j", "restore", keep=[self.aside])
        self.assertTrue(ex.is_file() and sup.is_file())


if __name__ == "__main__":
    unittest.main()
