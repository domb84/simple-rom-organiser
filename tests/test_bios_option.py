"""The library rule "Keep BIOS, firmware and key files": found by the scan, kept with the ROMs, or - when it is off - a file like any other."""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

from tests.servercase import ServerCase, SNES, put  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)

FIRMWARE = b"SNES CD boot rom, but not really" * 40
DAMAGED = b"SNES CD boot rom, but nOt really" * 40
PATCHED = b"SNES CD boot rom, but patched!!!" * 40
DAT = """<?xml version="1.0"?>
<datafile>
<header><name>Nintendo Super Famicom &amp; Super Entertainment System - Firmware</name><version>2012-09-08</version></header>
<game name="Super NES CD-ROM Boot ROM (1994)(Sony)"><description>x</description>
<rom name="boot.bin" size="%d" crc="%08x" md5="%s" sha1="%s"/></game>
<game name="Super NES CD-ROM Boot ROM (1994)(Sony)[b]"><description>x</description>
<rom name="boot[b].bin" size="%d" crc="%08x" md5="%s" sha1="%s"/></game>
<game name="Super NES CD-ROM Boot ROM (1994)(Sony)[m Patched]"><description>x</description>
<rom name="boot[m].bin" size="%d" crc="%08x" md5="%s" sha1="%s"/></game>
</datafile>
"""


