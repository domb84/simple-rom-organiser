"""Library totals (Amendment 17): "with your library rules: have N of M games".

Pure selection tests on synthetic DATs / profiles, the background manager (cache, invalidation, never blocking)
and the ``/api/library/totals`` endpoint with the real modules."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from dataclasses import replace
from pathlib import Path
from unittest import mock

from romorg import library, platforms, server, totals
from romorg.datfile import DatFile, Rom
from romorg.library import Item

GAMES = "Commodore Amiga - Games - [ADF]"
AMIGA = platforms.get_platform("Commodore Amiga")
GBA_DAT = "Nintendo - Game Boy Advance"
GBA = platforms.get_platform("Nintendo Game Boy Advance")
EN = library.default_profile(AMIGA)
EN_DE = replace(EN, languages=("En", "De"))


def rom(name: str, dat: str = GAMES) -> Rom:
    return Rom(name=name + ".adf", size=1, crc="", md5="", sha1="", game=name, dat=dat)


def dat_of(names: list[str], name: str = GAMES) -> DatFile:
    return DatFile(name=name, description=name, version="1", roms=[rom(n, name) for n in names])


def items_of(names: list[str], dat: str = GAMES) -> list[Item]:
    return [Item(key=i, dat=dat, rom=rom(n, dat), style="tosec", path=Path(n), member=None)
            for i, n in enumerate(names)]


def target(names: list[str], profile: library.LibraryProfile = EN) -> totals.Target:
    return totals.compute_target(AMIGA, [dat_of(names)], profile)


def user(names: list[str], profile: library.LibraryProfile = EN) -> totals.UserPart:
    return totals.compute_user_items(items_of(names), AMIGA, profile)


def numbers(dat: list[str], owned: list[str], profile: library.LibraryProfile = EN) -> dict:
    return totals.combine(target(dat, profile), user(owned, profile))


ALPHA, ALPHA2 = "Alpha (1990)(Pub)", "Alpha v1.1 (1991)(Pub)"
BETA = "Beta (1990)(Pub)(beta)"
BETA_FINAL = "Beta (1991)(Pub)"
GAMMA_DE = "Gamma (1990)(Pub)(de)"
CRACK = "Delta (1990)(Pub)[cr FLT]"
EPS1, EPS2 = "Eps (1990)(Pub)(Disk 1 of 2)", "Eps (1990)(Pub)(Disk 2 of 2)"
FOO1, FOO3 = "Foo (1991)(Pub)(Disk 1 of 3)", "Foo (1991)(Pub)(Disk 3 of 3)"
FOO2_DE = "Foo (1991)(Pub)(DE)(Disk 2 of 3)"
DAT = [ALPHA, ALPHA2, BETA, BETA_FINAL, GAMMA_DE, CRACK, EPS1, EPS2, "Solo (1992)(Pub)"]


class TargetTests(unittest.TestCase):
    def test_target_is_the_games_the_rules_keep_for_the_whole_dat(self) -> None:
        t = target(DAT)
        # Alpha (one game, newest kept), Beta (final; the (beta) is a separate, excluded product), Delta, Eps, Solo
        self.assertEqual(t.count, 5)
        self.assertEqual(sum(t.by_dat.values()), 5)
        names = {n for v in t.games.values() for n in v}
        self.assertIn(ALPHA2 + ".adf", names)
        self.assertNotIn(ALPHA + ".adf", names)                  # superseded
        self.assertNotIn(BETA + ".adf", names)                   # excluded (pre-release)
        self.assertNotIn(GAMMA_DE + ".adf", names)               # not English

    def test_languages(self) -> None:
        self.assertEqual(target(DAT, EN).count, 5)
        self.assertEqual(target(DAT, EN_DE).count, 6)
        self.assertEqual(target(DAT, replace(EN, languages=())).count, 6)

    def test_keep_flags_off_drops_cracks_only_games(self) -> None:
        no_cr = replace(EN, keep_flags=EN.keep_flags - {"cr"})
        self.assertEqual(target(DAT, no_cr).count, 4)

    def test_exclusion_rules_off_adds_the_beta_game(self) -> None:
        self.assertEqual(target(DAT, replace(EN, exclude=EN.exclude - {"pre_release"})).count, 6)

    def test_one_game_across_versions_when_latest_only_is_off(self) -> None:
        off = replace(EN, latest_only=False, best_variant=False)
        t = target(DAT, off)
        self.assertEqual(t.count, 5)                             # both Alpha versions are kept, still ONE game
        (alpha,) = [v for k, v in t.games.items() if "alpha" in repr(k).lower()]
        self.assertEqual(len(alpha), 2)

    def test_borrow_completes_a_set_only_when_on(self) -> None:
        dat = [FOO1, FOO3, FOO2_DE]
        self.assertEqual(target(dat, EN).count, 1)               # Foo (disk 2 from the German edition); the donor is no game
        t_off = target(dat, replace(EN, borrow_other_editions=False))
        self.assertEqual(t_off.count, 0)
        self.assertEqual(t_off.incomplete, 1)                    # the DAT itself only has the set in part
        self.assertEqual(totals.combine(target(dat, EN), None)["target_games"], 1)

    def test_whole_dat_counts_games_not_files(self) -> None:
        n = [f"G{i} (1990)(Pub)(Disk {d} of 2)" for i in range(4) for d in (1, 2)]
        t = target(n)
        self.assertEqual((t.count, t.items), (4, 8))


class UserTests(unittest.TestCase):
    def test_have_is_games_with_one_passing_version(self) -> None:
        r = numbers(DAT, [ALPHA, ALPHA2, "Solo (1992)(Pub)"])
        self.assertEqual((r["target_games"], r["have_games"], r["missing_games"]), (5, 2, 3))
        self.assertEqual(r["percent"], 40.0)
        self.assertEqual(r["owned_games"], 2)

    def test_multi_disk_game_incomplete_is_not_counted(self) -> None:
        r = numbers(DAT, [EPS1])
        self.assertEqual((r["have_games"], r["owned_incomplete"]), (0, 1))
        r = numbers(DAT, [EPS1, EPS2])
        self.assertEqual((r["have_games"], r["owned_incomplete"]), (1, 0))

    def test_owned_only_as_beta_is_owned_but_excluded(self) -> None:
        r = numbers(DAT, [BETA, ALPHA2])
        self.assertEqual((r["have_games"], r["owned_but_excluded"]), (1, 1))
        self.assertEqual(r["owned_games"], 2)
        r = numbers(DAT, [GAMMA_DE])                              # only in a language that is not selected
        self.assertEqual((r["have_games"], r["owned_but_excluded"]), (0, 1))
        self.assertEqual(numbers(DAT, [GAMMA_DE], EN_DE)["have_games"], 1)

    def test_upgrade_hint(self) -> None:
        r = numbers(DAT, [ALPHA])                                 # owns v1.0, the rules pick v1.1
        self.assertEqual((r["have_games"], r["not_preferred"]), (1, 1))
        r = numbers(DAT, [ALPHA, ALPHA2])                         # owns the preferred one as well
        self.assertEqual((r["have_games"], r["not_preferred"]), (1, 0))

    def test_keep_flags_change_what_counts(self) -> None:
        no_cr = replace(EN, keep_flags=EN.keep_flags - {"cr"})
        self.assertEqual(numbers(DAT, [CRACK])["have_games"], 1)
        r = numbers(DAT, [CRACK], no_cr)
        self.assertEqual((r["have_games"], r["owned_but_excluded"]), (0, 1))

    def test_borrow_on_and_off(self) -> None:
        dat = [FOO1, FOO3, FOO2_DE]
        r = numbers(dat, dat)
        self.assertEqual((r["target_games"], r["have_games"]), (1, 1))
        off = replace(EN, borrow_other_editions=False)
        r = numbers(dat, dat, off)
        self.assertEqual((r["target_games"], r["have_games"], r["target_incomplete"]), (0, 0, 1))

    def test_a_game_outside_the_target_is_never_counted_as_have(self) -> None:
        t = target(DAT)
        u = user(DAT + ["Unknown (1999)(Nobody)"])               # not in the target DAT (e.g. the DAT changed)
        r = totals.combine(t, u)
        self.assertEqual(r["owned_outside_target"], 1)
        self.assertLessEqual(r["have_games"], r["target_games"])

    def test_unscanned_has_no_user_numbers(self) -> None:
        r = totals.combine(target(DAT), None)
        self.assertEqual(r["target_games"], 5)
        self.assertNotIn("have_games", r)

    def test_consistency_and_idempotence_on_random_libraries(self) -> None:
        rng = random.Random(7)
        pool = DAT + [FOO1, FOO3, FOO2_DE, "Zed (1993)(Pub)", "Zed v1.1 (1994)(Pub)(Demo)"]
        for _ in range(60):
            owned = [n for n in pool if rng.random() < 0.5]
            profile = replace(EN, languages=rng.choice([("En",), ("En", "De"), ()]),
                              borrow_other_editions=rng.random() < 0.5,
                              latest_only=rng.random() < 0.8)
            a = numbers(pool, owned, profile)
            b = numbers(pool, owned, profile)
            self.assertEqual(a, b)                                # idempotent
            self.assertLessEqual(a["have_games"], a["target_games"])
            self.assertEqual(a["have_games"] + a["owned_but_excluded"] + a["owned_incomplete"]
                             + a["owned_outside_target"], a["owned_games"])
            self.assertEqual(a["missing_games"], a["target_games"] - a["have_games"])
            self.assertLessEqual(a["not_preferred"], a["have_games"])
            self.assertEqual(sum(v["target"] for v in a["by_dat"].values()), a["target_games"])
            self.assertEqual(sum(v["have"] for v in a["by_dat"].values()), a["have_games"])
        full = numbers(pool, pool)                                # owning everything = every target game, no upgrades
        self.assertEqual((full["have_games"], full["not_preferred"]), (full["target_games"], 0))

    def test_no_intro_unit_is_the_game(self) -> None:
        def ni(names: list[str]) -> list[Item]:
            return [Item(key=i, dat=GBA_DAT, rom=Rom(name=n + ".gba", size=1, crc="", md5="", sha1="", game=n,
                                                      dat=GBA_DAT, set_name=n), style="nointro", path=Path(n),
                         member=None) for i, n in enumerate(names)]

        names = ["Zap (USA)", "Zap (Europe)", "Zap (Japan)", "Zap (USA) (Beta)", "Other (Europe)"]
        dat = DatFile(GBA_DAT, GBA_DAT, "1", [r.rom for r in ni(names)], format="clrmamepro")
        profile = library.default_profile(GBA)
        t = totals.compute_target(GBA, [dat], profile)
        self.assertEqual(t.count, 2)                              # Zap (one per game), Other; the Beta is excluded
        u = totals.compute_user_items(ni(["Zap (USA)", "Zap (USA) (Beta)"]), GBA, profile)
        r = totals.combine(t, u)
        self.assertEqual((r["have_games"], r["owned_but_excluded"]), (1, 1))
        self.assertEqual(r["not_preferred"], 1)                   # Europe ranks above USA in the default priority


class OtherSystemsTests(unittest.TestCase):
    def test_whdload_unit_is_the_game_and_the_newest_version_is_preferred(self) -> None:
        whd = platforms.get_platform("Commodore Amiga - WHDLoad")
        dat_name = platforms.WHDLOAD_DAT

        def r(stem: str, game: str) -> Rom:
            return Rom(stem + ".lha", 1, "0", "", "", game, dat_name, stem)

        roms = [r("Gee_v1.0", "Gee"), r("Gee_v1.1", "Gee"), r("Haa_v1.0", "Haa")]
        dat = DatFile(dat_name, dat_name, "1", roms, format="clrmamepro")
        profile = library.default_profile(whd)
        t = totals.compute_target(whd, [dat], profile)
        self.assertEqual(t.count, 2)
        items = [Item(key=0, dat=dat_name, rom=roms[0], style="whdload", path=Path("x"), member=None)]
        res = totals.combine(t, totals.compute_user_items(items, whd, profile))
        self.assertEqual((res["have_games"], res["not_preferred"], res["missing_games"]), (1, 1, 1))

    def test_redump_unit_is_the_game_not_the_disc(self) -> None:
        dc = platforms.get_platform("Sega Dreamcast")
        name = platforms.REDUMP_DC_DAT
        roms = []
        for game in ("Big (USA) (Disc 1)", "Big (USA) (Disc 2)", "Solo (USA)"):
            roms.append(Rom(game + " (Track 1).bin", 100, "0", "", "", game, name, game, "Games"))
        dat = DatFile(name, name, "1", roms)
        profile = library.default_profile(dc)
        t = totals.compute_target(dc, [dat], profile)
        self.assertEqual((t.count, t.items), (2, 3))              # 3 discs, 2 games


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dat = dat_of(DAT)
        self.calls = 0
        self.gate: threading.Event | None = None
        self.started = threading.Event()
        real = totals.compute_target

        def slow(platform, dats, profile):
            self.calls += 1
            self.started.set()
            if self.gate is not None:
                self.gate.wait(10)
            return real(platform, dats, profile)

        p = mock.patch.object(totals, "compute_target", slow)
        p.start()
        self.addCleanup(p.stop)
        self.saved: dict[str, dict] = {}
        self.mgr = totals.TotalsManager(lambda platform: [self.dat], lambda n, r: self.saved.__setitem__(n, r))

    def state(self, names: list[str], serial: int = 1):
        class S:
            pass

        s = S()
        s.serial = serial
        s.result = None
        s.items = items_of(names)
        return s

    def test_request_never_blocks_and_the_result_arrives(self) -> None:
        self.gate = threading.Event()
        t0 = time.time()
        first = self.mgr.request(AMIGA, EN, "d1")
        self.assertLess(time.time() - t0, 1.0)
        self.assertTrue(first["calculating"])
        self.assertIsNone(first["target_games"])
        self.assertTrue(self.started.wait(5))
        again = self.mgr.request(AMIGA, EN, "d1")                  # polling while it runs: still one run
        self.assertTrue(again["calculating"])
        self.gate.set()
        self.assertTrue(self.mgr.wait_idle(10))
        done = self.mgr.request(AMIGA, EN, "d1")
        self.assertEqual((done["calculating"], done["target_games"], done["stale"]), (False, 5, False))
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.mgr.target_runs, 1)

    def test_cached_by_profile_and_dats(self) -> None:
        self.mgr.request(AMIGA, EN, "d1")
        self.assertTrue(self.mgr.wait_idle(10))
        for _ in range(3):
            self.mgr.request(AMIGA, EN, "d1")
        self.assertTrue(self.mgr.wait_idle(10))
        self.assertEqual(self.calls, 1)
        self.mgr.request(AMIGA, EN_DE, "d1")                       # a rule changed
        self.assertTrue(self.mgr.wait_idle(10))
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.mgr.request(AMIGA, EN_DE, "d1")["target_games"], 6)
        self.mgr.request(AMIGA, EN_DE, "d2")                       # a DAT update
        self.assertTrue(self.mgr.wait_idle(10))
        self.assertEqual(self.calls, 3)
        self.assertNotEqual(totals.profile_signature(EN), totals.profile_signature(EN_DE))

    def test_old_numbers_stay_visible_flagged_stale_while_new_ones_compute(self) -> None:
        self.mgr.request(AMIGA, EN, "d1")
        self.assertTrue(self.mgr.wait_idle(10))
        self.gate = threading.Event()
        self.started.clear()
        answer = self.mgr.request(AMIGA, EN_DE, "d1")
        self.assertTrue(answer["calculating"] and answer["stale"])
        self.assertEqual(answer["target_games"], 5)                # the previous rules' number
        self.gate.set()
        self.assertTrue(self.mgr.wait_idle(10))
        self.assertEqual(self.mgr.request(AMIGA, EN_DE, "d1")["target_games"], 6)

    def test_superseded_result_is_dropped_not_stored(self) -> None:
        self.gate = threading.Event()
        self.mgr.request(AMIGA, EN, "d1")
        self.assertTrue(self.started.wait(5))
        self.mgr.request(AMIGA, EN_DE, "d1")                       # the rules change while the first one runs
        self.gate.set()
        self.assertTrue(self.mgr.wait_idle(10))
        self.assertEqual(self.mgr.discarded, 1)
        self.assertEqual(self.calls, 2)                            # never two runs for the same key
        self.assertEqual(self.mgr.request(AMIGA, EN_DE, "d1")["target_games"], 6)

    def test_counts_are_persisted_and_answer_instantly_after_a_restart(self) -> None:
        self.mgr.request(AMIGA, EN, "d1")
        self.assertTrue(self.mgr.wait_idle(10))
        record = self.saved[AMIGA.name]
        self.assertEqual(record["target_games"], 5)
        self.assertNotIn("games", record)                          # counts only, never a list
        calls = self.calls
        fresh = totals.TotalsManager(lambda platform: [self.dat])
        answer = fresh.request(AMIGA, EN, "d1", None, record)
        self.assertEqual((answer["target_games"], answer["calculating"], answer["have_games"]), (5, False, None))
        self.assertEqual(self.calls, calls)                        # nothing was computed
        other = fresh.request(AMIGA, EN_DE, "d1", None, record)    # other rules: the record does not apply
        self.assertTrue(other["calculating"])
        self.assertTrue(fresh.wait_idle(10))

    def test_user_part_waits_for_a_scan_and_follows_its_serial(self) -> None:
        real_user = totals.compute_user
        with mock.patch.object(totals, "compute_user",
                               lambda result, platform, profile, *rest: totals.compute_user_items(self.cur.items, platform, profile)):
            self.cur = self.state([ALPHA, "Solo (1992)(Pub)"], 1)
            self.mgr.request(AMIGA, EN, "d1")
            self.assertTrue(self.mgr.wait_idle(10))
            self.assertFalse(self.mgr.request(AMIGA, EN, "d1")["scanned"])
            a = self.mgr.request(AMIGA, EN, "d1", self.cur)
            self.assertTrue(self.mgr.wait_idle(10))
            a = self.mgr.request(AMIGA, EN, "d1", self.cur)
            self.assertEqual((a["scanned"], a["have_games"], a["not_preferred"], a["calculating"]), (True, 2, 1, False))
            self.assertEqual(self.calls, 1)                        # the target was reused
            self.cur = self.state([ALPHA, ALPHA2, "Solo (1992)(Pub)"], 2)    # a re-scan
            b = self.mgr.request(AMIGA, EN, "d1", self.cur)
            self.assertTrue(b["calculating"] and b["stale"])
            self.assertTrue(self.mgr.wait_idle(10))
            b = self.mgr.request(AMIGA, EN, "d1", self.cur)
            self.assertEqual((b["have_games"], b["not_preferred"]), (2, 0))
        self.assertIs(totals.compute_user, real_user)

    def test_a_failing_computation_is_reported_not_retried(self) -> None:
        def boom(platform):
            raise RuntimeError("no dat")

        mgr = totals.TotalsManager(boom)
        with mock.patch("traceback.print_exc"):
            mgr.request(AMIGA, EN, "d1")
            self.assertTrue(mgr.wait_idle(10))
        answer = mgr.request(AMIGA, EN, "d1")
        self.assertEqual((answer["calculating"], answer["error"]), (False, "no dat"))


# ------------------------------------------------------------------------- the endpoint (real modules)

def _dat_xml(name: str, games: list[tuple[str, bytes]], version: str) -> str:
    lines = ['<?xml version="1.0"?>', "<datafile>",
             f"<header><name>{name}</name><description>{name}</description><version>{version}</version></header>"]
    for game, data in games:
        lines.append(f'<game name="{game}"><description>{game}</description>'
                     f'<rom name="{game}.adf" size="{len(data)}" crc="{zlib.crc32(data):08x}" '
                     f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game>')
    lines.append("</datafile>")
    return "\n".join(lines)


class TotalsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-totals-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.tmp / "amiga"
        self.root.mkdir()
        dats = self.tmp / "data" / "dats"
        dats.mkdir(parents=True)
        rng = random.Random(5)
        self.blobs = {n: bytes(rng.getrandbits(8) for _ in range(256)) for n in DAT}
        (dats / f"{GAMES} (TOSEC-v2025-01-30_CM).dat").write_text(
            _dat_xml(GAMES, list(self.blobs.items()), "2025-01-30"))
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.port = self.srv.port
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, method: str, path: str, body=None):
        headers = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers.update({"Content-Type": "application/json", "X-Romorg-Token": "t"})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return {"_status": err.code, **json.loads(err.read() or b"{}")}

    def totals(self, platform: str = "Commodore Amiga", wait: bool = True) -> dict:
        path = f"/api/library/totals?platform={urllib.request.quote(platform)}"
        end = time.time() + 20
        while True:
            out = self.call("GET", path)
            if not wait or not out.get("calculating") or time.time() > end:
                return out
            time.sleep(0.05)

    def scan(self) -> None:
        self.call("POST", "/api/scan", {"path": str(self.root), "platform": "Commodore Amiga"})
        end = time.time() + 20
        while time.time() < end:
            job = self.call("GET", "/api/job")
            if job["status"] != "running":
                self.assertEqual(job["status"], "done", job)
                return
            time.sleep(0.03)
        self.fail("scan did not finish")

    def put(self, *names: str) -> None:
        for n in names:
            (self.root / f"{n}.adf").write_bytes(self.blobs[n])

    def test_target_before_a_scan_then_have_after_it(self) -> None:
        first = self.call("GET", "/api/library/totals?platform=Commodore%20Amiga")
        self.assertTrue(first["available"])
        out = self.totals()
        self.assertEqual((out["calculating"], out["scanned"], out["target_games"]), (False, False, 5))
        self.assertIsNone(out["have_games"])
        self.assertEqual(set(out), set(out) | {"target_games", "have_games", "missing_games", "percent", "not_preferred",
                                               "owned_but_excluded", "calculating", "stale", "computed_at",
                                               "profile_signature"})
        sig = out["profile_signature"]
        self.put(ALPHA, BETA, EPS1, "Solo (1992)(Pub)")
        self.scan()
        out = self.totals()
        self.assertEqual((out["scanned"], out["target_games"], out["have_games"], out["missing_games"]), (True, 5, 2, 3))
        self.assertEqual((out["not_preferred"], out["owned_but_excluded"], out["owned_incomplete"]), (1, 1, 1))
        self.assertEqual(out["percent"], 40.0)
        self.assertEqual(out["profile_signature"], sig)
        # a rule change is picked up (new signature, new numbers) without restarting anything
        self.call("POST", "/api/library/profile", {"platform": "Commodore Amiga", "exclude": [
            r for r in server.App._rule_keys() if r != "pre_release"]})
        out2 = self.totals()
        self.assertNotEqual(out2["profile_signature"], sig)
        self.assertEqual((out2["target_games"], out2["have_games"], out2["owned_but_excluded"]), (6, 3, 0))
        self.call("POST", "/api/library/profile", {"platform": "Commodore Amiga", "reset": True})
        self.assertEqual(self.totals()["have_games"], 2)

    def test_counts_survive_a_restart_in_config_json(self) -> None:
        self.totals()
        saved = json.loads((self.tmp / "data" / "config.json").read_text())["library_totals"]["Commodore Amiga"]
        self.assertEqual(saved["target_games"], 5)
        self.assertEqual(set(saved), {"target_games", "target_incomplete", "by_dat", "profile_signature",
                                      "dats_signature", "computed_at"})

    def test_platform_without_installed_dats_and_unknown_platform(self) -> None:
        out = self.call("GET", "/api/library/totals?platform=Nintendo%2064")
        self.assertFalse(out["available"])
        self.assertIsNone(out["target_games"])
        self.assertEqual(self.call("GET", "/api/library/totals?platform=Nope")["_status"], 400)

    def test_apply_refuses_a_plan_of_other_rules(self) -> None:
        self.put(ALPHA, ALPHA2)
        self.scan()
        plan = self.call("POST", "/api/library/plan", {"limit": 5})
        self.assertTrue(plan["plan_id"])
        self.assertGreaterEqual(plan["files"], 2)
        again = self.call("POST", "/api/library/plan", {"limit": 5, "refresh": True})
        self.assertEqual(again["plan_id"], plan["plan_id"])        # same scan, same rules: same identity
        self.call("POST", "/api/library/profile", {"platform": "Commodore Amiga", "languages": ["De"]})
        refused = self.call("POST", "/api/library/apply", {"plan_id": plan["plan_id"]})
        self.assertEqual(refused["_status"], 409)
        self.assertIn("recalculate", refused["error"])
        self.assertFalse((self.root / GAMES).exists())             # nothing moved
        fresh = self.call("POST", "/api/library/plan", {"limit": 5})
        self.assertNotEqual(fresh["plan_id"], plan["plan_id"])
        self.assertEqual(fresh["profile"]["languages"], ["De"])    # the plan always follows the saved rules

    def test_status_scan_has_an_id_that_changes_with_every_scan(self) -> None:
        self.put(ALPHA)
        self.scan()
        a = self.call("GET", "/api/status")["scan"]["id"]
        self.scan()
        self.assertNotEqual(a, self.call("GET", "/api/status")["scan"]["id"])


class UiContractTests(unittest.TestCase):
    """Markup / script contract of the Recalculate affordance and the Overview totals (behaviour: headless browser)."""

    def setUp(self) -> None:
        self.html = server.read_static("index.html").decode()
        self.js = server.read_static("app.js").decode()
        self.css = server.read_static("style.css").decode()

    def test_overview_has_both_totals_blocks(self) -> None:
        for needle in ('id="totals-pair"', 'id="totals-all-body"', 'id="totals-lib-body"', "All DAT entries",
                       "With your library rules", 'aria-live="polite"'):
            self.assertIn(needle, self.html)
        for needle in ("/api/library/totals", "function renderTotals", "function loadTotals", "scheduleTotals",
                       "not the preferred version (an upgrade is available)", "excluded by your rules",
                       "Scan to see how many you have", "calculating...", "recalculating..."):
            self.assertIn(needle, self.js)
        self.assertIn(".totals-pair", self.css)
        self.assertRegex(self.css, r"@media \(max-width: 760px\) \{ \.totals-pair")

    def test_previews_recalculate_and_block_apply_while_stale(self) -> None:
        for needle in ("const Previews", "Recalculate preview", "Recalculates with your current rules and folder contents",
                       "Rules changed since this preview", "Out of date - calculated", "Could not calculate", '"Try again"',
                       "Calculating...", "clockText(it.at)", "staleAll", "Previews.onScan()", "plan_id: plan.plan_id",
                       'b.dataset.stale === "1"', 'b.dataset.busy === "1"', 'role: "status"', '"aria-live": "polite"'):
            self.assertIn(needle, self.js, needle)
        for pair in ("lib",):
            self.assertIn(f"{pair}: {{ btn:", self.js)
        for needle in (".pv-banner", ".pv-stale", ".pv-attn", ".pv-spin", ".pv-meta"):
            self.assertIn(needle, self.css)


if __name__ == "__main__":
    unittest.main()
