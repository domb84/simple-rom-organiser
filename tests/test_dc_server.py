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
        T.disable_native_flac(self)
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
        self.assertEqual(self.call("/api/chdman", {"engine": "chdman"})["engine"], "chdman")
        self.assertEqual(self.call("/api/chdman", {"engine": "auto"})["engine"], "auto")
        self.assertIn("native", out["flac"])                         # how audio is decoded: native libFLAC or not
        self.assertGreaterEqual(out["workers"], 1)                   # decode processes
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

    def test_stale_move_unmatched_false_is_ignored_unmatched_always_moves(self) -> None:
        self.call("/api/chdman", {"engine": "python"})
        self.put("Beta (Europe)", "x/y.chd", ["y.state"])
        (self.roms / "stray.txt").write_text("junk")
        self.scan()
        for path in ("/api/organise/plan", "/api/library/plan"):
            plan = self.call(path, {"platform": PLAT, "move_unmatched": False})   # an old client: no 400, ignored
            names = [r.get("name") or r.get("from") or "" for r in plan["items"]]
            self.assertTrue(any("stray.txt" in str(r) and "_unmatched" in str(r) for r in plan["items"]), (path, names))
        self.call("/api/library/apply", {"move_unmatched": False})
        self.assertEqual(self.job()["status"], "done")
        self.assertTrue((self.roms / "_unmatched" / "stray.txt").is_file())
        self.assertFalse((self.roms / "stray.txt").exists())

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
        ids = [c["id"] for c in info["catalog"] if c.get("group") != "ratings"]
        self.assertEqual(ids, ["pre_release", "prototype", "demo", "latest_only", "one_per_game", "languages",
                               "other_language", "region_priority"])
        self.assertTrue(info["available"]["one_per_game"])
        self.assertFalse(info["available"]["keep_flags"])
        self.assertEqual(info["profile"]["languages"], ["En"])
        self.assertTrue(info["profile"]["keep_other_language"])
        self.call("/api/library/profile", {"platform": PLAT, "keep_other_language": False})   # this test is about the exclusion
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

    def test_checksums_per_track_identified_verified_raw_missing_unmatched(self) -> None:
        import hashlib
        import zlib
        self.put("Beta (Europe)", "Beta/Beta (Europe).chd")
        self.discs["Gamma (Japan)"].write_raw(self.roms / "raw" / "gamma", "Gamma (Japan)")
        (self.roms / "junk.chd").write_bytes(b"not a chd")
        self.scan()

        def rows(kind: str) -> list:
            return self.call(f"/api/scan/results?kind={kind}&limit=100&checksums=1")["items"]

        disc = self.discs["Beta (Europe)"]
        beta = next(i for i in rows("matched") if i["game"] == "Beta (Europe)")["checksums"]
        self.assertEqual((beta["kind"], beta["level"], beta["source"]), ("disc", "identified", "redump"))
        t1, t2, t3 = beta["tracks"]
        self.assertEqual((t1["state"], t2["state"], t3["state"]), ("hashed", "length", "hashed"))
        self.assertEqual(t1["dat"]["sha1"], hashlib.sha1(disc.t1).hexdigest())
        self.assertEqual(t1["local"], {"crc32": f"{zlib.crc32(disc.t1) & 0xffffffff:08x}",
                                      "md5": hashlib.md5(disc.t1).hexdigest(), "sha1": hashlib.sha1(disc.t1).hexdigest()})
        self.assertEqual(t1["equal"], {"crc32": True, "md5": True, "sha1": True})
        self.assertEqual(t2["local"], {"crc32": None, "md5": None, "sha1": None})   # audio: length only, never invented
        self.assertEqual(t2["equal"], {"crc32": None, "md5": None, "sha1": None})
        self.assertEqual(t2["dat"]["sha1"], hashlib.sha1(disc.t2).hexdigest())      # but the DAT side is known
        self.assertEqual((beta["tracks"][1]["type"], t1["number"]), ("AUDIO", 1))
        # verified after Verify fully: the audio track gets its hashes
        self.run_job("/api/dc/verify", {})
        beta = next(i for i in rows("matched") if i["game"] == "Beta (Europe)")["checksums"]
        self.assertEqual((beta["level"], beta["tracks"][1]["state"]), ("verified", "hashed"))
        self.assertEqual(beta["tracks"][1]["equal"]["sha1"], True)
        # raw set: per-track file hashes (CRC32 + SHA-1)
        raw = next(i for i in rows("matched") if i["game"] == "Gamma (Japan)")["checksums"]
        self.assertEqual(raw["level"], "raw")
        self.assertTrue(all(t["state"] == "hashed" and t["equal"]["sha1"] for t in raw["tracks"]))
        # missing / games: DAT side only
        miss = next(i for i in rows("missing") if i["name"] == "Alpha (USA) (En,Fr)")["checksums"]
        self.assertEqual([t["state"] for t in miss["tracks"]], ["none"] * 3)
        self.assertIsNone(miss["tracks"][0]["local"])
        self.assertEqual(miss["tracks"][0]["dat"]["md5"], hashlib.md5(self.discs["Alpha (USA) (En,Fr)"].t1).hexdigest())
        game = next(i for i in rows("games") if i["name"] == "Beta (Europe)")["checksums"]
        self.assertEqual(game["kind"], "disc")
        self.assertEqual(game["tracks"][0]["equal"]["sha1"], True)
        # unmatched: no hashes are invented for something that is not even a CHD
        junk = next(i for i in rows("unmatched") if i["file"].endswith("junk.chd"))["checksums"]
        self.assertIn(junk["kind"], ("unmatched", "disc_unmatched"))
        self.assertEqual(junk.get("tracks", []), [])

    def test_scan_with_chdman_is_verified(self) -> None:
        self.use_fake_chdman()
        self.call("/api/chdman", {"engine": "chdman"})
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
        self.call("/api/chdman", {"engine": "chdman"})
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

    def test_convert_without_chdman_uses_the_builtin_writer(self) -> None:
        self.discs["Epsilon (USA)"].write_raw(self.roms / "raw set", "e")
        with mock.patch("romorg.chdtool.detect", return_value=None):
            info = self.call("/api/chdman?refresh=1")
            self.assertEqual((info["found"], info["writer"], info["preset"]), (False, "auto", "default"))
            self.scan()
            plan = self.call("/api/convert/plan", {})
            self.assertFalse(plan["chdman"]["found"])
            self.assertEqual(plan["chdman"]["writer"], "auto")
            self.assertEqual([i["status"] for i in plan["items"]], ["convert"])
            res = self.run_job("/api/convert/apply", {})
            self.assertEqual((res["converted"], res["failed"], res["written_by"]), (1, [], {"builtin": 1}))
        self.assertTrue((self.roms / "Epsilon (USA)" / "Epsilon (USA).chd").is_file())
        self.assertFalse((self.roms / "raw set" / "e.gdi").exists())           # kept in _converted_originals/

    def test_writer_and_preset_settings(self) -> None:
        self.assertEqual(self.call("/api/chdman", {"writer": "chdman"})["writer"], "chdman")
        self.assertEqual(self.call("/api/chdman", {"writer": "auto"})["writer"], "auto")
        self.call("/api/chdman", {"writer": "sometimes"}, expect=400)
        self.call("/api/chdman", {"preset": "tiny"}, expect=400)
        with mock.patch("romorg.chdwrite.zstd_available", return_value=False):
            self.call("/api/chdman", {"preset": "zstd"}, expect=409)
            self.assertFalse(self.call("/api/chdman")["zstd_writer"])
        with mock.patch("romorg.chdwrite.zstd_available", return_value=True):
            self.assertEqual(self.call("/api/chdman", {"preset": "zstd"})["preset"], "zstd")
        with mock.patch("romorg.chdwrite.zstd_available", return_value=False):
            self.assertEqual(self.call("/api/chdman")["preset"], "default")     # saved, but not usable here
        self.assertEqual(self.call("/api/chdman", {"preset": "default"})["preset"], "default")

    def test_flac_encoder_and_platform_facts(self) -> None:
        # without libFLAC the writer stores audio tracks with LZMA; the Convert step says so
        with mock.patch("romorg.flacenc.available", return_value=False):
            self.assertIs(self.call("/api/chdman")["flac_encoder"], False)
        with mock.patch("romorg.flacenc.available", return_value=True):
            self.assertIs(self.call("/api/chdman")["flac_encoder"], True)
        want = "windows" if sys.platform.startswith("win") else ("linux" if sys.platform.startswith("linux")
                                                                  else sys.platform)
        self.assertEqual(self.call("/api/chdman")["os"], want)          # which install steps / path examples to show
        self.assertEqual(self.call("/api/status")["os"], want)

    def test_libsndfile_decoding_is_named(self) -> None:
        sndfile = {"native": True, "library": "libsndfile", "note": "libFLAC not found"}
        with mock.patch("romorg.flacnative.status", return_value=sndfile):
            self.assertEqual(self.call("/api/chdman")["flac"]["library"], "libsndfile")
        js = server.read_static("app.js").decode()
        self.assertIn('flac.library === "libsndfile"', js)              # the UI says libsndfile, not libFLAC
        self.assertIn("chd-flac-note", js)
        self.assertIn('id="chd-flac-note"', server.read_static("index.html").decode())

    def test_startup_sweeps_stale_temp_folders(self) -> None:
        self.call("/api/folders", {"platform": PLAT, "path": str(self.roms)})
        dead = self.roms / ".romorg-chd-dead"
        dead.mkdir()
        (dead / "pid").write_text("999999999")
        (dead / "junk").write_bytes(b"x" * 10)
        srv2 = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.addCleanup(srv2.server_close)
        self.assertFalse(dead.exists())