class BiosOption(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "data" / "dats").mkdir(parents=True, exist_ok=True)
        (self.tmp / "data" / "dats" / "Nintendo Super Famicom & Super Entertainment System - Firmware (TOSEC-v2012-09-08_CM).dat").write_text(
            DAT % (len(FIRMWARE), zlib.crc32(FIRMWARE), hashlib.md5(FIRMWARE).hexdigest(), hashlib.sha1(FIRMWARE).hexdigest(),
                   len(DAMAGED), zlib.crc32(DAMAGED), hashlib.md5(DAMAGED).hexdigest(), hashlib.sha1(DAMAGED).hexdigest(),
                   len(PATCHED), zlib.crc32(PATCHED), hashlib.md5(PATCHED).hexdigest(), hashlib.sha1(PATCHED).hexdigest()))
        self.root = self.tmp / "snes"
        put(self.root / "a.sfc", self.rom["Alpha (USA)"])
        put(self.root / "BIOS Files" / "whatever.rom", FIRMWARE)
        put(self.root / "notes.bin", b"not a bios at all" * 30)

    def scan(self) -> dict:
        self.job("/api/scan", {"path": str(self.root), "platform": SNES})
        return self.call("GET", "/api/status")["scan"]["summary"]

    def plan(self) -> dict:
        self.call("POST", "/api/library/plan", {})
        rows = self.call("POST", "/api/library/plan", {"limit": 500})
        return {Path(i["from"]).name: i for i in rows["items"]}

    def test_the_scan_finds_the_firmware_and_the_plan_keeps_it_where_it_is(self) -> None:
        summary = self.scan()
        self.assertEqual(summary["bios_files"], 1)
        rows = self.call("GET", "/api/scan/results?kind=unmatched")["items"]
        self.assertEqual({r["file"]: bool(r.get("bios")) for r in rows}, {"BIOS Files/whatever.rom": True, "notes.bin": False})
        items = self.plan()
        self.assertNotIn("_unmatched", items["whatever.rom"]["to"])
        self.assertEqual(items["whatever.rom"]["to"], "BIOS Files/boot.bin")               # (named as its list names it, in its own folder)
        self.assertIn("_unmatched", items["notes.bin"]["to"] + items["notes.bin"].get("reason", "") + items["notes.bin"].get("category", ""))

    def test_switched_off_it_is_a_file_like_any_other(self) -> None:
        self.call("POST", "/api/library/defaults", {"keep_bios": False})
        self.scan()
        items = self.plan()
        self.assertNotEqual(items["whatever.rom"]["to"], "BIOS Files/whatever.rom")

    def test_a_system_can_override_the_default(self) -> None:
        self.call("POST", "/api/library/defaults", {"keep_bios": False})
        got = self.call("POST", "/api/library/profile", {"platform": SNES, "override": {"fields": ["keep_bios"], "on": True}})
        self.assertEqual(got["inherit"]["overridden"], ["keep_bios"])
        self.assertFalse(got["profile"]["keep_bios"])                                       # it starts at what the system used
        self.call("POST", "/api/library/profile", {"platform": SNES, "keep_bios": True})
        self.scan()
        self.assertEqual(self.plan()["whatever.rom"]["to"], "BIOS Files/boot.bin")
        defaults = self.call("GET", "/api/library/defaults")
        self.assertFalse(defaults["profile"]["keep_bios"])
        self.assertEqual(self.call("GET", f"/api/library/profile?platform={SNES.replace(' ', '%20')}")["profile"]["keep_bios"], True)

    def test_it_is_on_by_default_and_is_a_rule_of_every_system(self) -> None:
        info = self.call("GET", f"/api/library/profile?platform={SNES.replace(' ', '%20')}")
        self.assertTrue(info["profile"]["keep_bios"])
        self.assertIn("keep_bios", info["inherit"]["applicable"])
        self.assertIn("keep_bios", [e["field"] for e in info["catalog"] if e["kind"] == "option"])

    def test_the_bios_page_says_where_the_lists_come_from_and_what_the_scans_found(self) -> None:
        empty = self.call("GET", "/api/bios")
        self.assertEqual([x["name"] for x in empty["sources"]], ["libretro System.dat", "TOSEC firmware DATs"])
        self.assertEqual((empty["found"], empty["keep_default"], empty["overridden"]), ([], True, []))
        self.assertIn("prod.keys", empty["keys"])
        self.scan()
        self.call("POST", "/api/library/profile", {"platform": SNES, "override": {"fields": ["keep_bios"], "on": True}})
        got = self.call("GET", "/api/bios")
        self.assertEqual([(f["platform"], f["files"]) for f in got["found"]], [(SNES, 1)])
        self.assertEqual(got["overridden"], [SNES])
        self.assertEqual(got["sources"][1]["entries"], 3)

    def test_the_library_page_counts_them_and_can_show_just_them(self) -> None:
        self.scan()
        plan = self.call("POST", "/api/library/plan", {})
        self.assertEqual((plan["reasons"]["bios"], plan["categories"]["bios"]), (1, 1))
        self.assertEqual(plan["reasons"]["kept"], 1)                                   # (the game: the BIOS file is counted apart)
        only = self.call("POST", "/api/library/plan", {"reason": "bios"})
        self.assertEqual([Path(i["from"]).name for i in only["items"]], ["whatever.rom"])
        self.assertEqual(only["items"][0]["category"], "bios")
        self.assertEqual(self.call("POST", "/api/library/plan", {"reason": "kept"})["total"], 1)

    def test_switched_off_there_is_nothing_to_count(self) -> None:
        self.call("POST", "/api/library/defaults", {"keep_bios": False})
        self.scan()
        plan = self.call("POST", "/api/library/plan", {})
        self.assertEqual((plan["reasons"]["bios"], plan["categories"].get("bios", 0)), (0, 0))

    def test_the_overview_total_is_in_the_summary_also_for_none(self) -> None:
        self.assertEqual(self.scan()["bios_files"], 1)
        put(self.root / "BIOS Files" / "whatever.rom", b"x" * 200)                       # no longer a BIOS
        self.assertEqual(self.scan()["bios_files"], 0)

    def test_a_bad_dump_is_listed_with_the_name_tosec_gives_it(self) -> None:
        put(self.root / "damaged.bin", DAMAGED)
        self.assertEqual(self.scan()["bios_files"], 2)                                       # (both are on the list)
        rows = {r["file"]: r for r in self.call("GET", "/api/scan/results?kind=unmatched")["items"]}
        self.assertIn("[b]", rows["damaged.bin"]["bios"])
        good = self.call("GET", f"/api/scan/checksums?kind=unmatched&id={rows['BIOS Files/whatever.rom']['id']}")
        self.assertIn("Super Famicom", good["bios"])

    def test_the_rules_are_those_of_a_rom_whatever_flag_the_entry_has(self) -> None:
        put(self.root / "patched.bin", PATCHED)
        self.scan()
        rows = {Path(i["from"]).name: i for i in self.call("POST", "/api/library/plan", {"limit": 500})["items"]}
        self.assertEqual((rows["patched.bin"]["category"], rows["patched.bin"]["reasons"]), ("excluded", ["modified"]))
        self.assertEqual(rows["whatever.rom"]["category"], "bios")                          # (the good dump of the same list is kept)
        self.call("POST", "/api/library/defaults", {"exclude": ["bad_dump"]})                # "modified" off: as for a ROM, it stays
        rows = {Path(i["from"]).name: i for i in self.call("POST", "/api/library/plan", {"limit": 500})["items"]}
        self.assertEqual(rows["patched.bin"]["category"], "bios")
        self.assertEqual(rows["patched.bin"]["to"], "boot[m].bin")

    def test_a_bad_dump_is_excluded_like_a_game_with_that_flag(self) -> None:
        put(self.root / "damaged.bin", DAMAGED)
        self.scan()
        rows = {Path(i["from"]).name: i for i in self.call("POST", "/api/library/plan", {"limit": 500})["items"]}
        self.assertEqual((rows["damaged.bin"]["category"], rows["damaged.bin"]["reasons"]), ("excluded", ["bad_dump"]))
        self.assertTrue(rows["damaged.bin"]["to"].startswith("_excluded"))
        plan = self.call("POST", "/api/library/plan", {})
        self.assertEqual(plan["reasons"]["excluded_bad_dump"], 1)
        self.call("POST", "/api/library/defaults", {"exclude": ["virus"]})                    # "bad dump" is not a rule any more: as for a ROM, it stays
        rows = {Path(i["from"]).name: i for i in self.call("POST", "/api/library/plan", {"limit": 500})["items"]}
        self.assertEqual((rows["damaged.bin"]["category"], rows["damaged.bin"]["to"]), ("bios", "boot[b].bin"))

    def test_the_same_bios_twice_is_a_rom_twice_the_spare_goes_to_duplicates(self) -> None:
        put(self.root / "copy of it.bin", FIRMWARE)
        self.scan()
        rows = {Path(i["from"]).name: i for i in self.call("POST", "/api/library/plan", {"limit": 500})["items"]}
        spare, kept = sorted((rows["copy of it.bin"], rows["whatever.rom"]), key=lambda i: i["category"])[::-1]
        self.assertEqual((kept["category"], spare["category"]), ("bios", "duplicate"))
        self.assertTrue(spare["to"].startswith("_duplicates"))
        self.assertEqual(spare["keeper"], kept["to"])
        plan = self.call("POST", "/api/library/plan", {})
        self.assertEqual((plan["reasons"]["bios"], plan["reasons"]["duplicates"]), (1, 1))

    def test_a_spare_copy_beside_the_one_that_has_its_name_is_a_duplicate_too(self) -> None:
        put(self.root / "BIOS Files" / "boot.bin", FIRMWARE)                                   # (already named as the list names it)
        self.scan()
        rows = {Path(i["from"]).name: i for i in self.call("POST", "/api/library/plan", {"limit": 500})["items"]}
        self.assertEqual((rows["boot.bin"]["category"], rows["boot.bin"]["status"]), ("bios", "ok"))
        self.assertEqual((rows["whatever.rom"]["category"], rows["whatever.rom"]["keeper"]), ("duplicate", "BIOS Files/boot.bin"))

    def test_a_bios_an_earlier_build_set_aside_comes_back_as_a_rom_would(self) -> None:
        (self.root / "BIOS Files" / "whatever.rom").rename(self.root / "whatever.rom")
        put(self.root / "_unmatched" / "BIOS Files" / "whatever.rom", FIRMWARE + b"!")            # (not a BIOS: stays)
        (self.root / "_unmatched" / "BIOS Files" / "whatever.rom").unlink()
        put(self.root / "_unmatched" / "BIOS Files" / "old name.rom", FIRMWARE)
        put(self.root / "_excluded" / "bad.bin", DAMAGED)
        (self.root / "whatever.rom").unlink()
        self.scan()
        rows = {Path(i["from"]).name: i for i in self.call("POST", "/api/library/plan", {"limit": 500})["items"]}
        self.assertEqual(rows["old name.rom"]["to"], "BIOS Files/boot.bin")                     # (out of _unmatched, named as its list names it)
        self.assertEqual((rows["bad.bin"]["status"], rows["bad.bin"]["category"]), ("ok", "excluded"))    # (a bad dump stays where it is ...)
        self.call("POST", "/api/library/defaults", {"exclude": ["virus"]})                       # ... until its rule is off: then it comes back
        rows = {Path(i["from"]).name: i for i in self.call("POST", "/api/library/plan", {"limit": 500})["items"]}
        self.assertEqual(rows["bad.bin"]["to"], "boot[b].bin")

    def test_a_library_built_in_another_folder_takes_the_bios_with_its_database_name_and_not_the_spares(self) -> None:
        put(self.root / "copy of it.bin", FIRMWARE)
        put(self.root / "_excluded" / "bad.bin", DAMAGED)
        self.scan()
        dest = self.tmp / "clean-library"
        opts = {"export_to": str(dest), "export_mode": "copy"}
        plan = self.call("POST", "/api/library/plan", {"limit": 50, **opts})
        self.job("/api/library/apply", {**opts, "plan_id": plan["plan_id"]})
        built = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts)
        self.assertEqual([b for b in built if "boot" in b], ["boot.bin"])                         # one copy, as the list names it (the fewest folders, as for a ROM); the spare and the bad dump stay

    def test_a_build_and_its_undo_with_bios_files_set_aside_and_renamed(self) -> None:
        put(self.root / "copy of it.bin", FIRMWARE)
        put(self.root / "bad.bin", DAMAGED)
        mine = lambda: sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file() and not p.name.startswith(".romorg"))  # noqa: E731
        before = mine()
        self.scan()
        aside = self.tmp / "lib-archive"
        plan = self.call("POST", "/api/library/plan", {"aside_to": str(aside)})
        res = self.job("/api/library/apply", {"aside_to": str(aside), "plan_id": plan["plan_id"]})
        after = mine()
        self.assertIn("boot.bin", after)                                                      # (the one that stays: named as its list names it)
        self.assertNotIn("bad.bin", after)
        archived = sorted(p.relative_to(aside).as_posix() for p in aside.rglob("*") if p.is_file())
        self.assertTrue(any(a.endswith("_excluded/bad.bin") for a in archived) and any("_duplicates" in a for a in archived), archived)
        self.job("/api/library/undo", {"log": res["undo_log"]})
        self.assertEqual(mine(), before)

    def test_key_files_an_earlier_build_set_aside_come_back_too(self) -> None:
        put(self.root / "_unmatched" / "prod.keys", b"k")
        put(self.root / "_unmatched" / "other.bin", b"not a bios at all" * 30)
        self.scan()
        rows = {Path(i["from"]).name: i for i in self.call("POST", "/api/library/plan", {"limit": 500})["items"]}
        self.assertEqual((rows["prod.keys"]["to"], rows["prod.keys"]["category"]), ("prod.keys", "bios"))
        self.assertEqual((rows["other.bin"]["to"], rows["other.bin"]["status"]), ("_unmatched/other.bin", "ok"))


