"""Sega Dreamcast: scan (CHD identified / verified, raw sets, cache), tidy, Build library, playlists, Convert,
Verify fully, undo - on synthetic discs (see chdtestlib)."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
from romorg import chd as chdlib  # noqa: E402
from romorg import chdtool, datfile, dreamcast, library, organiser, platforms  # noqa: E402

DAT = "Sega - Dreamcast"


def tree(root: Path) -> dict[str, str]:
    """{relative path: sha1 or 'DIR'} of everything below root (hidden undo logs excluded)."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[(Path(dirpath) / d).relative_to(root).as_posix()] = "DIR"
        for f in filenames:
            if f.startswith(".romorg-undo-"):
                continue
            p = Path(dirpath) / f
            out[p.relative_to(root).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


class World:
    """A temp ROM folder, a synthetic Redump DAT and the discs it describes."""

    def __init__(self, testcase: unittest.TestCase) -> None:
        T.disable_native_flac(testcase)
        self.tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "roms"
        self.root.mkdir()
        self.cache = self.base / "hashes.sqlite"
        self.discs = {
            "Alpha (USA) (En,Fr)": T.Disc("a", 1),
            "Alpha (Europe) (En,Fr)": T.Disc("a2", 4),
            "Beta (Europe)": T.Disc("b", 2),
            "Gamma (Japan)": T.Disc("g", 3),
            "Delta (USA) (Demo)": T.Disc("d", 5),
            "Big Game (USA) (Disc 1)": T.Disc("b1", 6),
            "Big Game (USA) (Disc 2)": T.Disc("b2", 7),
            "Epsilon (USA)": T.Disc("e", 8),
            "Zeta (USA)": T.Disc("z", 9),
        }
        cats = {"Delta (USA) (Demo)": "Demos"}
        self.dat_path = T.write_dat(self.base / "dc.dat", [(n, cats.get(n, "Games"), d.bins)
                                                           for n, d in self.discs.items()])
        self.dat = datfile.parse_redump(self.dat_path)

    def chd(self, game: str, rel: str, sidecars=(), **kw) -> Path:
        """Write the CHD of ``game`` at ``root/rel`` plus sidecar files (names relative to its folder)."""
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        self.discs[game].write_chd(p, **kw)
        for s in sidecars:
            (p.parent / s).write_bytes(b"sidecar " + s.encode())
        return p

    def scan(self, **kw) -> dreamcast.DcScanResult:
        kw.setdefault("cache_path", self.cache)
        return dreamcast.scan(self.root, self.dat, **kw)

    def platform(self):
        return platforms.get_platform("Sega Dreamcast")


class ScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)

    def test_identified_chd_with_sidecars_and_summary(self) -> None:
        self.w.chd("Beta (Europe)", "Beta/Beta (Europe).chd", ["Beta (Europe).zip", "Beta (Europe).state"])
        self.w.chd("Gamma (Japan)", "weird name/x.chd")
        r = self.w.scan()
        by = {m.unit.game.name: m for m in r.matched}
        self.assertEqual(set(by), {"Beta (Europe)", "Gamma (Japan)"})
        self.assertEqual({m.level for m in r.matched}, {"identified"})
        u = by["Beta (Europe)"].unit
        self.assertEqual(u.folder, self.w.root / "Beta")
        self.assertEqual({f.name for f in u.files}, {"Beta (Europe).chd", "Beta (Europe).zip", "Beta (Europe).state"})
        self.assertEqual(r.unmatched, [])                      # sidecars are never "unmatched junk"
        s = r.summary()
        self.assertEqual((s["have"], s["missing"], s["dat_total"], s["identified"], s["verified"]),
                         (2, len(self.w.discs) - 2, len(self.w.discs), 2, 0))
        self.assertEqual(s["correctly_placed"], 0)
        self.assertEqual(s["to_rename"], 2)
        self.assertEqual(r.layout, "game_folder")
        # audio is compared by length only: only the data tracks were hashed
        trk = u.tracks
        self.assertEqual([bool(t["sha1"]) for t in trk], [True, False, True])

    def test_cache_means_no_second_decode(self) -> None:
        self.w.chd("Beta (Europe)", "Beta/Beta (Europe).chd")
        self.w.scan()
        with mock.patch.object(dreamcast.chdlib, "hash_track", side_effect=AssertionError("decoded again")):
            r = self.w.scan()
        self.assertEqual(r.matched[0].level, "identified")
        # the cache key includes size + mtime: touching the file decodes again
        p = self.w.root / "Beta/Beta (Europe).chd"
        os.utime(p, (1, 1))
        with mock.patch.object(dreamcast.chdlib, "hash_track", side_effect=AssertionError("decoded again")):
            with self.assertRaises(AssertionError):
                self.w.scan()

    def test_unmatched_reasons(self) -> None:
        d = T.Disc("other", 99, frames=(9, 5, 10))
        (self.w.root / "Unknown").mkdir()
        d.write_chd(self.w.root / "Unknown" / "Unknown.chd")
        bad = self.w.root / "Broken" / "Broken.chd"
        bad.parent.mkdir()
        bad.write_bytes(b"nonsense" * 40)
        # same track sizes as a DAT game but different content
        liar = T.Disc("liar", 123)
        (self.w.root / "Liar").mkdir()
        liar.write_chd(self.w.root / "Liar" / "Liar.chd")
        r = self.w.scan()
        reasons = {Path(e.rel).name: e.reason for e in r.unmatched}
        self.assertIn("track sizes", reasons["Unknown.chd"])
        self.assertEqual(len(r.unmatched), 3)
        self.assertTrue(all(e.reason for e in r.unmatched))
        self.assertIn("not a readable CHD", reasons["Broken.chd"])
        self.assertIn("do not match", reasons["Liar.chd"])

    def test_loose_chd_in_root_with_stem_sidecars_only(self) -> None:
        self.w.chd("Beta (Europe)", "loose.chd", ["loose.zip", "loose.state", "unrelated.txt"])
        r = self.w.scan()
        u = r.matched[0].unit
        self.assertIsNone(u.folder)
        self.assertEqual({f.name for f in u.files}, {"loose.chd", "loose.zip", "loose.state"})
        self.assertEqual([Path(e.rel).name for e in r.unmatched], ["unrelated.txt"])

    def test_two_chds_in_one_folder_are_separate_units(self) -> None:
        self.w.chd("Big Game (USA) (Disc 1)", "Big/disc1.chd", ["disc1.state"])
        self.w.chd("Big Game (USA) (Disc 2)", "Big/disc2.chd", ["disc2.state"])
        r = self.w.scan()
        self.assertEqual(len(r.matched), 2)
        self.assertTrue(all(m.unit.folder is None for m in r.matched))
        files = {m.unit.game.name: {f.name for f in m.unit.files} for m in r.matched}
        self.assertEqual(files["Big Game (USA) (Disc 1)"], {"disc1.chd", "disc1.state"})

    def test_raw_set_is_matched_per_track(self) -> None:
        self.w.discs["Epsilon (USA)"].write_raw(self.w.root / "raw1", "any name")
        sheet = self.w.discs["Beta (Europe)"].write_raw(self.w.root / "raw2", "b", gdi_style=False)   # .cue
        r = self.w.scan()
        by = {m.unit.game.name: m for m in r.matched}
        self.assertEqual(by["Epsilon (USA)"].level, "raw")
        self.assertEqual(by["Beta (Europe)"].kind, "raw")
        self.assertEqual(r.summary()["raw"], 2)
        self.assertEqual(r.summary()["convertible"], 2)
        self.assertEqual(r.summary()["verified"] + r.summary()["identified"], 0)
        self.assertTrue(sheet.is_file())

    def test_raw_set_with_a_modified_track_is_unmatched(self) -> None:
        sheet = self.w.discs["Epsilon (USA)"].write_raw(self.w.root / "raw1", "e")
        t3 = sheet.parent / "track03.bin"
        data = bytearray(t3.read_bytes())
        data[100] ^= 1
        t3.write_bytes(bytes(data))
        r = self.w.scan()
        self.assertEqual(r.matched, [])
        self.assertIn("do not match", r.unmatched[0].reason)

    def test_audio_tracks_do_not_count_until_verified(self) -> None:
        """A CHD whose AUDIO differs (same length) is 'identified'; Verify fully rejects it."""
        d = self.w.discs["Beta (Europe)"]
        liar = T.Disc("b", 2)
        liar.t2 = T.make_audio_track(6, 777)             # different audio, same length
        liar.tracks[1]["data"] = liar.t2
        p = self.w.root / "Beta" / "Beta (Europe).chd"
        p.parent.mkdir()
        liar.write_chd(p)
        r = self.w.scan()
        self.assertEqual(r.matched[0].level, "identified")
        res = dreamcast.verify_units(r, cache_path=self.w.cache)
        self.assertEqual((res["verified"], len(res["failed"])), (0, 1))
        self.assertIn("track 2", res["failed"][0]["error"])
        r2 = self.w.scan()                                # the cache now holds every hash: not a match any more
        self.assertEqual(r2.matched, [])
        self.assertIn("track 2", r2.unmatched[0].reason)
        self.assertIsNotNone(d)

    def test_verify_fully_upgrades_identified_to_verified(self) -> None:
        self.w.chd("Beta (Europe)", "Beta/Beta (Europe).chd")
        r = self.w.scan()
        self.assertEqual(r.summary()["identified"], 1)
        ticks = []
        res = dreamcast.verify_units(r, progress=lambda d, t, m: ticks.append(m), cache_path=self.w.cache)
        self.assertEqual((res["verified"], res["failed"], res["checked"]), (1, [], 1))
        self.assertTrue(any("Verifying" in m for m in ticks))
        with mock.patch.object(dreamcast.chdlib, "hash_track", side_effect=AssertionError("decoded again")):
            r2 = self.w.scan()
        self.assertEqual((r2.summary()["verified"], r2.summary()["identified"]), (1, 0))
        res2 = dreamcast.verify_units(r2, cache_path=self.w.cache)
        self.assertEqual((res2["checked"], res2["already"]), (0, 1))

    def test_cancel_during_scan(self) -> None:
        self.w.chd("Beta (Europe)", "Beta/Beta (Europe).chd")
        with self.assertRaises(dreamcast.scanner.ScanCancelled):
            self.w.scan(cancel=lambda: True)

    def test_scan_never_writes_into_the_folder(self) -> None:
        self.w.chd("Beta (Europe)", "Beta/Beta (Europe).chd", ["Beta (Europe).state"])
        before = tree(self.w.root)
        stat_before = {p: (self.w.root / p).stat().st_mtime_ns for p in before}
        self.w.scan()
        self.assertEqual(tree(self.w.root), before)
        self.assertEqual({p: (self.w.root / p).stat().st_mtime_ns for p in before}, stat_before)


class TidyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)

    def apply(self, ops, **kw):
        return dreamcast.apply_plan(ops, self.w.root, **kw)

    def test_folder_chd_and_sidecars_are_renamed_together(self) -> None:
        self.w.chd("Beta (Europe)", "old folder/old name.chd",
                   ["old name.zip", "old name.state", "old name.srm", "old name.state.auto", "notes.txt"])
        (self.w.root / "old folder" / "media").mkdir()
        (self.w.root / "old folder" / "media" / "box.png").write_bytes(b"png")
        r = self.w.scan()
        ops = dreamcast.plan_tidy(r)
        gop = [o for o in ops if o.game == "Beta (Europe)"][0]
        self.assertEqual((gop.status, gop.kind), ("move", "rename"))     # same parent: a folder rename
        self.assertEqual(gop.dst, self.w.root / "Beta (Europe)")
        res = self.apply(ops)
        self.assertEqual(res["failed"], [])
        got = tree(self.w.root)
        self.assertEqual(sorted(p for p in got if "/" in p and not got[p] == "DIR"), sorted([
            "Beta (Europe)/Beta (Europe).chd", "Beta (Europe)/Beta (Europe).zip",
            "Beta (Europe)/Beta (Europe).state", "Beta (Europe)/Beta (Europe).srm",
            "Beta (Europe)/Beta (Europe).state.auto", "Beta (Europe)/notes.txt",
            "Beta (Europe)/media/box.png"]))     # unrelated files travel with the folder, names untouched
        self.assertNotIn("old folder", got)       # the emptied old folder is removed
        # the saves keep their content (never deleted / rewritten)
        self.assertEqual((self.w.root / "Beta (Europe)" / "Beta (Europe).state").read_bytes(), b"sidecar old name.state")

    def test_rescan_after_apply_is_canonical_and_plan_is_empty(self) -> None:
        self.w.chd("Beta (Europe)", "x/y.chd", ["y.state"])
        self.apply(dreamcast.plan_tidy(self.w.scan()))
        r = self.w.scan()
        self.assertEqual(r.summary()["correctly_placed"], 1)
        ops = dreamcast.plan_tidy(r)
        self.assertEqual({o.status for o in ops}, {"ok"})
        self.assertEqual(dreamcast.file_ops(ops), [])

    def test_undo_restores_the_exact_tree(self) -> None:
        self.w.chd("Beta (Europe)", "old folder/old name.chd", ["old name.zip", "old name.state", "n.txt"])
        self.w.chd("Gamma (Japan)", "loose.chd", ["loose.state"])
        (self.w.root / "junk.bin").write_bytes(b"zz")
        (self.w.root / "gamelist.xml").write_text("<gameList/>")
        before = tree(self.w.root)
        res = self.apply(dreamcast.plan_tidy(self.w.scan()))
        self.assertTrue(res["undo_log"])
        self.assertNotEqual(tree(self.w.root), before)
        out = organiser.undo(Path(res["undo_log"]), root=self.w.root)
        self.assertEqual(out.get("remaining", 0), 0)
        self.assertEqual(tree(self.w.root), before)

    def test_loose_chd_gets_its_own_folder(self) -> None:
        self.w.chd("Gamma (Japan)", "loose.chd", ["loose.state", "loose.zip", "other.txt"])
        self.apply(dreamcast.plan_tidy(self.w.scan()))
        got = tree(self.w.root)
        self.assertIn("Gamma (Japan)/Gamma (Japan).chd", got)
        self.assertIn("Gamma (Japan)/Gamma (Japan).state", got)
        self.assertIn("Gamma (Japan)/Gamma (Japan).zip", got)
        self.assertNotIn("Gamma (Japan)/other.txt", got)   # not a sidecar of the CHD: does not travel with it
        self.assertIn("_unmatched/other.txt", got)          # plain junk goes to _unmatched/ in the same run

    def test_unmatched_chd_folder_moves_whole_to_unmatched(self) -> None:
        other = T.Disc("other", 99, frames=(9, 5, 10))
        (self.w.root / "Mystery").mkdir()
        other.write_chd(self.w.root / "Mystery" / "m.chd")
        (self.w.root / "Mystery" / "m.state").write_bytes(b"s")
        self.apply(dreamcast.plan_tidy(self.w.scan()))
        got = tree(self.w.root)
        self.assertIn("_unmatched/Mystery/m.chd", got)
        self.assertIn("_unmatched/Mystery/m.state", got)
        self.assertNotIn("Mystery", got)
        # idempotent: a rescan leaves it where it is
        ops = dreamcast.plan_tidy(self.w.scan())
        self.assertEqual({o.status for o in ops}, {"ok"})

    def test_move_unmatched_off_leaves_things(self) -> None:
        d = T.Disc("other", 99, frames=(9, 5, 10))
        (self.w.root / "Mystery").mkdir()
        d.write_chd(self.w.root / "Mystery" / "m.chd")
        (self.w.root / "readme.txt").write_text("hi")
        ops = dreamcast.plan_tidy(self.w.scan(), move_unmatched=False)
        self.assertEqual({o.status for o in ops}, {"skip"})
        self.assertEqual(dreamcast.file_ops(ops), [])

    def test_junk_rules_keep_files_and_frontend_dirs(self) -> None:
        for name in ("gamelist.xml", "save.srm", "random.dat"):
            (self.w.root / name).write_bytes(b"x")
        (self.w.root / "media").mkdir()
        (self.w.root / "media" / "a.png").write_bytes(b"x")
        ops = dreamcast.plan_tidy(self.w.scan())
        st = {Path(o.src).name: o.status for o in ops}
        self.assertEqual(st["gamelist.xml"], "skip")
        self.assertEqual(st["save.srm"], "skip")
        self.assertEqual(st["a.png"], "skip")
        self.assertEqual(st["random.dat"], "move")

    def test_never_overwrites_an_existing_target(self) -> None:
        self.w.chd("Beta (Europe)", "old/x.chd")
        taken = self.w.root / "Beta (Europe)"
        taken.mkdir()
        (taken / "Beta (Europe).chd").write_bytes(b"someone else's file")      # not a CHD of the game
        before = tree(self.w.root)
        ops = dreamcast.plan_tidy(self.w.scan(), move_unmatched=False)          # it stays where it is
        gop = [o for o in ops if o.game == "Beta (Europe)"][0]
        self.assertEqual(gop.status, "conflict")
        self.assertIn("exists", gop.reason)
        self.apply(ops)
        self.assertEqual(tree(self.w.root), before)

    def test_duplicates_keep_the_canonical_copy(self) -> None:
        self.w.chd("Beta (Europe)", "Beta (Europe)/Beta (Europe).chd", ["Beta (Europe).state"])
        self.w.chd("Beta (Europe)", "copy of beta/b.chd", ["b.state"])
        r = self.w.scan()
        self.assertEqual(r.summary()["duplicates"], 1)
        ops = dreamcast.plan_tidy(r)
        dup = [o for o in ops if o.code == "duplicate"][0]
        self.assertEqual(dup.folder, "_duplicates")
        self.assertEqual(dup.dst, self.w.root / "_duplicates" / "copy of beta")
        self.apply(ops)
        got = tree(self.w.root)
        self.assertIn("Beta (Europe)/Beta (Europe).chd", got)
        self.assertIn("_duplicates/copy of beta/b.chd", got)
        self.assertIn("_duplicates/copy of beta/b.state", got)        # its save travels with it
        self.assertEqual(self.w.scan().summary()["duplicates"], 0)    # set aside = not a pending duplicate

    def test_the_copy_with_the_saves_is_the_one_that_stays(self) -> None:
        self.w.chd("Beta (Europe)", "aaa first/x.chd")                                      # no saves
        self.w.chd("Beta (Europe)", "zzz second/y.chd", ["y.state", "y.zip"])               # has its saves
        ops = dreamcast.plan_tidy(self.w.scan())
        keep = [o for o in ops if o.game == "Beta (Europe)" and not o.code][0]
        dup = [o for o in ops if o.code == "duplicate"][0]
        self.assertEqual(keep.src, self.w.root / "zzz second")
        self.assertEqual(dup.src, self.w.root / "aaa first")

    def test_matched_game_inside_a_reserved_folder_moves_back(self) -> None:
        self.w.chd("Beta (Europe)", "_excluded/Beta (Europe)/Beta (Europe).chd")
        ops = dreamcast.plan_tidy(self.w.scan())
        gop = [o for o in ops if o.game == "Beta (Europe)"][0]
        self.assertEqual((gop.status, gop.dst), ("move", self.w.root / "Beta (Europe)"))

    def test_illegal_characters_in_names_are_made_safe(self) -> None:
        w = self.w
        d = T.Disc("q", 31, frames=(9, 5, 11))
        name = 'What? (USA): Part "2"'
        w.discs[name] = d
        w.dat_path = T.write_dat(w.base / "dc2.dat", [(n, "Games", dd.bins) for n, dd in w.discs.items()])
        w.dat = datfile.parse_redump(w.dat_path)
        (w.root / "q").mkdir()
        d.write_chd(w.root / "q" / "q.chd")
        self.apply(dreamcast.plan_tidy(w.scan()))
        self.assertTrue((w.root / "What_ (USA)_ Part _2_" / "What_ (USA)_ Part _2_.chd").is_file())

    def test_rows_for_the_server(self) -> None:
        self.w.chd("Beta (Europe)", "x/y.chd", ["y.zip"])
        r = self.w.scan()
        item = r.matched[0].item(type("S", (), {"root": self.w.root})())
        self.assertEqual((item["level"], item["game"], item["folder"]), ("identified", "Beta (Europe)", True))
        self.assertEqual(r.game_levels(), {"Beta (Europe)": ("identified", "chd")})


class LibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)
        w = self.w
        w.chd("Alpha (USA) (En,Fr)", "Alpha (USA) (En,Fr)/Alpha (USA) (En,Fr).chd", ["Alpha (USA) (En,Fr).state"])
        w.chd("Alpha (Europe) (En,Fr)", "alpha eu/a.chd", ["a.state"])
        w.chd("Beta (Europe)", "Beta (Europe)/Beta (Europe).chd")
        w.chd("Gamma (Japan)", "gamma/g.chd")
        w.chd("Delta (USA) (Demo)", "delta/d.chd")
        w.chd("Big Game (USA) (Disc 1)", "big1/b1.chd", ["b1.state"])
        w.chd("Big Game (USA) (Disc 2)", "big2/b2.chd", ["b2.state"])
        self.profile = library.default_profile(w.platform())

    def plan(self, profile=None, **kw):
        return dreamcast.plan_library(self.w.scan(), profile or self.profile, **kw)

    def codes(self, plan) -> dict[str, str]:
        return {o.game: (o.code or "kept") for o in plan.ops if isinstance(o, dreamcast.DcOp) and o.game}

    def test_default_rules(self) -> None:
        plan = self.plan()
        c = self.codes(plan)
        self.assertEqual(c["Alpha (Europe) (En,Fr)"], "kept")             # Europe before USA
        self.assertEqual(c["Alpha (USA) (En,Fr)"], "superseded")
        self.assertEqual(c["Gamma (Japan)"], "excluded")                   # no English
        self.assertEqual(c["Delta (USA) (Demo)"], "excluded")              # demo (also category Demos)
        self.assertEqual((c["Big Game (USA) (Disc 1)"], c["Big Game (USA) (Disc 2)"]), ("kept", "kept"))
        gam = [o for o in plan.ops if o.game == "Gamma (Japan)"][0]
        self.assertIn("language", gam.reasons)
        dem = [o for o in plan.ops if o.game == "Delta (USA) (Demo)"][0]
        self.assertIn("demo", dem.reasons)
        counts = organiser.reason_counts(plan)
        self.assertEqual((counts["excluded"], counts["superseded"], counts["kept"]), (2, 1, 4))
        self.assertEqual(counts["playlists_write"], 1)

    def test_discs_of_a_game_stay_together_and_get_one_playlist(self) -> None:
        plan = self.plan()
        pl = plan.playlists
        self.assertEqual(len(pl), 1)
        self.assertEqual(pl[0].path, self.w.root / "Big Game (USA) (Disc 1)" / "Big Game (USA).m3u")
        lines = [ln for ln in pl[0].lines if not ln.startswith("#")]
        self.assertEqual(lines, ["Big Game (USA) (Disc 1).chd", "../Big Game (USA) (Disc 2)/Big Game (USA) (Disc 2).chd"])
        res = dreamcast.apply_plan(plan.ops, self.w.root, playlists=plan.playlists)
        self.assertEqual(res["playlists_written"], 1)
        m3u = self.w.root / "Big Game (USA) (Disc 1)" / "Big Game (USA).m3u"
        self.assertTrue(m3u.is_file())
        for ln in m3u.read_text().splitlines():
            if ln and not ln.startswith("#"):
                self.assertTrue((m3u.parent / ln).is_file(), ln)

    def test_apply_idempotent_and_one_undo(self) -> None:
        before = tree(self.w.root)
        plan = self.plan()
        res = dreamcast.apply_plan(plan.ops, self.w.root, playlists=plan.playlists)
        self.assertEqual(res["failed"], [])
        got = tree(self.w.root)
        self.assertIn("Alpha (Europe) (En,Fr)/Alpha (Europe) (En,Fr).chd", got)
        self.assertIn("_superseded/Alpha (USA) (En,Fr)/Alpha (USA) (En,Fr).state", got)   # saves travel with the CHD
        self.assertIn("_excluded/gamma/g.chd", got)
        self.assertIn("_excluded/delta/d.chd", got)
        # second run: nothing left to do, no playlist write / stale
        plan2 = self.plan()
        self.assertEqual(dreamcast.file_ops([o for o in plan2.ops if o.status != "ok"]), [])
        self.assertEqual([p.status for p in plan2.playlists], ["ok"])
        self.assertEqual(organiser.reason_counts(plan2)["playlists_remove"], 0)
        # one undo restores everything (including removing the created playlist)
        out = organiser.undo(Path(res["undo_log"]), root=self.w.root)
        self.assertEqual(out.get("remaining", 0), 0)
        self.assertEqual(tree(self.w.root), before)

    def test_switching_a_rule_off_moves_games_back(self) -> None:
        plan = self.plan()
        dreamcast.apply_plan(plan.ops, self.w.root, playlists=plan.playlists)
        loose = library.LibraryProfile.from_dict({"languages": [], "exclude": []}, self.profile)
        plan2 = self.plan(loose)
        c = self.codes(plan2)
        self.assertEqual(c["Gamma (Japan)"], "kept")
        self.assertEqual(c["Delta (USA) (Demo)"], "kept")
        dreamcast.apply_plan(plan2.ops, self.w.root, playlists=plan2.playlists)
        got = tree(self.w.root)
        self.assertIn("Gamma (Japan)/Gamma (Japan).chd", got)
        self.assertNotIn("_excluded/gamma/g.chd", got)

    def test_one_per_game_off_keeps_every_region(self) -> None:
        prof = library.LibraryProfile.from_dict({"one_per_game": False}, self.profile)
        c = self.codes(self.plan(prof))
        self.assertEqual((c["Alpha (Europe) (En,Fr)"], c["Alpha (USA) (En,Fr)"]), ("kept", "kept"))

    def test_incomplete_multidisc_is_kept_without_playlist(self) -> None:
        for p in list(self.w.root.glob("big2/*")):
            p.unlink()
        (self.w.root / "big2").rmdir()
        plan = self.plan()
        c = self.codes(plan)
        self.assertEqual(c["Big Game (USA) (Disc 1)"], "kept")
        self.assertEqual(plan.playlists, [])
        self.assertEqual([(i.name, i.missing) for i in plan.selection.incomplete], [("Big Game (USA)", (2,))])

    def test_old_playlist_is_removed_when_a_disc_goes_away(self) -> None:
        plan = self.plan()
        dreamcast.apply_plan(plan.ops, self.w.root, playlists=plan.playlists)
        # the user deletes Disc 2: the playlist (ours) is stale and goes
        import shutil
        shutil.rmtree(self.w.root / "Big Game (USA) (Disc 2)")
        plan2 = self.plan()
        self.assertEqual(plan2.playlists, [])
        self.assertEqual([o.status for o in plan2.ops if o.kind == "m3u"], ["delete"])
        res = dreamcast.apply_plan(plan2.ops, self.w.root, playlists=plan2.playlists)
        self.assertEqual(res["deleted"], 1)
        self.assertFalse(list(self.w.root.rglob("*.m3u")))

    def test_user_playlist_is_never_touched(self) -> None:
        mine = self.w.root / "big1" / "my own.m3u"
        mine.write_text("b1.chd\n")
        plan = self.plan()
        dreamcast.apply_plan(plan.ops, self.w.root, playlists=plan.playlists)
        self.assertEqual((self.w.root / "Big Game (USA) (Disc 1)" / "my own.m3u").read_text(), "b1.chd\n")


class ConvertTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)
        w = self.w
        self.bin = w.base / "bin"
        self.bin.mkdir()
        self.fake = T.install_fake_chdman(self.bin)
        self.scratch = T.isolate_temp(self, w.base)
        self.disc = w.discs["Epsilon (USA)"]
        self.fixture = w.base / "fixture.chd"
        self.disc.write_chd(self.fixture)
        raw = T.prepare_fake_raw(w.base / "fakeraw", self.disc)
        env = {"FAKE_CHD": str(self.fixture), "FAKE_RAW": str(raw), "FAKE_LOG": str(w.base / "log.txt")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.chdman = chdtool.Chdman([str(self.fake)], "configured", str(self.fake))
        self.sheet = self.disc.write_raw(w.root / "raw dump", "Epsilon")
        (w.root / "raw dump" / "Epsilon.md5").write_text("sums")

    def plan(self, found=True):
        r = self.w.scan()
        return r, dreamcast.plan_convert(r, found)

    def run_convert(self, **kw):
        r, ops = self.plan()
        return dreamcast.apply_conversions(ops, self.w.root, self.chdman, r.index, **kw)

    def test_plan_shows_the_target_and_the_originals(self) -> None:
        r, ops = self.plan()
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertEqual((op.status, op.rom_name), ("convert", "Epsilon (USA)"))
        self.assertEqual(op.dst, self.w.root / "Epsilon (USA)" / "Epsilon (USA).chd")
        self.assertEqual(op.original_dst, self.w.root / "_converted_originals" / "raw dump")

    def test_convert_verifies_places_and_keeps_originals(self) -> None:
        before = tree(self.w.root)
        res = self.run_convert()
        self.assertEqual((res["converted"], res["failed"], res["error"]), (1, [], None))
        got = tree(self.w.root)
        self.assertEqual(got["Epsilon (USA)/Epsilon (USA).chd"], hashlib.sha1(self.fixture.read_bytes()).hexdigest())
        for name in ("Epsilon.gdi", "Epsilon.md5", "track01.bin", "track02.raw", "track03.bin"):
            self.assertIn(f"_converted_originals/raw dump/{name}", got)
        self.assertNotIn("raw dump", got)
        self.assertFalse([p for p in os.listdir(self.w.root) if p.startswith(".romorg-chd-")])   # temp is gone
        # the new CHD is a verified match on the next scan; the raw set is not an unmatched file
        r = self.w.scan()
        self.assertEqual([m.unit.game.name for m in r.matched], ["Epsilon (USA)"])
        self.assertEqual(r.matched[0].kind, "chd")
        self.assertEqual([e.rel for e in r.unmatched if not e.rel.startswith("_converted_originals")], [])
        # undo removes the new CHD and brings the raw files back
        out = organiser.undo(Path(res["undo_log"]), root=self.w.root)
        self.assertEqual(out.get("remaining", 0), 0)
        self.assertEqual(tree(self.w.root), before)

    def test_chdman_failure_leaves_nothing_behind(self) -> None:
        before = tree(self.w.root)
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "createcd"}):
            res = self.run_convert()
        self.assertEqual(res["converted"], 0)
        self.assertIn("simulated createcd failure", res["failed"][0]["error"])
        self.assertEqual(tree(self.w.root), before)
        self.assertFalse([p for p in os.listdir(self.w.root) if p.startswith(".romorg-chd-")])
        self.assertIsNone(res["undo_log"])

    def test_a_wrong_result_is_rejected_by_the_redump_check(self) -> None:
        bad = self.w.base / "bad.chd"
        T.Disc("e", 8, frames=(8, 6, 12)).write_chd(bad)         # same layout...
        liar = T.Disc("e", 8)
        liar.t3 = T.make_data_track(12, 4242, 4000)               # ...but other data
        liar.tracks[2]["data"] = liar.t3
        liar.write_chd(bad)
        before = tree(self.w.root)
        raw = T.prepare_fake_raw(self.w.base / "fakeraw2", liar)
        with mock.patch.dict(os.environ, {"FAKE_CHD": str(bad), "FAKE_RAW": str(raw)}):
            res = self.run_convert()
        self.assertEqual(res["converted"], 0)
        self.assertIn("does not match Redump", res["failed"][0]["error"])
        self.assertEqual(tree(self.w.root), before)

    def test_cancel_leaves_the_raw_set_alone(self) -> None:
        import threading
        ev = threading.Event()
        threading.Timer(0.5, ev.set).start()
        before = tree(self.w.root)
        with mock.patch.dict(os.environ, {"FAKE_SLOW": "1"}):
            res = self.run_convert(cancel=ev)
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["converted"], 0)
        self.assertEqual(tree(self.w.root), before)
        self.assertFalse([p for p in os.listdir(self.w.root) if p.startswith(".romorg-chd-")])

    def test_without_chdman_nothing_is_offered(self) -> None:
        r, ops = self.plan(found=False)
        self.assertEqual([o.status for o in ops], ["skip"])
        self.assertIn("chdman not found", ops[0].reason)
        res = dreamcast.apply_conversions(ops, self.w.root, self.chdman, r.index)
        self.assertEqual(res["converted"], 0)

    def test_not_enough_space_is_a_failure_not_a_partial_result(self) -> None:
        before = tree(self.w.root)
        with mock.patch("romorg.chdtool.shutil.disk_usage", return_value=mock.Mock(free=1000)):
            res = self.run_convert()
        self.assertEqual(res["converted"], 0)
        self.assertIn("not enough free space", res["failed"][0]["error"])
        self.assertEqual(tree(self.w.root), before)

    def test_existing_chd_of_the_game_skips_the_raw_set(self) -> None:
        self.disc.write_chd(self.w.root / "have" / "x.chd") if (self.w.root / "have").mkdir() is None else None
        r, ops = self.plan()
        self.assertEqual(ops[0].status, "skip")
        self.assertIn("already exists", ops[0].reason)

    def test_conflicts_are_not_overwritten(self) -> None:
        target = self.w.root / "Epsilon (USA)"
        target.mkdir()
        (target / "Epsilon (USA).chd").write_bytes(b"not mine")
        r, ops = self.plan()
        self.assertEqual(ops[0].status, "conflict")
        before = tree(self.w.root)
        dreamcast.apply_conversions(ops, self.w.root, self.chdman, r.index)
        self.assertEqual(tree(self.w.root), before)

    def test_cue_set_converts_too(self) -> None:
        import shutil
        shutil.rmtree(self.w.root / "raw dump")
        self.disc.write_raw(self.w.root / "cue dump", "e", gdi_style=False)
        res = self.run_convert()
        self.assertEqual(res["converted"], 1)
        self.assertTrue((self.w.root / "Epsilon (USA)" / "Epsilon (USA).chd").is_file())


