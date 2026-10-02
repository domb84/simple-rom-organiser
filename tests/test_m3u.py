"""Tests for romorg.m3u (synthetic fixtures + optional real-DAT coverage check)."""

from __future__ import annotations

import os
import tempfile
import unittest
import zlib
from pathlib import Path

from romorg.datfile import Rom
from romorg.m3u import (
    M3U_MARKER, DiskCand, PlaylistDisk, PlaylistSpec, content_of, group_disk_sets, is_neutral_flag,
    parse_disk_name, plan_m3us, plan_playlists, plan_stale, resolve_slots, write_m3us, write_playlist,
)
from romorg.scanner import Entry, Match, ScanResult

REAL_DAT = Path(
    "/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/cfc5ea37-4261-428c-9422-29acd65cac97/"
    "scratchpad/dats/TOSEC/Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat"
)


def _rom(name: str) -> Rom:
    crc = f"{zlib.crc32(name.encode()) & 0xFFFFFFFF:08x}"
    return Rom(name=name, size=901120, crc=crc, md5="", sha1="", game=name.rsplit(".", 1)[0])


class ParseTests(unittest.TestCase):
    def test_basic(self) -> None:
        d = parse_disk_name("Lemmings (1991)(Psygnosis)(Disk 2 of 3)[cr SR][t +2].adf")
        assert d is not None
        self.assertEqual((d.title, d.index, d.total, d.label), ("Lemmings (1991)(Psygnosis)", 2, 3, ""))
        self.assertEqual(d.flags, ("[cr SR]", "[t +2]"))

    def test_labels_letters_side(self) -> None:
        d = parse_disk_name("Game (1990)(Pub)(Disk 1 of 2)(Program)(Boot)[a].adf")
        assert d is not None
        self.assertEqual((d.title, d.label, d.flags), ("Game (1990)(Pub)", "Program - Boot", ("[a]",)))
        d = parse_disk_name("Game (1990)(Pub)(Disk B of C).adf")
        assert d is not None
        self.assertEqual((d.index, d.total), (2, 3))
        d = parse_disk_name("Game (1990)(Pub)(Side A of B).adf")
        assert d is not None
        self.assertEqual((d.index, d.total), (1, 2))
        d = parse_disk_name("Abduction (1998)(Epic)(PAL)(Disk 2 of 3)(Disk1)[a].adf")
        assert d is not None
        self.assertEqual((d.title, d.index, d.label), ("Abduction (1998)(Epic)(PAL)", 2, "Disk1"))

    def test_not_multi(self) -> None:
        self.assertIsNone(parse_disk_name("Game (1990)(Pub)[cr X].adf"))
        self.assertIsNone(parse_disk_name("Game (1990)(Discovery).adf"))
        self.assertIsNone(parse_disk_name("Game (1990)(Pub)(Disk 1 of 1).adf"))

    def test_neutral(self) -> None:
        for f in ("[!]", "[bootable]", "[cp manual code]", "[docs]", "[HD]", "[1Mb Chip]"):
            self.assertTrue(is_neutral_flag(f), f)
        for f in ("[cr SR]", "[cr]", "[t +2 X]", "[a]", "[a2]", "[b dump]", "[h Foo]", "[tr en]"):
            self.assertFalse(is_neutral_flag(f), f)


class GroupAndWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _matches(self, names: list[str], sub: str = "") -> list[Match]:
        out = []
        for n in names:
            p = self.root / sub / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(os.urandom(64))
            r = _rom(n)
            out.append(Match(Entry(p, None, r.size, r.crc, None), [r]))
        return out

    def _result(self, matches: list[Match], unmatched: list[Entry] | None = None,
                missing: list[str] | None = None) -> ScanResult:
        return ScanResult(self.root, "t", matches, unmatched or [], [], [], [_rom(n) for n in missing or []])

    def test_per_disk_flags_group(self) -> None:
        ms = self._matches([
            "'Allo (1993)(Alt)(Disk 1 of 2)[cr CSL].adf",
            "'Allo (1993)(Alt)(Disk 1 of 2)[cr CSL][t +12 GOD].adf",
            "'Allo (1993)(Alt)(Disk 2 of 2)[cr CSL].adf",
            "'Allo (1993)(Alt)(Disk 2 of 2)[cr QTX].adf",  # other crack: never mixed in
        ])
        # There is no [cr QTX] disk 1: that disk 2 is never mixed into the other sets and
        # never forms a set of its own.
        sets = {s.key: s for s in group_disk_sets(self._result(ms))}
        self.assertEqual(set(sets), {"'Allo (1993)(Alt)[cr CSL]", "'Allo (1993)(Alt)[cr CSL][t +12 GOD]"})
        t = sets["'Allo (1993)(Alt)[cr CSL][t +12 GOD]"]
        self.assertTrue(t.complete)
        self.assertEqual(t.disks[1].entry.path.name, "'Allo (1993)(Alt)(Disk 1 of 2)[cr CSL][t +12 GOD].adf")
        self.assertEqual(t.disks[2].entry.path.name, "'Allo (1993)(Alt)(Disk 2 of 2)[cr CSL].adf")

    def test_neutral_flags_and_labels(self) -> None:
        ms = self._matches([
            "Barb (1991)(Psy)(Disk 1 of 2)(Intro)[cr SR][!].adf",
            "Barb (1991)(Psy)(Disk 2 of 2)(1)[cr SR][t +36 PNS][bootable].adf",
        ])
        sets = group_disk_sets(self._result(ms))
        complete = [s for s in sets if s.complete]
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0].labels, {1: "Intro", 2: "1"})

    def test_contained_incomplete_dropped(self) -> None:
        ms = self._matches([
            "G (1990)(P)(Disk 1 of 2)[!].adf",
            "G (1990)(P)(Disk 2 of 2)[cr X].adf",
        ])
        sets = group_disk_sets(self._result(ms))
        # the "[!]" variant {disk 1} is contained in the complete [cr X] set -> dropped
        self.assertEqual([(s.key, s.complete) for s in sets], [("G (1990)(P)[cr X]", True)])

    def test_uncontained_incomplete_reported(self) -> None:
        ms = self._matches([
            "G (1990)(P)(Disk 1 of 2).adf",
            "G (1990)(P)(Disk 1 of 2)[cr X].adf",
            "G (1990)(P)(Disk 2 of 2)[cr X].adf",
        ])
        # no clean disk 2: the clean disk 1 can never complete; the game has a complete set,
        # so the incomplete variant is not reported ...
        sets = group_disk_sets(self._result(ms))
        self.assertEqual([(s.key, s.complete, s.missing) for s in sets], [("G (1990)(P)[cr X]", True, [])])
        # ... but resolve_slots (used by the library selection) does return it
        found = resolve_slots([DiskCand(i, m.roms[0].name) for i, m in enumerate(ms)])
        self.assertEqual([(s.name, s.complete, s.missing) for s in found],
                         [("G (1990)(P)", False, [2]), ("G (1990)(P)[cr X]", True, [])])

    def test_generalised_flags_complete_a_set(self) -> None:
        # Real TOSEC patterns: the untouched disk carries a bare / shorter crack flag.
        ms = self._matches([
            "Wolfchild (1992)(Core)(Disk 1 of 2)[cr CSL].adf",
            "Wolfchild (1992)(Core)(Disk 2 of 2)[cr].adf",
            "Winter Camp (1993)(X)(Disk 1 of 2)[cr Galahad v1].adf",
            "Winter Camp (1993)(X)(Disk 2 of 2)[cr Galahad].adf",
            "Robo (1990)(A)(Disk 1 of 2)[cr SR - Valhalla].adf",
            "Robo (1990)(A)(Disk 1 of 2)[cr Angels - Genesis].adf",
            "Robo (1990)(A)(Disk 2 of 2)[cr].adf",
            "Alt (1990)(A)(Disk 1 of 2)[a].adf",
            "Alt (1990)(A)(Disk 2 of 2)[a2].adf",  # [a] does not generalise [a2]
        ])
        sets = {s.key: s for s in group_disk_sets(self._result(ms))}
        for key in ("Wolfchild (1992)(Core)[cr CSL]", "Winter Camp (1993)(X)[cr Galahad v1]",
                    "Robo (1990)(A)[cr SR - Valhalla]", "Robo (1990)(A)[cr Angels - Genesis]"):
            self.assertTrue(sets[key].complete, key)
        self.assertNotIn("Wolfchild (1992)(Core)[cr]", sets)  # contained in the full set
        # [a] does not generalise [a2]: no best-effort set, the game is incomplete
        self.assertFalse(any(s.complete for s in sets.values() if s.key.startswith("Alt")))

    def test_exact_flag_preferred_over_generalised(self) -> None:
        ms = self._matches([
            "E (1990)(P)(Disk 1 of 2)[cr X].adf",
            "E (1990)(P)(Disk 2 of 2)[cr].adf",
            "E (1990)(P)(Disk 2 of 2)[cr X].adf",
        ])
        sets = {s.key: s for s in group_disk_sets(self._result(ms))}
        self.assertEqual(sets["E (1990)(P)[cr X]"].disks[2].entry.path.name, "E (1990)(P)(Disk 2 of 2)[cr X].adf")

    def test_name_uses_only_shared_notes_and_build_notes_not_neutral(self) -> None:
        ms = self._matches([
            "Bene (1994)(Psy)(Disk 1 of 2)[easy levels].adf",
            "Bene (1994)(Psy)(Disk 2 of 2)[hard levels].adf",
            "LQ (1991)(Ubi)(Disk 1 of 2)[beta 3].adf",
            "LQ (1991)(Ubi)(Disk 2 of 2)[beta 1].adf",
            "Both (1991)(X)(Disk 1 of 2)[!].adf",
            "Both (1991)(X)(Disk 2 of 2)[!].adf",
        ])
        keys = {s.key: s for s in group_disk_sets(self._result(ms))}
        self.assertTrue(keys["Bene (1994)(Psy)"].complete)
        self.assertTrue(keys["Both (1991)(X)[!]"].complete)  # shared by every disk: kept
        lq = [s for s in keys.values() if s.key.startswith("LQ")]
        self.assertFalse(any(s.complete and not s.note for s in lq))  # betas never mixed silently
        self.assertFalse(is_neutral_flag("[beta 3]"))
        self.assertFalse(is_neutral_flag("[AGA version]"))

    def test_name_independent_of_other_sets(self) -> None:
        three = [f"SotB (1990)(Psy)(Disk {i} of 3)[cr A].adf" for i in (1, 2, 3)]
        two = ["SotB (1990)(Psy)(Disk 1 of 2)[cr A].adf"]
        dat = three + two + ["SotB (1990)(Psy)(Disk 2 of 2)[cr A].adf"]
        ms = self._matches(three)
        names1 = {o.path.name for o in plan_m3us(self._result(ms, missing=dat[3:]))}
        ms2 = ms + self._matches(two)
        names2 = {o.path.name: o for o in plan_m3us(self._result(ms2, missing=dat[4:]))}
        self.assertEqual(names1, {"SotB (1990)(Psy)[cr A] (3 disks).m3u"})
        self.assertEqual(names2["SotB (1990)(Psy)[cr A] (3 disks).m3u"].status, "write")
        self.assertEqual(names2["SotB (1990)(Psy)[cr A] (2 disks).m3u"].status, "incomplete")

    def test_case_insensitive_name_collision(self) -> None:
        ms = self._matches([
            "Teen (1995)(M)(Disk 1 of 2).adf", "Teen (1995)(M)(Disk 2 of 2).adf",
        ], sub="x") + self._matches([
            "TEEN (1995)(M)(Disk 1 of 2).adf", "TEEN (1995)(M)(Disk 2 of 2).adf",
        ], sub="x")
        ops = [o for o in plan_m3us(self._result(ms)) if o.status == "write"]
        self.assertEqual(len(ops), 2)
        self.assertEqual(len({str(o.path).casefold() for o in ops}), 2)

    def test_stale_own_playlists_removed(self) -> None:
        ms = self._matches(["S (1990)(P)(Disk 1 of 2).adf", "S (1990)(P)(Disk 2 of 2).adf"], sub="new")
        old = self.root / "old" / "S (1990)(P).m3u"
        old.parent.mkdir()
        old.write_text(M3U_MARKER + "\ngone1.adf\ngone2.adf\n", encoding="utf-8")
        mine = self.root / "old" / "Mine.m3u"
        mine.write_text("gone1.adf\n", encoding="utf-8")  # user's: never touched
        twin = self.root / "new" / "S (1990)(P) old name.m3u"
        twin.write_text(M3U_MARKER + "\nS (1990)(P)(Disk 1 of 2).adf\nS (1990)(P)(Disk 2 of 2).adf|Disk 2\n",
                        encoding="utf-8")
        ops = {o.path: o for o in plan_m3us(self._result(ms))}
        self.assertEqual(ops[old].status, "stale")
        self.assertEqual(ops[twin].status, "stale")
        self.assertNotIn(mine, ops)
        res = write_m3us(list(ops.values()))
        self.assertEqual((res["written"], res["removed"], res["failed"]), (1, 2, []))
        self.assertFalse(old.exists() or twin.exists())
        self.assertTrue(mine.exists())
        self.assertEqual([p.name for p in (self.root / "new").iterdir() if p.name.startswith(".")], [])

    def test_plan_and_write(self) -> None:
        ms = self._matches([
            "Monkey (1990)(Lucas)(Disk 1 of 2)(Program).adf",
            "Monkey (1990)(Lucas)(Disk 2 of 2).adf",
            "#Hash (1990)(P)(Disk 1 of 2).adf",
            "#Hash (1990)(P)(Disk 2 of 2).adf",
            "Inc (1990)(P)(Disk 1 of 3).adf",
        ], sub="games")
        ops = {op.path.name: op for op in plan_m3us(self._result(ms), savedisk=True)}
        op = ops["Monkey (1990)(Lucas).m3u"]
        self.assertEqual(op.status, "write")
        self.assertEqual(op.path.parent, self.root / "games")
        self.assertEqual(op.lines, [
            M3U_MARKER,
            "Monkey (1990)(Lucas)(Disk 1 of 2)(Program).adf|Program",
            "Monkey (1990)(Lucas)(Disk 2 of 2).adf|Disk 2",
            "#SAVEDISK:",
        ])
        self.assertTrue(ops["#Hash (1990)(P).m3u"].lines[1].startswith("./#Hash"))
        self.assertEqual(ops["Inc (1990)(P).m3u"].status, "incomplete")
        self.assertIn("2, 3", ops["Inc (1990)(P).m3u"].reason)

        res = write_m3us(list(ops.values()))
        self.assertEqual(res["written"], 2)
        raw = op.path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"|Disk 2\n#SAVEDISK:\n"))

        # re-plan: identical -> ok; ours changed -> write; foreign -> conflict
        again = {o.path.name: o for o in plan_m3us(self._result(ms), savedisk=True)}
        self.assertEqual(again["Monkey (1990)(Lucas).m3u"].status, "ok")
        nolabel = {o.path.name: o for o in plan_m3us(self._result(ms), labels=False)}
        self.assertEqual(nolabel["Monkey (1990)(Lucas).m3u"].status, "write")
        self.assertEqual(nolabel["Monkey (1990)(Lucas).m3u"].lines[1],
                         "Monkey (1990)(Lucas)(Disk 1 of 2)(Program).adf")
        op.path.write_text("Monkey (1990)(Lucas)(Disk 1 of 2)(Program).adf\n", encoding="utf-8")
        foreign = {o.path.name: o for o in plan_m3us(self._result(ms))}
        self.assertEqual(foreign["Monkey (1990)(Lucas).m3u"].status, "conflict")

    def test_spread_dirs_and_archives(self) -> None:
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        r1, r2 = _rom("Z (1990)(P)(Disk 1 of 2).adf"), _rom("Z (1990)(P)(Disk 2 of 2).adf")
        zip1 = self.root / "a" / "z1.zip"
        multi = self.root / "b" / "both.zip"
        zip1.write_bytes(b"x")
        multi.write_bytes(b"x")
        m1 = Match(Entry(zip1, "inner1.adf", r1.size, r1.crc, None), [r1])
        m2 = Match(Entry(multi, "inner2.adf", r2.size, r2.crc, None), [r2])
        other = Entry(multi, "other.adf", 5, "00000000", None)
        ops = plan_m3us(self._result([m1, m2], [other]), labels=False)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].path, self.root / "a" / "Z (1990)(P).m3u")
        self.assertEqual(ops[0].lines[1:], ["z1.zip", "../b/both.zip#inner2.adf"])

    def test_rar_and_pipe(self) -> None:
        r1, r2 = _rom("R (1990)(P)(Disk 1 of 2).adf"), _rom("R (1990)(P)(Disk 2 of 2).adf")
        m1 = Match(Entry(self.root / "r.rar", "x.adf", r1.size, r1.crc, None), [r1])
        m2 = Match(Entry(self.root / "y.adf", None, r2.size, r2.crc, None), [r2])
        sets = group_disk_sets(self._result([m1, m2]))
        self.assertEqual(sets[0].missing, [1])  # .rar is unusable by PUAE
        ms = self._matches(["p|1.adf", "p2.adf"])
        ms[0].roms = [_rom("P (1990)(Disk 1 of 2).adf")]
        ms[1].roms = [_rom("P (1990)(Disk 2 of 2).adf")]
        ops = plan_m3us(self._result(ms))
        self.assertEqual(ops[0].status, "conflict")


