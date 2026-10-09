"""PlayStation 2 / PlayStation through the HTTP API: platforms, scan, Verify fully, organise, library (no playlists
for the PS2), convert (createdvd) - synthetic discs, throwaway data dir, fake chdman."""

from __future__ import annotations

import json
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
                         {"Sega - Dreamcast", PSX_DAT, PS2_DAT, "Nintendo - GameCube", "Nintendo - Wii"})

    def test_folder_is_saved_per_system_immediately(self) -> None:
        self.call("/api/folders", {"platform": PS2, "path": str(self.roms)})
        rows = {p["name"]: p for p in self.call("/api/platforms")}
        self.assertEqual(rows[PS2]["folder"], str(self.roms.resolve()))
        self.assertIsNone(rows[PSX]["folder"])

    def test_scan_verify_organise_library_and_undo(self) -> None:
        self.call("/api/chdman", {"engine": "python"})
        self.put_iso("Alpha (USA)", "Alpha loose.chd", ["Alpha loose.state"])
        self.put_iso("Beta (Europe) (En,Fr,De)", "Beta/b.chd", dvd=True)
        self.put_iso("Part (USA) (Disc 1)", "p1.chd")
        self.put_iso("Part (USA) (Disc 2)", "p2.chd")
        res = self.scan_ps2()
        s = res
        self.assertEqual((s["verified"], s["identified"], s["system"], s["have"]), (3, 1, "ps2", 4))
        res = self.run_job("/api/dc/verify", {})
        self.assertEqual((res["verified"], res["failed"]), (1, []))
        self.assertEqual(res["summary"]["identified"], 0)
        plan = self.call("/api/library/plan", {"platform": PS2})
        row = [r for r in plan["items"] if r.get("game") == "Alpha (USA)"][0]
        self.assertEqual((row["status"], row["to"], row["files"]), ("move", "Alpha (USA)/Alpha (USA).chd", 2))
        lib = self.call("/api/library/plan", {"platform": PS2})
        self.assertEqual(lib["playlists"]["write"], 0)
        self.call("/api/library/apply", {})
        j = self.job()
        self.assertEqual(j["status"], "done", j)
        self.assertTrue((self.roms / "Alpha (USA)" / "Alpha (USA).state").is_file())
        self.assertTrue((self.roms / "Part (USA) (Disc 2)" / "Part (USA) (Disc 2).chd").is_file())
        self.assertFalse(list(self.roms.rglob("*.m3u")))
        logs = self.call(f"/api/organise/undo-logs?path={self.roms}")["logs"]
        self.call("/api/organise/undo", {"log": logs[0]["log"]})
        self.assertEqual(self.job()["status"], "done")
        self.assertTrue((self.roms / "Alpha loose.chd").is_file())

    def test_convert_plan_offers_createdvd_for_an_iso(self) -> None:
        (self.roms / "raw").mkdir()
        (self.roms / "raw" / "a.iso").write_bytes(self.iso["Alpha (USA)"])
        iso_file = self.base / "fake.iso"
        iso_file.write_bytes(self.iso["Alpha (USA)"])
        fixture = self.base / "alpha.dvd.chd"
        T.build_dvd_chd(fixture, self.iso["Alpha (USA)"])
        self.use_fake_chdman()
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

    def test_keep_is_the_default_and_the_disc_game_stays(self) -> None:
        plan = self.call("/api/library/plan", {"platform": PS2})
        self.assertEqual((plan["reasons"]["kept_saved"], plan["saves"]["kept"]), (1, 1))
        self.assertEqual((plan["reasons"]["superseded"], plan["reasons"]["excluded"]), (0, 0))
        self.call("/api/library/apply", {})
        self.assertEqual(self.job()["status"], "done")
        self.assertEqual(self.games(), ["Gamma (Europe)/Gamma (Europe).chd", "Gamma (USA)/Gamma (USA).chd"])

    def test_archive_takes_the_saves_along_and_undo_brings_them_back(self) -> None:
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
        self.call("/api/library/undo", {"log": res["result"]["undo_log"]})
        back = self.job()
        self.assertEqual(back["status"], "done", back)
        self.assertEqual(self.games(), ["Gamma (Europe)/Gamma (Europe).chd", "Gamma (USA)/Gamma (USA).chd"])
        self.assertTrue((self.pcsx2 / "memcards" / "Mcd001.ps2" / "BASLUS-20946GAMMA" / "data.bin").is_file())
        self.assertTrue((self.pcsx2 / "sstates" / "SLUS-20946 (AABBCCDD).01.p2s").is_file())

    def test_collection_sort_a_disc_games_folder_keeps_everything_in_it_saves_beside_the_chd_included(self) -> None:
        """The user's real shape (psx/Spider - The Video Game (USA)/ with .srm .state .state1 beside the CHD) in a folder that
        has to be sorted: saves beside a ROM are ordinary files - the whole folder moves into the system's folder."""
        mixed = self.base / "mixed"
        game = mixed / "Dump" / "Gamma (Europe)"
        game.mkdir(parents=True)
        T.build_dvd_chd(game / "Gamma (Europe).chd", self.iso["Gamma (Europe)"])
        names = ("Gamma (Europe).zip", "Gamma (Europe).md5", "Gamma (Europe).srm", "Gamma (Europe).state", "Gamma (Europe).state1")
        for n in names:
            (game / n).write_bytes(n.encode())
        self.call("/api/collection/save", {"root": str(mixed)})
        self.run_job("/api/collection/scan", {})
        res = self.run_job("/api/collection/apply", {})
        self.assertEqual(res["sort"]["result"]["failed"], [])
        there = mixed / "ps2" / "Gamma (Europe)"
        self.assertEqual(sorted(p.name for p in there.iterdir()), sorted(("Gamma (Europe).chd",) + names))
        self.assertFalse((self.base / "rom-archive" / "_other").exists())
        self.call("/api/collection/undo", {})
        self.assertEqual(sorted(p.name for p in game.iterdir()), sorted(("Gamma (Europe).chd",) + names))


