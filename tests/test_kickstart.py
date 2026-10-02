"""Tests for romorg.kickstart (synthetic kickstarts; optional real Firmware DAT check)."""

from __future__ import annotations

import hashlib
import os
import random
import re
import subprocess
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from romorg import kickstart, scanner
from romorg.datfile import DatFile, Rom, parse_dat
from romorg.kickstart import PUAE_BIOS, apply_kickstarts, detect_system_dirs, plan_kickstarts

FW = "Commodore Amiga - Firmware"
GAMES = "Commodore Amiga - Games - [ADF]"
REAL_FW_DAT = Path("/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/"
                   "cfc5ea37-4261-428c-9422-29acd65cac97/scratchpad/dats/TOSEC/"
                   "Commodore Amiga - Firmware (TOSEC-v2025-01-03_CM).dat")


def _rom(name: str, data: bytes, dat: str) -> Rom:
    return Rom(name, len(data), f"{zlib.crc32(data) & 0xFFFFFFFF:08x}", hashlib.md5(data).hexdigest(),
               hashlib.sha1(data).hexdigest(), name.rsplit(".", 1)[0], dat)


class TableTests(unittest.TestCase):
    def test_table_shape(self) -> None:
        self.assertEqual(len(PUAE_BIOS), 15)
        for filename, md5, desc in PUAE_BIOS:
            self.assertRegex(filename, r"^kick\d{5}\.")
            self.assertRegex(md5, r"^[0-9a-f]{32}$")
            self.assertTrue(desc)
        self.assertEqual(len({m for _, m, _ in PUAE_BIOS}), 15)
        names = [f for f, _, _ in PUAE_BIOS]
        for expected in ("kick34005.A500", "kick40068.A1200", "kick40063.A600", "kick40060.CD32.ext",
                         "kick34005.CDTV", "kick40068.A4000"):
            self.assertIn(expected, names)

    @unittest.skipUnless(REAL_FW_DAT.is_file(), "real Firmware DAT not available")
    def test_md5s_against_real_firmware_dat(self) -> None:
        dat = parse_dat(REAL_FW_DAT)
        md5s = {r.md5 for r in dat.roms}
        present = [f for f, m, _ in PUAE_BIOS if m in md5s]
        absent = [(f, d) for f, m, d in PUAE_BIOS if m not in md5s]
        print(f"\n  PUAE bios in TOSEC Firmware DAT: {len(present)}/{len(PUAE_BIOS)}", end=" ")
        self.assertEqual(len(present), 14)
        self.assertEqual(absent, [("kick40060.CD32", "CD32 KS + extended v3.1 rev 40.060")])


class PlanApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "amiga"
        self.dest = base / "bios" / "system"   # does not exist yet
        self.root.mkdir()
        rnd = random.Random(7)
        self.k13 = rnd.randbytes(262144)
        self.k31 = rnd.randbytes(524288)
        self.cd32 = rnd.randbytes(1024)
        self.cd32_combined = rnd.randbytes(2048)
        self.ext = rnd.randbytes(512)
        md5 = lambda b: hashlib.md5(b).hexdigest()  # noqa: E731
        self.table = [
            ("kick34005.A500", md5(self.k13), "KS 1.3"),
            ("kick40068.A1200", md5(self.k31), "KS 3.1"),
            ("kick40060.CD32", md5(self.cd32), "CD32 KS"),
            ("kick40060.CD32.ext", md5(self.ext), "CD32 ext"),
            ("kick40060.CD32", md5(self.cd32_combined), "CD32 KS + ext"),
            ("kick99999.X", "0" * 32, "never found"),
        ]
        self.fw = DatFile(FW, "", "", [
            _rom("Kickstart v1.3 (1987)(Commodore).rom", self.k13, FW),
            _rom("Kickstart v3.1 (1993)(Commodore)(A1200).rom", self.k31, FW),
            _rom("CD32 Extended-ROM (1993)(Commodore).rom", self.ext, FW),
        ])
        self.games = DatFile(GAMES, "", "", [_rom("CD32 thing (1993).adf", self.cd32, GAMES)])
        self.patch = mock.patch.object(kickstart, "PUAE_BIOS", self.table)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def _scan(self, exe: str | None = None) -> scanner.ScanResult:
        with mock.patch.object(scanner, "find_7z", return_value=exe):
            return scanner.scan(self.root, [self.games, self.fw], use_cache=False)

    def _write(self, rel: str, data: bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_plan_apply_loose_and_zip(self) -> None:
        self._write(f"{FW}/Kickstart v1.3 (1987)(Commodore).rom", self.k13)
        with zipfile.ZipFile(self._write("deep/ks31.zip", b""), "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("whatever.rom", self.k31)
        self._write("cd32.adf", self.cd32)  # matched via the Games DAT md5
        ops = {op.target.name: op for op in plan_kickstarts(self._scan(), self.dest)}
        self.assertEqual(list(ops), ["kick34005.A500", "kick40068.A1200", "kick40060.CD32",
                                     "kick40060.CD32.ext", "kick99999.X"])  # one op per filename
        self.assertEqual(ops["kick34005.A500"].status, "copy")
        self.assertEqual(ops["kick34005.A500"].rom_name, "Kickstart v1.3 (1987)(Commodore).rom")
        self.assertEqual(ops["kick40068.A1200"].source.member, "whatever.rom")
        self.assertEqual(ops["kick40060.CD32"].status, "copy")
        self.assertEqual(ops["kick40060.CD32"].description, "CD32 KS")
        self.assertEqual(ops["kick40060.CD32.ext"].status, "missing")
        self.assertEqual(ops["kick99999.X"].status, "missing")

        res = apply_kickstarts(list(ops.values()))
        self.assertEqual((res["copied"], res["failed"]), (3, []))
        self.assertEqual((self.dest / "kick34005.A500").read_bytes(), self.k13)
        self.assertEqual((self.dest / "kick40068.A1200").read_bytes(), self.k31)
        self.assertEqual((self.dest / "kick40060.CD32").read_bytes(), self.cd32)
        # copies, never moves
        self.assertTrue((self.root / FW / "Kickstart v1.3 (1987)(Commodore).rom").exists())
        self.assertTrue((self.root / "deep" / "ks31.zip").exists())
        self.assertEqual(sorted(os.listdir(self.dest)),
                         ["kick34005.A500", "kick40060.CD32", "kick40068.A1200"])  # no temp leftovers

        again = {op.target.name: op.status for op in plan_kickstarts(self._scan(), self.dest)}
        self.assertEqual(again["kick34005.A500"], "ok")
        self.assertEqual(again["kick40060.CD32"], "ok")
        js = kickstart.to_json(plan_kickstarts(self._scan(), self.dest), self.root)
        self.assertEqual(next(j for j in js if j["filename"] == "kick40068.A1200")["source"],
                         "deep/ks31.zip::whatever.rom")

    def test_existing_targets_never_overwritten(self) -> None:
        self._write("k13.rom", self.k13)
        self._write("k31.rom", self.k31)
        self.dest.mkdir(parents=True)
        (self.dest / "kick34005.A500").write_bytes(b"user's own file")
        (self.dest / "kick40060.CD32").write_bytes(self.cd32_combined)  # other accepted variant
        ops = {op.target.name: op for op in plan_kickstarts(self._scan(), self.dest)}
        self.assertEqual(ops["kick34005.A500"].status, "conflict")
        self.assertEqual(ops["kick40060.CD32"].status, "ok")  # installed, though not found locally
        self.assertEqual(ops["kick40068.A1200"].status, "copy")

        # target appears between plan and apply -> fails, untouched
        (self.dest / "kick40068.A1200").write_bytes(b"raced")
        res = apply_kickstarts(list(ops.values()))
        self.assertEqual(res["copied"], 0)
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual((self.dest / "kick34005.A500").read_bytes(), b"user's own file")
        self.assertEqual((self.dest / "kick40068.A1200").read_bytes(), b"raced")

    def test_md5_verified_on_copy(self) -> None:
        p = self._write("k13.rom", self.k13)
        ops = plan_kickstarts(self._scan(), self.dest)
        p.write_bytes(b"changed after scan")
        res = apply_kickstarts(ops)
        self.assertEqual(res["copied"], 0)
        self.assertIn("md5 mismatch", res["failed"][0]["error"])
        self.assertFalse((self.dest / "kick34005.A500").exists())

    def test_7z_member_extracted(self) -> None:
        arc = self._write("ks.7z", b"7z\xbc\xaf\x27\x1c fake")
        slt = f"Path = Kick13.rom\nSize = {len(self.k13)}\nCRC = {zlib.crc32(self.k13):08X}\n\n"

        def fake_run(cmd, **kw):
            if cmd[1] == "l":
                return subprocess.CompletedProcess(cmd, 0, stdout=slt, stderr="")
            self.assertEqual(cmd[1:4], ["e", "-so", "-p"])
            self.assertEqual(cmd[-2:], [str(arc), "Kick13.rom"])
            return subprocess.CompletedProcess(cmd, 0, stdout=self.k13, stderr=b"")

        with mock.patch.object(scanner.subprocess, "run", side_effect=fake_run):
            result = self._scan(exe="/fake/7z")
            ops = {op.target.name: op for op in plan_kickstarts(result, self.dest)}
            self.assertEqual(ops["kick34005.A500"].status, "copy")
            with mock.patch.object(scanner, "find_7z", return_value="/fake/7z"):
                res = apply_kickstarts(list(ops.values()))
        self.assertEqual(res["copied"], 1, res)
        self.assertEqual((self.dest / "kick34005.A500").read_bytes(), self.k13)

    def test_prefers_firmware_dat_and_loose(self) -> None:
        # Same bytes listed in the Games DAT too: the Firmware match / loose file is preferred.
        self.games.roms.append(_rom("Kick disk (1987).adf", self.k13, GAMES))
        self.games._by_sha1 = self.games._by_crc_size = None
        with zipfile.ZipFile(self._write("a.zip", b""), "w") as zf:
            zf.writestr("k.rom", self.k13)
        self._write("z/k13.rom", self.k13)
        ops = {op.target.name: op for op in plan_kickstarts(self._scan(), self.dest)}
        op = ops["kick34005.A500"]
        self.assertIsNone(op.source.member)
        self.assertEqual(op.rom_name, "Kickstart v1.3 (1987)(Commodore).rom")


class SystemDirTests(unittest.TestCase):
    def test_detect(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            home, media = Path(d) / "home", Path(d) / "media"
            (home / "retrodeck" / "bios").mkdir(parents=True)
            (media / "deck" / "SD" / "Emulation" / "bios").mkdir(parents=True)
            dirs = detect_system_dirs(home=home, media_root=media)
            existing = [x for x in dirs if x["exists"]]
            self.assertEqual([x["label"] for x in existing], ["RetroDECK", "EmuDeck (SD)"])
            self.assertEqual(dirs[:2], existing)  # existing first
            self.assertTrue(any(x["path"].endswith("org.libretro.RetroArch/config/retroarch/system")
                                for x in dirs))
            self.assertTrue(all(re.match(r".+", x["path"]) for x in dirs))

    def test_configured_system_directory_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, media = Path(tmp) / "home", Path(tmp) / "media"
            steam = home / ".local/share/Steam/steamapps/common/RetroArch"
            (steam / "system").mkdir(parents=True)
            (home / "MEGA/Emulation/bios").mkdir(parents=True)
            (steam / "retroarch.cfg").write_text('video_driver = "gl"\nsystem_directory = "~/MEGA/Emulation/bios"\n')
            flat = home / ".var/app/org.libretro.RetroArch/config/retroarch"
            flat.mkdir(parents=True)
            (flat / "retroarch.cfg").write_text('system_directory = ":/system"\n')
            native = home / ".config/retroarch"
            native.mkdir(parents=True)
            (native / "retroarch.cfg").write_text('system_directory = "default"\n')
            sd_ra = media / "deck" / "SD" / "steamapps/common/RetroArch"
            (sd_ra / "system").mkdir(parents=True)
            (media / "deck" / "SD" / "retrodeck" / "bios").mkdir(parents=True)
            dirs = detect_system_dirs(home=home, media_root=media)
            self.assertEqual(dirs[0], {"path": str(home / "MEGA/Emulation/bios"),
                                       "label": "RetroArch (Steam) - configured", "exists": True})
            paths = [d["path"] for d in dirs]
            self.assertIn(str(flat / "system"), paths)
            self.assertEqual(sum(p == str(flat / "system") for p in paths), 1)  # deduplicated
            self.assertIn(str(sd_ra / "system"), paths)
            self.assertIn(str(media / "deck" / "SD" / "retrodeck" / "bios"), paths)
            self.assertFalse(any("default" in p for p in paths))


class WriteTests(unittest.TestCase):
    def test_partial_temp_removed_on_write_error(self) -> None:
        from romorg import kickstart
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "kick34005.A500"
            real_fsync = os.fsync

            def full(fd: int) -> None:
                raise OSError(28, "No space left on device")
            with mock.patch.object(kickstart.os, "fsync", full):
                with self.assertRaises(OSError):
                    kickstart._write_no_overwrite(target, b"x" * 100)
            self.assertEqual(os.listdir(tmp), [])
            del real_fsync


if __name__ == "__main__":
    unittest.main()
