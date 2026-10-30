"""One release of a game on the Amiga (``library._one_release``): its CD32 disc, else the version a WHDLoad install boots from, else the
best original (SPS) or floppy set; the others are superseded. Only with "One version per game" and "Best variant"."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from romorg import library, whdready
from romorg.library import KEEP, SUPERSEDED

from tests.test_library import (AMIGA, AMIGA_DP, SPS_DAT, acts, amiga_items, cd_items, sps_items, setUpModule, tearDownModule)  # noqa: F401  (the no-WHDLoad-list patch)


def kept(items, sel) -> list[str]:
    return sorted({it.rom.game for it in items if sel.decisions[it.key].action == KEEP})


def release(game: str, key: int, files: tuple = ("Disk1", "Disk2")) -> list:
    return sps_items(game, list(files), start=key)


IPF_DAT = "Commodore Amiga - Games - [Unofficial IPF]"


def ipf_items(names: list[str], start: int) -> list:
    """The Unofficial IPF DAT lists each disk of a game on its own."""
    from pathlib import Path
    from romorg.datfile import Rom
    return [library.Item(key=start + i, dat=IPF_DAT, rom=Rom(name=n + ".ipf", size=1, crc="", md5="", sha1="", game=n, dat=IPF_DAT),
                         style="tosec", path=Path(n + ".ipf"), member=None) for i, n in enumerate(names)]


class OneReleaseOfAGame(unittest.TestCase):
    def select(self, items, profile=AMIGA_DP):
        return library.select(items, profile, AMIGA)

    def test_the_newest_version_of_the_originals_stays(self) -> None:
        items = release("Falcon v1.00 (1989)(Mirrorsoft)[0368]", 10) + release("Falcon v1.1 (1989)(Mirrorsoft)[1754]", 20)
        sel = self.select(items)
        self.assertEqual(kept(items, sel), ["Falcon v1.1 (1989)(Mirrorsoft)[1754]"])
        self.assertEqual(sel.decisions[10].action, SUPERSEDED)
        self.assertIn("one release of the game", sel.decisions[10].reason)

    def test_a_compilation_a_budget_copy_and_an_alternate_dump_give_way_to_the_original(self) -> None:
        plain = "Lemmings (1991)(Psygnosis)[0132]"
        others = ["Lemmings (1991)(Psygnosis)[Book Club][2238]", "Lemmings (1991)(Psygnosis)[compilation Commodore Promotion Pack][0270]",
                  "Lemmings (1991)(Psygnosis)[a][1977]", "Lemmings (1991)(Psygnosis)[budget][1511]"]
        items = release(plain, 10)
        for n, name in enumerate(others):
            items += release(name, 20 + 10 * n)
        self.assertEqual(kept(items, self.select(items)), [plain])

    def test_publishers_of_one_game_are_one_game(self) -> None:
        items = release("Millennium 2.2 (1989)(Electric Dreams)[0537]", 10) + release("Millennium 2.2 (19xx)(boeder)[budget][1511]", 20)
        self.assertEqual(kept(items, self.select(items)), ["Millennium 2.2 (1989)(Electric Dreams)[0537]"])

    def test_the_better_chipset_wins(self) -> None:
        items = release("Chaos Engine, The (1993)(Renegade)[0106]", 10) + release("Chaos Engine, The (1994)(Renegade)(AGA)[1324]", 20)
        self.assertEqual(kept(items, self.select(items)), ["Chaos Engine, The (1994)(Renegade)(AGA)[1324]"])

    def test_an_original_beats_a_floppy_set_when_nothing_is_known_of_either(self) -> None:
        items = amiga_items(["Another World (1991)(Delphine)(Disk 1 of 2)[cr CSL]", "Another World (1991)(Delphine)(Disk 2 of 2)[cr CSL]"]) \
            + release("Another World (1991)(Delphine)[0425]", 50)
        sel = self.select(items)
        self.assertEqual(kept(items, sel), ["Another World (1991)(Delphine)[0425]"])
        self.assertEqual(sel.sets, [])

    def test_the_version_a_whdload_install_boots_from_wins(self) -> None:
        names = ["Zed (1990)(Pub)(Disk 1 of 2)[cr A]", "Zed (1990)(Pub)(Disk 2 of 2)[cr A]"]
        items = amiga_items(names) + release("Zed (1990)(Pub)[0001]", 50)
        with mock.patch.object(whdready, "level_of", return_value=whdready.BOOTS), \
                mock.patch.object(whdready, "release_of", return_value=[whdready.NO_BOOT, "Zed.lha"]):
            sel = self.select(items)
        self.assertEqual(acts(items, sel)[names[0] + ".adf"], KEEP)
        self.assertEqual(sel.decisions[50].action, SUPERSEDED)
        self.assertEqual([s.name for s in sel.sets], ["Zed (1990)(Pub)[cr A]"])

    def test_a_newer_floppy_set_that_boots_beats_an_older_original(self) -> None:
        names = ["Falcon v1.1 (1989)(Mirrorsoft)(Disk 1 of 2)", "Falcon v1.1 (1989)(Mirrorsoft)(Disk 2 of 2)"]
        items = amiga_items(names) + release("Falcon v1.00 (1989)(Mirrorsoft)[0368]", 50)
        with mock.patch.object(whdready, "level_of", return_value=whdready.BOOTS), \
                mock.patch.object(whdready, "release_of", return_value=[whdready.BOOTS, "Falcon.lha"]):
            sel = self.select(items)
        self.assertEqual(acts(items, sel)[names[0] + ".adf"], KEEP)
        self.assertEqual(sel.decisions[50].action, SUPERSEDED)

    def test_a_newer_version_that_only_installs_does_not_beat_an_older_one_that_boots(self) -> None:
        items = release("Kick Off 2 v1.4e (1990)(Anco)[0760]", 10) + release("Kick Off 2 v1.6e (1990)(Anco)[2191]", 20)
        levels = {"Kick Off 2 v1.4e (1990)(Anco)[0760]": whdready.BOOTS, "Kick Off 2 v1.6e (1990)(Anco)[2191]": whdready.IMAGES}
        with mock.patch.object(whdready, "release_of", side_effect=lambda rom: [levels[rom.game], "KickOff2.lha"]):
            sel = self.select(items)
        self.assertEqual(kept(items, sel), ["Kick Off 2 v1.4e (1990)(Anco)[0760]"])

    def test_the_better_chipset_still_beats_the_newer_version(self) -> None:
        items = release("Game v1.0 (1990)(Pub)(AGA)[0001]", 10) + release("Game v1.1 (1991)(Pub)[0002]", 20)
        self.assertEqual(kept(items, self.select(items)), ["Game v1.0 (1990)(Pub)(AGA)[0001]"])

    def test_a_cd32_disc_wins_over_every_original_and_floppy_set(self) -> None:
        items = amiga_items(["Cannon Fodder (1993)(Virgin)[cr CSL]"]) + release("Cannon Fodder (1993)(Virgin)[0860]", 50) \
            + release("Cannon Fodder (1993)(Virgin)(FR)[2332]", 60) + cd_items("Cannon Fodder (1993)(Virgin)[!][Amiga-CD32]")
        by = acts(items, self.select(items))
        self.assertEqual(len([k for k in by if "CD32" not in k]), 5)
        self.assertNotIn(KEEP, {v for k, v in by.items() if "CD32" not in k})
        self.assertEqual(by["Cannon Fodder (1993)(Virgin)[0860] - Disk1.ipf"], SUPERSEDED)
        self.assertEqual({v for k, v in by.items() if "CD32" in k}, {KEEP})

    def test_other_games_are_left_alone(self) -> None:
        items = release("Alpha (1990)(Pub)[0001]", 10) + release("Beta (1990)(Pub)[0002]", 20) + release("Kick Off 2 v1.6e (1990)(Anco)[2191]", 30) \
            + release("Kick Off 2 - The Final Whistle v1.9e (1991)(Anco)[data disk][0759]", 40)
        self.assertEqual(len(kept(items, self.select(items))), 4)

    def test_a_version_put_out_says_what_decided_it(self) -> None:
        items = release("Chaos Engine, The (1993)(Renegade)[0106]", 10) + release("Chaos Engine, The (1994)(Renegade)(AGA)[1324]", 20)
        self.assertIn("a better chipset (AGA > ECS > OCS)", self.select(items).decisions[10].reason)
        items = release("Falcon v1.00 (1989)(Mirrorsoft)[0368]", 10) + release("Falcon v1.1 (1989)(Mirrorsoft)[1754]", 20)
        self.assertIn("a newer version", self.select(items).decisions[10].reason)
        items = [*amiga_items(["Giga (1988)(SCA)[m baddump]"]), *amiga_items(["Giga (1988)(SCA)"])]
        items[1] = replace(items[1], key=2)
        sel = self.select(items, replace(AMIGA_DP, exclude=frozenset()))
        self.assertIn("a clean dump", sel.decisions[1].reason)

    def test_the_disks_of_an_unofficial_ipf_game_are_one_release(self) -> None:
        disks = ["Nova (1990)(Pub)(Disk 1 of 2)(Program Disk)[cp manual code]", "Nova (1990)(Pub)(Disk 2 of 2)(Course Disk)"]
        items = ipf_items(disks, 10) + amiga_items(["Nova (1990)(Pub)(Disk 1 of 2)[cr A]", "Nova (1990)(Pub)(Disk 2 of 2)[cr A]"])
        sel = self.select(items)
        ipf = {sel.decisions[k].action for k in (10, 11)}
        self.assertEqual(len(ipf), 1)                                  # (both disks stay, or both go: never one of them)

    def test_a_release_with_a_disk_missing_does_not_beat_a_whole_one(self) -> None:
        # the DAT's second disk of the IPF release is in another language (excluded): only disk 1 is kept
        items = ipf_items(["Chaos (1990)(On-line)(M3)(Disk 1 of 2)"], 10) \
            + amiga_items(["Chaos (1990)(On-line)(Disk 1 of 2)[cr A]", "Chaos (1990)(On-line)(Disk 2 of 2)[cr A]"])
        sel = self.select(items)
        self.assertEqual(sel.decisions[10].action, SUPERSEDED)
        self.assertEqual({sel.decisions[k].action for k in (1, 2)}, {KEEP})

    def test_an_sps_release_beats_an_unofficial_ipf_one_when_nothing_else_tells_them_apart(self) -> None:
        items = ipf_items(["Dyter (1990)(reLINE)(Disk 1 of 2)", "Dyter (1990)(reLINE)(Disk 2 of 2)"], 10) + sps_items("Dyter (1990)(reLINE)[0678]", ["Dyter"], start=50)
        sel = self.select(items)
        self.assertEqual(sel.decisions[50].action, KEEP)
        self.assertEqual({sel.decisions[10].action, sel.decisions[11].action}, {SUPERSEDED})

    def test_the_incomplete_disks_of_a_set_are_no_release(self) -> None:
        items = amiga_items(["M (1990)(P)(Disk 1 of 3)", "M (1990)(P)(Disk 3 of 3)"])
        sel = self.select(items, replace(AMIGA_DP, complete_only=False))
        self.assertEqual(set(acts(items, sel).values()), {KEEP})

    def test_it_is_the_rule_one_version_per_game_with_best_variant(self) -> None:
        items = release("Falcon v1.00 (1989)(Mirrorsoft)[0368]", 10) + release("Falcon v1.1 (1989)(Mirrorsoft)[1754]", 20)
        self.assertEqual(len(kept(items, self.select(items, replace(AMIGA_DP, one_per_game=False)))), 2)
        self.assertEqual(len(kept(items, self.select(items, replace(AMIGA_DP, best_variant=False)))), 2)
        self.assertEqual(len(kept(items, self.select(items))), 1)

    def test_the_default_is_on_for_the_amiga_only_where_it_means_something(self) -> None:
        from romorg import platforms
        self.assertTrue(library.default_profile(AMIGA).one_per_game)
        self.assertIn("one_per_game", library.applicable_fields(AMIGA))
        atari = platforms.get_platform("Atari Lynx")
        self.assertEqual(library.default_profile(atari).one_per_game, bool(atari.region_dats))


if __name__ == "__main__":
    unittest.main()
