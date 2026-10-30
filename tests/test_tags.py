"""Tests for romorg.tags (No-Intro / TOSEC name tags, version ordering, latest-only grouping)."""

from __future__ import annotations

import os
import re
import unittest
from collections import Counter
from pathlib import Path

from romorg import tags
from romorg.tags import parse_name, superseded, supersede_key, to_json, version_key

# ROMORG_REAL_SCRATCH: folder holding dats/TOSEC and nointro (default: the Steam Deck scratch folder)
SCRATCH = Path(os.environ.get("ROMORG_REAL_SCRATCH") or "/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/"
               "cfc5ea37-4261-428c-9422-29acd65cac97/scratchpad")
NOINTRO = SCRATCH / "nointro"
TOSEC_GAMES = SCRATCH / "dats/TOSEC/Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat"


class NoIntroParseTest(unittest.TestCase):
    def test_full_name(self) -> None:
        t = parse_name("Super Mario World (USA, Europe) (En,Fr,De) (Rev 1) (Beta 2) (Unl) [b].sfc")
        self.assertEqual(t.title, "Super Mario World")
        self.assertEqual(t.regions, ("USA", "Europe"))
        self.assertEqual(t.languages, ("En", "Fr", "De"))
        self.assertFalse(t.languages_implied)
        self.assertEqual((t.version, t.version_key), ("Rev 1", (1, 1)))
        self.assertEqual(t.status, "Beta 2")
        self.assertEqual(t.flags, ("Unl",))
        self.assertEqual(t.dump_flags, ("b",))
        self.assertTrue(t.bad)
        self.assertFalse(t.bios)
        self.assertEqual(t.video, ("NTSC", "PAL"))
        self.assertEqual(t.style, "nointro")

    def test_implied_languages_video_and_bios(self) -> None:
        t = parse_name("Game Boy Advance BIOS (World) [BIOS]")
        self.assertEqual(t.languages, ("En",))
        self.assertTrue(t.languages_implied)
        self.assertTrue(t.bios)
        self.assertEqual(t.video, ("NTSC", "PAL"))  # World -> both
        self.assertEqual(parse_name("X (Japan)").languages, ("Ja",))
        self.assertEqual(parse_name("X (Japan)").video, ("NTSC",))
        self.assertEqual(parse_name("X (Germany, Austria)").languages, ("De",))
        self.assertEqual(parse_name("X (Asia)").video, ())
        self.assertEqual(parse_name("X (Europe) (NTSC)").video, ("NTSC",))  # explicit tag wins
        self.assertEqual(parse_name("X (USA) (PAL-NTSC)").video, ("NTSC", "PAL"))

    def test_leading_bios_prefix(self) -> None:
        # DAT-o-MATIC puts [BIOS] in front of the title
        t = parse_name("[BIOS] Nintendo Game Boy Advance Boot ROM (World) (Rev 1).gba")
        self.assertEqual(t.title, "Nintendo Game Boy Advance Boot ROM")
        self.assertTrue(t.bios)
        self.assertEqual(t.dump_flags, ("BIOS",))
        self.assertEqual((t.regions, t.version), (("World",), "Rev 1"))
        t = parse_name("[BIOS] Nintendo Game Boy Advance - Game Boy Color Mode Boot ROM (World) (Rev 1)")
        self.assertEqual(t.title, "Nintendo Game Boy Advance - Game Boy Color Mode Boot ROM")
        self.assertTrue(t.bios)
        self.assertFalse(parse_name("[b] Other (USA)").bios)
        self.assertEqual(parse_name("[b] Other (USA)").title, "Other")
        self.assertEqual(supersede_key(parse_name("[BIOS] X (World)")),
                         supersede_key(parse_name("[BIOS] X (World) (Rev 1)")))

    def test_a_trailing_dot_after_the_last_tag_is_ignored(self) -> None:
        # the current No-Intro Game Boy DAT has one entry like this; without the fix it parsed as a title with no region
        t = parse_name("Rex Run (World) (Aftermarket) (Unl).")
        self.assertEqual((t.title, t.regions, t.flags), ("Rex Run", ("World",), ("Aftermarket", "Unl")))
        self.assertEqual(parse_name("Dr. Mario (World)").title, "Dr. Mario")

    def test_mpal_is_60hz_video(self) -> None:
        t = parse_name("Pyoro 64 (Unknown) (MPAL) (Aftermarket) (Unl).z64")
        self.assertEqual(t.video, ("NTSC",))
        self.assertEqual(tags.flag_kind("MPAL"), "video")

    def test_language_groups(self) -> None:
        t = parse_name("W (Europe) (En,Fr,De,Es,It,Nl+En,Fr,De,Es,It,Nl,Sv,No,Da,Fi).gba")
        self.assertEqual(t.languages, ("En", "Fr", "De", "Es", "It", "Nl", "Sv", "No", "Da", "Fi"))
        t = parse_name("B (Brazil) (En,Pt-BR) (Unl)")
        self.assertEqual(t.languages, ("En", "Pt"))
        self.assertIn("Pt-BR", t.flags)
        # second language-looking group is a flag
        self.assertEqual(parse_name("X (USA) (En) (Fr)").flags, ("Fr",))

    def test_versions_status_flags(self) -> None:
        self.assertEqual(parse_name("X (USA) (v1.1)").version, "v1.1")
        self.assertEqual(parse_name("X (Japan) (REV-B)").version, "REV-B")
        self.assertEqual(parse_name("X (USA) (V1.1)").version, "V1.1")
        self.assertEqual(parse_name("X (USA) (v1.0, v1.2)").version, "v1.2")
        self.assertEqual(parse_name("X (USA) (Proto 1)").status, "Proto 1")
        self.assertEqual(parse_name("X (USA) (Tech Demo)").status, "Tech Demo")
        t = parse_name("Aidyn Chronicles (USA) (Beta) (2000-02-10)")
        self.assertEqual((t.status, t.flags), ("Beta", ("2000-02-10",)))
        t = parse_name("X (USA) (Virtual Console) (Alt)")
        self.assertEqual(t.flags, ("Virtual Console", "Alt"))
        self.assertEqual(t.version, "")

    def test_title_edge_cases(self) -> None:
        self.assertEqual(parse_name("Xin Bao (Gedou Ban) - Super King (China) (Pirate)").title,
                         "Xin Bao (Gedou Ban) - Super King")
        self.assertEqual(parse_name("Lester - Le (^^; (Japan)").title, "Lester - Le (^^;")
        self.assertEqual(parse_name("Sansu 5 Nen (Jou) (Japan).nes").title, "Sansu 5 Nen (Jou)")
        self.assertEqual(parse_name("Dr. Mario (Japan, USA)").title, "Dr. Mario")
        self.assertEqual(parse_name("No Tags At All.gba").title, "No Tags At All")

    def test_cached_and_json(self) -> None:
        a = parse_name("Zelda (USA) (Rev 1).nes")
        self.assertIs(a, parse_name("Zelda (USA) (Rev 1).nes"))
        self.assertEqual(to_json(a), {
            "regions": ["USA"], "languages": ["En"], "languages_implied": True, "variant_languages": ["En"], "version": "Rev 1",
            "status": "", "flags": [], "dump_flags": [], "bad": False, "bios": False, "video": ["NTSC"],
            "bad_flags": [], "excluded_by": []})

    def test_tables(self) -> None:
        self.assertEqual(tags.REGIONS["Australia"], "PAL")
        self.assertEqual(tags.REGIONS["Brazil"], "NTSC")
        self.assertEqual(tags.REGIONS["World"], "")
        self.assertEqual(tags.TOSEC_COUNTRIES["GB"], "United Kingdom")
        self.assertTrue(all(c in tags.REGIONS for c in tags.TOSEC_COUNTRIES.values()))
        self.assertTrue(all(lang in tags.LANGUAGES for lang in tags.REGION_LANGUAGE.values()))
        self.assertEqual(len(tags.LANGUAGES), 43)

    def test_every_tosec_country_is_a_region_with_its_language(self) -> None:
        for code, lang in (("SK", "Sk"), ("SI", "Sl"), ("YU", "Sr"), ("RO", "Ro"), ("BG", "Bg"), ("LT", "Lt"), ("LV", "Lv"), ("EE", "Et"),
                           ("IL", "He"), ("IS", "Is"), ("TH", "Th"), ("CZ", "Cs"), ("HU", "Hu")):
            t = parse_name(f"Hra (1995)(Pub)({code}).adf", tags.STYLE_TOSEC)
            self.assertEqual((t.regions, t.flags, tags.variant_languages(t)), ((tags.TOSEC_COUNTRIES[code],), ("Pub",), {lang}), code)
        # (No-Intro: a region it has, never a product tag that parts a game)
        for region in ("Israel", "Thailand", "Singapore", "Ukraine", "Slovakia", "Bulgaria"):
            self.assertEqual(tags.game_key(parse_name(f"Game (USA, {region}).gba")), tags.game_key(parse_name("Game (USA).gba")), region)

    def test_flag_kind(self) -> None:
        self.assertEqual(tags.flag_kind("Unl"), "license")
        self.assertEqual(tags.flag_kind("2000-02-10"), "date")
        self.assertEqual(tags.flag_kind("Wii Virtual Console"), "distribution")
        self.assertEqual(tags.flag_kind("Alt 2"), "alt")
        self.assertEqual(tags.flag_kind("Tengen"), "other")
        self.assertEqual(tags.flag_kind("Rumble Version"), "feature")             # (a release of the same game)
        self.assertEqual(tags.flag_kind("Rumble, Frank"), "other")        # a TOSEC author, not a feature


