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


_DATA = Path(tempfile.mkdtemp(prefix="romorg-bios-data-"))
(_DATA / "dats").mkdir()


def setUpModule() -> None:
    """The tests use their own data folder: the TOSEC pack of the machine they run on must not change what they find."""
    patch = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(_DATA)})
    patch.start()
    unittest.addModuleCleanup(patch.stop)
    unittest.addModuleCleanup(shutil.rmtree, _DATA, True)
    bios._T_SIG = None


FW_32X = b"M" * 2048
FW_DAT = """<?xml version="1.0"?>
<datafile>
<header><name>Sega 32X - Firmware</name><version>2012-09-08</version></header>
<game name="Sega 32X Master Hitachi SH-2 ROM (1994)(Sega)"><description>x</description>
<rom name="Sega 32X Master Hitachi SH-2 ROM (1994)(Sega).bin" size="%d" crc="%08x" md5="%s" sha1="%s"/></game>
<game name="Sega 32X Slave ROM (1994)(Sega)[b]"><description>x</description>
<rom name="Sega 32X Slave ROM (1994)(Sega)[b].bin" size="9" crc="00000009" md5="%s" sha1="%s"/></game>
</datafile>
"""


class TosecFirmwareTest(unittest.TestCase):
    """Firmware told by the TOSEC "- Firmware" DATs of the installed pack, whatever it is called; no name is given to it."""

    def setUp(self) -> None:
        import zlib
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-bios-tosec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dat = _DATA / "dats" / "Sega 32X - Firmware (TOSEC-v2012-09-08_CM).dat"
        self.dat.write_text(FW_DAT % (len(FW_32X), zlib.crc32(FW_32X), hashlib.md5(FW_32X).hexdigest(), hashlib.sha1(FW_32X).hexdigest(),
                                      "0" * 32, "9" * 40))
        self.addCleanup(self.dat.unlink)
        bios._T_SIG = None
        self.addCleanup(setattr, bios, "_T_SIG", None)

    def test_a_file_is_told_by_its_checksum_and_gets_the_name_its_list_gives_it(self) -> None:
        f = self.tmp / "32X_M_BIOS.BIN"
        f.write_bytes(FW_32X)
        found = bios.identify(f)
        self.assertEqual((found.system, found.source, found.names), ("Sega 32X", "tosec", frozenset()))
        op = organiser.bios_op(f)
        self.assertEqual((op.status, op.dst), ("move", f.with_name("Sega 32X Master Hitachi SH-2 ROM (1994)(Sega).bin")))   # (as a ROM gets its DAT's)
        self.assertIn("BIOS / firmware (Sega 32X)", op.reason)
        self.assertEqual(bios.lookup(len(FW_32X), crc=f"{__import__('zlib').crc32(FW_32X):08x}").system, "Sega 32X")

    def test_a_file_both_lists_know_is_named_as_the_database_names_it(self) -> None:
        import zlib
        crc = f"{zlib.crc32(FW_32X):08x}"
        libretro = bios.Bios("Sega - 32X", "emulator_name.bin", frozenset(("emulator_name.bin",)))
        for attr, value in (("_BY_SHA1", {**bios._BY_SHA1, hashlib.sha1(FW_32X).hexdigest(): libretro}),
                            ("_BY_CRC", {**bios._BY_CRC, (len(FW_32X), crc): libretro}), ("_SEEN", {})):
            p = mock.patch.object(bios, attr, value)
            p.start()
            self.addCleanup(p.stop)
        f = self.tmp / "emulator_name.bin"                                           # (it already has the emulators' name)
        f.write_bytes(FW_32X)
        found = bios.identify(f)
        self.assertEqual((found.source, found.names), ("tosec", frozenset(("emulator_name.bin",))))
        op = organiser.bios_op(f)
        self.assertEqual((op.status, op.dst.name), ("move", "Sega 32X Master Hitachi SH-2 ROM (1994)(Sega).bin"))   # (the emulators' name is for the copy in their folder)

    def test_the_entry_keeps_its_name_flags_and_all_for_the_rules_to_read(self) -> None:
        found = bios.lookup(9, crc="00000009")
        self.assertEqual((found.system, "[b]" in found.game), ("Sega 32X", True))
        self.assertEqual(bios.lookup(len(FW_32X), crc=f"{__import__('zlib').crc32(FW_32X):08x}").game, "Sega 32X Master Hitachi SH-2 ROM (1994)(Sega)")
        self.assertIsNone(bios.lookup(9, crc="00000010"))

    def test_the_list_follows_the_pack(self) -> None:
        self.assertEqual([s["name"] for s in bios.sources()], ["libretro System.dat", "TOSEC firmware DATs"])
        self.assertEqual(bios.sources()[1]["entries"], 2)
        before = bios.signature()
        self.dat.rename(self.dat.with_name("Sega 32X - Firmware (TOSEC-v2020-01-01_CM).dat"))
        self.addCleanup(lambda: self.dat.with_name("Sega 32X - Firmware (TOSEC-v2020-01-01_CM).dat").rename(self.dat)
                        if self.dat.with_name("Sega 32X - Firmware (TOSEC-v2020-01-01_CM).dat").exists() else None)
        self.assertNotEqual(bios.signature(), before)                          # a new version of the pack is a different list

    def test_a_scan_finds_them_with_the_checksums_it_has(self) -> None:
        import types
        import zlib
        root = self.tmp
        crc = f"{zlib.crc32(FW_32X):08x}"
        entry = lambda path, member, size, c: types.SimpleNamespace(path=Path(path), member=member, size=size, crc=c, sha1=None)  # noqa: E731
        unmatched = [entry("BIOS Files/32X_M_BIOS.BIN", None, len(FW_32X), crc),
                     entry("pack.zip", "a.bin", len(FW_32X), crc), entry("pack.zip", "b.bin", len(FW_32X), crc),
                     entry("mixed.zip", "a.bin", len(FW_32X), crc), entry("mixed.zip", "game.rom", 99, "12345678"),
                     entry("game.rom", None, 99, "12345678")]
        found = bios.find_in_scan(types.SimpleNamespace(root=root, unmatched=unmatched, matched=[]))
        self.assertEqual(sorted(p.name for p in found), ["32X_M_BIOS.BIN", "pack.zip"])
        self.assertEqual((found[root / "pack.zip"].in_zip, found[root / "BIOS Files" / "32X_M_BIOS.BIN"].in_zip), (True, False))
        op = organiser.bios_op(root / "pack.zip", found)
        self.assertEqual((op.status, "in a zip" in op.reason), ("ok", True))
        self.assertIsNone(organiser.bios_op(root / "game.rom", found))         # (the scan said no: nothing is read)


