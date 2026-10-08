"""RetroArch saves, the way the user specified them (Amendment 31).

* Without a RetroArch config the app is not aware that saves exist (nothing walks a save folder, no field, no column).
* With one, every scan reports how many saves there are and which title each belongs to (a save index, built once).
* A build renames the saves with their ROMs; for a game the rules replace or archive, the profile's default or the game's own
  choice (keep both ROMs / archive the saves with the ROM / leave the saves) decides.
"""

from __future__ import annotations

import os
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

from tests.test_collection_api import CollectionCase, SNES, put, tree  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)
from tests.test_saves_in_builds import SavesCase, SAVE_NAMES

from romorg import library, retroarch, saveindex


def profile_url(platform: str = SNES) -> str:
    return "/api/library/profile?platform=" + urllib.parse.quote(platform)


class NoConfig(CollectionCase):
    """No RetroArch config: saves are not even looked at."""
    GAMES = SavesCase.GAMES

    def make(self, with_saves: bool) -> Path:
        root = self.tmp / "snes"
        put(root / "Zed (USA).sfc", self.rom["Zed (USA)"])
        put(root / "Zed (Europe).sfc", self.rom["Zed (Europe)"])
        put(root / "Zed (USA).srm", b"beside the ROM")             # (an ordinary file: it is in both worlds)
        if with_saves:
            for n in SAVE_NAMES:                      # a RetroArch saves folder lies around
                put(self.tmp / "assets" / "saves" / "bsnes" / n.format(n="Zed (USA)"), b"x")
        return root

    def trap(self) -> list[Any]:
        """Patches that fail the test when any save code runs."""
        return [mock.patch.object(retroarch, "SaveWalk", side_effect=AssertionError("a save folder was walked")),
                mock.patch.object(retroarch, "list_saves", side_effect=AssertionError("saves were listed")),
                mock.patch.object(saveindex, "build", side_effect=AssertionError("the save index was built"))]

    def test_nothing_about_saves_anywhere(self) -> None:
        root = self.make(True)
        patches = self.trap()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        status = self.call("GET", "/api/status")
        self.assertNotIn("saves", status["scan"])
        games = self.call("GET", "/api/scan/results?kind=games&limit=50")
        self.assertNotIn("saves_on", games)
        self.assertTrue(all("saves" not in i for i in games["items"]))
        for kind in ("matched", "missing"):
            page = self.call("GET", f"/api/scan/results?kind={kind}&limit=50&sort=saves_desc")
            self.assertTrue(all("saves" not in i for i in page["items"]))
        code, out = self.http("GET", "/api/scan/results?kind=saves")
        self.assertEqual((code, out.get("code")), (409, "no_retroarch"))
        plan = self.call("POST", "/api/library/plan", {"limit": 100})
        self.assertNotIn("saves", plan)
        self.assertTrue(all("saves" not in i for i in plan["items"]))
        self.assertNotIn("saved_games", plan["profile"])
        self.assertNotIn("saved_overrides", plan["profile"])
        info = self.call("GET", profile_url())
        self.assertNotIn("saved_games", info["profile"])
        self.assertNotIn("saved_games", info["defaults"])
        code, _ = self.http("POST", "/api/library/saved", {"platform": SNES, "choice": "archive", "games": [{"dat": "d", "game": "g"}]})
        self.assertEqual(code, 409)

    def test_a_plan_is_the_same_whether_save_files_lie_around_or_not(self) -> None:
        def plan_of(with_saves: bool) -> dict[str, Any]:
            root = self.make(with_saves)
            self.job("/api/scan", {"path": str(root), "platform": SNES})
            plan = self.call("POST", "/api/library/plan", {"limit": 100})
            keep = ("counts", "reasons", "categories", "actionable", "empty", "profile")
            return {**{k: plan[k] for k in keep}, "items": sorted((i["from"], i["to"], i["status"]) for i in plan["items"])}
        without = plan_of(False)
        for sub in (self.tmp / "snes", self.tmp / "assets"):
            if sub.exists():
                import shutil
                shutil.rmtree(sub)
        self.assertEqual(plan_of(True), without)

    def test_collection_scan_has_no_saves_field_and_a_loose_save_goes_to_other(self) -> None:
        root = self.tmp / "My ROMs"
        put(root / "Dump" / "zed usa.sfc", self.rom["Zed (USA)"])
        put(root / "Dump" / "zed usa.srm", b"a loose save beside a cartridge ROM")
        patches = self.trap()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        scan = self.scan(root)
        self.assertNotIn("saves", scan)
        self.assertTrue(all("saves" not in s for s in scan["systems"]))
        archive = root.with_name(root.name + "-archive")
        res = self.job("/api/collection/apply")
        self.assert_clean(res)
        self.assertTrue((archive / "_other" / "Dump" / "zed usa.srm").is_file())       # an ordinary non-ROM file
        self.assertFalse((root / "snes" / "zed usa.srm").exists())                     # (and no sidecar following its ROM)
        self.undo()
        self.assertTrue((root / "Dump" / "zed usa.srm").is_file())


