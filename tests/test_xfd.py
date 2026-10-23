"""xfdDecrunch without an Amiga (``romorg.xfd``): an Amiga library loaded and called in the 68000 interpreter - here a small one
assembled in the test that acts as xfdmaster.library (the real one comes from Aminet, not with the app) - and an install's
``xfdDecrunch`` command (``romorg.amigainstall``), the XFD package fetched once (``romorg.whdinstalls.xfd_package``)."""

from __future__ import annotations

import io
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from romorg import amigainstall as ai, whdinstalls, xfd
from tests.test_rawdic import Asm


def fake_library() -> bytes:
    """A library with a RomTag (auto-init: vectors, an InitStruct table, an init routine) whose xfdRecogBuffer knows files that
    start with "FAKE" and whose xfdDecrunchBuffer gives the rest (exec AllocMem / CopyMem for the target)."""
    a = Asm()
    a.hex("7000 4e75")                                               # (run as a program: nothing)
    a.label("tag")
    a.hex("4afc"); a.ptr("tag"); a.ptr("end"); a.hex("80 27 09 00"); a.ptr("name"); a.ptr("name"); a.ptr("inittab")
    a.label("inittab")
    a.hex("00000040"); a.ptr("vectors"); a.ptr("struct"); a.ptr("init")
    a.label("vectors")
    for f in ["open", "nil", "nil", "nil"] + ["nil"] * 4 + ["recog", "decr"] + ["nil"] * 8 + ["alloc"]:
        a.ptr(f)
    a.hex("ffffffff")
    a.label("struct")
    a.hex("e0 00 00 08 09 00 c0 00 00 0a"); a.ptr("name"); a.hex("d0 00 00 14 0027 00 00")
    a.label("init"); a.hex("4e75")                                   # (d0 = the base: kept)
    a.label("open"); a.hex("200e 4e75")                              # move.l a6,d0
    a.label("nil"); a.hex("7000 4e75")
    # xfdAllocObject: AllocMem(80, MEMF_CLEAR)
    a.label("alloc"); a.hex("2f0e 203c 00000050 223c 00010000 2c78 0004 4eae ff3a 2c5f 4e75")
    # xfdRecogBuffer(a0): "FAKE" at the start - the packer's name in xfdbi_PackerName
    a.label("recog"); a.hex("2250 0c91 46414b45 660c 43fa"); a.pcrel("packer"); a.hex("2149 000c 7001 4e75 7000 4e75")
    # xfdDecrunchBuffer(a0): a target of length - 4, the bytes after "FAKE" copied there
    a.label("decr")
    a.hex("48e7 0022 2448 202a 0004 5980 2540 0020 223c 00010000 2c78 0004 4eae ff3a 2540 0014 2240 2052 5888 202a 0020")
    a.hex("4eae fd90 7001 4cdf 4400 4e75")
    a.label("name"); a.raw(b"xfdmaster.library\0")
    a.label("packer"); a.raw(b"Fake Cruncher\0")
    a.align()
    a.label("end")
    return a.hunks()


def fake_package(tmp: Path) -> bytes:
    p = tmp / "xfdmaster.lha"
    ai.write_lha(p, {"xfd_User/Libs/xfdmaster.library": fake_library(), "xfd_User/Libs/xfd/.README.FIRST": b"read me"})
    return p.read_bytes()


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.x = xfd.Decruncher(fake_package(self.tmp))

    def test_the_library_is_loaded_initialised_and_opened(self) -> None:
        cpu = self.x.cpu
        self.assertEqual(cpu.cstring(cpu.r32(self.x.base + 10)), "xfdmaster.library")      # (ln_Name, from InitStruct)
        self.assertEqual((cpu.r8(self.x.base + 8), cpu.r16(self.x.base + 20)), (9, 39))     # (NT_LIBRARY, lib_Version)
        self.assertEqual(cpu.r16(self.x.base - 114), 0x4EF9)                                # (a jump in the table)

    def test_a_packed_file_and_one_that_is_not(self) -> None:
        self.assertEqual(self.x.decrunch(b"FAKE" + b"the game" * 100), (b"the game" * 100, "Fake Cruncher"))
        self.assertIsNone(self.x.decrunch(b"plain file"))
        self.assertIsNone(self.x.decrunch(b""))
        self.assertEqual(self.x.decrunch(b"FAKEagain"), (b"again", "Fake Cruncher"))     # (the library is used again)

    def test_not_the_package(self) -> None:
        with self.assertRaises(xfd.XfdError):
            xfd.Decruncher(b"not an archive")


