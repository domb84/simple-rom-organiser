"""Tests for romorg.library (the Build library selection) and the tags classification it uses."""

from __future__ import annotations

import random
import unittest
from dataclasses import replace
from pathlib import Path

from romorg import library, platforms, tags
from romorg.datfile import Rom
from romorg.library import EXCLUDED, INCOMPLETE, KEEP, SUPERSEDED, Item, LibraryProfile

GAMES = "Commodore Amiga - Games - [ADF]"
WB = "Commodore Amiga - Operating Systems - Workbench"
KSD = "Commodore Amiga - Kickstart-Disks"
AMIGA = platforms.get_platform("Commodore Amiga")
RESCUE = replace(library.default_profile(AMIGA), rescue_only_dump=True)
ALL_LANGS = replace(library.default_profile(AMIGA), languages=())


def amiga_items(names: list[str], dat: str = GAMES) -> list[Item]:
    return [Item(key=i, dat=dat, rom=Rom(name=n + ".adf", size=1, crc="", md5="", sha1="", game=n, dat=dat),
                 style="tosec", path=Path(n), member=None) for i, n in enumerate(names, 1)]


def ni_items(names: list[str], dat: str) -> list[Item]:
    return [Item(key=i, dat=dat, rom=Rom(name=n + ".gba", size=1, crc="", md5="", sha1="", game=n, dat=dat,
                                          set_name=n), style="nointro", path=Path(n), member=None)
            for i, n in enumerate(names, 1)]


def run(names: list[str], profile: LibraryProfile | None = None, dat: str = GAMES):
    items = amiga_items(names, dat)
    sel = library.select(items, profile or library.default_profile(AMIGA), AMIGA)
    by = {names[k - 1]: d for k, d in sel.decisions.items()}
    return items, sel, by


def actions(by: dict) -> dict[str, str]:
    return {n: d.action for n, d in by.items()}


ABC1 = "ABC Monday Night Football v1.1 (1991)(Data East)(US)(Disk 1 of 3)[cr SR]"
ABC_OLD1 = "ABC Monday Night Football (1990)(Data East)(US)(Disk 1 of 3)[cr SR]"
ABC2 = "ABC Monday Night Football (1990)(Data East)(US)(Disk 2 of 3)"
ABC3 = "ABC Monday Night Football (1990)(Data East)(US)(Disk 3 of 3)"
ABC_PRE = [f"ABC Monday Night Football (1990)(Data East)(US)(pre-release)(Disk {i} of 3)" for i in (1, 2, 3)]
ABC_BAD = "ABC Monday Night Football v1.1 (1991)(Data East)(US)(Disk 1 of 3)[cr SR][b corrupt file]"


class TokenTable(unittest.TestCase):
    def test_every_real_token(self) -> None:
        table = {
            "bad_dump": [("[", "b"), ("[", "b1"), ("[", "b corrupt file"), ("[", "b2 dump"), ("[", "b doscopy")],
            "virus": [("[", "v Saddam 1"), ("[", "v SystemZ v1.0"), ("[", "v")],
            "bad_size": [("[", "o"), ("[", "u"), ("[", "u2"), ("[", "o1")],
            "pre_release": [("(", "pre-release"), ("(", "beta"), ("(", "Beta 2"), ("(", "alpha"), ("(", "preview"),
                            ("(", "Debug"), ("(", "Debug Version"), ("(", "Test Program"), ("[", "beta 1")],
            "prototype": [("(", "proto"), ("(", "Proto 3"), ("(", "Possible Proto")],
            "demo": [("(", "demo-playable"), ("(", "demo-rolling"), ("(", "demo-slideshow"), ("(", "demo"),
                     ("(", "Demo 2"), ("(", "Sample"), ("(", "Kiosk"), ("(", "Kiosk, GameCube"),
                     ("(", "Auto Demo"), ("(", "Tech Demo"), ("[", "technical demo")],
            "faked": [("[", "faked BKAC 1"), ("[", "fake release"), ("[", "faked release")],
            "unreleased": [("[", "unreleased"), ("(", "unreleased")],
            "modified": [("[", "m"), ("[", "m2"), ("[", "m bamcopy"), ("[", "m baddump"), ("[", "modified tracks")],
        }
        for rule, toks in table.items():
            for kind, text in toks:
                self.assertEqual(tags.classify_token(kind, text), rule, (kind, text))

    def test_kept_tokens(self) -> None:
        for kind, text in [("[", "cr SR"), ("[", "cr"), ("[", "h QTX"), ("[", "t +3 ATX"), ("[", "a"), ("[", "a2 x"),
                           ("[", "f"), ("[", "tr de"), ("[", "p"), ("[", "!"), ("[", "bootable"), ("[", "BIOS"),
                           ("[", "budget"), ("[", "promo"), ("[", "aka Charlie"), ("[", "unknown hack"),
                           ("[", "inc. StarRay demo"), ("[", "U34"), ("[", "fixed"), ("[", "o-ring"[:0] or "HD"),
                           ("(", "Unl"), ("(", "Aftermarket"), ("(", "Pirate"), ("(", "Promo"), ("(", "AGA"),
                           ("(", "Alt")]:
            self.assertIsNone(tags.classify_token(kind, text), (kind, text))

    def test_bootable_is_not_bad(self) -> None:
        t = tags.parse_name("Kickstart v1.3 rev 34.5 (1987)(Commodore)(A500-A1000-A2000)[!][bootable].rom", "tosec")
        self.assertEqual(tags.bad_flags(t), [])
        self.assertEqual(tags.exclusion_info(t), [])
        t = tags.parse_name("X (1990)(Y)[b corrupt file].adf", "tosec")
        self.assertEqual(tags.bad_flags(t), ["[b corrupt file]"])
        self.assertEqual(tags.to_json(t)["bad_flags"], ["[b corrupt file]"])

    def test_crack_and_mods(self) -> None:
        t = tags.parse_name("X (1990)(Y)[cr SR][t +3 ATX][h Z][a]", "tosec")
        self.assertTrue(tags.is_cracked(t))
        self.assertEqual(tags.modification_count(t), 3)
        self.assertFalse(tags.is_cracked(tags.parse_name("X (1990)(Y)[h Z].adf", "tosec")))

    def test_identity_separates_products(self) -> None:
        def key(n):
            return tags.identity_key(tags.parse_name(n, "tosec"))
        self.assertNotEqual(key("Game (1990)(Alpha)(Disk 1 of 2).adf"), key("Game (1990)(Beta)(Disk 1 of 2).adf"))
        # chipset tags (AGA / OCS-AGA / ECS-AGA) and languages are ranking attributes, not identity
        self.assertEqual(key("Game (1990)(A).adf"), key("Game (1990)(A)(AGA).adf"))
        self.assertEqual(key("Game (1990)(A)(OCS-AGA).adf"), key("Game (1990)(A)(ECS-AGA).adf"))
        self.assertEqual(key("Game (1990)(A)(DE)(de).adf"), key("Game (1990)(A)(DE)(en).adf"))
        # real editions still separate (a labelled disk, a multi-language build, a dated edition flag)
        self.assertNotEqual(key("Game (1990)(A)(AGA Data).adf"), key("Game (1990)(A)(AGA).adf"))
        self.assertNotEqual(key("Game (1990)(A)(M3).adf"), key("Game (1990)(A).adf"))
        self.assertEqual(key("Game v1.0 (1990)(A).adf"), key("Game v1.1 (1991)(A)[cr X].adf"))
        self.assertNotEqual(key("Game (1990)(A)(US).adf"), key("Game (1990)(A)(DE).adf"))


FW = "Commodore Amiga - Firmware"


