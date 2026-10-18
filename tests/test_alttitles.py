"""The rule "Combine alternate titles": the titles a game was released under in other regions are one game (``romorg.alttitles``)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from romorg import alttitles, library, paths, platforms, ratings, totals
from romorg.datfile import Rom
from romorg.library import KEEP, SUPERSEDED, Item

GBA = platforms.get_platform("Nintendo Game Boy Advance")
DAT = "Nintendo - Game Boy Advance"
LB = "Nintendo Game Boy Advance"


def rom(name: str) -> Rom:
    return Rom(name=name + ".gba", size=1, crc="", md5="", sha1="", game=name, dat=DAT, set_name=name)


class AltTitlesCase(unittest.TestCase):
    # (key, dbid, alt): LaunchBox's names of the games
    NAMES = [("resident evil", 1, 0), ("biohazard", 1, 1),
             ("skies of arcadia", 2, 0), ("eternal arcadia", 2, 1),
             ("life is strange remastered collection", 3, 0), ("life is strange remastered", 3, 1),
             ("life is strange before storm remastered", 3, 1),
             ("tetris", 4, 0), ("block game", 4, 1), ("block game", 5, 0), ("falling blocks", 5, 1),
             ("ace attorney", 6, 0), ("大逆転裁判1＆2", 6, 1), ("1 2", 7, 0)]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(alttitles.forget)
        self.addCleanup(alttitles.clear_cache)
        alttitles.forget()
        alttitles.clear_cache()
        d = paths.ratings_dir()
        d.mkdir(parents=True, exist_ok=True)
        ratings.write_index([(LB, ratings.norm_title(k), 4.0, 10, i, k, 2000, a) for k, i, a in self.NAMES if k != "block game" or i == 5],
                            d / ratings.INDEX_NAME, {})
        # ("block game" is the name of two games: an index keeps one row per name, so the shared name is written by hand below)

    def learn(self, names: list[str]) -> list[Item]:
        dat = SimpleNamespace(name=DAT, roms=[rom(n) for n in names])
        alttitles.learn(GBA, [dat])
        return [Item(key=i, dat=DAT, rom=rom(n), style="nointro", path=Path(n), member=None) for i, n in enumerate(names, 1)]

    @staticmethod
    def profile(**changes) -> library.LibraryProfile:
        return replace(library.default_profile(GBA), **{"languages": (), "region_priority": ("USA", "Europe", "Japan"), **changes})

    def kept(self, names: list[str], **changes) -> list[str]:
        items = self.learn(names)
        sel = library.select(items, self.profile(**changes), GBA)
        return [n for k, n in enumerate(names, 1) if sel.decisions[k].action == KEEP]


class TheTitlesOfAGame(AltTitlesCase):
    def test_the_names_of_one_launchbox_game_are_one_group(self) -> None:
        tbl = alttitles.table(GBA)
        self.assertEqual(tbl["biohazard"], tbl["resident evil"])
        self.assertEqual(tbl["eternal arcadia"], tbl["skies of arcadia"])
        self.assertNotEqual(tbl["biohazard"], tbl["skies of arcadia"])

    def test_a_title_in_another_script_joins_nothing(self) -> None:
        self.assertEqual(alttitles.node("大逆転裁判1＆2"), "")
        self.assertNotIn("1 2", alttitles.table(GBA))

    def test_the_regions_of_a_title_tell_two_games_apart(self) -> None:
        names = ["Life is Strange Remastered (USA, Europe)", "Life is Strange - Before the Storm Remastered (USA, Europe)",
                 "Biohazard (Japan)", "Resident Evil (USA)"]
        self.learn(names)
        m = alttitles.dat_map(DAT)
        self.assertEqual(m["biohazard"], m["resident evil"])
        self.assertNotIn("life is strange remastered", m)      # (both are USA, Europe: two games)
        self.assertEqual(alttitles.merged(DAT), [["biohazard", "resident evil"]])


class OneReleaseOfTheGame(AltTitlesCase):
    NAMES_DAT = ["Biohazard (Japan)", "Resident Evil (USA)", "Resident Evil (Europe)", "Eternal Arcadia (Japan)"]

    def test_one_release_is_kept_by_your_regions(self) -> None:
        self.assertEqual(self.kept(self.NAMES_DAT), ["Resident Evil (USA)", "Eternal Arcadia (Japan)"])

    def test_off_every_title_is_a_game(self) -> None:
        self.assertEqual(self.kept(self.NAMES_DAT, combine_alt_titles=False),
                         ["Biohazard (Japan)", "Resident Evil (USA)", "Eternal Arcadia (Japan)"])

    def test_the_other_title_is_set_aside_for_the_one_kept(self) -> None:
        items = self.learn(self.NAMES_DAT)
        sel = library.select(items, self.profile(), GBA)
        d = sel.decisions[1]
        self.assertEqual((d.action, d.superseded_by), (SUPERSEDED, "Resident Evil (USA)"))

    def test_it_counts_once_in_the_totals(self) -> None:
        dat = SimpleNamespace(name=DAT, roms=[rom(n) for n in self.NAMES_DAT])
        alttitles.learn(GBA, [dat])
        on = totals.compute_target(GBA, [dat], self.profile())
        off = totals.compute_target(GBA, [dat], self.profile(combine_alt_titles=False))
        self.assertEqual((on.count, off.count), (2, 3))

    def test_the_rule_is_on_by_default_on_every_system(self) -> None:
        self.assertTrue(library.default_profile(GBA).combine_alt_titles)
        for name in ("Nintendo Game Boy Advance", "Commodore Amiga", "Commodore Amiga - WHDLoad", "Sega Dreamcast"):
            self.assertIn("combine_alt_titles", library.applicable_fields(platforms.get_platform(name)))

    def test_without_the_dat_learnt_nothing_is_combined(self) -> None:
        alttitles.forget()
        items = [Item(key=i, dat=DAT, rom=rom(n), style="nointro", path=Path(n), member=None) for i, n in enumerate(self.NAMES_DAT, 1)]
        sel = library.select(items, self.profile(), GBA)
        self.assertEqual(sum(1 for d in sel.decisions.values() if d.action == KEEP), 3)


if __name__ == "__main__":
    unittest.main()


AMIGA = platforms.get_platform("Commodore Amiga")
GAMES = "Commodore Amiga - Games - [ADF]"


def adf(name: str) -> Rom:
    return Rom(name=name + ".adf", size=1, crc="", md5="", sha1="", game=name, dat=GAMES)


class AmigaGames(unittest.TestCase):
    """TOSEC Amiga: the country, the publisher and the alternate titles of a game are one game; the best variant is chosen by your
    languages, and the language filter sets aside a game with no version in them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(alttitles.forget)
        self.addCleanup(alttitles.clear_cache)
        alttitles.forget()
        alttitles.clear_cache()
        d = paths.ratings_dir()
        d.mkdir(parents=True, exist_ok=True)
        rows = [("Commodore Amiga", "another world", 4.0, 10, 1, "Another World", 1991, 0),
                ("Commodore Amiga", "out of this world", 4.0, 10, 1, "Another World", 1991, 1)]
        ratings.write_index(rows, d / ratings.INDEX_NAME, {})

    def decide(self, names: list[str], **changes) -> dict[str, str]:
        dat = SimpleNamespace(name=GAMES, roms=[adf(n) for n in names])
        alttitles.learn(AMIGA, [dat])
        items = [Item(key=i, dat=GAMES, rom=adf(n), style="tosec", path=Path(n), member=None) for i, n in enumerate(names, 1)]
        prof = replace(library.default_profile(AMIGA), **{"languages": ("En",), **changes})
        sel = library.select(items, prof, AMIGA)
        return {n: sel.decisions[k].action for k, n in enumerate(names, 1)}

    def test_the_release_of_another_country_and_publisher_is_the_same_game(self) -> None:
        names = ["3D Pool (1989)(Firebird)", "3D Pool (1991)(MicroProse)(DE)"]
        self.assertEqual(self.decide(names, languages=()), {names[0]: SUPERSEDED, names[1]: KEEP})      # (the newer one)
        self.assertEqual(self.decide(names, languages=(), combine_alt_titles=False), {names[0]: KEEP, names[1]: KEEP})

    def test_alternate_titles_are_one_game_and_the_language_decides(self) -> None:
        names = ["Another World (1991)(Delphine)(FR)", "Out of this World (1991)(Interplay)(US)"]
        by = self.decide(names)
        self.assertEqual(by, {names[0]: library.EXCLUDED, names[1]: KEEP})
        by = self.decide(names, languages=("Fr", "En"))
        self.assertEqual(by, {names[0]: KEEP, names[1]: SUPERSEDED})

    def test_a_game_with_no_version_in_your_languages_is_set_aside(self) -> None:
        names = ["Anstoss (1993)(Ascon)(DE)"]
        self.assertEqual(self.decide(names), {names[0]: library.EXCLUDED})

    def test_known_different_games_that_share_a_title_stay_apart(self) -> None:
        names = ["Overlord (1991)(Virgin Mastertronic)(US)", "Overlord (1994)(Virgin)", "Legend (1989)(CRL)",
                 "Legend (1992)(Mindscape)", "Legend (1992)(Mindscape - Future Publishing)"]
        by = self.decide(names)
        self.assertEqual([by[n] for n in names[:3]], [KEEP, KEEP, KEEP])
        self.assertEqual(sorted(by[n] for n in names[3:]), [KEEP, SUPERSEDED])            # (the cover disk is Mindscape's Legend: one of the two)
        self.assertEqual(library._apart("tetris", "mirrorsoft"), "")


