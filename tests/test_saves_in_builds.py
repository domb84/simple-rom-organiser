"""RetroArch saves are part of the build (Amendment 30): a real server over HTTP, real folders, tiny synthetic No-Intro DATs and
a fake RetroArch install whose saves live in ONE folder, sorted into a sub-folder per core (the user's setup).

The three answers to "games you have saves for": ``keep`` (the default; the game stays), ``archive`` (the game is archived and
its saves go with it) and ``leave`` (today's behaviour: the game is archived, the saves stay where they are)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.servercase import ServerCase, SNES, put, tree  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)

BSNES_INFO = ('display_name = "Nintendo - SNES / SFC (bsnes)"\ncorename = "bsnes"\n'
              'supported_extensions = "smc|sfc|swc|fig|bs|st|gb|gbc|bml"\n')
GENESIS_INFO = ('display_name = "Sega - MS/GG/MD/CD (Genesis Plus GX)"\ncorename = "Genesis Plus GX"\n'
                'supported_extensions = "mdx|md|smd|gen|bin|cue|iso|chd|sms|gg|sg|68k|sgd"\n')
SAVE_NAMES = ("{n}.srm", "{n}.state1", "{n}.state1.png", "{n}.A1.bin")


class SavesCase(ServerCase):
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
        self.call("POST", "/api/library/defaults", {"prefer_saved": False})          # (these tests are about what happens to a game the rules replace)

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
    def setUp(self) -> None:
        super().setUp()
        self.mode("keep")

    def test_a_game_with_saves_is_kept_and_the_preview_says_so(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (USA)")
        plan = self.lib_plan(root)
        self.assertEqual(plan["profile"]["saved_games"], "keep")
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
        self.assertEqual(plan["saves"]["archive"]["states"], 1)                    # (a state's screenshot is moved with it but not counted)
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




class SavesOfGamesYouLack(SavesCase):
    def test_a_save_of_a_game_with_no_file_is_in_the_tree_under_its_game(self) -> None:
        root = self.rom_folder("Zed (USA)")
        self.save("bsnes", "Zed (USA)", ("{n}.srm",))
        self.save("bsnes", "Mix (Europe)", ("{n}.srm", "{n}.state1"))                      # (a game of the database you have no file for)
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        tree = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=saves")
        by = {f["name"]: f for f in tree["files"]}
        self.assertEqual(sorted(by), ["Mix (Europe)", "Zed (USA).sfc"])
        self.assertEqual((by["Mix (Europe)"]["state"], by["Mix (Europe)"]["row"]["saves"]["total"]), ("missing", 2))
        self.assertEqual(by["Zed (USA).sfc"]["row"]["saves"]["saves"], 1)
        self.assertEqual(tree["facets"]["show"]["saves"], 2)
        self.assertEqual(tree["facets"]["show"]["have"], 1)


class LatestReleaseForSaves(SavesCase):
    def test_a_cartridge_save_of_a_game_you_lack_is_shown_on_the_release_your_rules_prefer(self) -> None:
        root = self.rom_folder("Solo (USA)")
        self.save("bsnes", "Mix (USA)", ("{n}.srm",))                                        # (no ROM; Europe comes first by default)
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        tree = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=saves")
        by = {f["name"]: f for f in tree["files"]}
        self.assertEqual(sorted(by), ["Mix (Europe)"])
        self.assertEqual((by["Mix (Europe)"]["row"]["save_for"], by["Mix (Europe)"]["row"]["saves"]["saves"]), ("Mix (USA)", 1))

    def test_a_save_that_only_works_in_its_region_stays_on_its_region(self) -> None:
        from romorg import savecarry
        root = self.rom_folder("Solo (USA)")
        self.save("bsnes", "Mix (USA)", ("{n}.srm",))
        with mock.patch.object(savecarry, "ANY_RELEASE", frozenset()), \
                mock.patch.object(savecarry, "SAME_REGION", frozenset({"Super Nintendo Entertainment System"})):
            self.job("/api/scan", {"path": str(root), "platform": SNES})
            tree = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=saves")
        by = {f["name"]: f for f in tree["files"]}
        self.assertEqual(sorted(by), ["Mix (USA)"])
        self.assertNotIn("save_for", by["Mix (USA)"]["row"])


class SavesGoToTheReleaseYouHave(SavesCase):
    def test_a_save_known_by_its_title_goes_to_the_rom_you_have_before_a_release_you_lack(self) -> None:
        root = self.rom_folder("Zed (USA)")                                              # (Zed (Europe) is in the database, not here)
        self.save("bsnes", "Zed (Japan) - Copy", ("{n}.srm",))
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        tree = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=saves")
        by = {f["name"]: f for f in tree["files"]}
        self.assertEqual(sorted(by), ["Zed (USA).sfc"])
        self.assertEqual((by["Zed (USA).sfc"]["state"], by["Zed (USA).sfc"]["row"]["saves"]["saves"]), ("same", 1))


class CarryToTheReleaseYouHave(SavesCase):
    """``carry``: a save of a revision you have no file for is copied to the revision you have (same region), and both rows say so."""
    GAMES = {"snes": ["Zed (USA)", "Zed (USA) (Rev 1)"], "gba": ["Run (USA)"], "gb": ["Tetra (World)"]}

    def test_the_save_of_an_older_revision_is_copied_to_the_one_you_have(self) -> None:
        root = self.rom_folder("Zed (USA) (Rev 1)")
        old = self.save("bsnes", "Zed (USA)")
        self.mode("carry")
        plan = self.lib_plan(root)
        self.assertEqual(plan["saves"]["carry"]["games"], 1)
        row = [r for r in plan["items"] if r["from"].endswith("Zed (USA) (Rev 1).sfc")][0]
        self.assertEqual(row["saves"]["effect"], "receive")
        self.assertEqual(self.saved_files(), old)                                                  # (the preview copied nothing)
        res = self.lib_apply(plan)
        self.assertEqual(res["saves"]["carried"], 2)
        self.assertTrue((self.saves / "bsnes" / "Zed (USA) (Rev 1).srm").is_file())
        self.assertTrue((self.saves / "bsnes" / "Zed (USA).srm").is_file())                        # (the old save stays)
        self.assertTrue(res["saves"]["journal"])                                                   # (journalled: Retroarch's Undo removes the copies)

    def test_the_default_choice_copies_the_save_to_the_version_you_have(self) -> None:
        root = self.rom_folder("Zed (USA) (Rev 1)")
        self.save("bsnes", "Zed (USA)")
        plan = self.lib_plan(root)                                                                 # (no choice made: the default)
        self.assertEqual(plan["profile"]["saved_games"], "convert")
        self.assertEqual(plan["saves"]["carry"]["games"], 1)
        self.lib_apply(plan)
        self.assertTrue((self.saves / "bsnes" / "Zed (USA) (Rev 1).srm").is_file())
        self.assertTrue((self.saves / "bsnes" / "Zed (USA).srm").is_file())

    def test_with_another_choice_nothing_is_copied(self) -> None:
        root = self.rom_folder("Zed (USA) (Rev 1)")
        self.save("bsnes", "Zed (USA)")
        self.mode("keep")
        plan = self.lib_plan(root)
        self.assertEqual(plan["saves"]["carry"]["games"], 0)


class CarryToAnotherRegion(SavesCase):
    """The option "Copy saves to the version you have" across systems: a cartridge save works in every release, a disc system's in its region only."""
    GAMES = {"snes": ["Zed (USA)", "Zed (Europe)"], "gba": ["Run (USA)"], "gb": ["Tetra (World)"]}

    def games_carried(self, **patches: Any) -> int:
        from romorg import savecarry
        root = self.rom_folder("Zed (USA)")
        self.save("bsnes", "Zed (Europe)")
        with mock.patch.multiple(savecarry, **(patches or {"ANY_RELEASE": savecarry.ANY_RELEASE})):
            return self.lib_plan(root)["saves"]["carry"]["games"]

    def test_a_cartridge_save_of_another_region_is_copied(self) -> None:
        self.assertEqual(self.games_carried(), 1)

    def test_a_save_of_a_disc_system_is_copied_within_its_region_only(self) -> None:
        self.assertEqual(self.games_carried(SAME_REGION=frozenset({SNES}), ANY_RELEASE=frozenset()), 0)