class TosecParseTest(unittest.TestCase):
    def test_full_name(self) -> None:
        t = parse_name("Lotus v1.2 (demo) (1991)(Gremlin)(DE)(de-en)(AGA)(Disk 1 of 2)[cr FLT][a2][!].adf",
                       "tosec")
        self.assertEqual(t.title, "Lotus")
        self.assertEqual(t.version, "v1.2")
        self.assertEqual(t.status, "demo")
        self.assertEqual(t.date, "1991")
        self.assertEqual(t.regions, ("Germany",))
        self.assertEqual(t.languages, ("De", "En"))
        self.assertFalse(t.languages_implied)
        self.assertEqual(t.flags, ("Gremlin", "AGA", "Disk 1 of 2"))
        self.assertEqual(t.dump_flags, ("cr FLT", "a2", "!"))
        self.assertEqual(t.video, ("PAL",))
        self.assertFalse(t.bad)
        self.assertEqual(t.style, "tosec")

    def test_misc(self) -> None:
        t = parse_name("Game, The (1990)(Pub)(PAL)(M4)[b corrupt file]", "tosec")
        self.assertEqual((t.title, t.version, t.flags, t.video, t.bad),
                         ("Game, The", "", ("Pub", "PAL", "M4"), ("PAL",), True))
        self.assertEqual(parse_name("G (19xx)(-)(US-GB)", "tosec").regions, ("USA", "United Kingdom"))
        t = parse_name("A320 Airbus v1.3GE (1992-08-08)(Thalion)", "tosec")
        self.assertEqual((t.version, t.flags, t.date), ("v1.3", ("GE", "Thalion"), "1992-08-08"))
        t = parse_name("BM v1.3 rev2 (1989)(Software 2000)(DE)", "tosec")
        self.assertEqual((t.version, t.version_key), ("v1.3 rev2", (1, 1, 3, 0, 2)))
        self.assertEqual(parse_name("G v1.009 r30 (1990)(P)", "tosec").version, "v1.009 r30")