class AuditFixes(unittest.TestCase):
    """Regression tests of the review findings (OS DAT versions, bare rNNN, free-text flags ...)."""

    def test_only_dump_of_an_os_version_survives_modified(self) -> None:
        names = ["Workbench v1.3.2 rev 34.28 (1988)(Commodore)(Disk 1 of 2)[m]",
                 "Workbench v1.3.2 rev 34.28 (1988)(Commodore)(Disk 2 of 2)[m2]",
                 "Workbench v1.3 rev 34.20 (1988)(Commodore)(Disk 1 of 2)",
                 "Workbench v1.3 rev 34.20 (1988)(Commodore)(Disk 1 of 2)[m]",   # a better dump exists
                 "Workbench v1.3 rev 34.20 (1988)(Commodore)(Disk 2 of 2)",
                 "Workbench v1.4 rev 34.99 (1990)(Commodore)(Disk 1 of 2)[b]",   # bad dumps always go
                 "Workbench v1.4 rev 34.99 (1990)(Commodore)(Disk 2 of 2)"]
        # default: the user does NOT want modified Workbench disks (rescue_only_dump is opt-in)
        by_default = run(names, dat=WB)[2]
        self.assertEqual(by_default[names[0]].action, EXCLUDED)
        self.assertEqual(by_default[names[1]].action, EXCLUDED)
        _i, sel, by = run(names, RESCUE, dat=WB)
        self.assertEqual(by[names[0]].action, KEEP)
        self.assertEqual(by[names[1]].action, KEEP)
        self.assertIn("only dump of this version", by[names[0]].reason)
        self.assertEqual(by[names[3]].action, EXCLUDED)
        self.assertEqual(by[names[5]].action, EXCLUDED)
        self.assertEqual(by[names[2]].action, KEEP)
        self.assertEqual(len(sel.sets), 2)             # 1.3.2 (all [m]) and 1.3
        # games are never rescued: modified stays excluded there
        by = run(["Foo (1990)(Pub)[m]"], RESCUE, dat=GAMES)[2]
        self.assertEqual(by["Foo (1990)(Pub)[m]"].action, EXCLUDED)
        # firmware: only-[m] / only-[u] products stay, an [m] next to a clean dump goes
        fw = ["Action Replay Mk II v2.14 (1990)(Datel)[m]", "Amiga 1000 ROM Bootstrap (1985)(Commodore)[u]",
              "Kickstart v1.0 (1985)(Commodore)[!]", "Kickstart v1.0 (1985)(Commodore)[m]"]
        self.assertEqual(actions(run(fw, RESCUE, dat=FW)[2]),
                         {fw[0]: KEEP, fw[1]: KEEP, fw[2]: KEEP, fw[3]: EXCLUDED})
        self.assertEqual(actions(run(fw, dat=FW)[2]),            # default: nothing rescued
                         {fw[0]: EXCLUDED, fw[1]: EXCLUDED, fw[2]: KEEP, fw[3]: EXCLUDED})
        # idempotent: the kept output selects itself unchanged
        kept = [n for n in fw if actions(run(fw, RESCUE, dat=FW)[2])[n] == KEEP]
        self.assertTrue(all(a == KEEP for a in actions(run(kept, RESCUE, dat=FW)[2]).values()))

    def test_spare_os_disk_is_kept_not_incomplete(self) -> None:
        names = ["Workbench v1.1 rev 31.334 (1986)(Commodore)(Disk 1 of 2)(Workbench)",
                 "Workbench v1.1 rev 31.334 (1986)(Commodore)(Disk 2 of 2)(Extras)",
                 "Workbench v1.1 rev 31.334 (1986)(Commodore)(Disk 2 of 2)(Extras)[a]",
                 "Workbench v2.04 (1991)(Commodore)(A3000)(Disk 4 of 4)(Install)"]
        _i, sel, by = run(names, dat=WB)
        self.assertEqual(by[names[2]].action, KEEP)          # nothing missing: never INCOMPLETE with no disks
        for n, d in by.items():
            if d.action == INCOMPLETE:
                self.assertTrue(d.missing, n)
        self.assertEqual(by[names[3]].action, INCOMPLETE)    # a real orphan keeps its missing list
        self.assertEqual(by[names[3]].missing, (1, 2, 3))

    def test_rescued_only_dump_never_lands_in_incomplete(self) -> None:
        # disk 2 is only listed as [m] and fits no other disk: the version must not vanish
        names = ["Workbench v2.1 rev 38.30 (1992)(Commodore)(M10)(Disk 1 of 3)[a]",
                 "Workbench v2.1 rev 38.30 (1992)(Commodore)(M10)(Disk 2 of 3)[m]",
                 "Workbench v2.1 rev 38.30 (1992)(Commodore)(M10)(Disk 3 of 3)[a]"]
        _i, sel, by = run(names, RESCUE, dat=WB)
        self.assertEqual(by[names[1]].action, KEEP)
        self.assertIn("only dump of this version", by[names[1]].reason)
        self.assertEqual(by[names[0]].action, INCOMPLETE)     # a partial set stays partial (rule d)
        self.assertTrue(by[names[0]].missing)
        self.assertEqual(run([names[1]], RESCUE, dat=WB)[2][names[1]].action, KEEP)   # stable on its own output
        # a spare, flag-incompatible disk next to a complete set is kept on keep-every-version DATs
        names = ["Workbench v1.3 (1988)(Commodore)(Disk 1 of 2)", "Workbench v1.3 (1988)(Commodore)(Disk 2 of 2)",
                 "Workbench v1.3 (1988)(Commodore)(Disk 2 of 2)[h Foo]"]
        by = run(names, dat=WB)[2]
        self.assertTrue(all(d.action == KEEP for d in by.values()), actions(by))

    def test_bare_revision_tokens_are_versions(self) -> None:
        t = tags.parse_name("Hibernated 1 - This Interactive Fiction (1987)(Pub)(Disk 1 of 2)", "tosec")
        self.assertEqual(t.version, "")
        for name, title, ver in [("3D Construction Kit r01.1000 (1991)(Domark)", "3D Construction Kit", "r01.1000"),
                                 ("Lurking Horror, The r219 (1987)(Infocom)", "Lurking Horror, The", "r219"),
                                 ("East vs. West - Berlin 1948 rev1 (1994)(Pub)", "East vs. West - Berlin 1948", "rev1"),
                                 ("Falcon r2.1 (1989)(Spectrum)", "Falcon", "r2.1")]:
            t = tags.parse_name(name, "tosec")
            self.assertEqual((t.title, t.version), (title, ver), name)
        names = ["3D Construction Kit r01.0000 (1991)(Domark)", "3D Construction Kit r01.1000 (1991)(Domark)",
                 "3D Construction Kit r01.2000 (1991)(Domark)",
                 "Hibernated 1 - Director's Cut r06 (1991)(Pub)", "Hibernated 1 - Director's Cut r13 (1991)(Pub)",
                 "Hibernated 1 - Director's Cut r07 (1991)(Pub)",
                 "East vs. West - Berlin 1948 (1994)(Pub)", "East vs. West - Berlin 1948 rev1 (1994)(Pub)"]
        by = run(names)[2]
        self.assertEqual({n: by[n].action for n in names}, {
            names[0]: SUPERSEDED, names[1]: SUPERSEDED, names[2]: KEEP,
            names[3]: SUPERSEDED, names[4]: KEEP, names[5]: SUPERSEDED,
            names[6]: SUPERSEDED, names[7]: KEEP})

    def test_free_text_bad_dump_and_virus_alternates_are_excluded(self) -> None:
        for kind, text, rule in [("[", "a baddump", "bad_dump"), ("[", "a2 baddump", "bad_dump"),
                                 ("[", "inc. Virus", "virus"), ("[", "a virus removed", None),
                                 ("[", "a", None), ("[", "a2 other", None)]:
            self.assertEqual(tags.classify_token(kind, text), rule, text)
        by = run(["Zool (1992)(Gremlin)(Disk 1 of 2)", "Zool (1992)(Gremlin)(Disk 2 of 2)",
                  "Zool (1992)(Gremlin)(Disk 2 of 2)[a baddump]"])[2]
        self.assertEqual(by["Zool (1992)(Gremlin)(Disk 2 of 2)[a baddump]"].action, EXCLUDED)

    def test_incomplete_reason_mentions_disks_under_another_identity(self) -> None:
        names = ["B.A.T. II (1992)(Ubi Soft)(DE)(Disk 1 of 2)", "B.A.T. II (1992)(Ubi Soft)(Disk 2 of 2)"]
        _i, sel, by = run(names, replace(ALL_LANGS, borrow_other_editions=False))   # (DE) = German; every language
        d = by[names[0]]
        self.assertEqual((d.action, d.missing, d.elsewhere), (INCOMPLETE, (2,), (2,)))
        self.assertIn("different country / language / edition", d.reason)
        d = by[names[1]]                         # the neutral disk 2 lacks its disk 1
        self.assertEqual((d.action, d.missing, d.elsewhere), (INCOMPLETE, (1,), (1,)))

    def test_compilation_and_verified_dumps_in_slot_resolution(self) -> None:
        from romorg.m3u import DiskCand, resolve_slots
        d1 = "Another World (1991)(Delphine)(Disk 1 of 2)[cr CSL]"
        plain = "Another World (1991)(Delphine)(Disk 2 of 2)"
        comp = "Another World (1995)(Delphine - U.S. Gold)(Disk 2 of 2)[compilation Delphine Collection]"
        sets = resolve_slots([DiskCand(0, d1), DiskCand(1, plain), DiskCand(2, comp)])
        full = [s for s in sets if s.complete]
        self.assertEqual([s.slots[2].name for s in full], [plain])   # no mixing with a compilation re-release
        a = "Barbarian II (1991)(Palace)(Disk 1 of 2)[!]"
        b_plain = "Barbarian II (1991)(Palace)(Disk 2 of 2)"
        b_ok = "Barbarian II (1991)(Palace)(Disk 2 of 2)[!][more info]"
        full = [s for s in resolve_slots([DiskCand(0, a), DiskCand(1, b_plain), DiskCand(2, b_ok)]) if s.complete]
        self.assertEqual([s.slots[2].name for s in full], [b_ok])    # the verified dump wins the tie


class AbcRegression(unittest.TestCase):
    def test_slotwise_set(self) -> None:
        names = [ABC1, ABC_OLD1, ABC2, ABC3, *ABC_PRE, ABC_BAD, ABC1.replace("[cr SR]", "")]
        _items, sel, by = run(names)
        self.assertEqual([s.name for s in sel.sets], ["ABC Monday Night Football v1.1 (1991)(Data East)(US)[cr SR]"])
        s = sel.sets[0]
        self.assertEqual(s.total, 3)
        self.assertEqual([names[s.slots[i] - 1] for i in (1, 2, 3)], [ABC1, ABC2, ABC3])
        for n in (ABC1, ABC2, ABC3):
            self.assertEqual(by[n].action, KEEP)
            self.assertEqual(by[n].set_id, s.id)
        for n in ABC_PRE + [ABC_BAD]:
            self.assertEqual(by[n].action, EXCLUDED)
        self.assertIn("[b corrupt file]", by[ABC_BAD].detail)
        self.assertEqual(by[ABC_OLD1].action, SUPERSEDED)
        self.assertEqual(sel.incomplete, [])

    def test_unflagged_disk1_does_not_beat_cracked(self) -> None:
        names = [ABC1, ABC2, ABC3, ABC1.replace("[cr SR]", "")]
        _i, sel, by = run(names)
        self.assertEqual(by[names[3]].action, SUPERSEDED)


