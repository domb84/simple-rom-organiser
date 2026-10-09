"""Tests for romorg.collection: finding system folders in a ROM root, global rules over a system's defaults."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from romorg import collection, libexport, library, platforms, sortroot


class Detect(unittest.TestCase):
    def test_a_folder_named_like_the_full_system_name_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Sega Master System").mkdir()
            (root / "Nintendo Game Boy Advance").mkdir()
            found = {e["platform"]: e for e in collection.detect_systems(root, platforms.list_platforms())}
        self.assertTrue(found["Sega Master System"]["found"])
        self.assertTrue(found["Nintendo Game Boy Advance"]["found"])

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

    def test_the_top_of_a_drive_as_rom_folder(self) -> None:
        # E:\ (an SD card with the ROMs at its top) ends in a separator already: a folder inside it was not seen as inside
        with tempfile.TemporaryDirectory() as tmp:
            inside = Path(tmp).resolve()
            top = Path(inside.anchor)
            self.assertIn("inside the ROM root", collection.check_folders(top, inside) or "")
            self.assertIn("inside the destination", collection.check_folders(inside, top) or "")
            self.assertIsNotNone(collection.check_folders(top, top))
            with self.assertRaises(libexport.ExportError):
                libexport.check_destination(top, inside / "library")
            with self.assertRaises(libexport.ExportError):
                libexport.check_destination(inside, top)
            self.assertIsNotNone(collection.check_folders(top, top / "archive"))     # (an archive inside the drive's top is refused)

    def test_overlap_is_seen_through_other_spellings_of_the_same_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "Rom Folder"
            (root / "Sub Folder").mkdir(parents=True)
            self.assertIsNotNone(collection.check_folders(root, Path(str(root) + os.sep)))
            self.assertIsNotNone(collection.check_folders(root, root / ".." / root.name / "x"))
            self.assertIsNone(collection.check_folders(root, Path(str(root) + "-archive")))      # a longer name, not inside
            link = base / "link"
            try:
                if os.name == "nt":
                    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(root / "Sub Folder")], capture_output=True).returncode == 0
                else:
                    os.symlink(root / "Sub Folder", link)
                    made = True
            except OSError:
                made = False
            if made:                                               # a junction / link that leads into the ROM folder
                self.addCleanup(lambda: os.path.lexists(link) and (os.rmdir(link) if os.name == "nt" else os.unlink(link)))
                self.assertIsNotNone(collection.check_folders(root, link / "library"))
                os.rmdir(link) if os.name == "nt" else os.unlink(link)
            if os.name == "nt":
                self.assertIsNotNone(collection.check_folders(root, Path(str(root).upper())))
                self.assertIsNotNone(collection.check_folders(root, Path(str(root).upper() + "\\")))
                self.assertIsNotNone(collection.check_folders(root, Path(str(root).swapcase()) / "sub folder" / "x"))
                self.assertIsNotNone(collection.check_folders(root, Path(str(root).replace("\\", "/") + "/x")))
                import ctypes
                buf = ctypes.create_unicode_buffer(1024)
                if ctypes.windll.kernel32.GetShortPathNameW(str(root / "Sub Folder"), buf, 1024) and "~" in buf.value:
                    self.assertIsNotNone(collection.check_folders(root, Path(buf.value) / "x"))   # an 8.3 short name


class StandardFolder(unittest.TestCase):
    """``SNES`` is the standard folder ``snes`` only where the file system says so."""

    def test_where_case_is_ignored_the_folder_is_named_as_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "SNES").mkdir()
            (root / "Gameboy").mkdir()
            same = os.path.exists(root / "snes")                   # Windows, exFAT: yes; ext4: no
            std = collection.standard_folder(root, "snes", root / "SNES")
            self.assertEqual(str(std), str(root / ("SNES" if same else "snes")))
            self.assertEqual(str(collection.standard_folder(root, "gb", root / "Gameboy")), str(root / "gb"))
            self.assertEqual(str(collection.standard_folder(root, "gba", None)), str(root / "gba"))
            self.assertEqual(str(collection.standard_folder(root, "snes", root / "snes")), str(root / "snes"))

    @unittest.skipIf(os.name == "nt", "needs a file system where case matters")
    def test_where_case_matters_two_folders_stay_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "SNES").mkdir()
            (root / "snes").mkdir()
            if not os.path.samefile(root / "SNES", root / "snes"):
                self.assertEqual(str(collection.standard_folder(root, "snes", root / "SNES")), str(root / "snes"))


if __name__ == "__main__":
    unittest.main()