class StripExtTest(unittest.TestCase):
    def test_it_does_what_the_regex_does(self) -> None:
        for name in ("Game (1990)(Pub).adf", "Game.v1.2.zip", "Game.toolongext", "Game.", ".hidden", "Game", "", "a.b.c", "Game.\u0660ab",
                     "Game.ab\n", "Game.ab cd", "Game (USA).sfc", "x.12345", "x.123456"):
            self.assertEqual(tags.strip_ext(name), tags._EXT_RE.sub("", name), repr(name))


class VersionTest(unittest.TestCase):
    def test_order(self) -> None:
        ordered = ["", "Rev 1", "Rev 2", "Rev 10"]
        self.assertEqual(sorted(ordered, key=version_key), ordered)
        self.assertLess(version_key("Rev A"), version_key("Rev B"))
        self.assertLess(version_key("REV-A"), version_key("REV-B"))
        self.assertEqual(version_key("Rev B"), version_key("REV-B"))
        self.assertLess(version_key("v1.1"), version_key("v1.2"))
        self.assertLess(version_key("v1.9"), version_key("v1.10"))
        self.assertEqual(version_key("v1.02"), (1, 1, 2))
        self.assertLess(version_key("v1.0.1"), version_key("v1.1"))
        self.assertLess(version_key("v1.2"), version_key("v1.2a"))
        self.assertLess(version_key(""), version_key("v1.0"))
        self.assertLess(version_key("v2.0-alpha9"), version_key("v2.0-alpha10"))
        self.assertLess(version_key("v2.0-alpha10"), version_key("v2.0-beta1"))
        self.assertLess(version_key("v2.0-rc4"), version_key("v2.0"))
        self.assertLess(version_key("v1.9"), version_key("v2.0-alpha1"))
        self.assertLess(version_key("v1.3"), version_key("v1.3 rev1"))
        self.assertLess(version_key("Rev 1.2"), version_key("Rev 1.4"))


