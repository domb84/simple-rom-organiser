"""Tests for romorg.sortroot: sorting a mixed folder into system folders, and sweeping what a build set aside."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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


def _other_drive(real_rename=os.rename):
    """``os.rename`` as it behaves when the target is on another drive."""
    def rename(src, dst, *a, **k):
        raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
    return mock.patch.object(sr.os, "rename", rename)


def _set_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)
    if os.name == "nt":
        assert os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY


class MovesNeverDuplicate(World):
    """A move is a rename; only a target on another drive is a copy + delete, and then the file is never left in both places."""

    def move(self, src: Path, dst: Path, kind: str = "file") -> dict:
        return sr.apply_moves([sr.SMove(src, dst, "x", kind=kind)], self.aside.parent / "j", "sort", keep=[self.root])

    def test_a_file_another_program_holds_open_is_not_copied(self) -> None:
        a = self.f("in/a.sfc", b"aaaa")
        dst = self.root / "snes" / "a.sfc"
        with open(a, "rb"):                                # Windows: blocks rename and delete, allows reading
            with mock.patch.object(sr, "RETRY_SLEEP", 0.01):
                res = self.move(a, dst)
        if os.name == "nt":
            self.assertEqual(res["moved"], 0)
            self.assertEqual([f["path"] for f in res["failed"]], [str(a)])
            self.assertIsNone(res["journal"])
            self.assertTrue(a.is_file())
            self.assertFalse(dst.exists(), "the file must not be in two places")
        else:                                              # POSIX: an open file moves like any other
            self.assertEqual((res["moved"], res["failed"]), (1, []))
            self.assertTrue(dst.is_file() and not a.exists())

    def test_a_rename_that_fails_for_another_reason_than_another_drive_is_not_turned_into_a_copy(self) -> None:
        a = self.f("in/a.sfc", b"aaaa")
        dst = self.root / "snes" / "a.sfc"

        def denied(src, dst, *args, **kw):
            raise PermissionError(errno.EACCES, "Permission denied", str(src))

        with mock.patch.object(sr.os, "rename", denied), mock.patch.object(sr.shutil.os, "rename", denied):
            res = self.move(a, dst)
        self.assertEqual(res["moved"], 0)
        self.assertEqual(len(res["failed"]), 1)
        self.assertTrue(a.is_file())
        self.assertFalse(dst.exists())

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions (not root)")
    def test_posix_a_source_folder_without_write_permission_does_not_leave_a_copy(self) -> None:
        a = self.f("in/a.sfc", b"aaaa")
        dst = self.root / "snes" / "a.sfc"
        os.chmod(a.parent, 0o555)
        self.addCleanup(os.chmod, a.parent, 0o755)
        res = self.move(a, dst)
        self.assertEqual((res["moved"], len(res["failed"])), (0, 1))
        self.assertTrue(a.is_file())
        self.assertFalse(dst.exists())

    @unittest.skipUnless(os.name == "nt", "sharing violations are a Windows matter")
    def test_windows_a_file_that_is_held_for_a_moment_is_tried_again(self) -> None:
        a = self.f("in/a.sfc", b"aaaa")
        dst = self.root / "snes" / "a.sfc"
        fh = open(a, "rb")
        timer = threading.Timer(0.12, fh.close)
        timer.start()
        self.addCleanup(timer.join)
        res = self.move(a, dst)
        self.assertEqual((res["moved"], res["failed"]), (1, []))
        self.assertTrue(dst.is_file() and not a.exists())

    @unittest.skipUnless(os.name == "nt", "sharing violations are a Windows matter")
    def test_windows_many_blocked_files_do_not_each_wait(self) -> None:
        files = [self.f(f"in/{i}.sfc") for i in range(6)]
        naps: list = []
        with mock.patch.object(sr, "RETRY_BUDGET", 3), mock.patch.object(sr.time, "sleep", naps.append):
            handles = [open(p, "rb") for p in files]
            try:
                res = sr.apply_moves([sr.SMove(p, self.root / "snes" / p.name, "x") for p in files], self.aside.parent / "j", "sort")
            finally:
                for h in handles:
                    h.close()
        self.assertEqual((res["moved"], len(res["failed"])), (0, 6))
        self.assertEqual(len(naps), 3)

    def test_to_another_drive_it_is_a_copy_and_the_original_goes(self) -> None:
        a = self.f("in/a.sfc", b"aaaa")
        os.utime(a, (1_000_000_000, 1_000_000_000))
        dst = self.aside / "_unmatched" / "a.sfc"
        with _other_drive():
            res = self.move(a, dst)
            self.assertEqual((res["moved"], res["failed"]), (1, []))
            self.assertEqual(dst.read_bytes(), b"aaaa")
            self.assertEqual(int(dst.stat().st_mtime), 1_000_000_000)
            self.assertFalse(a.exists() or a.parent.exists())
            back = sr.undo_moves(Path(res["journal"]), keep=[self.root])
        self.assertEqual((back["restored"], back["skipped"]), (1, []))
        self.assertEqual(a.read_bytes(), b"aaaa")
        self.assertFalse(self.aside.exists())

    def test_to_another_drive_when_the_original_cannot_be_removed_the_copy_is_taken_away(self) -> None:
        a = self.f("in/a.sfc", b"aaaa")
        dst = self.aside / "_unmatched" / "a.sfc"
        real_unlink = os.unlink

        def unlink(path, *args, **kw):
            if Path(path) == a:
                raise PermissionError(errno.EACCES, "in use", str(path))
            return real_unlink(path, *args, **kw)

        with _other_drive(), mock.patch.object(sr.os, "unlink", unlink), mock.patch.object(sr, "RETRY_SLEEP", 0):
            res = self.move(a, dst)
        self.assertEqual(res["moved"], 0)
        self.assertEqual([f["path"] for f in res["failed"]], [str(a)])
        self.assertIsNone(res["journal"])
        self.assertEqual(a.read_bytes(), b"aaaa")
        self.assertFalse(dst.exists(), "the file must not be in two places")

    def test_a_folder_to_another_drive_is_whole_in_one_place_or_the_other(self) -> None:
        game = self.root / "Stuff" / "Crash (USA)"
        for rel in ("Crash (USA).cue", "Crash (USA) (Track 1).bin", "sub/notes.txt"):
            self.f(f"Stuff/Crash (USA)/{rel}", rel.encode())
        dst = self.aside / "psx" / "Crash (USA)"
        before = sorted(p.relative_to(game).as_posix() for p in game.rglob("*") if p.is_file())
        real_unlink = os.unlink

        def unlink(path, *args, **kw):
            if Path(path).name == "notes.txt" and Path(path).parent.parent == game:
                raise PermissionError(errno.EACCES, "in use", str(path))
            return real_unlink(path, *args, **kw)

        with _other_drive(), mock.patch.object(sr.os, "unlink", unlink), mock.patch.object(sr, "RETRY_SLEEP", 0):
            res = self.move(game, dst, "folder")
        self.assertEqual((res["moved"], len(res["failed"])), (0, 1))
        self.assertEqual(sorted(p.relative_to(game).as_posix() for p in game.rglob("*") if p.is_file()), before)
        self.assertFalse(dst.exists(), "no half-copied folder stays behind")
        with _other_drive():                               # and when nothing is in the way, all of it moves, and comes back
            res = self.move(game, dst, "folder")
            self.assertEqual((res["moved"], res["failed"]), (1, []))
            self.assertEqual(sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()), before)
            self.assertFalse(game.exists())
            back = sr.undo_moves(Path(res["journal"]), keep=[self.root])
        self.assertEqual((back["restored"], back["skipped"]), (1, []))
        self.assertEqual(sorted(p.relative_to(game).as_posix() for p in game.rglob("*") if p.is_file()), before)
        self.assertFalse(dst.exists())

    def test_read_only_files_move_also_to_another_drive_and_stay_read_only(self) -> None:
        a = self.f("in/a.sfc", b"aaaa")
        b = self.f("in/b.sfc", b"bbbb")
        _set_read_only(a)
        _set_read_only(b)
        d1, d2 = self.root / "snes" / "a.sfc", self.aside / "_unmatched" / "b.sfc"
        self.addCleanup(lambda: [os.chmod(p, stat.S_IWRITE | stat.S_IREAD) for p in (a, b, d1, d2) if p.exists()])
        res = self.move(a, d1)
        self.assertEqual((res["moved"], res["failed"]), (1, []))
        with _other_drive():
            res2 = self.move(b, d2)
            self.assertEqual((res2["moved"], res2["failed"]), (1, []))
            self.assertFalse(a.exists() or b.exists() or a.parent.exists())
            self.assertFalse(os.access(d2, os.W_OK) and os.name == "nt")
            self.assertEqual(sr.undo_moves(Path(res2["journal"]), keep=[self.root])["skipped"], [])
        self.assertEqual(sr.undo_moves(Path(res["journal"]), keep=[self.root])["skipped"], [])
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b"aaaa", b"bbbb"))
        self.assertFalse(d1.exists() or d2.exists())

    @unittest.skipUnless(os.name == "nt", "the read-only attribute of a folder is a Windows matter")
    def test_windows_an_emptied_read_only_folder_is_removed_and_a_full_one_keeps_its_attribute(self) -> None:
        a = self.f("in/deep/a.sfc")
        self.f("full/keep.txt")
        for d in (a.parent, a.parent.parent, self.root / "full"):
            _set_read_only(d)
        self.addCleanup(lambda: [os.chmod(d, stat.S_IWRITE | stat.S_IREAD) for d in (self.root / "full", self.root / "in") if d.exists()])
        res = self.move(a, self.root / "snes" / "a.sfc")
        self.assertEqual(res["moved"], 1)
        self.assertFalse((self.root / "in").exists())
        (self.root / "empty").mkdir()
        _set_read_only(self.root / "empty")
        sr.remove_empty_tree(self.root, keep=[self.root / "snes"])
        self.assertFalse((self.root / "empty").exists())
        self.assertTrue((self.root / "full" / "keep.txt").is_file())
        self.assertTrue(os.stat(self.root / "full").st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)

    def test_a_folder_in_use_stays_and_nothing_else_fails(self) -> None:
        a = self.f("busy/a.sfc")
        b = self.f("free/b.sfc")
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdout.write('up'); sys.stdout.flush(); sys.stdin.read()"],
                                cwd=str(a.parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.read(2), b"up")           # it runs, with busy/ as its current folder
            res = sr.apply_moves([sr.SMove(a, self.root / "snes" / "a.sfc", "x"), sr.SMove(b, self.root / "snes" / "b.sfc", "x")],
                                 self.aside.parent / "j", "sort", keep=[self.root])
            self.assertEqual((res["moved"], res["failed"]), (2, []))
            sr.remove_empty_tree(self.root, keep=[self.root / "snes"])
            self.assertFalse((self.root / "free").exists())
            if os.name == "nt":
                self.assertTrue((self.root / "busy").is_dir())     # Windows cannot remove another program's current folder
        finally:
            proc.communicate(b"")
            proc.stdout.close()


class Journal(World):
    def moves(self, n: int = 5) -> list:
        return [sr.SMove(self.f(f"in/{i}.sfc", str(i).encode()), self.root / "snes" / f"{i}.sfc", "x") for i in range(n)]

    def test_the_journal_is_on_disk_before_a_file_moves(self) -> None:
        moves = self.moves(3)
        seen: list = []
        real = sr.move_path

        def spy(src, dst):
            j = sr.read_journal(seen[0])
            self.assertTrue(j["partial"])
            self.assertIn(str(src), [m["from"] for m in j["moves"]])       # written down first
            return real(src, dst)

        with mock.patch.object(sr, "move_path", spy):
            res = sr.apply_moves(moves, self.aside.parent / "j", "sort", keep=[self.root], started=seen.append)
        self.assertEqual([str(p) for p in seen], [res["journal"]])
        done = json.loads(Path(res["journal"]).read_text(encoding="utf-8"))  # finished: the one JSON document it always was
        self.assertEqual((done["kind"], done["undone"], len(done["moves"])), ("sort", False, 3))
        self.assertNotIn("partial", done)
        self.assertEqual(set(done["moves"][0]), {"from", "to", "kind"})

    def test_undo_removes_the_folders_it_made_and_not_the_one_the_archive_lies_in(self) -> None:
        a = self.f("in/a.txt")
        outer = self.aside.parent / "My archives"                  # the user's own folder, empty but for our archive
        outer.mkdir()
        res = sr.apply_moves([sr.SMove(a, outer / "arch" / "_other" / "in" / "a.txt", "_other")], self.aside.parent / "j", "sort",
                             keep=[self.root])
        self.assertEqual(res["moved"], 1)
        self.assertEqual(sr.undo_moves(Path(res["journal"]))["skipped"], [])
        self.assertTrue(a.is_file())
        self.assertFalse((outer / "arch").exists())                # made by the move: gone again
        self.assertTrue(outer.is_dir(), "the folder above the archive is the user's")

    def test_a_failed_move_leaves_no_empty_folder_behind(self) -> None:
        gone = sr.SMove(self.f("in/a.txt"), self.aside / "_other" / "deep" / "a.txt", "_other")
        with mock.patch.object(sr, "move_path", side_effect=PermissionError(13, "no")):
            res = sr.apply_moves([gone], self.aside.parent / "j", "sort", keep=[self.root])
        self.assertEqual((res["moved"], len(res["failed"])), (0, 1))
        self.assertFalse(self.aside.exists())

    def test_nothing_moved_leaves_no_journal(self) -> None:
        gone = sr.SMove(self.root / "nope.sfc", self.root / "snes" / "nope.sfc", "x")
        res = sr.apply_moves([gone], self.aside.parent / "j", "sort")
        self.assertEqual((res["moved"], len(res["failed"]), res["journal"]), (0, 1, None))
        self.assertFalse(list((self.aside.parent / "j").glob("*")) if (self.aside.parent / "j").exists() else [])

    def test_a_run_that_is_killed_can_still_be_undone(self) -> None:
        moves = self.moves(5)
        jdir = self.aside.parent / "j"
        code = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "from romorg import sortroot as sr\n"
            "root, jdir = Path(sys.argv[1]), Path(sys.argv[2])\n"
            "real, n = sr.move_path, [0]\n"
            "def dying(src, dst):\n"
            "    n[0] += 1\n"
            "    if n[0] == 4:\n"
            "        os._exit(9)\n"                                  # killed: no clean-up of any kind runs
            "    real(src, dst)\n"
            "sr.move_path = dying\n"
            "sr.apply_moves([sr.SMove(root / 'in' / f'{i}.sfc', root / 'snes' / f'{i}.sfc', 'x') for i in range(5)], jdir, 'sort')\n"
        )
        repo = str(Path(sr.__file__).resolve().parent.parent)
        proc = subprocess.run([sys.executable, "-c", code, str(self.root), str(jdir)], cwd=repo, capture_output=True)
        self.assertEqual(proc.returncode, 9, proc.stderr)
        self.assertEqual(sorted(p.name for p in (self.root / "snes").iterdir()), ["0.sfc", "1.sfc", "2.sfc"])
        journals = list(jdir.glob("sort-*.json"))
        self.assertEqual(len(journals), 1, "the moves that were made are on record")
        j = sr.read_journal(journals[0])
        self.assertTrue(j["partial"])
        self.assertEqual([Path(m["from"]).name for m in j["moves"]], ["0.sfc", "1.sfc", "2.sfc", "3.sfc"])
        back = sr.undo_moves(journals[0], keep=[self.root])
        self.assertEqual((back["restored"], back["skipped"]), (3, []))           # the fourth was written down but never moved
        self.assertEqual(sorted(p.name for p in (self.root / "in").iterdir()), [f"{i}.sfc" for i in range(5)])
        self.assertFalse((self.root / "snes").exists())
        self.assertTrue(json.loads(journals[0].read_text(encoding="utf-8"))["undone"])
        del moves

    def test_a_torn_last_line_is_ignored(self) -> None:
        a = self.f("in/a.sfc")
        jdir = self.aside.parent / "j"
        jdir.mkdir()
        j = jdir / "sort-x.json"
        (self.root / "snes").mkdir()
        os.rename(a, self.root / "snes" / "a.sfc")
        j.write_text(json.dumps({"kind": "sort", "undone": False, "partial": True}) + "\n"
                     + json.dumps({"from": str(a), "to": str(self.root / "snes" / "a.sfc"), "kind": "file"}) + "\n"
                     + '{"from": "' + str(self.root / "in").replace("\\", "\\\\"), encoding="utf-8")
        back = sr.undo_moves(j, keep=[self.root])
        self.assertEqual((back["restored"], back["skipped"]), (1, []))
        self.assertTrue(a.is_file())


if __name__ == "__main__":
    unittest.main()
