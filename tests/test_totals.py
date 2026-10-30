"""Library totals (Amendment 17): "with your library rules: have N of M games".

Pure selection tests on synthetic DATs / profiles, the background manager (cache, invalidation, never blocking)
and the ``/api/library/totals`` endpoint with the real modules."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import random
import shutil
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
import zlib
from dataclasses import replace
from pathlib import Path
from unittest import mock
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

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

    def test_bios_and_firmware_are_no_games_on_any_system(self) -> None:
        nes = [Rom(name=n + ".gba", size=1, crc="", md5="", sha1="", game=n, dat=GBA_DAT, set_name=n)
               for n in ("Zap (USA)", "[BIOS] Game Boy Advance (World)")]
        t = totals.compute_target(GBA, [DatFile(GBA_DAT, GBA_DAT, "1", nes, format="clrmamepro")], library.default_profile(GBA))
        self.assertEqual(t.count, 1)                                                     # (a rom marked [BIOS] is no game)
        fw = "Commodore Amiga - Firmware"
        t = totals.compute_target(AMIGA, [dat_of(["Alpha (1990)(Pub)"]), dat_of(["Kickstart v1.3 (1987)(Commodore)"], fw)], EN)
        self.assertEqual(t.count, 1)                                                     # (nor is a game of a "- Firmware" DAT)
        u = totals.compute_user_items([*items_of(["Alpha (1990)(Pub)"]), *items_of(["Kickstart v1.3 (1987)(Commodore)"], fw)], AMIGA, EN)
        self.assertEqual(totals.combine(t, u)["have_games"], 1)

    def test_system_disks_are_counted_apart_from_the_games(self) -> None:
        wb, ks = "Commodore Amiga - Operating Systems - Workbench", "Commodore Amiga - Kickstart-Disks"
        self.assertEqual(totals.system_dats(AMIGA), frozenset({wb, ks, "Commodore Amiga - Firmware"}))
        self.assertEqual(totals.system_dats(GBA), frozenset())
        t = totals.compute_target(AMIGA, [dat_of(["Alpha (1990)(Pub)", "Beta (1991)(Pub)"]), dat_of(["Workbench v1.3 (1988)(Commodore)"], wb),
                                          dat_of(["EmuTOS v0.9.0 (2013-02-28)(EmuTOS Development Team)(A1000)(PD)"], ks)], EN)
        u = totals.compute_user_items([*items_of(["Alpha (1990)(Pub)"]), *items_of(["Workbench v1.3 (1988)(Commodore)"], wb)], AMIGA, EN)
        out = totals.combine(t, u, totals.system_dats(AMIGA))
        self.assertEqual((out["target_games"], out["have_games"], out["missing_games"], out["percent"]), (2, 1, 1, 50.0))
        self.assertEqual((out["target_system"], out["have_system"]), (2, 1))
        self.assertEqual(out["missing_names"], ["Beta (1991)(Pub)"])                     # (EmuTOS is no game you lack ...)
        self.assertNotIn("missing_system_names", out)                                  # (... nor is it listed as missing: nobody counts the system disks you lack)

    def test_updates_and_dlc_are_no_games_and_never_missing(self) -> None:
        sw = platforms.get_platform("Nintendo Switch")
        self.assertEqual(totals.addon_dats(sw), frozenset({"Nintendo - Switch (Updates)", "Nintendo - Switch (DLC)"}))
        self.assertEqual(totals.addon_dats(AMIGA), frozenset())
        game, dlc = ("Nintendo - Switch", ("zap",)), ("Nintendo - Switch (DLC)", ("zap pack",))
        t = totals.Target({game: frozenset({"Zap"}), dlc: frozenset({"Zap Pack"})}, {}, 0.0, names={game: "Zap", dlc: "Zap Pack"})
        u = totals.UserPart({game: (totals.HAVE, frozenset({"Zap"}))}, 0.0, 0.0, 1, {})
        out = totals.combine(t, u, totals.system_dats(sw), totals.addon_dats(sw))
        self.assertEqual((out["target_games"], out["have_games"], out["missing_games"]), (1, 1, 0))
        self.assertEqual((out["target_addons"], out["have_addons"], out["target_system"]), (1, 0, 0))
        self.assertEqual(out["missing_names"], [])                                          # (the DLC is not "missing")

    def test_a_better_version_is_a_release_not_a_disk(self) -> None:
        names = ["Elvira II (1992)(Accolade)(Disk 1 of 3)[cr][tr pl].adf", "Elvira II (1992)(Accolade)(Disk 2 of 3)[cr SR][tr pl].adf",
                 "Elvira II (1992)(Accolade)(Disk 3 of 3)[tr pl].adf"]
        self.assertEqual({totals._release_of(n) for n in names}, {"Elvira II (1992)(Accolade)"})
        self.assertEqual(totals._release_of("Zap (USA) (Disc 2).cue"), "Zap (USA)")

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
        self.assertEqual(again["working"]["step"], "target")       # what it is doing, for how long (shown while it works)
        self.assertGreaterEqual(again["working"]["seconds"], 0)
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
        up = self.call("GET", "/api/library/upgrades?platform=Commodore%20Amiga")
        self.assertTrue(up["ready"])
        self.assertEqual(len(up["items"]), out["not_preferred"])                     # the list is the count
        for kind, field in (("excluded", "owned_but_excluded"), ("incomplete", "owned_incomplete")):
            lst = self.call("GET", f"/api/library/upgrades?platform=Commodore%20Amiga&kind={kind}")
            self.assertTrue(lst["ready"])
            self.assertEqual(len(lst["items"]), out[field])                          # each count has its list
            self.assertTrue(all(r["have"] and r["title"] for r in lst["items"]))
        self.assertTrue(self.call("GET", "/api/library/upgrades?platform=Commodore%20Amiga&kind=excluded")["items"][0]["why"])   # and says why
        # other rules: the list is of the new rules, never of the ones before (it says "not ready" until they are calculated)
        self.call("POST", "/api/library/profile", {"platform": "Commodore Amiga", "latest_only": False, "best_variant": False})
        lst = {"ready": False}
        for _ in range(200):
            lst = self.call("GET", "/api/library/upgrades?platform=Commodore%20Amiga")
            if lst["ready"]:
                break
            time.sleep(0.05)
        self.assertTrue(lst["ready"])
        self.assertEqual(len(lst["items"]), self.totals()["not_preferred"])
        self.call("POST", "/api/library/profile", {"platform": "Commodore Amiga", "reset": True})
        out = self.totals()
        ins = {}
        for _ in range(100):
            ins = self.call("GET", "/api/library/insights?platform=Commodore%20Amiga&want=space,best_missing,avg_rating,regions")
            if ins["ready"]:
                break
            time.sleep(0.05)
        self.assertTrue(ins["ready"])
        self.assertTrue(0 < ins["space"]["have"] <= ins["space"]["target"])                  # what you own against the library the rules describe
        self.assertIsNone(ins["best_missing"])                                               # (no ratings installed)
        self.assertEqual(ins["avg_rating"], {"target": None, "owned": None, "rated_owned": 0})
        self.assertEqual(sum(r["games"] for r in ins["regions"]), out["have_games"])
        row = up["items"][0]
        self.assertTrue(row["title"] and row["have"] and row["better"])
        self.assertTrue(set(row["have"]).isdisjoint(b["name"] for b in row["better"]))
        self.assertTrue(all(f["crc"] and f["size"] for b in row["better"] for f in b["files"]))          # the checksums of the preferred version
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
        self.assertIn("build again", refused["error"])
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

    def test_the_library_page_has_the_tiles_the_rules_bar_and_the_live_list(self) -> None:
        for needle in ('id="sys-tiles"', 'id="tile-lists"', 'id="rules-bar"', 'id="lib-live"', 'id="lib-apply-btn"',
                       'id="set-tiles-panel"', 'aria-live="polite"'):
            self.assertIn(needle, self.html)
        self.assertNotIn('id="tab-overview"', self.html)                      # the Overview page is gone
        for needle in ("/api/library/totals", "/api/library/insights", "/api/settings/tiles", "function renderTotals", "function loadTotals",
                       "scheduleTotals", "const TILES", "have_rules", "Scan to see how many you have", "calculating...", "recalculating...",
                       "autoSchedule", "Updating the list..."):
            self.assertIn(needle, self.js, needle)
        self.assertIn(".tile-grid", self.css)

    def test_previews_recalculate_and_block_apply_while_stale(self) -> None:
        for needle in ("const Previews", "Recalculate preview", "Recalculates with your current rules and folder contents",
                       "Rules changed since this preview", "Could not calculate", '"Try again"',
                       "Calculating...", "clockText(it.at)", "staleAll", "Previews.onScan()", "plan_id: plan.plan_id",
                       'b.dataset.stale === "1"', 'b.dataset.busy === "1"', 'role: "status"', '"aria-live": "polite"'):
            self.assertIn(needle, self.js, needle)
        for pair in ("lib",):
            self.assertIn(f"{pair}: {{ btn:", self.js)
        for needle in (".pv-banner", ".pv-stale", ".pv-attn", ".pv-spin", ".pv-meta"):
            self.assertIn(needle, self.css)


if __name__ == "__main__":
    unittest.main()


class BugCheckTotalsTests(unittest.TestCase):
    """The tiles count what the library keeps (Amendment 54): a borrowed disk you lack is missing, the discs and SPS releases of the
    games DAT's games have their size and rating, a game is all of its files."""

    def manager(self, dats: list[DatFile]) -> totals.TotalsManager:
        m = totals.TotalsManager(lambda platform: dats)
        m.request(AMIGA, EN, "sig")
        self.assertTrue(m.wait_idle(30))
        return m

    def test_the_borrowed_disk_you_lack_is_missing(self) -> None:
        t = target([FOO1, FOO3, FOO2_DE, "Solo (1992)(Pub)"])
        self.assertEqual(list(t.borrowed.values()), [frozenset({FOO2_DE + ".adf"})])
        state = ManagerTests.state(None, [FOO1, FOO3, "Solo (1992)(Pub)"])        # (you have disks 1 and 3)
        m = totals.TotalsManager(lambda platform: [dat_of([FOO1, FOO3, FOO2_DE, "Solo (1992)(Pub)"])])
        with mock.patch.object(totals, "compute_user", lambda result, platform, profile, *rest: totals.compute_user_items(state.items, platform, profile)):
            m.request(AMIGA, EN, "d", state)
            self.assertTrue(m.wait_idle(30))
            m.request(AMIGA, EN, "d", state)
            self.assertTrue(m.wait_idle(30))
            self.assertIn(FOO2_DE + ".adf", m.missing_names(AMIGA.name))

    def test_the_system_disks_you_lack_are_never_missing(self) -> None:
        ks = "Commodore Amiga - Kickstart-Disks"
        emutos = "EmuTOS v0.9.0 (2013-02-28)(EmuTOS Development Team)(A1000)(PD)"
        dats = [dat_of(["Solo (1992)(Pub)", "Other (1992)(Pub)"]), dat_of([emutos], ks)]
        state = ManagerTests.state(None, ["Solo (1992)(Pub)"])
        m = totals.TotalsManager(lambda platform: dats)
        with mock.patch.object(totals, "compute_user", lambda result, platform, profile, *rest: totals.compute_user_items(state.items, platform, profile)):
            for _ in range(2):
                m.request(AMIGA, EN, "d", state)
                self.assertTrue(m.wait_idle(30))
            names = m.missing_names(AMIGA.name)
        self.assertEqual(names, frozenset({"Other (1992)(Pub).adf"}))

    def test_an_incomplete_system_disk_is_no_game_to_look_at(self) -> None:
        wb = "Commodore Amiga - Operating Systems - Workbench"
        m = totals.TotalsManager(lambda platform: [])
        m._want[AMIGA.name] = totals._Want(AMIGA, EN, ("t",), None, ("u",))
        incomplete = totals.INCOMPLETE_OWNED
        m._users[AMIGA.name] = (("u",), types.SimpleNamespace(games={}, problems={
            (wb, "Workbench v1.3"): (incomplete, ("Workbench v1.3 (Disk 2 of 2)(Extras).adf",), "missing disk 1"),
            ("Commodore Amiga - Games - [ADF]", "Solo"): (incomplete, ("Solo (1992)(Pub)(Disk 2 of 2).adf",), "missing disk 1")}))
        self.assertEqual([r["title"] for r in m.problems(AMIGA.name, incomplete)], ["Solo (1992)(Pub)(Disk 2 of 2).adf"])

    def test_one_rule_says_what_counts_as_a_game(self) -> None:
        apart = totals.apart_dats(AMIGA)
        self.assertEqual(apart, totals.system_dats(AMIGA))
        self.assertTrue(totals.counts_as_game("Commodore Amiga - Games - [ADF]", apart))
        for dat in ("Commodore Amiga - Firmware", "Commodore Amiga - Kickstart-Disks", "Commodore Amiga - Operating Systems - Workbench"):
            self.assertFalse(totals.counts_as_game(dat, apart), dat)
        self.assertFalse(totals.counts_as_game("Commodore Amiga - Games - [ADF]", apart, bios=True))             # (a BIOS file of a game DAT)
        sw = platforms.get_platform("Nintendo Switch")
        self.assertFalse(totals.counts_as_game("Nintendo - Switch (DLC)", totals.apart_dats(sw)))
        self.assertTrue(totals.row_is_bios({"category": "bios"}) and totals.row_is_bios({"tags": {"bios": True}}))
        self.assertFalse(totals.row_is_bios({"category": "kept"}) or totals.row_is_bios(None))

    def test_the_target_says_why_a_version_the_rules_put_out_lost(self) -> None:
        old, new = "Game v1.0 (1990)(Pub)", "Game v1.1 (1990)(Pub)"
        profile = dataclasses.replace(EN, one_per_game=True, best_variant=True)
        t = totals.compute_target(AMIGA, [dat_of([old, new])], profile)
        (key,) = t.games
        self.assertEqual(t.games[key], frozenset({new + ".adf"}))
        self.assertIn("a newer version", t.why[(key, old + ".adf")])
        self.assertNotIn((key, new + ".adf"), t.why)                                        # (the kept one lost to nothing)
        # ... and the list of better versions carries it (the file you own, then the row)
        m = self.manager([dat_of([old, new])])
        u = totals.compute_user_items([Item(key=0, dat=GAMES, rom=dat_of([old]).roms[0], style="tosec", path=Path("g.adf"), member=None)], AMIGA, profile)
        rows = [(k, sorted(n), sorted(t.games[k])) for k, (st, n) in u.games.items() if k in t.games and not (n & t.games[k])]
        self.assertEqual(m.describe_upgrades(AMIGA, rows, {old + ".adf": "because"})[0]["why"], "because")

    def test_a_borrowed_disk_is_part_of_its_set_s_space_and_no_game_of_its_own(self) -> None:
        m = self.manager([dat_of([FOO1, FOO3, FOO2_DE])])
        self.assertEqual(m.insights(AMIGA, ["space"], None)["space"]["target"], 3)              # (three disks of one byte each)
        self.assertEqual(m.borrowed_names(AMIGA.name), frozenset({FOO2_DE + ".adf"}))

    def test_space_and_ratings_count_every_file_of_discs_sps_releases_and_sets(self) -> None:
        cd, sps = "Commodore Amiga CD32 - Games - [ISO]", "Commodore Amiga - Games - SPS"
        r = lambda name, game, dat, size: Rom(name=name, size=size, crc="", md5="", sha1="", game=game, dat=dat)   # noqa: E731
        trio = [r(f"Trio (1991)(Pub)(Disk {i} of 3).adf", f"Trio (1991)(Pub)(Disk {i} of 3)", GAMES, 1000) for i in (1, 2, 3)]
        g = "Zool (1993)(Gremlin)"
        cd_roms = [r(g + ".cue", g, cd, 100), r(g + "[Track 01 of 02].iso", g, cd, 600_000), r(g + "[Track 02 of 02].wav", g, cd, 50_000)]
        library.register_disc_files(cd, cd_roms)
        rel = "Turrican (1990)(Rainbow Arts)[0123]"
        sps_roms = [r(f"{rel} - Turrican_Disk{i}.ipf", rel, sps, 10_000) for i in (1, 2)]
        dats = [DatFile(name=GAMES, description=GAMES, version="1", roms=[r("Zool (1993)(Gremlin).adf", g, GAMES, 1), *trio]),
                DatFile(name=sps, description=sps, version="1", roms=sps_roms), DatFile(name=cd, description=cd, version="1", roms=cd_roms)]
        m = self.manager(dats)
        lookup = {"Zool": (9.0, 500), "Turrican": (8.5, 400), "Trio": (6.0, 60)}.get
        out = m.insights(AMIGA, ["space", "best_missing", "avg_rating"], lookup)
        self.assertEqual(out["space"]["target"], 3 * 1000 + 650_100 + 2 * 10_000)
        self.assertEqual(out["best_missing"]["title"], "Zool")
        self.assertEqual(out["avg_rating"]["target"], round((9.0 + 8.5 + 6.0) / 3, 1))
        # you have the floppy: the better version is the CD32 disc, with its files
        u = totals.compute_user_items([Item(key=0, dat=GAMES, rom=dats[0].roms[0], style="tosec", path=Path("z.adf"), member=None)], AMIGA, EN)
        t = m._targets[AMIGA.name][1]
        rows = [(k, sorted(n), sorted(t.games[k])) for k, (st, n) in u.games.items() if k in t.games and not (n & t.games[k])]
        up = m.describe_upgrades(AMIGA, rows)
        self.assertEqual((up[0]["title"], len(up[0]["better"])), ("Zool", 1))                 # (one better version: the disc ...)
        self.assertEqual(len(up[0]["better"][0]["files"]), 3)                                 # (... with its cue and two tracks)

    def test_a_cd_game_whose_tracks_carry_their_own_tag_is_one_game(self) -> None:
        cd = "Commodore Amiga CD32 - Games - [BIN]"
        g = "Akira (1995)(Ice)(M3)[!]"
        roms = [Rom(name=g + f, size=1, crc="", md5="", sha1="", game=g, dat=cd) for f in (".cue", "(Track 01 of 02).bin", "(Track 02 of 02).bin")]
        library.register_disc_files(cd, roms)
        t = totals.compute_target(AMIGA, [DatFile(name=cd, description=cd, version="1", roms=roms)], EN)
        self.assertEqual(t.count, 1)