class OtherVersionForms(unittest.TestCase):
    def test_versions_the_first_pattern_does_not_know(self) -> None:
        for name, title, version in (
                ("Uridium 2 vP1.00-D1.00 (1993)(Renegade)(Disk 1 of 2)[cr CLS].adf", "Uridium 2", "vP1.00-D1.00"),
                ("Uridium 2 v0.90-1.00 (1993)(Renegade)(AGA)(Disk 1 of 2)[cr FLT].adf", "Uridium 2", "v0.90-1.00"),
                ("Civilization v855e.01 (1993)(MicroProse)(AGA)(Disk 1 of 4)[cr CSL].adf", "Civilization", "v855e.01"),
                ("Space Quest II - Vohaul's Revenge v2.0f i2.202 (1988)(Sierra).adf", "Space Quest II - Vohaul's Revenge", "v2.0f i2.202"),
                ("Reach for the Skies v1.0D3 (1993)(Virgin).adf", "Reach for the Skies", "v1.0D3"),
                ("Dream Zone v3.1 29701 (1988)(Baudville).adf", "Dream Zone", "v3.1 29701"),
                ("Whale's Voyage v1.2 - Super Enhanced (1993)(neo - Flair).adf", "Whale's Voyage - Super Enhanced", "v1.2"),
                ("Falcon v1.3 r2.0 - Mission Disk Volume II - Operation Firefight (1990)(Mirrorsoft).adf",
                 "Falcon - Mission Disk Volume II - Operation Firefight", "v1.3 r2.0"),
                ("Gunship 2000 v3.32 CD32 (1994)(MicroProse)[CD32 rip].adf", "Gunship 2000 CD32", "v3.32"),
                ("Death or Glory v1.0 Update Disk (1994)(Software 2000).adf", "Death or Glory v1.0 Update Disk", "")):   # (left as it is)
            t = parse_name(name, tags.STYLE_TOSEC)
            self.assertEqual((t.title, t.version), (title, version), name)
        t = parse_name("Dragon Wars v1.2-G (1990)(Interplay).adf", tags.STYLE_TOSEC)
        self.assertEqual((t.title, t.version, t.flags[0]), ("Dragon Wars", "v1.2", "G"))
        self.assertLess(version_key("v0.90-1.00"), version_key("vP1.00-D1.00"))

    def test_the_other_forms_order_against_the_plain_ones(self) -> None:
        # (their leading "vX.Y<letter>" is read as the plain form's: its letter, and the widths of the versions next to it)
        self.assertLess(version_key("v855e"), version_key("v855e.01"))
        self.assertLess(version_key("v2.0c"), version_key("v2.0f i2.202"))
        for older, newer in (("v1.05", "v1.1+"), ("v1.05", "v1.3 29701")):
            keys = tags.group_version_keys([f"Game {older} (1990)(X).adf", f"Game {newer} (1990)(X).adf"], tags.STYLE_TOSEC)
            self.assertLess(keys[f"Game {older} (1990)(X).adf"], keys[f"Game {newer} (1990)(X).adf"], newer)

    def test_a_version_letter_parts_no_game_when_titles_are_combined(self) -> None:
        from romorg import library
        k = lambda n: library.combined_key("Commodore Amiga - Games - [ADF]", tags.identity_key(parse_name(n, tags.STYLE_TOSEC)), True)
        self.assertEqual(k("Wolfpack v1.00 (1990)(Broderbund)(US).adf"), k("Wolfpack v1.00R (1990)(Mirrorsoft).adf"))
        self.assertEqual(k("Championship Manager '93 v1.10 (1993)(Domark).adf"), k("Championship Manager '93 v1.14C (1993)(Domark).adf"))
        self.assertNotEqual(tags.identity_key(parse_name("A320 Airbus v1.44GE (1994)(Thalion).adf", tags.STYLE_TOSEC)),
                            tags.identity_key(parse_name("A320 Airbus v1.44E (1994)(Thalion).adf", tags.STYLE_TOSEC)))   # (not combined: apart)