class Ranking(unittest.TestCase):
    def test_cracked_old_beats_uncracked_new(self) -> None:
        by = run(["G v1.0 (1990)(P)[cr A]", "G v1.1 (1991)(P)"])[2]
        self.assertEqual(actions(by), {"G v1.0 (1990)(P)[cr A]": KEEP, "G v1.1 (1991)(P)": SUPERSEDED})

    def test_newest_cracked_wins(self) -> None:
        by = run(["G v1.0 (1990)(P)[cr A]", "G v1.2 (1992)(P)[cr B]", "G v1.1 (1991)(P)[cr C]"])[2]
        self.assertEqual([n for n, d in by.items() if d.action == KEEP], ["G v1.2 (1992)(P)[cr B]"])

    def test_crack_only_kept_and_uncracked_latest(self) -> None:
        by = run(["H (1990)(P)[cr A]"])[2]
        self.assertEqual(by["H (1990)(P)[cr A]"].action, KEEP)
        by = run(["I v1.0 (1990)(P)", "I v1.1 (1991)(P)"])[2]
        self.assertEqual([n for n, d in by.items() if d.action == KEEP], ["I v1.1 (1991)(P)"])

    def test_fewest_mods_tiebreak(self) -> None:
        by = run(["J (1990)(P)[cr A][t +3 X]", "J (1990)(P)[cr B]"])[2]
        self.assertEqual(by["J (1990)(P)[cr B]"].action, KEEP)

    def test_publisher_and_edition_separate(self) -> None:
        names = ["K (1990)(P1)[cr A]", "K (1990)(P2)[cr A]", "K (1990)(P1)(M3)[cr A]", "K (1990)(P1)(US)[cr A]",
                 "K (1990)(P1)(DE)[cr A]"]
        self.assertTrue(all(d.action == KEEP for d in run(names, ALL_LANGS)[2].values()))

    def test_incomplete_prefers_older_complete(self) -> None:
        names = ["L v1.1 (1991)(P)(Disk 1 of 2)[cr A]", "L v1.0 (1990)(P)(Disk 1 of 2)[cr A]",
                 "L v1.0 (1990)(P)(Disk 2 of 2)"]
        # disk 2 (1990) is compatible with v1.1 disk 1 -> complete newest set
        by = run(names)[2]
        self.assertEqual(by[names[0]].action, KEEP)
        self.assertEqual(by[names[2]].action, KEEP)
        self.assertEqual(by[names[1]].action, SUPERSEDED)

    def test_incomplete_set(self) -> None:
        names = ["M (1990)(P)(Disk 1 of 3)", "M (1990)(P)(Disk 3 of 3)"]
        _i, sel, by = run(names)
        self.assertTrue(all(d.action == INCOMPLETE and d.missing == (2,) for d in by.values()))
        self.assertEqual(len(sel.incomplete), 1)
        self.assertEqual(sel.incomplete[0].missing, (2,))
        # completeness rule off: untouched
        prof = LibraryProfile(complete_only=False)
        self.assertTrue(all(d.action == KEEP for d in run(names, prof)[2].values()))


class Rules(unittest.TestCase):
    NAMES = ["R (1990)(P)", "R2 (1990)(P)(pre-release)", "R3 (1990)(P)[b]", "R4 (1990)(P)[v Foo]",
             "R5 (1990)(P)[o]", "R6 (1990)(P)(demo-playable)", "R7 (1990)(P)[m bamcopy]", "R8 (1990)(P)[faked x]",
             "R9 (1990)(P)[unreleased]", "S (1990)(P)(proto)", "S1 (1990)(P)[cr X][h Y][t +1 Z][tr de][f][a]"]

    def test_defaults_exclude_but_keep_cracks_hacks(self) -> None:
        by = run(self.NAMES)[2]
        self.assertEqual([n for n, d in by.items() if d.action == KEEP], [self.NAMES[0], self.NAMES[-1]])

    def test_each_rule_toggles(self) -> None:
        expect = {"R2": "pre_release", "R3": "bad_dump", "R4": "virus", "R5": "bad_size", "R6": "demo",
                  "R7": "modified", "R8": "faked", "R9": "unreleased", "S": "prototype"}
        for stem, rule in expect.items():
            n = next(x for x in self.NAMES if x.startswith(stem + " "))
            on = run([n])[2][n]
            self.assertEqual((on.action, on.codes), (EXCLUDED, (rule,)))
            off = run([n], LibraryProfile(exclude=frozenset(library.RULES) - {rule}))[2][n]
            self.assertEqual(off.action, KEEP, rule)

    def test_workbench_and_kickstart_keep_all_versions(self) -> None:
        names = ["Workbench v1.3 (1988)(Commodore)(Disk 1 of 2)", "Workbench v1.3 (1988)(Commodore)(Disk 2 of 2)",
                 "Workbench v2.0 (1990)(Commodore)(Disk 1 of 2)", "Workbench v2.0 (1990)(Commodore)(Disk 2 of 2)",
                 "Kickstart v1.2 (1986)(Commodore)", "Kickstart v1.3 (1987)(Commodore)"]
        for dat in (WB, KSD):
            by = run(names, dat=dat)[2]
            self.assertTrue(all(d.action == KEEP for d in by.values()), dat)
        _i, sel, _b = run(names, dat=WB)
        self.assertEqual(len(sel.sets), 2)

    def test_firmware_untouched_but_excluded(self) -> None:
        by = run(["Kickstart v1.0 (1985)(Commodore)[!]", "Kickstart v1.0 (1985)(Commodore)[b]"],
                 dat="Commodore Amiga - Firmware")[2]
        self.assertEqual(actions(by), {"Kickstart v1.0 (1985)(Commodore)[!]": KEEP,
                                       "Kickstart v1.0 (1985)(Commodore)[b]": EXCLUDED})

    def test_latest_only_without_best_variant(self) -> None:
        prof = LibraryProfile(best_variant=False)
        by = run(["G v1.0 (1990)(P)[cr A]", "G v1.1 (1991)(P)[cr A]", "G v1.1 (1991)(P)"], prof)[2]
        self.assertEqual(actions(by), {"G v1.0 (1990)(P)[cr A]": SUPERSEDED, "G v1.1 (1991)(P)[cr A]": KEEP,
                                       "G v1.1 (1991)(P)": KEEP})
        by = run(["G v1.0 (1990)(P)[cr A]", "G v1.1 (1991)(P)"], LibraryProfile(best_variant=False, latest_only=False))[2]
        self.assertTrue(all(d.action == KEEP for d in by.values()))


class OverrideTests(unittest.TestCase):
    """The user's per-game "always keep" / "always exclude" beat every rule."""

    def prof(self, *overrides: tuple[str, str]) -> LibraryProfile:
        return replace(library.default_profile(AMIGA), overrides=tuple((GAMES, n + ".adf", a) for n, a in overrides))

    # an override names the game as the DAT does: the set name (No-Intro) or the rom name with its extension (TOSEC)

    def test_always_keep_rescues_an_excluded_and_a_superseded_file(self) -> None:
        bad, old, new = "B (1990)(P)[b]", "G v1.0 (1990)(P)", "G v1.1 (1991)(P)"
        base = run([bad, old, new])[2]
        self.assertEqual((base[bad].action, base[old].action), (EXCLUDED, SUPERSEDED))
        by = run([bad, old, new], self.prof((bad, "keep"), (old, "keep")))[2]
        self.assertEqual(actions(by), {bad: KEEP, old: KEEP, new: KEEP})
        self.assertEqual(by[bad].codes, (library.OVERRIDE_KEEP,))

    def test_always_exclude_sets_a_kept_file_aside_with_its_own_code(self) -> None:
        name = "Good (1990)(P)"
        d = run([name], self.prof((name, "exclude")))[2][name]
        self.assertEqual((d.action, d.codes), (EXCLUDED, (library.OVERRIDE_EXCLUDE,)))

    def test_an_excluded_disk_drops_the_playlist_of_its_set(self) -> None:
        names = ["Two (1990)(P)(Disk 1 of 2)", "Two (1990)(P)(Disk 2 of 2)"]
        self.assertEqual(len(run(names)[1].sets), 1)
        _i, sel, by = run(names, self.prof((names[1], "exclude")))
        self.assertEqual(sel.sets, [])
        self.assertEqual(by[names[1]].action, EXCLUDED)

    def test_other_games_and_other_dats_are_untouched(self) -> None:
        a, b = "A (1990)(P)", "B (1990)(P)"
        by = run([a, b], self.prof((a, "exclude")))[2]
        self.assertEqual(actions(by), {a: EXCLUDED, b: KEEP})
        other = replace(library.default_profile(AMIGA), overrides=(("Some other DAT", a + ".adf", "exclude"),))
        self.assertEqual(actions(run([a], other)[2]), {a: KEEP})

    def test_overrides_round_trip_and_bad_entries_are_dropped(self) -> None:
        prof = LibraryProfile(overrides=[(GAMES, "B", "keep"), (GAMES, "A", "exclude"), (GAMES, "A", "keep"),
                                         ("", "x", "keep"), (GAMES, "C", "maybe"), "junk", (GAMES, "D")])
        self.assertEqual(prof.overrides, ((GAMES, "A", "keep"), (GAMES, "B", "keep")))        # last wins, sorted
        again = LibraryProfile.from_dict(prof.to_dict())
        self.assertEqual(again.overrides, prof.overrides)
        self.assertEqual(LibraryProfile.from_dict({"exclude": []}, prof).overrides, prof.overrides)   # kept when not mentioned

    def test_the_profile_signature_follows_the_overrides(self) -> None:
        from romorg import totals
        self.assertNotEqual(totals.profile_signature(self.prof()), totals.profile_signature(self.prof(("A (1990)(P)", "keep"))))


class NoIntro(unittest.TestCase):
    def test_latest_per_region_and_rules(self) -> None:
        dat = "Nintendo - Game Boy Advance"
        plat = platforms.get_platform("Nintendo Game Boy Advance")
        names = ["Game (USA)", "Game (USA) (Rev 1)", "Game (Europe)", "Game (Europe) (Rev 2)", "Game (Europe) (Rev 1)",
                 "Game (Japan) (Beta)", "Game (Japan) (Proto)", "Game (Japan) (Demo)", "Game (World) (Sample)",
                 "Game (USA) (Unl)", "Game (USA) (Aftermarket)", "Game (USA) (Possible Proto)", "Game (USA) (Kiosk)",
                 "Game (USA) (Debug)", "Game (USA) (Virtual Console)"]
        items = ni_items(names, dat)
        prof = replace(library.default_profile(plat), one_per_game=False)    # per-region latest
        sel = library.select(items, prof, plat)
        got = {names[k - 1]: d.action for k, d in sel.decisions.items()}
        self.assertEqual(got["Game (USA)"], SUPERSEDED)
        self.assertEqual(got["Game (USA) (Rev 1)"], KEEP)
        self.assertEqual(got["Game (Europe) (Rev 2)"], KEEP)
        self.assertEqual(got["Game (Europe) (Rev 1)"], SUPERSEDED)
        self.assertEqual(got["Game (Europe)"], SUPERSEDED)
        for n in ("Game (Japan) (Beta)", "Game (Japan) (Proto)", "Game (Japan) (Demo)", "Game (World) (Sample)",
                  "Game (USA) (Possible Proto)", "Game (USA) (Kiosk)", "Game (USA) (Debug)"):
            self.assertEqual(got[n], EXCLUDED, n)
        for n in ("Game (USA) (Unl)", "Game (USA) (Aftermarket)", "Game (USA) (Virtual Console)"):
            self.assertEqual(got[n], KEEP, n)


