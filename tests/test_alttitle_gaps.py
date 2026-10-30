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


def _two(a: tuple, b: tuple, plat: str = "") -> bool:
    return alttitles._two_games(a, b, alttitles.hand_games(plat), alttitles.not_same(plat))


def _t(title: str, *regions: str) -> tuple:
    return (title, {(): frozenset(regions)}, 1)


class NumberedTitles(unittest.TestCase):
    """A sequel number, a roman numeral or a year is no "subtitle": a region that has both titles has two games (the false merges of the
    sweep of 2026-10-30: Bomberman GB / GB 2, King's Field II / III, Motor Toon Grand Prix / 2); a rename in other regions stays one."""

    def test_the_same_words_with_other_numbers(self) -> None:
        for a, b in [("bomberman gb", "bomberman gb 2"), ("kings field 2", "kings field 3"), ("mario golf", "mario golf 64"),
                     ("winter sports 2009 next challenge", "winter sports 2 next challenge")]:
            self.assertTrue(alttitles._numbered(a, b), (a, b))
            self.assertTrue(alttitles._numbered(b, a), (a, b))
        for a, b in [("super mario world", "super mario world super mario bros 4"), ("neo atlas", "neo atlas"), ("2 2", "3 3"),
                     ("adventure island part 2", "adventure island 2"), ("tetris", "tetris plus")]:
            self.assertFalse(alttitles._numbered(a, b), (a, b))

    def test_a_region_with_both_numbers_has_two_games(self) -> None:
        self.assertTrue(_two(_t("bomberman gb", "Europe", "Japan", "USA"), _t("bomberman gb 2", "Japan")))
        self.assertTrue(_two(_t("kings field 2", "Japan", "USA"), _t("kings field 3", "Japan")))
        self.assertTrue(_two(_t("master fighter 2", "Asia", "Taiwan"), _t("master fighter 3", "Asia")))

    def test_a_rename_for_other_regions_is_one_game(self) -> None:
        # (the checked cases: Japan's "Gauntlet" is the Mega Drive's Gauntlet IV, "Ghostbusters III (Asia)" a copy of Ghostbusters II,
        #  the PS1 "International Track & Field 2" is the USA's "... 2000": one size, complementary regions, no third title)
        self.assertFalse(_two(_t("gauntlet", "Japan"), _t("gauntlet 4", "Europe", "USA")))
        self.assertFalse(_two(_t("ghostbusters 2", "Europe", "USA"), _t("ghostbusters 3", "Asia")))
        self.assertFalse(_two(_t("international track field 2", "Europe"), _t("international track field 2000", "USA")))
        self.assertFalse(_two(_t("final fantasy 2", "Europe", "USA"), _t("final fantasy 4", "Japan")))
        self.assertFalse(_two(_t("mario golf", "Europe", "USA"), _t("mario golf 64", "Japan")))

    def test_the_hand_beats_it_and_the_cases_are_listed(self) -> None:
        plat = "Sony PlayStation"
        self.assertTrue(_two(_t("kings field 2", "Japan", "USA"), _t("kings field 3", "Japan")))
        self.assertFalse(_two(_t("king's field ii", "Japan", "USA"), _t("king's field iii", "Japan"), plat))
        self.assertFalse(_two(_t("motor toon grand prix 2", "Europe", "Japan"), _t("motor toon grand prix", "Japan", "USA"), plat))
        self.assertFalse(_two(_t("final fantasy origins - final fantasy", "Europe"), _t("final fantasy origins - final fantasy ii", "Europe"), plat))
        self.assertFalse(_two(_t("bomberman gb", "Europe", "Japan", "USA"), _t("bomberman gb 2", "Japan"), "Nintendo Game Boy"))


def _game(plat: str, name: str, ext: str = ".gb", style: str = "nointro") -> tuple:
    p = platforms.get_platform(plat)
    dat = p.dats[0]
    r = Rom(name=name + ext, size=1, crc="", md5="", sha1="", game=name, dat=dat, set_name=name)
    return library.game_key(Item(key=1, dat=dat, rom=r, style=style, path=Path(name), member=None), False)