class SupersededTest(unittest.TestCase):
    def test_nointro_groups(self) -> None:
        names = [
            "Zelda (USA).nes", "Zelda (USA) (Rev 1).nes", "Zelda (USA) (Rev 2).nes",
            "Zelda (Europe).nes",                      # other region: never mixed
            "Zelda (USA) (Beta).nes",                  # beta: own group
            "Zelda (USA) (Beta) (Rev 1).nes",
            "Zelda (USA) (Virtual Console).nes",       # a re-release of the same game: the newest revision of the region wins
            "Mario (USA) (v1.9).gba", "Mario (USA) (v1.10).gba",
            "Solo (Japan) (Rev A).sfc", "Solo (Japan) (Rev B).sfc",
        ]
        self.assertEqual(superseded(names), {
            "Zelda (USA).nes": "Zelda (USA) (Rev 2).nes",
            "Zelda (USA) (Rev 1).nes": "Zelda (USA) (Rev 2).nes",
            "Zelda (USA) (Virtual Console).nes": "Zelda (USA) (Rev 2).nes",
            "Zelda (USA) (Beta).nes": "Zelda (USA) (Beta) (Rev 1).nes",
            "Mario (USA) (v1.9).gba": "Mario (USA) (v1.10).gba",
            "Solo (Japan) (Rev A).sfc": "Solo (Japan) (Rev B).sfc",
        })
        # equal versions never supersede each other; duplicates in the input are harmless
        self.assertEqual(superseded(["A (USA).gba", "A (USA).zip", "A (USA).gba"]), {})
        # explicit languages are part of the key, implied ones are not
        self.assertEqual(superseded(["A (Europe) (En,Fr)", "A (Europe) (En,De) (Rev 1)"]), {})
        self.assertEqual(superseded(["A (USA)", "A (USA) (En) (Rev 1)"]), {})
        self.assertEqual(supersede_key(parse_name("A (USA) (Rev 3)")), supersede_key(parse_name("A (USA)")))

    def test_zero_padded_versions_are_fractions(self) -> None:
        # real libretro families: a leading zero means a decimal fraction (v1.02 < v1.1)
        w02, w1 = ("Wordle (World) (v1.02) (Digital) (Aftermarket) (Unl).nes",
                   "Wordle (World) (v1.1) (Digital) (Aftermarket) (Unl).nes")
        self.assertEqual(superseded([w02, w1]), {w02: w1})
        z = [f"Legend of Zelda, The - The Sealed Palace (World) ({v}) (Aftermarket) (Pirate).z64"
             for v in ("v1.01", "v1.21", "v1.3")]
        self.assertEqual(superseded(z), {z[0]: z[2], z[1]: z[2]})
        f = ["Falling (World) (Aftermarket) (Unl).nes", "Falling (World) (v1.01) (Aftermarket) (Unl).nes",
             "Falling (World) (v1.1) (Aftermarket) (Unl).nes"]
        self.assertEqual(superseded(f), {f[0]: f[2], f[1]: f[2]})
        cube = [f"CUBE (World) ({v}) (Aftermarket) (Unl).nes" for v in ("v1.04", "v1.07.3", "v1.08.1")]
        self.assertEqual(superseded(cube), {cube[0]: cube[2], cube[1]: cube[2]})
        g = ["Galibot, Le (World) (Fr) (v1.03) (Aftermarket) (Unl).nes",
             "Galibot, Le (World) (Fr) (v1.4) (Aftermarket) (Unl).nes"]
        self.assertEqual(superseded(g), {g[0]: g[1]})
        # without zero padding the integer order stays (v1.9 < v1.10)
        self.assertEqual(superseded(["M (USA) (v1.9)", "M (USA) (v1.10)"]), {"M (USA) (v1.9)": "M (USA) (v1.10)"})

    def test_tosec_groups(self) -> None:
        names = [
            "Elite v1.0 (1988)(Firebird)[cr].adf", "Elite v2.0 (1989)(Firebird)[cr].adf",
            "Elite v1.0 (1988)(Firebird)[cr X].adf",       # other crack: own group
            "Elite v1.0 (1988)(Firebird)[!].adf",
            "Elite v1.1 (1988)(Firebird).adf",             # [!] ignored in the key
            "Elite v1.5 (1988)(Other Pub).adf",            # publisher is part of the key
            "Elite v1.0 (1988)(Firebird)[h Foo].adf",
        ]
        self.assertEqual(superseded(names, "tosec"), {
            "Elite v1.0 (1988)(Firebird)[cr].adf": "Elite v2.0 (1989)(Firebird)[cr].adf",
            "Elite v1.0 (1988)(Firebird)[!].adf": "Elite v1.1 (1988)(Firebird).adf",
        })

    def test_disk_sets(self) -> None:
        old = ["G v1.0 (1990)(P)(Disk 1 of 2).adf", "G v1.0 (1990)(P)(Disk 2 of 2).adf"]
        new = ["G v1.1 (1990)(P)(Disk 1 of 2).adf", "G v1.1 (1990)(P)(Disk 2 of 2).adf"]
        self.assertEqual(superseded(old + new, "tosec"), dict(zip(old, new)))
        # newer version incomplete locally -> nothing superseded
        self.assertEqual(superseded(old + new[:1], "tosec"), {})
        # newer version with a per-disk label and a different disk count
        new3 = ["G v2.0 (1991)(P)(Disk 1 of 3)(Boot).adf", "G v2.0 (1991)(P)(Disk 2 of 3).adf",
                "G v2.0 (1991)(P)(Disk 3 of 3).adf"]
        res = superseded(old + new3, "tosec")
        self.assertEqual(res, {old[0]: new3[0], old[1]: new3[1]})
        self.assertEqual(superseded(old + new3[:2], "tosec"), {})