class Pure(unittest.TestCase):
    """``saveindex.build`` on plain data: the shapes of names that are not No-Intro."""

    def entries(self, root: Path, names: list[str], here: bool = True) -> list[tuple[Path, Path, bool]]:
        return [(root / "core" / n, root, here) for n in names]

    def build(self, names: list[str], by_file: dict[str, Any] | None = None, by_dat: dict[str, Any] | None = None, per_core: bool = True) -> Any:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for n in names:
                put(root / "core" / n, b"x")
            return saveindex.build(self.entries(root, names), by_file or {}, by_dat or {}, per_core)

    def test_kinds_and_screenshots_are_not_counted(self) -> None:
        r = self.build(["G (USA).srm", "G (USA).state", "G (USA).state1", "G (USA).state1.png", "G (USA).state.auto", "G (USA).A1.bin",
                        "G (USA).1.srm", "G (USA).eep", "G (USA).mpk"])
        s = r.sets[retroarch.fold_name("G (USA)")]
        self.assertEqual((s.saves, s.states, s.shots), (5, 3, 1))
        self.assertEqual(r.totals()["files"], 8)

    def test_a_zip_matches_by_its_name_and_its_members_and_a_title_without_rom_by_the_dat(self) -> None:
        ref = ("DAT", "Super Mario World (USA)", "Super Mario World")
        by_file = {retroarch.fold_name("smw"): ref, retroarch.fold_name("Super Mario World (USA)"): ref}
        by_dat = {retroarch.fold_name("Yoshi (USA)"): ("DAT", "Yoshi (USA)", "Yoshi")}
        r = self.build(["smw.srm", "Super Mario World (USA).state1", "Yoshi (USA).srm", "Nothing (USA).srm"], by_file, by_dat)
        by = {s.name: s for s in r.sets.values()}
        self.assertEqual((by["smw"].match, by["smw"].title), ("rom", "Super Mario World"))
        self.assertEqual(by["Yoshi (USA)"].match, "dat")
        self.assertEqual(by["Nothing (USA)"].match, "none")
        t = r.totals()
        self.assertEqual((t["sets"], t["rom_sets"], t["dat_sets"], t["unmatched_sets"]), (4, 2, 1, 1))
        self.assertEqual(r.title_counts("Super Mario World")["total"], 2)

    def test_tosec_style_names_with_brackets_and_dots(self) -> None:
        name = "Dr. Mario v1.2 (1990)(Nintendo)(US)[a]"
        ref = ("TOSEC", name, "Dr. Mario")
        r = self.build([f"{name}.srm", f"{name}.state1", "Other (1991)(X)[!].srm"], {retroarch.fold_name(name): ref})
        s = r.sets[retroarch.fold_name(name)]
        self.assertEqual((s.match, s.saves, s.states), ("rom", 1, 1))
        self.assertEqual(r.game_counts("TOSEC", name)["total"], 2)

    def test_the_longest_known_name_wins_and_a_save_name_with_a_dot_is_not_cut_short(self) -> None:
        refs = {retroarch.fold_name("Game"): ("D", "Game", "Game"), retroarch.fold_name("Game (USA)"): ("D", "Game (USA)", "Game")}
        r = self.build(["Game (USA).A1.bin", "Game.srm"], refs)
        self.assertEqual(sorted((s.name, s.saves) for s in r.sets.values()), [("Game", 1), ("Game (USA)", 1)])

    def test_files_that_are_no_saves_are_ignored(self) -> None:
        r = self.build(["notes.txt", "cover.png", "Game (USA).png", "Game (USA).lpl"], {retroarch.fold_name("Game (USA)"): ("D", "Game (USA)", "Game")})
        self.assertEqual(r.sets, {})

    def test_unmatched_sets_in_other_cores_folders_are_not_counted(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mine, other = put(root / "bsnes" / "A (USA).srm", b"x"), put(root / "Some Game Folder" / "B (USA).srm", b"x")
            r = saveindex.build([(mine, root, True), (other, root, False)], {}, {}, per_core=True)
            self.assertEqual(r.totals()["unmatched_sets"], 1)
            r = saveindex.build([(mine, root, True), (other, root, False)], {}, {}, per_core=False)
            self.assertEqual(r.totals()["unmatched_sets"], 2)

    @unittest.skipUnless(os.name == "nt", "names are compared without regard to case on Windows only")
    def test_case_is_ignored_on_windows(self) -> None:
        r = self.build(["zed (usa).srm"], {retroarch.fold_name("Zed (USA)"): ("D", "Zed (USA)", "Zed")})
        self.assertEqual(next(iter(r.sets.values())).match, "rom")

    @unittest.skipIf(os.name == "nt", "names are compared without regard to case on Windows only")
    def test_case_matters_elsewhere(self) -> None:
        r = self.build(["zed (usa).srm"], {retroarch.fold_name("Zed (USA)"): ("D", "Zed (USA)", "Zed")})
        self.assertEqual(next(iter(r.sets.values())).match, "none")


class Index(SavesCase):
    """The index of a platform scan through the real server."""

    def setup_world(self) -> Path:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)", "Solo (USA)")
        self.save("bsnes", "Zed (USA)")                                       # .srm .state1 .state1.png .A1.bin
        self.save("bsnes", "Zed (Europe)", ("{n}.srm",))                      # another edition of the title
        self.save("bsnes", "Mix (Europe)", ("{n}.srm", "{n}.state2"))         # a DAT game without a ROM here
        self.save("bsnes", "Mystery (USA)", ("{n}.srm",))                     # nothing knows it
        self.save("Genesis Plus GX", "Zed (USA)", ("{n}.srm",))               # another system's core: never counted
        return root

    def test_counts_and_title_matching(self) -> None:
        root = self.setup_world()
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        t = self.call("GET", "/api/status")["scan"]["saves"]
        self.assertEqual((t["files"], t["saves"], t["states"], t["screenshots"]), (7, 5, 2, 1))
        self.assertEqual((t["sets"], t["rom_sets"], t["dat_sets"], t["unmatched_sets"]), (4, 2, 1, 1))
        rows = self.call("GET", "/api/scan/results?kind=saves&limit=50")["items"]
        by = {r["name"]: r for r in rows}
        self.assertEqual((by["Zed (USA)"]["match"], by["Zed (USA)"]["saves"], by["Zed (USA)"]["states"], by["Zed (USA)"]["screenshots"]),
                         ("rom", 2, 1, 1))
        self.assertEqual(by["Zed (USA)"]["cores"], ["bsnes"])
        self.assertEqual(by["Mix (Europe)"]["match"], "dat")
        self.assertEqual(by["Mystery (USA)"]["match"], "none")
        self.assertEqual([r["match"] for r in rows], ["rom", "rom", "dat", "none"])
        only = self.call("GET", "/api/scan/results?kind=saves&match=none")["items"]
        self.assertEqual([r["name"] for r in only], ["Mystery (USA)"])

    def test_browse_games_have_the_saves_column_the_title_total_the_filter_and_the_sort(self) -> None:
        root = self.setup_world()
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        page = self.call("GET", "/api/scan/results?kind=games&limit=50")
        self.assertTrue(page["saves_on"])
        by = {i["name"]: i for i in page["items"]}
        self.assertEqual(by["Zed (USA)"]["saves"], {"saves": 2, "states": 1, "total": 3, "title_total": 4})     # + Zed (Europe)'s .srm
        self.assertEqual(by["Zed (Europe)"]["saves"]["title_total"], 4)
        self.assertEqual(by["Mix (Europe)"]["saves"]["total"], 2)                                          # no ROM, still shown
        self.assertFalse(by["Mix (Europe)"]["have"])
        self.assertIsNone(by["Solo (USA)"]["saves"])
        only = self.call("GET", "/api/scan/results?kind=games&limit=50&saves=1")["items"]
        self.assertEqual(sorted(i["name"] for i in only), ["Mix (Europe)", "Zed (Europe)", "Zed (USA)"])
        top = self.call("GET", "/api/scan/results?kind=games&limit=50&sort=saves_desc")["items"]
        self.assertEqual([i["name"] for i in top][:3], ["Zed (USA)", "Mix (Europe)", "Zed (Europe)"])
        low = self.call("GET", "/api/scan/results?kind=games&limit=50&sort=saves_asc")["items"]
        self.assertEqual([i["name"] for i in low][:3], ["Zed (Europe)", "Mix (Europe)", "Zed (USA)"])
        self.assertEqual((low[-1]["saves"] or {}).get("total", 0), 0)                                        # games without saves last
        matched = self.call("GET", "/api/scan/results?kind=matched&limit=50&sort=saves_desc")
        self.assertTrue(matched["saves_on"])
        self.assertEqual(matched["items"][0]["file"], "Zed (USA).sfc")
        missing = self.call("GET", "/api/scan/results?kind=missing&limit=50&saves=1")["items"]
        self.assertEqual([i["set_name"] or i["name"] for i in missing], ["Mix (Europe)"])   # a title you have no ROM for

    def test_it_is_built_once_per_scan_with_one_walk(self) -> None:
        root = self.setup_world()
        with mock.patch.object(saveindex, "build", wraps=saveindex.build) as built, \
                mock.patch.object(retroarch, "SaveWalk", wraps=retroarch.SaveWalk) as walked:
            self.job("/api/scan", {"path": str(root), "platform": SNES})
            self.call("GET", "/api/status")
            self.call("GET", "/api/scan/results?kind=games&limit=5")
            self.call("GET", "/api/scan/results?kind=saves")
            self.call("POST", "/api/library/plan", {"limit": 5})
            self.call("POST", "/api/library/plan", {"limit": 5, "refresh": True})
            self.assertEqual((built.call_count, walked.call_count), (1, 1))
            self.job("/api/scan", {"path": str(root), "platform": SNES})            # a new scan: a new index
            self.call("GET", "/api/status")
            self.assertEqual((built.call_count, walked.call_count), (2, 2))

    def test_unsorted_save_folder_counts_what_it_cannot_place_as_unmatched(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        put(self.saves / "Zed (USA).srm", b"s")                                    # directly in the root
        put(self.saves / "Whatever (Japan).srm", b"s")
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        t = self.call("GET", "/api/status")["scan"]["saves"]
        self.assertEqual((t["rom_sets"], t["unmatched_sets"]), (1, 1))

    def test_collection_reports_saves_per_system_and_in_total(self) -> None:
        root = self.tmp / "My ROMs"
        put(root / "Dump" / "zed usa.sfc", self.rom["Zed (USA)"])
        put(root / "Dump" / "run.gba", self.rom["Run (USA)"])
        self.save("bsnes", "zed usa", ("{n}.srm", "{n}.state1"))
        self.save("bsnes", "Solo (USA)", ("{n}.srm",))                              # a DAT game without a ROM
        scan = self.scan(root)
        row = {s["name"]: s for s in scan["systems"]}
        self.assertEqual((row[SNES]["saves"]["files"], row[SNES]["saves"]["rom_sets"], row[SNES]["saves"]["dat_sets"]), (3, 1, 1))
        self.assertEqual(row["Nintendo Game Boy Advance"]["saves"]["files"], 0)
        self.assertEqual(scan["saves"]["files"], 3)


class Policies(SavesCase):
    """keep both ROMs / archive the saves with the ROM / leave the saves, as the default and per game."""

    def world(self) -> tuple[Path, list[str]]:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)", "Mix (USA)", "Mix (Europe)")
        saves = self.save("bsnes", "Zed (USA)") + self.save("bsnes", "Mix (USA)", ("{n}.srm", "{n}.state1"))
        return root, saves

    def roms(self, root: Path) -> list[str]:
        return sorted(x for x in tree(root) if not x.endswith("/"))

    def game(self, name: str) -> dict[str, str]:
        return {"dat": "Nintendo - Super Nintendo Entertainment System", "game": name}

    def choose(self, choice: str, *names: str) -> dict[str, Any]:
        return self.call("POST", "/api/library/saved", {"platform": SNES, "choice": choice, "games": [self.game(n) for n in names]})

    def test_preview_numbers_are_what_apply_does_for_every_default(self) -> None:
        for mode in ("keep", "leave", "archive"):
            with self.subTest(mode=mode):
                root, saves = self.world()
                self.mode(mode)
                aside = self.tmp / f"archive-{mode}"
                plan = self.lib_plan(root, aside_to=str(aside), refresh=True)
                s = plan["saves"]
                before = self.roms(root)
                self.assertEqual(self.roms(root), before)
                res = self.lib_apply(plan, aside_to=str(aside))
                if mode == "keep":
                    self.assertEqual((s["kept"], s["archive"]["files"], s["leave"]["files"]), (2, 0, 0))
                    self.assertEqual(self.roms(root), sorted(before))
                    self.assertNotIn("saves_archived", res)
                elif mode == "leave":
                    self.assertEqual((s["kept"], s["archive"]["files"], s["leave"]), (0, 0, {"files": 6, "games": 2}))
                    self.assertEqual(self.saved_files(), sorted(saves))
                    self.assertNotIn("saves_archived", res)
                else:
                    self.assertEqual((s["archive"]["files"], s["archive"]["games"]), (6, 2))
                    self.assertEqual(res["saves_archived"]["moved"], s["archive"]["files"])
                    self.assertEqual(self.saved_files(), [])
                if res.get("undo_log"):
                    self.job("/api/library/undo", {"log": res["undo_log"]})
                self.assertEqual(self.saved_files(), sorted(saves))
                self.assertEqual(self.roms(root), sorted(before))
                for p in list(self.saves.rglob("*")) + list(aside.rglob("*")):
                    pass
                import shutil
                shutil.rmtree(self.saves)
                self.saves.mkdir()
                shutil.rmtree(root)
                self.call("POST", "/api/library/profile", {"platform": SNES, "reset": True})

    def test_a_game_can_choose_against_the_default_and_the_choice_is_saved_in_the_profile(self) -> None:
        root, saves = self.world()
        self.mode("keep")
        info = self.choose("archive", "Zed (USA)")
        self.assertEqual(info["profile"]["saved_overrides"], [[self.game("x")["dat"], "Zed (USA)", "archive"]])
        again = self.call("GET", profile_url())
        self.assertEqual(again["profile"]["saved_overrides"], info["profile"]["saved_overrides"])
        aside = self.tmp / "archive"
        plan = self.lib_plan(root, aside_to=str(aside))
        s = plan["saves"]
        self.assertEqual((s["kept"], s["archive"]["games"], s["archive"]["files"], s["overrides"]), (1, 1, 4, 1))   # Mix (USA) stays
        by = {i["from"]: i for i in plan["items"]}
        self.assertEqual(by["Zed (USA).sfc"]["saves"]["effect"], "archive")
        self.assertEqual(by["Mix (USA).sfc"]["saves"]["effect"], "keep")
        res = self.lib_apply(plan, aside_to=str(aside))
        self.assertEqual(res["saves_archived"]["moved"], 4)
        self.assertEqual(self.saved_files(), sorted(s_ for s_ in saves if "Mix" in s_))
        self.assertEqual(self.roms(root), ["Mix (Europe).sfc", "Mix (USA).sfc", "Zed (Europe).sfc"])

    def test_the_other_way_round_a_game_is_kept_though_the_default_archives(self) -> None:
        root, saves = self.world()
        self.mode("archive")
        self.choose("keep", "Mix (USA)")
        plan = self.lib_plan(root)
        self.assertEqual((plan["saves"]["kept"], plan["saves"]["archive"]["games"]), (1, 1))
        self.lib_apply(plan)
        self.assertTrue((root / "Mix (USA).sfc").is_file())
        self.assertEqual(sorted(x for x in self.saved_files() if "Mix" in x), sorted(s for s in saves if "Mix" in s))
        self.assertEqual([x for x in self.saved_files() if "Zed" in x], [])

    def test_leave_for_one_game_and_back_to_the_default(self) -> None:
        root, saves = self.world()
        self.mode("archive")
        self.choose("leave", "Zed (USA)")
        plan = self.lib_plan(root)
        self.assertEqual((plan["saves"]["archive"]["games"], plan["saves"]["leave"]["games"]), (1, 1))
        self.choose("default", "Zed (USA)")
        self.assertEqual(self.call("GET", profile_url())["profile"]["saved_overrides"], [])
        plan = self.lib_plan(root, refresh=True)
        self.assertEqual((plan["saves"]["archive"]["games"], plan["saves"]["leave"]["games"]), (2, 0))

    def test_a_changed_choice_makes_an_old_preview_stale(self) -> None:
        root, _saves = self.world()
        plan = self.lib_plan(root)
        self.choose("archive", "Zed (USA)")
        code, out = self.http("POST", "/api/library/apply", {"plan_id": plan["plan_id"]})
        self.assertEqual((code, out.get("code")), (409, "stale_plan"))

    def test_bad_choices_are_refused(self) -> None:
        self.world()
        for body in ({"choice": "copy", "games": [self.game("x")]}, {"choice": "keep", "games": []}, {"choice": "keep"}):
            code, _ = self.http("POST", "/api/library/saved", {"platform": SNES, **body})
            self.assertEqual(code, 400, body)

    def test_library_filters_and_sort(self) -> None:
        root, _saves = self.world()
        put(root / "Solo (USA).sfc", self.rom["Solo (USA)"])
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        everything = self.call("POST", "/api/library/plan", {"limit": 100})
        with_saves = self.call("POST", "/api/library/plan", {"limit": 100, "saves": "with"})
        self.assertEqual(sorted(i["from"] for i in with_saves["items"]), ["Mix (USA).sfc", "Zed (USA).sfc"])
        self.assertEqual(with_saves["saves"]["rows_with"], 2)
        self.assertGreater(everything["total"], with_saves["total"])
        affected = self.call("POST", "/api/library/plan", {"limit": 100, "saves": "affected"})
        self.assertEqual(sorted(i["from"] for i in affected["items"]), ["Mix (USA).sfc", "Zed (USA).sfc"])      # both kept for their saves
        self.mode("leave")
        affected = self.call("POST", "/api/library/plan", {"limit": 100, "saves": "affected"})
        self.assertEqual({i["from"]: i["saves"]["effect"] for i in affected["items"]}, {"Mix (USA).sfc": "leave", "Zed (USA).sfc": "leave"})
        top = self.call("POST", "/api/library/plan", {"limit": 100, "sort": "saves_desc"})["items"]
        self.assertEqual(top[0]["from"], "Zed (USA).sfc")
        self.assertEqual(top[0]["saves"], {"saves": 2, "states": 1, "total": 3, "effect": "leave", "game": "Zed (USA)",
                                           "dat": "Nintendo - Super Nintendo Entertainment System"})
        code, _ = self.http("POST", "/api/library/plan", {"saves": "bogus"})
        self.assertEqual(code, 400)