class Idempotence(unittest.TestCase):
    def test_kept_output_is_stable_and_order_independent(self) -> None:
        names = [ABC1, ABC_OLD1, ABC2, ABC3, *ABC_PRE, ABC_BAD, "G v1.0 (1990)(P)[cr A]", "G v1.1 (1991)(P)",
                 "M (1990)(P)(Disk 1 of 3)", "M (1990)(P)(Disk 3 of 3)", "N (1990)(P)", "N (1990)(P)[h X]",
                 "N (1990)(P)[b]", "L v1.1 (1991)(P)(Disk 1 of 2)[cr A]", "L v1.0 (1990)(P)(Disk 2 of 2)"]
        prof = library.default_profile(AMIGA)
        items = amiga_items(names)
        sel = library.select(items, prof, AMIGA)
        kept = [i for i in items if sel.decisions[i.key].action == KEEP]
        random.Random(3).shuffle(kept)
        sel2 = library.select(kept, prof, AMIGA)
        self.assertTrue(all(sel2.decisions[i.key].action == KEEP for i in kept))
        self.assertEqual(sorted((s.name, tuple(sorted(s.slots.items()))) for s in sel.sets),
                         sorted((s.name, tuple(sorted(s.slots.items()))) for s in sel2.sets))
        shuffled = items[:]
        random.Random(5).shuffle(shuffled)
        sel3 = library.select(shuffled, prof, AMIGA)
        self.assertEqual({k: d.action for k, d in sel.decisions.items()},
                         {k: d.action for k, d in sel3.decisions.items()})


class ProfileTests(unittest.TestCase):
    def test_roundtrip_and_defaults(self) -> None:
        p = library.default_profile(AMIGA)
        self.assertEqual(LibraryProfile.from_dict(p.to_dict()), p)
        q = LibraryProfile.from_dict({"exclude": ["demo", "nonsense"], "latest_only": False, "junk": 1}, p)
        self.assertEqual(q.exclude, frozenset({"demo"}))
        self.assertFalse(q.latest_only)
        self.assertEqual(LibraryProfile.from_dict("x", p), p)
        gba = library.default_profile(platforms.get_platform("Nintendo Game Boy Advance"))
        self.assertFalse(gba.best_variant)
        self.assertFalse(gba.complete_only)
        self.assertTrue(gba.latest_only)

    def test_load_store_and_legacy(self) -> None:
        cfg: dict = {"latest_only": {AMIGA.name: False}}
        self.assertFalse(library.load_profile(cfg, AMIGA).latest_only)
        library.store_profile(cfg, AMIGA, LibraryProfile(exclude=frozenset({"demo"})))
        self.assertEqual(library.load_profile(cfg, AMIGA).exclude, frozenset({"demo"}))
        self.assertEqual(library.exclusion_of("X (1990)(P)[b corrupt file].adf", "tosec", LibraryProfile()),
                         (("bad_dump",), "[b corrupt file]"))


GBA_DAT = "Nintendo - Game Boy Advance"
GBA = platforms.get_platform("Nintendo Game Boy Advance")


def ni_run(names: list[str], profile: LibraryProfile | None = None):
    items = ni_items(names, GBA_DAT)
    sel = library.select(items, profile or library.default_profile(GBA), GBA)
    return sel, {names[k - 1]: d for k, d in sel.decisions.items()}


def tos(name: str) -> tags.Tags:
    return tags.parse_name(name, "tosec")


class PlatformRanking(unittest.TestCase):
    """AGA / OCS versions of one title are ONE game; the platform is a ranking attribute."""

    def kept(self, names, profile=None):
        by = run(names, profile)[2]
        return sorted(n for n, d in by.items() if d.action == KEEP)

    def test_aga_cracked_beats_older_ocs_cracked_and_newer_ocs(self) -> None:
        names = ["Alien (1993)(T17)(AGA)(Disk 1 of 2)[cr FLT]", "Alien (1993)(T17)(AGA)(Disk 2 of 2)[cr FLT]",
                 "Alien v1.1 (1994)(T17)(Disk 1 of 3)[cr FLT]", "Alien v1.1 (1994)(T17)(Disk 2 of 3)[cr FLT]",
                 "Alien v1.1 (1994)(T17)(Disk 3 of 3)[cr FLT]"]
        self.assertEqual(self.kept(names), sorted(names[:2]))

    def test_cracked_ocs_beats_uncracked_aga(self) -> None:
        names = ["B (1993)(P)(AGA)", "B (1993)(P)[cr X]"]
        self.assertEqual(self.kept(names), ["B (1993)(P)[cr X]"])

    def test_platform_beats_version_and_mods(self) -> None:
        names = ["C v1.0 (1993)(P)(AGA)[h X]", "C v1.2 (1994)(P)", "C v1.1 (1994)(P)(OCS-AGA)[t +1 Y][h Z]"]
        # AGA-class variants rank above plain; among them the newest wins
        self.assertEqual(self.kept(names), ["C v1.1 (1994)(P)(OCS-AGA)[t +1 Y][h Z]"])

    def test_all_chipset_spellings_are_aga_class(self) -> None:
        for chip in ("AGA", "OCS-AGA", "ECS-AGA", "OCS-ECS-AGA"):
            self.assertEqual(tags.platform_class(tos(f"G (1993)(P)({chip}).adf")), "AGA", chip)
        for chip in ("OCS", "ECS", "OCS-ECS"):
            self.assertEqual(tags.platform_class(tos(f"G (1993)(P)({chip}).adf")), "OCS", chip)
        self.assertEqual(tags.platform_class(tos("G (1993)(P).adf")), "OCS")
        # a labelled disk is no chipset tag
        self.assertEqual(tags.chipset(tos("G (1993)(P)(Disk 2 of 3)(AGA Data).adf")), ())

    def test_cd32_is_ready_to_slot_in_above_aga(self) -> None:
        self.assertEqual(tags.PLATFORM_ORDER, ("CD32", "AGA", "OCS"))
        self.assertLess(tags.platform_rank(tos("G (1994)(P)(CD32).adf")),
                        tags.platform_rank(tos("G (1993)(P)(AGA).adf")))
        self.assertEqual(self.kept(["D (1994)(P)(CD32)", "D (1993)(P)(AGA)", "D (1992)(P)"]), ["D (1994)(P)(CD32)"])
        # identity ignores it, too
        self.assertEqual(tags.identity_key(tos("D (1994)(P)(CD32).adf")), tags.identity_key(tos("D (1992)(P).adf")))

    def test_real_editions_stay_separate(self) -> None:
        names = ["E (1993)(P)(AGA)", "E (1993)(P)(AGA Data)", "E - World Cup Edition (1994)(P)(AGA)",
                 "E (1993)(P2)(AGA)", "E (1993)(P)(M3)(AGA)"]
        self.assertEqual(self.kept(names), sorted(names))

    def test_mixed_chipset_disk_sets_do_not_break(self) -> None:
        # SkidMarks / Coala: bare ECS-AGA tag before the disk token, chipset words in the disk labels
        coala = [f"Coala (1995)(Empire)(ECS-AGA)(Disk 1 of 3)(Program)[cr HLM]",
                 "Coala (1995)(Empire)(ECS-AGA)(Disk 2 of 3)(AGA Data)", "Coala (1995)(Empire)(ECS-AGA)(Disk 3 of 3)(ECS Data)"]
        skid = ["SkidMarks v1.06 (1993)(Acid)(ECS-AGA)(Disk 1 of 3)(Program Disk)[cr GOD]",
                "SkidMarks v1.06 (1993)(Acid)(ECS-AGA)(Disk 2 of 3)(AGA Car Disk 1)[cr GOD]",
                "SkidMarks v1.06 (1993)(Acid)(ECS-AGA)(Disk 3 of 3)(Track Disk 1)"]
        _i, sel, by = run(coala + skid)
        self.assertTrue(all(d.action == KEEP for d in by.values()), actions(by))
        self.assertEqual(len(sel.sets), 2)

    def test_disks_never_mix_chipsets(self) -> None:
        names = ["F (1993)(P)(AGA)(Disk 1 of 2)[cr A]", "F (1993)(P)(Disk 2 of 2)"]    # AGA disk 1, OCS disk 2
        by = run(names)[2]
        self.assertEqual({d.action for d in by.values()}, {INCOMPLETE})
        # same through the stand-alone playlist resolver
        from romorg import m3u
        sets = m3u.resolve_slots([m3u.DiskCand(n, n) for n in names])
        self.assertTrue(all(not s.complete for s in sets))

    def test_latest_only_without_best_variant_keeps_both_platforms(self) -> None:
        names = ["G v1.0 (1993)(P)(AGA)", "G v1.1 (1994)(P)(AGA)", "G v1.0 (1993)(P)", "G v1.1 (1994)(P)"]
        prof = LibraryProfile(best_variant=False)
        self.assertEqual(self.kept(names, prof), ["G v1.1 (1994)(P)", "G v1.1 (1994)(P)(AGA)"])

    def test_ranking_is_permutation_stable(self) -> None:
        names = ["H (1993)(P)(AGA)[cr A]", "H (1993)(P)[cr A]", "H v1.1 (1994)(P)(OCS-AGA)[h B]", "H v1.2 (1995)(P)[cr C]"]
        ref = self.kept(names)
        for seed in range(6):
            shuffled = names[:]
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(self.kept(shuffled), ref)


