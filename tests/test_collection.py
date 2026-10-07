"""Tests for romorg.collection: finding system folders in a ROM root, global rules over a system's defaults."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from romorg import collection, library, platforms


class Detect(unittest.TestCase):
    def test_folder_names_and_aliases_are_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("GBA", "Genesis", "snes", "PS1", ".hidden", "readme-dir"):
                (root / name).mkdir()
            found = {e["platform"]: e for e in collection.detect_systems(root, platforms.list_platforms())}
        self.assertTrue(found["Nintendo Game Boy Advance"]["found"])
        self.assertTrue(found["Sega Mega Drive - Genesis"]["found"])       # alias "genesis"
        self.assertTrue(found["Sony PlayStation"]["found"])                # alias "ps1"
        self.assertFalse(found["Nintendo 64"]["found"])
        self.assertTrue(found["Nintendo 64"]["path"].endswith("n64"))      # where it would be
        self.assertEqual(len(found), len(platforms.list_platforms()))


class Rules(unittest.TestCase):
    def test_global_rules_apply_only_where_the_system_has_them(self) -> None:
        gba = platforms.get_platform("Nintendo Game Boy Advance")
        amiga = platforms.get_platform("Commodore Amiga")
        g = collection.clean_global({"one_per_game": True, "languages": ["En", "De"], "region_priority": ["Japan"]})
        self.assertEqual(collection.effective_profile(gba, g).languages, ("En", "De"))
        self.assertEqual(collection.effective_profile(gba, g).region_priority[0], "Japan")
        self.assertFalse(library.default_profile(amiga).one_per_game)
        self.assertFalse(collection.effective_profile(amiga, g).one_per_game)   # no region DATs: stays off

    def test_nothing_set_means_each_systems_defaults(self) -> None:
        for p in platforms.list_platforms():
            self.assertEqual(collection.effective_profile(p, {}), library.default_profile(p))

    def test_unknown_and_bad_values_are_dropped(self) -> None:
        self.assertEqual(collection.clean_global({"nonsense": 1, "region_priority": ["Atlantis"]}), {"region_priority": []})

    def test_overlap_is_refused(self) -> None:
        self.assertIsNotNone(collection.check_folders(Path("/a/b"), Path("/a/b/c")))
        self.assertIsNotNone(collection.check_folders(Path("/a/b"), Path("/a")))
        self.assertIsNone(collection.check_folders(Path("/a/b"), Path("/a/c")))


if __name__ == "__main__":
    unittest.main()