class MissingNamesTests(unittest.TestCase):
    """``TotalsManager.missing_names`` (what the Files tree lists as missing) on random ADF / CD32 / SPS databases and random owned files:
    a name of a game you have is never in it, and its games are the totals' missing games, a borrowed disk with its set."""

    CD, SPS = "Commodore Amiga CD32 - Games - [ISO]", "Commodore Amiga - Games - SPS"

    def setUp(self) -> None:
        known = library._DISC_FILES.get(self.CD)
        self.addCleanup(lambda: library._DISC_FILES.__setitem__(self.CD, known) if known is not None else library._DISC_FILES.pop(self.CD, None))

    def databases(self, rng: random.Random) -> list[DatFile]:
        """Titles of one disk (with a newer version, a German and a cracked release), sets of 2-3 disks (one disk only in the German
        edition: a set made with a borrowed disk; sometimes a whole German set too), CD32 discs (cue + tracks; the floppy as well) and
        SPS releases of 1-3 images (some German)."""
        r = lambda name, game, dat: Rom(name=name, size=1, crc="", md5="", sha1="", game=game, dat=dat)     # noqa: E731
        adf: set[str] = set()
        cd: list[Rom] = []
        sps: list[Rom] = []
        for i in range(rng.randint(3, 8)):
            title, year = f"T{i}", 1990 + rng.randint(0, 4)
            base = f"{title} ({year})(Pub)"
            kind = rng.choice(["one", "one", "set", "borrow", "cd", "sps"])
            if kind == "one":
                adf.add(base)
                for extra, p in ((f"{title} v1.1 ({year})(Pub)", 0.4), (base + "(de)", 0.3), (base + "[cr FLT]", 0.3)):
                    if rng.random() < p:
                        adf.add(extra)
            elif kind in ("set", "borrow"):
                n = rng.randint(2, 3)
                lent = rng.randint(1, n) if kind == "borrow" else 0
                adf.update(f"{base}(Disk {d} of {n})" for d in range(1, n + 1) if d != lent)
                if lent:
                    adf.add(f"{base}(DE)(Disk {lent} of {n})")
                if rng.random() < 0.3:
                    adf.update(f"{base}(DE)(Disk {d} of {n})" for d in range(1, n + 1))
            if kind == "cd" or rng.random() < 0.2:
                g = base + ("(M3)" if rng.random() < 0.3 else "")
                cd += [r(g + f, g, self.CD) for f in (".cue", "[Track 01 of 02].iso", "[Track 02 of 02].wav")]
                if rng.random() < 0.3:
                    adf.add(base)
            if kind == "sps" or rng.random() < 0.2:
                rel = f"{base}{'(DE)' if rng.random() < 0.3 else ''}[{1000 + i}]"
                sps += [r(f"{rel} - {title}_Disk{d}.ipf", rel, self.SPS) for d in range(1, rng.randint(1, 3) + 1)]
        dats = [DatFile(name=GAMES, description=GAMES, version="1", roms=[r(n + ".adf", n, GAMES) for n in sorted(adf)])]
        if cd:
            library.register_disc_files(self.CD, cd)
            dats.append(DatFile(name=self.CD, description=self.CD, version="1", roms=cd))
        if sps:
            dats.append(DatFile(name=self.SPS, description=self.SPS, version="1", roms=sps))
        return dats

    def answer(self, dats: list[DatFile], owned: list[Item], profile: library.LibraryProfile) -> tuple[dict, frozenset]:
        m = totals.TotalsManager(lambda platform: dats)
        state = ManagerTests.state(None, [])
        with mock.patch.object(totals, "compute_user", lambda result, platform, prof, *rest: totals.compute_user_items(owned, platform, prof)):
            m.request(AMIGA, profile, "d", state)
            self.assertTrue(m.wait_idle(30))
            m.request(AMIGA, profile, "d", state)
            self.assertTrue(m.wait_idle(30))
            return m.request(AMIGA, profile, "d", state), m.missing_names(AMIGA.name)

    def test_missing_names_never_hold_a_game_you_have_on_random_libraries(self) -> None:
        rng = random.Random(11)
        lent_seen = owned_lent_seen = 0
        for _ in range(120):
            dats = self.databases(rng)
            owned = [Item(key=i, dat=x.dat, rom=x, style="tosec", path=Path(x.name), member=None)
                     for i, x in enumerate(x for d in dats for x in d.roms if rng.random() < 0.6)]
            profile = replace(EN, languages=rng.choice([("En",), ("En", "De"), ()]), borrow_other_editions=rng.random() < 0.7,
                              latest_only=rng.random() < 0.8, combine_alt_titles=rng.random() < 0.7)
            answer, missing = self.answer(dats, owned, profile)
            self.assertIsNotNone(missing)
            self.assertFalse(answer["calculating"])
            t = totals.compute_target(AMIGA, dats, profile)
            u = totals.compute_user_items(owned, AMIGA, profile)
            have = {k for k, (st, _n) in u.games.items() if st == totals.HAVE}
            lacking = [k for k in t.games if k not in have]
            lent = {n for k in lacking for n in t.borrowed.get(k, ())}
            self.assertEqual(len(lacking), answer["missing_games"])
            # a name of a game you have is never missing (a disk it shares with a set you lack, lent to that set, excepted) ...
            yours = {n for k in have for n in (*u.games[k][1], *t.games.get(k, ()))}
            self.assertEqual((missing & yours) - lent, frozenset())
            # ... so a file you own that is missing is one of a game you do not have: half a set, a version the rules leave out
            for it in owned:
                n = library._rom_name(it)
                if n in missing and n not in lent:
                    self.assertNotEqual((u.games.get(library.game_key(it, profile.combine_alt_titles)) or ("",))[0], totals.HAVE, n)
            # every file of every game you lack is missing (the disks its set borrows too), and nothing else
            self.assertEqual(missing, frozenset(n for k in lacking for n in (*t.games[k], *t.borrowed.get(k, ()))))
            lent_seen += bool(lent)
            owned_lent_seen += bool(lent & {library._rom_name(it) for it in owned})
        self.assertTrue(lent_seen and owned_lent_seen)                  # (the borrowed sets were drawn, owned and not)