class ToolTest(unittest.TestCase):
    def test_the_bios_tool_finds_a_file_by_the_name_a_library_build_gave_it_whatever_its_extension(self) -> None:
        from romorg import retroarch
        import zlib
        tmp = Path(tempfile.mkdtemp(prefix="romorg-bios-tool-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        dat = _DATA / "dats" / "Atari Lynx - Firmware (TOSEC-v2012-09-08_CM).dat"
        dat.write_text('<?xml version="1.0"?><datafile><header><name>Atari Lynx - Firmware</name><version>2012-09-08</version></header>'
                       '<game name="Atari Lynx Mystery ROM (1989)(Atari)"><description>x</description>'
                       f'<rom name="Atari Lynx Mystery ROM (1989)(Atari).xyz" size="{len(FW_32X)}" crc="{zlib.crc32(FW_32X):08x}" '
                       f'md5="{hashlib.md5(FW_32X).hexdigest()}" sha1="{hashlib.sha1(FW_32X).hexdigest()}"/></game></datafile>')
        self.addCleanup(dat.unlink)
        bios._T_SIG = None
        self.addCleanup(setattr, bios, "_T_SIG", None)
        (tmp / "Atari Lynx Mystery ROM (1989)(Atari).xyz").write_bytes(FW_32X)                   # (a name nothing in the tool's own filters knows)
        (tmp / "other.xyz").write_bytes(FW_32X)                                                  # (the same bytes: not looked at, as before)
        found, _complete = retroarch._find_candidates([tmp], [{"path": "lynx.bin", "md5": hashlib.md5(FW_32X).hexdigest()}])
        self.assertEqual([p.name for p in found.values()], ["Atari Lynx Mystery ROM (1989)(Atari).xyz"])


class ZipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-bioszip-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fake_list(self)
        import zlib
        self.crc = f"{zlib.crc32(CONTENT):08x}"
        entry = bios.Bios("Sony - PlayStation", "scph9999.bin", frozenset(("scph9999.bin",)))
        p = mock.patch.object(bios, "_BY_CRC", {**bios._BY_CRC, (len(CONTENT), self.crc): entry})
        p.start()
        self.addCleanup(p.stop)
        bios._SEEN_ZIPS.clear()

    def zip(self, name: str, files: dict) -> Path:
        import zipfile
        p = self.tmp / name
        with zipfile.ZipFile(p, "w") as z:
            for n, data in files.items():
                z.writestr(n, data)
        return p

    def test_a_zip_of_nothing_but_bios_files_is_kept_as_it_is(self) -> None:
        z = self.zip("[BIOS] PlayStation.zip", {"whatever.bin": CONTENT})
        self.assertEqual(bios.identify_archive(z).name, "scph9999.bin")
        op = organiser.bios_op(z)
        self.assertEqual((op.status, op.dst, op.unmatched), ("ok", z, False))
        self.assertIn("in a zip", op.reason)

    def test_a_zip_with_anything_else_in_it_is_not(self) -> None:
        self.assertIsNone(bios.identify_archive(self.zip("mixed.zip", {"a.bin": CONTENT, "game.rom": b"g" * 300})))
        self.assertIsNone(bios.identify_archive(self.zip("game.zip", {"game.rom": b"g" * 300})))
        self.assertIsNone(bios.identify_archive(self.zip("empty.zip", {})))
        (self.tmp / "broken.zip").write_bytes(b"PK not really")
        self.assertIsNone(bios.identify_archive(self.tmp / "broken.zip"))
        self.assertIsNone(bios.identify_archive(self.tmp / "missing.zip"))
        self.assertIsNone(organiser.bios_op(self.zip("game2.zip", {"game.rom": b"g" * 300})))

    def test_only_a_zip_is_looked_into(self) -> None:
        self.assertIsNone(bios.identify_archive(self.tmp / "x.7z"))


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


    def test_the_same_bios_twice_on_a_disc_system_the_spare_goes_to_duplicates(self) -> None:
        import zlib
        from romorg import paths
        data = b"a playstation bios, as far as the checksums go" * 30
        (paths.dats_dir() / "Sony PlayStation - Firmware (TOSEC-v2023-11-07_CM).dat").write_text(
            '<?xml version="1.0"?><datafile><header><name>Sony PlayStation - Firmware</name><version>2023-11-07</version></header>'
            '<game name="Sony PlayStation SCPH-5500 BIOS v3.0 (1996-09-09)(Sony)(JP)"><description>x</description>'
            f'<rom name="Sony PlayStation SCPH-5500 BIOS v3.0 (1996-09-09)(Sony)(JP).bin" size="{len(data)}" crc="{zlib.crc32(data):08x}" '
            f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game></datafile>')
        self.call("/api/chdman", {"engine": "python"})
        self.put_iso("Alpha (USA)", "Alpha (USA)/Alpha (USA).chd")
        (self.roms / "PSX - SCPH5500.BIN").write_bytes(data)
        (self.roms / "scph5500.bin").write_bytes(data)
        self.scan_ps2()
        rows = {Path(r["from"]).name: r for r in self.call("/api/library/plan", {"platform": ps.PS2})["items"]}
        name = "Sony PlayStation SCPH-5500 BIOS v3.0 (1996-09-09)(Sony)(JP).bin"
        self.assertEqual((rows["PSX - SCPH5500.BIN"]["to"], rows["PSX - SCPH5500.BIN"]["category"]), (name, "bios"))
        self.assertEqual((rows["scph5500.bin"]["to"], rows["scph5500.bin"]["category"]), ("_duplicates/scph5500.bin", "duplicate"))
        self.assertEqual(rows["scph5500.bin"]["keeper"], name)

    def test_a_bad_firmware_dump_is_set_aside_as_a_bad_dump_like_a_game_with_that_flag(self) -> None:
        import zlib
        from romorg import paths
        damaged = b"a damaged playstation bios" * 30
        (paths.dats_dir() / "Sony PlayStation - Firmware (TOSEC-v2023-11-07_CM).dat").write_text(
            '<?xml version="1.0"?><datafile><header><name>Sony PlayStation - Firmware</name><version>2023-11-07</version></header>'
            '<game name="Sony PlayStation SCPH-5502 BIOS v3.0 (1997-01-06)(Sony)[b]"><description>x</description>'
            f'<rom name="bios[b].bin" size="{len(damaged)}" crc="{zlib.crc32(damaged):08x}" md5="{hashlib.md5(damaged).hexdigest()}" '
            f'sha1="{hashlib.sha1(damaged).hexdigest()}"/></game></datafile>')
        self.call("/api/chdman", {"engine": "python"})
        self.put_iso("Alpha (USA)", "Alpha (USA)/Alpha (USA).chd")
        (self.roms / "PSX - SCPH5502.BIN").write_bytes(damaged)
        (self.roms / "stray.bin").write_bytes(b"stray" * 50)
        self.scan_ps2()
        rows = {Path(r["from"]).name: r for r in self.call("/api/library/plan", {"platform": ps.PS2})["items"]}
        self.assertTrue(rows["PSX - SCPH5502.BIN"]["to"].startswith("_excluded"))
        self.assertEqual((rows["PSX - SCPH5502.BIN"]["category"], rows["PSX - SCPH5502.BIN"]["reasons"]), ("excluded", ["bad_dump"]))
        self.assertIn("Bad dumps", rows["PSX - SCPH5502.BIN"]["reason"])
        self.assertEqual(rows["PSX - SCPH5502.BIN"]["bios"], False)                          # (an excluded file is no longer a kept BIOS)
        self.assertTrue(rows["stray.bin"]["to"].startswith("_unmatched"))                    # (an ordinary stray file is not)
        self.call("/api/library/defaults", {"exclude": []})                                  # the rule off: as for a game, it stays
        rows = {Path(r["from"]).name: r for r in self.call("/api/library/plan", {"platform": ps.PS2})["items"]}
        self.assertEqual(rows["PSX - SCPH5502.BIN"]["to"], "bios[b].bin")                    # (kept, and named as its list names it)


if __name__ == "__main__":
    unittest.main()


class KeyFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        from romorg import keyfiles
        self.k = keyfiles
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-keys-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.eden = self.tmp / "eden" / "nand"
        self.ryu = self.tmp / "ryujinx"
        self.cemu = self.tmp / "cemu"
        for d in (self.eden, self.ryu, self.cemu):
            d.mkdir(parents=True)
        self.nin = {"wii": {"data": "", "games": "", "off": ""}, "wiiu": {"data": str(self.cemu), "games": "", "off": ""},
                    "ps2": {"data": "", "games": "", "off": ""}}
        self.switch = {"eden": str(self.eden), "ryujinx": str(self.ryu)}
        self.good = b"header_key = " + bytes(range(32)).hex().encode() + b"\n"

    def rows(self, *dirs: Path) -> dict:
        res = self.k.check(self.nin, self.switch, dirs, home=self.home)
        return {(r["emulator"], r["file"]): r for r in res["rows"]}

    def test_a_missing_key_is_found_in_the_other_emulators_folder_and_copied_into_place(self) -> None:
        (self.ryu / "system").mkdir()
        (self.ryu / "system" / "prod.keys").write_bytes(self.good)
        rows = self.rows()
        self.assertEqual(rows[("ryujinx", "prod.keys")]["status"], "ok")
        eden = rows[("eden", "prod.keys")]
        self.assertEqual((eden["status"], eden["source"], eden["target"]), ("found", str(self.ryu / "system" / "prod.keys"), str(self.tmp / "eden" / "keys" / "prod.keys")))
        self.assertEqual(rows[("eden", "title.keys")]["status"], "missing")
        self.assertFalse(rows[("eden", "title.keys")]["required"])
        res = self.k.place(rows.values())
        self.assertEqual(res["placed"], 1)
        self.assertTrue((self.tmp / "eden" / "keys" / "prod.keys").is_file())
        self.assertTrue((self.ryu / "system" / "prod.keys").is_file())                  # (copied: the original stays)
        self.assertEqual(self.k.place(rows.values())["placed"], 0)                    # (again: the file is there now, nothing is overwritten)

    def test_a_key_file_in_a_searched_folder_is_found_and_a_foreign_file_with_the_name_is_not_taken(self) -> None:
        dl = self.tmp / "roms" / "switch"
        dl.mkdir(parents=True)
        (dl / "keys.txt").write_text("not keys at all\n")
        (dl / "prod.keys").write_bytes(b"nothing useful")
        rows = self.rows(self.tmp / "roms")
        self.assertEqual(rows[("cemu", "keys.txt")]["status"], "missing")
        self.assertEqual(rows[("eden", "prod.keys")]["status"], "missing")
        (dl / "keys.txt").write_text("# the Wii U keys\n" + "00112233445566778899aabbccddeeff # common key\n")
        rows = self.rows(self.tmp / "roms")
        self.assertEqual((rows[("cemu", "keys.txt")]["status"], rows[("cemu", "keys.txt")]["source"]), ("found", str(dl / "keys.txt")))
        (self.cemu / "keys.txt").write_text("garbage")
        self.assertEqual(self.rows(self.tmp / "roms")[("cemu", "keys.txt")]["status"], "invalid")      # (there, but not usable: left alone)
        self.assertEqual(self.k.place(self.rows(self.tmp / "roms").values())["placed"], 0)

    def test_an_emulator_that_is_not_set_up_has_no_rows(self) -> None:
        self.switch = {"eden": "", "ryujinx": ""}
        self.nin["wiiu"]["data"] = ""
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "APPDATA": "", "USERPROFILE": ""}):
            self.assertEqual(self.k.check(self.nin, self.switch, [], home=self.home)["rows"], [])