class ChdmanEngineTest(unittest.TestCase):
    """With chdman the scan extracts + hashes everything: level ``verified`` straight away."""

    def setUp(self) -> None:
        self.w = World(self)
        self.bin = self.w.base / "bin"
        self.bin.mkdir()
        self.fake = T.install_fake_chdman(self.bin)
        self.scratch = T.isolate_temp(self, self.w.base)
        disc = self.w.discs["Beta (Europe)"]
        raw = T.prepare_fake_raw(self.w.base / "fakeraw", disc)
        env = {"FAKE_CHD": str(self.w.base / "unused.chd"), "FAKE_RAW": str(raw), "FAKE_LOG": str(self.w.base / "log.txt")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.chdman = chdtool.Chdman([str(self.fake)], "configured", str(self.fake))
        self.w.chd("Beta (Europe)", "Beta/Beta (Europe).chd")

    def test_scan_with_chdman_is_verified_and_cached(self) -> None:
        r = self.w.scan(chdman=self.chdman)
        self.assertEqual((r.summary()["verified"], r.summary()["identified"], r.engine), (1, 0, "chdman"))
        self.assertEqual(r.matched[0].unit.via, "chdman")
        self.assertTrue(all(t["sha1"] for t in r.matched[0].unit.tracks))
        self.assertFalse([p for p in os.listdir(self.w.root) if p.startswith(".romorg-chd-")])
        log = (self.w.base / "log.txt").read_text()
        self.assertIn("extractcd", log)
        (self.w.base / "log.txt").unlink()
        r2 = self.w.scan(chdman=self.chdman)             # cached: chdman is not run again
        self.assertEqual(r2.summary()["verified"], 1)
        self.assertFalse((self.w.base / "log.txt").exists())

    def test_chdman_that_writes_other_tracks_is_not_trusted(self) -> None:
        other = T.prepare_fake_raw(self.w.base / "other_raw", T.Disc("zz", 55, frames=(9, 5, 10)))
        with mock.patch.dict(os.environ, {"FAKE_RAW": str(other)}):
            r = self.w.scan(chdman=self.chdman)
        self.assertEqual((r.matched[0].level, r.matched[0].unit.via), ("identified", "python"))

    def test_python_engine_ignores_chdman(self) -> None:
        r = self.w.scan(chdman=self.chdman, engine="python")
        self.assertEqual((r.summary()["identified"], r.engine), (1, "python"))
        self.assertFalse((self.w.base / "log.txt").exists())

    def test_chdman_failure_falls_back_to_the_python_reader(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "extractcd"}):
            r = self.w.scan(chdman=self.chdman)
        self.assertEqual(r.matched[0].level, "identified")
        self.assertEqual(r.matched[0].unit.via, "python")

    def test_no_space_falls_back_to_the_python_reader(self) -> None:
        with mock.patch("romorg.tempspace.free_bytes", return_value=1000):
            r = self.w.scan(chdman=self.chdman)
        self.assertEqual(r.matched[0].level, "identified")
        self.assertEqual(r.temp["python"], 1)
        self.assertIn("not enough temporary space", r.temp["last"]["reason"])

    def test_verify_uses_chdman_when_present(self) -> None:
        r = self.w.scan(engine="python")
        res = dreamcast.verify_units(r, self.chdman, cache_path=self.w.cache)
        self.assertEqual((res["verified"], res["failed"]), (1, []))
        self.assertIn("extractcd", (self.w.base / "log.txt").read_text())
        self.assertEqual(self.w.scan(engine="python").summary()["verified"], 1)


class IndexTest(unittest.TestCase):
    def test_cue_is_ignored_and_tracks_are_ordered(self) -> None:
        w = World(self)
        idx = dreamcast.get_index(w.dat)
        g = idx.games["Beta (Europe)"]
        self.assertEqual([r.name.endswith(".bin") for r in g.tracks], [True] * 3)
        self.assertIsNotNone(g.cue)
        self.assertEqual(g.sizes, tuple(len(b) for b in w.discs["Beta (Europe)"].bins))
        tot = idx.disc_totals()
        self.assertEqual((tot["Big Game (USA) (Disc 1)"], tot["Big Game (USA) (Disc 2)"], tot["Beta (Europe)"]), (2, 2, 0))
        self.assertIs(dreamcast.get_index(w.dat), idx)


if __name__ == "__main__":
    unittest.main()