class LongPathConvertTest(DcServerCase):
    """Convert through the HTTP API in a library whose paths run past Windows' old 260-character MAX_PATH (works
    with the LongPathsEnabled setting; skipped where such a folder cannot be made): the built-in writer's
    ``.chd.romorg.part`` file, the generated GDI of a cue-only Redump set, the placed CHD and the originals moved to
    ``_converted_originals`` all live there. The folder names have apostrophes and double spaces on purpose."""

    def setUp(self) -> None:
        super().setUp()
        deep = self.base / "roms"
        n = 0
        while len(str(deep)) < 250:
            n += 1
            deep = deep / f"Tony's  long folder name number {n}"
        try:
            deep.mkdir(parents=True)
            probe = deep / ("p" * 40) / "probe.bin"
            probe.parent.mkdir()
            probe.write_bytes(b"x")
        except OSError as exc:
            self.skipTest(f"this system cannot make paths over 260 characters ({exc})")
        self.roms = deep

    def test_convert_sets_deeper_than_260_characters(self) -> None:
        self.discs["Epsilon (USA)"].write_raw(self.roms / "gdi set", "e")
        self.discs["Gamma (Japan)"].write_raw(self.roms / "cue only set", "g", gdi_style=False, markers=True)
        with mock.patch("romorg.chdtool.detect", return_value=None):
            self.call("/api/chdman?refresh=1")
            s = self.scan()
            self.assertEqual((s["raw"], s["convertible"]), (2, 2))
            plan = self.call("/api/convert/plan", {})
            self.assertEqual(sorted(i["status"] for i in plan["items"]), ["convert", "convert"])
            res = self.run_job("/api/convert/apply", {})
        self.assertEqual((res["converted"], res["failed"], res["written_by"], res["generated_gdi"]),
                         (2, [], {"builtin": 2}, 1))
        for game in ("Epsilon (USA)", "Gamma (Japan)"):
            placed = self.roms / game / f"{game}.chd"
            self.assertGreater(len(str(placed)), 260)
            self.assertTrue(placed.is_file(), placed)
        self.assertTrue((self.roms / "_converted_originals" / "gdi set" / "e.gdi").is_file())
        self.assertTrue((self.roms / "_converted_originals" / "cue only set" / "g.cue").is_file())
        self.assertFalse([p for p in self.roms.rglob("*") if p.name.endswith(".romorg.part")])

    def test_chdman_is_not_given_paths_it_cannot_open(self) -> None:
        """chdman 0.289 on Windows fails on paths of 260 characters or more (and spins forever on a track file
        that long): with chdman chosen as the writer, such a set is written by the built-in writer instead."""
        self.discs["Epsilon (USA)"].write_raw(self.roms / "gdi set", "e")
        self.use_fake_chdman()
        self.call("/api/chdman", {"writer": "chdman", "engine": "python"})
        self.scan()
        with mock.patch("romorg.discsys._CHDMAN_PATH_LIMIT", True), \
                mock.patch("romorg.chdtool.create_cd", side_effect=AssertionError("chdman was run")):
            res = self.run_job("/api/convert/apply", {})
        self.assertEqual((res["converted"], res["failed"], res["written_by"]), (1, [], {"builtin": 1}))


