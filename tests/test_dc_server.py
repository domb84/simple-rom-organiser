"""Sega Dreamcast through the real HTTP server (real modules, synthetic discs, fake chdman)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
from romorg import paths, server  # noqa: E402

PLAT = "Sega Dreamcast"
DAT = "Sega - Dreamcast"


class DcServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.data = self.base / "data"
        self.data.mkdir()
        p = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.data), "ROMORG_OFFLINE": "1"})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("ROMORG_CHDMAN", None)
        self.roms = self.base / "roms"
        self.roms.mkdir()
        self.discs = {"Alpha (USA) (En,Fr)": T.Disc("a", 1), "Alpha (Europe) (En,Fr)": T.Disc("a2", 4),
                      "Beta (Europe)": T.Disc("b", 2), "Gamma (Japan)": T.Disc("g", 3),
                      "Delta (USA) (Demo)": T.Disc("d", 5), "Epsilon (USA)": T.Disc("e", 8),
                      "Big Game (USA) (Disc 1)": T.Disc("b1", 6), "Big Game (USA) (Disc 2)": T.Disc("b2", 7)}
        T.write_dat(paths.redump_dir() / f"{DAT}.dat",
                    [(n, "Demos" if "Demo" in n else "Games", d.bins) for n, d in self.discs.items()])
        # fake chdman: extractcd hands out Beta's tracks, createcd Epsilon's CHD
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.fake = T.install_fake_chdman(self.bin)
        self.fixture = self.base / "epsilon.chd"
        self.discs["Epsilon (USA)"].write_chd(self.fixture)
        self.raw = T.prepare_fake_raw(self.base / "fakeraw", self.discs["Beta (Europe)"])
        p = mock.patch.dict(os.environ, {"FAKE_CHD": str(self.fixture), "FAKE_RAW": str(self.raw)})
        p.start()
        self.addCleanup(p.stop)
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, path: str, body=None, expect: int = 200):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.port}{path}", data=data,
                                     headers={"Content-Type": "application/json", "X-Romorg-Token": "t"})
        try:
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, expect)
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read() or b"{}")
            self.assertEqual(exc.code, expect, payload)
            return payload

    def job(self, started: dict | None = None) -> dict:
        end = time.time() + 120
        while time.time() < end:
            j = self.call("/api/job")
            if j and j["status"] != "running":
                return j
            time.sleep(0.05)
        raise AssertionError("job timeout")

    def run_job(self, path: str, body: dict) -> dict:
        self.call(path, body)
        j = self.job()
        self.assertEqual(j["status"], "done", j)
        return j["result"]

    def scan(self, **opts) -> dict:
        return self.run_job("/api/scan", {"path": str(self.roms), "platform": PLAT, **opts})

    def put(self, game: str, rel: str, sidecars=()) -> None:
        p = self.roms / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        self.discs[game].write_chd(p)
        for s in sidecars:
            (p.parent / s).write_bytes(b"sidecar")

    def use_fake_chdman(self) -> None:
        os.environ["ROMORG_CHDMAN"] = str(self.fake)
        self.call("/api/chdman?refresh=1")


class DcEndpointTests(DcServerCase):
    def test_platform_status_and_dat(self) -> None:
        rows = {p["name"]: p for p in self.call("/api/platforms")}
        dc = rows[PLAT]
        self.assertEqual((dc["source"], dc["layout"], dc["chd"], dc["folder_hint"], dc["convertible"]),
                         ("redump", "game_folder", True, "dreamcast", True))
        self.assertTrue(dc["dats"][0]["present"])
        self.assertEqual(dc["dats"][0]["version"], "2026-06-14 18-25-41")
        self.assertEqual(dc["m3u_dats"], [])
        st = self.call("/api/status")
        self.assertEqual(st["redump"]["count"], 1)
        self.assertEqual(st["redump"]["dats"][0]["version"], "2026-06-14 18-25-41")
        self.assertEqual(st["redump"]["dir"], str(self.data / "redump"))
        self.assertIn("redump", st["updates"])
        self.assertFalse(rows["Commodore Amiga"]["chd"])

    def test_chdman_detection_endpoints(self) -> None:
        info = self.call("/api/chdman?refresh=1")
        # nothing configured here (PATH may or may not have a real chdman: only the shape is asserted)
        self.assertIn("found", info)
        os.environ["ROMORG_CHDMAN"] = str(self.fake)
        info = self.call("/api/chdman?refresh=1")
        self.assertTrue(info["found"])
        self.assertEqual((info["kind"], info["label"]), ("configured", str(self.fake)))
        self.call("/api/chdman", {"path": "relative/chdman"}, expect=400)
        self.call("/api/chdman", {"path": str(self.base / "nope")}, expect=400)
        self.call("/api/chdman", {"engine": "fast"}, expect=400)
        self.call("/api/chdman", {}, expect=400)
        out = self.call("/api/chdman", {"path": str(self.fake), "engine": "python"})
        self.assertEqual((out["engine"], out["override"]), ("python", str(self.fake)))
        self.assertEqual(json.loads((self.data / "config.json").read_text())["chd_engine"], "python")
        out = self.call("/api/chdman", {"path": ""})
        self.assertEqual(out["override"], "")

    def test_no_chdman_hint(self) -> None:
        with mock.patch("romorg.chdtool.detect", return_value=None):
            info = self.call("/api/chdman?refresh=1")
        self.assertFalse(info["found"])
        self.assertIn("MAME", info["hint"])
        self.assertTrue(info["steps"])

    def test_scan_results_levels_and_rows(self) -> None:
        self.call("/api/chdman", {"engine": "python"})
        self.put("Beta (Europe)", "Beta/Beta (Europe).chd", ["Beta (Europe).zip", "Beta (Europe).state"])
        self.put("Gamma (Japan)", "g/g.chd")
        other = T.Disc("o", 99, frames=(9, 5, 10))
        (self.roms / "Mystery").mkdir()
        other.write_chd(self.roms / "Mystery" / "m.chd")
        self.discs["Epsilon (USA)"].write_raw(self.roms / "raw", "e")
        s = self.scan()
        self.assertEqual((s["have"], s["identified"], s["verified"], s["raw"], s["unmatched_files"]), (3, 2, 0, 1, 1))
        self.assertEqual(s["dat_total"], len(self.discs))
        games = self.call("/api/scan/results?kind=games&limit=50")
        by = {g["name"]: g for g in games["items"]}
        self.assertEqual((by["Beta (Europe)"]["have"], by["Beta (Europe)"]["level"]), (True, "identified"))
        self.assertEqual(by["Epsilon (USA)"]["level"], "raw")
        self.assertFalse(by["Zeta"]["have"] if "Zeta" in by else False)
        self.assertEqual(by["Alpha (USA) (En,Fr)"]["have"], False)
        matched = self.call("/api/scan/results?kind=matched&limit=50")
        mrow = [m for m in matched["items"] if m["game"] == "Beta (Europe)"][0]
        self.assertEqual((mrow["level"], mrow["kind"], mrow["named_ok"]), ("identified", "chd", False))
        self.assertTrue(mrow["tags"]["regions"] == ["Europe"])
        un = self.call("/api/scan/results?kind=unmatched&limit=50")
        self.assertEqual([u["file"] for u in un["items"]], ["Mystery/m.chd"])
        self.assertIn("track sizes", un["items"][0]["reason"])
        missing = self.call("/api/scan/results?kind=missing&limit=50")
        self.assertIn("Alpha (USA) (En,Fr)", [m["name"] for m in missing["items"]])

    def test_organise_plan_apply_undo(self) -> None:
        self.call("/api/chdman", {"engine": "python"})
        self.put("Beta (Europe)", "x/y.chd", ["y.state"])
        self.scan()
        plan = self.call("/api/organise/plan", {})
        row = [r for r in plan["items"] if r.get("game") == "Beta (Europe)"][0]
        self.assertEqual((row["status"], row["to"], row["level"], row["files"]), ("move", "Beta (Europe)", "identified", 2))
        self.assertEqual(plan["layout"], "game_folder")
        self.call("/api/organise/apply", {})
        j = self.job()
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"]["failed"], [])
        self.assertEqual(j["result"]["summary"]["correctly_placed"], 1)
        self.assertTrue((self.roms / "Beta (Europe)" / "Beta (Europe).state").is_file())
        logs = self.call(f"/api/organise/undo-logs?path={self.roms}")["logs"]
        self.assertEqual(len(logs), 1)
        self.call("/api/organise/undo", {"log": logs[0]["log"]})
        self.assertEqual(self.job()["status"], "done")
        self.assertTrue((self.roms / "x" / "y.chd").is_file())
        self.assertFalse((self.roms / "Beta (Europe)").exists())

    def test_library_profile_plan_apply(self) -> None:
        self.call("/api/chdman", {"engine": "python"})
        self.put("Alpha (USA) (En,Fr)", "a1/a.chd")
        self.put("Alpha (Europe) (En,Fr)", "a2/a.chd")
        self.put("Gamma (Japan)", "g/g.chd")
        self.put("Delta (USA) (Demo)", "d/d.chd")
        self.put("Big Game (USA) (Disc 1)", "b1/b.chd")
        self.put("Big Game (USA) (Disc 2)", "b2/b.chd")
        info = self.call(f"/api/library/profile?platform={urllib.parse.quote(PLAT)}")
        self.assertEqual(info["style"], "redump")
        ids = [c["id"] for c in info["catalog"]]
        self.assertEqual(ids, ["pre_release", "prototype", "demo", "latest_only", "one_per_game", "languages",
                               "region_priority"])
        self.assertTrue(info["available"]["one_per_game"])
        self.assertFalse(info["available"]["keep_flags"])
        self.assertEqual(info["profile"]["languages"], ["En"])
        self.scan()
        plan = self.call("/api/library/plan", {"platform": PLAT})
        self.assertEqual((plan["reasons"]["excluded"], plan["reasons"]["superseded"], plan["reasons"]["kept"]), (2, 1, 3))
        self.assertEqual(plan["playlists"]["write"], 1)
        pl = [r for r in plan["items"] if r.get("item") == "playlist"][0]
        self.assertEqual(pl["disks"], 2)
        self.assertEqual(plan["layout"], "game_folder")
        self.call("/api/library/apply", {})
        j = self.job()
        self.assertEqual((j["status"], j["result"]["failed"], j["result"]["playlists_written"]), ("done", [], 1))
        self.assertTrue((self.roms / "_excluded" / "g" / "g.chd").is_file())
        self.assertTrue((self.roms / "_superseded" / "a1" / "a.chd").is_file())
        # after the rescan the plan is empty
        plan2 = self.call("/api/library/plan", {"platform": PLAT})
        self.assertTrue(plan2["empty"], plan2["counts"])
        log = j["result"]["undo_log"]
        self.call("/api/library/undo", {"log": log})
        self.assertEqual(self.job()["status"], "done")
        self.assertTrue((self.roms / "g" / "g.chd").is_file())
        self.assertFalse(list(self.roms.rglob("*.m3u")))

    def test_verify_fully_job(self) -> None:
        self.call("/api/chdman", {"engine": "python"})
        self.put("Beta (Europe)", "Beta/Beta (Europe).chd")
        s = self.scan()
        self.assertEqual((s["identified"], s["verified"]), (1, 0))
        res = self.run_job("/api/dc/verify", {})
        self.assertEqual((res["verified"], res["failed"], res["action"]), (1, [], "verify"))
        self.assertEqual((res["summary"]["identified"], res["summary"]["verified"]), (0, 1))
        res = self.run_job("/api/dc/verify", {})
        self.assertEqual(res["checked"], 0)

    def test_verify_needs_a_dreamcast_scan(self) -> None:
        self.call("/api/dc/verify", {}, expect=409)       # no scan yet

    def test_scan_with_chdman_is_verified(self) -> None:
        self.use_fake_chdman()
        self.put("Beta (Europe)", "Beta/Beta (Europe).chd")
        s = self.scan()
        self.assertEqual((s["verified"], s["identified"], s["engine"]), (1, 0, "chdman"))
        self.assertEqual(s["chdman"], str(self.fake))

    def test_scan_reports_where_chdman_decoded_and_never_in_the_library(self) -> None:
        from romorg import tempspace
        scratch = self.base / "scratch"
        ram = self.base / "fake-ram"
        ram.mkdir()
        self.use_fake_chdman()
        self.put("Beta (Europe)", "Beta/Beta (Europe).chd")
        before = sorted(str(p.relative_to(self.roms)) for p in self.roms.rglob("*"))
        for mem, where in ((16 * 1024 ** 3, "ram"), (1024 ** 3, "disk")):
            with mock.patch.dict(os.environ, {tempspace.ENV_DIR: str(scratch)}), \
                    mock.patch.object(tempspace, "ram_roots", return_value=[ram]), \
                    mock.patch.object(tempspace, "mem_available", return_value=mem):
                s = self.scan()
            if where == "ram":
                self.assertEqual(s["temp"]["last"]["where"], "ram")
                self.assertIn("in RAM", s["temp_text"])
                (self.data / "cache" / "hashes.sqlite").unlink()      # force a new decode for the second round
            else:
                self.assertEqual(s["temp"]["last"]["where"], "disk")
                self.assertIn(str(scratch), s["temp_text"])
            self.assertEqual(sorted(str(p.relative_to(self.roms)) for p in self.roms.rglob("*")), before)
        self.assertEqual(list(ram.iterdir()), [])
        self.assertEqual(list(scratch.iterdir()), [])

    def test_convert_plan_apply_and_undo(self) -> None:
        self.use_fake_chdman()
        self.call("/api/chdman", {"engine": "python"})
        raw_e = T.prepare_fake_raw(self.base / "fakeraw_e", self.discs["Epsilon (USA)"])
        os.environ["FAKE_RAW"] = str(raw_e)        # what the fake extractcd hands out for the verification
        self.discs["Epsilon (USA)"].write_raw(self.roms / "raw set", "e")
        s = self.scan()
        self.assertEqual((s["raw"], s["convertible"]), (1, 1))
        plan = self.call("/api/convert/plan", {})
        self.assertTrue(plan["available"])
        self.assertTrue(plan["chdman"]["found"])
        item = plan["items"][0]
        self.assertEqual((item["status"], item["to"], item["original_to"]),
                         ("convert", "Epsilon (USA)/Epsilon (USA).chd", "_converted_originals/raw set"))
        self.call("/api/convert/apply", {})
        j = self.job()
        self.assertEqual(j["status"], "done", j)
        self.assertEqual((j["result"]["converted"], j["result"]["failed"]), (1, []))
        self.assertTrue((self.roms / "Epsilon (USA)" / "Epsilon (USA).chd").is_file())
        self.assertTrue((self.roms / "_converted_originals" / "raw set" / "e.gdi").is_file())
        sm = j["result"]["summary"]
        self.assertEqual((sm["raw"], sm["have"], sm["converted_originals"]), (0, 1, 1))
        self.call("/api/organise/undo", {"log": j["result"]["undo_log"]})
        self.assertEqual(self.job()["status"], "done")
        self.assertTrue((self.roms / "raw set" / "e.gdi").is_file())
        self.assertFalse((self.roms / "Epsilon (USA)").exists())

    def test_convert_without_chdman_is_disabled_and_explains(self) -> None:
        self.discs["Epsilon (USA)"].write_raw(self.roms / "raw set", "e")
        with mock.patch("romorg.chdtool.detect", return_value=None):
            self.call("/api/chdman?refresh=1")
            self.scan()
            plan = self.call("/api/convert/plan", {})
            self.assertFalse(plan["chdman"]["found"])
            self.assertIn("MAME", plan["chdman"]["hint"])
            self.assertEqual([i["status"] for i in plan["items"]], ["skip"])
            err = self.call("/api/convert/apply", {}, expect=409)       # never pretends to convert
            self.assertIn("MAME", err["error"])
        self.assertTrue((self.roms / "raw set" / "e.gdi").is_file())

    def test_startup_sweeps_stale_temp_folders(self) -> None:
        self.call("/api/folders", {"platform": PLAT, "path": str(self.roms)})
        dead = self.roms / ".romorg-chd-dead"
        dead.mkdir()
        (dead / "pid").write_text("999999999")
        (dead / "junk").write_bytes(b"x" * 10)
        srv2 = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.addCleanup(srv2.server_close)
        self.assertFalse(dead.exists())


if __name__ == "__main__":
    unittest.main()
