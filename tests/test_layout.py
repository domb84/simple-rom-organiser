"""Amendment 9: the reserved folders are siblings under the platform root; legacy-layout migration."""

from __future__ import annotations

import hashlib
import json
import random
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from romorg import convert, folders, library, organiser, scanner
from romorg.datfile import DatFile, Rom

GAMES = "Commodore Amiga - Games - [ADF]"
ALPHA = "Alpha (1990)(Pub).adf"
BAD = "Bad (1990)(Pub)[b corrupt file].adf"
BETA = "Beta (1990)(Pub)(beta).adf"
OLD, NEW = "Delta v1.0 (1990)(Pub).adf", "Delta v1.1 (1991)(Pub).adf"
DISK1 = "Multi (1992)(Pub)(Disk 1 of 2).adf"
REASON_OF = {BAD: "_excluded", BETA: "_excluded", OLD: "_superseded", DISK1: "_incomplete"}


def _rom(name: str, data: bytes) -> Rom:
    return Rom(name=name, size=len(data), crc=f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
               md5=hashlib.md5(data).hexdigest(), sha1=hashlib.sha1(data).hexdigest(),
               game=name.rsplit(".", 1)[0], dat=GAMES)


def _snapshot(root: Path) -> tuple[dict[str, bytes], set[str]]:
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
             if p.is_file() and not p.name.startswith(".romorg-undo-")}
    dirs = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir()}
    return files, dirs


class FoldersModuleTests(unittest.TestCase):
    def test_reserved_names_are_one_constant(self) -> None:
        self.assertEqual(set(folders.RESERVED_DIRS), {"_unmatched", "_excluded", "_superseded", "_incomplete",
                                                      "_duplicates", "_converted_originals", "_saves"})
        self.assertIs(organiser.RESERVED_DIRS, folders.RESERVED_DIRS)
        self.assertIs(organiser.REASON_DIRS, folders.REASON_DIRS)
        self.assertIs(scanner.RESERVED_DIRS, folders.RESERVED_DIRS)
        self.assertIs(scanner.REASON_DIRS, folders.REASON_DIRS)

    def test_classify(self) -> None:
        c = folders.classify
        self.assertEqual(c(("_excluded", "a", "b.adf")), ("_excluded", False))
        self.assertEqual(c(("_Excluded", "b.adf")), ("_excluded", False))        # case-insensitive (exFAT)
        self.assertEqual(c(("_UNMATCHED", "_Excluded", "a", "b.adf")), ("_excluded", True))
        self.assertEqual(c(("_unmatched", "x", "b.adf")), ("_unmatched", False))
        self.assertEqual(c(("_unmatched", "_duplicates")), ("_unmatched", False))  # a file with that name
        self.assertIsNone(c(("_excluded",)))                                       # a root file
        self.assertIsNone(c(("games", "_excluded", "b.adf")))                      # only the top level counts
        self.assertEqual(folders.core_parts(("_unmatched", "_superseded", "a", "b")), ["a", "b"])
        self.assertEqual(folders.core_parts(("_superseded", "a", "b")), ["a", "b"])
        self.assertEqual(folders.core_parts(("_unmatched", "a", "b")), ["a", "b"])
        self.assertEqual(folders.core_parts(("a", "b")), ["a", "b"])


class LayoutBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "amiga"
        self.root.mkdir()
        self.data = {n: random.Random(i).randbytes(700) for i, n in enumerate(
            (ALPHA, BAD, BETA, OLD, NEW, DISK1, "Multi (1992)(Pub)(Disk 2 of 2).adf"))}
        self.dat = DatFile(GAMES, "", "", [_rom(n, d) for n, d in self.data.items()])
        self.profile = library.LibraryProfile()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def put(self, rel: str, name: str | None = None, data: bytes | None = None) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if data is not None else self.data[name or Path(rel).name])
        return p

    def plan(self):
        with mock.patch.object(scanner, "find_7z", return_value=None):
            res = scanner.scan(self.root, [self.dat], use_cache=False)
        return organiser.plan_library(res, self.profile)

    def build(self):
        plan = self.plan()
        out = organiser.apply_renames(plan.ops, self.root, playlists=plan.playlists)
        self.assertEqual(out["failed"], [], out)
        return plan, out

    def actions(self, plan) -> dict[str, str]:
        return {op.src.relative_to(self.root).as_posix(): op.dst.relative_to(self.root).as_posix()
                for op in plan.ops if op.status in organiser.ACTION_STATUSES and op.kind != "m3u"}