class Renames(SavesCase):
    def test_rename_in_a_platform_build_undo_gives_the_old_names_back(self) -> None:
        root = self.tmp / "snes"
        put(root / "zed usa.sfc", self.rom["Zed (USA)"])
        old = self.save("bsnes", "zed usa")
        plan = self.lib_plan(root)
        self.assertEqual(plan["saves"]["rename"]["files"], 4)
        res = self.lib_apply(plan)
        self.assertEqual(self.saved_files(), sorted(f.replace("zed usa", "Zed (USA)") for f in old))
        self.job("/api/library/undo", {"log": res["undo_log"]})
        self.assertEqual(self.saved_files(), sorted(old))

    def test_a_platform_build_in_another_folder_copies_the_saves_and_undo_removes_the_copies(self) -> None:
        root = self.tmp / "snes"
        put(root / "zed usa.sfc", self.rom["Zed (USA)"])
        old = self.save("bsnes", "zed usa", ("{n}.srm", "{n}.state1"))
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        dest = self.tmp / "clean"
        opts = {"export_to": str(dest), "export_mode": "copy"}
        plan = self.call("POST", "/api/library/plan", {"limit": 5, **opts})
        self.assertEqual(plan["saves"]["rename"]["files"], 2)
        res = self.job("/api/library/apply", {"plan_id": plan["plan_id"], **opts})
        self.assertEqual(res["saves"]["copied"], 2)
        self.assertEqual(self.saved_files(), sorted(old + [f.replace("zed usa", "Zed (USA)") for f in old]))
        self.call("POST", "/api/library/export/undo", {"dest": str(dest), "follow": res["saves"]["journal"]})
        self.assertEqual(self.saved_files(), sorted(old))

    def test_collection_in_place_renames_in_one_pass(self) -> None:
        root = self.tmp / "My ROMs"
        put(root / "Dump" / "zed usa.sfc", self.rom["Zed (USA)"])
        put(root / "Dump" / "run.gba", self.rom["Run (USA)"])
        self.save("bsnes", "zed usa", ("{n}.srm", "{n}.A1.bin"))
        self.save("mGBA", "run", ("{n}.sav",))
        self.scan(root)
        plan = self.job("/api/collection/plan")
        row = next(r for r in plan["systems"] if r["platform"] == SNES)
        self.assertEqual(row["saves_plan"]["rename"]["files"], 2)
        res = self.job("/api/collection/apply")
        self.assert_clean(res)
        self.assertEqual([x for x in self.saved_files() if "bsnes" in x], ["bsnes/Zed (USA).A1.bin", "bsnes/Zed (USA).srm"])

    def test_collection_elsewhere_copies_the_saves_with_the_new_names(self) -> None:
        root = self.tmp / "My ROMs"
        put(root / "Dump" / "zed usa.sfc", self.rom["Zed (USA)"])
        self.save("bsnes", "zed usa", ("{n}.srm", "{n}.state1"))
        self.call("POST", "/api/collection/save", {"place": "elsewhere", "dest": str(self.tmp / "clean"), "mode": "copy"})
        self.scan(root)
        plan = self.job("/api/collection/plan")
        row = next(r for r in plan["systems"] if r["platform"] == SNES)
        self.assertEqual(row["saves_plan"]["rename"]["files"], 2)
        self.assertTrue(row["saves_plan"]["elsewhere"])
        self.job("/api/collection/apply")
        self.assertEqual(self.saved_files(), sorted(["bsnes/zed usa.srm", "bsnes/zed usa.state1", "bsnes/Zed (USA).srm", "bsnes/Zed (USA).state1"]))


