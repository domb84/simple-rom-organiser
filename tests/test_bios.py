"""BIOS / firmware files are told by their checksum, kept with the ROMs under the name the emulators expect; key files by name."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from romorg import bios, biosdata, organiser  # noqa: E402

CONTENT = b"not a real bios, but its checksum is on the list for this test" * 100


def fake_list(test: unittest.TestCase, content: bytes = CONTENT, names=("scph9999.bin", "alias.bin")) -> bios.Bios:
    """Put ``content`` on the list (as a PlayStation BIOS with two names) for the duration of the test."""
    entry = bios.Bios("Sony - PlayStation", names[0], frozenset(n.casefold() for n in names))
    for attr, value in (("_BY_SHA1", {**bios._BY_SHA1, hashlib.sha1(content).hexdigest(): entry}),
                        ("_SIZES", bios._SIZES | {len(content)}), ("_SEEN", {})):
        p = mock.patch.object(bios, attr, value)
        p.start()
        test.addCleanup(p.stop)
    return entry


class ListTest(unittest.TestCase):
    def test_the_list_is_there_and_answers_by_checksum(self) -> None:
        self.assertGreater(len(biosdata.ENTRIES), 400)
        found = bios.lookup(524288, sha1="10155D8D6E6E832D6EA66DB9BC098321FB5E8EBF")
        self.assertEqual((found.system, found.name), ("Sony - PlayStation", "scph1001.bin"))
        self.assertEqual(bios.lookup(524288, crc="37157331").name, "scph1001.bin")
        self.assertIsNone(bios.lookup(524288, sha1="00" * 20))
        dc = bios.lookup(2097152, sha1="8951d1bb219ab2ff8583033d2119c899cc81f18c")
        self.assertEqual((dc.name, sorted(dc.names)), ("dc_boot.bin", ["boot.bin", "dc_boot.bin"]))       # (no folder; both names are right)


class FileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-bios-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fake_list(self)

    def put(self, name: str, data: bytes = CONTENT) -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_a_bios_is_told_by_its_content_whatever_it_is_called(self) -> None:
        self.assertEqual(bios.identify(self.put("whatever.rom")).name, "scph9999.bin")
        self.assertIsNone(bios.identify(self.put("scph9999.bin", b"x" * len(CONTENT))))            # the name alone is nothing
        self.assertIsNone(bios.identify(self.put("short.bin", b"abc")))
        self.assertIsNone(bios.identify(self.tmp / "missing.bin"))

    def test_the_op_renames_in_place_and_never_sets_aside(self) -> None:
        op = organiser.bios_op(self.put("my ps bios.bin"))
        self.assertEqual((op.status, op.dst.name, op.dst.parent, op.unmatched), ("move", "scph9999.bin", self.tmp, False))
        self.assertIn("BIOS / firmware (Sony - PlayStation)", op.reason)
        ok = organiser.bios_op(self.put("Alias.BIN"))                                              # one of its names already
        self.assertEqual((ok.status, ok.dst.name), ("ok", "Alias.BIN"))
        self.put("scph9999.bin")
        clash = organiser.bios_op(self.tmp / "my ps bios.bin")                                     # the name is taken: this copy stays
        self.assertEqual((clash.status, clash.dst.name), ("skip", "my ps bios.bin"))
        self.assertIsNone(organiser.bios_op(self.put("game.bin", b"g" * 100)))

    def test_key_files_are_left_alone(self) -> None:
        for name in ("prod.keys", "keys.txt", "Title.Keys"):
            op = organiser.bios_op(self.put(name, b"k"))
            self.assertEqual((op.status, op.dst.name, op.unmatched), ("skip", name, False), name)


import test_ps_server as ps  # noqa: E402


class LibraryTest(ps.PsServerCase):
    """Through the server, on a disc system: the BIOS in the games folder gets its name and stays; a stray file is set aside."""

    def test_the_library_build_keeps_the_bios_with_the_discs(self) -> None:
        fake_list(self)
        self.call("/api/chdman", {"engine": "python"})
        self.put_iso("Alpha (USA)", "Alpha (USA)/Alpha (USA).chd")
        (self.roms / "ps bios dump.bin").write_bytes(CONTENT)
        (self.roms / "stray.bin").write_bytes(b"stray" * 50)
        (self.roms / "prod.keys").write_bytes(b"k")
        self.scan_ps2()
        plan = self.call("/api/library/plan", {"platform": ps.PS2})
        rows = {Path(r["from"]).name if r.get("from") else r.get("file"): r for r in plan["items"]}
        self.assertEqual(rows["ps bios dump.bin"]["to"], "scph9999.bin")
        self.assertIn("BIOS / firmware", rows["ps bios dump.bin"]["reason"])
        self.assertTrue(rows["stray.bin"]["to"].startswith("_unmatched"))
        self.call("/api/library/apply", {})
        self.assertEqual(self.job()["status"], "done")
        self.assertTrue((self.roms / "scph9999.bin").is_file())
        self.assertTrue((self.roms / "prod.keys").is_file())
        self.assertTrue((self.roms / "_unmatched" / "stray.bin").is_file())


if __name__ == "__main__":
    unittest.main()