class LanguageTests(unittest.TestCase):
    def langs(self, name: str, style: str = "tosec") -> set[str]:
        return set(tags.variant_languages(tags.parse_name(name, style)))

    def test_tosec_language_rules(self) -> None:
        self.assertEqual(self.langs("X (1990)(P).adf"), {"En"})                  # no language, no country
        self.assertEqual(self.langs("X (1990)(P)(US).adf"), {"En"})
        self.assertEqual(self.langs("X (1990)(P)(DE).adf"), {"De"})              # country -> language
        self.assertEqual(self.langs("X (1990)(P)(PL).adf"), {"Pl"})
        self.assertEqual(self.langs("X (1990)(P)(DE)(de-en).adf"), {"De", "En"})  # all listed
        self.assertEqual(self.langs("X (1990)(P)(en).adf"), {"En"})
        self.assertEqual(self.langs("X (1990)(P)(CH).adf"), {"De"})
        self.assertEqual(self.langs("X (1990)(P)(DE)[tr en].adf"), {"De", "En"})
        self.assertEqual(self.langs("X (1990)(P)[tr de Foo].adf"), {"En", "De"})
        self.assertEqual(self.langs("X (1990)(P)(FR)[tr en-de x].adf"), {"Fr", "En", "De"})
        self.assertEqual(self.langs("X (1990)(P)(DE)(M3).adf"), {"De", "En"})

    def test_nointro_language_rules(self) -> None:
        self.assertEqual(self.langs("G (Europe) (En,Fr,De)", "nointro"), {"En", "Fr", "De"})
        self.assertEqual(self.langs("G (USA)", "nointro"), {"En"})
        self.assertEqual(self.langs("G (Japan)", "nointro"), {"Ja"})
        self.assertEqual(self.langs("G (World)", "nointro"), {"En"})
        self.assertEqual(self.langs("G (Unknown)", "nointro"), {"En"})
        self.assertEqual(self.langs("G (Japan) (En)", "nointro"), {"En"})
        self.assertEqual(self.langs("G (Germany)", "nointro"), {"De"})
        self.assertEqual(self.langs("G (Europe) (En,Pt-BR)", "nointro"), {"En", "Pt"})

    def test_language_is_a_filter_not_identity(self) -> None:
        names = ["L (1990)(P)", "L (1990)(P)(de)", "L (1990)(P)(DE)(de)"]
        by = run(names)[2]
        self.assertEqual(by[names[0]].action, KEEP)
        for n in names[1:]:
            self.assertEqual((by[n].action, by[n].codes), (EXCLUDED, ("language",)), n)
        self.assertIn("language", by[names[2]].reason)

    def test_titles_without_a_selected_language_vanish_with_a_reason(self) -> None:
        names = ["Anstoss (1993)(Ascon)(DE)", "Anstoss (1993)(Ascon)(DE)[cr X]", "Fine (1990)(P)", "Fine (1990)(P)(DE)(de)",
                 "Bad (1990)(P)[b]"]
        _i, sel, by = run(names)
        self.assertEqual([(v.title, v.reason, v.languages) for v in sel.vanished],
                         [("Anstoss", "language", ("De",)), ("Bad", "bad_dump", ())])
        self.assertEqual(sel.vanish_summary()["by_reason"], {"language": 1, "bad_dump": 1})
        ec = sel.exclusion_counts()
        self.assertEqual(ec["exclusive"]["language"], 3)
        self.assertEqual(ec["exclusive"]["bad_dump"], 1)

    def test_vanish_report_json(self) -> None:
        _i, sel, _by = run(["Anstoss (1993)(Ascon)(DE)", "Bad (1990)(P)[b]", "Ok (1990)(P)"])
        rep = library.vanish_report(sel)
        self.assertEqual((rep["titles"], rep["by_reason"]), (2, {"language": 1, "bad_dump": 1}))
        first = rep["items"][0]
        self.assertEqual((first["title"], first["reason"], first["languages"], first["text"]),
                         ("Anstoss", "language", ["De"], "no version in selected languages (has De)"))
        self.assertEqual([i["title"] for i in library.vanish_report(sel, reason="bad_dump")["items"]], ["Bad"])
        self.assertEqual(len(library.vanish_report(sel, limit=1)["items"]), 1)

    def test_multi_language_selection_keeps_one_variant_in_profile_order(self) -> None:
        names = ["M (1990)(P)[cr A]", "M (1990)(P)(de)[cr B]", "M (1990)(P)(de)"]
        prof = replace(library.default_profile(AMIGA), languages=("En", "De"))
        self.assertEqual([n for n, d in run(names, prof)[2].items() if d.action == KEEP], [names[0]])
        prof = replace(prof, languages=("De", "En"))       # German preferred: German beats the cracked English one
        self.assertEqual([n for n, d in run(names, prof)[2].items() if d.action == KEEP], [names[1]])
        prof = replace(prof, languages=("De",))
        self.assertEqual([n for n, d in run(names, prof)[2].items() if d.action == KEEP], [names[1]])

    def test_no_language_filter_when_empty(self) -> None:
        by = run(["N (1990)(P)(DE)(de)"], ALL_LANGS)[2]
        self.assertEqual({d.action for d in by.values()}, {KEEP})

    def test_language_filter_covers_games_and_consoles_only(self) -> None:
        # Workbench / Kickstart / Firmware are language-independent
        by = run(["Workbench v1.3 (1988)(Commodore)(DE)(Disk 1 of 1)"], dat=WB)[2]
        self.assertEqual({d.action for d in by.values()}, {KEEP})

    def test_available_languages(self) -> None:
        roms = [Rom(name=n + ".adf", size=1, crc="", md5="", sha1="", game=n, dat=GAMES)
                for n in ("A (1990)(P)", "A (1990)(P)(de)", "B (1990)(P)(DE)", "C (1990)(P)(PL)", "D (1990)(P)(US)")]
        from romorg.datfile import DatFile
        rows = library.available_languages(DatFile(GAMES, "", "", roms))
        self.assertEqual([r["code"] for r in rows], ["En", "De", "Pl"])
        self.assertEqual({r["code"]: (r["count"], r["games"]) for r in rows}, {"En": (2, 2), "De": (2, 2), "Pl": (1, 1)})
        self.assertEqual(rows[0]["name"], "English")
        items = ni_items(["G (Europe) (En,Fr,De)", "H (Japan)"], GBA_DAT)
        self.assertEqual([r["code"] for r in library.available_languages(items)], ["En", "De", "Fr", "Ja"])


class KeepFlagTests(unittest.TestCase):
    def test_flag_types(self) -> None:
        t = tos("X (1990)(P)[cr FLT][h Z][t +3 A][a2][f x][tr de][!].adf")
        self.assertEqual(tags.flag_types(t), frozenset(tags.KEEP_FLAGS))
        self.assertEqual(tags.flag_types(tos("X (1990)(P)[tr de].adf")), frozenset({"tr"}))   # not a [t]
        self.assertEqual(tags.flag_types(tos("X (1990)(P)[bootable][!].adf")), frozenset())

    def test_unticked_flag_excludes_with_flag_code(self) -> None:
        names = ["O (1990)(P)[cr A]", "O (1990)(P)", "Only (1990)(P)[cr B]", "H (1990)(P)[h X]", "Tr (1990)(P)[tr de]"]
        for f in ("cr", "h", "tr"):
            prof = replace(library.default_profile(AMIGA), keep_flags=frozenset(tags.KEEP_FLAGS) - {f})
            by = run(names, prof)[2]
            hit = [n for n, d in by.items() if d.action == EXCLUDED]
            for n in hit:
                self.assertEqual(by[n].codes, (f"flag_{f}",), n)
                self.assertIn(f"[{f}", by[n].detail)
            self.assertTrue(hit)
        prof = replace(library.default_profile(AMIGA), keep_flags=frozenset(tags.KEEP_FLAGS) - {"cr"})
        _i, sel, by = run(names, prof)
        self.assertEqual(by["O (1990)(P)"].action, KEEP)
        self.assertEqual(by["O (1990)(P)[cr A]"].action, EXCLUDED)
        self.assertEqual([(v.title, v.reason) for v in sel.vanished], [("Only", "flag_cr")])
        self.assertEqual(sel.vanish_summary()["by_reason"], {"flag_cr": 1})

    def test_default_keeps_every_flag_type_and_one_variant(self) -> None:
        names = ["Z (1990)(P)[cr A]", "Z (1990)(P)[cr A][t +3 B]", "Z (1990)(P)[h X]", "Z (1990)(P)[a]", "Z (1990)(P)"]
        by = run(names)[2]
        self.assertEqual([n for n, d in by.items() if d.action == KEEP], ["Z (1990)(P)[cr A]"])
        self.assertEqual(library.default_profile(AMIGA).keep_flags, frozenset(tags.KEEP_FLAGS))

    def test_hacks_and_trainers_rank_below_clean(self) -> None:
        by = run(["Y (1990)(P)[h X]", "Y (1990)(P)[t +2 A]", "Y (1990)(P)"])[2]
        self.assertEqual([n for n, d in by.items() if d.action == KEEP], ["Y (1990)(P)"])

    def test_flags_do_not_apply_to_other_dats(self) -> None:
        prof = replace(library.default_profile(AMIGA), keep_flags=frozenset())
        by = run(["Workbench v1.3 (1988)(Commodore)[a]"], prof, dat=WB)[2]
        self.assertEqual({d.action for d in by.values()}, {KEEP})