class Kickstarts(ServerCase):
    """The Amiga's Kickstarts are games of its own "- Firmware" DAT: they are matched as such, and counted as BIOS files all the same."""

    def setUp(self) -> None:
        super().setUp()
        self.kick = b"Kickstart 1.3 rom, as far as the checksums go" * 100
        (self.tmp / "data" / "dats").mkdir(parents=True, exist_ok=True)
        (self.tmp / "data" / "dats" / "Commodore Amiga - Firmware (TOSEC-v2012-09-08_CM).dat").write_text(
            '<?xml version="1.0"?><datafile><header><name>Commodore Amiga - Firmware</name><version>2012-09-08</version></header>'
            '<game name="Kickstart v1.3 r34.5 (1987)(Commodore)(A500)"><description>x</description>'
            f'<rom name="Kickstart v1.3 r34.5 (1987)(Commodore)(A500).rom" size="{len(self.kick)}" crc="{zlib.crc32(self.kick):08x}" '
            f'md5="{hashlib.md5(self.kick).hexdigest()}" sha1="{hashlib.sha1(self.kick).hexdigest()}"/></game></datafile>')
        self.root = self.tmp / "amiga"
        put(self.root / "kick13.rom", self.kick)

    def test_they_are_in_the_total_and_in_the_library_filter(self) -> None:
        self.job("/api/scan", {"path": str(self.root), "platform": "Commodore Amiga"})
        summary = self.call("GET", "/api/status")["scan"]["summary"]
        self.assertEqual((summary["bios_files"], summary["matched_files"]), (1, 1))                 # (matched, and counted)
        plan = self.call("POST", "/api/library/plan", {})
        self.assertEqual((plan["reasons"]["bios"], plan["reasons"]["kept"]), (1, 0))
        rows = self.call("POST", "/api/library/plan", {"reason": "bios"})["items"]
        self.assertEqual([i["category"] for i in rows], ["bios"])
        self.assertEqual(self.call("GET", "/api/bios")["found"][0]["platform"], "Commodore Amiga")

    def test_with_the_rule_off_a_kickstart_is_a_game_like_any_other(self) -> None:
        self.call("POST", "/api/library/defaults", {"keep_bios": False})
        self.job("/api/scan", {"path": str(self.root), "platform": "Commodore Amiga"})
        plan = self.call("POST", "/api/library/plan", {})
        self.assertEqual((plan["reasons"]["bios"], plan["reasons"]["kept"]), (0, 1))
