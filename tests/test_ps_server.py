"""PlayStation 2 / PlayStation through the HTTP API: platforms, scan, Verify fully, organise, library (no playlists
for the PS2), convert (createdvd) - synthetic discs, throwaway data dir, fake chdman."""

from __future__ import annotations

import os as _os
import tempfile as _tempfile
_os.environ.setdefault("ROMORG_DATA_DIR", _tempfile.mkdtemp(prefix="romorg-test-data-"))   # (undo logs live in the data folder: never the real one)

import os
import sys
import unittest
from pathlib import Path
from unittest import mock
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
import test_dc_server as base  # noqa: E402
from romorg import paths  # noqa: E402

PS2, PS2_DAT = "Sony PlayStation 2", "Sony - PlayStation 2"
PSX, PSX_DAT = "Sony PlayStation", "Sony - PlayStation"


class PsServerCase(base.DcServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.iso = {"Alpha (USA)": T.make_iso(40, 1), "Beta (Europe) (En,Fr,De)": T.make_iso(44, 2),
                    "Part (USA) (Disc 1)": T.make_iso(30, 3), "Part (USA) (Disc 2)": T.make_iso(31, 4)}
        T.write_dat(paths.redump_dir() / f"{PS2_DAT}.dat", [(n, "Games", [d], "iso") for n, d in self.iso.items()],
                    name=PS2_DAT, version="2026-06-15 03-41-38")
        self.roms = self.base / "ps2"
        self.roms.mkdir()

    def put_iso(self, game: str, rel: str, sidecars=(), dvd: bool = False) -> None:
        p = self.roms / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if dvd:
            T.build_dvd_chd(p, self.iso[game])
        else:
            T.build_chd(p, [{"type": "MODE1", "data": self.iso[game]}], gd=False)
        for s in sidecars:
            (p.parent / s).write_bytes(b"sidecar")

    def scan_ps2(self) -> dict:
        return self.run_job("/api/scan", {"path": str(self.roms), "platform": PS2})


class Ps2EndpointTests(PsServerCase):
    def test_platforms_and_redump_status(self) -> None:
        rows = {p["name"]: p for p in self.call("/api/platforms")}
        for name, dat, hint, playlists, iso in ((PS2, PS2_DAT, "ps2", False, True), (PSX, PSX_DAT, "psx", True, False)):
            r = rows[name]
            self.assertEqual((r["source"], r["layout"], r["chd"], r["folder_hint"], r["convertible"]),
                             ("redump", "game_folder", True, hint, True))
            self.assertEqual((r["disc"]["playlists"], r["disc"]["iso"]), (playlists, iso))
            self.assertEqual(r["dats"][0]["name"], dat)
        self.assertTrue(rows[PS2]["dats"][0]["present"])
        self.assertFalse(rows[PSX]["dats"][0]["present"])
        self.assertEqual(rows[PS2]["disc"]["iso_mode"], "createdvd")
        self.assertEqual({d["name"] for d in self.call("/api/status")["redump"]["dats"]},
                         {"Sega - Dreamcast", PSX_DAT, PS2_DAT})

    def test_folder_is_saved_per_system_immediately(self) -> None:
        self.call("/api/folders", {"platform": PS2, "path": str(self.roms)})
        rows = {p["name"]: p for p in self.call("/api/platforms")}
        self.assertEqual(rows[PS2]["folder"], str(self.roms.resolve()))
        self.assertIsNone(rows[PSX]["folder"])

    def test_scan_verify_organise_library(self) -> None:
        self.call("/api/chdman", {"engine": "python"})
        self.put_iso("Alpha (USA)", "Alpha loose.chd", ["Alpha loose.state"])
        self.put_iso("Beta (Europe) (En,Fr,De)", "Beta/b.chd", dvd=True)
        self.put_iso("Part (USA) (Disc 1)", "p1.chd")
        self.put_iso("Part (USA) (Disc 2)", "p2.chd")
        res = self.scan_ps2()
        s = res
        self.assertEqual((s["verified"], s["identified"], s["system"], s["have"]), (3, 1, "ps2", 4))
        self.call("/api/chdman", {"verify_scan": True})                # "Check every track while scanning": the DVD image is read too
        full = self.scan_ps2()
        self.assertEqual((full["verified"], full["identified"]), (4, 0))
        self.call("/api/chdman", {"verify_scan": False})
        plan = self.call("/api/library/plan", {"platform": PS2})
        row = [r for r in plan["items"] if r.get("game") == "Alpha (USA)"][0]
        self.assertEqual((row["status"], row["to"], row["files"]), ("move", "Alpha (USA)/Alpha (USA).chd", 1))
        lib = self.call("/api/library/plan", {"platform": PS2})
        self.assertEqual(lib["playlists"]["write"], 0)
        self.call("/api/library/apply", {})
        j = self.job()
        self.assertEqual(j["status"], "done", j)
        self.assertTrue((self.roms / "_saves" / "Alpha" / "Alpha loose.state").is_file())          # not a disc image: set aside under its game
        self.assertTrue((self.roms / "Part (USA)" / "Part (USA) (Disc 2)" / "Part (USA) (Disc 2).chd").is_file())          # (a disc of a multi-disc game: in its game's folder)
        self.assertFalse(list(self.roms.rglob("*.m3u")))
        self.assertFalse((self.roms / "Alpha loose.chd").exists())

    def test_convert_plan_offers_createdvd_for_an_iso(self) -> None:
        (self.roms / "raw").mkdir()
        (self.roms / "raw" / "a.iso").write_bytes(self.iso["Alpha (USA)"])
        iso_file = self.base / "fake.iso"
        iso_file.write_bytes(self.iso["Alpha (USA)"])
        fixture = self.base / "alpha.dvd.chd"
        T.build_dvd_chd(fixture, self.iso["Alpha (USA)"])
        self.use_fake_chdman()
        self.call("/api/library/profile", {"platform": PS2, "delete_converted": False})     # this test keeps the original
        self.scan_ps2()
        with mock.patch.dict(os.environ, {"FAKE_DVD_CHD": str(fixture), "FAKE_ISO": str(iso_file)}):
            plan = self.call("/api/convert/plan", {})
            self.assertEqual((plan["counts"], plan["items"][0]["mode"]), ({"convert": 1}, "createdvd"))
            self.assertTrue(plan["chdman"]["found"])
            self.assertEqual(plan["disc"]["key"], "ps2")
            self.call("/api/convert/apply", {})
            j = self.job()
            self.assertEqual((j["status"], j["result"]["converted"]), ("done", 1), j)
        self.assertTrue((self.roms / "Alpha (USA)" / "Alpha (USA).chd").is_file())
        self.assertTrue((self.roms / "_converted_originals" / "raw" / "a.iso").is_file())


class DiscSavesTests(PsServerCase):
    """A PlayStation 2 game with PCSX2 saves: kept, or archived with its saves (Amendment 30). PCSX2 tells a game by the serial on its
    disc, so its saves never need a new name."""

    def setUp(self) -> None:
        super().setUp()
        from test_nintendo import ps2_iso
        home = self.base / "home"
        home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(home), "APPDATA": "", "USERPROFILE": ""})
        env.start()
        self.addCleanup(env.stop)
        self.iso.update({"Gamma (USA)": ps2_iso("SLUS_209.46"), "Gamma (Europe)": ps2_iso("SLES_500.12")})
        T.write_dat(paths.redump_dir() / f"{PS2_DAT}.dat", [(n, "Games", [d], "iso") for n, d in self.iso.items()],
                    name=PS2_DAT, version="2026-06-15 03-41-38")
        self.call("/api/chdman", {"engine": "python"})
        self.pcsx2 = self.base / "pcsx2"
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        (card / "BASLUS-20946GAMMA").mkdir(parents=True)
        (card / "BASLUS-20946GAMMA" / "data.bin").write_bytes(b"save")
        (card / "BESLES-50012OTHER").mkdir()
        (card / "BESLES-50012OTHER" / "data.bin").write_bytes(b"other")
        (self.pcsx2 / "sstates").mkdir()
        (self.pcsx2 / "sstates" / "SLUS-20946 (AABBCCDD).01.p2s").write_bytes(b"state")
        (self.pcsx2 / "inis").mkdir()
        (self.pcsx2 / "inis" / "PCSX2.ini").write_text("[Folders]\n")
        self.call("/api/emulators/config", {"source": "pcsx2", "folder": str(self.pcsx2)})
        self.put_iso("Gamma (USA)", "Gamma (USA)/Gamma (USA).chd", dvd=True)
        self.put_iso("Gamma (Europe)", "Gamma (Europe)/Gamma (Europe).chd", dvd=True)
        self.scan_ps2()

    def games(self) -> list[str]:
        return sorted(p.relative_to(self.roms).as_posix() for p in self.roms.rglob("*.chd"))

    def test_saves_of_games_without_a_disc_are_listed_under_their_title_and_can_be_filtered(self) -> None:
        import sqlite3
        db = paths.data_dir() / "nintendo" / "gametdb.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE names (kind TEXT, id TEXT, name TEXT); CREATE TABLE info (key TEXT, value TEXT);")
        conn.execute("INSERT INTO names VALUES ('ps2', 'SLUS-20062', 'Grand Theft Auto III')")
        conn.commit()
        conn.close()
        folder = self.pcsx2 / "memcards" / "Mcd001.ps2" / "BASLUS-20062GTA30000"           # (a game with no disc here)
        folder.mkdir()
        (folder / "data.bin").write_bytes(b"gta")
        self.scan_ps2()
        everything = self.call("/api/scan/results?kind=saves")["items"]
        other = [s for s in everything if s["name"].startswith("SLUS-20062")][0]
        self.assertEqual((other["title"], other["match"]), ("Grand Theft Auto III", "dat"))               # (named by the database, no disc here)
        self.assertEqual(sorted(s["match"] for s in everything), ["dat", "rom", "rom"])
        have = self.call("/api/scan/results?kind=saves&have=1")["items"]
        none = self.call("/api/scan/results?kind=saves&have=0")["items"]
        self.assertEqual([s["match"] for s in have], ["rom", "rom"])
        self.assertEqual([s["title"] for s in none], ["Grand Theft Auto III"])
        self.assertEqual(self.call("/api/status")["scan"]["saves"]["dat_sets"], 1)
        tree = self.call("/api/tree?platform=Sony%20PlayStation%202&show=saves")                       # the tree lists them all too
        self.assertEqual(tree["facets"]["show"]["saves"], 2)                                                  # (games: the USA and European Gamma are one)
        titles = sorted(f["name"] for f in tree["files"])
        self.assertIn("Grand Theft Auto III (PCSX2)", titles)
        gta = [f for f in tree["files"] if f["name"].startswith("Grand Theft Auto III")][0]
        self.assertEqual((gta["state"], gta["row"]["saves"]["saves"]), ("saveonly", 1))
        self.assertEqual(self.call("/api/tree?platform=Sony%20PlayStation%202&show=missing")["facets"]["show"]["saves"], 2)
        self.assertEqual(tree["sides"]["library"], 2)                                                    # (no file is counted for a save-only entry)

    def test_a_save_of_a_game_with_no_disc_takes_the_database_game_of_its_region(self) -> None:
        import sqlite3
        self.iso.update({"Delta (USA)": T.make_iso(30, 9), "Delta (Europe)": T.make_iso(31, 10),
                         "Echo (USA)": T.make_iso(32, 11), "Echo (USA) (Greatest Hits)": T.make_iso(33, 12),
                         "Foxtrot (USA) (Rev 1)": T.make_iso(34, 13), "Foxtrot (USA) (Rev 2)": T.make_iso(35, 14),
                         "Ookami (USA)": T.make_iso(36, 15), "Ookami (USA) (Demo)": T.make_iso(37, 16)})
        T.write_dat(paths.redump_dir() / f"{PS2_DAT}.dat", [(n, "Games", [d], "iso") for n, d in self.iso.items()],
                    name=PS2_DAT, version="2026-06-15 03-41-38")
        db = paths.data_dir() / "nintendo" / "gametdb.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE names (kind TEXT, id TEXT, name TEXT); CREATE TABLE info (key TEXT, value TEXT);")
        conn.executemany("INSERT INTO names VALUES ('ps2', ?, ?)", [("SLUS-20999", "Delta"), ("SLUS-20998", "Echo"), ("SLUS-20997", "Foxtrot"), ("SLUS-20996", "Okami")])
        conn.commit()
        conn.close()
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        for name in ("BASLUS-20999DELTA", "BASLUS-20998ECHO", "BASLUS-20997FOX", "BASLUS-20996OKAMI"):
            (card / name).mkdir()
            (card / name / "data.bin").write_bytes(b"x")
        self.scan_ps2()
        tree = self.call("/api/tree?platform=Sony%20PlayStation%202&show=saves")
        by = {f["name"]: f for f in tree["files"]}
        delta = by["Delta (USA)"]                                              # one USA game of that title: it is that game, as any game you lack
        self.assertEqual((delta["state"], delta["row"]["saves"]["total"], delta["row"]["tags"]["regions"]), ("missing", 1, ["USA"]))
        self.assertTrue(delta["row"].get("cs_kind"))                           # (so it has the database's checksums)
        self.assertNotIn("Delta (Europe)", by)
        self.assertEqual((by["Echo (USA)"]["state"], by["Echo (USA)"]["row"]["saves"]["total"]), ("missing", 1))       # (the plain one of two: the name says it)
        self.assertNotIn("Echo (USA) (Greatest Hits)", by)
        fox = by["Foxtrot (USA) (Rev 2)"]                                      # two revisions and no plain one: the latest, the other is listed
        self.assertEqual((fox["state"], fox["row"]["saves"]["total"], fox["row"]["also_fits"]), ("missing", 1, ["Foxtrot (USA) (Rev 1)"]))
        self.assertNotIn("Foxtrot (USA) (Rev 1)", by)
        self.assertEqual(by["Echo (USA)"]["row"]["also_fits"], ["Echo (USA) (Greatest Hits)"])
        self.assertTrue(fox["row"].get("cs_kind"))
        self.assertEqual((by["Ookami (USA)"]["state"], by["Ookami (USA)"]["row"]["saves"]["total"], by["Ookami (USA)"]["row"]["also_fits"]), ("missing", 1, []))

    def gametdb(self, rows: list) -> None:
        import sqlite3
        db = paths.data_dir() / "nintendo" / "gametdb.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE IF NOT EXISTS names (kind TEXT, id TEXT, name TEXT); CREATE TABLE IF NOT EXISTS info (key TEXT, value TEXT);")
        conn.executemany("INSERT INTO names VALUES ('ps2', ?, ?)", rows)
        conn.commit()
        conn.close()

    def test_a_save_of_another_region_is_offered_converted_for_the_release_there(self) -> None:
        self.iso.update({"Golf (Europe)": T.make_iso(38, 17)})
        T.write_dat(paths.redump_dir() / f"{PS2_DAT}.dat", [(n, "Games", [d], "iso") for n, d in self.iso.items()],
                    name=PS2_DAT, version="2026-06-15 03-41-38")
        self.gametdb([("SLUS-20990", "Golf"), ("SLES-59990", "Golf")])
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        (card / "BASLUS-20990GOLF").mkdir()
        (card / "BASLUS-20990GOLF" / "BASLUS-20990GOLF").write_bytes(b"golf")
        self.scan_ps2()
        tree = self.call("/api/tree?platform=Sony%20PlayStation%202&show=saves")
        golf = {f["name"]: f for f in tree["files"]}["Golf (Europe)"]              # (no USA release in the database: Europe, by conversion)
        conv = golf["row"]["convert"]
        self.assertEqual((conv["from"], conv["to"]), ("SLUS-20990", ["SLES-59990"]))
        res = self.call("/api/saves/convert", {"platform": PS2, "set": conv["set"], "to_code": "SLES-59990"})
        self.assertEqual((res["saves"], res["copied"]), (1, 1))
        self.assertEqual((card / "BESLES-59990GOLF" / "BESLES-59990GOLF").read_bytes(), b"golf")
        self.assertTrue((card / "BASLUS-20990GOLF" / "BASLUS-20990GOLF").is_file())          # the original stays

    def test_the_default_converts_a_save_of_a_release_you_lack_for_the_one_you_have(self) -> None:
        from test_nintendo import ps2_iso
        self.iso.update({"Golf (Europe)": ps2_iso("SLES_599.90")})
        T.write_dat(paths.redump_dir() / f"{PS2_DAT}.dat", [(n, "Games", [d], "iso") for n, d in self.iso.items()],
                    name=PS2_DAT, version="2026-06-15 03-41-38")
        self.gametdb([("SLUS-20990", "Golf"), ("SLES-59990", "Golf")])
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        (card / "BASLUS-20990GOLF").mkdir()
        (card / "BASLUS-20990GOLF" / "BASLUS-20990GOLF").write_bytes(b"golf")
        self.put_iso("Golf (Europe)", "Golf (Europe)/Golf (Europe).chd", dvd=True)
        self.scan_ps2()
        plan = self.call("/api/library/plan", {"platform": PS2})
        self.assertEqual(plan["profile"]["saved_games"], "convert")
        row = [r for r in plan["items"] if r.get("game") == "Golf (Europe)"][0]
        self.assertEqual(row["saves"]["effect"], "receive")
        self.assertFalse((card / "BESLES-59990GOLF").exists())                                 # (the preview copied nothing)
        self.call("/api/library/apply", {"platform": PS2, "plan_id": plan["plan_id"]})
        self.assertEqual(self.job()["status"], "done")
        self.assertEqual((card / "BESLES-59990GOLF" / "BESLES-59990GOLF").read_bytes(), b"golf")
        self.assertTrue((card / "BASLUS-20990GOLF" / "BASLUS-20990GOLF").is_file())          # (the existing save stays)

    def test_a_raw_disc_the_build_converts_says_that_its_save_is_converted(self) -> None:
        import zipfile
        from test_nintendo import ps2_iso
        self.iso.update({"Golf (Europe)": ps2_iso("SLES_599.90")})
        T.write_dat(paths.redump_dir() / f"{PS2_DAT}.dat", [(n, "Games", [d], "iso") for n, d in self.iso.items()],
                    name=PS2_DAT, version="2026-06-15 03:41:38")
        self.gametdb([("SLUS-20990", "Golf"), ("SLES-59990", "Golf")])
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        (card / "BASLUS-20990GOLF").mkdir()
        (card / "BASLUS-20990GOLF" / "BASLUS-20990GOLF").write_bytes(b"golf")
        with zipfile.ZipFile(self.roms / "golf.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("golf.iso", self.iso["Golf (Europe)"])
        self.call("/api/library/profile", {"platform": PS2, "convert_first": True})
        self.scan_ps2()
        self.call("/api/library/plan", {"platform": PS2})
        shown = {}
        for d in self.call("/api/tree?platform=Sony%20PlayStation%202&show=")["dirs"]:
            for f in self.call("/api/tree?platform=Sony%20PlayStation%202&show=&path=" + d["name"].replace(" ", "%20"))["files"]:
                shown[f["name"]] = f
        saves = shown["Golf (Europe).chd"]["row"]["saves"]
        self.assertEqual((saves["effect"], saves["convert"]), ("receive", {"from": "SLUS-20990", "to": "SLES-59990"}))
        self.assertEqual(saves["saves"], 1)                                                       # (the one save, counted once)
        dirs = {d["name"]: d for d in self.call("/api/tree?platform=Sony%20PlayStation%202&show=")["dirs"]}
        self.assertEqual(dirs["Golf (Europe)"].get("save_move"), "convert")                       # (the folder says so too)

    def test_a_save_of_another_release_is_flagged_for_a_copy_only_when_the_game_code_differs(self) -> None:
        from test_nintendo import ps2_iso
        self.iso.update({"Delta (USA)": ps2_iso("SLUS_210.00") + b"\1" * 2048, "Delta (USA) (v2.00)": ps2_iso("SLUS_210.00") + b"\2" * 2048,
                         "Golf (Europe)": ps2_iso("SLES_599.90")})
        T.write_dat(paths.redump_dir() / f"{PS2_DAT}.dat", [(n, "Games", [d], "iso") for n, d in self.iso.items()],
                    name=PS2_DAT, version="2026-06-15 03:41:38")
        self.gametdb([("SLUS-21000", "Delta"), ("SLUS-20990", "Golf"), ("SLES-59990", "Golf")])
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        for d in ("BASLUS-21000DELTA", "BASLUS-20990GOLF"):
            (card / d).mkdir()
            (card / d / d).write_bytes(b"s")
        self.put_iso("Delta (USA) (v2.00)", "Delta (USA) (v2.00)/Delta (USA) (v2.00).chd", dvd=True)
        self.put_iso("Golf (Europe)", "Golf (Europe)/Golf (Europe).chd", dvd=True)
        self.call("/api/library/profile", {"platform": PS2, "saved_games": "archive"})
        self.scan_ps2()
        rows = {}
        for d in self.call("/api/tree?platform=Sony%20PlayStation%202&show=")["dirs"]:
            for f in self.call("/api/tree?platform=Sony%20PlayStation%202&show=&path=" + d["name"].replace(" ", "%20"))["files"]:
                rows[f["name"]] = f["row"] or {}
        self.assertIn("Delta (USA) (v2.00).chd", rows, sorted(rows))
        delta, golf = rows["Delta (USA) (v2.00).chd"], rows["Golf (Europe).chd"]
        self.assertEqual(((delta.get("saves") or {}).get("saves"), delta.get("save_copy")), (1, None))  # (the same code: it is this disc's own save)
        self.assertEqual((bool(golf.get("save_for")), golf.get("save_copy")), (True, True))          # (another code: only a converted copy works)

    def test_a_disc_you_have_is_offered_the_save_of_its_other_region(self) -> None:
        import shutil
        self.call("/api/library/profile", {"platform": PS2, "saved_games": "keep", "prefer_saved": False})
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        shutil.rmtree(card / "BESLES-50012OTHER")                                        # (the European disc has no save of its own)
        self.scan_ps2()
        self.call("/api/library/plan", {"platform": PS2})
        tree = self.call("/api/tree?platform=Sony%20PlayStation%202&show=have")
        rows = []
        for d in tree["dirs"]:
            rows += self.call("/api/tree?platform=Sony%20PlayStation%202&show=have&path=" + d["name"].replace(" ", "%20"))["files"]
        europe = [f for f in rows if f["name"].startswith("Gamma (Europe)")][0]["row"]
        self.assertTrue(europe["convert"]["disc"])
        self.assertEqual(europe["convert"]["from"], "SLUS-20946")
        res = self.call("/api/saves/convert", {"platform": PS2, "set": europe["convert"]["set"], "to_file": europe["from"]})
        self.assertEqual((res["old"], res["new"], res["saves"]), ("SLUS-20946", "SLES-50012", 1))
        self.assertEqual((card / "BESLES-50012GAMMA" / "data.bin").read_bytes(), b"save")
        self.call("/api/saves/convert", {"platform": PS2, "set": europe["convert"]["set"], "to_code": "nonsense"}, expect=400)

    def test_the_scan_counts_the_saves_of_each_game_and_the_browse_list_shows_them(self) -> None:
        games = {g["name"]: g for g in self.call("/api/scan/results?kind=games")["items"]}
        self.assertEqual(games["Gamma (USA)"]["saves"]["total"], 2)            # a card save and a state
        self.assertEqual(games["Gamma (USA)"]["saves"]["saves"], 1)
        self.assertEqual(games["Gamma (USA)"]["saves"]["states"], 1)
        self.assertEqual(games["Gamma (Europe)"]["saves"]["total"], 1)
        sets = self.call("/api/scan/results?kind=saves")["items"]
        self.assertEqual({s["source"] for s in sets}, {"pcsx2"})

    def test_a_system_without_its_emulator_has_no_saves_at_all(self) -> None:
        self.call("/api/emulators/config", {"source": "pcsx2", "enabled": False})
        self.scan_ps2()
        plan = self.call("/api/library/plan", {"platform": PS2})
        self.assertNotIn("saves", plan)
        self.assertNotIn("saved_games", self.call("/api/library/profile?platform=" + PS2.replace(" ", "%20"))["profile"])

    def test_with_keep_the_disc_game_stays(self) -> None:
        self.call("/api/library/profile", {"platform": PS2, "saved_games": "keep", "prefer_saved": False})
        plan = self.call("/api/library/plan", {"platform": PS2})
        self.assertEqual((plan["reasons"]["kept_saved"], plan["saves"]["kept"]), (1, 1))
        self.assertEqual((plan["reasons"]["superseded"], plan["reasons"]["excluded"]), (0, 0))
        self.call("/api/library/apply", {})
        self.assertEqual(self.job()["status"], "done")
        self.assertEqual(self.games(), ["Gamma (Europe)/Gamma (Europe).chd", "Gamma (USA)/Gamma (USA).chd"])

    def test_a_running_emulator_keeps_its_saves_where_they_are(self) -> None:
        from romorg import emulators
        self.call("/api/library/profile", {"platform": PS2, "saved_games": "archive"})
        aside = self.base / "archive"
        with mock.patch.object(emulators, "source_running", lambda source: source == "pcsx2"):
            plan = self.call("/api/library/plan", {"platform": PS2, "aside_to": str(aside)})
            self.assertEqual(plan["saves"]["archive"]["running"], ["PCSX2"])
            self.call("/api/library/apply", {"aside_to": str(aside), "plan_id": plan["plan_id"]})
            res = self.job()
        self.assertEqual(res["status"], "done", res)
        sa = res["result"]["saves_archived"]
        self.assertEqual((sa["skipped_running"], sa["running"], sa["moved"]), (True, ["PCSX2"], 0))
        self.assertTrue((self.pcsx2 / "memcards" / "Mcd001.ps2" / "BASLUS-20946GAMMA" / "data.bin").is_file())      # (the saves did not move)
        self.assertEqual(self.games(), ["Gamma (Europe)/Gamma (Europe).chd"])                                      # (the game was archived)

    def test_archive_takes_the_saves_along(self) -> None:
        self.call("/api/library/profile", {"platform": PS2, "saved_games": "archive"})
        aside = self.base / "archive"
        plan = self.call("/api/library/plan", {"platform": PS2, "aside_to": str(aside)})
        self.assertEqual(plan["saves"]["archive"]["files"], 2)
        self.call("/api/library/apply", {"aside_to": str(aside), "plan_id": plan["plan_id"]})
        res = self.job()
        self.assertEqual(res["status"], "done", res)
        self.assertEqual(res["result"]["saves_archived"]["moved"], 2)
        self.assertEqual(self.games(), ["Gamma (Europe)/Gamma (Europe).chd"])
        there = aside / "ps2" / "_saves"
        self.assertTrue((there / "Mcd001.ps2" / "BASLUS-20946GAMMA" / "data.bin").is_file())
        self.assertTrue((there / "sstates" / "SLUS-20946 (AABBCCDD).01.p2s").is_file())
        self.assertFalse((self.pcsx2 / "memcards" / "Mcd001.ps2" / "BASLUS-20946GAMMA").exists())
        self.assertTrue((self.pcsx2 / "memcards" / "Mcd001.ps2" / "BESLES-50012OTHER" / "data.bin").is_file())   # (the other game's save stays)


if __name__ == "__main__":
    unittest.main()



class ConvertPs2SavesTests(DiscSavesTests):
    """``convert``: a USA save is moved to the European release's code (a folder card: copied; a card image: rewritten in place)."""

    def convert(self) -> dict:
        self.call("/api/library/profile", {"platform": PS2, "saved_games": "convert"})
        plan = self.call("/api/library/plan", {"platform": PS2})
        self.call("/api/library/apply", {"plan_id": plan["plan_id"]})
        res = self.job()
        self.assertEqual(res["status"], "done", res)
        return plan, res["result"]

    def test_a_folder_card_save_is_copied_under_the_other_releases_code(self) -> None:
        self.call("/api/library/profile", {"platform": PS2, "saved_games": "convert"})
        plan = self.call("/api/library/plan", {"platform": PS2})
        self.assertEqual((plan["reasons"]["kept_saved"], plan["reasons"]["superseded"]), (0, 1))
        self.assertEqual(plan["saves"]["ps2"], {"saves": 1, "conflicts": 0})
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        self.assertFalse((card / "BESLES-50012GAMMA").exists())                      # (the preview copied nothing)
        self.call("/api/library/apply", {"plan_id": plan["plan_id"]})
        res = self.job()
        self.assertEqual(res["status"], "done", res)
        self.assertEqual(res["result"]["ps2_saves"]["saves"], 1)
        self.assertEqual((card / "BESLES-50012GAMMA" / "data.bin").read_bytes(), b"save")
        self.assertEqual((card / "BASLUS-20946GAMMA" / "data.bin").read_bytes(), b"save")        # the old save is not touched
        self.assertEqual((card / "BESLES-50012OTHER" / "data.bin").read_bytes(), b"other")
        self.assertEqual(self.games(), ["Gamma (Europe)/Gamma (Europe).chd", "_superseded/Gamma (USA)/Gamma (USA).chd"])        # (the USA disc is set aside)

    def test_a_save_the_card_has_under_the_new_code_is_not_overwritten(self) -> None:
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        (card / "BESLES-50012GAMMA").mkdir()
        (card / "BESLES-50012GAMMA" / "data.bin").write_bytes(b"mine")
        self.call("/api/library/profile", {"platform": PS2, "saved_games": "convert"})
        plan = self.call("/api/library/plan", {"platform": PS2})
        self.assertEqual(plan["saves"]["ps2"], {"saves": 0, "conflicts": 1})
        self.call("/api/library/apply", {"plan_id": plan["plan_id"]})
        self.assertEqual(self.job()["status"], "done")
        self.assertEqual((card / "BESLES-50012GAMMA" / "data.bin").read_bytes(), b"mine")

    def test_a_card_image_is_rewritten_in_place_and_the_old_image_is_kept(self) -> None:
        import shutil
        from test_nintendo import ps2_card_image
        from romorg import pcsx2
        card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        shutil.rmtree(card)
        image = ps2_card_image({"BASLUS-20946GAMMA": {"BASLUS-20946GAMMA": b"s" * 2000, "icon.sys": b"i" * 100}, "BESLES-50012OTHER": {"x": b"o" * 10}}, ecc=True)
        card.write_bytes(image)
        self.call("/api/library/profile", {"platform": PS2, "saved_games": "convert"})
        plan = self.call("/api/library/plan", {"platform": PS2})
        self.assertEqual(plan["saves"]["ps2"], {"saves": 1, "conflicts": 0})
        self.assertEqual(card.read_bytes(), image)                                               # (the preview wrote nothing)
        self.call("/api/library/apply", {"plan_id": plan["plan_id"]})
        res = self.job()
        self.assertEqual(res["status"], "done", res)
        self.assertEqual(res["result"]["ps2_saves"]["replaced"], 1)
        names = sorted(s.name for s in pcsx2.read_card_image(card))
        self.assertEqual(names, ["BESLES-50012GAMMA", "BESLES-50012OTHER"])
        kept = list(paths.data_dir().joinpath("save-backups", "replaced").glob("*Mcd001.ps2"))
        self.assertEqual([p.read_bytes() for p in kept], [image])                                # the old card, as it was


class MissingFollowsTheRules(PsServerCase):
    def test_missing_lists_the_games_the_rules_want_not_every_game_of_the_database(self) -> None:
        self.iso.update({"Delta (USA)": T.make_iso(34, 21), "Delta (Europe)": T.make_iso(35, 22), "Delta (USA) (Demo)": T.make_iso(36, 23)})
        T.write_dat(paths.redump_dir() / f"{PS2_DAT}.dat", [(n, "Games", [d], "iso") for n, d in self.iso.items()],
                    name=PS2_DAT, version="2026-06-15 03:41:38")
        self.put_iso("Alpha (USA)", "Alpha (USA)/Alpha (USA).chd")
        self.scan_ps2()
        tree = self.call("/api/tree?platform=Sony%20PlayStation%202&show=missing")
        names = sorted(f["name"] for f in tree["files"])
        self.assertEqual(sum(n.startswith("Delta") for n in names), 1, names)                  # (one release of the game: the one the rules prefer, not the three)
        self.assertNotIn("Delta (USA) (Demo)", names)                                            # (a demo is no game the rules want)
        self.assertEqual(tree["facets"]["show"]["missing"], len(names) - 1)                       # (counted in games: the two discs of Part are one)
        self.assertEqual(tree["facets"]["unit"], "games")

    def test_the_discs_of_a_game_count_once_and_its_playlist_not_at_all(self) -> None:
        self.put_iso("Alpha (USA)", "Alpha (USA)/Alpha (USA).chd")
        self.put_iso("Part (USA) (Disc 1)", "Part (USA) (Disc 1)/Part (USA) (Disc 1).chd")
        self.put_iso("Part (USA) (Disc 2)", "Part (USA) (Disc 2)/Part (USA) (Disc 2).chd")
        self.scan_ps2()
        self.call("/api/library/plan", {"platform": PS2})
        tree = self.call("/api/tree?platform=Sony%20PlayStation%202&show=")
        show = tree["facets"]["show"]
        self.assertEqual(show["have"], 2)                                                           # (Alpha and Part: three disc images)
        self.assertEqual(tree["totals"]["files"], 3)                                                # (the tree itself still lists the files)


class RawDiscTests(PsServerCase):
    """A disc that is not a CHD: an .iso, or a zip / 7z of one. It is identified by its checksums, named after its game, and converted
    (with the rule Convert when building) as a step of Build library; the originals are kept, or deleted with their own rule."""

    def zip_of(self, name: str, game: str, member: str = "disc.iso") -> Path:
        import zipfile
        path = self.roms / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(member, self.iso[game])
        return path

    def plan(self) -> dict:
        return self.call("/api/library/plan", {"platform": PS2, "limit": 200})

    def row(self, plan: dict, name: str) -> dict:
        return next(r for r in plan["items"] if r["from"].endswith(name))

    def build(self) -> dict:
        plan = self.plan()
        self.call("/api/library/apply", {"platform": PS2, "plan_id": plan["plan_id"]})
        res = self.job()
        self.assertEqual(res["status"], "done", res)
        return res["result"]

    def test_a_zip_of_an_iso_is_identified_by_its_checksums_and_named_after_its_game(self) -> None:
        self.zip_of("download (1).zip", "Alpha (USA)")
        (self.roms / "notes.zip").write_bytes(b"PK\x05\x06" + bytes(18))                       # an empty zip: no disc in it, as before
        self.call("/api/library/profile", {"platform": PS2, "convert_first": False})          # named, not converted
        self.scan_ps2()
        matched = {m["file"]: m for m in self.call("/api/scan/results?kind=matched")["items"]}
        self.assertEqual((matched["download (1).zip"]["game"], matched["download (1).zip"]["kind"]), ("Alpha (USA)", "raw"))
        plan = self.plan()
        row = self.row(plan, "download (1).zip")
        self.assertEqual((row["to"], row["status"]), ("Alpha (USA)/Alpha (USA).zip", "move"))
        self.assertEqual(self.row(plan, "notes.zip")["category"], "unmatched")
        self.build()
        self.assertTrue((self.roms / "Alpha (USA)" / "Alpha (USA).zip").is_file())

    def test_a_raw_disc_you_have_is_a_game_you_have_in_the_totals(self) -> None:
        import time
        self.zip_of("a.zip", "Alpha (USA)")
        self.scan_ps2()
        t = {}
        for _ in range(100):
            t = self.call("/api/library/totals?platform=Sony%20PlayStation%202")
            if t.get("have_games") is not None and not t.get("calculating"):
                break
            time.sleep(0.1)
        self.assertEqual(t.get("have_games"), 1)                                            # (not missing until it is a CHD)

    def test_a_zip_of_another_dump_is_not_matched(self) -> None:
        import zipfile
        with zipfile.ZipFile(self.roms / "other.zip", "w") as zf:
            zf.writestr("other.iso", self.iso["Alpha (USA)"][:-2048] + bytes(2048))
        self.scan_ps2()
        reasons = {u["file"]: u["reason"] for u in self.call("/api/scan/results?kind=unmatched")["items"]}
        self.assertIn("do not match", reasons["other.zip"])

    def test_a_loose_iso_is_renamed_with_its_game_into_its_folder(self) -> None:
        (self.roms / "VICE_CITY.iso").write_bytes(self.iso["Beta (Europe) (En,Fr,De)"])
        self.call("/api/library/profile", {"platform": PS2, "convert_first": False})          # renamed, not converted
        self.scan_ps2()
        row = self.row(self.plan(), "VICE_CITY.iso")
        self.assertEqual((row["to"], row["status"]), ("Beta (Europe) (En,Fr,De)/Beta (Europe) (En,Fr,De).iso", "move"))
        self.build()
        self.assertTrue((self.roms / "Beta (Europe) (En,Fr,De)" / "Beta (Europe) (En,Fr,De).iso").is_file())

    def test_the_rule_converts_a_zip_as_a_step_of_the_build_and_the_tree_says_so_first(self) -> None:
        self.zip_of("a.zip", "Alpha (USA)")
        self.assertTrue(self.call("/api/library/profile?platform=" + PS2.replace(" ", "%20"))["profile"]["delete_converted"])   # on by default
        self.call("/api/library/profile", {"platform": PS2, "convert_first": True, "delete_converted": False})
        self.scan_ps2()
        plan = self.plan()
        self.assertEqual(plan["convert"], {"count": 1, "kind": "chd", "archives": 1, "delete": False})
        self.assertEqual(self.row(plan, "a.zip")["to"], "Alpha (USA)/Alpha (USA).chd")             # (the name it will have once converted)
        tree = self.call("/api/tree?platform=Sony%20PlayStation%202&show=")
        names = {f["name"]: f for f in tree["files"]}
        self.assertEqual(names["_converted_originals"] if "_converted_originals" in names else None, None)
        self.assertEqual({d["name"]: (d.get("tags") or {}).get("regions") for d in tree["dirs"]}["Alpha (USA)"], ["USA"])        # (a folder shows its game's tags)
        self.assertEqual({d["name"]: d.get("states") for d in tree["dirs"]}["Alpha (USA)"], {"converted": 1})                 # (and what the build does below it)
        shown = {}
        for d in tree["dirs"]:
            shown[d["name"]] = self.call("/api/tree?platform=Sony%20PlayStation%202&show=&path=" + d["name"].replace(" ", "%20"))["files"]
        self.assertEqual([(f["name"], f["state"]) for f in shown["Alpha (USA)"]], [("Alpha (USA).chd", "converted")])
        self.assertEqual([(f["name"], f["state"]) for f in shown["_converted_originals"]], [("a.zip", "moved")])
        res = self.build()
        self.assertEqual((res["converted"]["count"], res["converted"]["failed"]), (1, []))
        self.assertTrue((self.roms / "Alpha (USA)" / "Alpha (USA).chd").is_file())
        self.assertTrue((self.roms / "_converted_originals" / "a.zip").is_file())                  # kept
        self.assertEqual(list(self.roms.glob(".romorg-chd-*")), [])                                # no unpacked copy is left behind
        self.assertEqual(self.call("/api/scan/results?kind=matched")["total"], 1)

    def test_a_disc_of_a_set_with_a_disc_missing_is_archived_unconverted_when_complete_sets_only_is_on(self) -> None:
        self.zip_of("p1.zip", "Part (USA) (Disc 1)")
        self.call("/api/library/profile", {"platform": PS2, "convert_first": True, "complete_only": True})
        self.scan_ps2()
        plan = self.plan()
        self.assertEqual(plan["convert"]["count"], 0)
        row = self.row(plan, "p1.zip")
        self.assertEqual(row["to"], "_incomplete/p1.zip")
        self.assertIn("missing disc 2", row["reason"])
        self.assertEqual([x["missing"] for x in plan["incomplete_sets"]], [[2]])
        res = self.build()
        self.assertNotIn("converted", res)
        self.assertTrue((self.roms / "_incomplete" / "p1.zip").is_file())
        self.assertEqual(list(self.roms.rglob("*.chd")), [])

    def test_with_complete_sets_only_off_a_disc_of_an_incomplete_set_is_converted_and_kept(self) -> None:
        self.zip_of("p1.zip", "Part (USA) (Disc 1)")
        self.call("/api/library/profile", {"platform": PS2, "convert_first": True, "complete_only": False})
        self.scan_ps2()
        plan = self.plan()
        self.assertEqual(plan["convert"]["count"], 1)
        self.assertEqual(self.row(plan, "p1.zip")["to"], "Part (USA)/Part (USA) (Disc 1)/Part (USA) (Disc 1).chd")
        self.build()
        self.assertTrue((self.roms / "Part (USA)" / "Part (USA) (Disc 1)" / "Part (USA) (Disc 1).chd").is_file())
        self.assertFalse((self.roms / "_incomplete").exists())

    def test_the_originals_can_be_deleted_after_converting(self) -> None:
        self.zip_of("a.zip", "Alpha (USA)")
        (self.roms / "b.iso").write_bytes(self.iso["Beta (Europe) (En,Fr,De)"])
        self.call("/api/library/profile", {"platform": PS2, "convert_first": True, "delete_converted": True})
        self.scan_ps2()
        plan = self.plan()
        self.assertEqual(plan["convert"], {"count": 2, "kind": "chd", "archives": 1, "delete": True})
        tree = self.call("/api/tree?platform=Sony%20PlayStation%202&show=")
        self.assertEqual([f["name"] for f in tree["files"] if f["name"] in ("a.zip", "b.iso")], [])          # (no row under the old name)
        names = {d["name"] for d in tree["dirs"]}
        self.assertTrue({"Alpha (USA)", "Beta (Europe) (En,Fr,De)"} <= names)
        res = self.build()
        self.assertEqual(res["converted"]["count"], 2)
        self.assertTrue((self.roms / "Alpha (USA)" / "Alpha (USA).chd").is_file())
        self.assertTrue((self.roms / "Beta (Europe) (En,Fr,De)" / "Beta (Europe) (En,Fr,De).chd").is_file())
        self.assertFalse((self.roms / "a.zip").exists() or (self.roms / "b.iso").exists())
        self.assertFalse((self.roms / "_converted_originals").exists())                           # nothing was kept

    def test_an_original_that_cannot_be_deleted_never_costs_the_checked_chd(self) -> None:
        self.zip_of("a.zip", "Alpha (USA)")
        self.call("/api/library/profile", {"platform": PS2, "convert_first": True, "delete_converted": True})
        self.scan_ps2()
        real = os.unlink

        def unlink(path: Any, *a: Any, **k: Any) -> None:
            if os.path.basename(path) == "a.zip":
                raise PermissionError(13, "in use", str(path))
            real(path, *a, **k)
        with mock.patch("os.unlink", side_effect=unlink):
            res = self.build()
        self.assertTrue((self.roms / "Alpha (USA)" / "Alpha (USA).chd").is_file())                # (checked: it stays)
        self.assertTrue((self.roms / "a.zip").is_file() or list(self.roms.rglob("a.zip")))       # (left where it is, reported)
        self.assertEqual(res["converted"]["count"], 1)

    def test_a_game_that_already_has_a_chd_is_not_converted_and_its_zip_is_left_alone(self) -> None:
        self.put_iso("Alpha (USA)", "Alpha (USA)/Alpha (USA).chd", dvd=True)
        self.zip_of("Alpha (USA) copy.zip", "Alpha (USA)")
        self.call("/api/library/profile", {"platform": PS2, "convert_first": True, "delete_converted": True})
        self.scan_ps2()
        plan = self.plan()
        self.assertEqual(plan["convert"]["count"], 0)
        self.build()
        self.assertTrue((self.roms / "Alpha (USA) copy.zip").is_file() or list(self.roms.rglob("Alpha (USA) copy.zip")))      # never deleted unconverted

    def test_a_7z_of_an_iso_is_read_like_a_zip(self) -> None:
        from tests.sevenzipmini import write_7z
        write_7z(self.roms / "b.7z", {"readme.txt": b"a note", "Some Folder/disc.iso": self.iso["Alpha (USA)"]})
        self.call("/api/library/profile", {"platform": PS2, "convert_first": True, "delete_converted": False})
        self.scan_ps2()
        matched = {m["file"]: m for m in self.call("/api/scan/results?kind=matched")["items"]}
        self.assertEqual((matched["b.7z"]["game"], matched["b.7z"]["kind"]), ("Alpha (USA)", "raw"))
        self.assertEqual(self.plan()["convert"]["archives"], 1)
        res = self.build()
        self.assertEqual((res["converted"]["count"], res["converted"]["failed"]), (1, []))
        self.assertTrue((self.roms / "Alpha (USA)" / "Alpha (USA).chd").is_file())
        self.assertTrue((self.roms / "_converted_originals" / "b.7z").is_file())

    def test_a_damaged_archive_is_left_alone(self) -> None:
        (self.roms / "broken.zip").write_bytes(b"PK\x03\x04" + bytes(300))
        (self.roms / "broken.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + bytes(100))
        self.call("/api/library/profile", {"platform": PS2, "convert_first": True, "delete_converted": True})
        self.scan_ps2()
        self.assertEqual(self.plan()["convert"]["count"], 0)
        self.build()
        self.assertTrue(list(self.roms.rglob("broken.zip")) and list(self.roms.rglob("broken.7z")))   # (set aside as unmatched, never deleted)
