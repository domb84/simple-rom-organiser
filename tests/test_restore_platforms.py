"""Archive and restore across the kinds of systems: a restored file comes back as a build would leave it - a raw cartridge dump as a
.7z where the system's games are kept as 7z, a raw disc as a CHD in its game's folder - and one already in that form as it is. Each
with a local archive folder; the cartridge case with an S3 bucket too (the S3 server of test_s3)."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
import test_dc_server as dcbase  # noqa: E402
import test_ps_server as psbase  # noqa: E402
from servercase import GB, SNES, ServerCase, put, tree  # noqa: E402
from test_s3 import AK, BUCKET, SK, FakeS3, _handler  # noqa: E402

from romorg import sevenzip  # noqa: E402


def seven_members(path: Path) -> dict[str, int]:
    return {n: s for n, s, _c in sevenzip.Archive(path).listing()}


class CartridgeSevenZip(ServerCase):
    """The SNES with the rule "Compress games as 7z" on (the app's default)."""
    COMPRESS_7Z = True

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "snes"
        put(self.root / "beta.sfc", self.rom["Beta (USA)"])

    def archive_dir(self) -> Path:
        return self.tmp / "rom-archive" / "snes"

    def scan(self) -> None:
        self.job("/api/scan", {"path": str(self.root), "platform": SNES})

    def test_a_raw_dump_is_restored_as_a_7z_and_a_7z_as_it_is(self) -> None:
        put(self.archive_dir() / "_excluded" / "alpha raw.sfc", self.rom["Alpha (USA)"])
        sevenzip.write_one(self.archive_dir() / "_excluded" / "gamma.7z", "Gamma (USA).sfc", [self.rom["Gamma (USA)"]],
                           len(self.rom["Gamma (USA)"]))
        self.scan()
        self.assertEqual(self.call("GET", f"/api/tree?platform={SNES.replace(' ', '%20')}&side=library&show=missing")["archive_restorable"], 2)
        res = self.job("/api/archive/restore", {"platform": SNES, "missing": True})
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["converted"], 1)
        got = sorted(tree(self.root))
        self.assertIn("Alpha (USA).7z", got)
        self.assertIn("Gamma (USA).7z", got)
        self.assertEqual(seven_members(self.root / "Alpha (USA).7z"), {"Alpha (USA).sfc": len(self.rom["Alpha (USA)"])})
        self.assertNotIn("alpha raw.sfc", got)                                       # (the raw copy went into the 7z)
        self.assertTrue((self.archive_dir() / "_excluded" / "alpha raw.sfc").is_file())   # the archive keeps its copy

    def test_with_an_s3_archive(self) -> None:
        fake = FakeS3()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.call("POST", "/api/settings/s3", {"endpoint": f"http://127.0.0.1:{srv.server_address[1]}", "access_key": AK, "secret_key": SK})
        self.call("POST", "/api/settings/archive", {"dir": f"s3://{BUCKET}/arch"})
        data = self.rom["Alpha (USA)"]
        fake.objects["arch/snes/_excluded/alpha raw.sfc"] = (data, {"x-amz-meta-sha1": hashlib.sha1(data).hexdigest(),
                                                                    "x-amz-meta-md5": hashlib.md5(data).hexdigest()})
        self.scan()
        res = self.job("/api/archive/restore", {"platform": SNES, "missing": True})
        self.assertEqual((res["restored"], res["converted"], res["failed"]), (1, 1, []))
        self.assertIn("Alpha (USA).7z", tree(self.root))
        self.assertIn("arch/snes/_excluded/alpha raw.sfc", fake.objects)


class CartridgeLoose(ServerCase):
    """The Game Boy with the rule off: a game is restored as the loose file it was."""

    def test_a_raw_dump_is_restored_loose_under_its_name(self) -> None:
        root = self.tmp / "gb"
        put(root / "mono.gb", self.rom["Mono (World)"])
        put(self.tmp / "rom-archive" / "gb" / "_superseded" / "t.gb", self.rom["Tetra (World)"])
        self.job("/api/scan", {"path": str(root), "platform": GB})
        res = self.job("/api/archive/restore", {"platform": GB, "missing": True})
        self.assertEqual((res["restored"], res["placed"], res.get("converted", 0)), (1, 1, 0))
        self.assertEqual((root / "Tetra (World).gb").read_bytes(), self.rom["Tetra (World)"])