ABC = "ABC Monday Night Football"


class SlotWiseTests(GroupAndWriteTests):
    """Regression for the per-disk-slot resolution (TOSEC lists only the disks that changed)."""

    test_names = [n for n in dir(unittest.TestCase)]  # (inherited tests are skipped below)

    def run(self, result=None):  # don't re-run the parent's tests in this subclass
        if self._testMethodName in GroupAndWriteTests.__dict__:
            return None
        return super().run(result)

    def _abc(self, extra: list[str] | None = None) -> list[Match]:
        return self._matches([
            f"{ABC} v1.1 (1991)(Data East)(US)(Disk 1 of 3)[cr SR].adf",
            f"{ABC} (1990)(Data East)(US)(Disk 2 of 3).adf",
            f"{ABC} (1990)(Data East)(US)(Disk 3 of 3).adf",
        ] + (extra or []))

    def test_abc_v11_uses_1990_disks(self) -> None:
        res = self._result(self._abc())
        sets = group_disk_sets(res)
        self.assertEqual([(s.key, s.complete, s.missing) for s in sets],
                         [(f"{ABC} v1.1 (1991)(Data East)(US)[cr SR]", True, [])])
        ops = plan_m3us(res)
        op = next(o for o in ops if o.status == "write")
        self.assertEqual(op.path.name, f"{ABC} v1.1 (1991)(Data East)(US)[cr SR].m3u")
        self.assertEqual(op.lines, [
            M3U_MARKER,
            f"{ABC} v1.1 (1991)(Data East)(US)(Disk 1 of 3)[cr SR].adf|Disk 1",
            f"{ABC} (1990)(Data East)(US)(Disk 2 of 3).adf|Disk 2",
            f"{ABC} (1990)(Data East)(US)(Disk 3 of 3).adf|Disk 3",
        ])
        self.assertFalse(any(o.status == "incomplete" for o in ops))

    def test_v10_disks_win_for_older_and_pre_release_never_borrowed(self) -> None:
        extra = [
            f"{ABC} v1.0 (1990)(Data East)(US)(Disk 1 of 3).adf",
            # a NEWER pre-release disk 2/3: never borrowed from
            f"{ABC} v1.2 (1991)(Data East)(US)(pre-release)(Disk 2 of 3).adf",
            f"{ABC} v1.2 (1991)(Data East)(US)(pre-release)(Disk 3 of 3).adf",
            f"{ABC} v1.2 (1991)(Data East)(US)(pre-release)(Disk 1 of 3).adf",
        ]
        res = self._result(self._abc(extra))
        sets = {s.key: s for s in group_disk_sets(res)}
        self.assertEqual(set(sets), {f"{ABC} v1.1 (1991)(Data East)(US)[cr SR]",
                                     f"{ABC} v1.0 (1990)(Data East)(US)"})
        for s in sets.values():
            self.assertTrue(s.complete)
            for i in (2, 3):
                self.assertNotIn("pre-release", s.disks[i].entry.path.name)
        # even when the exclusion rules are off, a pre-release is another identity
        sets = group_disk_sets(res, exclude_rules=())
        for s in sets:
            if "pre-release" not in s.key:
                self.assertFalse(any("pre-release" in m.entry.path.name for m in s.disks.values()))

    def test_bad_dump_disk_never_borrowed(self) -> None:
        res = self._result(self._matches([
            f"{ABC} v1.1 (1991)(Data East)(US)(Disk 1 of 2)[cr SR].adf",
            f"{ABC} (1990)(Data East)(US)(Disk 2 of 2)[b corrupt file].adf",
        ]))
        sets = group_disk_sets(res)
        self.assertEqual([(s.complete, s.missing) for s in sets], [(False, [1, 2][1:])])
        self.assertEqual(plan_m3us(res)[0].status, "incomplete")
        # with the bad-dump rule off the disk is usable ([b ..] is not generalised by a bare flag
        # set, so it must be in the anchor's flags to fit: it is not -> still incomplete)
        sets = group_disk_sets(res, exclude_rules=())
        self.assertFalse(sets[0].complete)

    def test_newer_disk_not_borrowed_by_older_anchor(self) -> None:
        names = [
            f"{ABC} v1.0 (1990)(Data East)(US)(Disk 1 of 2).adf",
            f"{ABC} v1.0 (1990)(Data East)(US)(Disk 2 of 2).adf",
            f"{ABC} v1.1 (1991)(Data East)(US)(Disk 2 of 2).adf",
        ]
        cands = [DiskCand(i, n) for i, n in enumerate(names)]
        sets = resolve_slots(cands)
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0].slots[2].name, names[1])  # v1.1 disk 2 is newer than disk 1 v1.0
        # newest older-or-equal disk wins
        names2 = names + [f"{ABC} (1989)(Data East)(US)(Disk 2 of 2).adf"]
        sets = resolve_slots([DiskCand(i, n) for i, n in enumerate(names2)])
        self.assertEqual(sets[0].slots[2].name, names[1])

    def test_v11_disk1_with_v10_disk2(self) -> None:
        names = [
            f"{ABC} v1.1 (1991)(Data East)(US)(Disk 1 of 2).adf",
            f"{ABC} v1.0 (1990)(Data East)(US)(Disk 2 of 2).adf",
        ]
        sets = resolve_slots([DiskCand(i, n) for i, n in enumerate(names)])
        self.assertTrue(sets[0].complete)
        self.assertEqual(sets[0].name, f"{ABC} v1.1 (1991)(Data East)(US)")

    def test_different_cracks_never_mixed(self) -> None:
        names = [
            "X (1990)(P)(Disk 1 of 2)[cr AAA].adf",
            "X (1990)(P)(Disk 2 of 2)[cr BBB].adf",
        ]
        sets = resolve_slots([DiskCand(i, n) for i, n in enumerate(names)])
        self.assertFalse(any(s.complete for s in sets))
        self.assertEqual([s.missing for s in sets if s.anchor], [[2]])

    def test_no_disk_one_gives_incomplete_pseudo_set(self) -> None:
        names = ["X (1990)(P)(Disk 2 of 3).adf", "X (1990)(P)(Disk 3 of 3).adf"]
        sets = resolve_slots([DiskCand(i, n) for i, n in enumerate(names)])
        self.assertEqual(len(sets), 1)
        self.assertIsNone(sets[0].anchor)
        self.assertEqual(sets[0].missing, [1])
        self.assertFalse(sets[0].complete)

    def test_merged_and_contained_sets(self) -> None:
        names = [
            "G (1990)(P)(Disk 1 of 2)[cr X].adf",
            "G (1990)(P)(Disk 1 of 2)[cr X][t +1 Y].adf",
            "G (1990)(P)(Disk 2 of 2)[cr X].adf",
        ]
        sets = resolve_slots([DiskCand(i, n) for i, n in enumerate(names)])
        self.assertEqual([s.name for s in sets], ["G (1990)(P)[cr X]", "G (1990)(P)[cr X][t +1 Y]"])
        self.assertTrue(all(s.complete for s in sets))
        # same selection from two anchors -> merged
        sets = resolve_slots([DiskCand(0, "H (1990)(P)(Disk 1 of 2).adf"),
                              DiskCand(1, "H (1990)(P)(Disk 2 of 2)[t +1 Z].adf")])
        self.assertEqual(len(sets), 1)
        self.assertTrue(sets[0].complete)  # trainer on disk 2 only: the variant joins disk 1

    def test_plan_playlists_and_stale(self) -> None:
        root = self.root
        for n in ("a/d1.adf", "a/d2.adf", "z.zip"):
            (root / n).parent.mkdir(exist_ok=True)
            (root / n).write_bytes(b"x")
        spec = PlaylistSpec("Game (1990)(P)", "Games", 2, [
            PlaylistDisk(2, root / "a" / "d2.adf", None, "Data"),
            PlaylistDisk(1, root / "a" / "d1.adf", None, ""),
        ])
        ops = plan_playlists([spec, PlaylistSpec("game (1990)(P)", "Games", 2, spec.disks)], root)
        self.assertEqual([o.status for o in ops], ["write", "write"])
        self.assertEqual(ops[0].path, root / "a" / "Game (1990)(P).m3u")
        self.assertEqual(ops[1].path.name, "game (1990)(P) (2).m3u")  # case-insensitive collision
        self.assertEqual(ops[0].lines, [M3U_MARKER, "d1.adf|Disk 1", "d2.adf|Data"])
        sha, size = write_playlist(ops[0].path, content_of(ops[0].lines))
        self.assertEqual(size, len(content_of(ops[0].lines).encode()))
        self.assertEqual(len(sha), 40)
        with self.assertRaises(FileExistsError):
            write_playlist(ops[0].path, "other\n")
        write_playlist(ops[0].path, content_of(ops[0].lines) + "#x\n", replace_own=True)
        self.assertEqual(plan_playlists([spec], root)[0].status, "write")
        # a foreign m3u is never replaced
        foreign = root / "a" / "foreign.m3u"
        foreign.write_text("d1.adf\n")
        with self.assertRaises(FileExistsError):
            write_playlist(foreign, "x\n", replace_own=True)
        # stale: own playlist whose disk is about to move / is gone, never a foreign one
        stale = plan_stale(root, [], moving=[str(root / "a" / "d1.adf")])
        self.assertEqual([o.path.name for o in stale], ["Game (1990)(P).m3u"])
        self.assertEqual(plan_stale(root, []), [])
        (root / "a" / "d2.adf").unlink()
        self.assertEqual([o.path.name for o in plan_stale(root, [])], ["Game (1990)(P).m3u"])

    def test_plan_playlists_archive_member_and_bad_names(self) -> None:
        root = self.root
        (root / "z.zip").write_bytes(b"x")
        (root / "p|1.adf").write_bytes(b"x")
        ops = plan_playlists([PlaylistSpec("Z", "Games", 2, [
            PlaylistDisk(1, root / "z.zip", "a.adf", ""), PlaylistDisk(2, root / "z.zip", "b.adf", "")])],
            root, member_counts={str(root / "z.zip"): 2})
        self.assertEqual(ops[0].lines[1:], ["z.zip#a.adf|Disk 1", "z.zip#b.adf|Disk 2"])
        ops = plan_playlists([PlaylistSpec("B", "Games", 1, [PlaylistDisk(1, root / "p|1.adf", None, "")])], root)
        self.assertEqual(ops[0].status, "conflict")
        ops = plan_playlists([PlaylistSpec("R", "Games", 1, [PlaylistDisk(1, root / "r.rar", "x.adf", "")])], root)
        self.assertEqual(ops[0].status, "conflict")