class TheSweepOfFalseMerges(unittest.TestCase):
    def test_game_boy_centipede_by_majesco_is_not_accolade_s(self) -> None:
        gb = "Nintendo Game Boy"
        self.assertNotEqual(_game(gb, "Centipede (USA) (Majesco)"), _game(gb, "Centipede (USA, Europe) (Accolade)"))
        self.assertEqual(_game(gb, "Centipede (USA, Europe) (Accolade)"), _game(gb, "Centipede (Europe) (Accolade)"))

    def test_game_boy_monopoly_stays_one_game_in_every_region(self) -> None:
        gb = "Nintendo Game Boy"       # (one title, the ratings data knows one game: nothing proves a second program)
        self.assertEqual(_game(gb, "Monopoly (USA)"), _game(gb, "Monopoly (Japan)"))
        self.assertEqual(_game(gb, "Monopoly (Europe) (En,Fr,De)"), _game(gb, "Monopoly (Japan)"))

    def test_bomberman_gb_of_japan_is_the_us_wario_blast_not_the_us_bomberman_gb(self) -> None:
        gb = "Nintendo Game Boy"
        self.assertNotEqual(_game(gb, "Bomberman GB (Japan) (SGB Enhanced)"), _game(gb, "Bomberman GB (USA, Europe) (SGB Enhanced)"))
        self.assertEqual(_game(gb, "Bomberman GB (USA, Europe) (SGB Enhanced)"), _game(gb, "Bomberman GB (USA, Europe) (SGB Enhanced)"))

    def test_the_kings_fields_of_the_playstation(self) -> None:
        ps = "Sony PlayStation"
        k = lambda n: _game(ps, n, ".cue", "redump")  # noqa: E731
        self.assertNotEqual(k("King's Field (Japan)"), k("King's Field (USA)"))
        self.assertEqual(k("King's Field (USA)"), k("King's Field (Europe) (En,Fr,De)"))
        self.assertNotEqual(k("King's Field II (Japan)"), k("King's Field II (USA)"))
        self.assertEqual(k("King's Field II (Japan) (PlayStation the Best)"), k("King's Field II (Japan)"))
        self.assertEqual(k("Motor Toon Grand Prix (USA)"), k("Motor Toon Grand Prix (USA)"))
        self.assertNotEqual(k("Motor Toon Grand Prix (Japan)"), k("Motor Toon Grand Prix (USA)"))
        self.assertEqual(k("Motor Toon Grand Prix (Japan)"), k("Motor Toon Grand Prix (Japan) (Rev 1)"))

    def test_the_hand_checked_pairs_of_the_sweep_are_split(self) -> None:
        for plat, pair in [("Nintendo Game Boy Color", ("Game Boy Gallery 3", "Game Boy Gallery 4")),
                           ("Nintendo Game Boy Color", ("Space Invasion", "Space Invasion & Painter")),
                           ("Nintendo Game Boy Advance", ("Sims 2, The", "Sims, The")),
                           ("Sega Dreamcast", ("Memories Off 2nd", "Memories Off 2nd - Promotion Disc")),
                           ("Sega Dreamcast", ("Sentimental Graffiti 2", "Sentimental Graffiti 2 - Third Window")),
                           ("Nintendo GameCube", ("Pokemon Colosseum", "Pokemon Colosseum - Nintendo Tokusei Disc")),
                           ("Sony PlayStation", ("Real Bout Garou Densetsu", "SNK Fan Collection - Garou Densetsu"))]:
            self.assertIn(pair, library.NOT_SAME_GAME[plat])
            self.assertTrue(_two(_t(alttitles.node(pair[0]), "Japan"), _t(alttitles.node(pair[1]), "Japan"), plat))

    def test_the_hand_pairs_are_where_the_checks_looked_for_them(self) -> None:
        pairs = {(s, tuple(p)) for s, ps in alttitles.SAME_GAME.items() for p in ps}
        self.assertIn(("Sony PlayStation", ("King's Field II", "King's Field III")), pairs)
        self.assertIn(("Nintendo Game Boy", ("Bomberman GB", "Bomberman GB 2")), pairs)
        self.assertEqual(library.KEEP_APART_PLATFORMS["Nintendo Game Boy"]["centipede"], [frozenset({"majesco"})])
        self.assertEqual(library.KEEP_APART_PLATFORMS["Sony PlayStation"]["motor toon grand prix"], [frozenset({"japan"})])