class CatalogTests(unittest.TestCase):
    def test_every_catalog_token_classifies_to_its_rule(self) -> None:
        for style in ("tosec", "nointro"):
            for e in library.rule_catalog(style):
                if e["kind"] == "exclude":
                    self.assertTrue(e["tokens"], (style, e["id"]))
                    for tok in e["tokens"]:
                        self.assertEqual(tags.token_rule(tok), e["id"], (style, tok))
                elif e["kind"] == "keep_flag":
                    for tok in e["tokens"]:
                        name = "X (1990)(P)" + tok.replace(" ...", " x")
                        self.assertIn(e["id"], tags.flag_types(tos(name)), tok)

    def test_every_classifier_token_is_in_the_catalog(self) -> None:
        cat = {e["id"]: set(e["tokens"]) for e in library.rule_catalog("tosec") if e["kind"] == "exclude"}
        for rule, words in tags._PAREN_WORDS.items():
            for w in words:
                self.assertIn(f"({w})", cat[rule], w)
        for rule, _rx, tokens in tags._BRACKET_RULES:
            for tok in tokens:
                self.assertIn(tok, cat[rule])
        self.assertIn("(demo-*)", cat["demo"])
        self.assertIn("(demo-playable)", cat["demo"])
        ni = {e["id"]: set(e["tokens"]) for e in library.rule_catalog("nointro") if e["kind"] == "exclude"}
        for w in ("beta", "proto", "demo", "sample", "kiosk", "debug"):
            self.assertTrue(any(w in t.lower() for ts in ni.values() for t in ts), w)
        # no token appears under two rules
        seen: dict[str, str] = {}
        for rule, toks in cat.items():
            for t in toks:
                self.assertNotIn(t, seen, t)
                seen[t] = rule

    def test_catalog_shape_and_defaults(self) -> None:
        cat = library.rule_catalog("tosec")
        self.assertEqual([e["id"] for e in cat if e["kind"] == "exclude"], list(tags.RULES))
        self.assertEqual([e["id"] for e in cat if e["kind"] == "keep_flag"], list(tags.KEEP_FLAGS))
        opts = {e["id"]: e for e in cat if e["kind"] == "option" and e.get("group") != "ratings"}
        self.assertEqual(set(opts), {"latest_only", "best_variant", "complete_only", "borrow_editions", "rescue", "languages"})
        self.assertFalse(opts["rescue"]["default"])
        self.assertEqual(opts["rescue"]["field"], "rescue_only_dump")
        self.assertEqual(opts["languages"]["default_value"], ["En"])
        for e in cat:
            if e.get("group") == "ratings":
                continue
            self.assertEqual(set(e), {"id", "field", "label", "kind", "default", "tokens", "description", "applies_to"}
                             | ({"default_value"} if "default_value" in e else set()))
            self.assertIn("tosec", e["applies_to"])
        ni = {e["id"]: e for e in library.rule_catalog("nointro")}
        self.assertEqual(ni["region_priority"]["default_value"], list(tags.DEFAULT_REGION_PRIORITY))
        self.assertNotIn("best_variant", ni)
        self.assertNotIn("cr", ni)
        self.assertIn("one_per_game", ni)

    def test_profile_info(self) -> None:
        info = library.profile_info(AMIGA)
        self.assertEqual(info["style"], "tosec")
        self.assertTrue(info["available"]["keep_flags"])
        self.assertFalse(info["available"]["one_per_game"])
        self.assertEqual(info["profile"], info["defaults"])
        n64 = library.profile_info(platforms.get_platform("Nintendo 64"))
        self.assertTrue(n64["available"]["one_per_game"])
        self.assertFalse(n64["available"]["keep_flags"])
        self.assertEqual(n64["regions"][:4], list(tags.DEFAULT_REGION_PRIORITY))
        import json
        json.dumps(info)


class OnePerGame(unittest.TestCase):
    def kept(self, names, profile=None):
        sel, by = ni_run(names, profile)
        return sorted(n for n, d in by.items() if d.action == KEEP)

    def test_region_priority_then_version(self) -> None:
        names = ["Game (USA)", "Game (Europe) (En,Fr,De)", "Game (Japan)", "Game (USA) (Rev 1)"]
        self.assertEqual(self.kept(names), ["Game (Europe) (En,Fr,De)"])
        prof = replace(library.default_profile(GBA), region_priority=("USA", "Europe"))
        self.assertEqual(self.kept(names, prof), ["Game (USA) (Rev 1)"])       # newest within the region
        prof = replace(library.default_profile(GBA), languages=("Ja", "En"), region_priority=("Japan",))
        self.assertEqual(self.kept(names, prof), ["Game (Japan)"])
        # language order is the first key: with English preferred the English Europe release wins
        prof = replace(library.default_profile(GBA), languages=("En", "Ja"), region_priority=("Japan",))
        self.assertEqual(self.kept(names, prof), ["Game (Europe) (En,Fr,De)"])

    def test_default_region_order(self) -> None:
        self.assertEqual(tags.region_order()[:4], ["Europe", "USA", "World", "Japan"])
        rest = tags.region_order()[4:]
        self.assertEqual(rest, sorted(rest))
        self.assertEqual(set(tags.region_order()), set(tags.REGIONS))

    def test_language_filters_first(self) -> None:
        names = ["G (Europe) (Fr,De)", "G (USA)", "G (Japan)"]
        self.assertEqual(self.kept(names), ["G (USA)"])
        prof = replace(library.default_profile(GBA), languages=("Ja",))
        self.assertEqual(self.kept(names, prof), ["G (Japan)"])
        off = replace(library.default_profile(GBA), keep_other_language=False)
        sel, by = ni_run(["G (Europe) (Fr,De)"], off)
        self.assertEqual(by["G (Europe) (Fr,De)"].codes, ("language",))

    def test_a_game_that_only_exists_in_other_languages_is_kept(self) -> None:
        sel, by = ni_run(["Only JP (Japan)", "Only JP (Japan) (Rev 1)", "Both (Japan)", "Both (USA)"])
        self.assertEqual(by["Only JP (Japan)"].action, "superseded")        # one per game: the newest revision wins
        self.assertEqual(by["Only JP (Japan) (Rev 1)"].action, "keep")
        self.assertIn("no version in your languages", by["Only JP (Japan) (Rev 1)"].reason)
        self.assertEqual(by["Both (USA)"].action, "keep")
        self.assertEqual(by["Both (Japan)"].codes, ("language",))          # an English version exists: unchanged

    def test_other_language_rule_follows_the_whole_dat_not_the_files_you_own(self) -> None:
        owned = ni_items(["G (Japan)"], GBA_DAT)
        dat = ni_items(["G (Japan)", "G (USA)"], GBA_DAT)
        prof = library.default_profile(GBA)
        lg = library.language_games(dat, prof, GBA)
        self.assertEqual(library.select(owned, prof, GBA, lang_games=lg).decisions[owned[0].key].codes, ("language",))
        self.assertEqual(library.select(owned, prof, GBA).decisions[owned[0].key].action, "keep")   # no DAT given: files only

    def test_distinct_products_are_not_merged(self) -> None:
        names = ["P (USA)", "P (USA) (Unl)", "P (USA) (Aftermarket)", "P (Europe) (Unl)", "P (USA) (Tengen)",
                 "P (USA) (Beta)", "P (USA) (Beta 2)", "P (USA) (Alt)", "P (Europe) (Virtual Console)"]
        prof = replace(library.default_profile(GBA), exclude=frozenset())
        kept = self.kept(names, prof)
        # P (USA) / (Alt) / (Virtual Console) are one game: the plain release wins (Europe first by priority,
        # so the Europe re-release is the best region and, being the only Europe one, is kept)
        self.assertIn("P (Europe) (Unl)", kept)
        self.assertIn("P (USA) (Aftermarket)", kept)
        self.assertIn("P (USA) (Tengen)", kept)
        self.assertIn("P (USA) (Beta)", kept)
        self.assertIn("P (USA) (Beta 2)", kept)
        self.assertNotIn("P (USA) (Alt)", kept)
        self.assertNotIn("P (USA)", kept)               # same game as the Europe Virtual Console copy
        self.assertEqual(len(kept), 6)

    def test_fewer_extra_tags_break_ties(self) -> None:
        self.assertEqual(self.kept(["Q (USA) (Alt)", "Q (USA)"]), ["Q (USA)"])

    def test_off_gives_per_region_latest(self) -> None:
        prof = replace(library.default_profile(GBA), one_per_game=False)
        self.assertEqual(self.kept(["R (USA)", "R (USA) (Rev 1)", "R (Europe)"], prof), ["R (Europe)", "R (USA) (Rev 1)"])

    def test_superseded_names_the_winner(self) -> None:
        _sel, by = ni_run(["S (USA)", "S (Europe)"])
        self.assertEqual((by["S (USA)"].action, by["S (USA)"].superseded_by), (SUPERSEDED, "S (Europe)"))


class ProfilePersistence(unittest.TestCase):
    def test_new_fields_roundtrip_and_old_profiles_load(self) -> None:
        p = replace(library.default_profile(AMIGA), languages=("En", "De"), keep_flags=frozenset({"h", "t"}),
                    rescue_only_dump=True)
        d = p.to_dict()
        self.assertEqual(d["languages"], ["En", "De"])
        self.assertEqual(d["keep_flags"], ["h", "t"])
        self.assertEqual(LibraryProfile.from_dict(d, library.default_profile(AMIGA)), p)
        import json
        self.assertEqual(LibraryProfile.from_dict(json.loads(json.dumps(d)), library.default_profile(AMIGA)), p)
        old = {"exclude": ["demo"], "latest_only": True, "best_variant": True, "complete_only": True}
        q = LibraryProfile.from_dict(old, library.default_profile(AMIGA))
        self.assertEqual((q.languages, q.keep_flags, q.rescue_only_dump), (("En",), frozenset(tags.KEEP_FLAGS), False))
        cfg = {"library": {AMIGA.name: old}}
        self.assertEqual(library.load_profile(cfg, AMIGA).languages, ("En",))

    def test_tolerates_junk_and_normalises(self) -> None:
        q = LibraryProfile.from_dict({"languages": ["De", "xx", "De", 3, "Fr"], "keep_flags": ["cr", "zz"],
                                      "region_priority": ["USA", "Atlantis", "USA"], "one_per_game": "yes",
                                      "rescue_only_dump": 1}, library.default_profile(GBA))
        self.assertEqual(q.languages, ("De", "Fr"))
        self.assertEqual(q.keep_flags, frozenset({"cr"}))
        self.assertEqual(q.region_priority, ("USA",))
        self.assertTrue(q.one_per_game)             # non-bool ignored -> default
        self.assertFalse(q.rescue_only_dump)
        self.assertEqual(LibraryProfile(languages=frozenset({"De", "En"})).languages, ("En", "De"))
        self.assertEqual(LibraryProfile(languages=["Fr", "En"]).languages, ("Fr", "En"))
        self.assertEqual(LibraryProfile.latest_only_profile().languages, ())
        self.assertFalse(LibraryProfile.latest_only_profile().one_per_game)

    def test_defaults_per_platform(self) -> None:
        a = library.default_profile(AMIGA)
        self.assertEqual((a.languages, a.one_per_game, a.rescue_only_dump), (("En",), False, False))
        g = library.default_profile(GBA)
        self.assertEqual((g.languages, g.one_per_game, g.region_priority),
                         (("En",), True, ("Europe", "USA", "World", "Japan")))


