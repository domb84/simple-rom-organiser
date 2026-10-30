"""The gaps of "Combine alternate titles" an audit found: titles in Japanese script, the same-region veto against hand-checked pairs and the
Japanese budget series, a combined game's rating, a missing ratings index, ``title_key``, and ``KEEP_APART`` on every platform."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from romorg import alttitles, library, paths, platforms, ratings
from romorg.datfile import Rom
from romorg.library import EXCLUDED, KEEP, Item
from tests.test_alttitles import DAT, GBA, LB, AltTitlesCase, rom


def merged() -> list[list[str]]:
    return sorted(sorted(g) for g in alttitles.merged(DAT))


def keys_of(names: list[str]) -> list[tuple]:
    return [library.game_key(Item(key=i, dat=DAT, rom=rom(n), style="nointro", path=Path(n), member=None), True)
            for i, n in enumerate(names, 1)]


class JapaneseScript(AltTitlesCase):
    NAMES = AltTitlesCase.NAMES + [("legend of heroes", 8, 0), (ratings.native_key("英雄伝説"), 8, 1)]

    def test_a_title_is_compared_by_its_own_letters(self) -> None:
        self.assertEqual(alttitles.node("大逆転裁判1＆2"), "~大逆転裁判12")
        self.assertEqual(alttitles.node("アイスクリームサーファー"), alttitles.node("アイスクリーム・サーファー"))
        self.assertNotEqual(alttitles.node("大逆転裁判1＆2"), alttitles.node("大逆転裁判3"))
        self.assertEqual(alttitles.node("ABZÛ (アブズ)"), "abzu")                    # (a gloss is no part of the title)

    def test_the_names_in_another_script_of_a_launchbox_game_are_kept_in_the_index(self) -> None:
        rows = ratings.build_rows({1: {"name": "Ace Attorney", "plat": LB, "rating": 4.0, "votes": 9, "year": 2000}},
                                  {1: ["大逆転裁判1＆2", "大逆転裁判 (Dai Gyakuten Saiban)"]})
        keys = {r[1] for r in rows}
        self.assertIn(ratings.native_key("大逆転裁判1＆2"), keys)
        self.assertNotIn("1 2", keys)                                               # (the Latin letters of it are no title)
        self.assertIn("dai gyakuten saiban", keys)

    def test_a_title_in_japanese_script_joins_the_release_of_its_launchbox_game(self) -> None:
        d = paths.ratings_dir()
        ratings.write_index([(LB, "legend of heroes", 4.0, 10, 8, "Legend of Heroes", 2000, 0),
                             (LB, ratings.native_key("英雄伝説"), 4.0, 10, 8, "Legend of Heroes", 2000, 1)], d / ratings.INDEX_NAME, {})
        alttitles.clear_cache()
        self.learn(["英雄伝説 (Japan)", "Legend of Heroes (USA)"])
        m = alttitles.dat_map(DAT)
        self.assertEqual(m["英雄伝説"], m["legend of heroes"])

    def test_its_latin_words_are_the_title_of_an_english_release(self) -> None:
        self.learn(["Astroneer -アストロニーア- (Japan)", "Astroneer (USA)", "Moto Roader MC (USA)",
                    "クラシック レーシング パック：Moto Roader MC + バイク大好き！ (Japan)"])
        m = alttitles.dat_map(DAT)
        self.assertEqual(m["astroneer -アストロニーア-"], m["astroneer"])
        self.assertNotIn("moto roader mc", m)                                       # (Japanese on both sides of the words: a pack)

    def test_a_series_in_japanese_script_and_a_long_subtitle_join_nothing(self) -> None:
        self.learn(["ゼロファイアー -Toaplan Arcade Garage- (Japan)", "究極タイガーヘリ -Toaplan Arcade Garage- (Japan)",
                    "Toaplan Arcade Garage (USA)", "Fit Boxing (USA)", "Fit Boxing 北斗の拳 ～お前はもう痩せている～ (Japan)",
                    "Vol.2 (USA)", "ゲームコレクション Vol.2 (Japan)"])
        self.assertEqual(merged(), [])

    def test_titles_in_japanese_script_spelt_alike_are_one_game(self) -> None:
        self.learn(["ガンズゴア＆カノーリ２ (Japan)", "ガンズゴア＆カノーリ2 (Japan) (Demo)"])
        m = alttitles.dat_map(DAT)
        self.assertEqual(len(set(m.values())), 1)


class TheHandBeatsTheVeto(AltTitlesCase):
    HAND = {"Nintendo Game Boy Advance": [("Minesweeper", "Mine Sweeper")]}

    def test_two_titles_of_one_region_are_two_games_unless_checked_by_hand(self) -> None:
        names = ["Minesweeper (USA)", "Mine Sweeper (USA)", "Sudoku (USA)", "Sudoku Master (USA)"]
        self.learn(names)
        self.assertEqual(merged(), [])
        with mock.patch.dict(alttitles.SAME_GAME, self.HAND):
            alttitles.clear_cache()
            self.learn(names)
            self.assertEqual(merged(), [["mine sweeper", "minesweeper"]])

    def test_the_hand_pairs_of_the_real_cases_are_there(self) -> None:
        pairs = {(s, tuple(p)) for s, ps in alttitles.SAME_GAME.items() for p in ps}
        for s, p in [("Sega Master System", ("Minesweeper", "Mine Sweeper")),
                     ("Super Nintendo Entertainment System", ("Cannondale Cup", "Exertainment Mountain Bike Rally")),
                     ("Sony PlayStation 2", ("Adiboo and the Energy Thieves", "Energy Thieves, The"))]:
            self.assertIn((s, p), pairs)

    def test_a_pair_checked_apart_is_never_joined(self) -> None:
        names = ["Alpha (USA)", "Beta (Japan)"]
        with mock.patch.dict(alttitles.SAME_GAME, {"Nintendo Game Boy Advance": [("Alpha", "Beta")]}):
            alttitles.clear_cache()
            self.learn(names)
            self.assertEqual(merged(), [["alpha", "beta"]])
            with mock.patch.dict(library.NOT_SAME_GAME, {"Nintendo Game Boy Advance": [("Alpha", "Beta")]}):
                alttitles.clear_cache()
                self.learn(names)
                self.assertEqual(merged(), [["alpha", "beta"]])        # (the hand wins)
        with mock.patch.dict(library.NOT_SAME_GAME, {"Nintendo Game Boy Advance": [("Resident Evil", "Biohazard")]}):
            alttitles.clear_cache()
            self.learn(["Biohazard (Japan)", "Resident Evil (USA)"])
            self.assertEqual(merged(), [])


class BudgetSeries(AltTitlesCase):
    def test_a_series_prefix_is_no_part_of_the_game(self) -> None:
        self.assertEqual(alttitles.series_title("SuperLite 1500 Series - Ring of Sias"), "Ring of Sias")
        self.assertEqual(alttitles.series_title("Simple 1500 Series Vol. 93 - The Puzzle Bobble - Puzzle Bobble 4"),
                         "The Puzzle Bobble - Puzzle Bobble 4")
        self.assertEqual(alttitles.series_title("SuperLite 2000 Vol. 6 - Konohana 2"), "Konohana 2")
        self.assertEqual(alttitles.series_title("Time Bokan Series - Foo"), "Time Bokan Series - Foo")

    def test_a_series_title_joins_its_bare_title_in_the_same_region(self) -> None:
        self.learn(["SuperLite 1500 Series - Ring of Sias (Japan)", "Ring of Sias (Japan)",
                    "Simple 1500 Series Vol. 93 - The Puzzle Bobble - Puzzle Bobble 4 (Japan)", "Puzzle Bobble 4 (Japan)",
                    "SuperLite 1500 Series - Mahjong II (Japan)", "Simple 1500 Series Vol. 39 - The Mahjong 2 (Japan)"])
        self.assertEqual(merged(), [["puzzle bobble 4", "simple 1500 series vol. 93 - the puzzle bobble - puzzle bobble 4"],
                                    ["ring of sias", "superlite 1500 series - ring of sias"],
                                    ["simple 1500 series vol. 39 - the mahjong 2", "superlite 1500 series - mahjong ii"]])

    def test_sequels_and_other_numbered_entries_stay_apart(self) -> None:
        self.learn(["SuperLite 1500 Series - Neo Atlas (Japan)", "Neo Atlas II (Japan)", "Neo Atlas (Japan)",
                    "Simple 1500 Series Vol. 5 - The Golf (Japan)", "Golf 2 (Japan)",
                    "SuperLite 2000 Vol. 37 - Memories Off - After Rain Vol. 1 (Japan)", "Memories Off - After Rain Vol. 2 (Japan)"])
        self.assertEqual(merged(), [["neo atlas", "superlite 1500 series - neo atlas"]])
        names = ["SuperLite 1500 Series - Neo Atlas (Japan)", "Neo Atlas II (Japan)", "Neo Atlas (Japan)"]
        a, b, c = keys_of(names)
        self.assertEqual(a, c)
        self.assertNotEqual(a, b)


class TheRatingOfACombinedGame(AltTitlesCase):
    def test_the_best_rating_of_its_titles_counts_whichever_release_is_kept(self) -> None:
        ratings_of = {"Alpha": (3.0, 50), "Beta": (4.5, 50), "Gamma": (4.0, 50)}
        lookup = lambda title: ratings_of.get(title)                                # noqa: E731
        items = [Item(key=i, dat=DAT, rom=rom(n), style="nointro", path=Path(n), member=None)
                 for i, n in enumerate(["Alpha (USA)", "Beta (Japan)", "Gamma (USA)"], 1)]
        with mock.patch.dict(alttitles.SAME_GAME, {"Nintendo Game Boy Advance": [("Alpha", "Beta")]}):
            alttitles.clear_cache()
            alttitles.learn(GBA, [SimpleNamespace(name=DAT, roms=[i.rom for i in items])])
            ctx = library.RatingContext(lookup, None)
            profile = self.profile(top_n=1, rank_scope="owned")
            sel = library.select(items, profile, GBA, ratings=ctx)
            self.assertEqual({sel.decisions[i.key].action == KEEP for i in items[:1]}, {True})      # (Alpha (USA) is the kept release)
            self.assertEqual(sel.decisions[3].action, EXCLUDED)                     # (Gamma: 4.0 < the 4.5 of Beta, Alpha's other title)
            self.assertEqual(sel.rating["rated"], 2)
            # (the release that is kept does not matter: Japan first keeps Beta, the same game is kept)
            sel2 = library.select(items, replace(profile, region_priority=("Japan", "USA")), GBA, ratings=ctx)
            self.assertEqual(sel2.decisions[2].action, KEEP)
            self.assertEqual(sel2.decisions[3].action, EXCLUDED)

    def test_a_vote_count_below_the_minimum_does_not_count(self) -> None:
        lookup = {"Alpha": (3.0, 50), "Beta": (5.0, 2)}.get
        self.assertEqual(library._game_rating(["Alpha", "Beta"], lookup, 10), ("Alpha", (3.0, 50)))
        self.assertEqual(library._game_rating(["Alpha", "Beta"], lookup, 1), ("Beta", (5.0, 2)))
        self.assertEqual(library._game_rating(["Zed"], lookup, 1), ("Zed", None))


class WithoutTheRatingsIndex(AltTitlesCase):
    NAMES_DAT = ["Biohazard (Japan)", "Resident Evil (USA)", "Eternal Arcadia (Japan)", "Skies of Arcadia (USA)"]

    def test_the_last_names_are_remembered_and_still_combine(self) -> None:
        self.learn(self.NAMES_DAT)
        self.assertEqual(len(merged()), 2)
        self.assertEqual(alttitles.notice(GBA), "")
        index = ratings.index_path(ratings.default_store().directory)
        self.assertTrue((index.parent / "alttitles.json").is_file())
        index.unlink()
        alttitles.clear_cache()
        alttitles.forget()
        self.learn(self.NAMES_DAT)
        self.assertEqual(len(merged()), 2)                             # (not collapsed)
        self.assertIn("ratings data is not installed", alttitles.notice(GBA))
        self.assertIn("before", alttitles.notice(GBA))

    def test_with_nothing_remembered_there_is_a_notice(self) -> None:
        index = ratings.index_path(ratings.default_store().directory)
        index.unlink()
        f = index.parent / "alttitles.json"
        if f.exists():
            f.unlink()
        alttitles.clear_cache()
        self.learn(self.NAMES_DAT)
        self.assertEqual(merged(), [])
        self.assertIn("not combined", alttitles.notice(GBA))
        self.assertEqual(alttitles.notice("Nintendo Switch"), alttitles.notice("Nintendo Switch"))
        self.assertEqual(alttitles.notice("Some Other System"), "")

    def test_the_plan_notices_it(self) -> None:
        from romorg import server
        app = server.App.__new__(server.App)
        app._profile = lambda platform, cfg=None: library.default_profile(platform)
        index = ratings.index_path(ratings.default_store().directory)
        index.unlink()
        (index.parent / "alttitles.json").unlink(missing_ok=True)
        alttitles.clear_cache()
        self.assertEqual(server.App._plan_warnings(app, SimpleNamespace(platform=GBA, root="/x"), []), [])     # (no reason to stop a Build)
        out = server.App._plan_notices(app, GBA)
        self.assertTrue(any("ratings data is not installed" in w for w in out))
        self.assertEqual(server.App._plan_notices(app, platforms.get_platform("Nintendo Switch")) != [], True)


class TheTitleKey(AltTitlesCase):
    def test_it_follows_the_combined_title(self) -> None:
        items = self.learn(["Biohazard (Japan)", "Resident Evil (USA)", "Zap (USA)"])
        a, b, c = items
        self.assertNotEqual(library.title_key(a), library.title_key(b))
        self.assertEqual(library.title_key(a, True), library.title_key(b, True))
        self.assertNotEqual(library.title_key(a, True), library.title_key(c, True))

    def test_a_game_kept_complete_in_another_title_covers_the_incomplete_one(self) -> None:
        items = self.learn(["Biohazard (Japan)", "Resident Evil (USA)"])
        sel = SimpleNamespace(decisions={2: SimpleNamespace(action=KEEP, missing=())}, combine_alt=True)
        self.assertEqual(library.kept_titles(items, sel), {library.title_key(items[0], True): "Resident Evil (USA)"})


class KeepApartOnEveryPlatform(unittest.TestCase):
    def test_a_no_intro_title_shared_by_two_games_is_two_games(self) -> None:
        gb = platforms.get_platform("Nintendo Game Boy")
        dat = gb.dats[0]

        def key(name: str) -> tuple:
            r = Rom(name=name + ".gb", size=1, crc="", md5="", sha1="", game=name, dat=dat, set_name=name)
            return library.game_key(Item(key=1, dat=dat, rom=r, style="nointro", path=Path(name), member=None), False)

        self.assertEqual(key("Space Invaders (USA) (SGB Enhanced)"), key("Space Invaders (Europe) (SGB Enhanced)"))
        self.assertNotEqual(key("Space Invaders (USA) (SGB Enhanced)"), key("Space Invaders (Japan) (En)"))
        self.assertEqual(key("Tetris (USA)"), key("Tetris (Japan)"))               # (any other title: one game)
        # (the same title on another platform is not listed)
        nes = platforms.get_platform("Nintendo Entertainment System")
        r = Rom(name="Space Invaders (Japan).nes", size=1, crc="", md5="", sha1="", game="x", dat=nes.dats[0], set_name="x")
        r2 = replace(r, name="Space Invaders (USA).nes")
        mk = lambda x: library.game_key(Item(key=1, dat=nes.dats[0], rom=x, style="nointro", path=Path(x.name), member=None), False)  # noqa: E731
        self.assertEqual(mk(r), mk(r2))

    def test_the_tosec_table_is_the_amiga_s(self) -> None:
        self.assertIs(library.KEEP_APART_PLATFORMS["Commodore Amiga"], library.KEEP_APART)
        self.assertEqual(library._apart("overlord", "virgin", "Commodore Amiga - Games - [ADF]"), "#1")
        self.assertEqual(library._apart("overlord", "virgin", "Nintendo - Game Boy"), "")      # (another platform's DAT: not its title)

    def test_the_hand_checked_pairs_of_the_ratings_data_are_split(self) -> None:
        self.assertIn(("Digimon World 2003", "Digimon World 3"), library.NOT_SAME_GAME["Sony PlayStation"])


if __name__ == "__main__":
    unittest.main()
