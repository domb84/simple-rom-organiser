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

    def test_a_build_without_an_archive_is_refused(self) -> None:
        root = self.tmp / "GBA"
        put(root / "what.gba", b"unknown")
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        code, out = self.http("POST", "/api/library/apply", {})
        self.assertEqual((code, out.get("code")), (409, "archive_required"))
        self.assertEqual(tree(root), ["what.gba"])




if __name__ == "__main__":
    unittest.main()
