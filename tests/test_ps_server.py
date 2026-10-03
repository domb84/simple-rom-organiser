"""PlayStation 2 / PlayStation through the HTTP API: platforms, scan, Verify fully, organise, library (no playlists
for the PS2), convert (createdvd) - synthetic discs, throwaway data dir, fake chdman."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

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
        plan = self.call("/api/organise/plan", {})
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


if __name__ == "__main__":
    unittest.main()
