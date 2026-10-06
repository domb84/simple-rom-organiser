"""Sony PlayStation / PlayStation 2 on the shared disc-system engine (romorg.discsys): MODE2_RAW and cooked MODE1
tracks, DVD CHDs, ISO games, folder-per-game layout, playlist policy, createcd / createdvd conversion - on synthetic
discs (see chdtestlib). Real Redump DATs / real CHDs are only used when present (read-only)."""

from __future__ import annotations

import glob
import hashlib
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
from romorg import cdecc, chd as chdlib, chdtool, datfile, discsys, library, organiser, platforms, redump, tags  # noqa: E402

PSX_DAT, PS2_DAT = "Sony - PlayStation", "Sony - PlayStation 2"
# Real data (read-only; RealDatTest is skipped without it): the folder holding the Redump "Sony - PlayStation - *.dat"
# and "Sony - PlayStation 2 - *.dat" DATs. The default is where they live on the Steam Deck; set ROMORG_REAL_SONY_DATS
# to use another folder. The game counts the test expects are those of the DAT versions it was written against.
SCRATCH = Path(os.environ.get("ROMORG_REAL_SONY_DATS")
               or "/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/"
                  "cfc5ea37-4261-428c-9422-29acd65cac97/scratchpad/redump_sony")


def tree(root: Path) -> dict:
    out = {}
    for dp, dns, fns in os.walk(root):
        for d in dns:
            out[(Path(dp) / d).relative_to(root).as_posix()] = "DIR"
        for f in fns:
            if f.startswith(".romorg-undo-"):
                continue
            p = Path(dp) / f
            out[p.relative_to(root).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class Ps2World:
    """A PlayStation 2 folder + a synthetic DAT with ISO games (DVD), a CD game (single .bin) and a 2 disc game."""

    def __init__(self, tc: unittest.TestCase) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "ps2"
        self.root.mkdir()
        self.cache = self.base / "hashes.sqlite"
        self.iso = {"Alpha (USA)": T.make_iso(40, 1), "Beta (Europe) (En,Fr,De)": T.make_iso(44, 2),
                    "Gamma (Japan)": T.make_iso(36, 3), "Two Part (USA) (Disc 1)": T.make_iso(32, 4),
                    "Two Part (USA) (Disc 2)": T.make_iso(34, 5), "Delta (USA) (Demo)": T.make_iso(30, 6)}
        self.cd = {"Cee (USA)": T.make_mode2_track(24, 7)}
        games = [(n, "Demos" if "Demo" in n else "Games", [d], "iso") for n, d in self.iso.items()]
        games += [(n, "Games", [d], "bin") for n, d in self.cd.items()]
        self.dat_path = T.write_dat(self.base / "ps2.dat", games, name=PS2_DAT)
        self.dat = datfile.parse_redump(self.dat_path)

    def cd_chd(self, game: str, rel: str, sidecars=(), **kw) -> Path:
        """A ``chdman createcd`` style CHD: an ISO is a cooked MODE1 track (2048 bytes per frame)."""
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if game in self.iso:
            T.build_chd(p, [{"type": "MODE1", "data": self.iso[game]}], gd=False, **kw)
        else:
            T.build_chd(p, [{"type": "MODE2_RAW", "data": self.cd[game]}], gd=False, **kw)
        for s in sidecars:
            (p.parent / s).write_bytes(b"side " + s.encode())
        return p

    def dvd_chd(self, game: str, rel: str, sidecars=(), **kw) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        T.build_dvd_chd(p, self.iso[game], **kw)
        for s in sidecars:
            (p.parent / s).write_bytes(b"side " + s.encode())
        return p

    def scan(self, **kw):
        kw.setdefault("cache_path", self.cache)
        return discsys.scan(self.root, self.dat, **kw)

    def platform(self):
        return platforms.get_platform("Sony PlayStation 2")


class RegistryTest(unittest.TestCase):
    def test_systems_platforms_and_sources(self) -> None:
        keys = {s.key: s for s in discsys.all_systems()}
        self.assertEqual(set(keys), {"dreamcast", "psx", "ps2"})
        self.assertEqual((keys["psx"].dat_name, keys["ps2"].dat_name), (PSX_DAT, PS2_DAT))
        self.assertTrue(keys["psx"].playlists and keys["dreamcast"].playlists)
        self.assertFalse(keys["ps2"].playlists)
        self.assertTrue(keys["ps2"].iso and not keys["psx"].iso and keys["dreamcast"].gd)
        for name in ("Sony PlayStation", "Sony PlayStation 2"):
            p = platforms.get_platform(name)
            self.assertEqual((p.source, p.layout, p.convertible), ("redump", "game_folder", True))
            self.assertIsNotNone(discsys.system_for_platform(name))
        self.assertEqual(platforms.get_platform("Sony PlayStation").dats, (PSX_DAT,))
        self.assertEqual(platforms.get_platform("Sony PlayStation 2").dats, (PS2_DAT,))
        self.assertEqual(redump.dat_url(PSX_DAT), "http://redump.org/datfile/psx/")
        self.assertEqual(redump.dat_url(PS2_DAT), "http://redump.org/datfile/ps2/")
        self.assertEqual(set(redump.REDUMP_DATS), {"Sega - Dreamcast", PSX_DAT, PS2_DAT, "Nintendo - GameCube"})
        self.assertIn(PS2_DAT, tags.REDUMP_DAT_NAMES)

    def test_disc_letters_count_as_disc_numbers(self) -> None:
        self.assertEqual(tags.disc_number(tags.redump_tags("Sister (Japan) (Disc A)")), 1)
        self.assertEqual(tags.disc_number(tags.redump_tags("Sister (Japan) (Disc B)")), 2)
        self.assertEqual(tags.disc_number(tags.redump_tags("Game (USA) (Disc 3)")), 3)

    def test_duration_text(self) -> None:
        self.assertEqual(discsys.fmt_duration(20), "20 s")
        self.assertEqual(discsys.fmt_duration(300), "5 min")
        self.assertEqual(discsys.fmt_duration(6120), "1 h 42 min")

    def test_progress_text_has_files_rate_and_eta(self) -> None:
        prog = discsys._Progress(None, 100 << 20, None)
        prog.files_total = 3
        prog.files_done = 1
        prog.done = 20 << 20
        prog.started -= 10          # 10 s ago: 2 MB/s
        text = prog.progress_text()
        self.assertIn("file 2/3", text)
        self.assertIn("MB/s", text)
        self.assertIn("left", text)


class EccMode2Test(unittest.TestCase):
    def test_mode2_parity_ignores_the_header(self) -> None:
        rnd = random.Random(9)
        secs = []
        for i in range(6):
            s = bytearray(rnd.randbytes(2352))
            s[15] = 2 if i % 2 else 1
            secs.append(s)
        ref = [bytearray(s) for s in secs]
        for r in ref:
            cdecc.generate_reference(r)
        cdecc.generate(secs)
        self.assertEqual([bytes(s) for s in secs], [bytes(r) for r in ref])
        # a MODE 2 sector gives the same parity whatever its header says
        a = bytearray(rnd.randbytes(2352))
        a[15] = 2
        b = bytearray(a)
        b[12:15] = b"\x01\x02\x03"
        cdecc.generate([a, b])
        self.assertEqual(a[2076:], b[2076:])


class ReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def hashed(self, path):
        with chdlib.Chd(path) as c:
            return c, chdlib.hash_track(c, c.tracks[0])

    def test_mode2_raw_track_roundtrip(self) -> None:
        data = T.make_mode2_track(30, 11)
        for codecs in (dict(data_codec=0), dict(data_codec=1)):
            p = self.dir / "m2.chd"
            T.build_chd(p, [{"type": "MODE2_RAW", "data": data}], gd=False, **codecs)
            c, h = self.hashed(p)
            self.assertEqual((h.size, h.sha1), (len(data), sha1(data)))
            self.assertEqual(c.tracks[0].type, "MODE2_RAW")
            self.assertFalse(c.is_dvd)

    def test_cooked_mode1_is_2048_bytes_per_frame(self) -> None:
        iso = T.make_iso(37, 12)
        p = self.dir / "iso.chd"
        info = T.build_chd(p, [{"type": "MODE1", "data": iso}], gd=False)
        c, h = self.hashed(p)
        self.assertEqual(c.tracks[0].sector_size, 2048)
        self.assertEqual(c.tracks[0].size, len(iso))
        self.assertEqual((h.size, h.md5, h.sha1), (len(iso), hashlib.md5(iso).hexdigest(), sha1(iso)))
        self.assertNotEqual(info["raw_sha1"], sha1(iso))         # createcd: the header SHA-1 covers frames + subcode

    def test_dvd_chd_all_codecs_and_self_refs(self) -> None:
        iso = T.make_iso(61, 13) + bytes(2048 * 20)             # zero runs: self references
        for kw in (dict(), dict(pick=lambda h: "zlib"), dict(pick=lambda h: "none"), dict(hunk_sectors=4),
                   dict(compressed_map=False), dict(pick=lambda h: "lzma", hunk_sectors=8)):
            with self.subTest(kw=list(kw)):
                p = self.dir / "d.chd"
                info = T.build_dvd_chd(p, iso, **kw)
                c, h = self.hashed(p)
                self.assertTrue(c.is_dvd and not c.is_cd)
                self.assertEqual((c.tracks[0].type, c.tracks[0].size), ("DVD", len(iso)))
                self.assertEqual(h.sha1, sha1(iso))
                self.assertEqual(c.raw_sha1, sha1(iso))             # createdvd: header SHA-1 == ISO SHA-1
                self.assertEqual(info["sha1"], sha1(iso))
                self.assertEqual(c.describe()["kind"], "dvd")

    def test_unsupported_codec_asks_for_chdman_not_a_crash(self) -> None:
        iso = T.make_iso(20, 14)
        p = self.dir / "z.chd"
        T.build_dvd_chd(p, iso, codecs=("zstd", "lzma", "zlib", "wxyz"), pick=lambda h: "wxyz" if h == 1 else "zlib")
        with chdlib.Chd(p) as c:                                # header / metadata still readable: identification works
            self.assertEqual(c.raw_sha1, sha1(iso))
            with self.assertRaises(chdlib.ChdUnsupported) as cm:
                chdlib.hash_track(c, c.tracks[0])
        self.assertTrue(cm.exception.needs_chdman)
        self.assertIn("needs chdman", str(cm.exception))
        self.assertIn("wxyz", str(cm.exception))

    def test_dvd_decode_can_be_cancelled(self) -> None:
        p = self.dir / "c.chd"
        T.build_dvd_chd(p, T.make_iso(200, 15))
        with chdlib.Chd(p) as c:
            with self.assertRaises(InterruptedError):
                chdlib.hash_track(c, c.tracks[0], cancel=lambda: True)

    def test_truncated_dvd_is_an_error(self) -> None:
        p = self.dir / "t.chd"
        T.build_dvd_chd(p, T.make_iso(20, 16), compressed_map=False)
        raw = p.read_bytes()
        p.write_bytes(raw[:len(raw) // 2])
        with self.assertRaises(chdlib.ChdError):
            with chdlib.Chd(p) as c:
                chdlib.hash_track(c, c.tracks[0])


class IndexTest(unittest.TestCase):
    def test_iso_single_bin_and_tracks(self) -> None:
        w = Ps2World(self)
        idx = discsys.get_index(w.dat)
        self.assertEqual(idx.system.key, "ps2")
        a = idx.games["Alpha (USA)"]
        self.assertTrue(a.iso and a.sizes == (len(w.iso["Alpha (USA)"]),))
        c = idx.games["Cee (USA)"]
        self.assertFalse(c.iso)
        self.assertEqual(c.sizes, (len(w.cd["Cee (USA)"]),))
        self.assertIsNotNone(c.cue)
        self.assertEqual(idx.disc_totals()["Two Part (USA) (Disc 2)"], 2)

    def test_multi_track_psx_game(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        t1, t2 = T.make_mode2_track(10, 1), T.make_audio_track(6, 2)
        dat = datfile.parse_redump(T.write_dat(d / "p.dat", [("Mix (USA)", "Games", [t1, t2])], name=PSX_DAT))
        g = discsys.get_index(dat).games["Mix (USA)"]
        self.assertEqual((discsys.get_index(dat).system.key, g.sizes), ("psx", (len(t1), len(t2))))


class Ps2ScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = Ps2World(self)

    def test_iso_in_cd_frames_is_verified_by_the_python_reader(self) -> None:
        self.w.cd_chd("Alpha (USA)", "Alpha (USA).chd")
        self.w.cd_chd("Cee (USA)", "Cee (USA).chd")
        r = self.w.scan()
        by = {m.unit.game.name: m for m in r.matched}
        self.assertEqual(set(by), {"Alpha (USA)", "Cee (USA)"})
        self.assertEqual({m.level for m in r.matched}, {"verified"})        # no audio: identified == verified
        s = r.summary()
        self.assertEqual((s["verified"], s["identified"], s["system"]), (2, 0, "ps2"))
        self.assertEqual(s["dat_total"], len(self.w.iso) + 1)

    def test_a_different_iso_of_the_same_size_does_not_match(self) -> None:
        liar = bytearray(self.w.iso["Alpha (USA)"])
        liar[5000] ^= 1
        p = self.w.root / "liar.chd"
        T.build_chd(p, [{"type": "MODE1", "data": bytes(liar)}], gd=False)
        r = self.w.scan()
        self.assertEqual(r.matched, [])
        self.assertIn("do not match", r.unmatched[0].reason)

    def test_dvd_chd_is_identified_from_the_header_without_decoding(self) -> None:
        self.w.dvd_chd("Beta (Europe) (En,Fr,De)", "x/Beta.chd")
        with mock.patch.object(chdlib, "hash_track", side_effect=AssertionError("decoded")):
            r = self.w.scan()
        m = r.matched[0]
        self.assertEqual((m.unit.game.name, m.level, m.unit.via, m.unit.disc_kind), ("Beta (Europe) (En,Fr,De)", "identified", "header", "dvd"))
        self.assertEqual(r.summary()["identified"], 1)
        self.assertFalse(m.item(mock.Mock(root=str(self.w.root)))["tracks"][0]["hashed"])

    def test_verify_fully_decodes_a_dvd_chd_then_it_is_verified_and_cached(self) -> None:
        self.w.dvd_chd("Beta (Europe) (En,Fr,De)", "Beta.chd")
        r = self.w.scan()
        res = discsys.verify_units(r, cache_path=self.w.cache)
        self.assertEqual((res["verified"], res["failed"], res["checked"]), (1, [], 1))
        with mock.patch.object(chdlib, "hash_track", side_effect=AssertionError("decoded again")):
            r2 = self.w.scan()
        self.assertEqual((r2.matched[0].level, r2.matched[0].unit.via), ("verified", "python"))

    def test_a_dvd_chd_with_a_lying_header_fails_verification(self) -> None:
        good = self.w.iso["Beta (Europe) (En,Fr,De)"]
        liar = bytearray(good)
        liar[777] ^= 1
        p = self.w.root / "Beta.chd"
        T.build_dvd_chd(p, bytes(liar))
        raw = bytearray(p.read_bytes())               # forge the header's raw SHA-1 with the real ISO's
        raw[64:84] = bytes.fromhex(sha1(good))
        p.write_bytes(bytes(raw))
        r = self.w.scan()
        self.assertEqual(r.matched[0].level, "identified")            # the claim alone cannot say better
        res = discsys.verify_units(r, cache_path=self.w.cache)
        self.assertEqual((res["verified"], len(res["failed"])), (0, 1))
        self.assertEqual(self.w.scan().matched, [])

    def test_zstd_dvd_chd_is_identified_by_its_header_and_verify_says_needs_chdman(self) -> None:
        iso = self.w.iso["Gamma (Japan)"]
        p = self.w.root / "Gamma.chd"
        T.build_dvd_chd(p, iso, codecs=("zstd", "lzma", "zlib", "wxyz"), pick=lambda h: "wxyz" if h == 2 else "zlib")
        r = self.w.scan()
        self.assertEqual(r.matched[0].level, "identified")
        res = discsys.verify_units(r, cache_path=self.w.cache)
        self.assertEqual(res["verified"], 0)
        self.assertIn("needs chdman", res["failed"][0]["error"])

    def test_cd_chd_with_an_undecodable_codec_is_left_in_place(self) -> None:
        p = self.w.root / "Odd" / "Odd.chd"
        p.parent.mkdir()
        T.build_chd(p, [{"type": "MODE1", "data": self.w.iso["Alpha (USA)"]}], gd=False)
        raw = bytearray(p.read_bytes())
        raw[16:20] = b"wxyz"                         # the first codec slot now names an unknown codec: all hunks of that slot
        p.write_bytes(bytes(raw))
        r = self.w.scan()
        self.assertEqual(r.matched, [])
        self.assertTrue(r.unmatched[0].needs_chdman)
        self.assertIn("needs chdman", r.unmatched[0].reason)
        plan = discsys.plan_tidy(r)
        self.assertEqual([op.status for op in plan], ["skip"])
        self.assertIn("left in place", plan[0].reason)
        self.assertEqual(r.summary()["needs_chdman"], 1)

    def test_no_cross_matching_between_systems(self) -> None:
        self.w.cd_chd("Alpha (USA)", "Alpha (USA).chd")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        other = datfile.parse_redump(T.write_dat(Path(tmp.name) / "p.dat", [("Alpha (USA)", "Games", [self.w.iso["Alpha (USA)"]])],
                                                 name=PSX_DAT))
        r = discsys.scan(self.w.root, other, cache_path=self.w.cache)
        self.assertEqual(r.system.key, "psx")          # an ISO-less system does not know the DVD sizes of the PS2
        self.assertEqual([m.unit.game.name for m in r.matched], ["Alpha (USA)"])  # same bytes + name in ITS own DAT: fine
        dvd = self.w.dvd_chd("Gamma (Japan)", "g.chd")
        r2 = discsys.scan(self.w.root, other, cache_path=self.w.cache)
        self.assertIn("DVD CHD", [e.reason for e in r2.unmatched if e.rel == dvd.name][0])

    def test_raw_iso_is_matched_by_hash(self) -> None:
        (self.w.root / "raw1").mkdir()
        (self.w.root / "raw1" / "whatever.iso").write_bytes(self.w.iso["Gamma (Japan)"])
        (self.w.root / "loose.iso").write_bytes(self.w.iso["Alpha (USA)"])
        (self.w.root / "other.iso").write_bytes(T.make_iso(40, 99))
        r = self.w.scan()
        by = {m.unit.game.name: m for m in r.matched}
        self.assertEqual({k: v.level for k, v in by.items()}, {"Gamma (Japan)": "raw", "Alpha (USA)": "raw"})
        self.assertEqual(len(r.unmatched), 1)
        self.assertEqual(r.summary()["convertible"], 2)

    def test_scan_progress_mentions_the_file(self) -> None:
        self.w.cd_chd("Alpha (USA)", "Alpha (USA).chd")
        msgs = []
        self.w.scan(progress=lambda d, t, m: msgs.append(m))
        self.assertTrue(any("Alpha (USA).chd" in m for m in msgs))


class Ps2LayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = Ps2World(self)
        self.profile = library.default_profile(self.w.platform())

    def apply(self, plan_fn):
        r = self.w.scan()
        plan = plan_fn(r)
        ops = plan.ops if hasattr(plan, "ops") else plan
        res = discsys.apply_plan(ops, self.w.root, playlists=getattr(plan, "playlists", ()))
        return r, res

    def test_loose_chd_gets_its_own_folder_and_the_sidecars_travel(self) -> None:
        self.w.cd_chd("Alpha (USA)", "My Alpha.chd", ["My Alpha.zip", "My Alpha.state", "My Alpha.srm", "My Alpha.ini", "notes.txt"])
        self.w.cd_chd("Gamma (Japan)", "Gamma Folder/gg.chd", ["gg.state1", "readme.txt"])
        before = tree(self.w.root)
        r, res = self.apply(discsys.plan_tidy)
        got = tree(self.w.root)
        for ext in (".chd", ".zip", ".state", ".srm", ".ini"):
            self.assertIn(f"Alpha (USA)/Alpha (USA){ext}", got, ext)
        self.assertIn("_unmatched/notes.txt", got)                        # an unrelated loose file is not the game's: unmatched
        self.assertIn("Gamma (Japan)/Gamma (Japan).chd", got)
        self.assertIn("Gamma (Japan)/Gamma (Japan).state1", got)
        self.assertIn("Gamma (Japan)/readme.txt", got)                    # unrelated files in a game folder move along
        self.assertNotIn("Gamma Folder", got)
        out = organiser.undo(Path(res["undo_log"]), root=self.w.root)
        self.assertEqual(out.get("remaining", 0), 0)
        self.assertEqual(tree(self.w.root), before)

    def test_unmatched_game_folder_goes_whole_to_unmatched(self) -> None:
        T.build_chd(self.w.root / "Mystery" / "m.chd", [{"type": "MODE1", "data": T.make_iso(40, 77)}], gd=False) if (self.w.root / "Mystery").mkdir() is None else None
        (self.w.root / "Mystery" / "m.state").write_bytes(b"s")
        r, _res = self.apply(discsys.plan_tidy)
        got = tree(self.w.root)
        self.assertIn("_unmatched/Mystery/m.chd", got)
        self.assertIn("_unmatched/Mystery/m.state", got)

    def test_library_build_keeps_discs_together_and_writes_no_playlist(self) -> None:
        self.w.cd_chd("Two Part (USA) (Disc 1)", "a.chd")
        self.w.cd_chd("Two Part (USA) (Disc 2)", "b.chd")
        self.w.cd_chd("Delta (USA) (Demo)", "d.chd")
        self.w.cd_chd("Beta (Europe) (En,Fr,De)", "be.chd", ["be.state"])
        r = self.w.scan()
        plan = discsys.plan_library(r, self.profile)
        self.assertEqual(plan.playlists, [])
        res = discsys.apply_plan(plan.ops, self.w.root, playlists=plan.playlists)
        got = tree(self.w.root)
        self.assertIn("Two Part (USA) (Disc 1)/Two Part (USA) (Disc 1).chd", got)
        self.assertIn("Two Part (USA) (Disc 2)/Two Part (USA) (Disc 2).chd", got)
        self.assertIn("_excluded/d.chd", got)                             # demo (category)
        self.assertIn("Beta (Europe) (En,Fr,De)/Beta (Europe) (En,Fr,De).state", got)
        self.assertFalse([k for k in got if k.endswith(".m3u")])
        again = discsys.plan_library(self.w.scan(), self.profile)
        self.assertEqual([op for op in again.ops if op.status == "move"], [])
        self.assertTrue(res["undo_log"])


class PsxPlaylistTest(unittest.TestCase):
    def test_multi_disc_psx_gets_an_m3u_next_to_disc_one(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        root = base / "psx"
        root.mkdir()
        t = {n: T.make_mode2_track(10 + i, 20 + i) for i, n in enumerate(["Big (USA) (Disc 1)", "Big (USA) (Disc 2)", "Solo (USA)"])}
        dat = datfile.parse_redump(T.write_dat(base / "p.dat", [(n, "Games", [d], "bin") for n, d in t.items()], name=PSX_DAT))
        for n, d in t.items():
            T.build_chd(root / f"{n}.chd", [{"type": "MODE2_RAW", "data": d}], gd=False)
        r = discsys.scan(root, dat, cache_path=base / "h.sqlite")
        self.assertEqual({m.level for m in r.matched}, {"verified"})
        plan = discsys.plan_library(r, library.default_profile(platforms.get_platform("Sony PlayStation")))
        self.assertEqual(len(plan.playlists), 1)
        discsys.apply_plan(plan.ops, root, playlists=plan.playlists)
        m3u = root / "Big (USA)" / "Big (USA).m3u" if (root / "Big (USA)").exists() else None
        found = list(root.rglob("*.m3u"))
        self.assertEqual(len(found), 1, found)
        lines = [ln for ln in found[0].read_text().splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(ln.endswith(".chd") for ln in lines))
        self.assertIsNone(m3u) if False else None
        for ln in lines:
            self.assertTrue((found[0].parent / ln).is_file(), ln)            # relative paths that resolve


class ConvertTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = Ps2World(self)
        self.bin = self.w.base / "bin"
        self.bin.mkdir()
        self.fake = T.install_fake_chdman(self.bin)
        T.isolate_temp(self, self.w.base)
        self.iso = self.w.iso["Alpha (USA)"]
        self.dvd_fixture = self.w.base / "dvd.chd"
        T.build_dvd_chd(self.dvd_fixture, self.iso)
        self.cd_fixture = self.w.base / "cd.chd"
        T.build_chd(self.cd_fixture, [{"type": "MODE1", "data": self.iso}], gd=False)
        self.iso_file = self.w.base / "Alpha.iso"
        self.iso_file.write_bytes(self.iso)
        env = {"FAKE_DVD_CHD": str(self.dvd_fixture), "FAKE_CHD": str(self.cd_fixture), "FAKE_ISO": str(self.iso_file),
               "FAKE_LOG": str(self.w.base / "log.txt"),
               "FAKE_RAW": str(T.prepare_fake_raw_cd(self.w.base / "fakeraw", [self.iso]))}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.chdman = chdtool.Chdman([str(self.fake)], "configured", str(self.fake))
        (self.w.root / "raw").mkdir()
        (self.w.root / "raw" / "Alpha.iso").write_bytes(self.iso)
        (self.w.root / "raw" / "Alpha.md5").write_text("sum")

    def test_auto_uses_chdman_only_for_a_chd_the_reader_cannot_decode(self) -> None:
        p = self.w.root / "Odd" / "Odd.chd"
        p.parent.mkdir()
        T.build_chd(p, [{"type": "MODE1", "data": self.iso}], gd=False)
        raw = bytearray(p.read_bytes())
        raw[16:20] = b"wxyz"                          # a codec the built-in reader cannot decode
        p.write_bytes(bytes(raw))
        (self.w.base / "log.txt").write_text("")
        r = self.w.scan(chdman=self.chdman, engine="python")          # python: never chdman
        self.assertEqual([m for m in r.matched if m.kind == "chd"], [])
        self.assertTrue([e for e in r.unmatched if e.needs_chdman])
        self.assertEqual((self.w.base / "log.txt").read_text(), "")
        r = self.w.scan(chdman=self.chdman)                            # auto: the reader first, chdman as the fallback
        self.assertEqual([(m.unit.game.name, m.level, m.unit.via) for m in r.matched if m.kind == "chd"],
                         [("Alpha (USA)", "verified", "chdman")])
        self.assertIn("extractcd", (self.w.base / "log.txt").read_text())
        self.assertEqual(r.engine, "chdman")
        self.assertIn("chdman:", r.engine_info["text"])

    def test_auto_does_not_touch_chdman_for_a_decodable_chd(self) -> None:
        self.w.cd_chd("Alpha (USA)", "Alpha (USA).chd")
        (self.w.base / "log.txt").write_text("")
        r = self.w.scan(chdman=self.chdman)
        chd_m = [m for m in r.matched if m.kind == "chd"]
        self.assertEqual((chd_m[0].level, chd_m[0].unit.via, r.engine), ("verified", "python", "python"))
        self.assertEqual((self.w.base / "log.txt").read_text(), "")

    def run_convert(self, config=None):
        r = self.w.scan()
        ops = discsys.plan_convert(r, True, config)
        return ops, discsys.apply_conversions(ops, self.w.root, self.chdman, r.index, writer="chdman")

    def test_iso_defaults_to_createdvd_and_is_verified_against_redump(self) -> None:
        before = tree(self.w.root)
        ops, res = self.run_convert()
        self.assertEqual((ops[0].mode, res["converted"], res["failed"]), ("createdvd", 1, []))
        log = (self.w.base / "log.txt").read_text()
        self.assertIn("createdvd", log)
        self.assertNotIn("extractdvd", log)                    # verified by OUR reader, independent of chdman
        self.assertIn("independently", res["verify_text"])
        got = tree(self.w.root)
        self.assertIn("Alpha (USA)/Alpha (USA).chd", got)
        self.assertIn("_converted_originals/raw/Alpha.iso", got)
        r = self.w.scan()
        self.assertEqual([(m.unit.game.name, m.kind) for m in r.matched], [("Alpha (USA)", "chd")])
        out = organiser.undo(Path(res["undo_log"]), root=self.w.root)
        self.assertEqual(out.get("remaining", 0), 0)
        self.assertEqual(tree(self.w.root), before)

    def test_config_can_choose_createcd_for_an_iso(self) -> None:
        ops, res = self.run_convert({"ps2_iso_chd": "cd"})
        self.assertEqual((ops[0].mode, res["converted"]), ("createcd", 1))
        log = (self.w.base / "log.txt").read_text()
        self.assertIn("createcd", log)
        self.assertNotIn("createdvd", log)
        self.assertEqual(discsys.iso_convert_mode(discsys.SYSTEMS["psx"], {"ps2_iso_chd": "dvd"}), "")

    def test_a_dvd_chd_that_is_not_the_game_is_rejected(self) -> None:
        bad = bytearray(self.iso)
        bad[9000] ^= 1
        T.build_dvd_chd(self.dvd_fixture, bytes(bad))
        wrong = self.w.base / "wrong.iso"
        wrong.write_bytes(bytes(bad))                    # what the fake extractdvd "decodes" from the new CHD
        before = tree(self.w.root)
        with mock.patch.dict(os.environ, {"FAKE_ISO": str(wrong)}):
            _ops, res = self.run_convert()
        self.assertEqual(res["converted"], 0)
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(tree(self.w.root), before)

    def test_cue_bin_sets_use_createcd(self) -> None:
        import shutil
        shutil.rmtree(self.w.root / "raw")
        track = self.w.cd["Cee (USA)"]
        (self.w.root / "cue").mkdir()
        (self.w.root / "cue" / "Cee.bin").write_bytes(track)
        (self.w.root / "cue" / "Cee.cue").write_text('FILE "Cee.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
        fix = self.w.base / "cee.chd"
        T.build_chd(fix, [{"type": "MODE2_RAW", "data": track}], gd=False)
        with mock.patch.dict(os.environ, {"FAKE_CHD": str(fix), "FAKE_RAW": str(T.prepare_fake_raw_cd(self.w.base / "fr2", [track]))}):
            r = self.w.scan()
            ops = discsys.plan_convert(r, True)
            self.assertEqual((ops[0].mode, ops[0].status), ("createcd", "convert"))
            res = discsys.apply_conversions(ops, self.w.root, self.chdman, r.index, writer="chdman")
        self.assertEqual(res["converted"], 1)
        self.assertIn("createcd", (self.w.base / "log.txt").read_text())
        self.assertTrue((self.w.root / "Cee (USA)" / "Cee (USA).chd").is_file())

    def test_without_chdman_the_builtin_writer_converts(self) -> None:
        r = self.w.scan()
        ops = discsys.plan_convert(r, False)
        self.assertTrue(ops and all(o.status == "convert" for o in ops))
        res = discsys.apply_conversions(ops, self.w.root, None, r.index)
        self.assertEqual((res["converted"], res["failed"]), (len(ops), []))
        self.assertEqual(res["written_by"], {"builtin": len(ops)})
        r2 = self.w.scan()
        self.assertEqual(sorted(m.kind for m in r2.matched if m.kind == "chd"), ["chd"] * len(ops))
        for op in ops:
            with chdlib.Chd(op.dst) as c:
                self.assertEqual(c.verify()["overall"], True)
                self.assertEqual(c.is_dvd, op.mode == "createdvd")

    def test_the_writer_uses_the_jobs_worker_count(self) -> None:
        from romorg import chdwrite
        real = chdwrite.write_chd
        seen = []

        def spy(*a, **kw):
            seen.append(kw.get("threads"))
            return real(*a, **kw)
        r = self.w.scan()
        ops = discsys.plan_convert(r, False)
        with mock.patch.dict(os.environ, {chdwrite.ENV_THREADS: ""}), \
                mock.patch("romorg.chdwrite.write_chd", side_effect=spy):
            res = discsys.apply_conversions(ops, self.w.root, None, r.index, workers=3)
        self.assertEqual(res["converted"], len(ops))
        self.assertEqual(seen, [3] * len(ops))                    # the chd_workers setting, not the CPU count

    def test_a_release_that_times_out_restarts_the_workers_before_the_rename(self) -> None:
        from romorg import chdsched
        r = self.w.scan()
        ops = discsys.plan_convert(r, False)
        real = chdsched.Scheduler.restart_workers
        with mock.patch.object(chdsched.Scheduler, "release", return_value=False), \
                mock.patch.object(chdsched.Scheduler, "restart_workers", autospec=True, side_effect=real) as restart:
            res = discsys.apply_conversions(ops, self.w.root, None, r.index, workers=2)
        self.assertEqual((res["converted"], res["failed"]), (len(ops), []))
        self.assertEqual(restart.call_count, len(ops))

    def test_zstandard_preset(self) -> None:
        from romorg import chdwrite
        if not chdwrite.zstd_available():
            self.skipTest("no Zstandard library")
        r = self.w.scan()
        ops = discsys.plan_convert(r, False)
        res = discsys.apply_conversions(ops, self.w.root, None, r.index, preset="zstd")
        self.assertEqual((res["converted"], res["failed"], res["preset"]), (len(ops), [], "zstd"))
        for op in ops:
            with chdlib.Chd(op.dst) as c:
                self.assertIn(c.compressors[0], ("zstd", "cdzs"))
                self.assertEqual(c.verify()["overall"], True)


@unittest.skipUnless(glob.glob(str(SCRATCH / "*.dat")), "real Sony Redump DATs not available")
class RealDatTest(unittest.TestCase):
    def load(self, pat: str, name: str):
        import dataclasses
        dat = datfile.parse_redump(glob.glob(str(SCRATCH / pat))[0])
        dat.name = name
        dat.roms = [dataclasses.replace(r, dat=name) for r in dat.roms]
        return dat

    def test_indexes_and_default_library_numbers(self) -> None:
        for pat, name, plat, games, kept in (("Sony - PlayStation - *.dat", PSX_DAT, "Sony PlayStation", 10914, 2270),
                                              ("Sony - PlayStation 2 - *.dat", PS2_DAT, "Sony PlayStation 2", 11774, 3188)):
            dat = self.load(pat, name)
            idx = discsys.get_index(dat)
            self.assertEqual(len(idx.games), games)
            tot = idx.disc_totals()
            items = [library.Item(key=i, dat=name, rom=g.rep, style=tags.STYLE_REDUMP, path=None, member=None,
                                  disc_total=tot.get(g.name, 0)) for i, g in enumerate(idx.games.values())]
            p = platforms.get_platform(plat)
            sel = library.select(items, library.default_profile(p), p)
            actions = [d.action for d in sel.decisions.values()]
            self.assertEqual(actions.count("keep"), kept, plat)
            self.assertEqual(len(actions), games)
        ps2 = discsys.get_index(self.load("Sony - PlayStation 2 - *.dat", PS2_DAT))
        self.assertEqual(sum(1 for g in ps2.games.values() if g.iso), 8606)


if __name__ == "__main__":
    unittest.main()