class CarryMode(SavesCase):
    """``carry``: the game a better version replaces goes, and its save games (never its states) are copied to the new ROM's name."""

    def test_the_save_is_copied_to_the_better_version_the_old_rom_goes_and_undo_removes_the_copy(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        old = self.save("bsnes", "Zed (USA)")
        self.mode("carry")
        plan = self.lib_plan(root)
        self.assertEqual((plan["reasons"]["kept_saved"], plan["reasons"]["superseded"]), (0, 1))
        self.assertEqual(plan["saves"]["carry"], {"files": 2, "games": 1, "conflicts": 0, "converted": 0})        # the .srm and the .A1.bin; not the state or its picture
        row = [r for r in plan["items"] if r["from"].endswith("Zed (USA).sfc")][0]
        self.assertEqual(row["saves"]["effect"], "carry")
        new = [r for r in plan["items"] if r["from"].endswith("Zed (Europe).sfc")][0]
        self.assertEqual((new["saves"]["effect"], new["saves"]["saves"]), ("receive", 2))             # (the version that gets them says so too)
        self.assertEqual(self.saved_files(), old)                                                   # (the preview copied nothing)
        shown = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=")
        self.assertEqual({f["name"]: (f["row"] or {}).get("saves", {}).get("effect") for f in shown["files"]}["Zed (Europe).sfc"], "receive")
        res = self.lib_apply(plan)
        self.assertEqual(res["saves"]["carried"], 2)
        self.assertEqual(self.saved_files(), sorted(old + ["bsnes/Zed (Europe).srm", "bsnes/Zed (Europe).A1.bin"]))
        self.assertEqual((self.saves / "bsnes" / "Zed (Europe).srm").read_bytes(), (self.saves / "bsnes" / "Zed (USA).srm").read_bytes())
        self.assertEqual(sorted(x for x in tree(root) if not x.endswith("/")), sorted(["Zed (Europe).sfc", "_superseded/Zed (USA).sfc"]))
        self.job("/api/library/undo", {"log": res["undo_log"]})
        self.assertEqual(self.saved_files(), old)
        self.assertTrue((root / "Zed (USA).sfc").is_file())

    def test_a_better_version_that_has_a_save_of_its_own_is_never_overwritten(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (USA)", ("{n}.srm",))
        self.save("bsnes", "Zed (Europe)", ("{n}.srm",))
        self.mode("carry")
        plan = self.lib_plan(root)
        self.assertEqual(plan["saves"]["carry"], {"files": 0, "games": 1, "conflicts": 1, "converted": 0})
        self.lib_apply(plan)
        self.assertEqual((self.saves / "bsnes" / "Zed (Europe).srm").read_bytes(), b"{n}.srm")

    def test_a_game_with_nothing_to_carry_to_stays_as_with_keep(self) -> None:
        root = self.rom_folder("Solo (USA)")
        self.save("bsnes", "Solo (USA)")
        self.mode("carry")
        plan = self.lib_plan(root)
        self.assertEqual(plan["saves"]["carry"]["files"], 0)
        self.assertEqual(plan["actionable"], 0)

    def test_it_can_be_chosen_for_a_single_game(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (USA)", ("{n}.srm",))
        self.mode("keep")
        plan = self.lib_plan(root)
        self.assertEqual(plan["reasons"]["kept_saved"], 1)                                           # (keep both)
        row = [r for r in plan["items"] if r["from"].endswith("Zed (USA).sfc")][0]
        self.call("POST", "/api/library/saved", {"platform": SNES, "games": [{"dat": row["saves"]["dat"], "game": row["saves"]["game"]}], "choice": "carry"})
        plan = self.lib_plan(root, refresh=True)
        self.assertEqual((plan["reasons"]["kept_saved"], plan["saves"]["carry"]["files"]), (0, 1))


def memory_card(*games: str) -> bytes:
    """A PlayStation memory card image: the frames of the games given (the product code and the name) in use, the rest free."""
    import struct
    card = bytearray(131072)
    card[0:2] = b"MC"
    for i in range(1, 16):
        card[i * 128] = 0xA0
    for n, name in enumerate(games, 1):
        card[n * 128] = 0x51
        struct.pack_into("<I", card, n * 128 + 4, 8192)
        card[n * 128 + 10:n * 128 + 10 + len(name)] = name.encode()
    return bytes(card)


class EmptyCards(SavesCase):
    def test_a_memory_card_with_no_game_on_it_is_no_save_and_one_with_a_game_is(self) -> None:
        root = self.rom_folder("Zed (USA)", "Solo (USA)")
        put(self.saves / "bsnes" / "Zed (USA).srm", memory_card())                                    # (what a core makes when nothing was ever saved)
        put(self.saves / "bsnes" / "Solo (USA).srm", memory_card("BASCUS-94455GT2"))
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        counts = {s["name"]: s["saves"] for s in self.call("GET", "/api/scan/results?kind=saves")["items"]}
        self.assertEqual(counts.get("Solo (USA)"), 1)
        self.assertNotIn("Zed (USA)", counts)                                                          # (nothing on it: not listed as a save)


class SavesBehaviourTable(unittest.TestCase):
    """The table of the Saves page is the rules of ``savecarry``, for every system the app has."""

    def test_every_system_is_in_exactly_one_row_and_the_rows_agree_with_the_rules(self) -> None:
        from romorg import platforms, savecarry
        rows = savecarry.behaviour()
        listed = [name for r in rows for name in r["systems"]]
        self.assertEqual(sorted(listed), sorted(p.name for p in platforms.list_platforms()))
        by = {name: r for r in rows for name in r["systems"]}
        for name in savecarry.ANY_RELEASE:                                   # (cartridges: a copy in every region)
            self.assertEqual((by[name]["follows"], by[name]["copies"]), ("Yes", "Yes, in any region"), name)
        for name in savecarry.CONVERTIBLE:                                   # (the two systems whose saves are converted say so)
            self.assertTrue(by[name]["converts"].startswith("Yes"), name)
        for name in ("Sega Dreamcast", "Nintendo GameCube", "Nintendo Wii", "Nintendo Wii U", "Nintendo Switch", "Commodore Amiga", "Commodore Amiga - WHDLoad"):
            self.assertEqual((by[name]["copies"], by[name]["converts"]), ("No", "No"), name)
        for name in savecarry.SERIAL_SAVES:                                  # (a save named by the game's code needs no new name when the code is the same)
            self.assertIn("same code", by[name]["copies"])
        self.assertFalse(any("state" in text.lower() for r in rows for text in (r["kept_by"], r["follows"], r["copies"], r["converts"])))


class WhichSavesWork(unittest.TestCase):
    def test_cartridge_saves_work_everywhere_and_disc_saves_only_in_their_region(self) -> None:
        from romorg import savecarry
        self.assertTrue(savecarry.works("Nintendo 64", ["USA"], ["Europe"]))
        self.assertTrue(savecarry.works("Super Nintendo Entertainment System", ["Japan"], ["USA"]))
        self.assertFalse(savecarry.works("Sony PlayStation", ["USA"], ["Europe"]))
        self.assertTrue(savecarry.works("Sony PlayStation", ["USA"], ["USA"]))
        self.assertFalse(savecarry.works("Sony PlayStation", [], ["USA"]))
        self.assertFalse(savecarry.works("Nintendo Switch", ["USA"], ["USA"]))
        self.assertFalse(savecarry.works("Commodore Amiga", [], []))


class ConvertMode(SavesCase):
    """``convert``: a PlayStation save is rewritten for the other region's disc. The harness plays the SNES with PlayStation cards and
    discs whose product codes are made up (the real card format and the real disc reading are tested apart)."""

    CODES = {"Zed (USA)": "SLUS-00001", "Zed (Europe)": "SLES-00002"}

    def setUp(self) -> None:
        super().setUp()
        from romorg import pcsx2, savecarry
        from tests.test_memcards import ps1_card
        self.card = ps1_card([("BASLUS-00001ZED", "ZED", 1), ("BASLUS-00099OTHER", "OTHER", 1)])
        for patch in (mock.patch.object(savecarry, "CONVERTIBLE", frozenset({"Super Nintendo Entertainment System"})),
                      mock.patch.object(savecarry, "CONVERTIBLE_DATS", frozenset({"Nintendo - Super Nintendo Entertainment System"})),
                      mock.patch.object(pcsx2, "read_serial", lambda path, psx=False: next((c for n, c in self.CODES.items() if n in str(path)), ""))):
            patch.start()
            self.addCleanup(patch.stop)
        self.mode("convert")

    def put_card(self, name: str = "Zed (USA)") -> Path:
        path = self.saves / "bsnes" / f"{name}.srm"
        put(path, self.card)
        return path

    def test_the_save_is_rewritten_for_the_other_region_and_only_that_game_s_entry_changes(self) -> None:
        from romorg import memcards, savecarry
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        old = self.put_card()
        plan = self.lib_plan(root)
        self.assertEqual(plan["saves"]["carry"], {"files": 1, "games": 1, "conflicts": 0, "converted": 1})
        res = self.lib_apply(plan)
        self.assertEqual((res["saves"]["carried"], res["saves"]["converted"]), (1, 1))
        new = self.saves / "bsnes" / "Zed (Europe).srm"
        self.assertEqual(old.read_bytes(), self.card)                                              # the original is untouched
        got = memcards.read(new)
        self.assertEqual([e.name for e in got.entries], ["BESLES-00002ZED", "BASLUS-00099OTHER"])   # (the other game's save is not)
        data = new.read_bytes()
        check = 0
        for b in data[128:128 + 127]:                                                                # the converted frame's checksum is right
            check ^= b
        self.assertEqual(check, data[128 + 127])
        self.assertEqual(sum(1 for a, b in zip(self.card, data) if a != b), 4)                       # A->E, U->E, the last digit, the checksum: nothing else
        self.job("/api/library/undo", {"log": res["undo_log"]})
        self.assertFalse(new.exists())

    def test_a_blank_card_or_a_card_without_the_game_is_not_copied(self) -> None:
        from tests.test_memcards import ps1_card
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        put(self.saves / "bsnes" / "Zed (USA).srm", ps1_card([("BASLUS-00099OTHER", "OTHER", 1)]))
        plan = self.lib_plan(root)
        self.assertEqual(plan["saves"]["carry"]["files"], 0)
        self.lib_apply(plan)
        self.assertFalse((self.saves / "bsnes" / "Zed (Europe).srm").exists())

    def test_without_both_product_codes_both_roms_stay(self) -> None:
        self.CODES = {"Zed (USA)": "SLUS-00001"}                                                    # (the European disc cannot be read)
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.put_card()
        plan = self.lib_plan(root)
        self.assertEqual((plan["reasons"]["kept_saved"], plan["reasons"]["superseded"]), (1, 0))

    def test_the_same_product_code_is_a_plain_copy(self) -> None:
        self.CODES = {"Zed (USA)": "SLUS-00001", "Zed (Europe)": "SLUS-00001"}
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.put_card()
        plan = self.lib_plan(root)
        self.assertEqual((plan["saves"]["carry"]["files"], plan["saves"]["carry"]["converted"]), (1, 0))
        self.lib_apply(plan)
        self.assertEqual((self.saves / "bsnes" / "Zed (Europe).srm").read_bytes(), self.card)


class ConvertingACard(unittest.TestCase):
    def test_the_name_and_the_checksum_of_the_game_s_entry_change_and_nothing_else(self) -> None:
        from romorg import memcards, savecarry
        from tests.test_memcards import ps1_card
        card = ps1_card([("BASLUS-00520FIFA98", "FIFA", 2), ("BESCES-00967CRASH2", "CRASH", 1)])
        data, n = savecarry.convert_ps1_card(card, "SLUS-00520", "SLES-00555")
        self.assertEqual(n, 1)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.srm"
            path.write_bytes(data)
            self.assertEqual([e.name for e in memcards.read(path).entries], ["BESLES-00555FIFA98", "BESCES-00967CRASH2"])
        for i in (1, 2):                                                                              # the directory frames check out
            check = 0
            for b in data[i * 128:i * 128 + 127]:
                check ^= b
            if i == 1:
                self.assertEqual(check, data[i * 128 + 127])
        self.assertEqual(data[2 * 128:3 * 128], card[2 * 128:3 * 128])                               # the other save's frame is the same bytes
        self.assertEqual(data[8192:], card[8192:])                                                    # and so are the save blocks

    def test_a_wrapped_card_keeps_its_header_and_a_wrong_input_gives_none(self) -> None:
        from romorg import savecarry
        from tests.test_memcards import ps1_card
        card = ps1_card([("BASLUS-00520FIFA98", "FIFA", 1)], header=b"\x01" * 128)
        data, n = savecarry.convert_ps1_card(card, "SLUS-00520", "SCPS-10001")
        self.assertEqual((n, data[:128], data[128 + 128 + 10:128 + 128 + 14]), (1, b"\x01" * 128, b"BISC"))            # BI = Japan
        self.assertIsNone(savecarry.convert_ps1_card(card, "SLUS-99999", "SLES-00555"))                 # no such game on the card
        self.assertIsNone(savecarry.convert_ps1_card(card, "SLUS-00520", "XXXX-00555"))                 # a code that says no region
        self.assertIsNone(savecarry.convert_ps1_card(b"not a card", "SLUS-00520", "SLES-00555"))
        self.assertEqual(savecarry.region_letter("SCES-00967"), "E")
        self.assertEqual(savecarry.region_letter("SLPM-86001"), "I")


class PlayStationDiscCode(unittest.TestCase):
    def test_the_product_code_of_a_playstation_disc_is_read_from_its_boot_line(self) -> None:
        from romorg import pcsx2
        self.assertEqual(pcsx2.serial_from_cnf("BOOT = cdrom:\\SLUS_005.20;1\r\nTCB = 4\r\nEVENT = 10\r\nSTACK = 801FFF00\r\n", ("BOOT", "BOOT2")), "SLUS-00520")
        self.assertEqual(pcsx2.serial_from_cnf("BOOT = cdrom:\\SLUS_005.20;1"), "")              # (not the PlayStation 2's: it plays these discs too)
        self.assertEqual(pcsx2.serial_from_cnf("BOOT = cdrom:SCES_009.67;1", ("BOOT",)), "SCES-00967")
        self.assertEqual(pcsx2.serial_from_cnf("BOOT2 = cdrom0:\\SLUS_209.46;1"), "SLUS-20946")            # (the PlayStation 2's line still reads)
        self.assertEqual(pcsx2.serial_from_cnf("VMODE = NTSC"), "")


class Ps2Names(unittest.TestCase):
    def test_a_save_folder_is_planned_under_the_new_code_without_pcsx2s_own_files(self) -> None:
        import tempfile
        from romorg import ps2save
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "BASLUS-20946GTA50000"
            (folder / "sub").mkdir(parents=True)
            for rel in ("BASLUS-20946GTA50000", "icon.sys", "sub/BASLUS-20946x", "_pcsx2_index"):
                (folder / rel).write_bytes(b"x")
            name, files = ps2save.folder_plan(folder, "SLUS-20946", "SLES-52005")
            self.assertEqual(name, "BESLES-52005GTA50000")
            self.assertEqual(sorted(rel for _f, rel in files), ["BESLES-52005GTA50000", "icon.sys", "sub/BESLES-52005x"])
            self.assertIsNone(ps2save.folder_plan(folder, "SLUS-99999", "SLES-52005"))           # not a save of that game
            self.assertIsNone(ps2save.folder_plan(folder, "SLUS-20946", "XXXX-52005"))           # a code that says no region

    def test_a_card_image_is_rewritten_with_its_ecc_and_reads_back(self) -> None:
        import tempfile
        from romorg import pcsx2, ps2save
        from tests.test_nintendo import ps2_card_image
        tree = {"BASLUS-20946GTA50000": {"BASLUS-20946GTA50000": b"a" * 3000, "sub": {"x": b"b" * 10}}, "BADATA-SYSTEM": {"icon.sys": b"i" * 500}}
        for ecc in (False, True):
            image = ps2_card_image(tree, ecc=ecc)
            data, n = ps2save.convert_image(image, "SLUS-20946", "SLES-52005")
            self.assertEqual((n, len(data)), (1, len(image)))
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "c.ps2"
                path.write_bytes(data)
                got = {s.name: (s.serial, s.files, s.bytes) for s in pcsx2.read_card_image(path)}
            self.assertEqual(got, {"BESLES-52005GTA50000": ("SLES-52005", 2, 3010), "BADATA-SYSTEM": ("", 1, 500)}, ecc)
            if ecc:                                                                                  # every changed page's spare bytes are its ECC
                changed = [p for p in range(len(image) // 528) if image[p * 528:p * 528 + 528] != data[p * 528:p * 528 + 528]]
                self.assertTrue(changed)
                for p in changed:
                    self.assertEqual(data[p * 528 + 512:p * 528 + 528], ps2save.page_spare(data[p * 528:p * 528 + 512]))
            else:
                self.assertEqual(len([1 for a, b in zip(image, data) if a != b]) > 0, True)
        self.assertIsNone(ps2save.convert_image(image, "SLUS-20946", "SLUS-20946"))               # the same code
        self.assertIsNone(ps2save.convert_image(image, "SLUS-11111", "SLES-52005"))               # no such save
        self.assertIsNone(ps2save.convert_image(b"nothing" * 100, "SLUS-20946", "SLES-52005"))
        both = ps2_card_image({"BASLUS-20946A": {"f": b"1"}, "BESLES-52005A": {"f": b"2"}})
        self.assertIsNone(ps2save.convert_image(both, "SLUS-20946", "SLES-52005"))                 # the new name is taken
        other = ps2_card_image({"BASLUS-20946A": {"f": b"1"}, "BESLES-52005B": {"f": b"2"}})
        self.assertIsNotNone(ps2save.convert_image(other, "SLUS-20946", "SLES-52005"))             # another save of that game is no clash

    def test_the_ecc_of_a_chunk_changes_with_any_bit(self) -> None:
        from romorg import ps2save
        self.assertEqual(ps2save.ecc(bytes(128)), bytes((0x77, 0x7F, 0x7F)))                       # (an all-zero chunk: what a real card shows)
        chunk = bytearray(range(128))
        base = ps2save.ecc(bytes(chunk))
        for i in (0, 5, 77, 127):
            for bit in (1, 0x80):
                other = bytearray(chunk)
                other[i] ^= bit
                self.assertNotEqual(ps2save.ecc(bytes(other)), base)


class EmulatorSettingsKey(SavesCase):
    def test_an_old_config_with_the_nintendo_key_is_moved_to_emulators(self) -> None:
        from romorg import paths
        folder = self.tmp / "wii-games"
        folder.mkdir()
        paths.update_config(lambda c: c.__setitem__("nintendo", {"wii": {"games": str(folder), "data": ""}, "ps2": {"games": "", "data": str(self.tmp)}}))
        self.srv.app._rename_emulator_settings()
        cfg = paths.load_config() if hasattr(paths, "load_config") else __import__("json").loads(paths.config_path().read_text())
        self.assertNotIn("nintendo", cfg)
        self.assertEqual(cfg["emulators"]["wii"]["games"], str(folder))
        self.assertEqual(cfg["emulators"]["ps2"]["data"], str(self.tmp))
        # a config that has both keeps the new one, and the old key is gone
        paths.update_config(lambda c: c.update({"nintendo": {"wii": {"games": "old", "data": ""}}}))
        self.srv.app._rename_emulator_settings()
        cfg = __import__("json").loads(paths.config_path().read_text())
        self.assertNotIn("nintendo", cfg)
        self.assertEqual(cfg["emulators"]["wii"]["games"], str(folder))
        # saving through the page writes the new key
        self.call("POST", "/api/emulators/config", {"source": "dolphin", "folder": str(self.tmp)})
        cfg = __import__("json").loads(paths.config_path().read_text())
        self.assertIn("emulators", cfg)
        self.assertNotIn("nintendo", cfg)


class TitlesOfSaves(unittest.TestCase):
    def test_titles_are_compared_without_articles_marks_accents_or_a_second_spelling(self) -> None:
        from romorg.server import App
        same = [("The Legend of Zelda™: Breath of the Wild", "Legend of Zelda, The - Breath of the Wild"), ("Luigi’s Mansion™ 3", "Luigi's Mansion 3"),
                ("Ōkami", "Okami")]
        for a, b in same:
            self.assertEqual(App._fold_title(a), App._fold_title(b), a)
        self.assertNotEqual(App._fold_title("Okami"), App._fold_title("Ookami"))
        self.assertEqual(App._loose_title("Okami"), App._loose_title("Ookami"))                  # (only when the exact title finds nothing)

    def test_a_disc_or_a_mode_is_part_of_the_edition_a_revision_or_a_re_release_is_not(self) -> None:
        from romorg.server import App
        self.assertNotEqual(App._edition("Gran Turismo 2 (USA) (Arcade Mode) (Rev 1)"), App._edition("Gran Turismo 2 (USA) (Simulation Mode) (Rev 2)"))
        self.assertNotEqual(App._edition("Driver 2 (USA) (Disc 1)"), App._edition("Driver 2 (USA) (Disc 2)"))
        self.assertEqual(App._edition("Driver 2 (USA) (Disc 1)"), App._edition("Driver 2 (USA) (Disc 1) (Rev 1)"))
        self.assertEqual(App._edition("Dark Cloud 2 (USA)"), App._edition("Dark Cloud 2 (USA) (Greatest Hits)"))


class SavesOfAnotherRelease(SavesCase):
    def test_a_save_of_a_release_you_lack_joins_the_one_rom_of_its_title_your_rules_prefer(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (Japan) - Copy", ("{n}.srm",))                             # (unmatched; read loosely it is Zed)
        self.mode("leave")
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        tree = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=saves")
        withsaves = [f for f in tree["files"] if (f["row"] or {}).get("saves")]
        self.assertEqual([f["name"] for f in withsaves], ["Zed (Europe).sfc"])            # (one ROM, not both: Europe comes first by default)
        self.assertEqual(withsaves[0]["row"]["saves"]["saves"], 1)
        self.assertEqual(tree["facets"]["show"]["saves"], 1)


class DreamcastProductNumbers(unittest.TestCase):
    """Flycast names a game's own memory cards after the disc's product number; the disc's header says which disc that is."""

    @staticmethod
    def header(code: str, raw: bool = True) -> bytes:
        ip = bytearray(2048)
        ip[0:16] = b"SEGA SEGAKATANA "
        ip[0x40:0x40 + len(code)] = code.encode().ljust(10)[:10]
        ip[0x80:0x80 + 4] = b"GAME"
        return bytes(16) + bytes(ip) + bytes(288) if raw else bytes(ip)

    def test_the_number_is_read_from_a_gdi_and_its_forms(self) -> None:
        import tempfile
        from romorg import dcdisc
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "track01.bin").write_bytes(self.header("HDR-0128"))
            (d / "track02.raw").write_bytes(bytes(2352))
            (d / "track03.bin").write_bytes(self.header("HDR-0128"))
            (d / "game.gdi").write_text("3\n1 0 4 2352 track01.bin 0\n2 600 0 2352 track02.raw 0\n3 45000 4 2352 track03.bin 0\n")
            self.assertEqual(dcdisc.read_product(d / "game.gdi"), "HDR-0128")
            (d / "none.gdi").write_text("1\n1 0 4 2352 nothing.bin 0\n")
            self.assertEqual(dcdisc.read_product(d / "none.gdi"), "")
        self.assertEqual(dcdisc.codes_of("T40509D 50"), {"T40509D 50", "T40509D50", "T40509D"})
        self.assertEqual(dcdisc._from_sector(self.header("MK-51000", raw=False)), "MK-51000")

    def test_a_card_named_by_the_number_is_the_save_of_the_disc_you_have(self) -> None:
        import tempfile
        from types import SimpleNamespace
        from romorg import saveindex, server
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Jet").mkdir()
            (root / "Jet" / "Jet.gdi").write_text("1\n1 0 4 2352 track01.bin 0\n")
            (root / "Jet" / "track01.bin").write_bytes(self.header("HDR-0128"))
            app = server.App.__new__(server.App)
            games = [({"name": "De La Jet Set Radio (Japan)", "dat": "Sega - Dreamcast", "title": "De La Jet Set Radio", "have": True,
                       "files": ["Jet/Jet.gdi"]}, "")]
            app._kind_rows = lambda state, kind: games
            state = SimpleNamespace(platform=SimpleNamespace(name="Sega Dreamcast"), root=root)
            report = saveindex.SaveReport(per_core=True, install="RetroArch")
            card = saveindex.SaveSet("HDR-0128", "hdr-0128", saves=4)
            other = saveindex.SaveSet("XYZ-9999", "xyz-9999", saves=1)
            report.sets = {card.key: card, other.key: other}
            app._match_product_codes(state, report)
            self.assertEqual((card.match, card.game, card.title), (saveindex.ROM, "De La Jet Set Radio (Japan)", "De La Jet Set Radio"))
            self.assertEqual(other.match, saveindex.NONE)                                        # (a number no disc you have carries)


class TreeCountsMatchTheTiles(SavesCase):
    """The Files tree counts games as the tiles do (found comparing the two on real folders)."""

    def tree(self) -> dict:
        return self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=")

    def test_a_zip_the_build_cannot_place_is_still_a_game_you_have(self) -> None:
        import time
        import zipfile
        root = self.rom_folder("Solo (USA)")
        with zipfile.ZipFile(root / "Zed pack.zip", "w") as zf:                     # (one member is a game, the other nothing: the build leaves it)
            zf.writestr("Zed (USA).sfc", self.rom["Zed (USA)"])
            zf.writestr("readme.txt", "hello")
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        self.lib_plan(root)
        show = self.tree()["facets"]["show"]
        self.assertEqual(show["have"], 2)                                                 # (the zip is a game you have)
        t = {}
        for _ in range(100):
            t = self.call("GET", "/api/library/totals?platform=Super%20Nintendo%20Entertainment%20System")
            if t.get("have_games") is not None and not t.get("calculating"):
                break
            time.sleep(0.1)
        self.assertEqual(t.get("have_games"), 2)                                          # (and the tile says the same)

    def test_a_game_the_rules_keep_again_comes_back_from_excluded_not_to_the_archive(self) -> None:
        root = self.rom_folder("Solo (USA)")
        put(root / "_excluded" / "Zed (USA).sfc", self.rom["Zed (USA)"])              # (set aside by an earlier build, kept by the rules now)
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        self.lib_plan(root)
        tree = self.tree()
        names = {f["name"]: f["state"] for f in tree["files"]}
        self.assertEqual(names.get("Zed (USA).sfc"), "moved")
        self.assertEqual(tree["facets"]["show"]["have"], 2)
        archive = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=&side=archive")
        self.assertEqual(archive["sides"]["archive"], 0)

    def test_a_release_you_lack_listed_for_its_saves_is_not_missing(self) -> None:
        import time
        root = self.rom_folder("Solo (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (USA)")                                                    # (you have the release the rules want, the saves are of another)
        no = mock.patch("romorg.savecarry.works", return_value=False)                      # (... that does not work in yours)
        no.start()
        self.addCleanup(no.stop)
        self.job("/api/scan", {"path": str(root), "platform": SNES})
        self.lib_plan(root)
        t = {}
        for _ in range(100):
            t = self.call("GET", "/api/library/totals?platform=Super%20Nintendo%20Entertainment%20System")
            if t.get("missing_games") is not None and not t.get("calculating"):
                break
            time.sleep(0.1)
        tree = self.tree()
        self.assertIn("Zed (USA)", [f["name"] for f in tree["files"]])                     # (listed, with its saves)
        self.assertEqual(tree["facets"]["show"]["missing"], t["missing_games"])
        missing = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=missing")
        self.assertNotIn("Zed (USA)", [f["name"] for f in missing["files"]])


class KeptForSavesCountOnTheirOwn(SavesCase):
    def test_a_game_kept_only_for_its_saves_is_its_own_count_and_in_no_other_filter(self) -> None:
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (USA)")
        self.mode("keep")                                                                  # (the rules prefer Europe; the saves keep the USA ROM)
        plan = self.lib_plan(root)
        self.assertEqual(plan["reasons"]["kept_saved"], 1)
        tree = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=")
        show = tree["facets"]["show"]
        self.assertEqual((show["have"], show["kept_saves"]), (1, 1))
        mine = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=have")
        self.assertEqual(mine["facets"]["region"], {"Europe": 1})                         # (the kept-for-saves USA ROM counts in no other filter)
        kept = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=kept_saves")
        self.assertEqual([f["name"] for f in kept["files"]], ["Zed (USA).sfc"])
        row = {f["name"]: f for f in tree["files"]}["Zed (USA).sfc"]
        self.assertEqual((row["saved_keep"], row["saved_why"]), (True, "you keep Zed (Europe)"))          # (in place, with why)
        hidden = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=&hide_saved=1")
        self.assertNotIn("Zed (USA).sfc", [f["name"] for f in hidden["files"]])
        self.assertEqual(hidden["facets"]["show"]["kept_saves"], 1)                         # (the switch still says how many)

    def test_with_the_default_the_release_with_saves_is_kept_and_nothing_is_kept_for_saves(self) -> None:
        self.call("POST", "/api/library/defaults", {"prefer_saved": True})
        root = self.rom_folder("Zed (USA)", "Zed (Europe)")
        self.save("bsnes", "Zed (USA)")
        self.mode("keep")
        plan = self.lib_plan(root)
        rows = {r["from"]: r for r in plan["items"]}
        self.assertEqual((rows["Zed (USA).sfc"]["status"], rows["Zed (Europe).sfc"]["dest"]), ("ok", "_superseded"))
        show = self.call("GET", "/api/tree?platform=Super%20Nintendo%20Entertainment%20System&show=")["facets"]["show"]
        self.assertEqual((show["have"], show["kept_saves"]), (1, 0))