class TopRatedDuplicates(AltTitlesCase):
    """What checking every system's top 250 for duplicates found."""
    NAMES = AltTitlesCase.NAMES + [("super c", 20, 0), ("probotector ii - return of the evil forces", 20, 1),
                                   ("ghouls'n ghosts", 21, 0), ("daimakaimura", 21, 1)]

    def test_a_collection_re_release_does_not_keep_the_regional_titles_apart(self) -> None:
        names = ["Super C (USA)", "Probotector II - Return of the Evil Forces (Europe)",
                 "Super C (World) (Contra Anniversary Collection)", "Probotector II - Return of the Evil Forces (World) (Collection)"]
        self.assertEqual(self.kept(names), ["Super C (USA)"])

    def test_one_title_released_in_the_others_region_too(self) -> None:
        self.assertEqual(self.kept(["Daimakaimura (Japan)", "Ghouls'n Ghosts (Japan, USA) (En,Ja)"]), ["Ghouls'n Ghosts (Japan, USA) (En,Ja)"])

    def test_titles_spelt_alike_are_one_game_without_launchbox(self) -> None:
        self.assertEqual(self.kept(["Rock 'n' Roll Racing (Europe)", "Rock n' Roll Racing (USA)"]), ["Rock n' Roll Racing (USA)"])
        self.assertEqual(alttitles.node("ABZÛ (アブズ)"), "abzu")                      # (a gloss in another script)

    def test_the_licensed_release_and_the_retail_one_come_first(self) -> None:
        self.assertEqual(self.kept(["Zap (Asia) (En) (Kaiser) (Pirate)", "Zap (USA)"], region_priority=("Asia", "USA")), ["Zap (USA)"])
        self.assertEqual(self.kept(["Zap (USA) (Sega Channel)", "Zap (USA)"]), ["Zap (USA)"])

    def test_every_disc_of_a_game_on_several_discs_stays(self) -> None:
        names = ["Zap (USA) (Disc 1)", "Zap (USA) (Disc 2)", "Zap (Europe) (Disc 1)", "Zap (Europe) (Disc 2)"]
        self.assertEqual(self.kept(names), ["Zap (USA) (Disc 1)", "Zap (USA) (Disc 2)"])