class NotTheSameGameOfTheSweep(AltTitlesCase):
    NAMES = AltTitlesCase.NAMES + [("sims 2", 30, 0), ("sims", 30, 1), ("foo bar", 31, 0), ("foo bar 2", 31, 1)]

    def test_a_pair_of_the_table_is_not_joined_by_the_ratings_data(self) -> None:
        self.learn(["Sims 2, The (USA, Europe)", "Sims, The (Japan)"])
        self.assertEqual(merged(), [])
        with mock.patch.dict(library.NOT_SAME_GAME, {"Nintendo Game Boy Advance": []}):
            alttitles.clear_cache()
            self.learn(["Sims 2, The (USA, Europe)", "Sims, The (Japan)"])
            self.assertEqual(merged(), [["sims 2, the", "sims, the"]])

    def test_numbered_titles_join_only_across_regions(self) -> None:
        self.learn(["Foo Bar (Japan, USA)", "Foo Bar 2 (Japan)"])
        self.assertEqual(merged(), [])
        alttitles.clear_cache()
        self.learn(["Foo Bar (USA)", "Foo Bar 2 (Japan)"])
        self.assertEqual(merged(), [["foo bar", "foo bar 2"]])


class KatakanaTitles(AltTitlesCase):
    """A Japanese release whose title is its English title in katakana (``リトルインフェルノ`` / ``Little Inferno``)."""
    YEARS = {"リトルインフェルノ": {2017}, "little inferno": {2017}, "ロスト・ウィング": {2020}, "lost wing": {2020},
             "ゴルフストーリー": {2018}, "golf story": {2017}, "ボムスリンガー": {2019}, "booom-slang!": {2024},
             "エイト・ドラゴン": {2021}, "tetragon": {2021}, "シカトリス": {2023}, "skittles": {2023}, "skye tales": {2023},
             "デーモンクリスタル": {2018}, "the demon crystal": {2019}, "デーモンクリスタル4": {2021}}

    def years(self, years: dict | None = None) -> None:
        patcher = mock.patch.object(alttitles, "release_years", lambda system: self.YEARS if years is None else years)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_consonants_of_katakana_are_those_of_the_english_title(self) -> None:
        from romorg import kana
        for ja, en in (("リトルインフェルノ", "Little Inferno"), ("ドラゴンクエスト", "Dragon Quest"), ("スプラトゥーン2", "Splatoon 2"),
                       ("ペルソナ５ タクティカ", "Persona 5 Tactica")):
            self.assertEqual(kana.ja_key(ja), kana.en_key(en), ja)
        self.assertEqual(kana.ja_key("北斗の拳"), "")                              # (a kanji: no reading)
        self.assertNotEqual(kana.ja_key("デーモンクリスタル4"), kana.en_key("The Demon Crystal"))

    def test_katakana_of_an_english_title_of_the_same_year_join_it(self) -> None:
        self.years()
        self.learn(["リトルインフェルノ (Japan)", "Little Inferno (USA)", "ロスト・ウィング (Japan)", "Lost Wing (USA)"])
        m = alttitles.dat_map(DAT)
        self.assertEqual(m["リトルインフェルノ"], m["little inferno"])
        self.assertEqual(m["ロスト・ウィング"], m["lost wing"])
        self.assertNotEqual(m["リトルインフェルノ"], m["lost wing"])

    def test_a_year_apart_by_one_still_joins_and_by_more_does_not(self) -> None:
        self.years()
        self.learn(["ゴルフストーリー (Japan)", "Golf Story (USA)", "ボムスリンガー (Japan)", "Booom-Slang! (USA)"])
        m = alttitles.dat_map(DAT)
        self.assertEqual(m["ゴルフストーリー"], m["golf story"])
        self.assertNotIn("ボムスリンガー", m)                                        # (2019 and 2024: another game with the same consonants)

    def test_a_short_skeleton_two_candidates_or_no_year_join_nothing(self) -> None:
        self.years()
        self.learn(["エイト・ドラゴン (Japan)", "Tetragon (USA)",                        # (five consonants: too little to go by)
                    "シカトリス (Japan)", "Skittles (USA)", "Skye Tales (USA)"])         # (two English titles with them)
        self.assertEqual(alttitles.dat_map(DAT), {})
        self.years({})                                                              # (no release dates known)
        alttitles.clear_cache()
        self.learn(["リトルインフェルノ (Japan)", "Little Inferno (USA)"])
        self.assertEqual(alttitles.dat_map(DAT), {})

    def test_a_sequel_stays_apart(self) -> None:
        self.years()
        self.learn(["デーモンクリスタル (Japan)", "The Demon Crystal (USA)", "デーモンクリスタル4 (Japan)"])
        m = alttitles.dat_map(DAT)
        self.assertEqual(m["デーモンクリスタル"], m["the demon crystal"])
        self.assertNotIn("デーモンクリスタル4", m)

    def test_the_same_region_veto_still_applies(self) -> None:
        self.years()
        self.learn(["リトルインフェルノ (Japan)", "Little Inferno (Japan)"])         # (both for Japan: two games)
        self.assertEqual(alttitles.dat_map(DAT), {})