ODD_NAME = "Tony's  Café Ünï 日本 (USA)"      # apostrophe, double space, accents, Japanese


@unittest.skipUnless(os.environ.get("ROMORG_CHDMAN_ORACLE"), "set ROMORG_CHDMAN_ORACLE to a chdman executable")
class OddNamesConvertTest(DcServerCase):
    """Names with an apostrophe, a double space and non-ASCII letters, as track files, sheet and game: the built-in
    writer and the real chdman (chosen as the writer; for a cue-only set it gets the generated GDI) must produce the
    same CHD (equal header SHA-1) from a ``.gdi`` set and from a ``.cue`` set, with the same files moved aside.

    chdman 0.289 on Windows cannot open an input file whose path has a non-ASCII character (measured), so there a
    ``.gdi`` set - and a ``.cue`` set whose scratch folder is not ASCII - goes to the built-in writer instead."""

    def raw_set(self, folder: Path, gdi_style: bool) -> None:
        disc = self.discs[ODD_NAME]
        folder.mkdir(parents=True)
        names = [f"{ODD_NAME} (Track {i}).{'raw' if i == 2 else 'bin'}" for i in (1, 2, 3)]
        for n, data in zip(names, disc.bins):
            (folder / n).write_bytes(data)
        if gdi_style:
            lba, rows = 0, ["3"]
            for i, (n, data) in enumerate(zip(names, disc.bins), 1):
                rows.append(f'{i} {lba} {0 if i == 2 else 4} 2352 "{n}" 0')
                lba += len(data) // 2352
            (folder / f"{ODD_NAME}.gdi").write_text("\n".join(rows) + "\n", encoding="utf-8")
        else:
            rows = []
            for i, n in enumerate(names, 1):
                if i in (1, 3):                    # the Redump markers of a GD-ROM cue
                    rows.append("REM SINGLE-DENSITY AREA" if i == 1 else "REM HIGH-DENSITY AREA")
                rows += [f'FILE "{n}" BINARY', f"  TRACK {i:02d} {'AUDIO' if i == 2 else 'MODE1/2352'}",
                         "    INDEX 01 00:00:00"]
            (folder / f"{ODD_NAME}.cue").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_both_writers_agree(self) -> None:
        from romorg import chd
        self.discs[ODD_NAME] = T.Disc("odd", 11)
        T.write_dat(paths.redump_dir() / f"{DAT}.dat", [(n, "Games", d.bins) for n, d in self.discs.items()])
        os.environ["ROMORG_CHDMAN"] = os.path.abspath(os.environ["ROMORG_CHDMAN_ORACLE"])
        got = {}
        for writer in ("auto", "chdman"):
            for gdi_style in (True, False):
                with self.subTest(writer=writer, gdi=gdi_style):
                    lib = self.base / f"roms-{writer}-{int(gdi_style)}"
                    self.raw_set(lib / "my  set", gdi_style)
                    self.call("/api/chdman", {"writer": writer, "engine": "python"})
                    self.call("/api/chdman?refresh=1")
                    s = self.run_job("/api/scan", {"path": str(lib), "platform": PLAT})
                    self.assertEqual((s["raw"], s["convertible"]), (1, 1), s)
                    res = self.run_job("/api/convert/apply", {})
                    via = "chdman" if writer == "chdman" else "builtin"
                    if os.name == "nt" and (gdi_style or not tempfile.gettempdir().isascii()):
                        via = "builtin"
                    self.assertEqual((res["converted"], res["failed"], res["written_by"]), (1, [], {via: 1}), res)
                    placed = lib / ODD_NAME / f"{ODD_NAME}.chd"
                    self.assertTrue(placed.is_file(), [str(p) for p in lib.rglob("*")])
                    with chd.Chd(placed, load_map=False) as c:
                        got[writer, gdi_style] = (c.sha1, c.raw_sha1)
                    aside = lib / "_converted_originals" / "my  set"
                    self.assertEqual(len(list(aside.iterdir())), 4)
                    self.assertFalse([p for p in lib.rglob("*") if p.name.endswith(".part")])
        self.assertEqual(len(got), 4, got)
        self.assertEqual(got["auto", True], got["chdman", True])
        self.assertEqual(got["auto", False], got["chdman", False])


if __name__ == "__main__":
    unittest.main()
