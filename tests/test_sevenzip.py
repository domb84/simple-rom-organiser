"""romorg.sevenzip: the pure-Python .7z reader (archives are built with a real 7-Zip, skipped when there is none)."""

from __future__ import annotations

import hashlib
import lzma
import os
import random
import shutil
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from romorg import scanner, sevenzip

EXE = scanner.find_7z()
NAMES = {
    "a.bin": os.urandom(50_000),
    "b.rom": bytes(random.Random(3).getrandbits(8) for _ in range(300)) * 1500,
    "sub dir/ünï/日本語 (Japan).sfc": b"x" * 40_000,
    "empty.txt": b"",
}


@unittest.skipUnless(EXE, "7-Zip not available to build test archives")
class SevenZipReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        src = cls.base / "src"
        for name, data in NAMES.items():
            p = src / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        (src / "emptydir").mkdir()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def make(self, label: str, *opts: str) -> Path:
        out = self.base / f"{label}.7z"
        subprocess.run([EXE, "a", "-bso0", "-bsp0", str(out), *opts, "*"], cwd=self.base / "src", check=True,
                       stdin=subprocess.DEVNULL)
        return out

    def check(self, path: Path) -> None:
        arc = sevenzip.Archive(path)
        got = {name: (size, crc) for name, size, crc in arc.listing()}
        self.assertEqual(sorted(got), sorted(NAMES))  # folders are not listed, empty files are
        for name, data in NAMES.items():
            self.assertEqual(got[name], (len(data), "%08x" % (zlib.crc32(data) & 0xFFFFFFFF)), name)
            self.assertEqual(b"".join(arc.iter_member(name)), data, name)

    def test_layouts(self) -> None:
        for label, opts in (("default", ()), ("store", ("-m0=copy",)), ("lzma1", ("-m0=lzma",)),
                            ("nosolid", ("-ms=off",)), ("plainheader", ("-mhc=off",)),
                            ("bigdict", ("-mx=9", "-md=64m"))):
            with self.subTest(label):
                self.check(self.make(label, *opts))

    def test_unsupported_methods_are_reported_not_misread(self) -> None:
        for label, opts in (("bcj", ("-mf=BCJ",)), ("ppmd", ("-m0=ppmd",)), ("bzip2", ("-m0=bzip2",))):
            path = self.make(label, *opts)
            with self.subTest(label):
                arc = sevenzip.Archive(path)          # the listing still works (the header is LZMA)
                self.assertEqual(len(arc.listing()), len(NAMES))
                with self.assertRaises(sevenzip.Unsupported):
                    b"".join(arc.iter_member("b.rom"))

    def test_damage_is_detected(self) -> None:
        path = self.make("damaged", "-ms=off", "-mhc=off")
        raw = bytearray(path.read_bytes())
        raw[40] ^= 0xFF  # inside the first packed stream
        path.write_bytes(bytes(raw))
        arc = sevenzip.Archive(path)
        with self.assertRaises((ValueError, lzma.LZMAError)):
            for name, _size, _crc in arc.files:
                b"".join(arc.iter_member(name))
        path.write_bytes(bytes(raw[:20]))
        with self.assertRaises(ValueError):
            sevenzip.Archive(path)

    def test_scan_without_7zip_reads_7z_archives(self) -> None:
        from romorg.datfile import DatFile, Rom
        data = NAMES["b.rom"]
        rom = Rom("b.rom", len(data), "%08x" % (zlib.crc32(data) & 0xFFFFFFFF), "", "0" * 40, "b", "D")
        d = self.base / "scan"
        d.mkdir(exist_ok=True)
        (d / "set.7z").write_bytes(self.make("scanset", "-ms=off").read_bytes())
        with mock.patch.object(scanner, "find_7z", return_value=None):
            res = scanner.scan(d, [DatFile("Test", "", "", [rom])], use_cache=False)
        self.assertEqual([m.entry.rel for m in res.matched], ["set.7z::b.rom"])
        self.assertEqual(res.unsupported, [])


class SolidBlockTests(unittest.TestCase):
    """A solid block (every file of a CD32 disc in one LZMA stream) is decoded once for all the files taken out of it."""

    def setUp(self) -> None:
        from tests.sevenzipmini import write_solid_7z
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.files = {"disc.cue": b"FILE ...\n" * 4, "disc.iso": os.urandom(3 << 20), "empty.txt": b"", "t02.wav": os.urandom(5 << 20)}
        self.path = write_solid_7z(self.tmp / "disc.7z", self.files)

    def test_every_file_comes_out_whole_from_one_pass(self) -> None:
        from romorg import discarchive
        members = discarchive.list_members(self.path)
        with mock.patch.object(sevenzip, "_folder_bytes", wraps=sevenzip._folder_bytes) as decode:
            out = discarchive.extract_members(self.path, members, self.tmp / "out")
        self.assertEqual(decode.call_count, 1)                                   # (one decode of the block, not one per file)
        self.assertEqual({n: p.read_bytes() for n, p in out.items()}, self.files)
        sums = discarchive.hash_members(self.path, [m for m in members if m.name != "disc.cue"])
        self.assertEqual(sums["t02.wav"][1], hashlib.sha1(self.files["t02.wav"]).hexdigest())
        self.assertEqual(set(sums), {"disc.iso", "empty.txt", "t02.wav"})

    def test_a_file_whose_crc_does_not_match_is_an_error(self) -> None:
        from romorg import discarchive
        arc = sevenzip.Archive(self.path)
        arc.files = [(n, sz, (c ^ 1) if n == "t02.wav" else c) for n, sz, c in arc.files]
        with self.assertRaisesRegex(ValueError, "CRC mismatch in t02.wav"):
            for _n, _b in arc.iter_files():
                pass
        with self.assertRaises(KeyError):
            list(arc.iter_files(["nope"]))


class SevenZipHeaderTests(unittest.TestCase):
    def test_not_a_7z_is_a_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.7z"
            p.write_bytes(b"PK\x03\x04" + bytes(100))
            with self.assertRaises(ValueError):
                sevenzip.Archive(p)

    def test_number_encoding(self) -> None:
        self.assertEqual(sevenzip._Buf(b"\x05").number(), 5)
        self.assertEqual(sevenzip._Buf(b"\x81\x23").number(), 0x123)
        self.assertEqual(sevenzip._Buf(b"\xc1\x23\x45").number(), 0x014523)


if __name__ == "__main__":
    unittest.main()
