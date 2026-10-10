"""End-to-end flow through the real modules with synthetic data.

platform DATs (+ an AMIX DAT that must be ignored) -> scan a messy platform folder
-> summary numbers -> plan -> apply (DAT folders + _unmatched/) -> rescan (all ok)
-> undo (byte-for-byte original tree) -> M3Us (relative paths, order, complete sets
only) -> re-organise -> M3Us inside DAT folders -> Kickstart plan/apply.

No-Intro consoles (SNES / N64 / NES, flat layout): scan -> per-game have/missing ->
plan (latest only) -> apply -> rescan (all ok) -> convert -> undo convert -> undo organise.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import random
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import quoteattr
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

from romorg import convert, library, m3u, organiser, paths, platforms, scanner

GAMES = "Commodore Amiga - Games - [ADF]"
WB = "Commodore Amiga - Operating Systems - Workbench"
KSD = "Commodore Amiga - Kickstart-Disks"
FW = "Commodore Amiga - Firmware"
AMIX = "Commodore Amiga - Operating Systems - AMIX"

DAT_FILES = {
    GAMES: "2025-01-30",
    WB: "2023-05-21",
    KSD: "2025-01-03",
    FW: "2025-01-03",
    AMIX: "2023-01-01",
}

_rng = random.Random(1234)


def blob(size: int = 2048) -> bytes:
    return bytes(_rng.getrandbits(8) for _ in range(size))


def rom_xml(name: str, data: bytes) -> str:
    return ('<rom name=%s size="%d" crc="%08x" md5="%s" sha1="%s"/>'
            % (quoteattr(name), len(data), zlib.crc32(data), hashlib.md5(data).hexdigest(),
               hashlib.sha1(data).hexdigest()))


def write_dat(directory: Path, dat_name: str, version: str, games: dict[str, list[tuple[str, bytes]]]) -> None:
    parts = ['<?xml version="1.0"?>',
             '<!DOCTYPE datafile PUBLIC "-//Logiqx//DTD ROM Management Datafile//EN" '
             '"http://www.logiqx.com/Dats/datafile.dtd">',
             "<datafile><header>",
             f"<name>{dat_name}</name><description>{dat_name} (TOSEC-v{version})</description>",
             f"<version>{version}</version></header>"]
    for game, roms in games.items():
        parts.append(f"<game name={quoteattr(game)}><description>{game}</description>")
        parts.extend(rom_xml(n, d) for n, d in roms)
        parts.append("</game>")
    parts.append("</datafile>")
    (directory / f"{dat_name} (TOSEC-v{version}_CM).dat").write_text("\n".join(parts), encoding="utf-8")


def snapshot(root: Path) -> dict[str, str]:
    """rel path -> sha1 (files) or "<dir>" (directories); hidden entries skipped."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        base = Path(dirpath)
        for d in dirnames:
            out[(base / d).relative_to(root).as_posix()] = "<dir>"
        for f in filenames:
            if not f.startswith("."):
                p = base / f
                out[p.relative_to(root).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


class IntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(base / "data")})
        env.start()
        self.addCleanup(env.stop)
        self.dats_dir = base / "dats"
        self.dats_dir.mkdir()
        self.root = base / "amiga"
        self.root.mkdir()
        self._build_dats()
        self._build_folder()
        self.platform = platforms.get_platform("Commodore Amiga")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------ fixtures

    def _build_dats(self) -> None:
        d = self.data = {
            "alpha": [blob() for _ in range(3)],
            "beta": [blob(), blob()],
            "gamma": blob(),          # same bytes under two TOSEC names
            "delta": blob(),          # zip-packed locally
            "eps": blob(),            # misnamed loose file
            "wb": [blob(), blob()],
            "ksd": blob(),            # never present locally
            "fw": blob(512),
            "amix": blob(),
        }
        self.alpha = [f"Alpha (1990)(Pub)(Disk {i} of 3).adf" for i in (1, 2, 3)]
        self.beta = [f"Beta (1991)(Pub)(Disk {i} of 2).adf" for i in (1, 2)]
        self.wb = [f"Workbench v1.3 (1988)(Commodore)(Disk {i} of 2).adf" for i in (1, 2)]
        self.gamma = ["Gamma (1992)(Pub).adf", "Gamma (1992)(Pub)[a].adf"]
        games = {n[:-4]: [(n, d["alpha"][i])] for i, n in enumerate(self.alpha)}
        games.update({n[:-4]: [(n, d["beta"][i])] for i, n in enumerate(self.beta)})
        games.update({n[:-4]: [(n, d["gamma"])] for n in self.gamma})
        games["Delta (1993)(Pub)"] = [("Delta (1993)(Pub).adf", d["delta"])]
        games["Epsilon (1994)(Pub)"] = [("Epsilon (1994)(Pub).adf", d["eps"])]
        write_dat(self.dats_dir, GAMES, DAT_FILES[GAMES], games)
        write_dat(self.dats_dir, WB, DAT_FILES[WB],
                  {n[:-4]: [(n, d["wb"][i])] for i, n in enumerate(self.wb)})
        write_dat(self.dats_dir, KSD, DAT_FILES[KSD],
                  {"Kickstart v1.3 (1987)(Commodore)": [("Kickstart v1.3 (1987)(Commodore).adf", d["ksd"])]})
        self.fw_name = "Kickstart v1.3 rev 34.5 (1987)(Commodore)(A500).rom"
        write_dat(self.dats_dir, FW, DAT_FILES[FW], {self.fw_name[:-4]: [(self.fw_name, d["fw"])]})
        write_dat(self.dats_dir, AMIX, DAT_FILES[AMIX],
                  {"AMIX (1990)(Commodore)": [("AMIX (1990)(Commodore).adf", d["amix"])]})

    def _put(self, rel: str, data: bytes) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def _build_folder(self) -> None:
        d = self.data
        self._put(self.alpha[0], d["alpha"][0])                              # misplaced (root)
        self._put(f"{GAMES}/{self.alpha[1]}", d["alpha"][1])                 # already in place
        self._put("stuff/a3.adf", d["alpha"][2])                             # misnamed, deeper
        self._put(f"sub/{self.beta[0]}", d["beta"][0])                       # disk 2 missing
        self._put("gamma.adf", d["gamma"])                                   # duplicate-hash rom
        self._put("copies/gamma copy.adf", d["gamma"])                       # 2nd TOSEC name
        self._put("copies/gamma again.adf", d["gamma"])                      # redundant 3rd copy
        (self.root / "zips").mkdir()
        with zipfile.ZipFile(self.root / "zips" / "whatever.zip", "w") as zf:  # zip-packed disk
            zf.writestr("delta.adf", d["delta"])
        self._put("deep/er/eps.adf", d["eps"])
        self._put("wb/disk1.adf", d["wb"][0])
        self._put("wb/disk2.adf", d["wb"][1])
        self._put("bios/ks13.rom", d["fw"])
        self._put("notes.txt", b"not a rom")
        self._put("deep/er/random.bin", blob(100))
        self._put("amix.adf", d["amix"])                                     # AMIX DAT is not loaded
        self._put(f"{GAMES}/junk.adf", blob(300))                            # unmatched in DAT folder
        self._put("mine.m3u", b"my own playlist\n")                          # user m3u: untouched
        self._put(".hidden", b"x")
        (self.root / "emptydir").mkdir()                                     # pre-existing empty dir

    # ------------------------------------------------------------------ helpers

    def _scan(self) -> scanner.ScanResult:
        dats, missing = platforms.load_platform_dats(self.platform, self.dats_dir)
        self.assertEqual(missing, [])
        self.assertEqual([x.name for x in dats], list(self.platform.dats))
        return scanner.scan(self.root, dats, use_cache=False)

    def _m3u_ops(self, result: scanner.ScanResult, labels: bool = False) -> list[m3u.M3UOp]:
        return m3u.plan_m3us(result, labels=labels, dats=self.platform.m3u_dats)

    # ------------------------------------------------------------------ the flow

    def test_full_flow(self) -> None:
        original = snapshot(self.root)

        # --- scan
        res = self._scan()
        s = res.summary()
        self.assertEqual(s["dat_total"], 9 + 2 + 1 + 1)
        self.assertEqual(s["have"], 8 + 2 + 0 + 1)
        self.assertEqual(s["missing"], 2)
        self.assertEqual(s["matched_files"], 12)
        self.assertEqual(s["unmatched_files"], 4)
        self.assertEqual(s["duplicates"], 2)  # 2nd + 3rd gamma add no new rom name
        self.assertEqual(s["correctly_placed"], 1)
        self.assertEqual(s["per_dat"][GAMES]["have"], 8)
        self.assertEqual(s["per_dat"][WB]["have"], 2)
        self.assertEqual(s["per_dat"][KSD]["missing"], 1)
        self.assertEqual(s["per_dat"][FW]["matched_files"], 1)
        self.assertNotIn(AMIX, s["per_dat"])
        self.assertEqual({r.name for r in res.missing}, {self.beta[1], "Kickstart v1.3 (1987)(Commodore).adf"})

        # --- plan
        ops = organiser.plan_renames(res)
        by_src = {op.src.relative_to(self.root).as_posix(): op for op in ops}
        rel = lambda op: op.dst.relative_to(self.root).as_posix()  # noqa: E731
        self.assertEqual(rel(by_src[self.alpha[0]]), f"{GAMES}/{self.alpha[0]}")
        self.assertEqual(by_src[f"{GAMES}/{self.alpha[1]}"].status, "ok")
        self.assertEqual(rel(by_src["stuff/a3.adf"]), f"{GAMES}/{self.alpha[2]}")
        self.assertEqual(rel(by_src["zips/whatever.zip"]), f"{GAMES}/Delta (1993)(Pub).zip")
        self.assertEqual(rel(by_src["deep/er/eps.adf"]), f"{GAMES}/Epsilon (1994)(Pub).adf")
        self.assertEqual(rel(by_src["wb/disk1.adf"]), f"{WB}/{self.wb[0]}")
        self.assertEqual(rel(by_src["bios/ks13.rom"]), f"{FW}/{self.fw_name}")
        self.assertEqual(rel(by_src["notes.txt"]), "_unmatched/notes.txt")
        self.assertEqual(rel(by_src["amix.adf"]), "_unmatched/amix.adf")
        self.assertEqual(rel(by_src[f"{GAMES}/junk.adf"]), f"_unmatched/{GAMES}/junk.adf")
        self.assertEqual(rel(by_src["deep/er/random.bin"]), "_unmatched/deep/er/random.bin")
        # Three identical files (two TOSEC names): the shortest path is kept, the spares are set aside.
        gamma_ops = {k: by_src[k] for k in ("gamma.adf", "copies/gamma copy.adf", "copies/gamma again.adf")}
        self.assertIn(rel(gamma_ops["gamma.adf"]), {f"{GAMES}/{n}" for n in self.gamma})
        for k in ("copies/gamma copy.adf", "copies/gamma again.adf"):
            self.assertEqual((gamma_ops[k].code, gamma_ops[k].keeper), ("duplicate", "gamma.adf"))
            self.assertEqual(rel(gamma_ops[k]), f"_duplicates/{k}")
        dup = self.root / "_duplicates" / "copies" / "gamma copy.adf"
        self.assertNotIn("mine.m3u", by_src)
        counts = organiser.plan_counts(ops)
        self.assertEqual(counts["move"], 15)
        self.assertEqual(counts["to_unmatched"], 4)
        self.assertEqual(counts["to_duplicates"], 2)

        # --- apply
        out = organiser.apply_renames(ops, self.root, dat_names=self.platform.dats)
        self.assertEqual(out["failed"], [])
        self.assertEqual(out["moved"], 15)
        self.assertTrue(Path(out["undo_log"]).is_file())
        for gone in ("stuff", "zips", "deep", "wb", "bios", "sub"):
            self.assertFalse((self.root / gone).exists(), gone)
        self.assertTrue((self.root / "emptydir").is_dir())
        self.assertTrue(dup.is_file())  # spare copies are set aside, never deleted
        self.assertTrue((self.root / "mine.m3u").is_file())
        top = {p.name for p in self.root.iterdir() if not p.name.startswith(".")}
        self.assertEqual(top, {GAMES, WB, FW, "_unmatched", "_duplicates", "emptydir", "mine.m3u"})

        # --- rescan: everything matched is in place (spare copies stay set aside)
        res2 = self._scan()
        s2 = res2.summary()
        self.assertEqual(s2["have"], s["have"])
        self.assertEqual(s2["correctly_placed"], 10)
        self.assertEqual((s2["to_rename"], s2["duplicates"], s2["duplicates_set_aside"]), (0, 0, 2))
        ops2 = organiser.plan_renames(res2)
        self.assertEqual({op.status for op in ops2}, {"ok"})
        self.assertEqual(organiser.plan_counts(ops2)["to_unmatched"], 0)

        # --- undo: original tree back, byte for byte, incl. removed/created dirs
        logs = organiser.list_undo_logs(self.root)
        self.assertEqual(len(logs), 1)
        u = organiser.undo(logs[0])
        self.assertEqual(u["failed"], [])
        self.assertEqual(u["restored"], 15)
        self.assertEqual(snapshot(self.root), original)
        self.assertEqual(organiser.list_undo_logs(self.root), [])

        # --- m3u on the original layout: relative paths, disk order, complete sets only
        res3 = self._scan()
        mops = self._m3u_ops(res3)
        by_name = {op.path.name: op for op in mops}
        alpha = by_name["Alpha (1990)(Pub).m3u"]
        self.assertEqual(alpha.status, "write")
        self.assertEqual(alpha.path.parent, self.root)  # next to disk 1
        self.assertEqual(alpha.lines, [m3u.M3U_MARKER, self.alpha[0], f"{GAMES}/{self.alpha[1]}", "stuff/a3.adf"])
        wb = by_name["Workbench v1.3 (1988)(Commodore).m3u"]
        self.assertEqual(wb.path.parent, self.root / "wb")
        self.assertEqual(wb.lines[1:], ["disk1.adf", "disk2.adf"])
        beta = by_name["Beta (1991)(Pub).m3u"]
        self.assertEqual(beta.status, "incomplete")
        self.assertIn("2", beta.reason)
        self.assertEqual(len(mops), 3)
        w = m3u.write_m3us(mops)
        self.assertEqual((w["written"], w["failed"]), (2, []))
        self.assertFalse(beta.path.exists())
        text = alpha.path.read_bytes().decode("utf-8")
        self.assertTrue(text.startswith(m3u.M3U_MARKER + "\n") and "\r" not in text)
        self.assertEqual({op.status for op in self._m3u_ops(self._scan()) if op.status != "incomplete"}, {"ok"})
        # Labels variant (PUAE "path|Label").
        labelled = {op.path.name: op for op in self._m3u_ops(res3, labels=True)}
        self.assertEqual(labelled["Alpha (1990)(Pub).m3u"].lines[1], f"{self.alpha[0]}|Disk 1")
        for op in mops:
            if op.status == "write":
                op.path.unlink()

        # --- organise again, then M3Us live in the DAT folders with bare filenames
        res4 = self._scan()
        out4 = organiser.apply_renames(organiser.plan_renames(res4), self.root)
        self.assertEqual(out4["failed"], [])
        res5 = self._scan()
        mops5 = {op.path.name: op for op in self._m3u_ops(res5)}
        a5 = mops5["Alpha (1990)(Pub).m3u"]
        self.assertEqual(a5.path.parent, self.root / GAMES)
        self.assertEqual(a5.lines[1:], self.alpha)
        self.assertEqual(mops5["Workbench v1.3 (1988)(Commodore).m3u"].path.parent, self.root / WB)
        self.assertEqual(m3u.write_m3us(mops5.values())["written"], 2)