class BorrowTests(unittest.TestCase):
    """Amendment 12: disks of other editions complete a set (``borrow_other_editions``)."""

    EN = replace(library.default_profile(AMIGA), languages=("En",))
    OFF = replace(EN, borrow_other_editions=False)

    def go(self, names, profile=None):
        return run(names, profile or self.EN)

    def test_default_is_on_for_amiga_games_only(self) -> None:
        self.assertTrue(library.default_profile(AMIGA).borrow_other_editions)
        self.assertFalse(library.default_profile(GBA).borrow_other_editions)
        self.assertFalse(LibraryProfile.latest_only_profile().borrow_other_editions)
        info = library.profile_info(AMIGA)
        self.assertTrue(info["available"]["borrow_editions"])
        self.assertFalse(library.profile_info(GBA)["available"]["borrow_editions"])
        self.assertTrue(info["profile"]["borrow_other_editions"])
        opt = [e for e in info["catalog"] if e["id"] == "borrow_editions"][0]
        self.assertEqual((opt["field"], opt["default"], opt["kind"]), ("borrow_other_editions", True, "option"))
        self.assertIn("country", opt["description"])
        self.assertIn("never borrowed", opt["description"])

    def test_old_profiles_load_with_the_default_and_roundtrip(self) -> None:
        old = {"exclude": ["demo"], "latest_only": True, "best_variant": True, "complete_only": True,
               "languages": ["En"]}
        q = LibraryProfile.from_dict(old, library.default_profile(AMIGA))
        self.assertTrue(q.borrow_other_editions)
        off = replace(q, borrow_other_editions=False)
        self.assertFalse(LibraryProfile.from_dict(off.to_dict(), library.default_profile(AMIGA)).borrow_other_editions)
        cfg: dict = {}
        library.store_profile(cfg, AMIGA, off)
        self.assertFalse(library.load_profile(cfg, AMIGA).borrow_other_editions)
        self.assertTrue(library.load_profile({"library": {AMIGA.name: old}}, AMIGA).borrow_other_editions)

    D1 = "Foo (1991)(Pub)(Disk 1 of 3)"
    D3 = "Foo (1991)(Pub)(Disk 3 of 3)"
    D2_DE = "Foo (1991)(Pub)(DE)(Disk 2 of 3)"

    def test_missing_disk_from_another_country_even_in_an_unselected_language(self) -> None:
        names = [self.D1, self.D3, self.D2_DE]
        _i, sel, by = self.go(names)
        self.assertEqual([s.name for s in sel.sets], ["Foo (1991)(Pub)"])
        s = sel.sets[0]
        self.assertEqual({k: names[v - 1] for k, v in s.slots.items()}, {1: self.D1, 2: self.D2_DE, 3: self.D3})
        self.assertEqual(sorted(s.borrowed), [2])
        b = s.borrowed[2]
        self.assertEqual((b["name"], b["edition"], b["differences"]), (self.D2_DE + ".adf", "(DE)", ["country", "language"]))
        self.assertEqual(b["text"], f"disk 2 borrowed from the (DE) edition ({self.D2_DE})")
        # kept - not excluded for language, not superseded - and tagged machine-readably
        d = by[self.D2_DE]
        self.assertEqual((d.action, d.codes, d.set_id), (KEEP, (library.BORROWED_CODE,), s.id))
        self.assertIn("borrowed as disk 2 of Foo (1991)(Pub)", d.reason)
        self.assertEqual({by[self.D1].action, by[self.D3].action}, {KEEP})
        self.assertEqual(sel.incomplete, [])
        self.assertEqual(sel.borrow_summary(), {"sets": 1, "disks": 1, "by_difference": {"country": 1, "language": 1}})

    def test_option_off_restores_the_old_behaviour(self) -> None:
        names = [self.D1, self.D3, self.D2_DE]
        _i, sel, by = self.go(names, self.OFF)
        self.assertEqual(sel.sets, [])
        self.assertEqual((by[self.D1].action, by[self.D3].action, by[self.D2_DE].action),
                         (INCOMPLETE, INCOMPLETE, EXCLUDED))
        self.assertEqual(by[self.D2_DE].codes, ("language",))

    def test_elsewhere_text_is_unchanged_when_off(self) -> None:
        names = ["B.A.T. II (1992)(Ubi Soft)(DE)(Disk 1 of 2)", "B.A.T. II (1992)(Ubi Soft)(Disk 2 of 2)"]
        _i, sel, by = run(names, replace(ALL_LANGS, borrow_other_editions=False))
        self.assertIn("different country / language / edition", by[names[0]].reason)
        _i, sel, by = run(names, ALL_LANGS)               # option on: the neutral disk 2 completes the DE set
        self.assertEqual([sel.decisions[k].action for k in (1, 2)], [KEEP, KEEP])
        self.assertEqual(sel.sets[0].name, "B.A.T. II (1992)(Ubi Soft)(DE)")
        self.assertEqual(sel.sets[0].borrowed[2]["differences"], ["country", "language"])

    def test_the_other_editions_remaining_disks_keep_their_classification(self) -> None:
        de = [f"Foo (1991)(Pub)(DE)(Disk {i} of 3)" for i in (1, 2, 3)]
        names = [self.D1, self.D3, *de]
        _i, sel, by = self.go(names)
        self.assertEqual(by[de[1]].action, KEEP)                   # borrowed
        self.assertEqual((by[de[0]].action, by[de[2]].action), (EXCLUDED, EXCLUDED))   # German, not selected
        self.assertEqual(by[de[0]].codes, ("language",))
        self.assertEqual(sorted(s.name for s in sel.sets), ["Foo (1991)(Pub)"])
        # German selected too: the German set is complete on its own and kept; the English one still completes
        _i, sel, by = self.go(names, replace(self.EN, languages=("En", "De")))
        self.assertEqual({d.action for d in by.values()}, {KEEP})
        self.assertEqual(sorted(s.name for s in sel.sets), ["Foo (1991)(Pub)", "Foo (1991)(Pub)(DE)"])

    def test_own_edition_comes_first(self) -> None:
        d2 = "Foo (1991)(Pub)(Disk 2 of 3)"
        _i, sel, by = self.go([self.D1, d2, self.D3, self.D2_DE])
        self.assertEqual(sel.borrow_summary()["sets"], 0)
        self.assertEqual(by[d2].action, KEEP)
        self.assertEqual(by[self.D2_DE].action, EXCLUDED)          # the German disk stays out

    def test_language_preference_then_newest(self) -> None:
        fr = "Foo (1991)(Pub)(FR)(Disk 2 of 3)"
        _i, sel, _by = self.go([self.D1, self.D3, self.D2_DE, fr], replace(self.EN, languages=("En", "Fr")))
        self.assertEqual(sel.sets[0].borrowed[2]["name"], fr + ".adf")        # a selected language wins
        older = "Foo (1990)(Pub)(ES)(Disk 2 of 3)"
        newer = "Foo (1992)(Pub)(IT)(Disk 2 of 3)"
        _i, sel, _by = self.go([self.D1, self.D3, older, newer])
        self.assertEqual(sel.sets[0].borrowed[2]["name"], newer + ".adf")     # neither selected: newest
        self.assertIn("year", sel.sets[0].borrowed[2]["differences"])

    def test_quality_exclusions_are_never_borrowed(self) -> None:
        for bad in ("[b corrupt file]", "[v Saddam 1]", "[m]", "[o]", "[u]", "[faked x]", "[unreleased]"):
            _i, sel, by = self.go([self.D1, self.D3, f"Foo (1991)(Pub)(DE)(Disk 2 of 3){bad}"])
            self.assertEqual(sel.sets, [], bad)
            self.assertEqual(by[self.D1].action, INCOMPLETE, bad)
        for status in ("(pre-release)", "(proto)", "(demo-playable)", "(beta)"):
            _i, sel, by = self.go([self.D1, self.D3, f"Foo (1991)(Pub)(DE){status}(Disk 2 of 3)"])
            self.assertEqual(sel.sets, [], status)
        # ... but with the rule switched off the disk is a normal candidate again
        names = [self.D1 + "[m]", self.D3 + "[m]", "Foo (1991)(Pub)(DE)(Disk 2 of 3)[m]"]
        on = replace(self.EN, exclude=self.EN.exclude - {"modified"})
        self.assertEqual(len(self.go(names, on)[1].sets), 1)

    def test_same_title_publisher_and_disk_count_are_required(self) -> None:
        for other in ("Foo (1991)(Other)(DE)(Disk 2 of 3)", "Foo (1991)(Pub)(DE)(Disk 2 of 4)",
                      "Bar (1991)(Pub)(DE)(Disk 2 of 3)", "Foo II (1991)(Pub)(DE)(Disk 2 of 3)"):
            _i, sel, by = self.go([self.D1, self.D3, other])
            self.assertEqual(sel.sets, [], other)

    def test_chipset_must_fit(self) -> None:
        aga1 = "Foo (1991)(Pub)(AGA)(Disk 1 of 2)"
        aga3 = "Foo (1991)(Pub)(AGA)(Disk 3 of 3)"
        plain2 = "Foo (1991)(Pub)(DE)(Disk 2 of 2)"
        _i, sel, _b = self.go([aga1, plain2])                         # an AGA set never takes an OCS disk
        self.assertEqual(sel.sets, [])
        _i, sel, _b = self.go([aga1, "Foo (1991)(Pub)(DE)(AGA)(Disk 2 of 2)"])
        self.assertEqual(len(sel.sets), 1)
        _i, sel, _b = self.go([aga1, "Foo (1991)(Pub)(DE)(OCS-AGA)(Disk 2 of 2)"])   # a hybrid disk runs on both
        self.assertEqual(len(sel.sets), 1)
        ocs1 = "Foo (1991)(Pub)(Disk 1 of 2)"
        _i, sel, _b = self.go([ocs1, "Foo (1991)(Pub)(DE)(AGA)(Disk 2 of 2)"])        # an OCS set never takes AGA-only
        self.assertEqual(sel.sets, [])
        _i, sel, _b = self.go([ocs1, "Foo (1991)(Pub)(DE)(OCS-AGA)(Disk 2 of 2)"])
        self.assertEqual(len(sel.sets), 1)
        _i, sel, _b = self.go([ocs1, "Foo (1991)(Pub)(DE)(ECS)(Disk 2 of 2)"])        # ECS counts as OCS
        self.assertEqual(len(sel.sets), 1)
        self.assertEqual(aga3[:3], "Foo")

    def test_dump_flags_must_fit_disk_1(self) -> None:
        d1 = "Foo (1991)(Pub)(Disk 1 of 2)[cr X]"
        _i, sel, _b = self.go([d1, "Foo (1991)(Pub)(DE)(Disk 2 of 2)[cr Y]"])         # a different crack
        self.assertEqual(sel.sets, [])
        _i, sel, _b = self.go([d1, "Foo (1991)(Pub)(DE)(Disk 2 of 2)[cr]"])           # a generic crack flag fits
        self.assertEqual(len(sel.sets), 1)
        _i, sel, _b = self.go([d1, "Foo (1991)(Pub)(DE)(Disk 2 of 2)"])               # a bare disk fits any crack
        self.assertEqual(len(sel.sets), 1)
        _i, sel, _b = self.go(["Foo (1991)(Pub)(Disk 1 of 2)", "Foo (1991)(Pub)(DE)(Disk 2 of 2)[cr Y]"])
        self.assertEqual(sel.sets, [])                                                 # uncracked 1 + cracked 2
        # crack-compatible candidates win over incompatible ones, whatever their language
        _i, sel, _b = self.go([d1, "Foo (1991)(Pub)(DE)(Disk 2 of 2)[cr Y]", "Foo (1991)(Pub)(FR)(Disk 2 of 2)"])
        self.assertEqual(sel.sets[0].borrowed[2]["edition"], "(FR)")

    def test_borrowing_only_when_the_group_has_no_complete_set(self) -> None:
        own = [f"Foo (1991)(Pub)(Disk {i} of 3)" for i in (1, 2, 3)]
        newer = "Foo v1.1 (1992)(Pub)(Disk 1 of 3)[cr Q]"
        _i, sel, by = self.go([*own, newer, self.D2_DE])
        self.assertEqual(sel.borrow_summary()["sets"], 0)           # newer disk 1 + own disks 2/3 is complete as before
        self.assertEqual(by[own[0]].action, SUPERSEDED)
        self.assertEqual(by[self.D2_DE].action, EXCLUDED)
        _i, sel, by = self.go([*own, self.D2_DE])
        self.assertEqual((sel.borrow_summary()["sets"], by[self.D2_DE].action), (0, EXCLUDED))

    def test_idempotent_and_borrowed_disk_never_superseded(self) -> None:
        names = [self.D1, self.D3, self.D2_DE, "Foo (1990)(Pub)(DE)(Disk 2 of 3)", "Foo (1991)(Pub)(DE)(Disk 1 of 3)"]
        items, sel, by = self.go(names)
        kept = [it for it in items if sel.decisions[it.key].action == KEEP]
        again = library.select(kept, self.EN, AMIGA)
        self.assertEqual([again.decisions[it.key].action for it in kept], [KEEP] * len(kept))
        self.assertEqual([s.name for s in again.sets], [s.name for s in sel.sets])
        # the older German disk 2 is a plain excluded (language) file, the newest one is borrowed
        self.assertEqual(by["Foo (1990)(Pub)(DE)(Disk 2 of 3)"].action, EXCLUDED)
        self.assertEqual(by[self.D2_DE].action, KEEP)

    def test_workbench_and_kickstart_disks_never_borrow(self) -> None:
        names = ["Workbench v1.3 (1988)(Commodore)(Disk 1 of 2)", "Workbench v1.3 (1988)(Commodore)(DE)(Disk 2 of 2)"]
        for dat in (WB, KSD):
            items = amiga_items(names, dat)
            sel = library.select(items, self.EN, AMIGA)
            self.assertEqual(sel.borrow_summary()["sets"], 0)
            self.assertEqual({d.action for d in sel.decisions.values()}, {INCOMPLETE})



