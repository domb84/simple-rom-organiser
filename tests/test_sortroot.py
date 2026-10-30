"""Tests for romorg.sortroot: sweeping what a build set aside, moves that never duplicate a file."""

from __future__ import annotations

import os as _os
import tempfile as _tempfile
_os.environ.setdefault("ROMORG_DATA_DIR", _tempfile.mkdtemp(prefix="romorg-test-data-"))   # (undo logs live in the data folder: never the real one)

import errno
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


class Sweep(World):
    def test_set_aside_folders_leave_the_rom_folder(self) -> None:
        keep = self.f("gba/keep.gba")
        ex = self.f("gba/_excluded/beta.gba")
        sup = self.f("snes/_superseded/Sub/old.sfc")
        orig = self.f("gba/_converted_originals/raw/x.bin")              # the user's safety copies stay
        moves = sr.plan_sweep(self.folders, self.aside)
        self.assertEqual(sorted(m.src.name for m in moves), ["beta.gba", "old.sfc"])
        res = sr.apply_moves(moves, keep=[self.root, *self.folders.values()])
        self.assertEqual(res["moved"], 2)
        self.assertTrue((self.aside / "gba" / "Excluded" / "beta.gba").is_file())
        self.assertTrue((self.aside / "snes" / "Superseded" / "Sub" / "old.sfc").is_file())
        self.assertTrue(keep.is_file() and orig.is_file())
        self.assertFalse((self.root / "gba" / "_excluded").exists())
        self.assertFalse(ex.exists() or sup.exists())


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
        return sr.apply_moves([sr.SMove(src, dst, "x", kind=kind)], keep=[self.root])

    def test_a_file_another_program_holds_open_is_not_copied(self) -> None:
        a = self.f("in/a.sfc", b"aaaa")
        dst = self.root / "snes" / "a.sfc"
        with open(a, "rb"):                                # Windows: blocks rename and delete, allows reading
            with mock.patch.object(sr, "RETRY_SLEEP", 0.01):
                res = self.move(a, dst)
        if os.name == "nt":
            self.assertEqual(res["moved"], 0)
            self.assertEqual([f["path"] for f in res["failed"]], [str(a)])
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
                res = sr.apply_moves([sr.SMove(p, self.root / "snes" / p.name, "x") for p in files])
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
        with _other_drive():                               # and when nothing is in the way, all of it moves
            res = self.move(game, dst, "folder")
            self.assertEqual((res["moved"], res["failed"]), (1, []))
            self.assertEqual(sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()), before)
            self.assertFalse(game.exists())

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
        self.assertEqual((d1.read_bytes(), d2.read_bytes()), (b"aaaa", b"bbbb"))
        self.assertFalse(os.access(d1, os.W_OK))

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
                                 keep=[self.root])
            self.assertEqual((res["moved"], res["failed"]), (2, []))
            self.assertFalse((self.root / "free").exists())
            if os.name == "nt":
                self.assertTrue((self.root / "busy").is_dir())     # Windows cannot remove another program's current folder
        finally:
            proc.communicate(b"")
            proc.stdout.close()


class Folders(World):
    def test_a_failed_move_leaves_no_empty_folder_behind(self) -> None:
        gone = sr.SMove(self.f("in/a.txt"), self.aside / "_other" / "deep" / "a.txt", "_other")
        with mock.patch.object(sr, "move_path", side_effect=PermissionError(13, "no")):
            res = sr.apply_moves([gone], keep=[self.root])
        self.assertEqual((res["moved"], len(res["failed"])), (0, 1))
        self.assertFalse(self.aside.exists())

    def test_the_folder_the_archive_lies_in_is_never_removed(self) -> None:
        a = self.f("in/a.txt")
        outer = self.aside.parent / "My archives"                  # the user's own folder, empty but for our archive
        outer.mkdir()
        res = sr.apply_moves([sr.SMove(a, outer / "arch" / "_other" / "in" / "a.txt", "_other")], keep=[self.root])
        self.assertEqual(res["moved"], 1)
        self.assertTrue((outer / "arch" / "_other" / "in" / "a.txt").is_file())
        self.assertFalse((self.root / "in").exists())              # emptied by the move: gone


if __name__ == "__main__":
    unittest.main()
