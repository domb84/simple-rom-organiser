"""The "Commodore Amiga - WHDLoad" system (Amendment 10) and the folder-persistence / config fixes.

Synthetic data everywhere; the real DAT (4121 entries) is only used when present (skipped otherwise).
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import random
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from unittest import mock
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from romorg import autoupdate, datfile, folders, kickstart, library, organiser, paths, platforms  # noqa: E402
from romorg import scanner, server, tags, whdload  # noqa: E402
from romorg.tosec import Cancelled, DatInfo  # noqa: E402

REAL_DAT = Path(os.environ.get("WHD_REAL_DAT", "/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/"
                               "cfc5ea37-4261-428c-9422-29acd65cac97/scratchpad/whd/whd.dat"))
DAT = "Commodore - Amiga - WHDLoad"
PLAT = "Commodore Amiga - WHDLoad"


def _blob(rng: random.Random, n: int = 600) -> bytes:
    return b"\x00\x00-lh5-" + bytes(rng.getrandbits(8) for _ in range(n))


def _dat_text(entries: list[tuple[str, str, bytes]], header: str = DAT, date: str = "2026-07-05") -> str:
    out = [f'clrmamepro (\n\tname "{header}"\n\tdescription "{header}"\n\tdate "{date}"\n\tauthor "MrV2k"\n)\n']
    for game, name, data in entries:
        out.append(f'game (\n\tname "{game}"\n\tdescription "{game}"\n\trom ( name "{name}" size {len(data)} '
                   f'crc {zlib.crc32(data):08X} md5 {hashlib.md5(data).hexdigest().upper()} '
                   f'sha1 {hashlib.sha1(data).hexdigest()} )\n)\n')
    return "\n".join(out)


class _Resp:
    def __init__(self, body: bytes = b"", status: int = 200, etag: str = '"e1"') -> None:
        self._b = io.BytesIO(body)
        self.status = status
        self.headers = {"ETag": etag, "Content-Length": str(len(body))}

    def read(self, n: int = -1) -> bytes:
        return self._b.read(n)

    def close(self) -> None:
        pass


class DataDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": self.tmp.name, "ROMORG_OFFLINE": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.dir = Path(self.tmp.name)
        # ROM folders must live OUTSIDE the data dir (the app refuses folders inside it)
        self.work_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.work_tmp.cleanup)
        self.work = Path(self.work_tmp.name)


# --------------------------------------------------------------------------- DAT source


class SourceTest(DataDirCase):
    def setUp(self) -> None:
        super().setUp()
        rng = random.Random(1)
        self.entries = [("Game A", "GameA_v1.0.lha", _blob(rng)), ("Game B (AGA)", "GameB_v1.0_AGA.lha", _blob(rng))]
        self.body = _dat_text(self.entries).encode()

    def test_own_directory_and_names(self) -> None:
        self.assertEqual(paths.whdload_dir(), self.dir / "whdload")
        self.assertEqual(whdload.DAT_NAME, DAT)
        self.assertEqual(tags.WHDLOAD_DAT_NAME, DAT)
        self.assertEqual(platforms.WHDLOAD_DAT, DAT)
        self.assertEqual(whdload.dat_url(), "https://raw.githubusercontent.com/MrV2K/WHDLoad-Database/main/"
                                            "Commodore%20-%20Amiga%20-%20WHDLoad.dat")

    def test_download_validates_and_writes_manifest(self) -> None:
        seen = []

        def opener(req):
            seen.append((req.get_method(), req.full_url, req.get_header("If-none-match")))
            return _Resp(self.body)

        res = whdload.download_dat(opener=opener)
        self.assertEqual((res["status"], res["version"]), ("downloaded", "2026-07-05"))
        info = whdload.find_dat()
        self.assertEqual((info.name, info.version), (DAT, "2026-07-05"))
        self.assertEqual(whdload.read_manifest()[DAT]["etag"], '"e1"')
        self.assertFalse((paths.whdload_dir() / f"{DAT}.dat.part").exists())
        # second run sends the stored ETag; 304 -> unchanged
        err = urllib.error.HTTPError("u", 304, "Not Modified", {}, None)  # type: ignore[arg-type]

        def opener2(req):
            seen.append(req.get_header("If-none-match"))
            raise err

        self.assertEqual(whdload.download_dat(opener=opener2)["status"], "unchanged")
        self.assertEqual(seen[-1], '"e1"')

    def test_invalid_download_keeps_old_file(self) -> None:
        whdload.download_dat(opener=lambda r: _Resp(self.body))
        before = (paths.whdload_dir() / f"{DAT}.dat").read_bytes()
        for bad in (b"<html>nope</html>", _dat_text(self.entries, header="Other DAT").encode(),
                    b'clrmamepro (\n name "' + DAT.encode() + b'"\n)\n'):
            with self.assertRaises(whdload.WhdloadError):
                whdload.download_dat(opener=lambda r, b=bad: _Resp(b), force=True)
            self.assertEqual((paths.whdload_dir() / f"{DAT}.dat").read_bytes(), before)
            self.assertFalse((paths.whdload_dir() / f"{DAT}.dat.part").exists())
        with self.assertRaises(whdload.WhdloadError):
            whdload.download_dat(opener=lambda r: (_ for _ in ()).throw(urllib.error.URLError("no route")), force=True)

    def test_cancel_and_check_updates(self) -> None:
        ev = threading.Event()
        ev.set()
        with self.assertRaises(Cancelled):
            whdload.download_dat(opener=lambda r: _Resp(self.body), cancel=ev)
        self.assertFalse(list(paths.whdload_dir().glob("*.part")))
        rows = whdload.check_updates(opener=lambda r: _Resp(b"", etag='"e1"'))
        self.assertEqual(rows[0]["status"], "missing")
        whdload.download_dat(opener=lambda r: _Resp(self.body))
        self.assertEqual(whdload.check_updates(opener=lambda r: _Resp(b"", etag='W/"e1"'))[0]["status"], "up_to_date")
        self.assertEqual(whdload.check_updates(opener=lambda r: _Resp(b"", etag='"e2"'))[0]["status"], "update_available")
        bad = whdload.check_updates(opener=lambda r: (_ for _ in ()).throw(urllib.error.URLError("x")))
        self.assertEqual(bad[0]["status"], "error")

    def test_update_dats_offline_raises_only_without_cache(self) -> None:
        boom = lambda r: (_ for _ in ()).throw(urllib.error.URLError("no route"))  # noqa: E731
        with self.assertRaises(whdload.WhdloadError):
            whdload.update_dats(opener=boom)
        whdload.download_dat(opener=lambda r: _Resp(self.body))
        res = whdload.update_dats(opener=boom, force=True)
        self.assertEqual((res["failed"], res["count"], res["source"]), (1, 1, "whdload"))

    def test_header_date_is_the_version(self) -> None:
        p = self.dir / "x.dat"
        p.write_bytes(self.body)
        self.assertEqual(whdload.header_version(p), "2026-07-05")
        self.assertEqual(datfile.parse_dat(p).version, "2026-07-05")

    def test_windows_1252_dat_parses(self) -> None:
        p = self.dir / "y.dat"
        text = _dat_text([("Alien\xb3 (Beta)", "Alien3_v1.0.lha", b"abc")])
        p.write_bytes(text.encode("cp1252"))
        self.assertEqual(datfile.parse_dat(p).roms[0].game, "Alien\xb3 (Beta)")

    def test_never_wiped_by_other_updates_and_separate_dir(self) -> None:
        whdload.download_dat(opener=lambda r: _Resp(self.body))
        # a TOSEC swap and a No-Intro listing never see / remove it
        from romorg import nointro, tosec
        self.assertEqual(nointro.list_dats(), [])
        self.assertEqual(tosec.list_dats(), [])
        (paths.dats_dir() / "Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat").write_text("x")
        self.assertIsNotNone(whdload.find_dat())
        self.assertTrue(platforms.locate_dats(platforms.get_platform(PLAT)))
        self.assertNotIn(DAT, platforms.locate_dats(platforms.get_platform("Commodore Amiga")))


class AutoUpdateTest(DataDirCase):
    def test_manager_updates_whdload_in_its_own_scope(self) -> None:
        class W:
            def __init__(self) -> None:
                self.local = False
                self.calls = []

            def check_updates(self, names=None, **kw):
                return [{"name": DAT, "installed": "2026-07-05" if self.local else None,
                         "status": "up_to_date" if self.local else "missing"}]

            def list_dats(self, directory=None):
                return [DatInfo(DAT, "2026-07-05", paths.whdload_dir() / f"{DAT}.dat")] if self.local else []

            def update_dats(self, names=None, progress=None, cancel=None, force=False, commit_lock=None, **kw):
                self.calls.append(list(names))
                with commit_lock:
                    (paths.whdload_dir() / f"{DAT}.dat").write_text(_dat_text([("G", "G_v1.0.lha", b"x")]))
                    self.local = True
                return {"dats": [{"name": DAT, "version": "2026-07-05", "status": "downloaded"}], "failed": 0}

        class Nothing:   # TOSEC / No-Intro must not be touched by a WHDLoad-scoped update
            def __getattr__(self, name):
                if name == "update_lock":
                    raise AttributeError(name)
                raise AssertionError(f"unexpected call {name}")

        w = W()
        os.environ.pop("ROMORG_OFFLINE", None)
        mgr = autoupdate.UpdateManager(tosec=Nothing(), nointro=Nothing(), whdload=w, redump=None, enabled=True)
        mgr.ensure(platforms.get_platform(PLAT))
        self.assertEqual(w.calls, [[DAT]])
        st = mgr.status()
        self.assertEqual((st["whdload"]["installed"], st["whdload"]["status"]), ("2026-07-05", "up_to_date"))
        self.assertIn("whdload", st)


# --------------------------------------------------------------------------- tags


class TagsTest(unittest.TestCase):
    def t(self, stem: str, game: str = "") -> tags.Tags:
        return tags.parse_whdload(stem, game)

    def test_basic_tokens(self) -> None:
        t = self.t("1869_v1.0_De_AGA_1653", "1869 - Erlebte Geschichte (Teil 1) (German) (AGA)")
        self.assertEqual((t.title, t.version, t.build, t.languages, t.style),
                         ("1869 - Erlebte Geschichte", "v1.0", 1653, ("De",), "whdload"))
        self.assertIn("AGA", t.flags)
        self.assertEqual(tags.platform_class(t), "AGA")
        self.assertEqual(self.t("Zool_v1.2_CD32_NTSC", "Zool (NTSC) (CD32)").video, ("NTSC",))
        self.assertEqual(tags.platform_class(self.t("Zool_v1.2_CD32_NTSC", "Zool (NTSC) (CD32)")), "CD32")
        plain = self.t("Time_v1.2_1697", "Time")
        self.assertTrue(plain.languages_implied)
        self.assertEqual(tags.variant_languages(plain), frozenset({"En"}))

    def test_languages_multi_and_stem_codes(self) -> None:
        t = self.t("Elf_v1.3_Ocean_EnFrDe_0473", "Elf (Ocean)")
        self.assertEqual(t.languages, ("En", "Fr", "De"))
        self.assertEqual(tags.variant_languages(t), frozenset({"En", "Fr", "De"}))
        self.assertEqual(self.t("X_v1.0_Cz").languages, ("Cs",))
        self.assertEqual(self.t("X_v1.0_Dk").languages, ("Da",))
        self.assertEqual(self.t("X_v1.0_Se").languages, ("Sv",))
        self.assertEqual(self.t("X_v1.0_Gr").languages, ("El",))
        self.assertEqual(self.t("Shadow_v1.0_AGA_No_jump_by_ztronzo").languages, ())   # "No" is not Norwegian

    def test_versions(self) -> None:
        self.assertEqual(self.t("A_v1.4a_X").version, "v1.4a")
        self.assertEqual(self.t("A_v2.1-B_AGA").version, "v2.1b")
        self.assertEqual(self.t("A_v3.0.0_1MB").version, "v3.0.0")
        self.assertEqual(self.t("A_Beta3_AGA").version, "")
        self.assertLess(self.t("A_v1.9").version_key, self.t("A_v1.10").version_key)

    def test_memory_variants(self) -> None:
        std = self.t("T_v1.0", "T")
        for stem, game in (("T_v1.0_512k", "T (512KB)"), ("T_v1.0_LowMem", "T (Low Mem)"), ("T_v1.0_512Kb", "T (512k)")):
            self.assertEqual(tags.whd_memory_rank(self.t(stem, game)), 2, stem)
        for stem, game in (("T_v1.0_1MB", "T (1MB)"), ("T_v1.0_Fast", "T (Fast Mem)"), ("T_v1.0_15MB", "T (1.5MB)"),
                           ("T_v1.0_1MbChip", "T (1MB Chip)"), ("T_v1.0_Chip", "T (Chip Mem)")):
            self.assertEqual(tags.whd_memory_rank(self.t(stem, game)), 1, stem)
        self.assertEqual(tags.whd_memory_rank(std), 0)
        # memory / NTSC / chipset / language / version are NOT identity
        key = tags.identity_key(std)
        for stem, game in (("T_v1.0_512k_NTSC_AGA_De_0001", "T (German) (AGA) (NTSC) (512KB)"), ("T_v2.0_1MB", "T (1MB)")):
            self.assertEqual(tags.identity_key(self.t(stem, game)), key)

    def test_products_stay_apart(self) -> None:
        base = tags.identity_key(self.t("D_v1.0", "D"))
        for game in ("D (Two Disk)", "D (One Disk)", "D (Image)", "D (Files)", "D (Game Demo)", "D (Cover Disk)",
                     "D (Hack)", "D (CDTV)", "D (CD-ROM)", "D (Beta)", "D (Enhanced)", "D (Arcadia)", "D (Ocean)"):
            self.assertNotEqual(tags.identity_key(self.t("D_v1.0", game)), base, game)
        self.assertNotEqual(tags.identity_key(self.t("D_v1.0_2Disk", "D (Two Disk)")),
                            tags.identity_key(self.t("D_v1.0_1Disk", "D (One Disk)")))
        self.assertEqual(tags.identity_key(self.t("D_v1.0_2Disk", "D (Two Disk)")),
                         tags.identity_key(self.t("D_v1.0_2Disk", "D (2 Disk)")))

    def test_hack_author_and_status(self) -> None:
        a = self.t("Area88_v1.2_Hack_by_Earok", "Area 88 (Hack) (Beta)")
        b = self.t("Area88_v1.2_Hack_by_Other", "Area 88 (Hack) (Beta)")
        self.assertIn("Hack", a.flags)
        self.assertIn("by Earok", a.flags)
        self.assertNotEqual(tags.identity_key(a), tags.identity_key(b))
        self.assertEqual(a.status, "Beta")
        # hack named only in the archive stem
        self.assertIn("Hack", self.t("Apidya_v1.1_Hack_by_Earok", "Apidya (Beta)").flags)
        self.assertEqual(self.t("Z_v1.0_BETA", "Z").status, "Beta")

    def test_rules_use_the_whdload_vocabulary(self) -> None:
        def rules(game: str, stem: str = "G_v1.0") -> set[str]:
            return tags.exclusion_rules(self.t(stem, game))
        self.assertEqual(rules("G (Beta)"), {"pre_release"})
        self.assertEqual(rules("G (Pre Release)"), {"pre_release"})
        self.assertEqual(rules("G (Preview)"), {"pre_release"})
        self.assertEqual(rules("G (Game Demo) (AGA)"), {"demo"})
        self.assertEqual(rules("G (Playable) (Game Demo)"), {"demo"})
        self.assertEqual(rules("G (Demo) (Beta)"), {"demo", "pre_release"})
        self.assertEqual(rules("G (Unreleased) (Files)"), {"unreleased"})
        for kept in ("G (Cover Disk)", "G (PD)", "G (Hack)", "G (Two Disk)", "G (Image)", "G (German)", "G (Alt)"):
            self.assertEqual(rules(kept), set(), kept)
        self.assertEqual(rules("G"), set())
        # TOSEC vocabulary is unchanged: "(demo-playable)" is a demo there, "Game Demo" is nothing
        self.assertEqual(tags.classify_token("(", "Game Demo"), None)
        self.assertEqual(tags.classify_token("(", "Game Demo", tags.STYLE_WHDLOAD), "demo")

    def test_rule_tokens_and_catalog(self) -> None:
        toks = tags.rule_tokens(tags.STYLE_WHDLOAD)
        self.assertEqual({r for r, v in toks.items() if v}, {"pre_release", "demo", "unreleased"})
        for rule, items in toks.items():
            for tok in items:
                self.assertEqual(tags.token_rule(tok, tags.STYLE_WHDLOAD), rule, tok)
        cat = [e for e in library.rule_catalog("whdload") if e.get("group") != "ratings"]
        ids = [e["id"] for e in cat]
        self.assertEqual(ids, ["pre_release", "demo", "unreleased", "latest_only", "best_variant", "languages"])
        for e in cat:
            self.assertIn("whdload", e["applies_to"])
        # the existing styles do not gain / lose entries because of it
        self.assertNotIn("whdload", [e for e in library.rule_catalog("tosec") if e["id"] == "cr"][0]["applies_to"])
        self.assertEqual(len([e for e in library.rule_catalog("tosec") if e.get("group") != "ratings"]), 9 + 6 + 6)

    def test_rom_tags_dispatch(self) -> None:
        rom = datfile.Rom("G_v1.0_AGA.lha", 1, "0", "", "", "G (AGA)", DAT, "G_v1.0_AGA")
        self.assertEqual(rom.tags.style, "whdload")
        other = datfile.Rom("G (USA).nes", 1, "0", "", "", "G (USA)", "Nintendo - Nintendo Entertainment System", "G (USA)")
        self.assertEqual(other.tags.style, "nointro")
        # the same text under the TOSEC DAT name is parsed as TOSEC (no cross-pollination)
        tosec_rom = datfile.Rom("G_v1.0_AGA.adf", 1, "0", "", "", "G", "Commodore Amiga - Games - [ADF]", "")
        self.assertEqual(tosec_rom.tags.style, "tosec")


# --------------------------------------------------------------------------- library on the real DAT


@unittest.skipUnless(REAL_DAT.is_file(), "real WHDLoad DAT not available")
class RealDatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dat = datfile.parse_dat(REAL_DAT, set_names=True)
        cls.platform = platforms.get_platform(PLAT)
        seen: dict[str, datfile.Rom] = {}
        for r in sorted(cls.dat.roms, key=lambda r: (r.set_name, r.game)):
            seen.setdefault(r.set_name, r)
        cls.items = [library.Item(i, DAT, r, tags.style_of_rom(r), Path("/x") / r.name, None)
                     for i, r in enumerate(seen.values())]

    def test_dat_shape(self) -> None:
        self.assertEqual((self.dat.name, self.dat.version, len(self.dat.roms)), (DAT, "2026-07-05", 4121))
        self.assertEqual(len(self.items), 4112)
        self.assertEqual({Path(r.name).suffix for r in self.dat.roms}, {".lha", ".lzx"})

    def test_every_name_parses(self) -> None:
        for r in self.dat.roms:
            t = r.tags
            self.assertTrue(t.title, r.name)
            self.assertEqual(t.style, "whdload")

    def test_default_numbers(self) -> None:
        prof = library.default_profile(self.platform)
        sel = library.select(self.items, prof, self.platform)
        acts = {}
        for d in sel.decisions.values():
            acts[d.action] = acts.get(d.action, 0) + 1
        self.assertEqual(acts, {"keep": 2763, "excluded": 924, "superseded": 425})
        self.assertEqual(sel.exclusion_counts()["exclusive"],
                         {"language": 711, "demo": 74, "pre_release": 137, "unreleased": 2})
        self.assertEqual(sel.vanish_summary()["titles"], 376)
        self.assertEqual(sel.vanish_summary()["by_reason"]["language"], 269)

    def test_idempotent_and_order_independent(self) -> None:
        prof = library.default_profile(self.platform)
        sel = library.select(self.items, prof, self.platform)
        kept = [i for i in self.items if sel.decisions[i.key].action == "keep"]
        again = library.select(kept, prof, self.platform)
        self.assertTrue(all(d.action == "keep" for d in again.decisions.values()))
        shuffled = list(self.items)
        random.Random(3).shuffle(shuffled)
        sel2 = library.select(shuffled, prof, self.platform)
        self.assertEqual({k: d.action for k, d in sel.decisions.items()},
                         {k: d.action for k, d in sel2.decisions.items()})
        for variant in (dataclasses.replace(prof, languages=("En", "De")), dataclasses.replace(prof, languages=()),
                        dataclasses.replace(prof, best_variant=False),
                        dataclasses.replace(prof, best_variant=False, latest_only=False, exclude=frozenset())):
            s = library.select(self.items, variant, self.platform)
            k = [i for i in self.items if s.decisions[i.key].action == "keep"]
            s2 = library.select(k, variant, self.platform)
            self.assertTrue(all(d.action == "keep" for d in s2.decisions.values()), variant)

    def test_everything_off_keeps_all(self) -> None:
        prof = dataclasses.replace(library.default_profile(self.platform), best_variant=False, latest_only=False,
                                   exclude=frozenset(), languages=())
        sel = library.select(self.items, prof, self.platform)
        self.assertEqual({d.action for d in sel.decisions.values()}, {"keep"})

    def test_one_per_game_never_merges_products(self) -> None:
        prof = library.default_profile(self.platform)
        sel = library.select(self.items, prof, self.platform)
        by_game: dict[tuple, list[library.Item]] = {}
        for it in self.items:
            by_game.setdefault(tags.identity_key(it.rom.tags), []).append(it)
        for key, group in by_game.items():
            kept = [i for i in group if sel.decisions[i.key].action == "keep"]
            self.assertLessEqual(len({i.rom.name for i in kept}), 1, key)
        # Two Disk / One Disk, Image / Files are separate games
        titled = [k for k in by_game if k[1] == "adidas championship football"]
        self.assertEqual(len(titled), 2)

    def test_chosen_variants(self) -> None:
        prof = dataclasses.replace(library.default_profile(self.platform), exclude=frozenset())
        sel = library.select(self.items, prof, self.platform)
        keep = {i.rom.set_name for i in self.items if sel.decisions[i.key].action == "keep"}
        self.assertIn("DarkSeed_v2.0_CD32", keep)             # CD32 > AGA > OCS
        self.assertNotIn("DarkSeed_v1.5_2141", keep)
        self.assertIn("Blade_v1.0_AGA_1801", keep)
        self.assertNotIn("Blade_v1.0_1801", keep)
        self.assertIn("TennisCup_v1.3a_1Mb_0737", keep)       # standard memory over 512KB
        self.assertNotIn("TennisCup_v1.3a_512Kb_0737", keep)
        self.assertIn("Predator2_v1.2_0274", keep)            # PAL over NTSC
        self.assertNotIn("Predator2_v1.2_NTSC", keep)


# --------------------------------------------------------------------------- scan / organise / kickstart


class FolderCase(DataDirCase):
    def setUp(self) -> None:
        super().setUp()
        rng = random.Random(5)
        self.root = self.work / "whd"
        self.root.mkdir()
        self.blobs = {
            "Alpha_v1.0.lha": ("Alpha", _blob(rng)),
            "Beta_v1.2_AGA.lha": ("Beta (AGA)", _blob(rng)),
            "Gamma_v1.0_De.lha": ("Gamma (German)", _blob(rng)),
            "Zed_v1.0_Files.lzx": ("Zed (Files)", _blob(rng)),
        }
        text = _dat_text([(g, n, b) for n, (g, b) in self.blobs.items()] + [("Missing Game", "Missing_v1.0.lha", b"zz")])
        folder = paths.whdload_dir()
        (folder / f"{DAT}.dat").write_text(text, encoding="utf-8")
        self.platform = platforms.get_platform(PLAT)
        self.dats, self.missing = platforms.load_platform_dats(self.platform)

    def scan(self) -> scanner.ScanResult:
        return scanner.scan(self.root, self.dats, layout="flat", use_cache=False,
                            protected_dirs=self.platform.protected_dirs)


class PlatformTest(FolderCase):
    def test_registry(self) -> None:
        p = self.platform
        self.assertEqual((p.source, p.layout, p.extensions, p.m3u_dats, p.kickstart_dat),
                         ("whdload", "flat", (".lha", ".lzx"), (), None))
        self.assertEqual((p.kickstart_folder, p.protected_dirs), ("Kickstarts", ("Kickstarts",)))
        self.assertTrue(platforms.has_kickstart(p))
        amiga = platforms.get_platform("Commodore Amiga")
        self.assertNotIn(DAT, amiga.dats)
        self.assertEqual((amiga.kickstart_folder, amiga.protected_dirs), ("", ()))
        self.assertEqual(self.missing, [])
        self.assertEqual(self.dats[0].roms[0].dat, DAT)


class ScanTest(FolderCase):
    def test_lha_is_hashed_whole_never_opened(self) -> None:
        for n, (_g, b) in self.blobs.items():
            (self.root / n).write_bytes(b)
        (self.root / "renamed.lha").write_bytes(self.blobs["Alpha_v1.0.lha"][1][:0] + b"unrelated")
        with mock.patch.object(zipfile_guard(), "ZipFile", side_effect=AssertionError("opened as zip")), \
                mock.patch.object(scanner.subprocess, "run", side_effect=AssertionError("7z used")), \
                mock.patch.object(scanner.subprocess, "Popen", side_effect=AssertionError("7z used")):
            res = self.scan()
        self.assertEqual(len(res.matched), 4)
        self.assertEqual([e.path.name for e in res.unmatched], ["renamed.lha"])
        for m in res.matched:
            self.assertIsNone(m.entry.member)
            self.assertEqual(m.entry.sha1, hashlib.sha1((self.root / m.entry.path.name).read_bytes()).hexdigest())
        self.assertNotIn(".lha", scanner.ZIP_EXTS | scanner.SEVENZIP_EXTS)
        self.assertNotIn(".lzx", scanner.ZIP_EXTS | scanner.SEVENZIP_EXTS)
        s = res.summary()
        self.assertEqual((s["have"], s["missing"], s["dat_total"], s["count_by"]), (4, 1, 5, "game"))

    def test_lha_that_is_a_valid_zip_is_still_one_file(self) -> None:
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("inner.bin", b"x" * 50)
        data = buf.getvalue()
        text = _dat_text([("Sneaky", "Sneaky_v1.0.lha", data)])
        (paths.whdload_dir() / f"{DAT}.dat").write_text(text, encoding="utf-8")
        dats, _ = platforms.load_platform_dats(self.platform)
        (self.root / "whatever.lha").write_bytes(data)
        res = scanner.scan(self.root, dats, layout="flat", use_cache=False)
        self.assertEqual([(m.entry.path.name, m.entry.member) for m in res.matched], [("whatever.lha", None)])

    def test_kickstarts_folder_is_not_scanned(self) -> None:
        ks = self.root / "Kickstarts"
        ks.mkdir()
        (ks / "kick.rom").write_bytes(b"k" * 100)
        (self.root / "KICKSTARTS_x.lha").write_bytes(b"u")
        res = self.scan()
        self.assertEqual([e.path.name for e in res.unmatched], ["KICKSTARTS_x.lha"])
        # without the protection (any other system) the same folder is scanned
        res2 = scanner.scan(self.root, self.dats, layout="flat", use_cache=False)
        self.assertEqual(sorted(e.path.name for e in res2.unmatched), ["KICKSTARTS_x.lha", "kick.rom"])
        self.assertTrue(folders.is_protected(("kickstarts", "a.rom")))
        self.assertFalse(folders.is_protected(("a.rom",)))
        self.assertNotIn("Kickstarts", folders.RESERVED_DIRS)
        self.assertNotIn("Kickstarts", folders.REASON_DIRS)


def zipfile_guard():
    import zipfile
    return zipfile


class OrganiseTest(FolderCase):
    def test_rename_to_dat_name_and_unmatched(self) -> None:
        sub = self.root / "old"
        sub.mkdir()
        (sub / "x1.lha").write_bytes(self.blobs["Alpha_v1.0.lha"][1])
        (self.root / "wrongname.lha").write_bytes(self.blobs["Beta_v1.2_AGA.lha"][1])
        (self.root / "Gamma_v1.0_De.lha").write_bytes(self.blobs["Gamma_v1.0_De.lha"][1])
        (self.root / "notes.txt").write_text("x")
        ks = self.root / "Kickstarts"
        ks.mkdir()
        (ks / "kick.rom").write_bytes(b"k")
        res = self.scan()
        ops = organiser.plan_renames(res, layout="flat")
        dst = {o.src.name: (o.dst.relative_to(self.root).as_posix(), o.status) for o in ops}
        self.assertEqual(dst["x1.lha"], ("Alpha_v1.0.lha", "move"))
        self.assertEqual(dst["wrongname.lha"], ("Beta_v1.2_AGA.lha", "move"))
        self.assertEqual(dst["Gamma_v1.0_De.lha"][1], "ok")
        self.assertEqual(dst["notes.txt"][0], "_unmatched/notes.txt")
        self.assertNotIn("kick.rom", dst)
        organiser.apply_renames(ops, self.root)
        self.assertTrue((self.root / "Alpha_v1.0.lha").is_file())
        self.assertTrue((ks / "kick.rom").is_file())

    def test_library_plan_flat_and_idempotent(self) -> None:
        for n, (_g, b) in self.blobs.items():
            (self.root / n).write_bytes(b)
        prof = library.default_profile(self.platform)
        res = self.scan()
        plan = organiser.plan_library(res, prof, layout="flat")
        by = {o.src.name: o for o in plan.ops}
        self.assertEqual(by["Gamma_v1.0_De.lha"].code, "excluded")      # German only, English profile
        self.assertTrue(by["Gamma_v1.0_De.lha"].dst.relative_to(self.root).parts[0] == "_excluded")
        organiser.apply_renames(plan.ops, self.root, playlists=plan.playlists)
        res2 = self.scan()
        plan2 = organiser.plan_library(res2, prof, layout="flat")
        self.assertTrue(all(o.status in ("ok", "skip") for o in plan2.ops))
        self.assertEqual(plan2.playlists, [])


class KickstartFolderTest(FolderCase):
    def setUp(self) -> None:
        super().setUp()
        self.ks = self.root / "Kickstarts"
        (self.ks / "sub").mkdir(parents=True)
        self.rom = b"R" * 3000
        self.md5 = hashlib.md5(self.rom).hexdigest()
        table = list(kickstart.PUAE_BIOS)
        table[3] = ("kick34005.A500", self.md5, "Kickstart v1.3 (test)")
        p = mock.patch.object(kickstart, "PUAE_BIOS", table)
        p.start()
        self.addCleanup(p.stop)
        (self.ks / "sub" / "my13.rom").write_bytes(self.rom)
        (self.ks / "other.bin").write_bytes(b"nope")
        (self.ks / ".hidden").write_bytes(self.rom)
        (self.ks / "huge.bin").write_bytes(b"h" * (kickstart.MAX_KICKSTART_SIZE + 5))
        self.dest = self.work / "bios"
        self.dest.mkdir()

    def test_plan_by_md5_unmatched_reported(self) -> None:
        ops = kickstart.plan_kickstarts_from_folder(self.ks, self.dest)
        st = {}
        for o in ops:
            st.setdefault(o.status, []).append(o)
        self.assertEqual([o.target.name for o in st["copy"]], ["kick34005.A500"])
        self.assertEqual(len(st["missing"]), 13)
        self.assertEqual(sorted(o.target.name for o in st["unmatched"]), ["huge.bin", "other.bin"])
        self.assertIn("too large", [o.reason for o in st["unmatched"] if o.target.name == "huge.bin"][0])

    def test_apply_copies_never_overwrites_never_moves(self) -> None:
        res = kickstart.apply_kickstarts(kickstart.plan_kickstarts_from_folder(self.ks, self.dest))
        self.assertEqual(res["copied"], 1)
        self.assertEqual((self.dest / "kick34005.A500").read_bytes(), self.rom)
        self.assertTrue((self.ks / "sub" / "my13.rom").is_file())            # copied, not moved
        again = kickstart.plan_kickstarts_from_folder(self.ks, self.dest)
        self.assertEqual([o.status for o in again if o.target.name == "kick34005.A500"], ["ok"])
        (self.dest / "kick34005.A500").write_bytes(b"different")
        c = kickstart.plan_kickstarts_from_folder(self.ks, self.dest)
        self.assertEqual([o.status for o in c if o.target.name == "kick34005.A500"], ["conflict"])
        kickstart.apply_kickstarts(c)
        self.assertEqual((self.dest / "kick34005.A500").read_bytes(), b"different")

    def test_missing_folder_is_all_missing(self) -> None:
        ops = kickstart.plan_kickstarts_from_folder(None, self.dest)
        self.assertEqual({o.status for o in ops}, {"missing"})

    def test_tosec_plan_unchanged_signature(self) -> None:
        # the DAT-based planner still exists with its old signature and statuses
        res = scanner.ScanResult(self.root, [], [], [], [], [], [], 0, {})
        ops = kickstart.plan_kickstarts(res, self.dest)
        self.assertEqual({o.status for o in ops}, {"missing"})


# --------------------------------------------------------------------------- server


class ServerCase(DataDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, path: str, body=None, expect=200):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.port}{path}", data=data,
                                     headers={"Content-Type": "application/json", "X-Romorg-Token": "t"})
        try:
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, expect)
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, expect, exc.read())
            return json.loads(exc.read() or b"{}") if False else {}

    def job(self) -> dict:
        import time
        end = time.time() + 30
        while time.time() < end:
            j = self.call("/api/job")
            if j and j["status"] != "running":
                return j
            time.sleep(0.05)
        raise AssertionError("job timeout")


class ServerWhdloadTest(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        rng = random.Random(9)
        self.root = self.work / "whd"
        self.root.mkdir()
        self.rom = b"K" * 2500
        table = list(kickstart.PUAE_BIOS)
        table[3] = ("kick34005.A500", hashlib.md5(self.rom).hexdigest(), "KS 1.3 (test)")
        p = mock.patch.object(kickstart, "PUAE_BIOS", table)
        p.start()
        self.addCleanup(p.stop)
        self.entries = [("Alpha", "Alpha_v1.0.lha", _blob(rng)), ("Beta (German)", "Beta_v1.0_De.lha", _blob(rng))]
        (paths.whdload_dir() / f"{DAT}.dat").write_text(_dat_text(self.entries), encoding="utf-8")
        for _g, n, b in self.entries:
            (self.root / n).write_bytes(b)
        (self.root / "Kickstarts").mkdir()
        (self.root / "Kickstarts" / "ks13.rom").write_bytes(self.rom)
        self.bios = self.work / "bios"
        self.bios.mkdir()

    def test_platform_listed_with_own_dat_and_kickstart_folder(self) -> None:
        rows = {p["name"]: p for p in self.call("/api/platforms")}
        w = rows[PLAT]
        self.assertEqual((w["source"], w["layout"], w["kickstart_folder"], w["has_kickstart"], w["kickstart_dat"]),
                         ("whdload", "flat", "Kickstarts", True, None))
        self.assertEqual([(d["name"], d["version"], d["present"]) for d in w["dats"]], [(DAT, "2026-07-05", True)])
        self.assertEqual(rows["Commodore Amiga"]["kickstart_folder"], "")
        self.assertFalse(rows["Commodore Amiga"]["dats"][0]["present"])      # TOSEC DATs are absent: no cross-match
        self.assertFalse(rows["Nintendo 64"]["has_kickstart"])

    def test_scan_games_and_library_profile(self) -> None:
        self.call("/api/folders", {"platform": PLAT, "path": str(self.root)})
        self.call("/api/scan", {"path": str(self.root), "platform": PLAT})
        self.assertEqual(self.job()["status"], "done")
        st = self.call("/api/status")["scan"]
        self.assertEqual((st["dat_names"], st["summary"]["have"], st["summary"]["missing"]), ([DAT], 2, 0))
        games = self.call(f"/api/scan/results?kind=games")
        self.assertEqual({g["name"]: g["have"] for g in games["items"]}, {"Alpha_v1.0": True, "Beta_v1.0_De": True})
        info = self.call(f"/api/library/profile?platform={urllib.request.quote(PLAT)}")
        self.assertEqual(info["style"], "whdload")
        self.assertEqual([e["id"] for e in info["catalog"] if e["kind"] == "exclude"], ["pre_release", "demo", "unreleased"])
        self.assertFalse(info["available"]["keep_flags"])
        self.assertFalse(info["available"]["complete_only"])
        self.assertIn("CD32", info["ranking"])
        self.assertEqual({r["code"] for r in info["available_languages"]}, {"En", "De"})
        plan = self.call("/api/library/plan", {"limit": 5})
        self.assertEqual((plan["reasons"]["kept"], plan["reasons"]["excluded"]), (1, 1))     # German excluded
        # profile persists under its own key
        self.call("/api/library/profile", {"platform": PLAT, "languages": ["En", "De"]})
        cfg = json.loads(paths.config_path().read_text())
        self.assertEqual(cfg["library"][PLAT]["languages"], ["En", "De"])
        self.assertNotIn("Commodore Amiga", cfg["library"])

    def test_kickstart_endpoints_scoped_by_platform(self) -> None:
        q = urllib.request.quote(PLAT)
        d = self.call(f"/api/kickstart/dirs?platform={q}")
        self.assertEqual((d["kickstart_folder"], d["kickstart_dat"], d["last"]), ("Kickstarts", None, None))
        # no folder chosen yet -> 409 with a hint
        self.call("/api/kickstart/plan", {"platform": PLAT, "dest": str(self.bios)}, expect=409)
        self.call("/api/folders", {"platform": PLAT, "path": str(self.root)})
        plan = self.call("/api/kickstart/plan", {"platform": PLAT, "dest": str(self.bios)})
        self.assertEqual((plan["source"], plan["counts"].get("copy"), plan["source_exists"]), ("folder", 1, True))
        self.assertTrue(plan["source_dir"].endswith("Kickstarts"))
        # the TOSEC Amiga needs its own scan (and uses its own DAT source)
        self.call("/api/kickstart/plan", {"platform": "Commodore Amiga", "dest": str(self.bios)}, expect=409)
        self.call("/api/kickstart/plan", {"platform": "Nintendo 64", "dest": str(self.bios)}, expect=409)
        self.call("/api/kickstart/apply", {"platform": PLAT, "dest": str(self.bios)})
        j = self.job()
        self.assertEqual((j["status"], j["result"]["copied"], j["result"]["platform"]), ("done", 1, PLAT))
        self.assertEqual((self.bios / "kick34005.A500").read_bytes(), self.rom)
        self.assertTrue((self.root / "Kickstarts" / "ks13.rom").is_file())
        st = self.call("/api/status")
        self.assertEqual(st["kickstart_dests"], {PLAT: str(self.bios.resolve())})
        self.assertNotIn("Commodore Amiga", st["kickstart_dests"])
        self.assertEqual(self.call(f"/api/kickstart/dirs?platform={q}")["last"], str(self.bios.resolve()))
        self.assertIsNone(self.call("/api/kickstart/dirs?platform=Commodore%20Amiga")["last"])

    def test_kickstart_dest_endpoint_saves_immediately(self) -> None:
        r = self.call("/api/kickstart/dest", {"platform": PLAT, "dest": str(self.bios)})
        self.assertEqual(r["platform"], PLAT)
        self.assertEqual(json.loads(paths.config_path().read_text())["kickstart_dests"], {PLAT: str(self.bios.resolve())})
        self.call("/api/kickstart/dest", {"platform": "Nintendo 64", "dest": str(self.bios)}, expect=409)
        self.call("/api/kickstart/dest", {"platform": PLAT, "dest": "relative"}, expect=400)

    def test_missing_kickstarts_folder_lists_everything_missing(self) -> None:
        import shutil
        shutil.rmtree(self.root / "Kickstarts")
        self.call("/api/folders", {"platform": PLAT, "path": str(self.root)})
        plan = self.call("/api/kickstart/plan", {"platform": PLAT, "dest": str(self.bios)})
        self.assertEqual((plan["source_exists"], set(plan["counts"])), (False, {"missing"}))

    def test_library_plan_ignores_kickstarts(self) -> None:
        self.call("/api/scan", {"path": str(self.root), "platform": PLAT})
        self.job()
        plan = self.call("/api/library/plan", {})
        self.assertFalse(any("Kickstarts" in (i.get("from") or "") for i in plan["items"]))


# --------------------------------------------------------------------------- config + folder persistence


class ConfigTest(DataDirCase):
    def test_legacy_kickstart_dest_migrates_to_amiga_only(self) -> None:
        paths.save_config({"kickstart_dest": "/x/bios", "folders": {"Nintendo 64": "/n"}})
        app = server.App(auto_update=False)
        self.assertEqual(server._kick_dest(app._config(), "Commodore Amiga"), "/x/bios")
        self.assertIsNone(server._kick_dest(app._config(), PLAT))
        app._config_update(last_platform="Nintendo 64")
        cfg = paths.load_config()
        self.assertNotIn("kickstart_dest", cfg)
        self.assertEqual(cfg["kickstart_dests"], {"Commodore Amiga": "/x/bios"})
        self.assertEqual(cfg["folders"], {"Nintendo 64": "/n"})
        self.assertEqual(app.status({}, None)["kickstart_dest"], "/x/bios")

    def test_concurrent_writers_never_lose_keys(self) -> None:
        app = server.App(auto_update=False)
        library_mod = library
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            try:
                for i in range(25):
                    if n % 3 == 0:
                        app._config_update(folder_for=(f"P{n}-{i}", f"/f/{n}/{i}"))
                    elif n % 3 == 1:
                        platform = platforms.get_platform(PLAT)
                        app._save_profile(platform, dataclasses.replace(library_mod.default_profile(platform),
                                                                      languages=("En",) if i % 2 else ()))
                    else:
                        app._config_update(kickstart_for=(f"K{n}-{i}", f"/k/{n}/{i}"), last_platform=f"P{n}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(9)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        cfg = paths.load_config()
        folders_ = cfg["folders"]
        self.assertEqual(len(folders_), 3 * 25)                  # nothing was overwritten by a stale snapshot
        self.assertEqual(len(cfg["kickstart_dests"]), 3 * 25)
        self.assertIn(PLAT, cfg["library"])

    def test_update_config_reads_latest_and_keeps_damaged_copy(self) -> None:
        paths.config_path().write_text("{not json")
        paths.update_config(lambda c: c.update(a=1))
        self.assertEqual(paths.load_config(), {"a": 1})
        self.assertEqual((paths.config_path().parent / "config.json.damaged").read_text(), "{not json")
        seen = []
        paths.update_config(lambda c: seen.append(dict(c)))
        self.assertEqual(seen, [{"a": 1}])

    def test_failed_write_is_reported_for_folders(self) -> None:
        app = server.App(auto_update=False)
        with mock.patch.object(paths, "_write_config", side_effect=OSError("disk full")):
            with self.assertRaises(server.ApiError) as cm:
                app.folders_save({}, {"platform": "Nintendo 64", "path": str(self.work)})
        self.assertEqual(cm.exception.status, 500)
        self.assertNotIn("folders", paths.load_config())


class FolderPersistenceTest(ServerCase):
    def test_folders_survive_a_restart_for_every_platform(self) -> None:
        dirs = {}
        for p in self.call("/api/platforms"):
            d = self.work / "roms" / p["name"].replace(" ", "_")
            d.mkdir(parents=True)
            dirs[p["name"]] = str(d.resolve())
            self.call("/api/folders", {"platform": p["name"], "path": str(d)})
        self.assertEqual(len(dirs), len(platforms.list_platforms()))
        # a profile save, a kickstart dest and a scan-time update later, nothing is forgotten
        self.call("/api/library/profile", {"platform": "Nintendo 64", "languages": ["En", "De"]})
        self.call("/api/platforms/options", {"platform": "Nintendo 64", "latest_only": False})
        self.call("/api/kickstart/dest", {"platform": "Commodore Amiga", "dest": str(self.work)})
        # "restart": a brand-new App/server on the same data dir
        self._stop()
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        rows = {p["name"]: p["folder"] for p in self.call("/api/platforms")}
        self.assertEqual(rows, dirs)
        self.assertEqual(self.call("/api/status")["folders"], dirs)

    def test_ui_saves_without_a_save_button(self) -> None:
        js = server.read_static("app.js").decode()
        self.assertNotIn('text: "Save"', js)
        for needle in ('addEventListener("change"', "function commitFolder", "dataset.folderState",
                       'dispatchEvent(new Event("change"))', "await commitFolder(p, true)"):
            self.assertIn(needle, js)


if __name__ == "__main__":
    unittest.main()