class IdempotenceProperty(unittest.TestCase):
    """Re-selecting the kept output changes nothing, for random profiles and name sets."""

    AMIGA_NAMES = [
        "A (1990)(P)", "A (1990)(P)(AGA)", "A (1990)(P)(AGA)[cr X]", "A (1990)(P)(DE)(de)", "A (1990)(P)(DE)(de)[cr Y]",
        "A v1.1 (1991)(P)[h Z]", "A (1990)(P)[b]", "A (1990)(P)[tr de]", "A (1990)(P)(demo-playable)",
        "B (1990)(P)(AGA)(Disk 1 of 2)[cr A]", "B (1990)(P)(AGA)(Disk 2 of 2)", "B (1990)(P)(Disk 1 of 2)",
        "B (1990)(P)(Disk 2 of 2)", "B (1990)(P)(Disk 1 of 2)[cr A]", "B (1990)(P)(DE)(Disk 1 of 2)",
        "B (1990)(P)(DE)(Disk 2 of 2)", "C (1990)(P)(OCS-AGA)[cr A][t +1 B]", "C v1.2 (1992)(P)[a]",
        "C (1990)(P)(ECS-AGA)(Disk 1 of 3)(Program)[cr H]", "C (1990)(P)(ECS-AGA)(Disk 2 of 3)(AGA Data)",
        "C (1990)(P)(ECS-AGA)(Disk 3 of 3)(ECS Data)", "D (1990)(P)(FR)", "D (1990)(P)(FR)[f x]",
        "D (1990)(P)(M3)", "E (1990)(P)[m]", "E (1990)(P)[cr X][m bam]",
        "F (1991)(P)(Disk 1 of 3)[cr Q]", "F (1991)(P)(Disk 3 of 3)", "F (1991)(P)(DE)(Disk 2 of 3)",
        "F (1991)(P)(FR)(Disk 2 of 3)[cr Q]", "F (1992)(P)(FR)(Disk 1 of 3)", "F (1992)(P)(DE)(Disk 3 of 3)",
        "G (1991)(P)(AGA)(Disk 1 of 2)", "G (1991)(P)(DE)(Disk 2 of 2)", "G (1991)(P)(DE)(AGA)(Disk 2 of 2)",
        "G (1991)(P)(Disk 2 of 2)[b]",
    ]
    NI_NAMES = [
        "G (USA)", "G (USA) (Rev 1)", "G (Europe) (En,Fr,De)", "G (Europe) (Fr,De) (Rev 1)", "G (Japan)", "G (Japan) (En)",
        "G (USA) (Beta)", "G (USA) (Unl)", "G (Europe) (Unl)", "G (World)", "G (Germany)", "H (Japan)", "H (Asia)",
        "H (USA) (Alt)", "H (USA)", "I (Europe) (Virtual Console)", "I (USA)", "I (Brazil)", "I (Poland)",
    ]

    def check(self, names, plat, style_items, seed):
        rng = random.Random(seed)
        prof = LibraryProfile(
            exclude=frozenset(r for r in library.RULES if rng.random() < 0.8),
            latest_only=rng.random() < 0.8, best_variant=rng.random() < 0.8, complete_only=rng.random() < 0.8,
            languages=tuple(rng.sample(["En", "De", "Fr", "Ja", "Pl"], rng.randint(0, 3))),
            keep_flags=frozenset(f for f in tags.KEEP_FLAGS if rng.random() < 0.8),
            rescue_only_dump=rng.random() < 0.5,
            region_priority=tuple(rng.sample(["Europe", "USA", "World", "Japan", "Germany"], rng.randint(0, 4))),
            one_per_game=rng.random() < 0.7, borrow_other_editions=rng.random() < 0.7)
        items = style_items(names)
        sel = library.select(items, prof, plat)
        kept = [it for it in items if sel.decisions[it.key].action == KEEP]
        rng.shuffle(kept)
        again = library.select(kept, prof, plat)
        bad = [it.rom.name for it in kept if again.decisions[it.key].action != KEEP]
        self.assertEqual(bad, [], prof)
        shuffled = items[:]
        rng.shuffle(shuffled)
        sel3 = library.select(shuffled, prof, plat)
        self.assertEqual({k: d.action for k, d in sel.decisions.items()}, {k: d.action for k, d in sel3.decisions.items()})

    def test_amiga(self) -> None:
        for seed in range(60):
            self.check(self.AMIGA_NAMES, AMIGA, lambda n: amiga_items(n), seed)

    def test_nointro(self) -> None:
        for seed in range(60):
            self.check(self.NI_NAMES, GBA, lambda n: ni_items(n, GBA_DAT), seed)


if __name__ == "__main__":
    unittest.main()
