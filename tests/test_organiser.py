"""Tests for romorg.organiser: organising a platform folder into DAT folders (synthetic files only)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import random
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, os.path.dirname(__file__))
from chdtestlib import make_symlink  # noqa: E402

from romorg import library, m3u, organiser, platforms, scanner
from romorg.datfile import DatFile, Rom
from romorg.organiser import (
    CONVERTED_DIR, SUPERSEDED_DIR, UNMATCHED_DIR, RenameOp, apply_renames, canonical_dir, choose_rom,
    list_undo_logs, plan_counts, plan_renames, safe_filename, target_filename, undo,
)

GAMES = "Commodore Amiga - Games - [ADF]"
WB = "Commodore Amiga - Operating Systems - Workbench"
FW = "Commodore Amiga - Firmware"


def _rand(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


def _rom(name: str, data: bytes = b"", game: str | None = None, dat: str = GAMES) -> Rom:
    crc = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    return Rom(name=name, size=len(data), crc=crc, md5=hashlib.md5(data).hexdigest(),
               sha1=hashlib.sha1(data).hexdigest(),
               game=game if game is not None else name.rsplit(".", 1)[0], dat=dat)


class SafeFilenameTests(unittest.TestCase):
    def test_illegal_chars_replaced(self) -> None:
        self.assertEqual(safe_filename('a:b?c*d"e<f>g|h\\i/j.adf'), "a_b_c_d_e_f_g_h_i_j.adf")

    def test_control_chars_and_trailing(self) -> None:
        self.assertEqual(safe_filename("ab\x01c\x7f. . "), "ab_c_")
        self.assertEqual(safe_filename("..."), "_")

    def test_unchanged_typical_tosec(self) -> None:
        n = "'Allo 'Allo! Cartoon Fun! (1993)(Alternative)(Disk 1 of 2)[cr CSL].adf"
        self.assertEqual(safe_filename(n), n)
        self.assertEqual(organiser.dat_folder_name(GAMES), GAMES)

    def test_reserved_names(self) -> None:
        self.assertEqual(safe_filename("CON.adf"), "_CON.adf")
        self.assertEqual(safe_filename("Console.adf"), "Console.adf")

    def test_length_limit_preserves_extension(self) -> None:
        out = safe_filename("é" * 300 + ".adf")
        self.assertLessEqual(len(out.encode("utf-8")), 255)
        self.assertTrue(out.endswith(".adf"))


class ChooseRomTests(unittest.TestCase):
    def test_exact_wins(self) -> None:
        a, b = _rom("Game (1990)(X)[a].adf"), _rom("Game (1990)(X).adf")
        chosen, alts = choose_rom("Game (1990)(X).adf", [a, b], key=lambda r: r.name)
        self.assertIs(chosen, b)
        self.assertEqual(alts, [a])

    def test_closest_then_alphabetical(self) -> None:
        a, b = _rom("Zork (1990).adf"), _rom("Lemmings (1991)(Psygnosis).adf")
        self.assertIs(choose_rom("lemmings.adf", [a, b], key=lambda r: r.name)[0], b)
        c, d = _rom("BB.adf"), _rom("AA.adf")
        self.assertEqual(choose_rom("xx.adf", [c, d], key=lambda r: r.name)[0].name, "AA.adf")


class OrganiseTests(unittest.TestCase):
    """End-to-end: build files, scan them with real DatFiles, plan and apply."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "amiga"
        self.root.mkdir()
        self.roms: dict[str, list[Rom]] = {GAMES: [], WB: [], FW: []}
        self._seed = 100

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # helpers ---------------------------------------------------------------

    def data(self, n: int = 2048) -> bytes:
        self._seed += 1
        return _rand(n, self._seed)

    def add_rom(self, dat: str, name: str, data: bytes, game: str | None = None) -> Rom:
        r = _rom(name, data, game, dat)
        self.roms[dat].append(r)
        return r

    def write(self, rel: str, data: bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def scan(self) -> scanner.ScanResult:
        dats = [DatFile(n, "", "", list(self.roms[n])) for n in (GAMES, WB, FW)]
        with mock.patch.object(scanner, "find_7z", return_value=None):
            return scanner.scan(self.root, dats, use_cache=False)

    def plan(self) -> dict[str, RenameOp]:
        return {op.src.relative_to(self.root).as_posix(): op for op in plan_renames(self.scan())}

    def files(self) -> set[str]:
        return {p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                if p.is_file() and not p.name.startswith(".romorg-undo-")}

    def dirs(self) -> set[str]:
        return {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_dir()}

    # tests -----------------------------------------------------------------

    def test_moves_from_nested_dirs_cleanup_and_undo(self) -> None:
        d1, d2 = self.data(), self.data()
        self.add_rom(GAMES, "Alpha (1990)(Pub).adf", d1)
        self.add_rom(FW, "Kickstart v1.3 (1987)(Commodore).rom", d2)
        self.write("a/b/c/alpha.adf", d1)
        self.write("roms/kick.rom", d2)
        (self.root / "pre-existing-empty").mkdir()
        (self.root / "keep" / "inner").mkdir(parents=True)
        self.write("keep/.hidden", b"x")  # hidden file: never touched, keeps its dir alive
        self.write("keep/Kickstart v1.3 (1987)(Commodore).rom", d2)  # right name, wrong place

        ops = self.plan()
        self.assertEqual(ops["a/b/c/alpha.adf"].status, "move")
        self.assertEqual(ops["a/b/c/alpha.adf"].dst, self.root / GAMES / "Alpha (1990)(Pub).adf")
        self.assertEqual(ops["a/b/c/alpha.adf"].kind, "move")
        self.assertEqual(ops["a/b/c/alpha.adf"].dat, GAMES)
        # same content: neither copy is at its canonical place -> the shorter path is kept (renamed),
        # the spare copy goes to _duplicates/ with its own file name
        self.assertEqual(ops["roms/kick.rom"].dst, self.root / FW / "Kickstart v1.3 (1987)(Commodore).rom")
        spare = ops["keep/Kickstart v1.3 (1987)(Commodore).rom"]
        self.assertEqual((spare.status, spare.code, spare.keeper), ("move", "duplicate", "roms/kick.rom"))
        self.assertEqual(spare.dst, self.root / "_duplicates" / "keep" / spare.src.name)
        self.assertNotIn("keep/.hidden", ops)

        before_files, before_dirs = self.files(), self.dirs()
        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual(res["moved"], 3, res)
        self.assertEqual(res["failed"], [])
        self.assertEqual(self.files(), {
            f"{GAMES}/Alpha (1990)(Pub).adf", f"{FW}/Kickstart v1.3 (1987)(Commodore).rom",
            "keep/.hidden", f"_duplicates/keep/{spare.src.name}",
        })
        dirs = self.dirs()
        for gone in ("a", "a/b", "a/b/c", "roms"):
            self.assertNotIn(gone, dirs)
        for kept in ("pre-existing-empty", "keep", "keep/inner", GAMES, FW):
            self.assertIn(kept, dirs)
        self.assertEqual(sorted(Path(d).relative_to(self.root).as_posix() for d in res["removed_dirs"]),
                         ["a", "a/b", "a/b/c", "roms"])

        log = organiser.read_undo_log(Path(res["undo_log"]))
        self.assertEqual(log["version"], 3)
        self.assertTrue(log["complete"])
        self.assertEqual(len(log["moves"]), 3)
        raw = Path(res["undo_log"]).read_text()
        self.assertNotIn(str(self.root), raw.split("\n", 1)[1])  # entries are relative to the folder

        # rescan after organising: everything already in place
        ops2 = self.plan()
        self.assertEqual({k: o.status for k, o in ops2.items()},
                         {f"{GAMES}/Alpha (1990)(Pub).adf": "ok",
                          f"{FW}/Kickstart v1.3 (1987)(Commodore).rom": "ok",
                          f"_duplicates/keep/{spare.src.name}": "ok"})

        u = undo(Path(res["undo_log"]))
        self.assertEqual(u["restored"], 3, u)
        self.assertEqual(self.files(), before_files)
        self.assertEqual(self.dirs(), before_dirs)  # removed dirs recreated, created DAT folders gone
        self.assertEqual(list_undo_logs(self.root), [])

    def test_files_already_in_place_are_ok(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Alpha (1990)(Pub).adf", d)
        self.write(f"{GAMES}/Alpha (1990)(Pub).adf", d)
        ops = self.plan()
        self.assertEqual([o.status for o in ops.values()], ["ok"])
        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual(res["moved"], 0)
        self.assertIsNone(res["undo_log"])
        self.assertEqual(self.scan().summary()["correctly_placed"], 1)

    def test_rename_inside_dat_folder(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Alpha (1990)(Pub).adf", d)
        self.write(f"{GAMES}/alpha.adf", d)
        op = self.plan()[f"{GAMES}/alpha.adf"]
        self.assertEqual((op.status, op.kind), ("move", "rename"))

    def test_unmatched_go_to_unmatched_preserving_paths(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Alpha (1990)(Pub).adf", d)
        self.write("sub/dir/junk.bin", b"junk")
        self.write(f"{GAMES}/stray.txt", b"stray")            # unmatched inside a DAT folder
        self.write(f"{UNMATCHED_DIR}/old/thing.bin", b"old")  # already in _unmatched: stays
        self.write(f"{UNMATCHED_DIR}/deep/alpha.adf", d)      # matched now: leaves _unmatched
        self.write("game.7z", b"7z\xbc\xaf\x27\x1c")          # unsupported (no 7z)
        with zipfile.ZipFile(self.write("multi.zip", b""), "w") as zf:  # nothing matches
            zf.writestr("a.txt", "a")
            zf.writestr("b.txt", "b")
        with zipfile.ZipFile(self.write("partial.zip", b""), "w") as zf:  # one of two matches
            zf.writestr("alpha.adf", d)
            zf.writestr("readme.txt", "hi")
        self.write("Mine.m3u", b"x.adf\n")  # user m3u: never touched

        ops = self.plan()
        U = self.root / UNMATCHED_DIR
        self.assertEqual(ops["sub/dir/junk.bin"].dst, U / "sub" / "dir" / "junk.bin")
        self.assertTrue(ops["sub/dir/junk.bin"].unmatched)
        self.assertEqual(ops[f"{GAMES}/stray.txt"].dst, U / GAMES / "stray.txt")
        self.assertEqual(ops[f"{UNMATCHED_DIR}/old/thing.bin"].status, "ok")
        self.assertEqual(ops[f"{UNMATCHED_DIR}/deep/alpha.adf"].dst, self.root / GAMES / "Alpha (1990)(Pub).adf")
        self.assertEqual(ops["game.7z"].dst, U / "game.7z")
        self.assertEqual(ops["multi.zip"].dst, U / "multi.zip")
        self.assertEqual(ops["partial.zip"].status, "skip")
        self.assertNotIn("Mine.m3u", ops)
        self.assertEqual(plan_counts(ops.values())["to_unmatched"], 4)

        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual(res["failed"], [])
        self.assertEqual(self.files(), {
            f"{GAMES}/Alpha (1990)(Pub).adf", f"{UNMATCHED_DIR}/sub/dir/junk.bin",
            f"{UNMATCHED_DIR}/{GAMES}/stray.txt", f"{UNMATCHED_DIR}/old/thing.bin",
            f"{UNMATCHED_DIR}/game.7z", f"{UNMATCHED_DIR}/multi.zip", "partial.zip", "Mine.m3u",
        })
        self.assertNotIn(f"{UNMATCHED_DIR}/deep", self.dirs())  # emptied by our move
        self.assertIn(UNMATCHED_DIR, self.dirs())

    def test_dat_folders_never_removed(self) -> None:
        self.write(f"{FW}/notes.txt", b"n")  # only content of the Firmware folder is unmatched
        ops = self.plan()
        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual(res["moved"], 1)
        self.assertTrue((self.root / FW).is_dir())
        self.assertEqual(res["removed_dirs"], [])

    def test_priority_decides_folder(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Workbench Game (1988).adf", d)
        self.add_rom(WB, "Workbench v1.3 (1988)(Commodore).adf", d)
        self.write(f"{WB}/Workbench v1.3 (1988)(Commodore).adf", d)  # in lower-priority folder
        op = self.plan()[f"{WB}/Workbench v1.3 (1988)(Commodore).adf"]
        self.assertEqual(op.dat, GAMES)
        self.assertEqual(op.dst, self.root / GAMES / "Workbench Game (1988).adf")

    def test_archive_single_member(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Game (1990)(Disk 1 of 2).adf", d, game="Game (1990)(Disk 1 of 2)")
        with zipfile.ZipFile(self.write("x/whatever.zip", b""), "w") as zf:
            zf.writestr("inner.adf", d)
        op = self.plan()["x/whatever.zip"]
        self.assertEqual(op.status, "move")
        self.assertEqual(op.dst, self.root / GAMES / "Game (1990)(Disk 1 of 2).zip")

    def test_sanitised_target(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Game: Three? (1992).adf", d)
        self.write("three.adf", d)
        op = self.plan()["three.adf"]
        self.assertEqual(op.dst.name, "Game_ Three_ (1992).adf")
        self.assertIn("sanitised", op.reason)
        self.assertEqual(op.rom_name, "Game: Three? (1992).adf")

    def test_conflicts_and_duplicates(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Target.adf", d)
        self.write("a.adf", d)
        self.write(f"{GAMES}/Target.adf", b"different")  # unmatched occupant moves away first
        ops = self.plan()
        self.assertEqual(ops["a.adf"].status, "move")
        self.assertIn("moved away first", ops["a.adf"].reason)
        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual(res["failed"], [])
        self.assertEqual((self.root / GAMES / "Target.adf").read_bytes(), d)
        self.assertEqual((self.root / UNMATCHED_DIR / GAMES / "Target.adf").read_bytes(), b"different")

        # two sources, same content -> one move, one duplicate left in place
        (self.root / GAMES / "Target.adf").rename(self.root / "b.adf")
        self.write("c.adf", d)
        ops = self.plan()
        self.assertEqual(ops["b.adf"].dst, self.root / GAMES / "Target.adf")  # alphabetical keeper
        self.assertEqual((ops["c.adf"].status, ops["c.adf"].code, ops["c.adf"].keeper),
                         ("move", "duplicate", "b.adf"))
        self.assertEqual(ops["c.adf"].dst, self.root / "_duplicates" / "c.adf")

    def test_conflict_with_unscanned_file(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Target.adf", d)
        self.write("a.adf", d)
        (self.root / GAMES).mkdir()
        (self.root / GAMES / "Target.adf").mkdir()  # a directory occupies the target
        self.assertEqual(self.plan()["a.adf"].status, "conflict")

    def test_apply_never_overwrites(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "One.adf", d)
        self.write("one.adf", d)
        ops = list(self.plan().values())
        self.write(f"{GAMES}/One.adf", b"appeared later")
        res = apply_renames(ops, self.root)
        self.assertEqual(res["moved"], 0)
        self.assertEqual(len(res["failed"]), 1)
        self.assertIsNone(res["undo_log"])
        self.assertEqual((self.root / GAMES / "One.adf").read_bytes(), b"appeared later")
        self.assertEqual((self.root / "one.adf").read_bytes(), d)

    def test_chain(self) -> None:
        # a.adf must become b.adf while b.adf must become c.adf (same DAT folder)
        da, db = self.data(), self.data()
        self.add_rom(GAMES, "b.adf", da)
        self.add_rom(GAMES, "c.adf", db)
        self.write(f"{GAMES}/a.adf", da)
        self.write(f"{GAMES}/b.adf", db)
        ops = list(self.plan().values())
        self.assertTrue(all(op.status == "move" for op in ops), ops)
        res = apply_renames(ops, self.root)
        self.assertEqual(res["moved"], 2, res)
        self.assertEqual((self.root / GAMES / "b.adf").read_bytes(), da)
        self.assertEqual((self.root / GAMES / "c.adf").read_bytes(), db)

    def test_undo_skips_when_original_taken(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "One.adf", d)
        self.write("one.adf", d)
        res = apply_renames(list(self.plan().values()), self.root)
        self.write("one.adf", b"new file")
        u = undo(Path(res["undo_log"]))
        self.assertEqual(u["restored"], 0)
        self.assertEqual(len(u["skipped"]), 1)
        self.assertTrue((self.root / GAMES / "One.adf").exists())

    def test_undo_reads_version1_logs(self) -> None:
        self.write("new.adf", b"x")
        log = self.root / ".romorg-undo-20250101-000000.json"
        log.write_text(json.dumps([{"src": str(self.root / "old" / "old.adf"),
                                    "dst": str(self.root / "new.adf")}]))
        u = undo(log)
        self.assertEqual(u["restored"], 1)
        self.assertTrue((self.root / "old" / "old.adf").exists())

    def test_progress(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "One.adf", d)
        self.write("one.adf", d)
        calls: list[tuple[int, int, str]] = []
        apply_renames(list(self.plan().values()), self.root, progress=lambda *a: calls.append(a))
        self.assertEqual(calls, [(1, 1, "One.adf")])

    # --- review fixes -------------------------------------------------------

    def test_missing_dat_folder_is_left_alone(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Alpha (1990)(Pub).adf", d)
        self.write(f"{FW}/Kickstart v1.3 (1987)(Commodore).rom", b"kick")  # Firmware DAT not loaded
        self.write("loose/junk.bin", b"junk")
        res = self.scan()
        ops = {op.src.relative_to(self.root).as_posix(): op
               for op in plan_renames(res, missing_dats=[FW, WB])}
        op = ops[f"{FW}/Kickstart v1.3 (1987)(Commodore).rom"]
        self.assertEqual(op.status, "skip")
        self.assertIn("not loaded", op.reason)
        self.assertEqual(ops["loose/junk.bin"].status, "move")  # elsewhere unmatched still moves

    def test_unmatched_opt_out_and_frontend_files(self) -> None:
        self.write("systeminfo.txt", b"es-de")
        self.write("gamelist.xml", b"<gameList/>")
        self.write("cfg/game.uae", b"cfg")
        self.write("other/thing.bin", b"t")
        ops = self.plan()
        for k in ("systeminfo.txt", "gamelist.xml", "cfg/game.uae"):
            self.assertEqual(ops[k].status, "skip", k)
        self.assertEqual(ops["other/thing.bin"].status, "move")
        ops2 = plan_renames(self.scan(), move_unmatched=False)
        self.assertEqual({o.status for o in ops2}, {"skip"})

    def test_symlink_left_alone(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "Alpha (1990)(Pub).adf", d)
        self.write("library/alpha.adf", d)
        make_symlink(self.root / "link.adf", "library/alpha.adf")
        ops = self.plan()
        self.assertEqual(ops["link.adf"].status, "skip")
        self.assertIn("symbolic link", ops["link.adf"].reason)

    def test_own_m3u_deleted_and_restored_by_undo(self) -> None:
        from romorg import m3u
        d1, d2 = self.data(), self.data()
        self.add_rom(GAMES, "Foo (1990)(X)(Disk 1 of 2).adf", d1)
        self.add_rom(GAMES, "Foo (1990)(X)(Disk 2 of 2).adf", d2)
        self.write("old/sub/foo1.adf", d1)
        self.write("old/sub/foo2.adf", d2)
        own = self.write("old/sub/Foo (1990)(X).m3u", f"{m3u.M3U_MARKER}\nfoo1.adf|Disk 1\nfoo2.adf|Disk 2\n".encode())
        user = self.write("old/My list.m3u", b"sub/foo1.adf\n")
        ops = self.plan()
        self.assertEqual(ops["old/sub/Foo (1990)(X).m3u"].status, "delete")
        self.assertEqual(ops["old/My list.m3u"].status, "skip")
        self.assertEqual(plan_counts(ops.values())["delete"], 1)
        before_files, before_dirs = self.files(), self.dirs()
        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual((res["moved"], res["deleted"], res["failed"]), (2, 1, []))
        self.assertFalse(own.exists())
        self.assertTrue(user.exists())
        self.assertNotIn("old/sub", self.dirs())  # nothing keeps the old folder alive
        u = undo(Path(res["undo_log"]))
        self.assertEqual((u["restored"], u["m3us_restored"]), (2, 1))
        self.assertEqual(self.files(), before_files)
        self.assertEqual(self.dirs(), before_dirs)
        self.assertTrue(own.read_text().startswith(m3u.M3U_MARKER))

    def test_undo_removes_own_m3u_written_after_organise(self) -> None:
        from romorg import m3u
        d1, d2 = self.data(), self.data()
        self.add_rom(GAMES, "Foo (1990)(X)(Disk 1 of 2).adf", d1)
        self.add_rom(GAMES, "Foo (1990)(X)(Disk 2 of 2).adf", d2)
        self.write("foo1.adf", d1)
        self.write("foo2.adf", d2)
        res = apply_renames(list(self.plan().values()), self.root)
        ops = m3u.plan_m3us(self.scan())
        self.assertEqual(m3u.write_m3us(ops)["written"], 1)
        u = undo(Path(res["undo_log"]))
        self.assertEqual((u["restored"], u["m3us_removed"]), (2, 1))
        self.assertFalse((self.root / GAMES).exists())  # the created DAT folder could be removed

    def test_duplicate_blocking_a_canonical_name_moves_out_of_the_way(self) -> None:
        da, db = self.data(), self.data()
        self.add_rom(GAMES, "A.adf", da)
        self.add_rom(GAMES, "B.adf", db)
        self.write(f"{GAMES}/A.adf", db)  # misnamed copy of B sitting at A's name
        self.write(f"{GAMES}/B.adf", db)
        self.write("x/A.adf", da)
        ops = self.plan()
        self.assertEqual(ops[f"{GAMES}/A.adf"].status, "move")
        self.assertEqual(ops[f"{GAMES}/A.adf"].dst, self.root / "_duplicates" / GAMES / "A.adf")
        self.assertEqual(ops["x/A.adf"].status, "move")
        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual(res["failed"], [])
        self.assertEqual((self.root / GAMES / "A.adf").read_bytes(), da)

    def test_holder_that_stays_makes_dependent_a_conflict(self) -> None:
        da = self.data()
        self.add_rom(GAMES, "A.adf", da)
        self.write(f"{UNMATCHED_DIR}/A.adf", b"x")  # stays in _unmatched (not a target)
        self.add_rom(GAMES, "C.adf", self.data())
        # x/A.adf -> Games/A.adf, held by Games/A.adf which conflicts with another file
        self.write(f"{GAMES}/A.adf", b"held")
        self.write(f"{UNMATCHED_DIR}/{GAMES}/A.adf", b"occupied")  # its own _unmatched target is taken
        self.write("x/A.adf", da)
        ops = self.plan()
        self.assertEqual(ops[f"{GAMES}/A.adf"].status, "conflict")
        self.assertEqual(ops["x/A.adf"].status, "conflict")
        self.assertIn("stays", ops["x/A.adf"].reason)

    def test_swap_cycle_applied_via_temp(self) -> None:
        da, db = self.data(), self.data()
        self.add_rom(GAMES, "A.adf", da)
        self.add_rom(GAMES, "B.adf", db)
        self.write(f"{GAMES}/A.adf", db)
        self.write(f"{GAMES}/B.adf", da)
        ops = list(self.plan().values())
        self.assertEqual([o.status for o in ops], ["move", "move"])
        res = apply_renames(ops, self.root)
        self.assertEqual((res["moved"], res["failed"]), (2, []))
        self.assertEqual((self.root / GAMES / "A.adf").read_bytes(), da)
        self.assertEqual((self.root / GAMES / "B.adf").read_bytes(), db)
        self.assertEqual(sorted(os.listdir(self.root / GAMES)), ["A.adf", "B.adf"])
        u = undo(Path(res["undo_log"]))
        self.assertEqual(u["failed"], [])
        self.assertEqual((self.root / GAMES / "A.adf").read_bytes(), db)

    def test_undo_exact_after_failed_move(self) -> None:
        d1, d2 = self.data(), self.data()
        self.add_rom(GAMES, "One.adf", d1)
        self.add_rom(GAMES, "Two.adf", d2)
        self.write("x/a.adf", d1)
        self.write("y/b.adf", d2)
        self.write("z/junk.bin", b"junk")
        ops = list(self.plan().values())
        (self.root / "z" / "junk.bin").unlink()  # vanishes between preview and apply
        before_files, before_dirs = self.files(), self.dirs()
        res = apply_renames(ops, self.root)
        self.assertEqual((res["moved"], len(res["failed"])), (2, 1))
        self.assertNotIn(str(self.root / UNMATCHED_DIR / "z"), res["removed_dirs"])
        undo(Path(res["undo_log"]))
        self.assertEqual(self.files(), before_files)
        self.assertEqual(self.dirs(), before_dirs)

    def test_interrupted_apply_can_be_undone(self) -> None:
        names = [f"G{i} (1990).adf" for i in range(6)]
        for i, n in enumerate(names):
            d = self.data(256)
            self.add_rom(GAMES, n, d)
            self.write(f"src/{i}.adf", d)
        before = self.files()
        ops = list(self.plan().values())

        def boom(done: int, total: int, name: str) -> None:
            if done == 3:
                raise KeyboardInterrupt  # SIGTERM / Ctrl+C mid-apply
        with self.assertRaises(KeyboardInterrupt):
            apply_renames(ops, self.root, progress=boom)
        logs = list_undo_logs(self.root)
        self.assertEqual(len(logs), 1)
        self.assertEqual(len(organiser.read_undo_log(logs[0])["moves"]), 3)
        u = undo(logs[0])
        self.assertEqual(u["restored"], 3)
        self.assertEqual(self.files(), before)

    def test_torn_log_and_temp_leftover(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "One.adf", d)
        src = self.write("one.adf", d)
        # A crash during a case-only rename leaves the file at its temp name and the
        # log with an intent record (and possibly a torn last line).
        tmp = src.with_name("one.adf" + organiser.TEMP_MARKER + "abcd1234")
        src.rename(tmp)
        log = self.root / ".romorg-undo-20250101-000000.json"
        log.write_text(json.dumps({"version": 3, "root": str(self.root)}) + "\n"
                       + json.dumps({"op": "move", "src": "one.adf", "dst": f"{GAMES}/One.adf", "i": 1})
                       + "\n{\"op\": \"mo", encoding="utf-8")
        u = undo(log)
        self.assertEqual(u["restored"], 1)
        self.assertEqual(src.read_bytes(), d)

    def test_scanner_reports_temp_leftovers(self) -> None:
        self.write("game.adf" + organiser.TEMP_MARKER + "abcd1234", b"x")
        res = self.scan()
        self.assertEqual(len(res.errors), 1)
        self.assertIn("interrupted", res.errors[0][1])

    def test_undo_after_remount_uses_relative_paths(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "One.adf", d)
        self.write("in/one.adf", d)
        res = apply_renames(list(self.plan().values()), self.root)
        moved_root = self.root.with_name("amiga-remounted")
        self.root.rename(moved_root)
        u = undo(moved_root / Path(res["undo_log"]).name)
        self.assertEqual(u["restored"], 1)
        self.assertTrue((moved_root / "in" / "one.adf").is_file())

    def test_old_absolute_log_rebased_after_remount(self) -> None:
        self.write(f"{GAMES}/One.adf", b"x")
        log = self.root / ".romorg-undo-20240101-000000.json"
        old = "/run/media/mmcblk0p1/amiga"
        log.write_text(json.dumps({"version": 2, "root": old, "moves": [
            {"src": f"{old}/in/one.adf", "dst": f"{old}/{GAMES}/One.adf"}], "created_dirs": [],
            "removed_dirs": [f"{old}/in"]}))
        u = undo(log)
        self.assertEqual(u["restored"], 1)
        self.assertTrue((self.root / "in" / "one.adf").is_file())

    def test_partial_undo_keeps_log_for_retry(self) -> None:
        d1, d2 = self.data(), self.data()
        self.add_rom(GAMES, "One.adf", d1)
        self.add_rom(GAMES, "Two.adf", d2)
        self.write("one.adf", d1)
        self.write("two.adf", d2)
        res = apply_renames(list(self.plan().values()), self.root)
        blocker = self.write("one.adf", b"blocker")
        u = undo(Path(res["undo_log"]))
        self.assertEqual((u["restored"], u["remaining"]), (1, 1))
        logs = list_undo_logs(self.root)
        self.assertEqual(len(logs), 1)
        blocker.unlink()
        u2 = undo(logs[0])
        self.assertEqual((u2["restored"], u2["remaining"]), (1, 0))
        self.assertEqual(list_undo_logs(self.root), [])
        self.assertEqual((self.root / "one.adf").read_bytes(), d1)

    def test_planted_log_cannot_reach_outside(self) -> None:
        outside = Path(self.tmp.name) / "victim"
        outside.mkdir()
        (outside / "id_rsa").write_bytes(b"secret")
        make_symlink(self.root / "evil", outside)
        log = self.root / ".romorg-undo-29991231-000000.json"
        log.write_text(json.dumps({"version": 2, "moves": [
            {"src": str(self.root / "stolen"), "dst": str(outside / "id_rsa")},
            {"src": "stolen2", "dst": "../victim/id_rsa"},
            {"src": "stolen3", "dst": "evil/id_rsa"}],
            "created_dirs": [str(outside)], "removed_dirs": []}))
        u = undo(log)
        self.assertEqual(u["restored"], 0)
        self.assertEqual(u["rejected"], 4)
        self.assertTrue((outside / "id_rsa").is_file())
        self.assertFalse((self.root / "stolen").exists())
        with self.assertRaises(ValueError):
            undo(log, root=outside)

    def test_cancel_stops_between_moves(self) -> None:
        for i in range(4):
            d = self.data(128)
            self.add_rom(GAMES, f"C{i}.adf", d)
            self.write(f"s/{i}.adf", d)
        ops = list(self.plan().values())
        calls = {"n": 0}

        def cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 2
        res = apply_renames(ops, self.root, cancel=cancel)
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["moved"], 2)
        self.assertEqual(undo(Path(res["undo_log"]))["restored"], 2)

    def test_undo_log_unwritable_stops_before_moving(self) -> None:
        d = self.data()
        self.add_rom(GAMES, "One.adf", d)
        self.write("one.adf", d)
        ops = list(self.plan().values())
        with mock.patch.object(organiser, "_new_log_path", return_value=self.root / "nodir" / "x.json"):
            res = apply_renames(ops, self.root)
        self.assertEqual(res["moved"], 0)
        self.assertIn("undo log", res["error"])
        self.assertTrue((self.root / "one.adf").exists())

    @unittest.skipUnless(os.name == "posix", "bytes filenames")
    def test_undecodable_names_apply_and_undo(self) -> None:
        name = os.fsdecode(b"junk\xff.bin")
        try:
            self.write(f"odd/{name}", b"j")
        except OSError:
            self.skipTest("filesystem rejects the name")
        res = apply_renames(list(self.plan().values()), self.root)
        self.assertEqual((res["moved"], res["failed"]), (1, []))
        self.assertTrue((self.root / UNMATCHED_DIR / "odd" / name).is_file())
        self.assertEqual(undo(Path(res["undo_log"]))["restored"], 1)
        self.assertTrue((self.root / "odd" / name).is_file())

    def test_move_never_replaces_a_target_created_after_the_check(self) -> None:
        a = self.write("a.adf", b"a")
        b = self.write("b.adf", b"b")
        with mock.patch.object(organiser, "_exists", lambda p: Path(p) == a):  # check misses b
            with self.assertRaises(FileExistsError):
                organiser._rename_no_overwrite(a, b)
        self.assertEqual(b.read_bytes(), b"b")
        self.assertEqual(a.read_bytes(), b"a")


class CaseInsensitiveFsTests(unittest.TestCase):
    """Simulate exFAT-style case-insensitive name lookup on a case-sensitive test FS."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.renames: list[tuple[str, str]] = []
        real_rename, real_link = os.rename, os.link

        def ci_exists(p: Path) -> bool:
            p = Path(p)
            try:
                return any(c.name.casefold() == p.name.casefold() for c in p.parent.iterdir())
            except OSError:
                return False

        def ci_same(a: Path, b: Path) -> bool:
            return ci_exists(a) and ci_exists(b) and str(a).casefold() == str(b).casefold()

        def spy_rename(a, b) -> None:
            self.renames.append((Path(a).name, Path(b).name))
            real_rename(a, b)

        def spy_link(a, b, **kw) -> None:
            self.renames.append((Path(a).name, Path(b).name))
            real_link(a, b, **kw)

        self.patches = [mock.patch.object(organiser, "_exists", ci_exists),
                        mock.patch.object(organiser, "_same_file", ci_same),
                        mock.patch.object(organiser.os, "rename", spy_rename),
                        mock.patch.object(organiser.os, "link", spy_link)]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _scan(self, roms: list[Rom]) -> scanner.ScanResult:
        with mock.patch.object(scanner, "find_7z", return_value=None):
            return scanner.scan(self.root, [DatFile(GAMES, "", "", roms)], use_cache=False)

    def test_case_only_rename_goes_via_temp(self) -> None:
        d = _rand(500, 1)
        (self.root / GAMES).mkdir()
        (self.root / GAMES / "game.adf").write_bytes(d)
        ops = plan_renames(self._scan([_rom("Game.adf", d)]))
        self.assertEqual(ops[0].status, "move")
        self.assertIn("case-only", ops[0].reason)
        res = apply_renames(ops, self.root)
        self.assertEqual(res["moved"], 1, res)
        self.assertEqual(os.listdir(self.root / GAMES), ["Game.adf"])
        self.assertEqual(len(self.renames), 2)  # src -> temp -> dst (rename, then link+unlink)
        self.assertIn(".romorg-tmp-", self.renames[0][1])
        undo(Path(res["undo_log"]))
        self.assertEqual(os.listdir(self.root / GAMES), ["game.adf"])

    def test_case_variant_of_other_file_is_conflict(self) -> None:
        d = _rand(500, 2)
        (self.root / GAMES).mkdir()
        (self.root / GAMES / "GAME.ADF").write_bytes(b"something else")
        (self.root / "x.adf").write_bytes(d)
        ops = {op.src.name: op for op in plan_renames(self._scan([_rom("Game.adf", d)]))}
        # GAME.ADF (unmatched) moves away first, so x.adf may take the name afterwards
        self.assertEqual(ops["x.adf"].status, "move")
        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual(res["failed"], [])
        self.assertEqual(os.listdir(self.root / GAMES), ["Game.adf"])
        self.assertEqual((self.root / GAMES / "Game.adf").read_bytes(), d)

    def test_two_targets_differing_in_case_conflict(self) -> None:
        d1, d2 = _rand(500, 3), _rand(500, 4)
        (self.root / "a.adf").write_bytes(d1)
        (self.root / "b.adf").write_bytes(d2)
        ops = {op.src.name: op for op in plan_renames(self._scan([_rom("Game.adf", d1), _rom("GAME.adf", d2)]))}
        self.assertEqual(sorted(o.status for o in ops.values()), ["conflict", "move"])



# --------------------------------------------------------------------------- No-Intro (Amendment 4)

GBA = "Nintendo - Game Boy Advance"
SNES = "Nintendo - Super Nintendo Entertainment System"
N64 = "Nintendo - Nintendo 64"
NES = "Nintendo - Nintendo Entertainment System"


def _nrom(name: str, data: bytes = b"", dat: str = GBA, game: str | None = None) -> Rom:
    """A No-Intro style rom: set name = rom name without its extension."""
    r = _rom(name, data, game, dat)
    return dataclasses.replace(r, set_name=name.rsplit(".", 1)[0])


class TargetFilenameTests(unittest.TestCase):
    def test_raw_and_archive(self) -> None:
        r = _nrom("Game (USA) (Aftermarket) (Unl).gba", game="Game (USA)")
        self.assertEqual(target_filename(r, "whatever.gba"), "Game (USA) (Aftermarket) (Unl).gba")
        # archive name comes from the set (rom stem), not the game name
        self.assertEqual(target_filename(r, "x.zip", ".zip"), "Game (USA) (Aftermarket) (Unl).zip")
        t = _rom("Alpha (1990)(Pub)(Disk 1 of 2).adf", game="Alpha (1990)(Pub)")
        self.assertEqual(target_filename(t, "a.7z", ".7z"), "Alpha (1990)(Pub).7z")  # TOSEC: game name
        self.assertEqual(target_filename(_nrom("A: B (USA).gba"), "x.gba"), "A_ B (USA).gba")

    def test_headerless_keeps_an_honest_extension(self) -> None:
        snes = _nrom("Super Game (USA).sfc", dat=SNES)
        self.assertEqual(target_filename(snes, "sg.smc", None, "headerless"), "Super Game (USA).smc")
        self.assertEqual(target_filename(snes, "sg.SWC", None, "headerless"), "Super Game (USA).SWC")
        self.assertEqual(target_filename(snes, "sg.sfc", None, "headerless"), "Super Game (USA).smc")
        self.assertEqual(target_filename(snes, "sg", None, "headerless"), "Super Game (USA).smc")
        unh = _nrom("Nes Game (USA).unh", dat=NES)
        self.assertEqual(target_filename(unh, "n.nes", None, "headerless"), "Nes Game (USA).nes")
        self.assertEqual(target_filename(unh, "n.unh", None, "headerless"), "Nes Game (USA).nes")
        # archives are named after the set whatever the member's format
        self.assertEqual(target_filename(snes, "sg.zip", ".zip", "headerless"), "Super Game (USA).zip")

    def test_byteswapped(self) -> None:
        z = _nrom("Mario (USA).z64", dat=N64)
        self.assertEqual(target_filename(z, "m.z64", None, "byteswapped", "v64"), "Mario (USA).v64")
        self.assertEqual(target_filename(z, "m.v64", None, "byteswapped", "n64"), "Mario (USA).n64")
        self.assertEqual(target_filename(z, "m.rom", None, "byteswapped", ""), "Mario (USA).rom")
        # the DAT's own .v64 entries match raw
        self.assertEqual(target_filename(_nrom("Mario (USA).v64", dat=N64), "m.v64"), "Mario (USA).v64")

    def test_canonical_dir(self) -> None:
        root = Path("/r")
        self.assertEqual(canonical_dir(root, GAMES, "per_dat"), root / GAMES)
        self.assertEqual(canonical_dir(root, GBA, "flat"), root)
        self.assertEqual(canonical_dir(root, GAMES), root / GAMES)

    def test_public_aliases(self) -> None:
        for name in ("Journal", "rename_no_overwrite", "move_exclusive", "make_dirs", "temp_name",
                     "rel_str", "remove_empty_dirs", "protected_dirs", "UndoLogError"):
            self.assertTrue(callable(getattr(organiser, name)), name)
        self.assertIs(organiser.Journal, organiser._Journal)


class NoIntroOrganiseTests(unittest.TestCase):
    """Flat layout, alternate-hash names, latest-only and converted originals."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "gba"
        self.root.mkdir()
        self.roms: list[Rom] = []
        self.dat_name = GBA
        self._seed = 500

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def data(self, n: int = 1024) -> bytes:
        self._seed += 1
        return _rand(n, self._seed)

    def add(self, name: str, data: bytes | None = None, tosec: bool = False) -> bytes:
        data = self.data() if data is None else data
        self.roms.append(_rom(name, data, None, self.dat_name) if tosec else _nrom(name, data, self.dat_name))
        return data

    def write(self, rel: str, data: bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def scan(self, layout: str = "flat") -> scanner.ScanResult:
        with mock.patch.object(scanner, "find_7z", return_value=None):
            res = scanner.scan(self.root, [DatFile(self.dat_name, "", "", list(self.roms))], use_cache=False)
        res.layout = layout
        return res

    def plan(self, layout: str = "flat", **kw) -> dict[str, RenameOp]:
        return {op.src.relative_to(self.root).as_posix(): op
                for op in plan_renames(self.scan(layout), layout=layout, **kw)}

    def files(self) -> set[str]:
        return {p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                if p.is_file() and not p.name.startswith(".romorg-undo-")}

    def dirs(self) -> set[str]:
        return {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_dir()}

    # flat layout -------------------------------------------------------------

    def test_flat_layout_moves_everything_into_the_root(self) -> None:
        a, b, c, z = self.add("Alpha (USA).gba"), self.add("Beta Quest (Europe) (En,Fr,De).gba"), \
            self.add("Gamma (Japan).gba"), self.add("Zipped (World).gba")
        self.write("sub/dir/alpha.gba", a)
        self.write(f"{GBA}/Beta Quest (Europe) (En,Fr,De).gba", b)  # an old DAT folder
        self.write("_unmatched/old/gamma.gba", c)                     # matched: comes back
        with zipfile.ZipFile(self.root / "zz.zip", "w") as zf:
            zf.writestr("zipped.gba", z)
        self.write("junk/readme.txt", b"hello")
        self.write("media/images/alpha.png", b"png")                  # frontend media folder
        self.write("Images/box.png", b"png2")
        self.write("gamelist.xml", b"<gameList/>")

        ops = self.plan(missing_dats=[GBA])  # the missing-DAT folder rule does not apply to flat roots
        self.assertEqual(ops["sub/dir/alpha.gba"].dst, self.root / "Alpha (USA).gba")
        self.assertEqual(ops[f"{GBA}/Beta Quest (Europe) (En,Fr,De).gba"].dst,
                         self.root / "Beta Quest (Europe) (En,Fr,De).gba")
        self.assertEqual(ops["_unmatched/old/gamma.gba"].dst, self.root / "Gamma (Japan).gba")
        self.assertEqual(ops["zz.zip"].dst, self.root / "Zipped (World).zip")
        self.assertEqual(ops["zz.zip"].kind, "rename")
        self.assertEqual(ops["junk/readme.txt"].dst, self.root / UNMATCHED_DIR / "junk/readme.txt")
        for keep in ("media/images/alpha.png", "Images/box.png"):
            self.assertEqual(ops[keep].status, "skip")
            self.assertIn("frontend media folder", ops[keep].reason)
        self.assertEqual(ops["gamelist.xml"].status, "skip")

        before = self.files()
        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual(res["failed"], [])
        self.assertEqual(self.files(), {
            "Alpha (USA).gba", "Beta Quest (Europe) (En,Fr,De).gba", "Gamma (Japan).gba",
            "Zipped (World).zip", "_unmatched/junk/readme.txt", "media/images/alpha.png",
            "Images/box.png", "gamelist.xml"})
        self.assertNotIn("sub", self.dirs())
        self.assertNotIn("_unmatched/old", self.dirs())
        self.assertEqual({o.status for o in self.plan().values()} - {"skip"}, {"ok"})
        undo(Path(res["undo_log"]))
        self.assertEqual(self.files(), before)

    def test_alternates_of_one_set_keep_their_own_names(self) -> None:
        self.dat_name = NES
        prg = self.data(2048)
        nes = self.add("Nes Game (USA).nes", b"NES\x1a" + bytes(12) + prg)
        unh = self.add("Nes Game (USA).unh", prg)
        self.write("a.nes", nes)
        self.write("b/a.unh", unh)
        ops = self.plan()
        self.assertEqual(ops["a.nes"].dst, self.root / "Nes Game (USA).nes")
        self.assertEqual(ops["b/a.unh"].dst, self.root / "Nes Game (USA).unh")

    def test_alternate_hash_matches_are_renamed_honestly(self) -> None:
        self.dat_name = SNES
        clean = self.data(4096)
        self.add("Super Game (USA).sfc", clean)
        self.write("roms/super game.sfc", bytes(512) + clean)  # copier header, claims to be clean
        res = self.scan()
        m = res.matched[0] if res.matched else None
        if m is None:  # scanner without alt-hash support: fake the headerless match
            e = res.unmatched[0]
            m = scanner.Match(e, [self.roms[0]])
            res.matched, res.unmatched = [m], []
        m.matched_via, m.header = "headerless", 512
        ops = {o.src.name: o for o in plan_renames(res, layout="flat")}
        op = ops["super game.sfc"]
        self.assertEqual(op.dst, self.root / "Super Game (USA).smc")
        self.assertEqual(op.rom_name, "Super Game (USA).sfc")
        self.assertIn("header skipped", op.reason)
        # byte-swapped N64
        m.roms = [_nrom("Mario (USA).z64", clean, N64)]
        m.matched_via, m.byte_order = "byteswapped", "v64"
        op = plan_renames(res, layout="flat")[0]
        self.assertEqual(op.dst, self.root / "Mario (USA).v64")
        self.assertEqual(op.dat, N64)

    # latest only -------------------------------------------------------------

    def _versions(self) -> dict[str, bytes]:
        names = [
            "Racer (USA).gba", "Racer (USA) (Rev 1).gba", "Racer (USA) (Rev 2).gba",
            "Racer (Europe) (En,Fr,De).gba", "Racer (Europe) (En,Fr,De) (Rev A).gba",
            "Racer (USA) (Beta).gba", "Racer (USA) (Proto 1).gba",
            "Puzzle (USA, Europe).gba", "Puzzle (USA, Europe) (v1.1).gba", "Puzzle (USA, Europe) (v1.10).gba",
            "Puzzle (USA, Europe) (v1.9).gba", "Solo (Japan).gba",
        ]
        out = {n: self.add(n) for n in names}
        self.add("Solo (Japan) (Rev 1).gba")  # in the DAT, but not present locally
        return out

    def test_latest_only_moves_older_versions_and_off_brings_them_back(self) -> None:
        local = self._versions()
        for i, (name, data) in enumerate(local.items()):
            self.write(f"in/{i}.gba", data)
        ops = self.plan(latest_only=True)
        by_rom = {o.rom_name: o for o in ops.values() if o.rom_name}
        sup = self.root / SUPERSEDED_DIR
        expected = {
            "Racer (USA).gba": "Racer (USA) (Rev 2).gba",
            "Racer (USA) (Rev 1).gba": "Racer (USA) (Rev 2).gba",
            "Racer (Europe) (En,Fr,De).gba": "Racer (Europe) (En,Fr,De) (Rev A).gba",
            "Puzzle (USA, Europe).gba": "Puzzle (USA, Europe) (v1.10).gba",
            "Puzzle (USA, Europe) (v1.1).gba": "Puzzle (USA, Europe) (v1.10).gba",
            "Puzzle (USA, Europe) (v1.9).gba": "Puzzle (USA, Europe) (v1.10).gba",
        }
        for name, op in by_rom.items():
            if name in expected:
                self.assertEqual(op.superseded_by, expected[name][:-4], name)  # set name
                self.assertEqual(op.dst, sup / op.src.relative_to(self.root))  # keeps its own file name
                self.assertEqual(op.code, "superseded")
                self.assertTrue(op.unmatched)
                self.assertTrue(op.reason.startswith(f"superseded by {expected[name][:-4]}"), op.reason)
            else:  # newest, other region, Beta / Proto (own groups), newest local (Solo Rev 1 absent)
                self.assertEqual(op.superseded_by, "", name)
                self.assertEqual(op.dst, self.root / name)
        counts = plan_counts(ops.values())
        self.assertEqual(counts["to_superseded"], len(expected))
        self.assertEqual(counts["to_superseded"], len(expected))
        self.assertEqual(counts["to_unmatched"], 0)

        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual(res["failed"], [])
        index = {n: i for i, n in enumerate(local)}
        self.assertEqual(self.files(), {f"{SUPERSEDED_DIR}/in/{index[n]}.gba" if n in expected else n
                                        for n in local})
        self.assertEqual({o.status for o in self.plan(latest_only=True).values()}, {"ok"})

        # Turning the option off: superseded files go back to their canonical place.
        ops2 = self.plan(latest_only=False)
        for name in expected:
            op = ops2[f"{SUPERSEDED_DIR}/in/{index[name]}.gba"]
            self.assertEqual((op.status, op.dst, op.superseded_by), ("move", self.root / name, ""))
        self.assertEqual(plan_counts(ops2.values())["to_superseded"], 0)
        res2 = apply_renames(list(ops2.values()), self.root)
        self.assertEqual(res2["failed"], [])
        self.assertEqual(self.files(), set(local))
        self.assertNotIn(f"{SUPERSEDED_DIR}", self.dirs())

        # both applies undo cleanly, newest first
        undo(Path(res2["undo_log"]))
        undo(Path(res["undo_log"]))
        self.assertEqual(self.files(), {f"in/{i}.gba" for i in range(len(local))})

    def test_latest_only_nes_keeps_the_headered_copy(self) -> None:
        # a newer headerless .unh never sends away the only (iNES-headered) playable copy
        self.dat_name = NES
        hdr = b"NES\x1a" + bytes(12)
        p_old, p_new, q_old, q_new = (self.data(2048) for _ in range(4))
        self.add("Nemo (USA).nes", hdr + p_old)
        self.add("Nemo (USA).unh", p_old)
        self.add("Nemo (USA) (Rev 1).nes", hdr + p_new)
        self.add("Nemo (USA) (Rev 1).unh", p_new)
        self.add("Quest (Europe).nes", hdr + q_old)
        self.add("Quest (Europe) (Rev 1).nes", hdr + q_new)
        self.add("Quest (Europe).unh", q_old)
        self.add("Quest (Europe) (Rev 1).unh", q_new)
        self.write("nemo.nes", hdr + p_old)           # old, headered
        self.write("nemo r1.unh", p_new)              # new, headerless
        self.write("quest.nes", hdr + q_old)          # old + new headered: superseded as usual
        self.write("quest r1.nes", hdr + q_new)
        self.write("quest.unh", q_old)                # old + new headerless: superseded as usual
        self.write("quest r1.unh", q_new)
        ops = self.plan(latest_only=True)
        sup = self.root / SUPERSEDED_DIR
        self.assertEqual((ops["nemo.nes"].dst, ops["nemo.nes"].superseded_by), (self.root / "Nemo (USA).nes", ""))
        self.assertEqual(ops["nemo r1.unh"].dst, self.root / "Nemo (USA) (Rev 1).unh")
        self.assertEqual((ops["quest.nes"].dst, ops["quest.nes"].superseded_by),
                         (sup / "quest.nes", "Quest (Europe) (Rev 1)"))
        self.assertEqual((ops["quest.unh"].dst, ops["quest.unh"].superseded_by),
                         (sup / "quest.unh", "Quest (Europe) (Rev 1)"))
        # a headered dump matched with its header skipped (via the .unh rom) is a headered copy
        unh = _nrom("X (USA).unh", b"x", NES)
        self.assertEqual(organiser._header_form(unh, "headerless"), "")
        self.assertEqual(organiser._header_form(unh, "raw"), "headerless")

    def test_latest_only_when_newest_vanished(self) -> None:
        old, new = self.add("Racer (USA).gba"), self.add("Racer (USA) (Rev 1).gba")
        self.write("Racer (USA) (Rev 1).gba", new)
        self.write(f"{SUPERSEDED_DIR}/Racer (USA).gba", old)
        ops = self.plan(latest_only=True)
        self.assertEqual(ops[f"{SUPERSEDED_DIR}/Racer (USA).gba"].status, "ok")
        (self.root / "Racer (USA) (Rev 1).gba").unlink()  # the newer version is gone
        ops = self.plan(latest_only=True)
        op = ops[f"{SUPERSEDED_DIR}/Racer (USA).gba"]
        self.assertEqual((op.status, op.dst), ("move", self.root / "Racer (USA).gba"))

    def test_latest_only_never_overwrites_in_superseded(self) -> None:
        old, new = self.add("A (USA).gba"), self.add("A (USA) (Rev 1).gba")
        old2, new2 = self.add("B (USA).gba"), self.add("B (USA) (Rev 1).gba")
        self.write("A (USA).gba", old)
        self.write("A (USA) (Rev 1).gba", new)
        self.write(f"{SUPERSEDED_DIR}/A (USA).gba", b"something else")  # unmatched file
        self.write("B (USA).gba", old2)
        self.write("B (USA) (Rev 1).gba", new2)
        ops = self.plan(latest_only=True)
        # the spare takes a free name next to the stranger instead of getting stuck
        self.assertEqual(ops["A (USA).gba"].status, "move")
        self.assertEqual(ops["A (USA).gba"].dst.name, "A (USA) (2).gba")
        self.assertEqual(ops["B (USA).gba"].status, "move")
        res = apply_renames(list(ops.values()), self.root)
        self.assertEqual((self.root / SUPERSEDED_DIR / "A (USA).gba").read_bytes(),
                         b"something else")
        self.assertEqual((self.root / SUPERSEDED_DIR / "A (USA) (2).gba").read_bytes(), old)
        self.assertFalse((self.root / "A (USA).gba").exists())
        self.assertEqual(res["moved"], 2)

    def test_latest_only_tosec_per_dat(self) -> None:
        self.dat_name = GAMES
        names = ["Game v1.2 (1990)(Pub).adf", "Game v1.10 (1990)(Pub).adf", "Game (1989)(Pub).adf",
                 "Game v1.0 (1990)(Pub)[cr X].adf", "Game v1.1 (1990)(Pub)[cr X].adf",
                 "Game v1.0 (1990)(Other).adf"]
        local = {n: self.add(n, tosec=True) for n in names}
        for i, d in enumerate(local.values()):
            self.write(f"{i}.adf", d)
        ops = plan_renames(self.scan("per_dat"), latest_only=True)  # layout from the result
        by_rom = {o.rom_name: o for o in ops}
        sup = self.root / SUPERSEDED_DIR
        self.assertEqual(by_rom["Game v1.2 (1990)(Pub).adf"].dst, sup / "0.adf")  # keeps its own file name
        for old in ("Game v1.2 (1990)(Pub).adf", "Game (1989)(Pub).adf", "Game v1.0 (1990)(Pub)[cr X].adf"):
            self.assertEqual(by_rom[old].code, "superseded", old)
            self.assertTrue(by_rom[old].superseded_by, old)
        for keep in ("Game v1.10 (1990)(Pub).adf", "Game v1.1 (1990)(Pub)[cr X].adf", "Game v1.0 (1990)(Other).adf"):
            self.assertEqual(by_rom[keep].dst, self.root / GAMES / keep)
            self.assertEqual(by_rom[keep].superseded_by, "")
        # without the option nothing is superseded (Amiga behaviour unchanged)
        self.assertFalse(any(o.superseded_by for o in plan_renames(self.scan("per_dat"))))

    # converted originals ------------------------------------------------------

    def test_converted_originals_are_left_alone(self) -> None:
        self.dat_name = SNES
        clean = self.data(2048)
        self.add("Super Game (USA).sfc", clean)
        old = self.add("Super Game (USA) (Rev 1).sfc")
        self.write("Super Game (USA).sfc", clean)
        self.write(f"{CONVERTED_DIR}/sub/sg.sfc", clean)  # matched raw copy
        self.write(f"{CONVERTED_DIR}/x.smc", b"headered original")
        self.write(f"{CONVERTED_DIR}/rev1.sfc", old)
        for latest in (False, True):
            ops = self.plan(latest_only=latest)
            for rel in ("sub/sg.sfc", "x.smc", "rev1.sfc"):
                op = ops[f"{CONVERTED_DIR}/{rel}"]
                self.assertEqual((op.status, op.dst, op.reason, op.unmatched),
                                 ("ok", op.src, "original kept by Convert", True))
            self.assertEqual(ops["Super Game (USA).sfc"].status, "ok")  # Rev 1 there doesn't count
            self.assertEqual(ops["Super Game (USA).sfc"].superseded_by, "")


class CreateRecordUndoTests(unittest.TestCase):
    """Undo log v3 ``create`` records (written by convert.py through the public aliases)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _convert_like(self, original: bytes, clean: bytes, crash_before_create: bool = False) -> Path:
        """Mimic apply_conversions: move the original away, then create the clean file."""
        root = self.root
        src = root / "game.smc"
        src.write_bytes(original)
        orig_dst = root / CONVERTED_DIR / "game.smc"
        dst = root / "Game (USA).sfc"
        journal = organiser.Journal(root)
        created: list[str] = []
        organiser.make_dirs(orig_dst.parent, created, journal)
        journal.record({"op": "move", "src": organiser.rel_str(src, root), "dst": organiser.rel_str(orig_dst, root)})
        organiser.rename_no_overwrite(src, orig_dst)
        journal.useful = True
        tmp = organiser.temp_name(dst)
        tmp.write_bytes(clean)
        seq = journal.record({"op": "create", "path": organiser.rel_str(dst, root),
                              "sha1": hashlib.sha1(clean).hexdigest(), "size": len(clean)})
        if crash_before_create:
            journal.failed(seq)
            tmp.unlink()
        else:
            organiser.move_exclusive(tmp, dst)
        log = journal.close()
        assert log is not None
        return log

    def test_read_log_steps_and_created_files(self) -> None:
        log = self._convert_like(b"H" * 512 + b"rom", b"rom")
        info = organiser.read_undo_log(log)
        self.assertEqual([s["op"] for s in info["steps"]], ["move", "create"])
        self.assertEqual(len(info["moves"]), 1)
        self.assertEqual(info["created_files"], [{"path": self.root / "Game (USA).sfc",
                                                  "sha1": hashlib.sha1(b"rom").hexdigest(), "size": 3}])

    def test_undo_removes_created_file_and_restores_original(self) -> None:
        log = self._convert_like(b"H" * 512 + b"rom", b"rom")
        u = undo(log)
        self.assertEqual((u["created_removed"], u["restored"], u["remaining"]), (1, 1, 0), u)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if not p.name.startswith(".romorg")),
                         ["game.smc"])
        self.assertEqual((self.root / "game.smc").read_bytes(), b"H" * 512 + b"rom")
        self.assertEqual(list_undo_logs(self.root), [])

    def test_failed_create_is_ignored(self) -> None:
        log = self._convert_like(b"orig", b"rom", crash_before_create=True)
        self.assertEqual(organiser.read_undo_log(log)["created_files"], [])
        u = undo(log)
        self.assertEqual((u["created_removed"], u["restored"]), (0, 1))

    def test_changed_created_file_is_kept_and_log_kept_for_retry(self) -> None:
        log = self._convert_like(b"orig", b"rom")
        (self.root / "Game (USA).sfc").write_bytes(b"edited by the user")
        u = undo(log)
        self.assertEqual(u["created_removed"], 0)
        self.assertEqual(u["remaining"], 1)
        self.assertTrue(any(s["reason"] == "changed since it was created" for s in u["skipped"]))
        self.assertEqual((self.root / "Game (USA).sfc").read_bytes(), b"edited by the user")
        self.assertEqual((self.root / "game.smc").read_bytes(), b"orig")  # the move was still undone
        info = organiser.read_undo_log(log)  # rewritten log keeps the create record
        self.assertEqual([s["op"] for s in info["steps"]], ["create"])
        # once the user restores the content, a retry removes it
        (self.root / "Game (USA).sfc").write_bytes(b"rom")
        u2 = undo(log)
        self.assertEqual((u2["created_removed"], u2["remaining"]), (1, 0))
        self.assertFalse((self.root / "Game (USA).sfc").exists())

    def test_already_gone_created_file_is_skipped(self) -> None:
        log = self._convert_like(b"orig", b"rom")
        (self.root / "Game (USA).sfc").unlink()
        u = undo(log)
        self.assertEqual((u["created_removed"], u["restored"], u["remaining"]), (0, 1, 0))
        self.assertTrue(any(s["reason"] == "already gone" for s in u["skipped"]))

    def test_create_record_outside_the_folder_is_rejected(self) -> None:
        log = self.root / ".romorg-undo-20260101-000000.json"
        outside = Path(self.tmp.name).parent / "victim.bin"
        lines = [{"version": 3, "root": str(self.root)},
                 {"op": "create", "path": "../victim.bin", "sha1": "a" * 40, "size": 1, "i": 1},
                 {"op": "create", "path": str(outside), "sha1": "a" * 40, "size": 1, "i": 2},
                 {"op": "create", "path": "ok.bin", "sha1": 5, "size": 1, "i": 3},
                 {"op": "end"}]
        log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        info = organiser.read_undo_log(log)
        self.assertEqual(info["rejected"], 3)
        self.assertEqual(info["created_files"], [])

    def test_case_insensitive_flat_rename(self) -> None:
        # exFAT-style: a case-only rename of a No-Intro file in a flat root goes via a temp name.
        d = _rand(300, 9)
        (self.root / "alpha (usa).gba").write_bytes(d)
        with mock.patch.object(scanner, "find_7z", return_value=None):
            res = scanner.scan(self.root, [DatFile(GBA, "", "", [_nrom("Alpha (USA).gba", d)])], use_cache=False)
        real_rename = os.rename
        calls: list[str] = []

        def ci_exists(p: Path) -> bool:
            p = Path(p)
            try:
                return any(c.name.casefold() == p.name.casefold() for c in p.parent.iterdir())
            except OSError:
                return False

        def ci_same(a: Path, b: Path) -> bool:
            return ci_exists(a) and ci_exists(b) and str(a).casefold() == str(b).casefold()

        def spy(a, b) -> None:
            calls.append(Path(b).name)
            real_rename(a, b)

        with mock.patch.object(organiser, "_exists", ci_exists), \
                mock.patch.object(organiser, "_same_file", ci_same), \
                mock.patch.object(organiser.os, "rename", spy):
            ops = plan_renames(res, layout="flat")
            self.assertEqual((ops[0].status, ops[0].kind), ("move", "rename"))
            out = apply_renames(ops, self.root)
        self.assertEqual(out["moved"], 1, out)
        self.assertIn(".romorg-tmp-", calls[0])
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if not p.name.startswith(".romorg")),
                         ["Alpha (USA).gba"])


# --------------------------------------------------------------------------- Amendment 6: Build library

ABC1 = "ABC Monday Night Football v1.1 (1991)(Data East)(US)(Disk 1 of 3)"
ABC_NAMES = [
    ABC1 + "[cr SR].adf",                                                   # only disk 1 exists for v1.1
    "ABC Monday Night Football (1990)(Data East)(US)(Disk 2 of 3).adf",     # disks 2 + 3 only exist
    "ABC Monday Night Football (1990)(Data East)(US)(Disk 3 of 3).adf",     # under the older (1990) title
    "ABC Monday Night Football (1990)(Data East)(US)(pre-release)(Disk 2 of 3).adf",
    "Solo Game (1992)(Pub)[b corrupt file].adf",
    "Solo Game (1993)(Pub).adf",
]
ABC_PLAYLIST = "ABC Monday Night Football v1.1 (1991)(Data East)(US)[cr SR].m3u"


class LibraryPlanTests(unittest.TestCase):
    """plan_library / apply_renames(playlists=) on a synthetic Amiga library."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "amiga"
        self.root.mkdir()
        self.roms: list[Rom] = []
        self.blobs: dict[str, bytes] = {}
        self.profile = library.LibraryProfile()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def have(self, name: str, rel: str | None = None, data: bytes | None = None) -> Path:
        data = data if data is not None else _rand(600, len(self.blobs) + 7)
        self.blobs[name] = data
        self.roms.append(_rom(name, data))
        p = self.root / (rel if rel is not None else name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def scan(self) -> scanner.ScanResult:
        with mock.patch.object(scanner, "find_7z", return_value=None):
            return scanner.scan(self.root, [DatFile(GAMES, "", "", list(self.roms))], use_cache=False)

    def plan(self, **kw):
        return organiser.plan_library(self.scan(), kw.pop("profile", self.profile), **kw)

    def files(self) -> dict[str, bytes]:
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and not p.name.startswith(".romorg-undo-")}

    def build(self, **kw):
        plan = self.plan(**kw)
        res = apply_renames(plan.ops, self.root, playlists=plan.playlists)
        self.assertEqual(res["failed"], [], res)
        return plan, res

    def abc(self) -> None:
        for n in ABC_NAMES:
            self.have(n, f"in/{n}")
        self.have(ABC1 + ".adf", "in/plain1.adf")   # uncracked disk 1 of v1.1: superseded by the cracked set

    def test_abc_playlist_per_slot_and_reason_folders(self) -> None:
        self.abc()
        plan = self.plan()
        by_src = {op.src.relative_to(self.root).as_posix(): op for op in plan.ops if op.kind != "m3u"}
        rel = lambda op: op.dst.relative_to(self.root).as_posix()  # noqa: E731
        self.assertEqual(rel(by_src[f"in/{ABC_NAMES[0]}"]), f"{GAMES}/{ABC_NAMES[0]}")
        self.assertEqual(rel(by_src[f"in/{ABC_NAMES[1]}"]), f"{GAMES}/{ABC_NAMES[1]}")
        self.assertEqual(rel(by_src[f"in/{ABC_NAMES[2]}"]), f"{GAMES}/{ABC_NAMES[2]}")
        pre = by_src[f"in/{ABC_NAMES[3]}"]
        self.assertEqual((pre.code, rel(pre)), ("excluded", f"_excluded/in/{ABC_NAMES[3]}"))
        self.assertIn("pre-release", pre.flags_text)
        bad = by_src[f"in/{ABC_NAMES[4]}"]
        self.assertEqual((bad.code, bad.flags_text), ("excluded", "[b corrupt file]"))
        self.assertEqual(rel(by_src[f"in/{ABC_NAMES[5]}"]), f"{GAMES}/{ABC_NAMES[5]}")
        plain = by_src["in/plain1.adf"]
        self.assertEqual((plain.code, rel(plain)), ("superseded", "_superseded/in/plain1.adf"))
        self.assertEqual(len(plan.selection.sets), 1)
        self.assertEqual(plan.selection.sets[0].name, ABC_PLAYLIST[:-4])
        self.assertEqual(plan.selection.incomplete, [])
        self.assertEqual([(p.path.name, p.status) for p in plan.playlists], [(ABC_PLAYLIST, "write")])
        pl = plan.playlists[0]
        self.assertEqual(pl.path.parent, self.root / GAMES)
        self.assertEqual(pl.lines[0], m3u.M3U_MARKER)
        self.assertEqual(pl.lines[1:], [f"{ABC_NAMES[0]}|Disk 1", f"{ABC_NAMES[1]}|Disk 2", f"{ABC_NAMES[2]}|Disk 3"])
        rc = organiser.reason_counts(plan)
        self.assertEqual((rc["excluded"], rc["superseded"], rc["incomplete"], rc["duplicates"],
                          rc["playlists_write"], rc["playlists_remove"]), (2, 1, 0, 0, 1, 0))
        self.assertEqual(rc["kept"], 4)
        self.assertEqual(plan.counts()["to_excluded"], 2)

    def test_language_and_flag_exclusions_get_reason_counts_and_are_idempotent(self) -> None:
        for n in ("Fine (1990)(Pub).adf", "Fine (1990)(Pub)(DE)(de).adf", "Nur Deutsch (1991)(Pub)(DE).adf",
                  "Crack (1990)(Pub)[cr X].adf", "Clean (1990)(Pub).adf", "Clean (1990)(Pub)[cr Y].adf"):
            self.have(n, f"in/{n}")
        prof = dataclasses.replace(library.default_profile(platforms.get_platform("Commodore Amiga")),
                                   keep_flags=frozenset(library.tags.KEEP_FLAGS) - {"cr"})
        plan = self.plan(profile=prof)
        by = {op.src.name: op for op in plan.ops if op.kind != "m3u"}
        self.assertEqual(by["Fine (1990)(Pub)(DE)(de).adf"].reasons, ("language",))
        self.assertEqual(by["Nur Deutsch (1991)(Pub)(DE).adf"].code, "excluded")
        self.assertEqual(by["Crack (1990)(Pub)[cr X].adf"].reasons, ("flag_cr",))
        self.assertEqual(by["Crack (1990)(Pub)[cr X].adf"].flags_text, "[cr X]")
        self.assertTrue(by["Crack (1990)(Pub)[cr X].adf"].dst.as_posix().endswith("_excluded/in/Crack (1990)(Pub)[cr X].adf"))
        self.assertEqual(by["Clean (1990)(Pub)[cr Y].adf"].code, "excluded")
        self.assertEqual(by["Clean (1990)(Pub).adf"].code, "")
        rc = organiser.reason_counts(plan)
        self.assertEqual((rc["excluded"], rc["excluded_language"], rc["excluded_flag_cr"], rc["excluded_bad_dump"]),
                         (4, 2, 2, 0))
        self.assertEqual(plan.exclusion_counts()["exclusive"], {"language": 2, "flag_cr": 2})
        self.assertEqual(sorted((v.title, v.reason) for v in plan.vanished),
                         [("Crack", "flag_cr"), ("Nur Deutsch", "language")])
        self.assertEqual(plan.vanish_summary()["by_reason"], {"flag_cr": 1, "language": 1})
        res = apply_renames(plan.ops, self.root, playlists=plan.playlists)
        self.assertEqual(res["failed"], [])
        plan2 = self.plan(profile=prof)
        self.assertEqual({op.status for op in plan2.ops}, {"ok"})
        # switching the filters off brings everything back in one build
        plan3 = self.plan(profile=dataclasses.replace(prof, languages=(), keep_flags=frozenset(library.tags.KEEP_FLAGS)))
        self.assertEqual(sum(1 for op in plan3.ops if op.status == "move" and not op.unmatched), 4)

    def test_build_is_idempotent_and_undo_removes_playlists(self) -> None:
        self.abc()
        before = self.files()
        plan, res = self.build()
        self.assertEqual(res["playlists_written"], 1)
        text = (self.root / GAMES / ABC_PLAYLIST).read_text()
        self.assertTrue(text.startswith(m3u.M3U_MARKER))
        # a second build of the built library: nothing left to do
        plan2 = self.plan()
        self.assertEqual({op.status for op in plan2.ops}, {"ok"}, [(o.src.name, o.status) for o in plan2.ops])
        self.assertEqual([p.status for p in plan2.playlists], ["ok"])
        rc = organiser.reason_counts(plan2)
        self.assertEqual({k: v for k, v in rc.items() if k not in ("kept", "playlists_ok")},
                         {k: 0 for k in rc if k not in ("kept", "playlists_ok")})
        res2 = apply_renames(plan2.ops, self.root, playlists=plan2.playlists)
        self.assertEqual((res2["moved"], res2["deleted"], res2["playlists_written"], res2["undo_log"]),
                         (0, 0, 0, None))
        # one undo: the playlist the build created goes, every file is back
        u = undo(Path(res["undo_log"]))
        self.assertEqual((u["created_removed"], u["failed"]), (1, []))
        self.assertEqual(self.files(), before)
        self.assertFalse((self.root / GAMES).exists())
        self.assertFalse((self.root / "_unmatched").exists())

    def test_undo_keeps_a_playlist_the_user_changed(self) -> None:
        self.abc()
        _, res = self.build()
        pl = self.root / GAMES / ABC_PLAYLIST
        pl.write_text(pl.read_text() + "# mine\n")
        u = undo(Path(res["undo_log"]))
        self.assertEqual(u["created_removed"], 0)
        self.assertTrue(any(s["reason"] == "changed since it was created" for s in u["skipped"]))
        self.assertTrue(pl.exists())

    def test_interrupted_build_is_undoable(self) -> None:
        self.abc()
        before = self.files()
        plan = self.plan()
        n_moves = sum(1 for op in plan.ops if op.status in organiser.MOVE_STATUSES)
        n_deletes = sum(1 for op in plan.ops if op.status == "delete")
        # cancelled right before the first playlist: moves stay done and journalled, no playlist yet
        calls = []

        def cancel() -> bool:
            calls.append(1)
            return len(calls) > n_moves + n_deletes

        res = apply_renames(plan.ops, self.root, playlists=plan.playlists, cancel=cancel)
        self.assertTrue(res["cancelled"])
        self.assertEqual((res["moved"], res["playlists_written"]), (n_moves, 0))
        self.assertFalse((self.root / GAMES / ABC_PLAYLIST).exists())
        self.assertEqual(undo(Path(res["undo_log"]))["failed"], [])
        self.assertEqual(self.files(), before)

    def test_crash_while_writing_a_playlist_is_undoable(self) -> None:
        self.abc()
        before = self.files()
        plan = self.plan()

        class Crash(BaseException):
            pass

        def boom(path, text, replace_own=False):
            raise Crash()

        with mock.patch.object(m3u, "write_playlist", boom), self.assertRaises(Crash):
            apply_renames(plan.ops, self.root, playlists=plan.playlists)
        logs = list_undo_logs(self.root)
        self.assertEqual(len(logs), 1)
        log = organiser.read_undo_log(logs[0])
        self.assertEqual(len(log["created_files"]), 1)        # the create record was written first
        u = undo(logs[0])
        self.assertEqual(u["failed"], [])
        self.assertEqual(self.files(), before)

    def test_failed_playlist_write_is_reported(self) -> None:
        self.abc()
        plan = self.plan()
        (self.root / GAMES).mkdir()
        (self.root / GAMES / ABC_PLAYLIST).write_text("my own playlist\n")  # appeared after the plan
        res = apply_renames(plan.ops, self.root, playlists=plan.playlists)
        self.assertEqual(res["playlists_written"], 0)
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual((self.root / GAMES / ABC_PLAYLIST).read_text(), "my own playlist\n")

    def test_playlist_skipped_when_a_disk_move_failed(self) -> None:
        self.abc()
        plan = self.plan()
        blocker = self.root / GAMES / ABC_NAMES[1]
        blocker.parent.mkdir(parents=True)
        blocker.write_bytes(b"appeared later")                 # disk 2's target is taken
        res = apply_renames(plan.ops, self.root, playlists=plan.playlists)
        self.assertEqual(res["playlists_written"], 0)
        self.assertTrue(any("disk not at its planned path" in f["error"] for f in res["failed"]), res["failed"])

    def test_stale_playlist_is_deleted_and_undo_restores_it(self) -> None:
        self.abc()
        old = self.root / "Old.m3u"
        old.write_text(m3u.M3U_MARKER + f"\nin/{ABC_NAMES[0]}\nin/{ABC_NAMES[1]}\n")
        plan = self.plan()
        deletes = [op for op in plan.ops if op.status == "delete"]
        self.assertEqual([op.src for op in deletes], [old])
        self.assertEqual(organiser.reason_counts(plan)["playlists_remove"], 1)
        res = apply_renames(plan.ops, self.root, playlists=plan.playlists)
        self.assertEqual((res["deleted"], res["playlists_written"], res["failed"]), (1, 1, []))
        self.assertFalse(old.exists())
        u = undo(Path(res["undo_log"]))
        self.assertEqual((u["m3us_restored"], u["created_removed"]), (1, 1))
        self.assertTrue(old.read_text().startswith(m3u.M3U_MARKER))

    def test_rewritten_playlist_is_one_write_and_undo_restores_the_old_text(self) -> None:
        self.abc()
        self.build()
        pl = self.root / GAMES / ABC_PLAYLIST
        old_text = pl.read_text()
        plan = self.plan(savedisk=True)   # same name, other content
        self.assertEqual([(p.path, p.status) for p in plan.playlists], [(pl, "write")])
        self.assertEqual([op for op in plan.ops if op.status == "delete"], [])
        res = apply_renames(plan.ops, self.root, playlists=plan.playlists)
        self.assertEqual((res["playlists_written"], res["failed"]), (1, []))
        self.assertIn("#SAVEDISK:", pl.read_text())
        undo(Path(res["undo_log"]))
        self.assertEqual(pl.read_text(), old_text)

    def test_user_playlist_is_never_replaced(self) -> None:
        self.abc()
        (self.root / GAMES).mkdir()
        (self.root / GAMES / ABC_PLAYLIST).write_text("mine\n")
        plan = self.plan()
        self.assertEqual([p.status for p in plan.playlists], ["conflict"])
        res = apply_renames(plan.ops, self.root, playlists=plan.playlists)
        self.assertEqual(res["playlists_written"], 0)
        self.assertEqual((self.root / GAMES / ABC_PLAYLIST).read_text(), "mine\n")

    def test_incomplete_set_goes_to_incomplete_folder(self) -> None:
        self.have("Lonely (1990)(Pub)(Disk 1 of 3).adf", "x/l1.adf")
        self.have("Lonely (1990)(Pub)(Disk 3 of 3).adf", "x/l3.adf")
        plan = self.plan()
        ops = {op.src.name: op for op in plan.ops}
        for n in ("l1.adf", "l3.adf"):
            self.assertEqual((ops[n].code, ops[n].missing), ("incomplete", (2,)))
            self.assertEqual(ops[n].dst, self.root / "_incomplete" / "x" / n)
        self.assertEqual(plan.playlists, [])
        self.assertEqual([(i.name, i.missing) for i in plan.selection.incomplete],
                         [("Lonely (1990)(Pub)", (2,))])
        self.build()
        self.assertEqual({op.status for op in self.plan().ops}, {"ok"})

    def test_move_back_when_a_rule_is_switched_off(self) -> None:
        self.abc()
        self.build()
        self.assertTrue((self.root / "_excluded/in" / ABC_NAMES[3]).exists())
        relaxed = library.LibraryProfile(exclude=frozenset(), latest_only=True, best_variant=True,
                                         complete_only=True)
        plan = self.plan(profile=relaxed)
        ops = {op.src.name: op for op in plan.ops if op.kind != "m3u"}
        self.assertEqual(ops[ABC_NAMES[3]].code, "incomplete")   # no longer excluded: a lone disk 2
        res = apply_renames(plan.ops, self.root, playlists=plan.playlists)
        self.assertEqual(res["failed"], [])
        self.assertFalse((self.root / "_excluded").exists())  # emptied folders are removed
        self.assertTrue((self.root / "_incomplete/in" / ABC_NAMES[3]).exists())
        self.assertEqual({op.status for op in self.plan(profile=relaxed).ops}, {"ok"})

    def test_duplicates_scan_summary_matches_plan(self) -> None:
        d = _rand(600, 5)
        self.have("Copy (1990)(Pub).adf", "Copy (1990)(Pub).adf", d)                  # loose, in the root
        for rel in ("a/b/copy.adf", "z.adf"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_bytes(d)
        with zipfile.ZipFile(self.root / "c.zip", "w") as zf:
            zf.writestr("whatever.adf", d)
        res = self.scan()
        plan = organiser.plan_library(res, self.profile)
        n = organiser.plan_counts(plan.ops)["to_duplicates"]
        self.assertEqual(n, 3)
        self.assertEqual(res.summary()["duplicates"], n)
        # keeper: nothing is canonical, no archive, same depth -> the shortest path wins
        self.assertEqual({op.keeper for op in plan.ops if op.code == "duplicate"}, {"z.adf"})
        self.build()
        s = self.scan().summary()
        self.assertEqual((s["duplicates"], s["duplicates_set_aside"]), (0, 3))
        self.assertEqual({op.status for op in self.plan().ops}, {"ok"})

    def test_spare_with_a_taken_reason_target_gets_a_free_name(self) -> None:
        d = _rand(600, 31)
        self.have("Solo (1990)(Pub).adf", "Solo (1990)(Pub).adf", d)
        (self.root / "copies").mkdir()
        (self.root / "copies" / "again.adf").write_bytes(d)
        self.build()
        dup = self.root / "_duplicates/copies/again.adf"
        self.assertTrue(dup.is_file())
        # the same bytes show up again under the same relative path
        (self.root / "copies").mkdir(exist_ok=True)
        (self.root / "copies" / "again.adf").write_bytes(d)
        res = self.scan()
        plan = organiser.plan_library(res, self.profile)
        moves = [op for op in plan.ops if op.code == "duplicate" and op.status in organiser.MOVE_STATUSES]
        self.assertEqual([m.dst.relative_to(self.root).as_posix() for m in moves],
                         ["_duplicates/copies/again (2).adf"])
        self.assertEqual(organiser.plan_counts(plan.ops)["to_duplicates"], 1)
        self.assertEqual(organiser.reason_counts(plan)["duplicates"], 1)
        self.assertEqual(res.summary()["duplicates"], 1)
        self.build()
        self.assertTrue((self.root / "_duplicates/copies/again (2).adf").is_file())
        self.assertFalse((self.root / "copies" / "again.adf").exists())
        self.assertEqual({op.status for op in self.plan().ops}, {"ok"})     # settled in one round
        # different content at an occupied reason path (a second bad dump) is moved aside too
        self.have("Bad (1990)(Pub)[b].adf", "x/bad.adf")
        self.have("Bad (1991)(Pub)[b].adf", "y/bad.adf")
        self.build()
        self.assertEqual(sorted(p.name for p in (self.root / "_excluded").rglob("*.adf")),
                         ["bad.adf", "bad.adf"])
        self.assertEqual({op.status for op in self.plan().ops}, {"ok"})

    def test_symlinked_disk_still_fills_its_slot(self) -> None:
        d1 = self.have("Duo (1990)(Pub)(Disk 1 of 2).adf", "real/d1.adf")
        data = _rand(600, 77)
        self.roms.append(_rom("Duo (1990)(Pub)(Disk 2 of 2).adf", data))
        outside = self.root.parent / "outside"        # the real disk 2 lives outside the folder
        outside.mkdir(exist_ok=True)
        (outside / "d2.adf").write_bytes(data)
        try:
            os.symlink(outside / "d2.adf", self.root / "d2link.adf")
        except OSError:
            self.skipTest("symlinks unavailable")
        plan = self.plan()
        by = {op.src.name: op for op in plan.ops if op.kind != "m3u"}
        self.assertEqual(by["d2link.adf"].status, "skip")
        self.assertIn("d2link.adf", by)
        self.assertEqual(by["d1.adf"].code, "")                # disk 1 is NOT sent to _incomplete
        self.assertEqual(plan.selection.incomplete, [])
        self.assertEqual(len(plan.selection.sets), 1)

    def test_playlist_removal_reason_names_the_missing_disk(self) -> None:
        self.abc()
        self.build()
        (self.root / GAMES / ABC_NAMES[2]).unlink()          # disk 3 is gone
        plan = self.plan()
        dels = [op for op in plan.ops if op.status == "delete"]
        self.assertEqual(len(dels), 1)
        self.assertIn("set incomplete (missing disk 3)", dels[0].reason)
        self.assertIn("undo restores it", dels[0].reason)
        self.assertNotIn("write the M3Us again", dels[0].reason)

    def test_loose_beats_archive_then_shortest_then_alphabetical(self) -> None:
        d = _rand(600, 9)
        self.have("Pick (1990)(Pub).adf", "aa/Pick (1990)(Pub).adf", d)
        with zipfile.ZipFile(self.root / "p.zip", "w") as zf:      # shorter path, but an archive
            zf.writestr("m.adf", d)
        (self.root / "bb").mkdir()
        (self.root / "bb" / "pick.adf").write_bytes(d)             # loose, and a shorter path than aa/Pick ...
        ops = {op.src.relative_to(self.root).as_posix(): op for op in plan_renames(self.scan())}
        keepers = {op.keeper for op in ops.values() if op.code == "duplicate"}
        self.assertEqual(keepers, {"bb/pick.adf"})   # loose, 2 path parts, shorter than aa/Pick...
        self.assertEqual(ops["bb/pick.adf"].dst, self.root / GAMES / "Pick (1990)(Pub).adf")

    def test_canonical_copy_is_kept_even_when_another_path_is_shorter(self) -> None:
        d = _rand(600, 6)
        self.have("Copy (1990)(Pub).adf", f"{GAMES}/Copy (1990)(Pub).adf", d)
        (self.root / "x.adf").write_bytes(d)
        ops = {op.src.name: op for op in self.plan().ops}
        self.assertEqual(ops["Copy (1990)(Pub).adf"].status, "ok")
        self.assertEqual((ops["x.adf"].code, ops["x.adf"].dst),
                         ("duplicate", self.root / "_duplicates/x.adf"))

    def test_playlist_of_zipped_disks_points_at_the_zips(self) -> None:
        for n in ABC_NAMES[:3]:
            data = _rand(600, len(self.blobs) + 70)
            self.blobs[n] = data
            self.roms.append(_rom(n, data))
            with zipfile.ZipFile(self.root / f"{len(self.blobs)}.zip", "w") as zf:
                zf.writestr("disk.adf", data)
        plan = self.plan()
        self.assertEqual([p.status for p in plan.playlists], ["write"])
        self.assertEqual(plan.playlists[0].lines[1:],
                         [f"{n[:-4]}.zip|Disk {i}" for i, n in enumerate(ABC_NAMES[:3], 1)])
        _, res = self.build()
        self.assertEqual(res["playlists_written"], 1)
        self.assertEqual({op.status for op in self.plan().ops}, {"ok"})

    def test_symlinks_are_left_alone(self) -> None:
        real = self.have("Linked (1990)(Pub).adf", "real/Linked (1990)(Pub).adf")
        link = self.root / "link.adf"
        try:
            os.symlink(real, link)
        except OSError:
            self.skipTest("symlinks unavailable")
        ops = {op.src.name: op for op in self.plan().ops}
        self.assertEqual((ops["link.adf"].status, ops["link.adf"].code), ("skip", ""))
        self.assertEqual(ops["Linked (1990)(Pub).adf"].dst, self.root / GAMES / "Linked (1990)(Pub).adf")

    def test_nointro_latest_per_region_and_exclusions(self) -> None:
        names = ["Racer (USA).gba", "Racer (USA) (Rev 1).gba", "Racer (Europe).gba", "Racer (USA) (Beta).gba",
                 "Racer (USA) (Unl).gba"]
        roms = []
        for i, n in enumerate(names):
            data = _rand(300, 50 + i)
            roms.append(_nrom(n, data))
            (self.root / f"in{i}.gba").write_bytes(data)
        with mock.patch.object(scanner, "find_7z", return_value=None):
            res = scanner.scan(self.root, [DatFile(GBA, "", "", roms)], use_cache=False, layout="flat")
        prof = dataclasses.replace(library.default_profile(
            platforms.get_platform("Nintendo Game Boy Advance")), one_per_game=False)   # per-region latest
        plan = organiser.plan_library(res, prof)
        by = {op.src.name: op for op in plan.ops}
        self.assertEqual(by["in0.gba"].code, "superseded")        # Rev 1 is newer in the same region
        self.assertEqual(by["in1.gba"].dst, self.root / "Racer (USA) (Rev 1).gba")
        self.assertEqual(by["in2.gba"].dst, self.root / "Racer (Europe).gba")   # other region kept
        self.assertEqual(by["in3.gba"].code, "excluded")           # beta
        self.assertEqual(by["in4.gba"].dst, self.root / "Racer (USA) (Unl).gba")  # Unl kept
        self.assertEqual(plan.playlists, [])

    def test_legacy_latest_only_flag_maps_to_the_profile(self) -> None:
        self.have("Game v1.0 (1990)(Pub).adf")
        self.have("Game v1.1 (1990)(Pub).adf")
        ops = plan_renames(self.scan(), latest_only=True)
        self.assertEqual(sorted(o.code for o in ops), ["", "superseded"])
        self.assertEqual([o.code for o in plan_renames(self.scan())], ["", ""])

    def test_reason_destination(self) -> None:
        r = self.root
        rd = organiser.reason_destination
        self.assertEqual(rd(r / "a/b.adf", r, "_excluded"), r / "_excluded/a/b.adf")
        self.assertEqual(rd(r / "_unmatched/x.adf", r, "_excluded"), r / "_excluded/x.adf")
        self.assertEqual(rd(r / "_excluded/a/b.adf", r, "_excluded"), r / "_excluded/a/b.adf")
        self.assertEqual(rd(r / "_superseded/a/b.adf", r, "_duplicates"),
                         r / "_duplicates/a/b.adf")
        self.assertEqual(rd(r / "b.adf", r, "_incomplete"), r / "_incomplete/b.adf")
        self.assertEqual(rd(r / "_UNMATCHED/_Excluded/a.adf", r, "_superseded"), r / "_superseded/a.adf")
        with self.assertRaises(ValueError):
            rd(Path("/elsewhere/a.adf"), r, "_excluded")


if __name__ == "__main__":
    unittest.main()
