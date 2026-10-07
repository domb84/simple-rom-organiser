"""Tests for romorg.libexport: build the library in another folder, leave the source alone, undo."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from romorg import libexport


def op(src: Path, dst: Path, status: str = "move", **kw):
    fields = dict(kind="move", code="", unmatched=False, folder="")
    fields.update(kw)
    return NS(src=src, dst=dst, status=status, **fields)


class Export(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ROMORG_TEST_TMP"))
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root, self.dest = base / "src", base / "lib"
        self.root.mkdir()
        (self.root / "a.gba").write_bytes(b"A" * 1000)
        (self.root / "b.gba").write_bytes(b"B" * 500)
        (self.root / "c.gba").write_bytes(b"C" * 10)
        self.plan = NS(ops=[
            op(self.root / "a.gba", self.root / "Game A.gba"),
            op(self.root / "b.gba", self.root / "b.gba", "ok"),
            op(self.root / "c.gba", self.root / "_excluded" / "c.gba", code="excluded", folder="_excluded"),
        ], playlists=[])

    def test_only_kept_files_are_built_source_untouched(self) -> None:
        ep = libexport.plan_export(self.plan, self.root, self.dest, "copy")
        self.assertEqual(sorted(t.rel for t in ep.items), ["Game A.gba", "b.gba"])
        res = libexport.apply_export(ep)
        self.assertEqual(res["copied"], 2)
        self.assertEqual((self.dest / "Game A.gba").read_bytes(), b"A" * 1000)
        self.assertFalse((self.dest / "_excluded").exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.gba", "b.gba", "c.gba"])
        self.assertFalse(list(self.dest.rglob("*.part")))

    def test_rerun_adds_nothing_and_undo_removes_only_ours(self) -> None:
        libexport.apply_export(libexport.plan_export(self.plan, self.root, self.dest, "copy"))
        again = libexport.plan_export(self.plan, self.root, self.dest, "copy")
        self.assertEqual(again.pending(), 0)
        self.assertEqual(len(libexport.list_runs(self.dest)), 1)
        (self.dest / "mine.txt").write_text("keep")
        res = libexport.undo_run(self.dest)
        self.assertEqual(res["removed"], 2)
        self.assertTrue((self.dest / "mine.txt").exists())
        self.assertFalse((self.dest / "b.gba").exists())
        self.assertEqual((self.root / "a.gba").read_bytes(), b"A" * 1000)   # a copy: the source keeps its file
        self.assertEqual(libexport.list_runs(self.dest), [])

    def test_foreign_file_is_never_overwritten(self) -> None:
        self.dest.mkdir()
        (self.dest / "b.gba").write_bytes(b"other")
        ep = libexport.plan_export(self.plan, self.root, self.dest, "copy")
        self.assertEqual(ep.counts()["conflict"], 1)
        libexport.apply_export(ep)
        self.assertEqual((self.dest / "b.gba").read_bytes(), b"other")

    def test_undo_leaves_a_changed_file(self) -> None:
        libexport.apply_export(libexport.plan_export(self.plan, self.root, self.dest, "copy"))
        (self.dest / "b.gba").write_bytes(b"edited by the user")
        res = libexport.undo_run(self.dest)
        self.assertEqual(len(res["skipped"]), 1)
        self.assertTrue((self.dest / "b.gba").exists())

    def test_overlapping_folders_are_refused(self) -> None:
        for dest in (self.root, self.root / "out", self.root.parent):
            with self.assertRaises(libexport.ExportError):
                libexport.plan_export(self.plan, self.root, dest)

    def test_old_link_modes_are_a_copy(self) -> None:
        for old in ("auto", "hardlink", "symlink"):
            ep = libexport.plan_export(self.plan, self.root, self.dest, old)
            self.assertEqual(ep.mode, "copy")
            self.assertEqual({t.action for t in ep.items}, {"copy"})

    def test_move_takes_the_files_out_of_the_source_and_undo_puts_them_back(self) -> None:
        ep = libexport.plan_export(self.plan, self.root, self.dest, "move")
        self.assertEqual(ep.counts()["move"], 2)
        self.assertEqual(ep.bytes_to_copy(), 0)                                # same drive: a rename writes nothing
        res = libexport.apply_export(ep)
        self.assertEqual((res["moved"], res["copied"], res["failed"]), (2, 0, []))
        self.assertEqual((self.dest / "Game A.gba").read_bytes(), b"A" * 1000)
        self.assertFalse((self.root / "a.gba").exists())
        self.assertTrue((self.root / "c.gba").exists())                          # not kept: never touched
        out = libexport.undo_run(self.dest)
        self.assertEqual((out["removed"], out["skipped"]), (2, []))
        self.assertEqual((self.root / "a.gba").read_bytes(), b"A" * 1000)
        self.assertFalse((self.dest / "Game A.gba").exists())

    def test_undo_of_a_move_never_overwrites_the_original_place(self) -> None:
        libexport.apply_export(libexport.plan_export(self.plan, self.root, self.dest, "move"))
        (self.root / "a.gba").write_bytes(b"a new file")
        out = libexport.undo_run(self.dest)
        self.assertEqual(len(out["skipped"]), 1)
        self.assertEqual((self.root / "a.gba").read_bytes(), b"a new file")
        self.assertTrue((self.dest / "Game A.gba").exists())

    def test_move_and_sync_do_not_go_together(self) -> None:
        with self.assertRaises(libexport.ExportError):
            libexport.plan_export(self.plan, self.root, self.dest, "move", sync=True)

    def test_playlist_is_written_in_dest(self) -> None:
        pl = NS(path=self.root / "Disc.m3u", lines=["# Generated by simple-rom-organiser", "Game A.gba"], status="write")
        self.plan.playlists = [pl]
        libexport.apply_export(libexport.plan_export(self.plan, self.root, self.dest, "copy"))
        self.assertIn("Game A.gba", (self.dest / "Disc.m3u").read_text())
        self.assertFalse((self.root / "Disc.m3u").exists())

    def test_disc_unit_travels_as_chd_without_sidecars(self) -> None:
        d = self.root / "Game"
        d.mkdir()
        chd, sav = d / "g.chd", d / "g.sav"
        chd.write_bytes(b"C" * 20)
        sav.write_bytes(b"S")
        unit = NS(kind="chd", path=chd, files=[chd, sav])
        o = op(d, self.root / "Sorted", "move", moves=[(chd, self.root / "Sorted" / "g.chd"),
                                                        (sav, self.root / "Sorted" / "g.sav")], unit=unit)
        ep = libexport.plan_export(NS(ops=[o], playlists=[]), self.root, self.dest, "copy")
        self.assertEqual([t.rel for t in ep.items], ["Sorted/g.chd"])
        ep = libexport.plan_export(NS(ops=[o], playlists=[]), self.root, self.dest, "copy", sidecars=True)
        self.assertEqual(sorted(t.rel for t in ep.items), ["Sorted/g.chd", "Sorted/g.sav"])


class Sync(Export):
    def build(self, mode: str = "copy"):
        return libexport.apply_export(libexport.plan_export(self.plan, self.root, self.dest, mode, sync=True))

    def test_rule_change_removes_what_is_no_longer_kept_and_undo_restores_it(self) -> None:
        self.build()
        self.plan.ops = [self.plan.ops[0]]                       # b.gba is no longer kept
        ep = libexport.plan_export(self.plan, self.root, self.dest, "copy", sync=True)
        self.assertEqual([r.rel for r in ep.to_remove()], ["b.gba"])
        res = libexport.apply_export(ep)
        self.assertEqual(res["removed"], 1)
        self.assertFalse((self.dest / "b.gba").exists())
        self.assertTrue((self.root / "b.gba").exists())          # the source never changes
        self.assertTrue((self.dest / "Game A.gba").exists())
        undone = libexport.undo_run(self.dest, res["run"])
        self.assertEqual(undone["restored"], 1)
        self.assertEqual((self.dest / "b.gba").read_bytes(), b"B" * 500)

    def test_without_sync_nothing_is_removed(self) -> None:
        self.build()
        self.plan.ops = [self.plan.ops[0]]
        ep = libexport.plan_export(self.plan, self.root, self.dest, "copy")
        self.assertEqual(ep.to_remove(), [])
        libexport.apply_export(ep)
        self.assertTrue((self.dest / "b.gba").exists())

    def test_a_file_the_user_changed_is_kept_and_reported(self) -> None:
        self.build()
        (self.dest / "b.gba").write_bytes(b"my edit")
        self.plan.ops = [self.plan.ops[0]]
        ep = libexport.plan_export(self.plan, self.root, self.dest, "copy", sync=True)
        self.assertEqual(ep.to_remove(), [])
        self.assertEqual(ep.counts()["kept_edited"], 1)
        libexport.apply_export(ep)
        self.assertEqual((self.dest / "b.gba").read_bytes(), b"my edit")

    def test_a_changed_source_replaces_the_copy_only_if_the_copy_is_untouched(self) -> None:
        self.build()
        (self.root / "a.gba").write_bytes(b"NEW" * 300)
        ep = libexport.plan_export(self.plan, self.root, self.dest, "copy", sync=True)
        self.assertEqual(ep.counts()["replace"], 1)
        res = libexport.apply_export(ep)
        self.assertEqual(res["replaced"], 1)
        self.assertEqual((self.dest / "Game A.gba").read_bytes(), b"NEW" * 300)
        (self.dest / "Game A.gba").write_bytes(b"edit")
        (self.root / "a.gba").write_bytes(b"NEWER" * 300)
        ep = libexport.plan_export(self.plan, self.root, self.dest, "copy", sync=True)
        self.assertEqual(ep.counts()["conflict"], 1)

    def test_a_changed_source_replaces_the_copy_again_even_when_the_editor_wrote_a_new_file(self) -> None:
        self.build("copy")
        os.unlink(self.root / "a.gba")                           # an editor that writes a new file
        (self.root / "a.gba").write_bytes(b"NEW" * 300)
        ep = libexport.plan_export(self.plan, self.root, self.dest, "copy", sync=True)
        self.assertEqual(ep.counts()["replace"], 1)
        libexport.apply_export(ep)
        self.assertEqual((self.dest / "Game A.gba").read_bytes(), b"NEW" * 300)

    def test_an_empty_or_unmounted_source_removes_nothing(self) -> None:
        self.build()
        self.plan.ops = []
        with self.assertRaises(libexport.ExportError):
            libexport.plan_export(self.plan, self.root, self.dest, "copy", sync=True)
        self.assertTrue((self.dest / "b.gba").exists())

    def test_removing_most_of_the_library_needs_a_say_so(self) -> None:
        ops = []
        for i in range(40):
            f = self.root / f"g{i}.gba"
            f.write_bytes(bytes([i]) * 10)
            ops.append(op(f, f, "ok"))
        self.plan.ops = ops
        self.build()
        self.plan.ops = ops[:5]
        ep = libexport.plan_export(self.plan, self.root, self.dest, "copy", sync=True)
        self.assertTrue(ep.mass_removal())
        with self.assertRaises(libexport.ExportError):
            libexport.apply_export(ep)
        self.assertEqual(len(list(self.dest.glob("g*.gba"))), 40)
        self.assertEqual(libexport.apply_export(ep, allow_mass=True)["removed"], 35)

    def test_empty_folders_are_tidied_after_a_removal(self) -> None:
        sub = self.root / "Sub"
        sub.mkdir()
        (sub / "x.gba").write_bytes(b"X")
        self.plan.ops.append(op(sub / "x.gba", sub / "x.gba", "ok"))
        self.build()
        self.assertTrue((self.dest / "Sub" / "x.gba").exists())
        self.plan.ops.pop()
        libexport.apply_export(libexport.plan_export(self.plan, self.root, self.dest, "copy", sync=True))
        self.assertFalse((self.dest / "Sub").exists())
        self.assertTrue((self.dest / libexport.MANIFEST_DIR).exists())


if __name__ == "__main__":
    unittest.main()