if __name__ == "__main__":
    unittest.main()


class MixedRootSortTests(PsServerCase):
    """A folder of mixed discs: ONE scan reads every CHD once and tries it against the DATs of all disc systems; the sort and
    the tidy then work from that scan."""

    def test_discs_of_two_systems_are_found_in_one_scan_and_sorted(self) -> None:
        from romorg import discsys
        self.call("/api/chdman", {"engine": "python"})
        self.call("/api/settings/archive", {"dir": str(self.base / "rom-archive")})              # (there is no default archive folder)
        mixed = self.base / "mixed"
        dump = mixed / "Dump"
        dump.mkdir(parents=True)
        self.discs["Beta (Europe)"].write_chd(dump / "dc game.chd")                       # a Dreamcast GD-ROM
        T.build_chd(dump / "ps2 cd.chd", [{"type": "MODE1", "data": self.iso["Alpha (USA)"]}], gd=False)
        T.build_dvd_chd(dump / "ps2 dvd.chd", self.iso["Beta (Europe) (En,Fr,De)"])
        T.build_chd(dump / "unknown.chd", [{"type": "MODE1", "data": T.make_iso(36, 99)}], gd=False)
        (dump / "cover.jpg").write_bytes(b"jpg")
        self.call("/api/collection/save", {"root": str(mixed)})
        reads: dict[tuple, int] = {}
        real = discsys.hash_tracks_python

        def spy(info, idx, *a, **k):
            for i in idx:
                key = (str(getattr(info, "path", "")), i)
                reads[key] = reads.get(key, 0) + 1
            return real(info, idx, *a, **k)

        with mock.patch.object(discsys, "hash_tracks_python", spy):
            scan = self.run_job("/api/collection/scan", {})["scan"]
            self.assertEqual({s["name"]: s["games"] for s in scan["systems"]}, {"Sega Dreamcast": 1, PS2: 2})
            self.assertEqual((scan["unmatched"], scan["other"]), (1, 1))
            self.assertTrue(reads and max(reads.values()) == 1, f"a track was decoded more than once: {reads}")
            before = dict(reads)
            plan = self.run_job("/api/collection/plan", {})
            self.assertEqual(reads, before)                                                  # the preview reads no disc again
        self.assertEqual(plan["sort"]["counts"], {"Sega Dreamcast": 1, PS2: 2, "_unmatched": 1, "_other": 1}, plan["sort"])
        self.assertTrue((dump / "cover.jpg").exists())
        res = self.run_job("/api/collection/apply", {})
        self.assertEqual(res["sort"]["result"]["failed"], [])
        self.assertEqual(len(list((mixed / "dreamcast").rglob("*.chd"))), 1)
        self.assertEqual(len(list((mixed / "ps2").rglob("*.chd"))), 2)
        self.assertTrue((self.base / "rom-archive" / "_unmatched" / "Dump" / "unknown.chd").is_file())
        self.assertTrue((self.base / "rom-archive" / "_other" / "Dump" / "cover.jpg").is_file())

    def test_collection_can_convert_raw_discs_to_chd_while_tidying(self) -> None:
        self.use_fake_chdman()
        self.call("/api/chdman", {"engine": "python"})
        raw_e = T.prepare_fake_raw(self.base / "fakeraw_c", self.discs["Epsilon (USA)"])
        os.environ["FAKE_RAW"] = str(raw_e)
        mixed = self.base / "mixed2"
        self.discs["Epsilon (USA)"].write_raw(mixed / "Dump" / "raw set", "e")
        self.call("/api/collection/save", {"root": str(mixed)})
        scan = self.run_job("/api/collection/scan", {})["scan"]
        row = next(s for s in scan["systems"] if s["name"] == "Sega Dreamcast")
        self.assertEqual((row["convert"], row["chd"]), (1, True))
        self.call("/api/collection/save", {"convert": True})
        plan = self.run_job("/api/collection/plan", {})
        self.assertEqual(next(r for r in plan["systems"] if r["platform"] == "Sega Dreamcast")["convert"], 1)
        self.assertTrue((mixed / "Dump" / "raw set" / "e.gdi").is_file())                  # a preview converts nothing
        res = self.run_job("/api/collection/apply", {})
        self.assertFalse([r for r in res["systems"] if r["status"] != "ok"], res)
        self.assertTrue(list((mixed / "dreamcast").rglob("*.chd")))
        self.assertTrue(list((mixed / "dreamcast" / "_converted_originals").rglob("e.gdi")))
        steps = []                                                                         # one move per file, converting included
        for log in mixed.glob(".romorg-undo-*.json"):
            steps += [json.loads(line) for line in log.read_text().splitlines()[1:]]
        steps = [(s["src"], s["dst"]) for s in steps if s.get("op") == "move"]
        self.assertEqual(len({a for a, _b in steps}), len(steps))
        self.assertFalse({a for a, _b in steps} & {b for _a, b in steps})
        self.call("/api/collection/undo", {})
        self.assertTrue((mixed / "Dump" / "raw set" / "e.gdi").is_file())                  # the raw set is back where it was
        self.assertFalse(list(mixed.glob("dreamcast/**/*.chd")))