class AGlossAfterTheWords(AltTitlesCase):
    def test_a_long_katakana_text_that_says_the_words_does_not_make_another_game(self) -> None:
        self.learn(["Skyline Bowling - スカイライン・ボウリング (Japan)", "Skyline Bowling (USA)",
                    "Blade of Darkness ブレードオブダークネス (Japan)", "Blade of Darkness (USA)",
                    "Fit Boxing 北斗の拳 ～お前はもう痩せている～ (Japan)", "Fit Boxing (USA)",
                    "Knight's Retreat チェスパズル - ナイツ リトリート (Japan)", "Knight's Retreat (USA)"])
        m = alttitles.dat_map(DAT)
        self.assertEqual(m["skyline bowling - スカイライン・ボウリング"], m["skyline bowling"])
        self.assertEqual(m["blade of darkness ブレードオブダークネス"], m["blade of darkness"])
        self.assertNotIn("fit boxing 北斗の拳 ～お前はもう痩せている～", m)             # (a kanji text: a subtitle)
        self.assertNotIn("knight's retreat チェスパズル - ナイツ リトリート", m)         # (more than the words: another text)

    def test_an_art_book_with_the_words_is_no_release_of_the_game(self) -> None:
        self.learn(["Crystar クライスタ デジタルアートブック (Japan)", "Crystar (USA)"])
        self.assertEqual(alttitles.dat_map(DAT), {})


class TheSwitchReleaseYears(AltTitlesCase):
    def test_only_the_switch_has_release_dates_and_they_come_from_the_title_database(self) -> None:
        import sqlite3
        from romorg import switchdb
        self.assertEqual(alttitles.release_years("Nintendo Game Boy Advance"), {})
        self.assertEqual(alttitles.release_years("Nintendo Switch"), {})                 # (no database)
        path = switchdb.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE titles (id TEXT PRIMARY KEY, name TEXT, publisher TEXT, released INTEGER);"
                          "CREATE TABLE catalogue (dat TEXT, id TEXT, version INTEGER, label TEXT);"
                          "INSERT INTO titles VALUES ('0100000000001000', 'Little Inferno', 'TC', 20170701), ('0100000000002000', 'x', '', 0);"
                          "INSERT INTO catalogue VALUES ('Nintendo - Switch', '0100000000001000', -1, 'Little Inferno (USA) (En)'),"
                          "('Nintendo - Switch', '0100000000002000', -1, 'Nothing Known (USA) (En)'),"
                          "('Nintendo - Switch (DLC)', '0100000000001000', -1, 'Little Inferno Extra (DLC)');")
        con.commit()
        con.close()
        self.assertEqual(alttitles.release_years("Nintendo Switch"), {"little inferno": frozenset({2017})})


if __name__ == "__main__":
    unittest.main()