class NewLayoutTests(LayoutBase):
    def test_each_reason_gets_its_own_top_level_folder(self) -> None:
        for n in (ALPHA, BAD, BETA, OLD, NEW, DISK1):
            self.put(f"in/{n}")
        self.put("in/Alpha copy.adf", ALPHA)
        self.put("junk/readme.bin", data=b"nothing")
        plan = self.plan()
        act = self.actions(plan)
        self.assertEqual(act[f"in/{BAD}"], f"_excluded/in/{BAD}")
        self.assertEqual(act[f"in/{BETA}"], f"_excluded/in/{BETA}")
        self.assertEqual(act[f"in/{OLD}"], f"_superseded/in/{OLD}")
        self.assertEqual(act[f"in/{DISK1}"], f"_incomplete/in/{DISK1}")
        self.assertEqual(act["junk/readme.bin"], "_unmatched/junk/readme.bin")
        dups = [v for k, v in act.items() if v.startswith("_duplicates/")]
        self.assertEqual(len(dups), 1)
        self.assertEqual(act[f"in/{NEW}"], f"{GAMES}/{NEW}")
        for op in plan.ops:       # nothing is ever planned INSIDE _unmatched/ except unmatched files
            if op.code:
                self.assertNotEqual(op.dst.relative_to(self.root).parts[0], "_unmatched")
        counts = organiser.plan_counts(plan.ops)
        self.assertEqual((counts["to_unmatched"], counts["to_excluded"], counts["to_superseded"],
                          counts["to_incomplete"], counts["to_duplicates"]), (1, 2, 1, 1, 1))
        self.build()
        top = {p.name for p in self.root.iterdir() if not p.name.startswith(".")}
        self.assertEqual(top, {GAMES, "_unmatched", "_excluded", "_superseded", "_incomplete", "_duplicates"})
        self.assertEqual({p.name for p in (self.root / "_unmatched").iterdir()}, {"junk"})
        self.assertEqual([op for op in self.plan().ops if op.status in organiser.ACTION_STATUSES], [])

    def test_reserved_name_matches_case_insensitively(self) -> None:
        # a folder the user (or an exFAT card) spelled "_Excluded": its files count as set aside there
        self.put(f"_Excluded/in/{BAD}")
        self.put(f"_Excluded/in/{ALPHA}")          # qualifies for the library: goes to its canonical place
        plan = self.plan()
        act = self.actions(plan)
        self.assertEqual(act.pop(f"_Excluded/in/{ALPHA}"), f"{GAMES}/{ALPHA}")
        # still excluded: stays in "its" folder (on a case-sensitive disk only the spelling is normalised)
        self.assertEqual({k.casefold(): v.casefold() for k, v in act.items()}, {k.casefold(): k.casefold() for k in act})
        ops = {op.src.name: op for op in plan.ops}
        self.assertEqual(ops[BAD].code, "excluded")
        self.assertEqual(ops[BAD].folder, "_excluded")
        res = scanner.scan(self.root, [self.dat], use_cache=False)
        self.assertTrue(scanner.is_in_reason_dir(self.root / "_Excluded" / "in" / BAD, self.root))
        self.assertTrue(scanner.is_set_aside(self.root / "_EXCLUDED" / BAD, self.root))
        self.assertEqual(res.summary()["matched_files"], 2)

    def test_user_folder_with_a_reserved_name_is_treated_as_that_reason_folder(self) -> None:
        # design: ROMs a user keeps in _superseded/ that no rule supersedes move back to the library
        self.put(f"_superseded/{NEW}")
        self.assertEqual(self.actions(self.plan()), {f"_superseded/{NEW}": f"{GAMES}/{NEW}"})

    def test_scan_summary_ignores_set_aside_files(self) -> None:
        self.put(f"_excluded/{BAD}")
        self.put(f"_duplicates/{ALPHA}")
        self.put(f"{GAMES}/{ALPHA}")
        res = scanner.scan(self.root, [self.dat], use_cache=False)
        s = res.summary()
        self.assertEqual((s["duplicates"], s["duplicates_set_aside"], s["bad_dump_files"]), (0, 1, 0))
        self.assertEqual(s["to_rename"], 0)

    def test_plain_user_folder_is_not_reserved(self) -> None:
        self.put(f"games/_excluded/{ALPHA}")        # only TOP-level names are reserved
        self.assertEqual(self.actions(self.plan()), {f"games/_excluded/{ALPHA}": f"{GAMES}/{ALPHA}"})

    def test_plan_renames_latest_only_uses_top_level_superseded(self) -> None:
        self.put(OLD)
        self.put(NEW)
        with mock.patch.object(scanner, "find_7z", return_value=None):
            res = scanner.scan(self.root, [self.dat], use_cache=False)
        ops = {op.src.name: op for op in organiser.plan_renames(res, latest_only=True)}
        self.assertEqual(ops[OLD].dst, self.root / "_superseded" / OLD)
        self.assertEqual(ops[OLD].folder, "_superseded")


