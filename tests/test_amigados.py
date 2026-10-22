"""An install's own Amiga programs run here (``romorg.amigados``): the argument parser, RawDoFmt, AmigaDOS paths, and a small
program assembled in the test - it copies a file with dos.library and prints - run alone and from an install's script."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from romorg import amigados, amigainstall as ai
from tests.test_rawdic import Asm


def copy_program(src: bytes = b"WORK:Game/data", dst: bytes = b"WORK:Game/copy") -> bytes:
    """Opens ``src``, reads up to 100 bytes, writes them to ``dst`` (a new file), prints "copied <n>" (VPrintf), returns 0;
    20 (FAIL) when ``src`` cannot be opened."""
    a = Asm()
    a.hex("2c78 0004 43fa"); a.pcrel("dosname"); a.hex("7000 4eae fdd8 2c40")       # OpenLibrary("dos.library")
    a.hex("41fa"); a.pcrel("src"); a.hex("2208 243c 000003ed 4eae ffe2 2800")      # Open(src, MODE_OLDFILE) -> d4
    a.hex("6700"); a.pcrel("fail")
    a.hex("2204 41fa"); a.pcrel("buf"); a.hex("2408 7664 4eae ffd6 2a00")         # Read(d4, buf, 100) -> d5
    a.hex("2204 4eae ffdc")                                                        # Close(d4)
    a.hex("41fa"); a.pcrel("dst"); a.hex("2208 243c 000003ee 4eae ffe2 2800")      # Open(dst, MODE_NEWFILE)
    a.hex("2204 41fa"); a.pcrel("buf"); a.hex("2408 2605 4eae ffd0")              # Write(d4, buf, d5)
    a.hex("2204 4eae ffdc")                                                        # Close
    a.hex("41fa"); a.pcrel("argv"); a.hex("2085")                                  # argv[0] = d5
    a.hex("41fa"); a.pcrel("fmt"); a.hex("2208 41fa"); a.pcrel("argv"); a.hex("2408 4eae fc46")   # VPrintf("copied %ld\n", argv)
    a.hex("7000 4e75")
    a.label("fail"); a.hex("7014 4e75")
    a.label("dosname"); a.raw(b"dos.library\0")
    a.label("src"); a.raw(src + b"\0")
    a.label("dst"); a.raw(dst + b"\0")
    a.label("fmt"); a.raw(b"copied %ld\n\0")
    a.align()
    a.label("argv"); a.raw(bytes(8))
    a.ptr("buf")                                                                   # (one relocated pointer: a hunk file has them)
    a.label("buf"); a.raw(bytes(100))
    return a.hunks()


class DictFs:
    """Files by path ("Vol:dir/file"), as the installer's volumes are to a program."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)

    def exists(self, path: str) -> int:
        low = path.casefold()
        if any(k.casefold() == low for k in self.files):
            return 1
        return 2 if low.endswith(":") or any(k.casefold().startswith(low + "/") for k in self.files) else 0

    def read(self, path: str) -> bytes:
        return next(v for k, v in self.files.items() if k.casefold() == path.casefold())

    def write(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def listing(self, path: str) -> list[tuple[str, bool]]:
        pre = path.rstrip("/") + ("/" if not path.endswith(":") else "")
        out = {}
        for k in self.files:
            if k.casefold().startswith(pre.casefold()):
                rest = k[len(pre):]
                out[rest.split("/")[0]] = "/" in rest
        return sorted(out.items())

    def mkdir(self, path: str) -> None:
        pass

    def delete(self, path: str, everything: bool = False) -> None:
        self.files.pop(path, None)

    def rename(self, old: str, new: str) -> None:
        self.files[new] = self.files.pop(old)


class PartsTests(unittest.TestCase):
    def test_read_args(self) -> None:
        t = "FROM/A,TO/A,ALL/S,NUM/N,PAT/K"
        self.assertEqual(amigados.read_args(t, "df0: RAM:x ALL\n"), ["df0:", "RAM:x", True, None, None])
        self.assertEqual(amigados.read_args(t, 'TO="Work:My Game" FROM a NUM=3 PAT #?.info'), ["a", "Work:My Game", None, 3, "#?.info"])
        self.assertIsNone(amigados.read_args(t, "only-one"))                        # (TO is required)
        self.assertIsNone(amigados.read_args(t, "a b c"))                           # (too many)
        self.assertEqual(amigados.read_args("FILES/M/A,TO/A", "a b c dest"), [["a", "b", "c"], "dest"])
        self.assertEqual(amigados.read_args("NAME,REST/F", "x and all the others"), ["x", "and all the others"])

    def test_raw_do_fmt(self) -> None:
        stream = [7, 0xFFFF, 0x12345678, 0x40]                                       # (a word, a word, a long, a long: a string)
        sizes = iter([2, 2, 4, 4])
        def arg(n: int) -> int:
            self.assertEqual(n, next(sizes))
            return stream.pop(0)
        got = amigados.raw_do_fmt(b"%d|%d|%08lx|%-6s|%%", arg, lambda p: b"name", lambda p: b"")
        self.assertEqual(got, b"7|-1|12345678|name  |%")

    def test_paths(self) -> None:
        j = amigados.join_path
        self.assertEqual(j("RomorgHD:Game/data", "file"), "RomorgHD:Game/data/file")
        self.assertEqual(j("RomorgHD:Game/data", "/x"), "RomorgHD:Game/x")
        self.assertEqual(j("RomorgHD:Game/data", ":x"), "RomorgHD:x")
        self.assertEqual(j("RomorgHD:Game", "a//b"), "RomorgHD:Game/b")
        self.assertEqual(j("", "Disk1:c/dearc"), "Disk1:c/dearc")
        self.assertEqual(j("", "data"), "data")


def patcher_module() -> bytes:
    """A Patcher module: one cycle, its load routine opens the source drive and reads the disk's first 16 bytes (CMD_READ), the
    changes table puts an "X" at offset 3; the file is Game:disk.1."""
    a = Asm()
    a.hex("7014 4e75"); a.ptr("tab"); a.raw(b"PTCH")
    a.label("tab")
    for tag, lab in ((0x80004450, None), (0x80004452, "dn"), (0x80004454, "fn"), (0x80004460, "lt"), (0x80004455, "sp"), (0x80004456, "ch")):
        a.hex(f"{tag:08x}")
        a.ptr(lab) if lab else a.hex("00000001")
    a.hex("00000000")
    a.label("dn"); a.ptr("dname")
    a.label("fn"); a.ptr("fname")
    a.label("lt"); a.hex("00000010")
    a.label("sp"); a.ptr("special")
    a.label("ch"); a.ptr("chtab")
    a.label("chtab"); a.hex("00000003 58400000")                        # (offset 3: "X" - the next long's top byte -, the end)
    a.label("special")
    a.hex("7000 206d 0024 4e90")                                         # PTB_OPENDEVICE(0)
    a.hex("226d 0028 337c 0002 001c 236d 0004 0028 237c 00000010 0024 42a9 002c")   # CMD_READ 16 bytes into PTB_ADDRESSOFFILE
    a.hex("2c78 0004 4eae fe38 7000 4e75")                               # DoIO
    a.label("dname"); a.raw(b"Game\0")
    a.label("fname"); a.raw(b"disk.1\0")
    a.align()
    return a.hunks()


class RunTests(unittest.TestCase):
    def test_a_patcher_module_images_a_disk(self) -> None:
        from tests.test_amigainstall import make_adf
        disk = ai.floppy(make_adf("Orig", {"game": b"exe"}))
        fs = DictFs({"Game:x": b""})                                    # (the volume the script assigned)
        amigados.run_patcher(patcher_module(), fs, [disk], name="Game.Imager")
        self.assertEqual(fs.files["Game:disk.1"], b"DOSX" + disk.data[4:16])
        self.assertEqual(amigados.crc16(b"123456789"), 0xBB3D)
        with self.assertRaises(amigados.AmigaDosError):
            amigados.run_patcher(copy_program(), fs, [disk])

    def test_raw_reads(self) -> None:
        from tests.test_amigainstall import make_adf
        disk = ai.floppy(make_adf("Orig", {"game": b"exe"}))
        turn, nbits = disk.tracks.track(0)
        raw = amigados._Machine._rawread(disk, 0, 0x7C00, 0x10)            # (IOTDF_INDEXSYNC: from the index, the turn again)
        self.assertEqual(raw[:64], turn[:64])
        self.assertEqual(len(raw), 0x7C00)
        synced = amigados._Machine._rawread(disk, 0, 64, 0x20)             # (IOTDF_WORDSYNC: from a sync word)
        self.assertEqual(synced[:2], b"\x44\x89")

    def test_a_program_copies_a_file(self) -> None:
        fs = DictFs({"WORK:Game/data": b"the game's data"})
        rc, out = amigados.run(copy_program(), "", fs, name="CopyIt")
        self.assertEqual((rc, out), (0, "copied 15\n"))
        self.assertEqual(fs.files["WORK:Game/copy"], b"the game's data")
        rc, _out = amigados.run(copy_program(src=b"WORK:nothing"), "", fs)
        self.assertEqual(rc, 20)

    def test_flags_after_a_library_call(self) -> None:
        a = Asm()                                                           # (Open, then beq at once - no tst - as kcdepack does)
        a.hex("2c78 0004 43fa"); a.pcrel("dosname"); a.hex("7000 4eae fdd8 2c40")
        a.hex("41fa"); a.pcrel("name"); a.hex("2208 243c 000003ed 4eae ffe2 6700"); a.pcrel("fail")
        a.hex("7000 4e75")
        a.label("fail"); a.hex("7014 4e75")
        a.label("dosname"); a.raw(b"dos.library\0")
        a.label("name"); a.raw(b"WORK:there\0")
        a.align(); a.ptr("name")
        exe = a.hunks()
        self.assertEqual(amigados.run(exe, "", DictFs({"WORK:there": b"x"}))[0], 0)
        self.assertEqual(amigados.run(exe, "", DictFs({}))[0], 20)

    def test_a_name_from_exnext_opens_as_it_is(self) -> None:
        a = Asm()                                                           # (as DecrunchCIV walks a drawer: Lock, Examine, ExNext, CurrentDir,
        a.hex("2c78 0004 43fa"); a.pcrel("dosname"); a.hex("7000 4eae fdd8 2c40")          #  then Open(fib_FileName) - a C string)
        a.hex("41fa"); a.pcrel("dir"); a.hex("2208 74fe 4eae ffac 2800")                     # Lock("WORK:Game", SHARED) -> d4
        a.hex("2204 41fa"); a.pcrel("fib"); a.hex("2408 4eae ff9a")                          # Examine(d4, fib)
        a.hex("2204 41fa"); a.pcrel("fib"); a.hex("2408 4eae ff94 4a80 6700"); a.pcrel("fail")   # ExNext(d4, fib)
        a.hex("2204 4eae ff82")                                                              # CurrentDir(d4)
        a.hex("41fa"); a.pcrel("fib"); a.hex("5088 2208 243c 000003ed 4eae ffe2 4a80 6700"); a.pcrel("fail")   # Open(fib+8)
        a.hex("7000 4e75")
        a.label("fail"); a.hex("7014 4e75")
        a.label("dosname"); a.raw(b"dos.library\0")
        a.label("dir"); a.raw(b"WORK:Game\0")
        a.align(); a.ptr("dir")
        a.label("fib"); a.raw(bytes(260))
        self.assertEqual(amigados.run(a.hunks(), "", DictFs({"WORK:Game/Crystal": b"x"}))[0], 0)

    def test_not_a_program(self) -> None:
        with self.assertRaises(amigados.AmigaDosError):
            amigados.run(b"plain data", "", DictFs({}))

    def test_from_an_install(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        pkg = tmp / "GameInstall"
        pkg.mkdir()
        (pkg / "Game.slave").write_bytes(b"SLAVE")
        (pkg / "data").write_bytes(b"level data")
        (pkg / "FixData").write_bytes(copy_program())                          # (the install's own tool)
        (pkg / "Install").write_text('(copyfiles (source "Game.slave") (dest "WORK:Game")) (copyfiles (source "data") (dest "WORK:Game"))'
                                     ' (if (<> 0 (run "FixData >CON:///1000//CLOSE")) (abort "it failed"))')
        res = ai.install(pkg, [])
        self.assertEqual(res.files["Game/copy"], b"level data")
        self.assertTrue(any("ran FixData" in x and "copied 10" in x for x in res.log))
        (pkg / "FixData").write_bytes(copy_program(src=b"WORK:Game/gone"))
        with self.assertRaisesRegex(ai.InstallError, "it failed.*FixData returned 20"):    # (the script's own check stops it)
            ai.install(pkg, [])


if __name__ == "__main__":
    unittest.main()