class WritePlaylistNoHardLinkTests(unittest.TestCase):
    def test_fat_like_fs_without_hard_links(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "a.m3u"
            with mock.patch("os.link", side_effect=PermissionError(1, "no hard links")):
                sha, size = write_playlist(target, "x\n")
                self.assertEqual((target.read_text(), size), ("x\n", 2))
                with self.assertRaises(FileExistsError):
                    write_playlist(target, "y\n")
            self.assertEqual(target.read_text(), "x\n")
            self.assertEqual([p.name for p in Path(d).iterdir()], ["a.m3u"])


class PerDatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _m(self, folder: str, name: str, dat: str, extra: list[Rom] | None = None) -> Match:
        p = self.root / folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        r = Rom(name, 901120, "00000000", "", "", name[:-4], dat)
        return Match(Entry(p, None, r.size, r.crc, None), [r] + (extra or []))

    def test_sets_never_span_dats_and_filter(self) -> None:
        g, w = "Games", "Workbench"
        ms = [
            self._m(g, "X (1990)(C)(Disk 1 of 2).adf", g),
            self._m(w, "X (1990)(C)(Disk 2 of 2).adf", w),   # same title, other DAT
            self._m(w, "WB (1990)(C)(Disk 1 of 2).adf", w),
            self._m(w, "WB (1990)(C)(Disk 2 of 2).adf", w),
            self._m("Firmware", "F (1990)(C)(Disk 1 of 2).adf", "Firmware"),
            self._m("Firmware", "F (1990)(C)(Disk 2 of 2).adf", "Firmware"),
        ]
        res = ScanResult(self.root, [g, w, "Firmware"], ms, [], [], [], [])
        sets = group_disk_sets(res, dats=[g, w])
        got = sorted((s.dat, s.key, s.complete) for s in sets)
        self.assertEqual(got, [("Games", "X (1990)(C)", False), ("Workbench", "WB (1990)(C)", True),
                               ("Workbench", "X (1990)(C)", False)])
        ops = plan_m3us(res, dats=[g, w])
        wb = next(o for o in ops if o.path.name == "WB (1990)(C).m3u")
        self.assertEqual((wb.status, wb.dat, wb.path.parent), ("write", w, self.root / w))
        self.assertEqual(wb.lines[1], "WB (1990)(C)(Disk 1 of 2).adf|Disk 1")
        self.assertFalse(any(o.dat == "Firmware" for o in ops))
        self.assertEqual(len(plan_m3us(res)), 4)  # no filter -> all DATs

    def test_only_primary_roms_group(self) -> None:
        # A file whose primary DAT is Games but that also matches a Workbench rom
        # only takes part in Games sets.
        wb_alias = Rom("Other (1990)(C)(Disk 1 of 2).adf", 901120, "00000000", "", "", "Other", "Workbench")
        ms = [self._m("Games", "Y (1990)(Disk 1 of 2).adf", "Games", [wb_alias]),
              self._m("Games", "Y (1990)(Disk 2 of 2).adf", "Games")]
        sets = group_disk_sets(ScanResult(self.root, ["Games", "Workbench"], ms, [], [], [], []))
        self.assertEqual([(s.dat, s.key, s.complete) for s in sets], [("Games", "Y (1990)", True)])


@unittest.skipUnless(REAL_DAT.exists(), "real TOSEC DAT not available")
class RealDatCoverageTest(unittest.TestCase):
    """Treat every rom of the real DAT as present and measure grouping coverage."""

    def test_real_dat(self) -> None:
        from romorg.datfile import parse_dat

        dat = parse_dat(REAL_DAT)
        root = Path("/nonexistent-root")
        matches = [Match(Entry(root / r.name, None, r.size, r.crc, r.sha1), [r]) for r in dat.roms]
        sets = group_disk_sets(ScanResult(root, dat.name, matches, [], [], [], []))
        families: dict[tuple[str, int], list[bool]] = {}
        for s in sets:
            d = parse_disk_name(next(iter(s.roms.values())).name)
            assert d is not None
            families.setdefault((d.title, d.total), []).append(s.complete)
        fam_ok = sum(any(v) for v in families.values())
        complete = sum(s.complete for s in sets)
        print(f"\n[real DAT] families={len(families)} with complete set={fam_ok} "
              f"({fam_ok / len(families):.1%}); variants={len(sets)} complete={complete} "
              f"({complete / len(sets):.1%})")
        self.assertGreater(fam_ok / len(families), 0.9)
        self.assertGreater(complete / len(sets), 0.8)


if __name__ == "__main__":
    unittest.main()