class DiscPs2(psbase.PsServerCase):
    """PlayStation 2: an .iso comes back as a CHD in its game's folder (Convert when building, on by default); a CHD as it is."""

    def setUp(self) -> None:
        super().setUp()
        self.arch = self.base / "arch"
        self.call("/api/settings/archive", {"dir": str(self.arch)})
        self.call("/api/chdman", {"engine": "python"})
        self.put_iso("Beta (Europe) (En,Fr,De)", "Beta (Europe) (En,Fr,De)/Beta (Europe) (En,Fr,De).chd")

    def test_an_iso_is_restored_as_a_chd_and_a_chd_as_it_is(self) -> None:
        put(self.arch / "ps2" / "_excluded" / "alpha.iso", self.iso["Alpha (USA)"])
        (self.arch / "ps2" / "_superseded" / "part1").mkdir(parents=True)
        T.build_chd(self.arch / "ps2" / "_superseded" / "part1" / "part one.chd",
                    [{"type": "MODE1", "data": self.iso["Part (USA) (Disc 1)"]}], gd=False)
        self.scan_ps2()
        res = self.run_job("/api/archive/restore", {"platform": psbase.PS2, "missing": True})
        self.assertEqual(res["failed"], [], res["failed"])
        self.assertEqual(res["converted"], 1)
        self.assertTrue((self.roms / "Alpha (USA)" / "Alpha (USA).chd").is_file())
        self.assertFalse((self.roms / "alpha.iso").exists())                                   # (converted: the rule deletes the original)
        self.assertTrue(any(p.suffix == ".chd" and "Part (USA) (Disc 1)" in p.name for p in self.roms.rglob("*.chd")))
        self.assertTrue((self.arch / "ps2" / "_excluded" / "alpha.iso").is_file())


class DiscPs2S3(psbase.PsServerCase):
    """PlayStation 2 with the archive in a bucket: a build uploads a disc with the game it identified; a missing game in the bucket is
    found by that name and restored into its game's folder."""

    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeS3()
        self.s3srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.fake))
        threading.Thread(target=self.s3srv.serve_forever, daemon=True).start()
        self.addCleanup(self.s3srv.server_close)
        self.addCleanup(self.s3srv.shutdown)
        self.call("/api/settings/s3", {"endpoint": f"http://127.0.0.1:{self.s3srv.server_address[1]}", "access_key": AK, "secret_key": SK})
        self.call("/api/settings/archive", {"dir": f"s3://{BUCKET}/arch"})
        self.call("/api/chdman", {"engine": "python"})

    def test_archive_a_duplicate_disc_then_restore_a_missing_one(self) -> None:
        self.put_iso("Beta (Europe) (En,Fr,De)", "Beta (Europe) (En,Fr,De)/Beta (Europe) (En,Fr,De).chd")
        self.put_iso("Beta (Europe) (En,Fr,De)", "spare/beta copy.chd")                           # a second copy: archived
        self.scan_ps2()
        res = self.run_job("/api/library/apply", {"platform": psbase.PS2, "aside_to": f"s3://{BUCKET}/arch"})
        self.assertEqual(res["aside"]["failed"], [])
        keys = [k for k in self.fake.objects if k.startswith("arch/ps2/_duplicates/")]
        self.assertEqual(len(keys), 1, sorted(self.fake.objects))
        self.assertEqual(self.fake.objects[keys[0]][1].get("x-amz-meta-rom"), "Beta (Europe) (En,Fr,De)")
        self.assertFalse((self.roms / "spare").exists())
        alpha = self.base / "alpha.chd"
        T.build_chd(alpha, [{"type": "MODE1", "data": self.iso["Alpha (USA)"]}], gd=False)
        self.fake.objects["arch/ps2/_excluded/Alpha (USA)/old name.chd"] = (alpha.read_bytes(), {"x-amz-meta-rom": "Alpha (USA)"})
        self.scan_ps2()
        back = self.run_job("/api/archive/restore", {"platform": psbase.PS2, "missing": True})
        self.assertEqual(back["failed"], [], back)
        self.assertEqual((self.roms / "Alpha (USA)" / "Alpha (USA).chd").read_bytes(), alpha.read_bytes())


class DiscDreamcast(dcbase.DcServerCase):
    """Dreamcast: a raw .gdi set (a folder of tracks) comes back as one CHD named after its game."""

    def test_a_raw_gdi_set_is_restored_as_a_chd(self) -> None:
        arch = self.base / "arch"
        self.call("/api/settings/archive", {"dir": str(arch)})
        self.call("/api/chdman", {"engine": "python"})
        self.discs["Epsilon (USA)"].write_raw(arch / self.roms.name / "_excluded" / "raw set", "e")
        self.run_job("/api/scan", {"path": str(self.roms), "platform": dcbase.PLAT})
        res = self.run_job("/api/archive/restore", {"platform": dcbase.PLAT, "paths": ["_excluded/raw set"]})
        self.assertEqual(res["failed"], [])
        self.assertTrue((self.roms / "Epsilon (USA)" / "Epsilon (USA).chd").is_file(), sorted(str(p) for p in self.roms.rglob("*")))
        self.assertTrue((arch / self.roms.name / "_excluded" / "raw set" / "e.gdi").is_file())


if __name__ == "__main__":
    unittest.main()
