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
            "regions": ["USA"], "languages": ["En"], "languages_implied": True, "version": "Rev 1",
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

    def test_flag_kind(self) -> None:
        self.assertEqual(tags.flag_kind("Unl"), "license")
        self.assertEqual(tags.flag_kind("2000-02-10"), "date")
        self.assertEqual(tags.flag_kind("Wii Virtual Console"), "distribution")
        self.assertEqual(tags.flag_kind("Alt 2"), "alt")
        self.assertEqual(tags.flag_kind("Tengen"), "other")


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


class SupersededTest(unittest.TestCase):
    def test_nointro_groups(self) -> None:
        names = [
            "Zelda (USA).nes", "Zelda (USA) (Rev 1).nes", "Zelda (USA) (Rev 2).nes",
            "Zelda (Europe).nes",                      # other region: never mixed
            "Zelda (USA) (Beta).nes",                  # beta: own group
            "Zelda (USA) (Beta) (Rev 1).nes",
            "Zelda (USA) (Virtual Console).nes",       # other flag: own group
            "Mario (USA) (v1.9).gba", "Mario (USA) (v1.10).gba",
            "Solo (Japan) (Rev A).sfc", "Solo (Japan) (Rev B).sfc",
        ]
        self.assertEqual(superseded(names), {
            "Zelda (USA).nes": "Zelda (USA) (Rev 2).nes",
            "Zelda (USA) (Rev 1).nes": "Zelda (USA) (Rev 2).nes",
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
                tail = set_name[len(t.title):]
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