class RealDatCoverageTest(unittest.TestCase):
    @unittest.skipUnless(NOINTRO.is_dir(), "real No-Intro DATs not available")
    def test_nointro_coverage(self) -> None:
        from romorg.datfile import parse_dat
        total = with_other = 0
        other: Counter[str] = Counter()
        for path in sorted(NOINTRO.glob("*.dat")):
            for set_name in parse_dat(path).sets():
                t = parse_name(set_name)
                total += 1
                self.assertTrue(t.regions, set_name)  # every No-Intro name has a region
                tail = set_name[len(t.title):].rstrip(".")      # one Game Boy entry ends "(Unl)."
                self.assertEqual(re.sub(r"\s*(\([^()]*\)|\[[^\[\]]*\])", "", tail).strip(), "", set_name)
                odd = [f for f in t.flags if tags.flag_kind(f) == "other"]
                with_other += bool(odd)
                other.update(odd)
        pct = 100.0 * with_other / max(total, 1)
        print(f"\n  No-Intro tags: {total} names, {pct:.1f}% with free-text flags "
              f"(top: {', '.join(k for k, _ in other.most_common(5))})", end=" ")
        self.assertLess(pct, 6.0)

    @unittest.skipUnless(TOSEC_GAMES.is_file(), "real TOSEC DAT not available")
    def test_tosec_versions(self) -> None:
        from romorg.datfile import parse_dat
        dat = parse_dat(TOSEC_GAMES)
        versions = sum(1 for r in dat.roms if r.tags.version)
        dated = sum(1 for r in dat.roms if r.tags.date)
        self.assertGreater(versions, 7400)
        self.assertEqual(dated, len(dat.roms))
        sup = superseded([r.name for r in dat.roms], "tosec")
        self.assertGreater(len(sup), 100)
        for old, new in sup.items():
            self.assertLess(parse_name(old, "tosec").version_key, parse_name(new, "tosec").version_key)


if __name__ == "__main__":
    unittest.main()


