"""The helpers that several platforms share (found by the duplicate-code pass of v0.4): cancellation, atomic writes, the kept
catalogue files, the native-library search, the emulator-folder readers, and the conversion placement."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from romorg import cancellation, cdimage, emudata, kept, nativelib, organiser, paths  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)


class Cancellation(unittest.TestCase):
    def test_an_event_a_function_and_nothing(self) -> None:
        ev = threading.Event()
        self.assertFalse(cancellation.is_cancelled(None))
        self.assertFalse(cancellation.is_cancelled(ev))
        self.assertFalse(cancellation.is_cancelled(lambda: False))
        ev.set()
        self.assertTrue(cancellation.is_cancelled(ev))
        self.assertTrue(cancellation.is_cancelled(lambda: True))

    def test_check_raises_only_when_cancelled(self) -> None:
        cancellation.check(None)
        cancellation.check(threading.Event())
        ev = threading.Event()
        ev.set()
        with self.assertRaises(InterruptedError):
            cancellation.check(ev)

    def test_the_old_names_are_the_same_function(self) -> None:
        from romorg import scanner, switchverify
        self.assertIs(organiser._is_cancelled, cancellation.is_cancelled)
        self.assertIs(scanner._is_cancelled, cancellation.is_cancelled)
        self.assertIs(switchverify._is_cancelled, cancellation.is_cancelled)


class AtomicWrite(Tmp):
    def test_text_and_bytes_replace_the_file_and_leave_nothing_behind(self) -> None:
        target = self.root / "a.json"
        paths.atomic_write(target, "first é", ".t-")
        self.assertEqual(target.read_bytes(), "first é".encode("utf-8"))
        paths.atomic_write(target, b"second", ".t-", fsync=True)
        self.assertEqual(target.read_bytes(), b"second")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.json"])

    def test_a_failure_keeps_the_old_file_and_removes_the_temporary_one(self) -> None:
        target = self.root / "a.json"
        target.write_bytes(b"old")
        with mock.patch("romorg.paths.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                paths.atomic_write(target, b"new", ".t-")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.json"])


class Kept(Tmp):
    def test_fetch_uses_the_opener_and_closes_the_answer(self) -> None:
        seen = {}

        class Resp(io.BytesIO):
            def close(self) -> None:
                seen["closed"] = True
                super().close()
        out = kept.fetch("https://example.test/x", opener=lambda req: (seen.setdefault("ua", req.get_header("User-agent")), Resp(b"data"))[1])
        self.assertEqual((out, seen), (b"data", {"ua": kept.USER_AGENT, "closed": True}))

    def test_age_gate(self) -> None:
        info = {"available": True, "fetched_at": 1000.0}
        self.assertTrue(kept.is_fresh(info, 1, True, 1000.0 + 3600))
        self.assertFalse(kept.is_fresh(info, 1, True, 1000.0 + 2 * 86400))
        self.assertFalse(kept.is_fresh(info, 1, False, 1000.0))                       # (the gate off: always ask)
        self.assertEqual(kept.check_age({"available": False}, 7), {"status": "missing"})
        self.assertEqual(kept.check_age(info, 7, True, 1000.0 + 60), {"status": "up_to_date", "skipped": True})
        self.assertEqual(kept.check_age(info, 7, True, 1000.0 + 8 * 86400), {"status": "update_available"})

    def test_a_kept_json_round_trips_validates_and_remembers(self) -> None:
        for gz in (False, True):
            store = kept.KeptJson(lambda d: isinstance(d.get("items"), list), gz=gz)
            path = self.root / f"kept-{gz}.json"
            self.assertIsNone(store.load(path))                                       # missing
            store.write(path, {"items": [1, 2], "name": "café"})
            first = store.load(path)
            self.assertEqual(first, {"items": [1, 2], "name": "café"})
            self.assertIs(store.load(path), first)                                    # remembered while the file is the same
            path.write_bytes(b"not json at all")
            self.assertIsNone(store.load(path))                                       # damaged
            store.write(path, {"other": 1})
            self.assertIsNone(store.load(path))                                       # not what validate accepts
            self.assertEqual([p.name for p in self.root.glob(".kept-*")], [])

    def test_a_read_only_database_is_empty_when_missing_or_wrong(self) -> None:
        import sqlite3

        class Db(kept.ReadOnlyDb):
            probe = "SELECT 1 FROM things LIMIT 1"
        self.assertFalse(Db(self.root / "none.sqlite").available)
        bad = self.root / "bad.sqlite"
        bad.write_bytes(b"this is not a database")
        self.assertFalse(Db(bad).available)
        good = self.root / "good.sqlite"
        con = sqlite3.connect(good)
        con.execute("CREATE TABLE things (x)")
        con.execute("INSERT INTO things VALUES (1)")
        con.commit()
        con.close()
        db = Db(good)
        self.assertTrue(db.available)
        with self.assertRaises(sqlite3.OperationalError):
            db.conn.execute("INSERT INTO things VALUES (2)")                           # read-only
        db.close()
        self.assertFalse(db.available)
        wrong = self.root / "wrong.sqlite"
        sqlite3.connect(wrong).execute("CREATE TABLE other (x)").connection.close()
        self.assertFalse(Db(wrong).available)                                         # the tables are not there


class NativeLib(unittest.TestCase):
    def test_the_search_order_and_no_duplicates(self) -> None:
        with mock.patch.dict(os.environ, {"X_LIB": "/mine/libx.so"}), \
                mock.patch.object(nativelib.bundle, "lib_dir", return_value=None), \
                mock.patch.object(nativelib.ctypes.util, "find_library", return_value="libx.so.1"):
            got = nativelib.candidates("libx", "x", "X_LIB", names=("libx.so.1", "libx.so"), win_names=("x.dll",))
        self.assertEqual(got[0], "/mine/libx.so")                                     # the variable first
        self.assertEqual(got[1], "libx.so.1")                                         # then the loader's own answer
        self.assertEqual(len(got), len(set(got)))                                     # ("libx.so.1" given twice, listed once)
        self.assertIn("libx.so", got)
        if not sys.platform.startswith("win"):
            self.assertNotIn("x.dll", got)

    def test_a_bundled_library_comes_before_the_system_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shipped = Path(tmp)
            (shipped / "libx.so.9").write_bytes(b"")
            with mock.patch.object(nativelib.bundle, "lib_dir", return_value=shipped), \
                    mock.patch.object(nativelib.ctypes.util, "find_library", return_value="libx.so.1"):
                got = nativelib.candidates("libx", "x", None, names=("libx.so.1",))
        self.assertEqual(got[0], str(shipped / "libx.so.9"))

    def test_once_runs_the_search_once_even_from_many_threads_until_reset(self) -> None:
        calls = []
        gate = threading.Event()

        def find():
            calls.append(1)
            gate.wait(0.2)
            return "library"
        once = nativelib.Once(find)
        out = []
        threads = [threading.Thread(target=lambda: out.append(once.get())) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((out, len(calls)), (["library"] * 8, 1))
        once.reset()
        self.assertEqual(once.get(), "library")
        self.assertEqual(len(calls), 2)

    def test_the_app_folders_include_the_package_parent(self) -> None:
        self.assertIn(Path(nativelib.__file__).resolve().parent.parent, nativelib.app_folders())


class EmuData(Tmp):
    def test_measure_counts_skips_and_lists(self) -> None:
        (self.root / "a").mkdir()
        (self.root / "a" / "one.bin").write_bytes(b"12345")
        (self.root / "two.bin").write_bytes(b"123")
        (self.root / "_pcsx2_note").write_bytes(b"ignored")
        files, total, newest, listing = emudata.measure(self.root)
        self.assertEqual((files, total, listing), (3, 15, ()))
        self.assertGreater(newest, 0)
        files, total, _n, listing = emudata.measure(self.root, skip_prefix="_pcsx2_", listing=True)
        self.assertEqual((files, total, listing), (2, 8, (("a/one.bin", 5), ("two.bin", 3))))
        self.assertEqual(emudata.measure(self.root / "missing")[:3], (0, 0, 0.0))

    def test_read_lines(self) -> None:
        f = self.root / "x.ini"
        f.write_bytes(b"a=1\nb=\xff2\n")
        self.assertEqual(emudata.read_lines(f), ["a=1", "b=�2"])
        self.assertEqual(emudata.read_lines(self.root / "nope.ini"), [])


class Placement(Tmp):
    def op(self, *names: str, delete: bool = False) -> SimpleNamespace:
        moves = []
        for n in names:
            (self.root / n).write_bytes(n.encode())
            moves.append((self.root / n, self.root / "_converted_originals" / n))
        return SimpleNamespace(moves=moves, delete_originals=delete, deleted=[], left=[])

    def test_commit_keeps_the_originals_and_rollback_puts_everything_back(self) -> None:
        op = self.op("a.raw", "b.raw")
        new = self.root / "game.part"
        new.write_bytes(b"chd")
        created: list[str] = []
        place = organiser.Placement(self.root / "game.chd", created)
        place.commit(new, op)
        self.assertEqual((self.root / "game.chd").read_bytes(), b"chd")
        self.assertFalse((self.root / "a.raw").exists() or new.exists())
        self.assertTrue((self.root / "_converted_originals" / "b.raw").is_file())
        self.assertEqual(created, [str(self.root / "_converted_originals")])
        self.assertEqual(op.moves, [(self.root / n, self.root / "_converted_originals" / n) for n in ("a.raw", "b.raw")])
        place.rollback()
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()), ["a.raw", "b.raw"])
        self.assertFalse((self.root / "game.chd").exists())

    def test_commit_can_delete_the_originals_and_reports_one_it_could_not(self) -> None:
        op = self.op("a.raw", "b.raw", delete=True)
        new = self.root / "game.part"
        new.write_bytes(b"chd")
        real = os.unlink

        def unlink(p, *a, **k):
            if Path(p).name == "b.raw":
                raise PermissionError("in use")
            return real(p, *a, **k)
        with mock.patch("romorg.organiser.os.unlink", unlink):
            place = organiser.Placement(self.root / "game.chd", [])
            place.commit(new, op)
        self.assertEqual((op.deleted, op.moves), ([self.root / "a.raw"], []))
        self.assertEqual(len(op.left), 1)
        self.assertIn("b.raw", op.left[0])
        self.assertTrue((self.root / "game.chd").is_file() and (self.root / "b.raw").is_file())

    def test_a_failure_part_way_rolls_back(self) -> None:
        op = self.op("a.raw", "b.raw")
        new = self.root / "game.part"
        new.write_bytes(b"chd")
        real = organiser.rename_no_overwrite
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise PermissionError("locked")
            return real(src, dst)
        place = organiser.Placement(self.root / "game.chd", [])
        with mock.patch("romorg.organiser.rename_no_overwrite", flaky):
            with self.assertRaises(PermissionError):
                place.commit(new, op)
            place.rollback()
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()), ["a.raw", "b.raw"])

    def test_tidy_after_returns_only_folders_that_were_there_before(self) -> None:
        old = self.root / "old"
        old.mkdir()
        made = self.root / "made"
        made.mkdir()
        gone = organiser.tidy_after(self.root, [old], [str(made)], set())
        self.assertEqual(gone, [str(old)])
        self.assertFalse(old.exists() or made.exists())

    def test_discard_part(self) -> None:
        part = self.root / "x.part"
        part.write_bytes(b"half")
        organiser.discard_part(part)
        self.assertFalse(part.exists())
        organiser.discard_part(part)                                                  # (nothing there: fine)
        target = self.root / "real"
        target.write_bytes(b"keep")
        link = self.root / "link.part"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("no symbolic links")
        organiser.discard_part(link)
        self.assertTrue(link.is_symlink() and target.read_bytes() == b"keep")        # a link is never followed or removed
        part.write_bytes(b"x")
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            organiser.discard_part(part)                                              # (failure: silent ...)
            with self.assertRaises(PermissionError):
                organiser.discard_part(part, strict=True)                             # ... unless asked


class Small(unittest.TestCase):
    def test_swap16(self) -> None:
        self.assertEqual(cdimage.swap16(b"\x01\x02\x03\x04"), b"\x02\x01\x04\x03")
        from romorg import chd
        self.assertIs(chd.swap16, cdimage.swap16)


if __name__ == "__main__":
    unittest.main()
