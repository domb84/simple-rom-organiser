"""RetroArch saves are part of the build (Amendment 30): a real server over HTTP, real folders, tiny synthetic No-Intro DATs and
a fake RetroArch install whose saves live in ONE folder, sorted into a sub-folder per core (the user's setup).

The three answers to "games you have saves for": ``keep`` (the default; the game stays), ``archive`` (the game is archived and
its saves go with it) and ``leave`` (today's behaviour: the game is archived, the saves stay where they are)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest import mock

from tests.test_collection_api import CollectionCase, SNES, put, tree  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)

BSNES_INFO = ('display_name = "Nintendo - SNES / SFC (bsnes)"\ncorename = "bsnes"\n'
              'supported_extensions = "smc|sfc|swc|fig|bs|st|gb|gbc|bml"\n')
GENESIS_INFO = ('display_name = "Sega - MS/GG/MD/CD (Genesis Plus GX)"\ncorename = "Genesis Plus GX"\n'
                'supported_extensions = "mdx|md|smd|gen|bin|cue|iso|chd|sms|gg|sg|68k|sgd"\n')
SAVE_NAMES = ("{n}.srm", "{n}.state1", "{n}.state1.png", "{n}.A1.bin")


class SavesCase(CollectionCase):
    GAMES = {"snes": ["Zed (USA)", "Zed (Europe)", "Solo (USA)", "Mix (USA)", "Mix (Europe)"],
             "gba": ["Run (USA)", "Bash (USA)"], "gb": ["Tetra (World)"]}

    def setUp(self) -> None:
        super().setUp()
        self.running = mock.patch("romorg.retroarch.is_running", return_value=False)
        self.running_mock = self.running.start()
        self.addCleanup(self.running.stop)
        self.saves = self.tmp / "assets" / "saves"
        ra_dir = self.tmp / "RetroArch"
        (ra_dir / "info").mkdir(parents=True)
        (ra_dir / "info" / "bsnes_libretro.info").write_text(BSNES_INFO)
        (ra_dir / "info" / "genesis_plus_gx_libretro.info").write_text(GENESIS_INFO)
        self.saves.mkdir(parents=True)
        (ra_dir / "retroarch.cfg").write_text(f'savefile_directory = "{self.saves}"\nsavestate_directory = "{self.saves}"\n'
                                              'sort_savefiles_enable = "true"\nsort_savestates_enable = "true"\n')
        self.call("POST", "/api/retroarch/select", {"custom": str(ra_dir)})

    # ---- helpers
    def save(self, core: str, name: str, patterns: tuple[str, ...] = SAVE_NAMES) -> list[str]:
        out = []
        for p in patterns:
            put(self.saves / core / p.format(n=name), p.encode())
            out.append(f"{core}/{p.format(n=name)}")
        return sorted(out)

    def mode(self, value: str) -> None:
        self.call("POST", "/api/library/profile", {"platform": SNES, "saved_games": value})

    def rom_folder(self, *names: str) -> Path:
        root = self.tmp / "snes"
        for n in names:
            put(root / f"{n}.sfc", self.rom[n])
        return root

    def lib_plan(self, root: Path, **extra: Any) -> dict[str, Any]:
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        return self.call("POST", "/api/library/plan", {"limit": 100, **extra})

    def lib_apply(self, plan: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return self.job("/api/library/apply", {"plan_id": plan["plan_id"], **extra})

    def saved_files(self) -> list[str]:
        return [x for x in tree(self.saves) if not x.endswith("/")]


class KeepMode(SavesCase):
    def test_a_game_with_saves_is_kept_and_the_preview_says_so(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (USA)")
        plan = self.lib_plan(root)
        self.assertEqual(plan["profile"]["saved_games"], "keep")                  # the default
        self.assertEqual(plan["reasons"]["kept_saved"], 1)
        self.assertEqual((plan["reasons"]["superseded"], plan["reasons"]["excluded"]), (0, 0))
        self.assertEqual(plan["saves"]["kept"], 1)
        self.assertTrue(any("you have saves" in (i.get("reason") or "") for i in plan["items"]), plan["items"])
        before = tree(root)
        self.assertEqual(plan["actionable"], 0)
        self.assertEqual(tree(root), before)
        self.assertEqual(len(self.saved_files()), 4)

    def test_without_saves_the_game_is_archived_as_before(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Something else (USA)")                                 # a save of another game
        plan = self.lib_plan(root)
        self.assertEqual((plan["reasons"]["kept_saved"], plan["reasons"]["superseded"] + plan["reasons"]["excluded"]), (0, 1))
        self.lib_apply(plan)
        self.assertEqual(sorted(x for x in tree(root) if not x.endswith("/")),
                         sorted(["Zed (Europe).sfc", "_superseded/Zed (USA).sfc"]))

    def test_the_save_of_another_systems_core_does_not_protect_a_game(self) -> None:
        root = self.rom_folder("Mix (USA)", "Mix (Europe)")
        self.save("Genesis Plus GX", "Mix (USA)")                                  # a Mega Drive game with the same name
        plan = self.lib_plan(root)
        self.assertEqual(plan["reasons"]["kept_saved"], 0)
        self.assertEqual(plan["reasons"]["superseded"] + plan["reasons"]["excluded"], 1)
        self.save("bsnes", "Mix (USA)")                                            # the SNES core's own save does
        plan = self.lib_plan(root, refresh=True)
        self.assertEqual(plan["reasons"]["kept_saved"], 1)

    def test_a_save_in_an_unsorted_saves_folder_protects_the_game(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        put(self.saves / "Zed (USA).srm", b"s")                                    # RetroArch does not sort by core here
        self.assertEqual(self.lib_plan(root)["reasons"]["kept_saved"], 1)

    def test_a_dot_in_a_name_and_a_longer_name_are_not_mixed_up(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (USA) Special", ("{n}.srm",))                       # another game that starts with the name
        self.assertEqual(self.lib_plan(root)["reasons"]["kept_saved"], 0)


class LeaveMode(SavesCase):
    def test_leave_is_todays_behaviour_the_game_goes_and_the_saves_stay(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        saves = self.save("bsnes", "Zed (USA)")
        self.mode("leave")
        plan = self.lib_plan(root)
        self.assertEqual(plan["reasons"]["kept_saved"], 0)
        self.assertEqual(plan["saves"]["archive"]["files"], 0)
        res = self.lib_apply(plan)
        self.assertEqual(sorted(x for x in tree(root) if not x.endswith("/")),
                         sorted(["Zed (Europe).sfc", "_superseded/Zed (USA).sfc"]))
        self.assertEqual(self.saved_files(), saves)
        self.assertNotIn("saves_archived", res)


class ArchiveMode(SavesCase):
    def test_the_saves_of_an_archived_game_go_with_it_and_undo_brings_them_back(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)", "Solo (USA)")
        saves = self.save("bsnes", "Zed (USA)")
        solo = self.save("bsnes", "Solo (USA)")                                    # a kept game: its saves never move
        self.mode("archive")
        aside = self.tmp / "archive"
        plan = self.lib_plan(root, aside_to=str(aside))
        self.assertEqual(plan["saves"]["archive"]["files"], 4)
        self.assertEqual(plan["saves"]["archive"]["games"], 1)
        self.assertEqual(plan["saves"]["archive"]["states"], 2)
        self.assertEqual(plan["reasons"]["kept_saved"], 0)
        before_saves, before_roms = self.saved_files(), tree(root)
        self.assertEqual(self.saved_files(), sorted(saves + solo))                 # (the preview moved nothing)
        res = self.lib_apply(plan, aside_to=str(aside))
        self.assertEqual(res["saves_archived"]["moved"], 4)
        self.assertEqual(self.saved_files(), solo)
        self.assertEqual(sorted(x for x in tree(aside) if not x.endswith("/") and "_saves" in x),
                         sorted(f"snes/_saves/{s}" for s in saves))
        self.assertTrue((aside / "snes" / "_superseded" / "Zed (USA).sfc").is_file())
        back = self.job("/api/library/undo", {"log": res["undo_log"]})
        self.assertEqual(back["aside_restored"], 5)                                # the ROM and its four save files
        self.assertEqual(tree(root), before_roms)
        self.assertEqual(self.saved_files(), before_saves)
        self.assertEqual([x for x in tree(aside) if not x.endswith("/")], [])

    def test_without_an_archive_folder_they_go_to_a_saves_folder_next_to_the_archived_roms(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        saves = self.save("bsnes", "Zed (USA)")
        self.mode("archive")
        plan = self.lib_plan(root)
        res = self.lib_apply(plan)
        self.assertEqual(res["saves_archived"]["moved"], 4)
        self.assertEqual(self.saved_files(), [])
        self.assertEqual(sorted(x for x in tree(root) if x.startswith("_saves") and not x.endswith("/")),
                         sorted(f"_saves/{s}" for s in saves))
        self.job("/api/library/undo", {"log": res["undo_log"]})
        self.assertEqual(self.saved_files(), saves)
        self.assertEqual(sorted(x for x in tree(root) if not x.endswith("/")), ["Zed (Europe).sfc", "Zed (USA).sfc"])

    def test_a_save_already_at_the_destination_is_never_overwritten(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (USA)", ("{n}.srm",))
        self.mode("archive")
        aside = self.tmp / "archive"
        put(aside / "snes" / "_saves" / "bsnes" / "Zed (USA).srm", b"older archive")
        plan = self.lib_plan(root, aside_to=str(aside))
        self.lib_apply(plan, aside_to=str(aside))
        self.assertEqual((aside / "snes" / "_saves" / "bsnes" / "Zed (USA).srm").read_bytes(), b"older archive")
        self.assertEqual((aside / "snes" / "_saves" / "bsnes" / "Zed (USA) (2).srm").read_bytes(), b"{n}.srm")
        self.assertEqual(self.saved_files(), [])

    def test_while_retroarch_runs_the_saves_stay_and_the_rom_is_archived(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        saves = self.save("bsnes", "Zed (USA)")
        self.mode("archive")
        plan = self.lib_plan(root)
        self.running_mock.return_value = True
        res = self.lib_apply(plan)
        self.assertTrue(res["saves_archived"]["skipped_running"])
        self.assertEqual(self.saved_files(), saves)
        self.assertTrue((root / "_superseded" / "Zed (USA).sfc").is_file())

    def test_a_game_that_is_kept_keeps_its_saves_even_when_a_duplicate_of_it_is_archived(self) -> None:
        root = self.rom_folder("Solo (USA)")
        put(root / "solo copy.sfc", self.rom["Solo (USA)"])                         # the same ROM again, another file name
        solo = self.save("bsnes", "Solo (USA)")
        self.save("bsnes", "solo copy")
        self.mode("archive")
        plan = self.lib_plan(root)
        self.lib_apply(plan)
        self.assertTrue(set(solo) <= set(self.saved_files()))                       # the kept file's saves are still there
        self.assertEqual([x for x in self.saved_files() if "solo copy" in x], [])    # those of the spare copy went with it


class RenamePreview(SavesCase):
    def test_the_preview_counts_what_the_build_then_renames(self) -> None:
        root = self.tmp / "snes"
        put(root / "solo.sfc", self.rom["Solo (USA)"])                              # the DAT calls it Solo (USA)
        put(root / "zed.sfc", self.rom["Zed (Europe)"])
        old = self.save("bsnes", "solo") + self.save("bsnes", "zed", ("{n}.srm", "{n}.state1", "{n}.B1.bin"))
        plan = self.lib_plan(root)
        self.assertTrue(plan["saves"]["follow"])
        self.assertEqual(plan["saves"]["rename"], {"files": 7, "games": 2, "conflicts": 0})
        res = self.lib_apply(plan)
        self.assertEqual(res["saves"]["followed"], plan["saves"]["rename"]["files"])
        self.assertEqual(self.saved_files(), sorted(f.replace("solo", "Solo (USA)").replace("zed", "Zed (Europe)") for f in old))

    def test_a_conflict_is_counted_and_nothing_is_overwritten(self) -> None:
        root = self.tmp / "snes"
        put(root / "solo.sfc", self.rom["Solo (USA)"])
        self.save("bsnes", "solo", ("{n}.srm",))
        self.save("bsnes", "Solo (USA)", ("{n}.srm",))
        plan = self.lib_plan(root)
        self.assertEqual(plan["saves"]["rename"], {"files": 0, "games": 1, "conflicts": 1})

    def test_the_switch_turns_the_renaming_off_everywhere(self) -> None:
        root = self.tmp / "snes"
        put(root / "solo.sfc", self.rom["Solo (USA)"])
        self.save("bsnes", "solo", ("{n}.srm",))
        self.call("POST", "/api/retroarch/follow", {"follow": False})
        plan = self.lib_plan(root)
        self.assertFalse(plan["saves"]["follow"])
        self.assertEqual(plan["saves"]["rename"]["files"], 0)
        self.assertNotIn("saves", self.lib_apply(plan))


class CollectionSinglePass(SavesCase):
    def make(self) -> Path:
        root = self.tmp / "My ROMs"
        put(root / "Dump" / "zed usa.sfc", self.rom["Zed (USA)"])
        put(root / "Dump" / "zed eu.sfc", self.rom["Zed (Europe)"])
        put(root / "Dump" / "solo.sfc", self.rom["Solo (USA)"])
        put(root / "Dump" / "solo.srm", b"a loose save beside its ROM")
        put(root / "Dump" / "run.gba", self.rom["Run (USA)"])
        return root

    def test_archive_mode_moves_rom_and_saves_once_with_one_journal_and_undo_restores_all(self) -> None:
        root = self.make()
        archive = root.with_name(root.name + "-archive")
        saves = self.save("bsnes", "zed usa")                                       # named after the file the ROM has now
        self.save("bsnes", "solo", ("{n}.srm",))
        self.call("POST", "/api/collection/save", {"global": {"saved_games": "archive"}})
        before, before_saves = tree(root), self.saved_files()
        self.scan(root)
        plan = self.job("/api/collection/plan")
        row = next(r for r in plan["systems"] if r["platform"] == SNES)
        self.assertEqual(row["saves_plan"]["archive"]["files"], 4)
        self.assertEqual(row["saves_plan"]["rename"]["files"], 1)                   # solo.srm follows Solo (USA).sfc
        self.assertEqual(tree(root), before)                                         # (a preview moves nothing)
        res = self.job("/api/collection/apply")
        self.assert_clean(res)
        self.assertEqual(res["saves_archived"]["files"], 4)
        self.assertEqual(self.saved_files(), ["bsnes/Solo (USA).srm"])
        self.assertEqual(sorted(x for x in tree(archive) if "_saves" in x and not x.endswith("/")),
                         sorted(f"snes/_saves/{s}" for s in saves))
        self.assertTrue((archive / "_other" / "Dump" / "solo.srm").is_file())       # the loose save is not a ROM: it goes to _other
        self.assertFalse((root / "snes" / "solo.srm").exists())
        self.assert_one_move_per_file(root)
        last = self.call("GET", "/api/collection")["last"]
        self.assertTrue(last["sort"])
        self.undo()
        self.assertEqual(tree(root), before)
        self.assertEqual(self.saved_files(), before_saves)
        self.assertEqual([x for x in tree(archive) if not x.endswith("/")], [])

    def test_keep_mode_keeps_the_game_in_the_collection(self) -> None:
        root = self.make()
        self.save("bsnes", "zed usa", ("{n}.srm",))
        self.scan(root)
        plan = self.job("/api/collection/plan")
        row = next(r for r in plan["systems"] if r["platform"] == SNES)
        self.assertEqual(row["saves_plan"]["kept"], 1)
        res = self.job("/api/collection/apply")
        self.assert_clean(res)
        self.assertEqual(sorted(x for x in tree(root) if x.startswith("snes/") and not x.endswith("/")),
                         ["snes/Solo (USA).sfc", "snes/Zed (Europe).sfc", "snes/Zed (USA).sfc"])
        self.assertEqual(self.saved_files(), ["bsnes/Zed (USA).srm"])

    def test_archive_mode_in_a_copy_build_leaves_the_users_saves_alone(self) -> None:
        root = self.make()
        self.save("bsnes", "zed usa", ("{n}.srm",))
        self.call("POST", "/api/collection/save", {"global": {"saved_games": "archive"}, "place": "elsewhere",
                                                    "dest": str(self.tmp / "clean"), "mode": "copy"})
        self.scan(root)
        plan = self.job("/api/collection/plan")
        row = next(r for r in plan["systems"] if r["platform"] == SNES)
        self.assertTrue(row["saves_plan"]["elsewhere"])
        self.assertEqual(row["saves_plan"]["archive"]["files"], 0)
        self.job("/api/collection/apply")
        self.assertEqual(self.saved_files(), ["bsnes/zed usa.srm"])
        self.assertTrue(os.path.isfile(self.tmp / "My ROMs" / "Dump" / "zed usa.sfc"))