class EditionsAndDemosTest(unittest.TestCase):
    """Editions, budget re-releases, store editions and bundles are the same game as the plain release, which they never beat;
    the demo, preview and trial discs every disc system has are demos."""

    def test_an_edition_is_the_same_game_and_the_plain_release_wins(self) -> None:
        for edition in ("Genteiban", "@barai", "Premium Box", "PlayStation 2 the Best", "Toys R Us", "With GunCon2", "Controller Set", "EDC"):
            plain, other = tags.redump_tags("Game (Japan)"), tags.redump_tags(f"Game (Japan) ({edition})")
            self.assertEqual(tags.game_key(plain), tags.game_key(other), edition)
        self.assertEqual(tags.superseded(["Jackal (World)", "Jackal (World) (Konami Collector's Series)"]),
                         {"Jackal (World) (Konami Collector's Series)": "Jackal (World)"})
        self.assertEqual(tags.superseded(["Zap (USA) (Walmart Edition)", "Zap (USA) (Rev 1)"]), {"Zap (USA) (Walmart Edition)": "Zap (USA) (Rev 1)"})

    def test_a_premium_edition_is_preferred_a_budget_release_comes_last(self) -> None:
        for premium in ("Special Edition", "Limited Edition", "Collector's Edition", "Premium Box", "Genteiban", "Disney Classic Games"):
            self.assertEqual(tags.edition_kind(premium), "premium", premium)
        for budget in ("Greatest Hits", "Platinum", "PlayStation 2 the Best", "@barai", "Major Wave"):
            self.assertEqual(tags.edition_kind(budget), "budget", budget)
        for other in ("Bonus Disc", "Shenmue Passport", "Simulation Mode"):            # (other discs, other products: no release of the same game)
            self.assertEqual(tags.edition_kind(other), "", other)
        ranks = {n: tags.edition_rank(tags.redump_tags(f"Game (USA){n}")) for n in ("", " (Special Edition)", " (Toys R Us)", " (Greatest Hits)")}
        self.assertEqual(sorted(ranks, key=ranks.get), [" (Special Edition)", "", " (Toys R Us)", " (Greatest Hits)"])
        self.assertEqual(tags.edition_rank(tags.redump_tags("Game (USA) (Special Edition)"), prefer_premium=False), 2)   # (off: after the plain one)
        self.assertEqual(tags.superseded(["Zap (USA)", "Zap (USA) (Limited Edition)", "Zap (USA) (Greatest Hits)"]),
                         {"Zap (USA)": "Zap (USA) (Limited Edition)", "Zap (USA) (Greatest Hits)": "Zap (USA) (Limited Edition)"})

    def test_budget_releases_and_online_only_titles_are_rules(self) -> None:
        rules = lambda n: {r for r, _x in tags.exclusion_info(tags.redump_tags(n, ""))}     # noqa: E731
        self.assertEqual(rules("Naruto (Japan) (PlayStation 2 the Best)"), {"budget"})
        self.assertEqual(rules("Shenmue (USA) (Shenmue Passport)"), {"online"})
        self.assertEqual(rules("Final Fantasy XI - Online (USA, Canada)"), {"online"})
        self.assertEqual(rules("Phantasy Star Online (USA) (En,Ja,Fr,De,Es)"), set())      # (it has an offline mode: not online only)

    def test_previews_samples_and_trials_are_demos_without_a_category(self) -> None:
        for name in ("Pikmin (Japan) (Jitsuen-you Sample)", "Resident Evil 4 (USA) (Preview Disc)", "P.N. 03 (Japan) (Demo Eizou)",
                     "Kororinpa 2 (Japan) (Otameshiban)", "Akuji the Heartless (USA) (Trade Demo)", "SSX 3 (Korea) (Cheheompan)",
                     "Star Wars - Rogue Squadron III - Rebel Strike (USA) (Limited Edition Preview Disc)"):
            rules = [e["rule"] for e in tags.to_json(tags.redump_tags(name, "")).get("excluded_by", [])]
            self.assertIn("demo", rules, name)