class LegacyMigrationTests(LayoutBase):
    def legacy_tree(self) -> None:
        self.put(f"_unmatched/_excluded/in/{BAD}")
        self.put(f"_unmatched/_excluded/in/{ALPHA}")                  # still qualifies: back to its place
        self.put(f"_unmatched/_excluded/stray/readme.bin", data=b"x")  # unmatched, in a legacy reason folder
        self.put(f"_unmatched/_superseded/in/{OLD}")
        self.put(f"in/{NEW}")
        self.put(f"_unmatched/_incomplete/in/{DISK1}")
        self.put(f"_unmatched/_duplicates/in/Alpha copy.adf", ALPHA)
        self.put("_unmatched/_converted_originals/sub/orig.smc", data=b"headered original")
        self.put("_unmatched/plain/loose.bin", data=b"unmatched")

    def test_legacy_files_move_to_the_new_folders_and_second_plan_is_empty(self) -> None:
        self.legacy_tree()
        before = _snapshot(self.root)
        plan = self.plan()
        act = self.actions(plan)
        self.assertEqual(act[f"_unmatched/_excluded/in/{BAD}"], f"_excluded/in/{BAD}")
        self.assertEqual(act[f"_unmatched/_excluded/in/{ALPHA}"], f"{GAMES}/{ALPHA}")
        self.assertEqual(act["_unmatched/_excluded/stray/readme.bin"], "_excluded/stray/readme.bin")
        self.assertEqual(act[f"_unmatched/_superseded/in/{OLD}"], f"_superseded/in/{OLD}")
        self.assertEqual(act[f"_unmatched/_incomplete/in/{DISK1}"], f"_incomplete/in/{DISK1}")
        self.assertEqual(act["_unmatched/_duplicates/in/Alpha copy.adf"], "_duplicates/in/Alpha copy.adf")
        self.assertEqual(act["_unmatched/_converted_originals/sub/orig.smc"], "_converted_originals/sub/orig.smc")
        self.assertNotIn("_unmatched/plain/loose.bin", act)         # a real unmatched file stays
        ops = {op.src.relative_to(self.root).as_posix(): op for op in plan.ops}
        self.assertIn("moved out of _unmatched/_excluded/ (new layout)", ops[f"_unmatched/_excluded/in/{BAD}"].reason)
        self.assertIn("moved out of _unmatched/_excluded/ (new layout)",
                      ops[f"_unmatched/_excluded/in/{ALPHA}"].reason)
        self.assertIn("moved out of _unmatched/_converted_originals/ (new layout)",
                      ops["_unmatched/_converted_originals/sub/orig.smc"].reason)
        self.assertEqual(_snapshot(self.root), before)             # planning changes nothing
        _plan, out = self.build()
        # the duplicate copy of Alpha: one keeper stays in the library, the other is set aside
        files, dirs = _snapshot(self.root)
        self.assertFalse([f for f in files if f.startswith("_unmatched/_")], files.keys())
        self.assertEqual({d for d in dirs if d.startswith("_unmatched/")}, {"_unmatched/plain"})  # empty legacy dirs gone
        self.assertTrue((self.root / "_unmatched").is_dir())
        self.assertIn("_converted_originals/sub/orig.smc", files)
        self.assertIn("_excluded/stray/readme.bin", files)
        self.assertEqual([op for op in self.plan().ops if op.status in organiser.ACTION_STATUSES], [])
        # undo puts the old tree back byte for byte (dirs included)
        log = organiser.list_undo_logs(self.root)[0]
        res = organiser.undo(Path(log["path"]) if isinstance(log, dict) else log, self.root)
        self.assertEqual(res.get("failed", []), [], res)
        self.assertEqual(_snapshot(self.root), before)

    def test_old_v3_log_from_the_legacy_layout_still_undoes(self) -> None:
        # what the previous version wrote: moves INTO _unmatched/_excluded/ and _unmatched/_converted_originals/
        self.put(f"in/{BAD}")
        self.put("sub/orig.smc", data=b"orig")
        before = _snapshot(self.root)
        steps = [("in/" + BAD, f"_unmatched/_excluded/in/{BAD}"), ("sub/orig.smc", "_unmatched/_converted_originals/sub/orig.smc")]
        lines = [{"version": 3, "root": str(self.root), "started": "2026-10-01T10:00:00"}]
        seq = 0
        for d in ("_unmatched", "_unmatched/_excluded", "_unmatched/_excluded/in", "_unmatched/_converted_originals",
                  "_unmatched/_converted_originals/sub"):
            seq += 1
            lines.append({"op": "mkdir", "path": d, "i": seq})
        for src, dst in steps:
            seq += 1
            lines.append({"op": "move", "src": src, "dst": dst, "i": seq})
            (self.root / dst).parent.mkdir(parents=True, exist_ok=True)
            (self.root / src).rename(self.root / dst)
        lines.append({"op": "end"})
        for d in ("in", "sub"):
            (self.root / d).rmdir()
        log = self.root / ".romorg-undo-20261001-100000.json"
        log.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        res = organiser.undo(log, self.root)
        self.assertEqual(res.get("failed", []), [], res)
        files, _dirs = _snapshot(self.root)
        self.assertEqual(files, before[0])
        self.assertFalse((self.root / "_unmatched").exists())

    def test_legacy_converted_originals_are_recognised_by_scan_and_convert(self) -> None:
        self.put("_unmatched/_converted_originals/sub/a.adf", ALPHA)
        res = scanner.scan(self.root, [self.dat], use_cache=False)
        p = self.root / "_unmatched" / "_converted_originals" / "sub" / "a.adf"
        self.assertTrue(scanner.is_converted_original(p, self.root))
        self.assertEqual(res.summary()["converted_originals"], 1)
        self.assertFalse(convert.plan_conversions(res))


class ConvertOriginalsLocationTests(unittest.TestCase):
    def test_original_dst_is_top_level(self) -> None:
        root = Path("/r")
        self.assertEqual(convert._original_dst(root / "sub" / "a.smc", root), root / "_converted_originals" / "sub" / "a.smc")
        self.assertEqual(convert._original_dst(root / "_unmatched" / "a.smc", root), root / "_converted_originals" / "a.smc")
        self.assertEqual(convert._original_dst(root / "_Unmatched" / "_Excluded" / "a.smc", root),
                         root / "_converted_originals" / "a.smc")


if __name__ == "__main__":
    unittest.main()