def make_lzx() -> bytes:
    """An LZX archive of two files packed together (method 2: literals and matches, one of them at the last offset)."""
    import zlib
    acc, n, out = 0, 0, bytearray()

    def put(v: int, k: int) -> None:                    # (unlzx's order: 16-bit big-endian words, each from its lowest bit up)
        nonlocal acc, n
        acc |= v << n
        n += k
        while n >= 16:
            out.extend((acc & 0xFFFF).to_bytes(2, "big"))
            acc >>= 16
            n -= 16

    def code(c: int, k: int) -> None:                   # (a Huffman code: its top bit first)
        for i in reversed(range(k)):
            put((c >> i) & 1, 1)

    put(2, 3)
    for b in (0, 0, 10):
        put(b, 8)
    for count, zeros in ((256, 0), (256, 256)):         # (symbols 0-511 9 bits long, 512-767 none: by a pretree of "8" and "0")
        for i in range(20):
            put(1 if i in (0, 8) else 0, 4)
        for _ in range(count):
            code(1, 1)
        for _ in range(zeros):
            code(0, 1)
    for sym in (ord("a"), 256 + (2 << 5) + 1, ord("b"), 256):     # (a, 5 at offset 1, b, 3 at the last offset)
        code(sym, 9)
    put(0, 16)
    data = bytes(out)

    def entry(name: bytes, body: bytes, packed: bytes) -> bytes:
        h = bytearray(31)
        struct.pack_into("<II", h, 2, len(body), len(packed))
        h[11], h[30] = 2, len(name)
        struct.pack_into("<I", h, 22, zlib.crc32(body))
        struct.pack_into("<I", h, 26, zlib.crc32(bytes(h) + name))
        return bytes(h) + name + packed
    return b"LZX" + bytes(7) + entry(b"x", b"aaaaaa", b"") + entry(b"d/y", b"bbbb", data)


