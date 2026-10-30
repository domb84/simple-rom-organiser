"""One system's Build library in place, with an archive folder for what the rules set aside."""

from __future__ import annotations

import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from servercase import GBA, ServerCase, lower, put, tree  # noqa: E402


class LibraryWithArchive(ServerCase):
    """One system's *Build library* in place, with an archive folder for what the rules set aside."""

    def test_everything_the_build_sets_aside_goes_to_the_archive(self) -> None:
        root = self.tmp / "GBA"
        put(root / "run.gba", self.rom["Run (USA)"])
        put(root / "run copy.gba", self.rom["Run (USA)"])                          # a duplicate: archived by this build
        put(root / "_excluded" / "old leftover.gba", b"left by an earlier build")   # swept out by this build as well
        put(root / "what.gba", b"unknown")
        aside = self.tmp / "lib-archive"
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        plan = self.call("POST", "/api/library/plan", {"limit": 50, "aside_to": str(aside)})
        res = self.job("/api/library/apply", {"aside_to": str(aside), "plan_id": plan["plan_id"]})
        self.assertEqual((res["aside"]["moved"], res["aside"]["failed"]), (3, []))
        self.assertEqual(tree(root), ["Run (USA).gba"])
        self.assertEqual(lower([x for x in tree(aside) if not x.endswith("/")]),
                         ["gba/duplicates/run copy.gba", "gba/excluded/old leftover.gba", "gba/unmatched/what.gba"])


    def test_a_build_cleans_up_after_a_run_that_was_killed(self) -> None:
        root = self.tmp / "GBA"
        put(root / "_converted_originals" / "sub" / "run.gba", self.rom["Run (USA)"])   # moved away by a conversion that never finished
        put(root / "Run (USA).gba.romorg-convert-abcd1234", b"half written")           # its partial output
        put(root / "in" / "x.gba.romorg-tmp-0123abcd", b"unknown")                       # a move cut short: renamed back, then sorted
        put(root / "Big.gba.romorg.part", b"part")
        aside = self.tmp.parent / (self.tmp.name + "-archive")
        self.addCleanup(shutil.rmtree, aside, True)
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        res = self.job("/api/library/apply", {"aside_to": str(aside)})
        self.assertEqual(res["recovered"], {"deleted": 2, "renamed": 1, "originals_back": 1, "left": []})
        self.assertEqual(tree(root), ["Run (USA).gba"])
        self.assertEqual(lower([x for x in tree(aside) if not x.endswith("/")]), ["gba/unmatched/in/x.gba"])
        self.assertEqual((root / "Run (USA).gba").read_bytes(), self.rom["Run (USA)"])

    def test_a_build_with_nothing_left_behind_says_nothing_about_it(self) -> None:
        root = self.tmp / "GBA"
        put(root / "run.gba", self.rom["Run (USA)"])
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        aside = self.tmp.parent / (self.tmp.name + "-archive")
        self.addCleanup(shutil.rmtree, aside, True)
        self.assertNotIn("recovered", self.job("/api/library/apply", {"aside_to": str(aside)}))

    def test_a_file_no_database_knows_is_archived_even_when_its_place_in_unmatched_is_taken(self) -> None:
        root = self.tmp / "GBA"
        put(root / "run.gba", self.rom["Run (USA)"])
        put(root / "notes" / "what.gba", b"unknown")
        put(root / "_unmatched" / "notes" / "what.gba", b"another file at the same place")      # (its place in the library's _unmatched/ is taken)
        aside = self.tmp.parent / (self.tmp.name + "-archive")
        self.addCleanup(shutil.rmtree, aside, True)
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        self.job("/api/library/apply", {"aside_to": str(aside)})
        self.assertFalse((root / "notes" / "what.gba").exists())
        got = lower([x for x in tree(aside) if not x.endswith("/")])
        self.assertIn("gba/unmatched/notes/what.gba", got)                                      # (the archive is not that folder: it gets both)
        self.assertEqual(len([x for x in got if x.startswith("gba/unmatched/notes/")]), 2)

    def test_a_damaged_zip_matches_nothing_and_is_archived(self) -> None:
        root = self.tmp / "GBA"
        put(root / "run.gba", self.rom["Run (USA)"])
        put(root / "broken.zip", b"PK\x03\x04 this is not a zip at all")
        aside = self.tmp.parent / (self.tmp.name + "-archive")
        self.addCleanup(shutil.rmtree, aside, True)
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        self.job("/api/library/apply", {"aside_to": str(aside)})
        self.assertEqual(tree(root), ["Run (USA).gba"])
        self.assertEqual(lower([x for x in tree(aside) if not x.endswith("/")]), ["gba/unmatched/broken.zip"])

    def test_a_zip_that_holds_a_game_next_to_other_files_is_cleaned_and_the_rest_archived_as_unmatched(self) -> None:
        import zipfile
        root = self.tmp / "GBA"
        (root / "games").mkdir(parents=True)
        with zipfile.ZipFile(root / "games" / "run.zip", "w") as zf:
            zf.writestr("Run (USA).gba", self.rom["Run (USA)"])
            zf.writestr("readme.txt", "notes")
            zf.writestr("scans/cover.png", b"png")
        aside = self.tmp.parent / (self.tmp.name + "-archive")
        self.addCleanup(shutil.rmtree, aside, True)
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        res = self.job("/api/library/apply", {"aside_to": str(aside)})
        self.assertEqual(res["split"], {"zips": 1, "files": 2, "failed": []})
        zips = [p for p in root.rglob("*.zip")]
        self.assertEqual(len(zips), 1)
        with zipfile.ZipFile(zips[0]) as zf:
            self.assertEqual(zf.namelist(), ["Run (USA).gba"])                   # (the game only, as it was)
            self.assertEqual(zf.read("Run (USA).gba"), self.rom["Run (USA)"])
        got = lower([x for x in tree(aside) if not x.endswith("/")])
        self.assertEqual(got, ["gba/unmatched/games/run/readme.txt", "gba/unmatched/games/run/scans/cover.png"])
        self.assertEqual(sorted(x for x in tree(root) if "_unmatched" in x), [])      # (nothing of it is left in the library folder)

    def test_a_zip_is_left_alone_when_it_cannot_be_checked(self) -> None:
        import zipfile
        from romorg import convert
        root = self.tmp / "GBA"
        root.mkdir()
        z = root / "run.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Run (USA).gba", self.rom["Run (USA)"])
            zf.writestr("readme.txt", "notes")
        before = z.read_bytes()
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        st = self.app._scan if hasattr(self, "app") else None
        ops = convert.SplitOp(z, ["Run (USA).gba"], ["no such member"], root / "_unmatched" / "run")
        res = convert.apply_splits([ops], root)
        self.assertEqual((res["split"], len(res["failed"])), (0, 1))
        self.assertEqual(z.read_bytes(), before)                                  # (the zip is as it was)
        self.assertFalse((root / "_unmatched").exists() and any((root / "_unmatched").rglob("*.*")))

    def test_a_build_without_an_archive_is_refused(self) -> None:
        root = self.tmp / "GBA"
        put(root / "what.gba", b"unknown")
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        code, out = self.http("POST", "/api/library/apply", {})
        self.assertEqual((code, out.get("code")), (409, "archive_required"))
        self.assertEqual(tree(root), ["what.gba"])




if __name__ == "__main__":
    unittest.main()
