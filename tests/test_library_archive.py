"""One system's Build library in place, with an archive folder for what the rules set aside."""

from __future__ import annotations

import os
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
                         ["gba/_duplicates/run copy.gba", "gba/_excluded/old leftover.gba", "gba/_unmatched/what.gba"])


    def test_a_build_cleans_up_after_a_run_that_was_killed(self) -> None:
        root = self.tmp / "GBA"
        put(root / "_converted_originals" / "sub" / "run.gba", self.rom["Run (USA)"])   # moved away by a conversion that never finished
        put(root / "Run (USA).gba.romorg-convert-abcd1234", b"half written")           # its partial output
        put(root / "in" / "x.gba.romorg-tmp-0123abcd", b"unknown")                       # a move cut short: renamed back, then sorted
        put(root / "Big.gba.romorg.part", b"part")
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        res = self.job("/api/library/apply", {})
        self.assertEqual(res["recovered"], {"deleted": 2, "renamed": 1, "originals_back": 1, "left": []})
        self.assertEqual(tree(root), ["Run (USA).gba", "_unmatched/", "_unmatched/in/", "_unmatched/in/x.gba"])
        self.assertEqual((root / "Run (USA).gba").read_bytes(), self.rom["Run (USA)"])

    def test_a_build_with_nothing_left_behind_says_nothing_about_it(self) -> None:
        root = self.tmp / "GBA"
        put(root / "run.gba", self.rom["Run (USA)"])
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        self.assertNotIn("recovered", self.job("/api/library/apply", {}))




if __name__ == "__main__":
    unittest.main()