class ProfileRoundTrip(unittest.TestCase):
    def test_saved_overrides_round_trip_and_are_normalised(self) -> None:
        p = library.LibraryProfile(saved_games="archive", saved_overrides=[("D", "b", "leave"), ("D", "a", "keep"), ("D", "a", "archive"),
                                                                           ("D", "c", "copy"), ("", "x", "keep"), ("D",)])
        self.assertEqual(p.saved_overrides, (("D", "a", "archive"), ("D", "b", "leave")))
        again = library.LibraryProfile.from_dict(p.to_dict())
        self.assertEqual(again, p)
        cfg: dict[str, Any] = {}
        plat = mock.Mock()
        plat.name = "Some System"
        plat.latest_dats = plat.best_variant_dats = plat.m3u_dats = plat.language_dats = plat.region_dats = ()
        library.store_profile(cfg, plat, p)
        self.assertEqual(library.load_profile(cfg, plat).saved_overrides, p.saved_overrides)

    def test_an_old_profile_without_the_fields_loads_with_the_defaults(self) -> None:
        old = library.LibraryProfile().to_dict()
        del old["saved_games"], old["saved_overrides"]
        got = library.LibraryProfile.from_dict(old)
        self.assertEqual((got.saved_games, got.saved_overrides), ("keep", ()))
        self.assertEqual(library.LibraryProfile.from_dict({"saved_overrides": "nonsense"}).saved_overrides, ())

    def test_signature_of_the_totals_ignores_the_saves_choices(self) -> None:
        from romorg import totals
        a, b = library.LibraryProfile(), library.LibraryProfile(saved_games="archive", saved_overrides=[("D", "g", "leave")])
        self.assertEqual(totals.profile_signature(a), totals.profile_signature(b))


if __name__ == "__main__":
    unittest.main()