# ============================================================================ No-Intro consoles

SNES = "Nintendo - Super Nintendo Entertainment System"
N64 = "Nintendo - Nintendo 64"
NES = "Nintendo - Nintendo Entertainment System"


def cmp_rom(name: str, data: bytes) -> str:
    return ('rom ( name "%s" size %d crc %08X md5 %s sha1 %s )'
            % (name, len(data), zlib.crc32(data), hashlib.md5(data).hexdigest().upper(),
               hashlib.sha1(data).hexdigest().upper()))


def write_cmp_dat(directory: Path, dat_name: str, games: list[tuple[str, str, bytes]]) -> None:
    """libretro-style clrmamepro DAT: one ``game (`` block per rom (alternates share the game name)."""
    parts = ["clrmamepro (", f'\tname "{dat_name}"', f'\tdescription "{dat_name}"',
             '\tversion "2026.08.01"', '\thomepage "No-Intro"', ")", ""]
    for game, rom_name, data in games:
        parts += ["game (", f'\tname "{game}"', f"\t{cmp_rom(rom_name, data)}", ")", ""]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{dat_name}.dat").write_text("\n".join(parts), encoding="utf-8")


def z64(size: int = 4096) -> bytes:
    return b"\x80\x37\x12\x40" + blob(size - 4)


