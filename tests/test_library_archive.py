"""One system's Build library in place, with an archive folder for what the rules set aside."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from servercase import GBA, ServerCase, lower, put, tree  # noqa: E402


class LibraryWithArchive(ServerCase):
    """One system's *Build library* in place, with an archive folder for what the rules set aside."""

    def test_undo_brings_back_everything_the_build_archived(self) -> None:
        root = self.tmp / "GBA"
        put(root / "run.gba", self.rom["Run (USA)"])
        put(root / "run copy.gba", self.rom["Run (USA)"])                          # a duplicate: archived by this build
        put(root / "_excluded" / "old leftover.gba", b"left by an earlier build")   # swept out by this build as well
        put(root / "what.gba", b"unknown")
        before = tree(root)
        aside = self.tmp / "lib-archive"
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        plan = self.call("POST", "/api/library/plan", {"limit": 50, "aside_to": str(aside)})
        res = self.job("/api/library/apply", {"aside_to": str(aside), "plan_id": plan["plan_id"]})
        self.assertEqual((res["aside"]["moved"], res["aside"]["failed"]), (3, []))
        self.assertEqual(tree(root), ["Run (USA).gba"])
        self.assertEqual(lower([x for x in tree(aside) if not x.endswith("/")]),
                         ["gba/_duplicates/run copy.gba", "gba/_excluded/old leftover.gba", "gba/_unmatched/what.gba"])
        back = self.job("/api/library/undo", {"log": res["undo_log"]})
        self.assertEqual(back["aside_restored"], 3)
        self.assertEqual(tree(root), before)
        self.assertEqual([x for x in tree(aside) if not x.endswith("/")], [])




if __name__ == "__main__":
    unittest.main()