class FakeDecruncher:
    def decrunch(self, data: bytes):
        return (data[4:], "Fake") if data.startswith(b"FAKE") else None


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = self.tmp / "GameInstall"
        self.pkg.mkdir()
        (self.pkg / "Game.slave").write_bytes(b"SLAVE")
        (self.pkg / "game").write_bytes(b"FAKEexe")
        (self.pkg / "data").mkdir()
        (self.pkg / "data" / "level1").write_bytes(b"FAKEL1")
        (self.pkg / "data" / "plain").write_bytes(b"P")

    def run_script(self, cmd: str, decruncher=FakeDecruncher):
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
                                          '(copyfiles (source "game") (dest #dest)) (copyfiles (source "data") (dest (tackon #dest "data"))'
                                          ' (all)) (run ("cd \\"%s\\"\\n' + cmd + '" #dest))')
        return ai.install(self.pkg, [], decruncher=(lambda: decruncher()) if decruncher else None).files

    def test_over_itself(self) -> None:
        files = self.run_script("xfdDecrunch game")
        self.assertEqual(files["Game/game"], b"exe")

    def test_a_drawer_into_another_and_a_pattern(self) -> None:
        files = self.run_script("xfdDecrunch data unpacked COPYALL")
        self.assertEqual((files["Game/unpacked/level1"], files["Game/unpacked/plain"]), (b"L1", b"P"))
        self.assertEqual(files["Game/data/level1"], b"FAKEL1")                          # (the source as it was)
        files = self.run_script("xfdDecrunch data/#? FORCE")
        self.assertEqual((files["Game/data/level1"], files["Game/data/plain"]), (b"L1", b"P"))

    def test_a_command_in_several_strings(self) -> None:
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
                                          '(copyfiles (source "game") (dest #dest)) '
                                          '(run "xfdDecrunch " (tackon #dest "game") " >CON:///1000//CLOSE")')
        self.assertEqual(ai.install(self.pkg, [], decruncher=FakeDecruncher).files["Game/game"], b"exe")

    def test_foreach_and_join(self) -> None:
        (self.pkg / "Install").write_text(
            '(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest))'
            ' (copyfiles (source "data") (dest (tackon #dest "data")) (all))'
            ' (foreach "data" "#?" (if (= @each-type -3) (copyfiles (source (tackon "data" @each-name)) (dest (tackon #dest "flat")))))'
            ' (run ("cd \\"%s\\"\\nJoin flat/level1 flat/plain AS whole" #dest))')
        files = ai.install(self.pkg, []).files
        self.assertEqual((files["Game/flat/level1"], files["Game/whole"]), (b"FAKEL1", b"FAKEL1P"))

    def test_xadunfile_a_disk_image_and_an_lha(self) -> None:
        from tests.test_amigainstall import make_adf
        (self.pkg / "disk.1").write_bytes(make_adf("Game", {"game": b"exe", "game.info": b"icon", "levels/l1": b"L1"}))
        ai.write_lha(self.pkg / "extra.lha", {"docs/readme": b"hello"})
        (self.pkg / "Install").write_text(
            '(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest))'
            ' (copyfiles (source "disk.1") (dest #dest)) (copyfiles (source "extra.lha") (dest #dest))'
            ' (run ("xadunfile \\"%s/disk.1\\" DEST \\"%s/data\\" DIMG FILE ~(#?.info) >CON:" #dest #dest))'
            ' (run ("xadunfile FROM \\"%s/extra.lha\\" \\"%s\\"" #dest #dest))')
        files = ai.install(self.pkg, []).files
        self.assertEqual((files["Game/data/game"], files["Game/data/levels/l1"], files["Game/docs/readme"]), (b"exe", b"L1", b"hello"))
        self.assertNotIn("Game/data/game.info", files)

    def test_source_a_keyword_apart(self) -> None:
        files = self.run_script("xfdDecrunch SOURCE data ALL")                          # (Xmas Lemmings: "SOURCE dir ALL")
        self.assertEqual((files["Game/data/level1"], files["Game/data/plain"]), (b"L1", b"P"))

    def test_xadunfilem_an_lzx_split_in_parts(self) -> None:
        (self.pkg / "arc.1").write_bytes(make_lzx()[:40])                                # (Dungeon Master II: split over the disks)
        (self.pkg / "arc.2").write_bytes(make_lzx()[40:])
        (self.pkg / "Install").write_text(
            '(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest))'
            ' (copyfiles (source "arc.1") (dest #dest)) (copyfiles (source "arc.2") (dest #dest))'
            ' (if (<> 0 (run ("xadUnFileM \\"%s/arc.#?\\" DEST \\"%s/data\\"" #dest #dest))) (abort "failed"))')
        files = ai.install(self.pkg, []).files
        self.assertEqual((files["Game/data/x"], files["Game/data/d/y"]), (b"aaaaaa", b"bbbb"))

    def test_without_the_package(self) -> None:
        with self.assertRaisesRegex(ai.InstallError, "needs xfdDecrunch"):
            self.run_script("xfdDecrunch game", decruncher=None)


class PackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(whdinstalls, "db_path", return_value=self.tmp / "whdinstalls" / "installs.json")
        p.start()
        self.addCleanup(p.stop)

    def test_fetched_once_and_checked(self) -> None:
        good = fake_package(self.tmp)
        calls = []
        def opener(req):
            calls.append(req.full_url)
            return io.BytesIO(good)
        self.assertEqual(whdinstalls.xfd_package(opener=opener), good)
        self.assertEqual(whdinstalls.xfd_package(opener=opener), good)                    # (from the cache)
        self.assertEqual(calls, [whdinstalls.XFD_URL])
        (self.tmp / "whdinstalls" / "xfdmaster.lha").unlink()
        with self.assertRaises(whdinstalls.WhdInstallError):
            whdinstalls.xfd_package(opener=lambda req: io.BytesIO(b"<html>not it</html>"))