def to_v64(data: bytes) -> bytes:
    out = bytearray(data)
    out[0::2], out[1::2] = data[1::2], data[0::2]
    return bytes(out)


def to_n64(data: bytes) -> bytes:
    return b"".join(data[i:i + 4][::-1] for i in range(0, len(data), 4))


def ines(prg: bytes, flags: bytes = b"\x01\x01") -> bytes:
    return b"NES\x1a" + flags + b"\x00" * 10 + prg


PLAIN_SYSTEMS = (
    # platform, the DAT's rom extension, how the DAT's rom is zipped / misnamed by a user
    ("Nintendo Game Boy", ".gb"), ("Nintendo Game Boy Color", ".gbc"), ("Nintendo DS", ".nds"),
    ("Sega Mega Drive - Genesis", ".md"), ("Sega Master System", ".sms"), ("Sega Game Gear", ".gg"),
    ("Sega 32X", ".32x"), ("Atari Lynx", ".lnx"),
)


class PlainSystemsIntegrationTest(unittest.TestCase):
    """The systems whose DAT hashes are those of the files as they are: scan -> plan -> apply -> settled -> undo."""

    def test_flow_of_every_plain_system(self) -> None:
        for name, ext in PLAIN_SYSTEMS:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                with mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(base / "data")}):
                    self.flow(name, ext, base)

    def flow(self, name: str, ext: str, base: Path) -> None:
        plat = platforms.get_platform(name)
        dat = plat.dats[0]
        root = base / "roms"
        (root / "sub").mkdir(parents=True)
        (root / "zips").mkdir()
        d = {k: blob(3000) for k in ("alpha", "bravo", "charlie", "delta")}
        games = [("Alpha (USA)", f"Alpha (USA){ext}", d["alpha"]),
                 ("Bravo (Europe)", f"Bravo (Europe){ext}", d["bravo"]),
                 ("Charlie (Japan)", f"Charlie (Japan){ext}", d["charlie"]),
                 ("Delta (USA)", f"Delta (USA){ext}", d["delta"])]
        if name == "Atari Lynx":     # the libretro DAT lists a game's headered .lnx and raw .lyx as separate roms
            raw = blob(2000)
            games.append(("Alpha (USA)", "Alpha (USA).lyx", raw))
            (root / "alpha raw.lyx").write_bytes(raw)
        write_cmp_dat(paths.nointro_dir(), dat, games)
        (root / f"Alpha (USA){ext}").write_bytes(d["alpha"])                  # right name, right place
        (root / "sub" / f"bravo{ext}").write_bytes(d["bravo"])                # misnamed, in a subfolder
        with zipfile.ZipFile(root / "zips" / "c.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"c{ext}", d["charlie"])                              # zipped
        (root / f"mystery{ext}").write_bytes(blob(1500))                       # in no DAT
        before = snapshot(root)

        dats, missing = platforms.load_platform_dats(plat)
        self.assertEqual((missing, [x.name for x in dats]), ([], [dat]))
        res = scanner.scan(root, dats, use_cache=False, alt_hashes=plat.alt_hashes, layout=plat.layout)
        s = res.summary()
        self.assertEqual((s["games_total"], s["games_have"], s["games_missing"]), (4, 3, 1), name)
        self.assertEqual({r.name for r in res.missing}, {f"Delta (USA){ext}"})
        self.assertEqual(snapshot(root), before)                               # a scan changes nothing

        ops = organiser.plan_renames(res, latest_only=True)
        dst = {op.src.relative_to(root).as_posix(): op.dst.relative_to(root).as_posix() for op in ops}
        self.assertEqual(dst[f"Alpha (USA){ext}"], f"Alpha (USA){ext}")
        self.assertEqual(dst[f"sub/bravo{ext}"], f"Bravo (Europe){ext}")
        self.assertEqual(dst["zips/c.zip"], "Charlie (Japan).zip")
        self.assertEqual(dst[f"mystery{ext}"], f"_unmatched/mystery{ext}")
        if name == "Atari Lynx":
            self.assertEqual(dst["alpha raw.lyx"].rsplit(".", 1)[-1], "lyx")  # an honest extension, not a rename
        out = organiser.apply_renames(ops, root, dat_names=[dat])
        self.assertEqual(out["failed"], [])
        res2 = scanner.scan(root, dats, use_cache=False, alt_hashes=plat.alt_hashes, layout=plat.layout)
        again = [o for o in organiser.plan_renames(res2, latest_only=True) if o.status not in ("ok", "skip")]
        self.assertEqual(again, [], name)
        organiser.undo(Path(out["undo_log"]))
        self.assertEqual({k: v for k, v in snapshot(root).items() if not k.startswith("_")}, before)


class NoIntroIntegrationTest(unittest.TestCase):
    """Console flow: scan -> per-game counts -> plan (latest only) -> apply -> rescan ->
    convert -> undo convert -> undo organise (byte-exact)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(base / "data")})
        env.start()
        self.addCleanup(env.stop)
        self.ni_dir = paths.nointro_dir()
        self.root = base / "roms"
        self.root.mkdir()

    def _put(self, rel: str, data: bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def _zip(self, rel: str, member: str, data: bytes) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(member, data)

    def _scan(self, platform_name: str) -> scanner.ScanResult:
        plat = platforms.get_platform(platform_name)
        dats, missing = platforms.load_platform_dats(plat)  # default dirs -> paths.nointro_dir()
        self.assertEqual(missing, [])
        self.assertEqual([d.name for d in dats], list(plat.dats))
        return scanner.scan(self.root, dats, use_cache=False, alt_hashes=plat.alt_hashes, layout=plat.layout)

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def _games(self, res: scanner.ScanResult) -> dict[str, bool]:
        return {g["name"]: g["have"] for g in scanner.game_status(res)}

    def _assert_settled(self, platform_name: str, latest_only: bool = True) -> scanner.ScanResult:
        res = self._scan(platform_name)
        ops = organiser.plan_renames(res, latest_only=latest_only)
        moving = [(self._rel(op.src), op.status, op.reason) for op in ops if op.status not in ("ok", "skip")]
        self.assertEqual(moving, [])
        return res

    # ------------------------------------------------------------------ SNES

    def test_snes_flow(self) -> None:
        d = {k: blob(2048) for k in ("alpha", "bravo", "charlie", "delta", "echo0", "echo1", "echo2",
                                     "beta", "fox")}
        write_cmp_dat(self.ni_dir, SNES, [
            ("Alpha (USA)", "Alpha (USA).sfc", d["alpha"]),
            ("Bravo (Europe)", "Bravo (Europe).sfc", d["bravo"]),
            ("Charlie (Japan)", "Charlie (Japan).sfc", d["charlie"]),
            ("Delta (USA)", "Delta (USA).sfc", d["delta"]),
            ("Echo (USA)", "Echo (USA).sfc", d["echo0"]),
            ("Echo (USA) (Rev 1)", "Echo (USA) (Rev 1).sfc", d["echo1"]),
            ("Echo (USA) (Rev 2)", "Echo (USA) (Rev 2).sfc", d["echo2"]),
            ("Echo (USA) (Beta)", "Echo (USA) (Beta).sfc", d["beta"]),
            ("Foxtrot (Europe)", "Foxtrot (Europe).sfc", d["fox"]),
        ])
        header = b"\x00" * 512
        self._put("Alpha (USA).sfc", d["alpha"])                       # correct
        self._put("sub/bravo.sfc", d["bravo"])                          # misnamed, in a subfolder
        self._put("charlie.smc", header + d["charlie"])                 # copier header
        self._zip("zips/delta.zip", "d.sfc", d["delta"])                # zipped
        self._put("echo r1.sfc", d["echo1"])                            # Rev 1 (older)
        self._put("Echo (USA) (Rev 2).sfc", d["echo2"])                 # Rev 2 (newest present)
        self._put("echo beta.sfc", d["beta"])                           # Beta never superseded
        self._put("random.sfc", blob(3000))                             # unmatched
        self._put("media/box.png", b"png")                              # frontend media: left alone
        original = snapshot(self.root)

        # --- scan: per-game have/missing, headered match, nothing modified
        res = self._scan("Super Nintendo Entertainment System")
        s = res.summary()
        self.assertEqual(s["count_by"], "game")
        self.assertEqual((s["games_total"], s["games_have"], s["games_missing"]), (9, 7, 2))
        self.assertEqual((s["dat_total"], s["have"], s["missing"]), (9, 7, 2))
        self.assertEqual(s["matched_via"]["headerless"], 1)
        self.assertEqual(s["convertible"], 1)
        games = self._games(res)
        self.assertFalse(games["Echo (USA)"])
        self.assertFalse(games["Foxtrot (Europe)"])
        self.assertTrue(games["Charlie (Japan)"])
        self.assertEqual({r.name for r in res.missing}, {"Echo (USA).sfc", "Foxtrot (Europe).sfc"})
        self.assertEqual(snapshot(self.root), original)
        rows = scanner.to_json(res)
        self.assertEqual(rows["layout"], "flat")

        # --- plan with latest only: flat root, honest .smc, zip named after the set, Rev 1 superseded
        ops = organiser.plan_renames(res, latest_only=True)
        by_src = {self._rel(op.src): op for op in ops}
        dst = lambda k: self._rel(by_src[k].dst)  # noqa: E731
        self.assertEqual(by_src["Alpha (USA).sfc"].status, "ok")
        self.assertEqual(dst("sub/bravo.sfc"), "Bravo (Europe).sfc")
        self.assertEqual(dst("charlie.smc"), "Charlie (Japan).smc")
        self.assertEqual(dst("zips/delta.zip"), "Delta (USA).zip")
        self.assertEqual(dst("echo r1.sfc"), "_superseded/echo r1.sfc")  # keeps its own name
        self.assertEqual(by_src["echo r1.sfc"].superseded_by, "Echo (USA) (Rev 2)")
        self.assertEqual(by_src["Echo (USA) (Rev 2).sfc"].status, "ok")
        self.assertEqual(dst("echo beta.sfc"), "Echo (USA) (Beta).sfc")
        self.assertEqual(dst("random.sfc"), "_unmatched/random.sfc")
        self.assertEqual(by_src["media/box.png"].status, "skip")
        counts = organiser.plan_counts(ops)
        self.assertEqual(counts["to_superseded"], 1)
        self.assertEqual(counts["move"], 6)

        # --- apply + rescan: everything settled
        out = organiser.apply_renames(ops, self.root, dat_names=[SNES])
        self.assertEqual(out["failed"], [])
        self.assertEqual(out["moved"], 6)
        org_log = Path(out["undo_log"])
        for gone in ("sub", "zips"):
            self.assertFalse((self.root / gone).exists(), gone)
        res2 = self._assert_settled("Super Nintendo Entertainment System")
        self.assertEqual(res2.summary()["games_have"], 7)
        # Latest-only off: the superseded Rev 1 would go back to the root (canonical rule).
        back = {self._rel(op.src): self._rel(op.dst) for op in organiser.plan_renames(res2) if op.status == "move"}
        self.assertEqual(back, {"_superseded/echo r1.sfc": "Echo (USA) (Rev 1).sfc"})

        # --- convert the headered file: clean .sfc, original kept
        cops = convert.plan_conversions(res2, latest_only=True)
        self.assertEqual([(self._rel(op.src), op.status) for op in cops], [("Charlie (Japan).smc", "convert")])
        self.assertEqual(self._rel(cops[0].dst), "Charlie (Japan).sfc")
        self.assertEqual(self._rel(cops[0].original_dst), "_converted_originals/Charlie (Japan).smc")
        c = convert.apply_conversions(cops, self.root)
        self.assertEqual((c["converted"], c["failed"], c["error"]), (1, [], None))
        self.assertEqual((self.root / "Charlie (Japan).sfc").read_bytes(), d["charlie"])
        self.assertEqual((self.root / "_converted_originals/Charlie (Japan).smc").read_bytes(),
                         header + d["charlie"])
        res3 = self._assert_settled("Super Nintendo Entertainment System")
        self.assertEqual(res3.summary()["convertible"], 0)
        # the kept original is "in place", not a duplicate / alternate match to act on
        s3 = res3.summary()
        self.assertEqual((s3["matched_via"]["headerless"], s3["converted_originals"], s3["duplicates"]), (0, 1, 0))
        # every file in place except the superseded Rev 1 (in _superseded/ only with latest-only)
        self.assertEqual((s3["to_rename"], s3["correctly_placed"]), (0, s3["matched_files"] - 1))
        self.assertEqual(convert.plan_conversions(res3), [])

        # --- undo convert, then undo organise -> byte-exact original tree
        u1 = organiser.undo(Path(c["undo_log"]))
        self.assertEqual(u1["failed"], [])
        self.assertEqual(u1["created_removed"], 1)
        self.assertFalse((self.root / "Charlie (Japan).sfc").exists())
        self.assertTrue((self.root / "Charlie (Japan).smc").is_file())
        u2 = organiser.undo(org_log)
        self.assertEqual(u2["failed"], [])
        self.assertEqual(snapshot(self.root), original)
        self.assertEqual(organiser.list_undo_logs(self.root), [])

    # ------------------------------------------------------------------ N64

    def test_n64_flow(self) -> None:
        d = {k: z64() for k in ("golf", "hotel", "india", "kilo", "juliet")}
        write_cmp_dat(self.ni_dir, N64, [
            ("Golf (USA)", "Golf (USA).z64", d["golf"]),
            ("Hotel (Japan)", "Hotel (Japan).z64", d["hotel"]),
            ("Hotel (Japan)", "Hotel (Japan).v64", to_v64(d["hotel"])),   # the DAT's own .v64 alternate
            ("India (Europe)", "India (Europe).z64", d["india"]),
            ("Juliet (USA)", "Juliet (USA).z64", d["juliet"]),
            ("Kilo (USA)", "Kilo (USA).z64", d["kilo"]),
        ])
        self._put("golf.v64", to_v64(d["golf"]))                    # byte-swapped
        self._put("n64/hotel.v64", to_v64(d["hotel"]))              # raw match of the DAT's .v64
        self._put("india.n64", to_n64(d["india"]))                  # word-swapped
        self._zip("kilo.zip", "k.v64", to_v64(d["kilo"]))           # byte-swapped inside a zip
        self._put("notes.txt", b"hello")
        original = snapshot(self.root)

        res = self._scan("Nintendo 64")
        s = res.summary()
        self.assertEqual((s["games_total"], s["games_have"], s["games_missing"]), (5, 4, 1))
        self.assertEqual(s["matched_via"], {"raw": 1, "headerless": 0, "byteswapped": 3})
        self.assertEqual(s["convertible"], 4)  # incl. hotel.v64: raw match of the DAT's .v64 -> its .z64
        self.assertEqual([r.name for r in res.missing], ["Juliet (USA).z64"])
        via = {self._rel(m.entry.path): (m.matched_via, m.byte_order) for m in res.matched}
        self.assertEqual(via["golf.v64"], ("byteswapped", "v64"))
        self.assertEqual(via["india.n64"], ("byteswapped", "n64"))
        self.assertEqual(via["n64/hotel.v64"], ("raw", ""))
        self.assertEqual(snapshot(self.root), original)

        ops = organiser.plan_renames(res, latest_only=True)
        dst = {self._rel(op.src): self._rel(op.dst) for op in ops}
        self.assertEqual(dst["golf.v64"], "Golf (USA).v64")            # extension stays honest
        self.assertEqual(dst["n64/hotel.v64"], "Hotel (Japan).v64")
        self.assertEqual(dst["india.n64"], "India (Europe).n64")
        self.assertEqual(dst["kilo.zip"], "Kilo (USA).zip")
        self.assertEqual(dst["notes.txt"], "_unmatched/notes.txt")
        out = organiser.apply_renames(ops, self.root, dat_names=[N64])
        self.assertEqual(out["failed"], [])
        org_log = Path(out["undo_log"])
        res2 = self._assert_settled("Nintendo 64")

        cops = {self._rel(op.src): op for op in convert.plan_conversions(res2)}
        self.assertEqual(set(cops), {"Golf (USA).v64", "Hotel (Japan).v64", "India (Europe).n64", "Kilo (USA).zip"})
        self.assertEqual((cops["Hotel (Japan).v64"].via, cops["Hotel (Japan).v64"].transform,
                          self._rel(cops["Hotel (Japan).v64"].dst)), ("raw", "swap16", "Hotel (Japan).z64"))
        self.assertEqual({op.status for op in cops.values()}, {"convert"})
        self.assertEqual(cops["Golf (USA).v64"].transform, "swap16")
        self.assertEqual(cops["India (Europe).n64"].transform, "swap32")
        self.assertEqual(self._rel(cops["Kilo (USA).zip"].dst), "Kilo (USA).zip")
        self.assertEqual(self._rel(cops["Kilo (USA).zip"].original_dst),
                         "_converted_originals/Kilo (USA).zip")
        c = convert.apply_conversions(cops.values(), self.root)
        self.assertEqual((c["converted"], c["failed"], c["error"]), (4, [], None))
        self.assertEqual((self.root / "Golf (USA).z64").read_bytes(), d["golf"])
        self.assertEqual((self.root / "Hotel (Japan).z64").read_bytes(), d["hotel"])
        self.assertEqual((self.root / "India (Europe).z64").read_bytes(), d["india"])
        with zipfile.ZipFile(self.root / "Kilo (USA).zip") as zf:
            self.assertEqual(zf.namelist(), ["Kilo (USA).z64"])
            self.assertEqual(zf.read("Kilo (USA).z64"), d["kilo"])
        res3 = self._assert_settled("Nintendo 64")
        self.assertEqual(res3.summary()["games_have"], 4)
        self.assertEqual(res3.summary()["convertible"], 0)

        self.assertEqual(organiser.undo(Path(c["undo_log"]))["created_removed"], 4)
        self.assertEqual(organiser.undo(org_log)["failed"], [])
        self.assertEqual(snapshot(self.root), original)

    # ------------------------------------------------------------------ NES

    def test_nes_flow(self) -> None:
        prg = {k: blob(2048) for k in ("lima", "mike", "nov", "oscar")}
        write_cmp_dat(self.ni_dir, NES, [
            (g, f"{g}{ext}", ines(prg[k]) if ext == ".nes" else prg[k])
            for k, g in (("lima", "Lima (USA)"), ("mike", "Mike (Europe)"),
                         ("nov", "November (Japan)"), ("oscar", "Oscar (USA)"))
            for ext in (".nes", ".unh")
        ])
        self._put("lima.nes", ines(prg["lima"]))                    # raw .nes
        self._put("copies/lima.unh", prg["lima"])                   # raw .unh of the same game
        self._put("Mike (Europe).unh", prg["mike"])                 # correct already
        self._put("november.nes", ines(prg["nov"], b"\x02\x00"))    # different iNES header
        original = snapshot(self.root)

        res = self._scan("Nintendo Entertainment System")
        s = res.summary()
        self.assertEqual((s["games_total"], s["games_have"], s["games_missing"]), (4, 3, 1))
        self.assertEqual(s["duplicates"], 0)  # .nes + .unh of one game are two forms, not duplicates
        self.assertEqual(s["matched_via"]["headerless"], 1)
        self.assertEqual(s["convertible"], 0)  # NES headerless matches are never converted
        self.assertEqual([r.name for r in res.missing], ["Oscar (USA).nes"])
        lima = next(g for g in scanner.game_status(res) if g["name"] == "Lima (USA)")
        self.assertTrue(lima["have"])
        self.assertEqual(sorted(lima["files"]), ["copies/lima.unh", "lima.nes"])

        ops = organiser.plan_renames(res, latest_only=True)
        dst = {self._rel(op.src): self._rel(op.dst) for op in ops}
        self.assertEqual(dst["lima.nes"], "Lima (USA).nes")
        self.assertEqual(dst["copies/lima.unh"], "Lima (USA).unh")
        self.assertEqual(dst["Mike (Europe).unh"], "Mike (Europe).unh")
        self.assertEqual(dst["november.nes"], "November (Japan).nes")
        out = organiser.apply_renames(ops, self.root, dat_names=[NES])
        self.assertEqual(out["failed"], [])
        self._assert_settled("Nintendo Entertainment System")
        self.assertEqual(convert.plan_conversions(self._scan("Nintendo Entertainment System")), [])
        self.assertEqual(organiser.undo(Path(out["undo_log"]))["failed"], [])
        self.assertEqual(snapshot(self.root), original)


class LibraryIntegrationTest(unittest.TestCase):
    """End-to-end "Build library" (Amendment 6) on a synthetic Amiga folder with real modules."""

    ABC = ["ABC Monday Night Football v1.1 (1991)(Data East)(US)(Disk 1 of 3)[cr SR].adf",
           "ABC Monday Night Football (1990)(Data East)(US)(Disk 2 of 3).adf",
           "ABC Monday Night Football (1990)(Data East)(US)(Disk 3 of 3).adf"]
    ABC_PRE = "ABC Monday Night Football (1990)(Data East)(US)(pre-release)(Disk 2 of 3).adf"
    ABC_M3U = "ABC Monday Night Football v1.1 (1991)(Data East)(US)[cr SR].m3u"
    HERO = ["Hero v1.0 (1991)(Pub)[cr X].adf", "Hero v1.1 (1992)(Pub).adf", "Hero v1.2 (1993)(Pub)[b].adf",
            "Hero (1990)(Pub)(demo-playable).adf"]
    WB = ["Workbench v1.2 (1986)(Commodore)(Disk 1 of 2).adf", "Workbench v1.2 (1986)(Commodore)(Disk 2 of 2).adf",
          "Workbench v1.3 (1988)(Commodore)(Disk 1 of 2).adf", "Workbench v1.3 (1988)(Commodore)(Disk 2 of 2).adf",
          "Workbench v1.3 (1988)(Commodore)(Disk 2 of 2)[m hack].adf"]

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(base / "data")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.dats_dir = base / "dats"
        self.dats_dir.mkdir()
        self.root = base / "amiga"
        self.root.mkdir()
        self.platform = platforms.get_platform("Commodore Amiga")
        self.blobs: dict[str, bytes] = {}

        def data(name: str) -> bytes:
            return self.blobs.setdefault(name, blob(1024))

        games = [*self.ABC, self.ABC_PRE, *self.HERO]
        write_dat(self.dats_dir, GAMES, DAT_FILES[GAMES], {n[:-4]: [(n, data(n))] for n in games})
        write_dat(self.dats_dir, WB, DAT_FILES[WB], {n[:-4]: [(n, data(n))] for n in self.WB})
        ks = ["Kickstart v1.2 (1986)(Commodore)(Disk 1 of 1).adf", "Kickstart v1.3 (1987)(Commodore)(Disk 1 of 1).adf"]
        write_dat(self.dats_dir, KSD, DAT_FILES[KSD], {n[:-4]: [(n, data(n))] for n in ks})
        fw = "Kickstart v1.3 rev 34.5 (1987)(Commodore)(A500)[!].rom"
        write_dat(self.dats_dir, FW, DAT_FILES[FW], {fw[:-4]: [(fw, data(fw))]})
        self.names = {"abc": self.ABC, "pre": [self.ABC_PRE], "hero": self.HERO, "wb": self.WB, "ks": ks, "fw": [fw]}
        for i, n in enumerate(self.ABC):
            self._put(f"abc/disk{i + 1}.adf", self.blobs[n])
        self._put("abc/pre.adf", self.blobs[self.ABC_PRE])
        for i, n in enumerate(self.HERO):
            self._put(f"hero/h{i}.adf", self.blobs[n])
        for i, n in enumerate(self.WB):
            self._put(f"wb/w{i}.adf", self.blobs[n])
        for i, n in enumerate(ks):
            self._put(f"ks/k{i}.adf", self.blobs[n])
        self._put("bios/kick.rom", self.blobs[fw])
        self._put("hero/copy of h1.adf", self.blobs[self.HERO[1]])          # a spare copy
        self._put("mine.m3u", b"my own playlist\n")
        self._put("notes.txt", b"hello")
        self.original = snapshot(self.root)

    def _put(self, rel: str, data: bytes) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def _plan(self):
        dats, missing = platforms.load_platform_dats(self.platform, self.dats_dir)
        self.assertEqual(missing, [])
        res = scanner.scan(self.root, dats, use_cache=False)
        profile = library.default_profile(self.platform)
        return res, organiser.plan_library(res, profile, missing_dats=missing)

    def test_build_library_end_to_end(self) -> None:
        res, plan = self._plan()
        dst = {op.src.relative_to(self.root).as_posix(): op.dst.relative_to(self.root).as_posix()
               for op in plan.ops if op.kind != "m3u"}
        code = {op.src.relative_to(self.root).as_posix(): op.code for op in plan.ops if op.kind != "m3u"}
        # ABC: per-slot playlist, v1.1 disk 1 + the (1990) disks 2/3; pre-release excluded
        for i, n in enumerate(self.ABC):
            self.assertEqual(dst[f"abc/disk{i + 1}.adf"], f"{GAMES}/{n}")
        self.assertEqual(dst["abc/pre.adf"], f"_excluded/abc/pre.adf")
        self.assertIn((self.ABC_M3U, "write"), [(p.path.name, p.status) for p in plan.playlists])
        # Hero: cracked v1.0 beats uncracked v1.1; [b] v1.2 and the demo are excluded; the rest superseded
        self.assertEqual(dst["hero/h0.adf"], f"{GAMES}/{self.HERO[0]}")
        self.assertEqual((code["hero/h1.adf"], code["hero/h2.adf"], code["hero/h3.adf"]),
                         ("superseded", "excluded", "excluded"))
        self.assertEqual(code["hero/copy of h1.adf"], "duplicate")
        # Workbench: every version kept (no latest/best rule), the [m] variant excluded, two playlists
        self.assertEqual([code[f"wb/w{i}.adf"] for i in range(5)], ["", "", "", "", "excluded"])
        self.assertEqual(sorted(p.path.name for p in plan.playlists),
                         sorted([self.ABC_M3U, "Workbench v1.2 (1986)(Commodore).m3u",
                                 "Workbench v1.3 (1988)(Commodore).m3u"]))
        # Kickstart disks + firmware: nothing is superseded, both Kickstarts kept
        self.assertEqual([code["ks/k0.adf"], code["ks/k1.adf"], code["bios/kick.rom"]], ["", "", ""])
        self.assertEqual(dst["notes.txt"], "_unmatched/notes.txt")
        self.assertEqual(res.summary()["duplicates"], organiser.plan_counts(plan.ops)["to_duplicates"])
        rc = organiser.reason_counts(plan)
        self.assertEqual((rc["excluded"], rc["superseded"], rc["duplicates"], rc["incomplete"], rc["playlists_write"]),
                         (4, 1, 1, 0, 3))

        out = organiser.apply_renames(plan.ops, self.root, dat_names=self.platform.dats, playlists=plan.playlists)
        self.assertEqual(out["failed"], [], out)
        self.assertEqual(out["playlists_written"], 3)
        self.assertTrue((self.root / GAMES / self.ABC_M3U).is_file())
        self.assertEqual((self.root / "mine.m3u").read_bytes(), b"my own playlist\n")
        built = snapshot(self.root)

        # re-running Build library on the built library: an empty plan
        res2, plan2 = self._plan()
        self.assertEqual({op.status for op in plan2.ops}, {"ok"}, [(o.src.name, o.status) for o in plan2.ops
                                                                  if o.status != "ok"])
        self.assertEqual({p.status for p in plan2.playlists}, {"ok"})
        rc2 = organiser.reason_counts(plan2)
        self.assertEqual({k: v for k, v in rc2.items() if k not in ("kept", "playlists_ok")},
                         {k: 0 for k in rc2 if k not in ("kept", "playlists_ok")})
        self.assertEqual(res2.summary()["duplicates"], 0)
        out2 = organiser.apply_renames(plan2.ops, self.root, playlists=plan2.playlists)
        self.assertEqual((out2["moved"], out2["playlists_written"], out2["undo_log"]), (0, 0, None))
        self.assertEqual(snapshot(self.root), built)

        # one undo: the playlists it created are gone and every file is back
        u = organiser.undo(Path(out["undo_log"]))
        self.assertEqual((u["failed"], u["created_removed"]), ([], 3))
        self.assertEqual(snapshot(self.root), self.original)

    def test_interrupted_build_then_undo_then_build_again(self) -> None:
        _, plan = self._plan()
        steps = sum(1 for op in plan.ops if op.status in organiser.MOVE_STATUSES or op.status == "delete")
        for stop_after in (3, steps, steps + 1):          # mid-moves, before the playlists, mid-playlists
            calls: list[int] = []

            def cancel() -> bool:
                calls.append(1)
                return len(calls) > stop_after

            out = organiser.apply_renames(plan.ops, self.root, playlists=plan.playlists, cancel=cancel)
            self.assertTrue(out["cancelled"], stop_after)
            self.assertEqual(out["failed"], [])
            if stop_after > steps:
                self.assertEqual(out["playlists_written"], 1)
            u = organiser.undo(Path(out["undo_log"]))
            self.assertEqual(u["failed"], [], u)
            self.assertEqual(snapshot(self.root), self.original, stop_after)
        _, again = self._plan()
        self.assertEqual([(o.src, o.dst, o.status) for o in again.ops], [(o.src, o.dst, o.status) for o in plan.ops])
        out = organiser.apply_renames(again.ops, self.root, playlists=again.playlists)
        self.assertEqual((out["failed"], out["playlists_written"]), ([], 3))


class BorrowIntegrationTest(unittest.TestCase):
    """Amendment 12 end to end: disk 2 only exists as the German edition; Build library completes the set."""

    D = ["Foo (1991)(Pub)(Disk 1 of 3)[cr X].adf", "Foo (1991)(Pub)(DE)(Disk 2 of 3).adf",
         "Foo (1991)(Pub)(Disk 3 of 3).adf"]
    OTHER = "Foo (1991)(Pub)(DE)(Disk 1 of 3).adf"        # the German edition's own, unneeded disk 1
    M3U = "Foo (1991)(Pub)[cr X].m3u"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(base / "data")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.dats_dir = base / "dats"
        self.dats_dir.mkdir()
        self.root = base / "amiga"
        self.root.mkdir()
        self.platform = platforms.get_platform("Commodore Amiga")
        self.blobs = {n: blob(1024) for n in [*self.D, self.OTHER]}
        write_dat(self.dats_dir, GAMES, DAT_FILES[GAMES], {n[:-4]: [(n, self.blobs[n])] for n in self.blobs})
        for dat in (WB, KSD, FW):
            write_dat(self.dats_dir, dat, DAT_FILES[dat], {})
        for i, n in enumerate([*self.D, self.OTHER]):
            (self.root / f"d{i}.adf").write_bytes(self.blobs[n])
        self.original = snapshot(self.root)

    def plan(self, **changes):
        dats, missing = platforms.load_platform_dats(self.platform, self.dats_dir)
        res = scanner.scan(self.root, dats, use_cache=False)
        profile = dataclasses.replace(library.default_profile(self.platform), **changes)
        return organiser.plan_library(res, profile, missing_dats=missing)

    def test_build_rebuild_empty_undo(self) -> None:
        plan = self.plan()
        rel = {op.src.name: (op.dst.relative_to(self.root).as_posix(), op.code) for op in plan.ops if op.kind != "m3u"}
        self.assertEqual(rel["d0.adf"][0], f"{GAMES}/{self.D[0]}")
        self.assertEqual(rel["d1.adf"], (f"{GAMES}/{self.D[1]}", ""))          # borrowed: kept, not _excluded
        self.assertEqual(rel["d3.adf"], (f"_excluded/d3.adf", "excluded"))      # the other edition's spare disk
        [pl] = plan.playlists
        self.assertEqual((pl.path.name, pl.status), (self.M3U, "write"))
        self.assertEqual([ln.split("|")[0] for ln in pl.lines[1:]], [self.D[0], self.D[1], self.D[2]])
        self.assertEqual(pl.notes, [f"disk 2 borrowed from the (DE) edition ({self.D[1][:-4]})"])
        row = [op for op in plan.ops if op.src.name == "d1.adf"][0]
        self.assertIn("borrowed as disk 2", row.reason)
        rc = organiser.reason_counts(plan)
        self.assertEqual((rc["borrowed_sets"], rc["borrowed_disks"], rc["incomplete"], rc["excluded"]), (1, 1, 0, 1))

        out = organiser.apply_renames(plan.ops, self.root, dat_names=self.platform.dats, playlists=plan.playlists)
        self.assertEqual((out["failed"], out["playlists_written"]), ([], 1))
        built = snapshot(self.root)
        text = (self.root / GAMES / self.M3U).read_text()
        self.assertEqual(text.count(".adf"), 3)

        plan2 = self.plan()                                  # re-running Build library: nothing to do
        self.assertEqual({op.status for op in plan2.ops}, {"ok"})
        self.assertEqual({p.status for p in plan2.playlists}, {"ok"})
        self.assertEqual(organiser.reason_counts(plan2)["borrowed_sets"], 1)
        out2 = organiser.apply_renames(plan2.ops, self.root, playlists=plan2.playlists)
        self.assertEqual((out2["moved"], out2["playlists_written"], out2["undo_log"]), (0, 0, None))
        self.assertEqual(snapshot(self.root), built)

        u = organiser.undo(Path(out["undo_log"]))
        self.assertEqual(u["failed"], [])
        self.assertEqual(snapshot(self.root), self.original)

    def test_option_off_leaves_the_set_incomplete(self) -> None:
        plan = self.plan(borrow_other_editions=False)
        self.assertEqual(plan.playlists, [])
        codes = {op.src.name: op.code for op in plan.ops if op.kind != "m3u"}
        self.assertEqual((codes["d0.adf"], codes["d1.adf"], codes["d2.adf"]), ("incomplete", "excluded", "incomplete"))
        self.assertEqual(organiser.reason_counts(plan)["borrowed_sets"], 0)



def _rels(plan, root: Path) -> dict[str, tuple[str, str]]:
    """src rel -> (dst rel, reason code) for the file ops of a library plan."""
    return {op.src.relative_to(root).as_posix(): (op.dst.relative_to(root).as_posix(), op.code or "")
            for op in plan.ops if op.kind != "m3u"}


class LibraryScenarioTest(unittest.TestCase):
    """Build library on richer synthetic roots: Amiga (versions, ABC pattern, zips, firmware) and No-Intro."""

    SAGA = ["Saga v1.0 (1990)(Pub)(Disk 1 of 3).adf", "Saga (1990)(Pub)(Disk 2 of 3).adf",
            "Saga (1990)(Pub)(Disk 3 of 3).adf", "Saga v1.1 (1991)(Pub)(Disk 1 of 3)[cr SR].adf"]
    SAGA_M3U = "Saga v1.1 (1991)(Pub)[cr SR].m3u"
    SAGA_PRE = "Saga v1.2 (1992)(Pub)(pre-release)(Disk 1 of 3).adf"
    LONELY = ["Lonely (1991)(Pub)(Disk 1 of 2).adf", "Lonely (1991)(Pub)(Disk 2 of 2).adf"]
    FRAGILE = ["Fragile (1991)(Pub)(Disk 1 of 2).adf", "Fragile (1991)(Pub)(Disk 2 of 2)[b corrupt file].adf"]
    SOLO = "Solo v1.0 (1991)(Pub).adf"
    DEMO = "Showoff (1992)(Pub)(demo-playable).adf"
    KS = ["Kickstart v1.3 rev 34.5 (1987)(Commodore)(A500)[!].rom",
          "Kickstart v2.04 rev 37.175 (1991)(Commodore)(A500+)[!].rom",
          "Kickstart v1.2 rev 33.180 (1986)(Commodore)(A500)[b].rom"]

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(base / "data")})
        env.start()
        self.addCleanup(env.stop)
        self.dats_dir = base / "dats"
        self.dats_dir.mkdir()
        self.root = base / "amiga"
        self.root.mkdir()
        self.platform = platforms.get_platform("Commodore Amiga")
        self.blobs: dict[str, bytes] = {}
        self.base = base

    def _data(self, name: str) -> bytes:
        return self.blobs.setdefault(name, blob(1024))

    def _put(self, rel: str, data: bytes) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def _amiga(self) -> None:
        games = [*self.SAGA, self.SAGA_PRE, *self.LONELY, *self.FRAGILE, self.SOLO, self.DEMO]
        write_dat(self.dats_dir, GAMES, DAT_FILES[GAMES], {n[:-4]: [(n, self._data(n))] for n in games})
        write_dat(self.dats_dir, FW, DAT_FILES[FW], {n[:-4]: [(n, self._data(n))] for n in self.KS})
        write_dat(self.dats_dir, WB, DAT_FILES[WB], {})
        write_dat(self.dats_dir, KSD, DAT_FILES[KSD], {})
        d = self._data
        # Saga: v1.0 complete, v1.1 [cr] has only disk 1 locally (disks 2/3 come from the 1990 release)
        for i, n in enumerate(self.SAGA):
            self._put(f"saga/{i}.adf", d(n))
        self._put("saga/pre.adf", d(self.SAGA_PRE))
        self._put("lonely/a.adf", d(self.LONELY[0]))                      # disk 2 missing
        self._put("fragile/a.adf", d(self.FRAGILE[0]))
        self._put("fragile/b.adf", d(self.FRAGILE[1]))                    # bad dump -> excluded
        self._put("solo.adf", d(self.SOLO))
        self._put("copies/solo again.adf", d(self.SOLO))                   # loose duplicate
        with zipfile.ZipFile(self.root / "solo.zip", "w") as zf:           # duplicate inside an archive
            zf.writestr("x.adf", d(self.SOLO))
        self._put("demo.adf", d(self.DEMO))
        for i, n in enumerate(self.KS):
            self._put(f"bios/k{i}.rom", d(n))
        self._put("mine.m3u", b"my own playlist\n")
        self.original = snapshot(self.root)

    def _plan(self, profile=None):
        dats, missing = platforms.load_platform_dats(self.platform, self.dats_dir)
        self.assertEqual(missing, [])
        res = scanner.scan(self.root, dats, use_cache=False)
        # per-region "latest" semantics: one_per_game is covered by its own tests
        profile = profile or dataclasses.replace(library.default_profile(self.platform), one_per_game=False,
                                                          languages=())
        return res, organiser.plan_library(res, profile, missing_dats=missing)

    def _apply(self, plan) -> dict:
        out = organiser.apply_renames(plan.ops, self.root, dat_names=self.platform.dats, playlists=plan.playlists)
        self.assertEqual(out["failed"], [], out)
        return out

    def _assert_empty(self, plan) -> None:
        self.assertEqual([(o.src.name, o.status) for o in plan.ops if o.status != "ok"], [])
        self.assertLessEqual({p.status for p in plan.playlists}, {"ok"})

    def test_amiga_build_rebuild_undo_and_rule_toggle(self) -> None:
        self._amiga()
        res, plan = self._plan()
        m = _rels(plan, self.root)
        # Saga: ONE set kept - v1.1 [cr SR] disk 1 + the 1990 disks 2/3; v1.0 disk 1 and v1.2 pre-release out
        kept = [f"saga/{i}.adf" for i in (1, 2, 3)]
        for k in kept:
            self.assertEqual(m[k][1], "", k)
        self.assertEqual(m["saga/0.adf"], ("_superseded/saga/0.adf", "superseded"))
        self.assertEqual(m["saga/pre.adf"][1], "excluded")
        self.assertEqual(plan.selection is not None and len([s for s in plan.selection.sets
                                                              if s.name.startswith("Saga")]), 1)
        pl = {p.path.name: p for p in plan.playlists}
        self.assertEqual(sorted(pl), sorted([self.SAGA_M3U]))
        self.assertEqual([x.split("|")[0].rsplit("/", 1)[-1] for x in pl[self.SAGA_M3U].lines[1:]],
                         [self.SAGA[3], self.SAGA[1], self.SAGA[2]])
        # incomplete 2-disk set; bad-dump disk excluded and its partner incomplete
        self.assertEqual(m["lonely/a.adf"][1], "incomplete")
        self.assertEqual(m["fragile/b.adf"][1], "excluded")
        self.assertEqual(m["fragile/a.adf"][1], "incomplete")
        self.assertTrue(m["lonely/a.adf"][0].startswith("_incomplete/"))
        # demo excluded; duplicates (loose + zip): exactly one kept, the others in _duplicates
        self.assertEqual(m["demo.adf"][1], "excluded")
        solo = [k for k in ("solo.adf", "copies/solo again.adf", "solo.zip") if m[k][1] != "duplicate"]
        self.assertEqual(solo, ["solo.adf"])                           # loose + shortest path wins
        for k in ("copies/solo again.adf", "solo.zip"):
            self.assertEqual(m[k][0], f"_duplicates/{k}")
        self.assertEqual(res.summary()["duplicates"], organiser.plan_counts(plan.ops)["to_duplicates"])
        # kickstarts: all kept except the [b]
        self.assertEqual([m[f"bios/k{i}.rom"][1] for i in range(3)], ["", "", "excluded"])
        self.assertEqual(m["bios/k2.rom"][0], "_excluded/bios/k2.rom")
        self.assertNotIn("mine.m3u", m)

        out = self._apply(plan)
        self.assertEqual(out["playlists_written"], 1)
        built = snapshot(self.root)
        reasons = {p.split("/")[0] for p in built if p.split("/")[0] in organiser.REASON_DIRS}
        self.assertTrue({"_excluded", "_superseded", "_incomplete", "_duplicates"} <= reasons, reasons)
        self.assertTrue((self.root / GAMES / self.SAGA_M3U).is_file())

        # rebuild -> EMPTY plan
        res2, plan2 = self._plan()
        self._assert_empty(plan2)
        self.assertEqual(res2.summary()["duplicates"], 0)
        again = organiser.apply_renames(plan2.ops, self.root, playlists=plan2.playlists)
        self.assertEqual((again["moved"], again["playlists_written"]), (0, 0))
        self.assertEqual(snapshot(self.root), built)

        # rule off -> the demo returns to its canonical place on the next build (and stays there)
        prof = library.default_profile(self.platform)
        prof = library.LibraryProfile(exclude=prof.exclude - {"demo"}, latest_only=prof.latest_only,
                                      best_variant=prof.best_variant, complete_only=prof.complete_only)
        _, plan3 = self._plan(prof)
        m3 = _rels(plan3, self.root)
        back = m3[f"_excluded/demo.adf"]
        self.assertEqual(back[0], f"{GAMES}/{self.DEMO}")
        self._apply(plan3)
        self.assertTrue((self.root / GAMES / self.DEMO).is_file())
        _, plan4 = self._plan(prof)
        self._assert_empty(plan4)

        # undo both builds -> the original tree, byte for byte, playlists gone
        for log in organiser.list_undo_logs(self.root):
            self.assertEqual(organiser.undo(log)["failed"], [])
        self.assertEqual(snapshot(self.root), self.original)

    def test_amiga_all_sets_complete_have_exactly_one_per_game(self) -> None:
        self._amiga()
        _, plan = self._plan()
        names = [s.name for s in plan.selection.sets]
        self.assertEqual(len([n for n in names if n.startswith("Saga")]), 1)
        self.assertFalse([n for n in names if n.startswith(("Lonely", "Fragile"))])

    def _nointro_root(self) -> dict[str, bytes]:
        ni = paths.nointro_dir()
        d = {k: blob(2048) for k in ("a", "b1", "b2", "e1", "e2", "j", "beta", "proto", "demo", "bad", "unl")}
        write_cmp_dat(ni, SNES, [
            ("Ace (USA)", "Ace (USA).sfc", d["a"]),
            ("Bolt (USA)", "Bolt (USA).sfc", d["b1"]),
            ("Bolt (USA) (Rev 1)", "Bolt (USA) (Rev 1).sfc", d["b2"]),
            ("Bolt (Europe)", "Bolt (Europe).sfc", d["e1"]),
            ("Bolt (Europe) (Rev 1)", "Bolt (Europe) (Rev 1).sfc", d["e2"]),
            ("Bolt (Japan)", "Bolt (Japan).sfc", d["j"]),
            ("Ace (USA) (Beta)", "Ace (USA) (Beta).sfc", d["beta"]),
            ("Ace (USA) (Proto)", "Ace (USA) (Proto).sfc", d["proto"]),
            ("Ace (USA) (Demo)", "Ace (USA) (Demo).sfc", d["demo"]),
            ("Dud (USA) [b]", "Dud (USA) [b].sfc", d["bad"]),
            ("Ace (USA) (Unl)", "Ace (USA) (Unl).sfc", d["unl"]),
        ])
        self.platform = platforms.get_platform("Super Nintendo Entertainment System")
        for k, rel in (("a", "ace.sfc"), ("b1", "bolt0.sfc"), ("b2", "sub/bolt1.sfc"), ("e1", "bolt e0.sfc"),
                       ("e2", "bolt e1.sfc"), ("j", "bolt j.sfc"), ("beta", "x/beta.sfc"), ("proto", "proto.sfc"),
                       ("demo", "demo.sfc"), ("bad", "bad.sfc"), ("unl", "unl.sfc")):
            self._put(rel, d[k])
        self._put("copies/ace copy.sfc", d["a"])                              # duplicate
        with zipfile.ZipFile(self.root / "ace.zip", "w") as zf:               # duplicate in an archive
            zf.writestr("a.sfc", d["a"])
        self.original = snapshot(self.root)
        return d

    def _ni_plan(self, profile=None):
        dats, missing = platforms.load_platform_dats(self.platform)
        self.assertEqual(missing, [])
        res = scanner.scan(self.root, dats, use_cache=False, alt_hashes=self.platform.alt_hashes,
                           layout=self.platform.layout)
        # per-region "latest" semantics: one_per_game is covered by its own tests
        profile = profile or dataclasses.replace(library.default_profile(self.platform), one_per_game=False,
                                                          languages=())
        return res, organiser.plan_library(res, profile, missing_dats=missing)

    def test_nointro_build_rebuild_undo_and_rule_toggle(self) -> None:
        self._nointro_root()
        res, plan = self._ni_plan()
        m = _rels(plan, self.root)
        code = lambda k: m[k][1]  # noqa: E731
        self.assertEqual(m["ace.sfc"], ("Ace (USA).sfc", ""))
        self.assertEqual(code("x/beta.sfc"), "excluded")
        self.assertEqual(code("proto.sfc"), "excluded")
        self.assertEqual(code("demo.sfc"), "excluded")
        self.assertEqual(code("bad.sfc"), "excluded")
        self.assertEqual(m["unl.sfc"][0], "Ace (USA) (Unl).sfc")             # (Unl) is kept
        # latest Rev PER REGION: USA Rev 1 and Europe Rev 1 win, never compared across regions
        self.assertEqual(code("bolt0.sfc"), "superseded")
        self.assertEqual(code("bolt e0.sfc"), "superseded")
        self.assertEqual(m["sub/bolt1.sfc"][0], "Bolt (USA) (Rev 1).sfc")
        self.assertEqual(m["bolt e1.sfc"][0], "Bolt (Europe) (Rev 1).sfc")
        self.assertEqual(m["bolt j.sfc"][0], "Bolt (Japan).sfc")
        dup = [k for k in ("ace.sfc", "copies/ace copy.sfc", "ace.zip") if code(k) != "duplicate"]
        self.assertEqual(dup, ["ace.sfc"])
        self.assertEqual(res.summary()["duplicates"], organiser.plan_counts(plan.ops)["to_duplicates"])
        self.assertEqual(plan.playlists, [])

        out = organiser.apply_renames(plan.ops, self.root, dat_names=self.platform.dats)
        self.assertEqual(out["failed"], [], out)
        built = snapshot(self.root)
        _, plan2 = self._ni_plan()
        self._assert_empty(plan2)
        self.assertEqual(organiser.apply_renames(plan2.ops, self.root)["moved"], 0)
        self.assertEqual(snapshot(self.root), built)

        # rule off (beta) -> it comes back, canonical name, next build
        prof = library.default_profile(self.platform)
        prof = dataclasses.replace(prof, exclude=prof.exclude - {"pre_release"}, one_per_game=False, languages=())
        _, plan3 = self._ni_plan(prof)
        back = _rels(plan3, self.root)["_excluded/x/beta.sfc"]
        self.assertEqual(back[0], "Ace (USA) (Beta).sfc")
        out3 = organiser.apply_renames(plan3.ops, self.root)
        self.assertEqual(out3["failed"], [])
        _, plan4 = self._ni_plan(prof)
        self._assert_empty(plan4)

        for log in organiser.list_undo_logs(self.root):
            self.assertEqual(organiser.undo(log)["failed"], [])
        self.assertEqual(snapshot(self.root), self.original)


if __name__ == "__main__":
    unittest.main()