class OneGameManyReleases(unittest.TestCase):
    """Releases of one game that are no game of their own (found checking every system's top 250 for duplicates)."""

    @staticmethod
    def key(name: str, style: str = "nointro") -> tuple:
        return tags.game_key(tags.redump_tags(name) if style == "redump" else parse_name(name, style))

    def test_console_features_and_re_releases_are_the_same_game(self) -> None:
        for other in ("Foo (USA) (SGB Enhanced) (GB Compatible)", "Foo (USA) (Sega Channel)", "Foo (USA) (Mega Drive Mini, Genesis Mini)",
                      "Foo (World) (Contra Anniversary Collection)", "Foo (USA) (Sega Smash Pack 2)", "Foo (USA) (Rumble Version)",
                      "Foo (Europe) (Joypad)"):
            self.assertEqual(self.key(other), self.key("Foo (USA)"), other)
        self.assertEqual(tags.edition_kind("Sega Channel"), "store")
        self.assertEqual(tags.edition_kind("Koei the Best"), "budget")
        self.assertEqual(tags.edition_kind("Shindou Edition"), "premium")

    def test_tosec_versions_of_every_form_are_no_part_of_the_title(self) -> None:
        for name, title, version in (("Carrier Command vA.12 (1988-08-19)(Rainbird)", "Carrier Command", "vA.12"),
                                     ("Liberation - Captive II v2.02-2.00c (1994-04-08)(Mindscape)", "Liberation - Captive II", "v2.02-2.00c"),
                                     ("Bridge 6.0 r2.49b (1990)(Artworx)", "Bridge 6.0", "r2.49b"),
                                     ("Faery Tale Adventure, The - release 2 (1987)(MicroIllusions)", "Faery Tale Adventure, The", "release 2"),
                                     ("Uridium 2 vP1.00-D1.00 (1993)(Renegade)", "Uridium 2", "vP1.00-D1.00")):
            t = parse_name(name + ".adf", "tosec")
            self.assertEqual((t.title, t.version), (title, version), name)
        self.assertLess(version_key("release 2"), version_key("release 10"))

    def test_pirate_unlicensed_and_publisher_releases_are_the_game_but_the_known_other_games_are_not(self) -> None:
        plain = self.key("Contra (USA)")
        self.assertEqual(self.key("Contra (Asia) (En) (Kaiser) (Pirate)"), plain)
        self.assertEqual(self.key("Contra (Taiwan) (En) (Unl)"), plain)
        self.assertTrue(tags.unlicensed(parse_name("Contra (Taiwan) (En) (Unl)", "nointro")))
        # one release per game: Tengen's unlicensed carts, another region's publisher, a board, a cart size, a re-release label
        for a, b in (("Gauntlet (USA) (V1.1) (Unl)", "Gauntlet (USA)"), ("Klax (USA) (Unl)", "Klax (Japan)"),
                     ("Medal of Honor - Underground (Europe) (En,Fr,Es,It) (Ubi Soft)", "Medal of Honor - Underground (USA)"),
                     ("Pyramid (USA) (NINA-03) (Unl)", "Pyramid (USA) (NINA-06) (Unl)"), ("Rod Land (World) (Retro-Bit) (256KiB)", "Rod Land (World) (Retro-Bit) (512KiB)"),
                     ("Felix the Cat (USA) (Hudson)", "Felix the Cat (World) (En)"), ("Adam & Eve (Spain) (En) (Unl)", "Adam & Eve (Taiwan) (En) (Unl)")):
            self.assertEqual(self.key(a), self.key(b), a)
        # different games of one title stay apart (tags.KEEP_APART_PRODUCTS)
        tetris = {self.key(n) for n in ("Tetris (USA)", "Tetris (USA) (Tengen) (Unl)", "Tetris (Japan) (En) (Rev 2) (Bullet-Proof)")}
        self.assertEqual(len(tetris), 3)
        self.assertNotEqual(self.key("Star Wars (Japan) (Namco)"), self.key("Star Wars (Japan) (Victor)"))
        self.assertEqual(self.key("Star Wars (Japan) (Victor)"), self.key("Star Wars (USA)"))
        self.assertNotEqual(self.key("Airwolf (Japan) (Kyugo)"), self.key("Airwolf (USA) (Acclaim)"))

    def test_homebrew_versions_are_one_game_and_not_the_licensed_one(self) -> None:
        a = self.key("Block Dude (World) (v1.01) (NESDev 2018) (Aftermarket) (Unl)")
        self.assertEqual(a, self.key("Block Dude (World) (v1.02) (Sly Dog Studios) (UxROM) (Aftermarket) (Unl)"))
        self.assertNotEqual(self.key("Pac-Man (World) (Aftermarket) (Unl)"), self.key("Pac-Man (USA)"))

    def test_discs_and_their_labels_are_one_game(self) -> None:
        self.assertEqual(self.key("Biohazard 2 (Japan) (Disc 1) (Leon-hen)", "redump"), self.key("Biohazard 2 (Japan) (Disc 2)", "redump"))
        self.assertEqual(self.key("Resident Evil 4 (USA) (Disc 1)"), self.key("Resident Evil 4 (USA) (Disc 2)"))   # (GameCube, Wii)

    def test_trials_kiosk_downloads_and_trailers_are_demos(self) -> None:
        for n in ("Biohazard 4 (Japan) (En,Ja) (Taikenban)", "Mario Party DS (USA) (Video)", "Sonic Rush (USA, Europe) (Multiplayer) (DS Broadcast)",
                  "Professor Layton (USA) (Video) (DS Download Station Vol. 8)", "Super Bomberman 2 - Caravan You Taikenban (Japan)",
                  "Naruto (Japan) (Taikai Sen'you Disc)"):
            self.assertIn("demo", tags.exclusion_rules(parse_name(n, "nointro")), n)

    def test_bonus_discs_disc_ids_and_language_spellings(self) -> None:
        self.assertTrue(tags.bonus_disc(tags.redump_tags("Lunar 2 - Eternal Blue Complete (USA) (The Making of)")))
        self.assertTrue(tags.bonus_disc(parse_name("Pokemon Colosseum (USA) (Bonus Disc)", "nointro")))
        self.assertFalse(tags.bonus_disc(parse_name("Pokemon Colosseum (USA)", "nointro")))
        self.assertEqual(self.key("Call of Duty - Ghosts (Europe) [ACPP52]"), self.key("Call of Duty - Ghosts (USA)"))
        self.assertEqual(parse_name("LEGO Dimensions (Europe) (En,Fr,De,Nl,Dk)", "nointro").languages, ("En", "Fr", "De", "Nl", "Da"))
