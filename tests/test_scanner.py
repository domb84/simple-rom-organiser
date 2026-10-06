"""Tests for romorg.scanner (synthetic fixtures only)."""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from romorg import scanner
from romorg.datfile import DatFile, Rom

sys.path.insert(0, os.path.dirname(__file__))
from chdtestlib import install_script, make_symlink  # noqa: E402


def _rand(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


def _rom_for(name: str, game: str, data: bytes, sha1: bool = True) -> Rom:
    return Rom(
        name=name,
        size=len(data),
        crc=f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
        md5=hashlib.md5(data).hexdigest(),
        sha1=hashlib.sha1(data).hexdigest() if sha1 else "",
        game=game,
    )


SLT_7Z = """\
Path = sub
Size = 0
Packed Size = 0
Modified = 2026-10-01 23:08:11.2016934
Attributes = D drwxr-xr-x
CRC =
Encrypted = -
Method =
Block =

Path = sub/empty.adf
Size = 0
Packed Size = 0
Modified = 2026-10-01 23:08:11.2006934
Attributes = A -rw-r--r--
CRC =
Encrypted = -
Method =
Block =

Path = a.adf
Size = 1000
Packed Size = 6004
Modified = 2026-10-01 23:08:11.2006934
Attributes = A -rw-r--r--
CRC = 42790103
Encrypted = -
Method = LZMA2:6k
Block = 0

Path = sub/b.adf
Size = 5000
Packed Size =
Modified = 2026-10-01 23:08:11.2026934
Attributes = A -rw-r--r--
CRC = 5FCF1AF8
Encrypted = -
Method = LZMA2:6k
Block = 0

"""

SLT_ZIP_RAR_STYLE = """\
Path = a.adf
Folder = -
Size = 1000
Packed Size = 1000
Attributes =  -rw-r--r--
Encrypted = -
CRC = 42790103
Method = Store

Path = sub
Folder = +
Size = 0
Packed Size = 0
Attributes = D drwxr-xr-x
CRC =
Method = Store

Path = Game (1990)(Pub)(Disk 1 of 2).adf
Folder = -
Size = 901120
CRC = 0ABC1234
"""


class ParserTests(unittest.TestCase):
    def test_parse_7z_format(self) -> None:
        self.assertEqual(
            scanner.parse_7z_slt(SLT_7Z),
            [("sub/empty.adf", 0, "00000000"), ("a.adf", 1000, "42790103"), ("sub/b.adf", 5000, "5fcf1af8")],
        )

    def test_parse_folder_flag_and_no_trailing_blank(self) -> None:
        self.assertEqual(
            scanner.parse_7z_slt(SLT_ZIP_RAR_STYLE),
            [("a.adf", 1000, "42790103"), ("Game (1990)(Pub)(Disk 1 of 2).adf", 901120, "0abc1234")],
        )

    def test_parse_empty(self) -> None:
        self.assertEqual(scanner.parse_7z_slt(""), [])

    def test_hash_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data = _rand(scanner.CHUNK_SIZE * 2 + 17, 1)
            p = Path(d) / "x.bin"
            p.write_bytes(data)
            crc, sha1 = scanner.hash_file(p)
            self.assertEqual(crc, f"{zlib.crc32(data) & 0xFFFFFFFF:08x}")
            self.assertEqual(sha1, hashlib.sha1(data).hexdigest())


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "roms"
        self.root.mkdir()
        self.cache = base / "cache" / "hashes.sqlite"

        self.d_a = _rand(901120, 10)
        self.d_b1 = _rand(4000, 11)
        self.d_b2 = _rand(4001, 12)
        self.d_c = _rand(3000, 13)   # rom without sha1 in DAT -> crc+size match
        self.d_z = _rand(2500, 14)   # zipped single
        self.d_dup = _rand(2000, 15)  # same dump under two names in the DAT
        self.d_miss = _rand(1000, 16)
        roms = [
            _rom_for("Alpha (1990)(Pub).adf", "Alpha (1990)(Pub)", self.d_a),
            _rom_for("Beta (1991)(Pub)(Disk 1 of 2).adf", "Beta (1991)(Pub)(Disk 1 of 2)", self.d_b1),
            _rom_for("Beta (1991)(Pub)(Disk 2 of 2).adf", "Beta (1991)(Pub)(Disk 2 of 2)", self.d_b2),
            _rom_for("Gamma (1992)(Pub).adf", "Gamma (1992)(Pub)", self.d_c, sha1=False),
            _rom_for("Zed (1993)(Pub).adf", "Zed (1993)(Pub)", self.d_z),
            _rom_for("Dup (1994)(Pub).adf", "Dup (1994)(Pub)", self.d_dup),
            _rom_for("Dup (1994)(Pub)[a].adf", "Dup (1994)(Pub)[a]", self.d_dup),
            _rom_for("Missing (1995)(Pub).adf", "Missing (1995)(Pub)", self.d_miss),
        ]
        self.dat = DatFile("Test DAT", "desc", "2025", roms)

        r = self.root
        (r / "Alpha (1990)(Pub).adf").write_bytes(self.d_a)            # correctly named
        (r / "sub").mkdir()
        (r / "sub" / "beta1.adf").write_bytes(self.d_b1)               # needs rename
        (r / "sub" / "beta2.ADF").write_bytes(self.d_b2)               # needs rename
        (r / "gamma.adf").write_bytes(self.d_c)                        # crc+size only
        (r / "dup1.adf").write_bytes(self.d_dup)                       # matches 2 roms
        (r / "dup2.adf").write_bytes(self.d_dup)                       # duplicate file
        (r / "copy of alpha.adf").write_bytes(self.d_a)                # duplicate file
        (r / "junk.adf").write_bytes(_rand(1234, 99))                  # unmatched
        (r / ".hidden.adf").write_bytes(self.d_miss)                   # skipped
        (r / ".hiddendir").mkdir()
        (r / ".hiddendir" / "x.adf").write_bytes(self.d_miss)          # skipped
        (r / "Beta.m3u").write_text("beta1.adf\n")                     # skipped
        (r / "dl.adf.part").write_bytes(self.d_miss)                   # skipped
        (r / ".romorg-undo-20250101.json").write_text("[]")            # skipped
        with zipfile.ZipFile(r / "Zed (1993)(Pub).zip", "w") as zf:     # correctly named archive
            zf.writestr("whatever.adf", self.d_z)
        with zipfile.ZipFile(r / "multi.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("dir/", b"")
            zf.writestr("dir/zed.adf", self.d_z)                       # duplicate of Zed
            zf.writestr("dir/other.txt", b"hello")                     # unmatched member
        (r / "broken.zip").write_bytes(b"PK\x03\x04 not really a zip")  # error
        (r / "game.7z").write_bytes(b"7z\xbc\xaf\x27\x1c")                # unsupported without 7z
        (r / "game.rar").write_bytes(b"Rar!\x1a\x07\x00")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self, **kw):
        kw.setdefault("cache_path", self.cache)
        with mock.patch.object(scanner, "find_7z", return_value=None):
            return scanner.scan(self.root, self.dat, **kw)

    def _by_rel(self, res) -> dict[str, scanner.Match]:
        return {m.entry.rel: m for m in res.matched}

    def test_full_scan(self) -> None:
        res = self._scan()
        matched = self._by_rel(res)
        self.assertEqual(
            sorted(matched),
            sorted([
                "Alpha (1990)(Pub).adf", "copy of alpha.adf", "sub/beta1.adf", "sub/beta2.ADF",
                "gamma.adf", "dup1.adf", "dup2.adf", "Zed (1993)(Pub).zip::whatever.adf",
                "multi.zip::dir/zed.adf",
            ]),
        )
        self.assertEqual(len(matched["dup1.adf"].roms), 2)
        self.assertEqual(matched["gamma.adf"].roms[0].name, "Gamma (1992)(Pub).adf")
        self.assertIsNone(matched["multi.zip::dir/zed.adf"].entry.sha1)
        self.assertEqual(matched["sub/beta1.adf"].entry.sha1, hashlib.sha1(self.d_b1).hexdigest())
        self.assertEqual(sorted(e.rel for e in res.unmatched), ["junk.adf", "multi.zip::dir/other.txt"])
        self.assertEqual(sorted(p.name for p in res.unsupported), ["game.7z", "game.rar"])
        self.assertEqual([p.name for p, _ in res.errors], ["broken.zip"])
        self.assertEqual([r.name for r in res.missing], ["Missing (1995)(Pub).adf"])
        self.assertEqual(res.dat_name, "Test DAT")

        self.assertEqual(
            res.summary(),
            {
                "dat_total": 8,
                "have": 7,
                "missing": 1,
                "matched_files": 9,
                "unmatched_files": 2,
                "duplicates": 2,  # copy of alpha, dup2 (multi.zip is skipped by the organiser: not a unit)
                "duplicates_set_aside": 0,
                "duplicate_groups": 2,
                "bad_dump_files": 0,
                "unsupported": 2,
                "errors": 1,
                # Alpha loose + Zed zip; multi.zip (3 members) is neither.
                "correctly_named": 2,
                "to_rename": 6,
                "correctly_placed": 0,  # nothing is inside a "Test DAT/" folder yet
                "per_dat": {"Test DAT": {"dat_total": 8, "have": 7, "missing": 1,
                                         "matched_files": 9, "correctly_placed": 0,
                                         "games_total": 8, "games_have": 7, "games_missing": 1,
                                         "count_by": "rom"}},
                # AMENDMENT 4 additions (TOSEC: games = rom games, nothing alternate)
                "games_total": 8,
                "games_have": 7,
                "games_missing": 1,
                "count_by": "rom",
                "matched_via": {"raw": 9, "headerless": 0, "byteswapped": 0},
                "convertible": 0,
                "converted_originals": 0,
            },
        )
        self.assertEqual(res.dat_names, ["Test DAT"])
        self.assertTrue(all(r.dat == "Test DAT" for m in res.matched for r in m.roms))

    def test_non_recursive(self) -> None:
        res = self._scan(recursive=False)
        rels = {m.entry.rel for m in res.matched}
        self.assertNotIn("sub/beta1.adf", rels)
        self.assertIn("Alpha (1990)(Pub).adf", rels)

    def test_to_json(self) -> None:
        res = self._scan()
        js = scanner.to_json(res)
        self.assertEqual(js["root"], str(self.root.absolute()))
        self.assertEqual(js["summary"]["have"], 7)
        paths = {m["path"] for m in js["matched"]}
        self.assertIn("sub/beta1.adf", paths)
        self.assertTrue(all(not os.path.isabs(p) for p in paths))
        zed = next(m for m in js["matched"] if m["member"] == "whatever.adf")
        self.assertEqual(zed["rel"], "Zed (1993)(Pub).zip::whatever.adf")
        self.assertTrue(zed["correctly_named"])
        self.assertEqual(zed["games"], ["Zed (1993)(Pub)"])
        self.assertEqual(js["errors"][0]["path"], "broken.zip")
        self.assertEqual(js["missing"][0]["name"], "Missing (1995)(Pub).adf")
        limited = scanner.to_json(res, limit=1)
        self.assertEqual(len(limited["matched"]), 1)
        self.assertEqual(limited["summary"]["matched_files"], 9)
        import json
        json.dumps(js)  # serialisable

    def test_progress_and_cancel(self) -> None:
        calls: list[tuple[int, int, str]] = []
        self._scan(progress=lambda d, t, n: calls.append((d, t, n)))
        total = calls[0][1]
        self.assertEqual(calls[-1], (total, total, ""))
        self.assertEqual(len(calls), total + 1)

        ev = threading.Event()

        def prog(d: int, t: int, n: str) -> None:
            if d == 2:
                ev.set()

        with self.assertRaises(scanner.ScanCancelled):
            self._scan(progress=prog, cancel=ev)
        with self.assertRaises(scanner.ScanCancelled):
            self._scan(cancel=lambda: True)

    def test_hash_cache_hits_and_invalidation(self) -> None:
        self._scan()
        self.assertTrue(self.cache.exists())
        with mock.patch.object(scanner, "hash_file", side_effect=AssertionError("should be cached")):
            res = self._scan()
        self.assertEqual(res.summary()["have"], 7)

        # Changing content+mtime invalidates the entry.
        p = self.root / "junk.adf"
        p.write_bytes(self.d_miss)
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        real = scanner.hash_file
        hashed: list[str] = []

        def spy(path: Path):
            hashed.append(path.name)
            return real(path)

        with mock.patch.object(scanner, "hash_file", side_effect=spy):
            res = self._scan()
        self.assertEqual(hashed, ["junk.adf"])
        self.assertEqual(res.missing, [])

    def test_unwritable_cache_falls_back(self) -> None:
        blocker = Path(self._tmp.name) / "afile"
        blocker.write_text("x")
        res = self._scan(cache_path=blocker / "sub" / "hashes.sqlite")
        self.assertEqual(res.summary()["have"], 7)
        res = self._scan(use_cache=False)
        self.assertEqual(res.summary()["have"], 7)

    def test_default_cache_path_used(self) -> None:
        with mock.patch.object(scanner, "default_cache_path", return_value=self.cache):
            with mock.patch.object(scanner, "find_7z", return_value=None):
                scanner.scan(self.root, self.dat)
        self.assertTrue(self.cache.exists())

    def test_7z_errors_are_reported(self) -> None:
        fake = subprocess.CompletedProcess([], 2, stdout="", stderr="ERROR: game.7z : Cannot open the file as archive\n\nERRORS:\nIs not archive\n")
        ok = subprocess.CompletedProcess([], 0, stdout=f"Path = x.adf\nSize = {len(self.d_miss)}\n"
                                         f"CRC = {zlib.crc32(self.d_miss):08X}\n\n", stderr="")
        with mock.patch.object(scanner, "find_7z", return_value="/fake/7z"), \
                mock.patch.object(scanner.subprocess, "run",
                                  side_effect=lambda argv, **kw: fake if argv[-1].endswith("game.7z") else ok):
            res = scanner.scan(self.root, self.dat, cache_path=self.cache)      # listings run concurrently: key by file
        self.assertEqual(res.unsupported, [])
        errs = {p.name: msg for p, msg in res.errors}
        self.assertIn("Cannot open the file as archive", errs["game.7z"])
        self.assertIn("game.rar::x.adf", {m.entry.rel for m in res.matched})
        self.assertEqual(res.missing, [])

    def test_not_a_directory(self) -> None:
        with self.assertRaises(NotADirectoryError):
            scanner.scan(self.root / "nope", self.dat, use_cache=False)


class MultiDatTests(unittest.TestCase):
    """Several DATs scanned together, in priority order."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "amiga"
        self.root.mkdir()
        self.shared = _rand(5000, 1)    # in both DATs (e.g. a Workbench disk also listed as a game)
        self.game = _rand(5001, 2)
        self.fw = _rand(4096, 3)
        self.games = DatFile("Games", "", "", [
            _rom_for("Shared (1990)(C).adf", "Shared (1990)(C)", self.shared),
            _rom_for("Game (1990)(P).adf", "Game (1990)(P)", self.game),
            _rom_for("Missing Game (1990)(P).adf", "Missing Game (1990)(P)", _rand(10, 9)),
        ])
        self.wb = DatFile("Workbench", "", "", [
            _rom_for("Workbench v1.3 (1988)(C).adf", "Workbench v1.3 (1988)(C)", self.shared),
        ])
        self.firmware = DatFile("Firmware", "", "", [
            _rom_for("Kickstart v1.3 (1987)(C).rom", "Kickstart v1.3 (1987)(C)", self.fw),
        ])
        (self.root / "x.adf").write_bytes(self.shared)
        (self.root / "Games").mkdir()
        (self.root / "Games" / "Game (1990)(P).adf").write_bytes(self.game)  # already placed
        (self.root / "_unmatched" / "deep").mkdir(parents=True)
        (self.root / "_unmatched" / "deep" / "kick.rom").write_bytes(self.fw)  # matched now
        (self.root / "_unmatched" / "junk.bin").write_bytes(b"junk")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self, dats):
        with mock.patch.object(scanner, "find_7z", return_value=None):
            return scanner.scan(self.root, dats, use_cache=False)

    def test_priority_and_primary(self) -> None:
        res = self._scan([self.games, self.wb, self.firmware])
        self.assertEqual(res.dat_names, ["Games", "Workbench", "Firmware"])
        by = {m.entry.rel: m for m in res.matched}
        shared = by["x.adf"]
        self.assertEqual([r.dat for r in shared.roms], ["Games", "Workbench"])
        self.assertEqual([r.name for r in shared.primary], ["Shared (1990)(C).adf"])
        self.assertEqual(shared.dat, "Games")
        self.assertEqual(by["_unmatched/deep/kick.rom"].dat, "Firmware")  # _unmatched is scanned
        self.assertEqual([e.rel for e in res.unmatched], ["_unmatched/junk.bin"])
        # reversed priority -> Workbench wins
        res2 = self._scan([self.wb, self.games, self.firmware])
        self.assertEqual({m.entry.rel: m for m in res2.matched}["x.adf"].dat, "Workbench")

    def test_summary_per_dat(self) -> None:
        res = self._scan([self.games, self.wb, self.firmware])
        s = res.summary()
        self.assertEqual((s["dat_total"], s["have"], s["missing"]), (5, 4, 1))
        self.assertEqual(s["correctly_placed"], 1)
        self.assertEqual(s["per_dat"]["Games"], {"dat_total": 3, "have": 2, "missing": 1,
                                                 "matched_files": 2, "correctly_placed": 1,
                                                 "games_total": 3, "games_have": 2, "games_missing": 1,
                                                 "count_by": "rom"})
        self.assertEqual(s["per_dat"]["Workbench"], {"dat_total": 1, "have": 1, "missing": 0,
                                                     "matched_files": 0, "correctly_placed": 0,
                                                     "games_total": 1, "games_have": 1, "games_missing": 0,
                                                     "count_by": "rom"})
        self.assertEqual(s["per_dat"]["Firmware"]["matched_files"], 1)
        self.assertEqual([(r.dat, r.name) for r in res.missing], [("Games", "Missing Game (1990)(P).adf")])
        js = scanner.to_json(res)
        x = next(m for m in js["matched"] if m["path"] == "x.adf")
        self.assertEqual((x["dat"], x["dats"]), ("Games", ["Games", "Workbench"]))
        self.assertEqual(js["dat_names"], ["Games", "Workbench", "Firmware"])
        self.assertEqual(js["missing"][0]["dat"], "Games")

    def test_single_dat_still_accepted(self) -> None:
        res = self._scan(self.firmware)
        self.assertEqual(res.dat_name, "Firmware")
        self.assertEqual(len(res.matched), 1)

    def test_crc_match_rejected_when_sha1_differs(self) -> None:
        data = _rand(100, 5)
        fake = Rom("Fake.adf", 100, f"{zlib.crc32(data) & 0xFFFFFFFF:08x}", "", "0" * 40, "Fake", "D")
        (self.root / "fake.adf").write_bytes(data)
        res = self._scan(DatFile("D", "", "", [fake]))
        self.assertNotIn("fake.adf", {m.entry.rel for m in res.matched})

    def test_temp_files_skipped(self) -> None:
        (self.root / "a.adf.romorg-tmp-1234abcd").write_bytes(self.game)
        res = self._scan([self.games])
        self.assertNotIn("a.adf.romorg-tmp-1234abcd", {m.entry.rel for m in res.matched})
        # ... but reported, so an interrupted move can be recovered
        self.assertIn(self.root / "a.adf.romorg-tmp-1234abcd", [p for p, _ in res.errors])

    def test_dangling_symlink_reported(self) -> None:
        make_symlink(self.root / "gone.adf", "../nowhere/gone.adf")
        res = self._scan([self.games])
        self.assertEqual([(p.name, "dangling" in m) for p, m in res.errors], [("gone.adf", True)])

    @unittest.skipUnless(os.name == "posix", "bytes filenames")
    def test_undecodable_filename_matched_with_cache(self) -> None:
        name = os.fsdecode(b"G\xe4me\xff.adf")
        try:
            (self.root / name).write_bytes(self.game)
        except OSError:
            self.skipTest("filesystem rejects the name")
        with mock.patch.object(scanner, "find_7z", return_value=None):
            res = scanner.scan(self.root, [self.games], cache_path=self.root.parent / "cache.sqlite")
        self.assertIn(name, {m.entry.path.name for m in res.matched})
        self.assertEqual(res.errors, [])


class SevenZipIntegrationTest(unittest.TestCase):
    @unittest.skipUnless(scanner.find_7z(), "7z binary not on PATH")
    def test_real_7z_archive(self) -> None:
        exe = scanner.find_7z()
        assert exe
        data = _rand(5000, 42)
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src"
            src.mkdir()
            (src / "Disk (1990)(Pub).adf").write_bytes(data)
            (src / "empty.adf").write_bytes(b"")
            root = Path(d) / "roms"
            root.mkdir()
            subprocess.run([exe, "a", "-bd", str(root / "Disk (1990)(Pub).7z"), str(src / "Disk (1990)(Pub).adf"),
                            str(src / "empty.adf")], check=True, capture_output=True)
            dat = DatFile("t", "", "", [_rom_for("Disk (1990)(Pub).adf", "Disk (1990)(Pub)", data)])
            res = scanner.scan(root, dat, use_cache=False)
            self.assertEqual([m.entry.member for m in res.matched], ["Disk (1990)(Pub).adf"])
            self.assertEqual([e.crc for e in res.unmatched], ["00000000"])
            self.assertEqual(res.summary()["correctly_named"], 0)  # 2 members in archive


# --------------------------------------------------------------------------- No-Intro (AMENDMENT 4)

# ROMORG_REAL_SCRATCH: folder holding nointro (default: the Steam Deck scratch folder)
SCRATCH_NOINTRO = Path(os.environ.get("ROMORG_REAL_SCRATCH") or "/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/"
                       "cfc5ea37-4261-428c-9422-29acd65cac97/scratchpad") / "nointro"


def swap16(data: bytes) -> bytes:
    """Reference implementation (independent of scanner.StreamTransform)."""
    out = bytearray(len(data))
    out[0::2] = data[1::2]
    out[1::2] = data[0::2]
    return bytes(out)


def swap32(data: bytes) -> bytes:
    out = bytearray(len(data))
    for i in range(4):
        out[i::4] = data[3 - i::4]
    return bytes(out)


def ni_rom(name: str, data: bytes, dat: str, game: str | None = None) -> Rom:
    """No-Intro style rom: set_name = rom name without its extension."""
    stem = name.rsplit(".", 1)[0]
    return Rom(name, len(data), f"{zlib.crc32(data) & 0xFFFFFFFF:08x}", hashlib.md5(data).hexdigest(),
               hashlib.sha1(data).hexdigest(), game or stem, dat, stem)


def snes_body(seed: int, kib: int = 8) -> bytes:
    return _rand(kib * 1024, seed)


def n64_z64(seed: int, size: int = 8192) -> bytes:
    return scanner.N64_Z64_MAGIC + _rand(size - 4, seed)


def nes_pair(seed: int, prg: int = 4096) -> tuple[bytes, bytes]:
    body = _rand(prg, seed)
    header = b"NES\x1a" + bytes([2, 1, 0, 0]) + bytes(8)
    return header + body, body


class TransformTests(unittest.TestCase):
    def test_stream_transform_chunking_matches_one_shot(self) -> None:
        data = _rand(10007, 5)
        expect = {"swap16": swap16(data[:-1]) + data[-1:], "swap32": swap32(data[:-3]) + data[-3:],
                  "strip512": data[512:], "strip16": data[16:], "": data}
        for transform, want in expect.items():
            for sizes in ([len(data)], [1, 2, 3, 5, 7, 11], [513, 1, 1], [4096]):
                t = scanner.StreamTransform(transform)
                out = bytearray()
                pos = i = 0
                while pos < len(data):
                    n = sizes[i % len(sizes)]
                    out += t.feed(data[pos:pos + n])
                    pos += n
                    i += 1
                out += t.finish()
                self.assertEqual(bytes(out), want, (transform, sizes))

    def test_unknown_transform(self) -> None:
        with self.assertRaises(ValueError):
            scanner.StreamTransform("rot13")

    def test_variant_rules(self) -> None:
        v = scanner.variant_for
        self.assertEqual(v("snes_header", 1024 * 8 + 512, None), "snes_header")
        self.assertIsNone(v("snes_header", 1024 * 8, None))
        self.assertIsNone(v("snes_header", 512, None))
        self.assertEqual(v("nes_header", 100, b"NES\x1a"), "nes_header")
        self.assertIsNone(v("nes_header", 16, b"NES\x1a"))
        self.assertIsNone(v("nes_header", 100, b"NES\x00"))
        self.assertEqual(v("n64_byteorder", 64, scanner.N64_V64_MAGIC), "n64_v64")
        self.assertEqual(v("n64_byteorder", 64, scanner.N64_N64_MAGIC), "n64_n64")
        self.assertIsNone(v("n64_byteorder", 64, scanner.N64_Z64_MAGIC))
        self.assertIsNone(v("bogus", 64, b""))

    def test_hash_stream_single_pass(self) -> None:
        body = snes_body(1)
        data = _rand(512, 2) + body
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.smc"
            p.write_bytes(data)
            raw, alts = scanner.hash_file_variants(p, ["snes_header", "n64_byteorder"])
        self.assertEqual(raw, (f"{zlib.crc32(data) & 0xFFFFFFFF:08x}", hashlib.sha1(data).hexdigest()))
        self.assertEqual(alts, {"snes_header": (f"{zlib.crc32(body) & 0xFFFFFFFF:08x}",
                                                hashlib.sha1(body).hexdigest(), len(body))})


class _CountingOpen:
    """Wraps builtins.open in romorg.scanner and counts bytes read per path."""

    def __init__(self) -> None:
        self.read: dict[str, int] = {}
        self._open = open

    def __call__(self, path, mode="r", *a, **kw):
        f = self._open(path, mode, *a, **kw)
        if "b" not in mode or "r" not in mode:
            return f
        counter = self

        class Wrapped:
            def __init__(self, inner) -> None:
                self.inner = inner

            def read(self, n=-1):
                data = self.inner.read(n)
                counter.read[str(path)] = counter.read.get(str(path), 0) + len(data)
                return data

            def fileno(self):
                return self.inner.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.inner.close()

            def close(self):
                self.inner.close()

        return Wrapped(f)


class NoIntroScanTests(unittest.TestCase):
    """Synthetic No-Intro DATs: SNES copier headers, N64 byte orders, NES .nes/.unh pairs, zips."""

    SNES = "Nintendo - Super Nintendo Entertainment System"
    N64 = "Nintendo - Nintendo 64"
    NES = "Nintendo - Nintendo Entertainment System"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.cache = self.base / "hashes.sqlite"
        # SNES
        self.s_a = snes_body(10)                 # headered locally (loose)
        self.s_b = snes_body(11)                 # clean locally
        self.s_c = snes_body(12)                 # headered inside a zip
        self.s_miss = snes_body(13)
        self.snes = DatFile(self.SNES, "", "2026.08.01", [
            ni_rom("Alpha (USA).sfc", self.s_a, self.SNES),
            ni_rom("Bravo (Europe) (Rev 1).sfc", self.s_b, self.SNES),
            ni_rom("Charlie (Japan).sfc", self.s_c, self.SNES),
            ni_rom("Missing (USA).sfc", self.s_miss, self.SNES),
        ])
        # N64: the DAT itself lists a .v64 for one set (raw match)
        self.z_a = n64_z64(20)
        self.z_b = n64_z64(21)
        self.z_c = n64_z64(22)
        self.n64 = DatFile(self.N64, "", "2026.08.01", [
            ni_rom("Echo (USA).z64", self.z_a, self.N64),
            ni_rom("Foxtrot (Europe).z64", self.z_b, self.N64),
            ni_rom("Golf (Japan).z64", self.z_c, self.N64),
            ni_rom("Golf (Japan).v64", swap16(self.z_c), self.N64),
        ])
        # NES: .nes (headered) + .unh (headerless) per set
        self.n_a, self.n_a_unh = nes_pair(30)
        self.n_b, self.n_b_unh = nes_pair(31)
        self.n_m, self.n_m_unh = nes_pair(32)
        self.nes = DatFile(self.NES, "", "2026.08.01", [
            ni_rom("Hotel (USA).nes", self.n_a, self.NES),
            ni_rom("Hotel (USA).unh", self.n_a_unh, self.NES),
            ni_rom("India (Europe).nes", self.n_b, self.NES),
            ni_rom("India (Europe).unh", self.n_b_unh, self.NES),
            ni_rom("Juliett (Japan).nes", self.n_m, self.NES),
            ni_rom("Juliett (Japan).unh", self.n_m_unh, self.NES),
        ])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _dir(self, name: str) -> Path:
        d = self.base / name
        d.mkdir()
        return d

    def _scan(self, root: Path, dat: DatFile, alt: tuple[str, ...], **kw):
        kw.setdefault("cache_path", self.cache)
        with mock.patch.object(scanner, "find_7z", return_value=kw.pop("exe", None)):
            return scanner.scan(root, dat, alt_hashes=alt, layout="flat", **kw)

    def _snes_root(self) -> Path:
        r = self._dir("snes")
        (r / "alpha.smc").write_bytes(_rand(512, 1) + self.s_a)
        (r / "Bravo (Europe) (Rev 1).sfc").write_bytes(self.s_b)
        with zipfile.ZipFile(r / "charlie.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("charlie.smc", _rand(512, 2) + self.s_c)
        (r / "Alpha (USA).sfc").write_bytes(_rand(512, 3) + self.s_a)  # claims to be clean, has a header
        (r / "noise.smc").write_bytes(_rand(512 + 2048, 4))            # header-sized, no match
        (r / "readme.txt").write_bytes(b"hello")
        return r

    def test_snes_headerless_matching(self) -> None:
        res = self._scan(self._snes_root(), self.snes, ("snes_header",))
        by = {m.entry.rel: m for m in res.matched}
        self.assertEqual(sorted(by), ["Alpha (USA).sfc", "Bravo (Europe) (Rev 1).sfc", "alpha.smc",
                                      "charlie.zip::charlie.smc"])
        a = by["alpha.smc"]
        self.assertEqual((a.matched_via, a.header, a.byte_order), ("headerless", 512, ""))
        self.assertEqual(a.alt_sha1, hashlib.sha1(self.s_a).hexdigest())
        self.assertEqual(a.entry.size, 512 + len(self.s_a))           # Entry stays the raw file
        self.assertEqual(a.entry.sha1, hashlib.sha1((self.base / "snes" / "alpha.smc").read_bytes()).hexdigest())
        self.assertEqual(by["Bravo (Europe) (Rev 1).sfc"].matched_via, "raw")
        z = by["charlie.zip::charlie.smc"]
        self.assertEqual((z.matched_via, z.header), ("headerless", 512))
        self.assertEqual(z.alt_sha1, hashlib.sha1(self.s_c).hexdigest())
        self.assertEqual(sorted(e.rel for e in res.unmatched), ["noise.smc", "readme.txt"])
        # names: a headered file is "correctly named" as <set>.smc, never .sfc
        self.assertEqual(scanner.canonical_name(a), "Alpha (USA).smc")
        self.assertEqual(scanner.canonical_name(by["Alpha (USA).sfc"]), "Alpha (USA).smc")
        self.assertFalse(scanner.is_correctly_named(by["Alpha (USA).sfc"]))
        self.assertTrue(scanner.is_correctly_named(by["Bravo (Europe) (Rev 1).sfc"]))
        self.assertEqual(scanner.canonical_name(z), "Charlie (Japan).zip")
        s = res.summary()
        self.assertEqual((s["dat_total"], s["have"], s["missing"]), (4, 3, 1))
        self.assertEqual((s["games_total"], s["games_have"], s["games_missing"]), (4, 3, 1))
        self.assertEqual(s["count_by"], "game")
        self.assertEqual(s["matched_via"], {"raw": 1, "headerless": 3, "byteswapped": 0})
        self.assertEqual(s["convertible"], 2)  # the second headered Alpha converts to the same name
        self.assertEqual(s["duplicates"], 1)  # second headered Alpha
        self.assertEqual(s["correctly_placed"], 1)  # flat: Bravo already in root with its name
        self.assertEqual(s["per_dat"][self.SNES]["count_by"], "game")
        self.assertEqual(res.layout, "flat")
        self.assertEqual([r.name for r in res.missing], ["Missing (USA).sfc"])

    def test_alternates_only_when_raw_fails_and_strategy_enabled(self) -> None:
        r = self._snes_root()
        res = self._scan(r, self.snes, ())  # no strategies: plain raw scan (TOSEC behaviour)
        self.assertEqual([m.entry.rel for m in res.matched], ["Bravo (Europe) (Rev 1).sfc"])
        # a raw match wins over the alternate even if both could apply
        body = snes_body(40)
        raw_and_alt = _rand(512, 41) + body
        dat = DatFile("X", "", "", [ni_rom("Raw (USA).sfc", raw_and_alt, "X"), ni_rom("Alt (USA).sfc", body, "X")])
        d = self._dir("both")
        (d / "f.smc").write_bytes(raw_and_alt)
        res = self._scan(d, dat, ("snes_header",))
        self.assertEqual([(m.matched_via, [x.name for x in m.roms]) for m in res.matched],
                         [("raw", ["Raw (USA).sfc"])])

    def test_single_read_per_file(self) -> None:
        r = self._snes_root()
        counter = _CountingOpen()
        with mock.patch("builtins.open", counter):
            self._scan(r, self.snes, ("snes_header", "nes_header", "n64_byteorder"), use_cache=False)
        loose = [p for p in r.iterdir() if p.suffix != ".zip"]
        for p in loose:
            self.assertEqual(counter.read.get(str(p), 0), p.stat().st_size, p.name)

    def test_alt_hash_cache(self) -> None:
        r = self._snes_root()
        first = self._scan(r, self.snes, ("snes_header",))
        boom = AssertionError("must come from the cache")
        with mock.patch.object(scanner, "hash_file", side_effect=boom), \
                mock.patch.object(scanner, "hash_file_variants", side_effect=boom), \
                mock.patch.object(scanner, "hash_stream", side_effect=boom):
            second = self._scan(r, self.snes, ("snes_header",))
        key = lambda res: sorted((m.entry.rel, m.matched_via, m.alt_sha1) for m in res.matched)
        self.assertEqual(key(first), key(second))
        # stale rows: rewrite the file with other content -> rehashed, no longer matches
        p = r / "alpha.smc"
        p.write_bytes(_rand(512 + 1024, 77))
        os.utime(p, ns=(10**9, 10**9))
        third = self._scan(r, self.snes, ("snes_header",))
        self.assertNotIn("alpha.smc", {m.entry.rel for m in third.matched})
        import sqlite3
        conn = sqlite3.connect(self.cache)
        rows = conn.execute("SELECT size, mtime_ns, member, variant FROM alt_hashes WHERE path=?",
                            (str(p),)).fetchall()
        conn.close()
        self.assertEqual(rows, [(512 + 1024, 10**9, "", "snes_header")])

    def test_n64_byte_orders(self) -> None:
        r = self._dir("n64")
        (r / "echo.v64").write_bytes(swap16(self.z_a))
        (r / "foxtrot.n64").write_bytes(swap32(self.z_b))
        (r / "Golf (Japan).v64").write_bytes(swap16(self.z_c))    # the DAT's own .v64 -> raw
        (r / "Echo (USA).z64").write_bytes(self.z_a)              # clean
        with zipfile.ZipFile(r / "fox.zip", "w") as zf:
            zf.writestr("fox.n64", swap32(self.z_b))
        res = self._scan(r, self.n64, ("n64_byteorder",))
        by = {m.entry.rel: m for m in res.matched}
        self.assertEqual((by["echo.v64"].matched_via, by["echo.v64"].byte_order), ("byteswapped", "v64"))
        self.assertEqual((by["foxtrot.n64"].matched_via, by["foxtrot.n64"].byte_order), ("byteswapped", "n64"))
        self.assertEqual(by["foxtrot.n64"].alt_sha1, hashlib.sha1(self.z_b).hexdigest())
        self.assertEqual(by["fox.zip::fox.n64"].byte_order, "n64")
        g = by["Golf (Japan).v64"]
        self.assertEqual((g.matched_via, [x.name for x in g.roms]), ("raw", ["Golf (Japan).v64"]))
        self.assertTrue(scanner.is_correctly_named(g))
        self.assertEqual(scanner.canonical_name(by["echo.v64"]), "Echo (USA).v64")
        self.assertEqual(scanner.canonical_name(by["foxtrot.n64"]), "Foxtrot (Europe).n64")
        s = res.summary()
        self.assertEqual((s["games_total"], s["games_have"]), (3, 3))
        self.assertEqual(s["matched_via"], {"raw": 2, "headerless": 0, "byteswapped": 3})
        self.assertEqual(s["convertible"], 3)

    def test_nes_alternates_count_once(self) -> None:
        r = self._dir("nes")
        (r / "Hotel (USA).nes").write_bytes(self.n_a)                     # raw .nes
        (r / "hotel.unh").write_bytes(self.n_a_unh)                       # raw .unh: same game
        other_header = b"NES\x1a" + bytes([2, 1, 0x10, 0]) + bytes(8)
        (r / "india.nes").write_bytes(other_header + self.n_b_unh)        # different header
        with zipfile.ZipFile(r / "india2.zip", "w") as zf:
            zf.writestr("india.nes", other_header + self.n_b_unh)
        res = self._scan(r, self.nes, ("nes_header",))
        by = {m.entry.rel: m for m in res.matched}
        self.assertEqual(by["Hotel (USA).nes"].matched_via, "raw")
        i = by["india.nes"]
        self.assertEqual((i.matched_via, i.header, [x.name for x in i.roms]),
                         ("headerless", 16, ["India (Europe).unh"]))
        self.assertEqual(by["india2.zip::india.nes"].matched_via, "headerless")
        self.assertEqual(scanner.canonical_name(i), "India (Europe).nes")
        s = res.summary()
        self.assertEqual((s["dat_total"], s["have"], s["missing"]), (3, 2, 1))
        self.assertEqual((s["games_total"], s["games_have"], s["games_missing"]), (3, 2, 1))
        # .nes and .unh are different forms of a game (not duplicates); india.nes + india2.zip are
        self.assertEqual((s["duplicates"], s["duplicate_groups"]), (1, 1))
        self.assertEqual(s["convertible"], 0)  # NES is never converted
        self.assertEqual([x.name for x in res.missing], ["Juliett (Japan).nes"])  # one rom per set
        rows = scanner.game_status(res)
        self.assertEqual([(g["name"], g["have"]) for g in rows],
                         [("Hotel (USA)", True), ("India (Europe)", True), ("Juliett (Japan)", False)])
        self.assertEqual(rows[0]["roms"], ["Hotel (USA).nes", "Hotel (USA).unh"])
        self.assertEqual(sorted(rows[0]["files"]), ["Hotel (USA).nes", "hotel.unh"])
        self.assertIs(rows[2]["rom"], self.nes.roms[4])
        js = scanner.to_json(res)
        self.assertEqual(js["layout"], "flat")
        row = next(m for m in js["matched"] if m["path"] == "india.nes")
        self.assertEqual((row["via"], row["header"], row["byte_order"]), ("headerless", 16, ""))
        self.assertIn("tags", row)
        self.assertEqual(js["missing"][0]["set_name"], "Juliett (Japan)")
        if row["tags"] is not None:  # tags.py present
            self.assertEqual(row["tags"]["regions"], ["Europe"])

    def test_7z_member_streaming_with_fake_7z(self) -> None:
        import sys
        r = self._dir("snes7z")
        member_data = _rand(512, 5) + self.s_a
        blob = self.base / "member.bin"
        blob.write_bytes(member_data)
        listing = (f"Path = alpha.smc\nSize = {len(member_data)}\nPacked Size = 1\nAttributes = A\n"
                   f"CRC = {zlib.crc32(member_data) & 0xFFFFFFFF:08X}\n\n"
                   f"Path = notes.txt\nSize = 3\nAttributes = A\nCRC = 00000001\n\n")
        fake = install_script(self.base / "fake7z",
            "import sys\n"
            f"if sys.argv[1] == 'l':\n    sys.stdout.write({listing!r})\n"
            f"elif sys.argv[1] == 'e':\n"
            f"    assert sys.argv[-1] == 'alpha.smc', sys.argv\n"
            f"    sys.stdout.buffer.write(open({str(blob)!r}, 'rb').read())\n"
            f"else:\n    sys.exit(2)\n")
        (r / "alpha.7z").write_bytes(b"7z\xbc\xaf\x27\x1c")
        res = self._scan(r, self.snes, ("snes_header",), exe=str(fake))
        self.assertEqual([(m.entry.rel, m.matched_via) for m in res.matched],
                         [("alpha.7z::alpha.smc", "headerless")])
        self.assertEqual([e.rel for e in res.unmatched], ["alpha.7z::notes.txt"])
        self.assertEqual(res.errors, [])
        # extraction failures are reported, the member stays unmatched
        fake = install_script(self.base / "fake7z", "import sys\n"
                        f"if sys.argv[1] == 'l':\n    sys.stdout.write({listing!r})\n"
                        f"else:\n    sys.stderr.write('ERROR: Data Error\\n'); sys.exit(2)\n")
        res = self._scan(r, self.snes, ("snes_header",), use_cache=False, exe=str(fake))
        self.assertEqual(res.matched, [])
        self.assertEqual(len(res.errors), 1)
        self.assertIn("alpha.smc: RuntimeError: ERROR: Data Error", res.errors[0][1])

    def test_7z_member_without_variant_is_not_rehashed(self) -> None:
        # a .v64-named member that is really big-endian: no variant; cached as such
        r = self._dir("n647z")
        (r / "golf.7z").write_bytes(b"7z\xbc\xaf\x27\x1c")
        data = self.z_a  # z64 magic
        listing = [("golf.v64", len(data) + 4, "deadbeef")]  # no raw match either
        with mock.patch.object(scanner, "list_7z", return_value=listing), \
                mock.patch.object(scanner, "hash_7z_member", return_value={}) as h:
            for _ in range(3):
                res = self._scan(r, self.n64, ("n64_byteorder",), exe="/fake/7z")
                self.assertEqual([e.rel for e in res.unmatched], ["golf.7z::golf.v64"])
            self.assertEqual(h.call_count, 1)
            # another strategy set is computed again (the "no variant" row is per strategies)
            self._scan(r, self.n64, ("n64_byteorder", "nes_header"), exe="/fake/7z")
            self.assertEqual(h.call_count, 2)
        # an extraction error is not cached as "no variant"
        with mock.patch.object(scanner, "list_7z", return_value=[("x.v64", 64, "deadbeef")]), \
                mock.patch.object(scanner, "hash_7z_member", side_effect=RuntimeError("boom")) as h2:
            (r / "golf.7z").write_bytes(b"7z\xbc\xaf\x27\x1c\x00")  # new size: new cache key
            self._scan(r, self.n64, ("n64_byteorder",), exe="/fake/7z")
            self._scan(r, self.n64, ("n64_byteorder",), exe="/fake/7z")
            self.assertEqual(h2.call_count, 2)

    def test_7z_member_stream_stops_when_no_variant_applies(self) -> None:
        import sys
        fake = self.base / "slow7z"
        # writes 2 MiB of big-endian N64 data, then would keep going for a long time
        fake = install_script(fake, "import sys, time\n"
                        f"sys.stdout.buffer.write({scanner.N64_Z64_MAGIC!r} + bytes(2 * 1024 * 1024 - 4))\n"
                        f"sys.stdout.buffer.flush()\ntime.sleep(30)\n")
        t0 = time.monotonic()
        alt = scanner.hash_7z_member(self.base / "x.7z", "x.v64", str(fake), 64 * 1024 * 1024,
                                     ["n64_byteorder"])
        self.assertEqual(alt, {})
        self.assertLess(time.monotonic() - t0, 10)

    def test_snes_header_on_odd_sized_dat_rom(self) -> None:
        # real SNES DAT roms that are not a multiple of 1 KiB (262143, 2097153 bytes ...)
        body = _rand(262143, 40)
        dat = DatFile(self.SNES, "", "", [ni_rom("Desolate (World) (v1.1).sfc", body, self.SNES)])
        r = self._dir("odd")
        (r / "desolate.smc").write_bytes(bytes(512) + body)
        with zipfile.ZipFile(r / "d.zip", "w") as zf:
            zf.writestr("d.smc", bytes(512) + body)
        (r / "other.smc").write_bytes(bytes(512) + _rand(262000, 41))  # size not in the DAT
        res = self._scan(r, dat, ("snes_header",))
        self.assertEqual(sorted((m.entry.rel, m.matched_via) for m in res.matched),
                         [("d.zip::d.smc", "headerless"), ("desolate.smc", "headerless")])
        self.assertEqual([e.rel for e in res.unmatched], ["other.smc"])
        self.assertIsNone(scanner.variant_for("snes_header", 262143 + 512, None))
        self.assertEqual(scanner.variant_for("snes_header", 262143 + 512, None, {262143}), "snes_header")

    def test_clrmamepro_dat_end_to_end(self) -> None:
        from romorg import datfile
        if not hasattr(datfile, "parse_clrmamepro"):
            self.skipTest("clrmamepro parser not available")
        text = ['clrmamepro (\n\tname "Nintendo - Nintendo Entertainment System"\n\tversion "2026.08.01"\n)\n']
        for name, data in (("Hotel (USA).nes", self.n_a), ("Hotel (USA).unh", self.n_a_unh)):
            text.append(f'game (\n\tname "Hotel (USA)"\n\tregion "USA"\n\trom ( name "{name}" size {len(data)} '
                        f'crc {zlib.crc32(data) & 0xFFFFFFFF:08X} md5 {hashlib.md5(data).hexdigest().upper()} '
                        f'sha1 {hashlib.sha1(data).hexdigest().upper()} )\n)\n')
        p = self.base / "nes.dat"
        p.write_text("".join(text))
        dat = datfile.parse_dat(p)
        r = self._dir("nes2")
        (r / "x.nes").write_bytes(b"NES\x1a" + bytes(12) + self.n_a_unh)
        res = self._scan(r, dat, ("nes_header",))
        self.assertEqual([m.matched_via for m in res.matched], ["headerless"])
        self.assertEqual(res.summary()["games_have"], 1)
        self.assertEqual(res.summary()["dat_total"], 1)

    @unittest.skipUnless((SCRATCH_NOINTRO / "Nintendo - Nintendo Entertainment System.dat").exists(),
                         "real No-Intro DATs not present")
    def test_real_nes_dat_counts_sets(self) -> None:
        from romorg import datfile
        dat = datfile.parse_dat(SCRATCH_NOINTRO / "Nintendo - Nintendo Entertainment System.dat")
        res = self._scan(self._dir("empty"), dat, ("nes_header",), use_cache=False)
        s = res.summary()
        self.assertEqual((s["dat_total"], s["games_total"], s["have"]), (7070, 7070, 0))
        self.assertEqual(len(res.missing), 7070)
        self.assertEqual(len(scanner.game_status(res)), 7070)


class PerformanceTest(unittest.TestCase):
    def test_cached_scan_of_2000_files_is_fast(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "roms"
            root.mkdir()
            roms = []
            for i in range(2000):
                data = i.to_bytes(4, "little") * 64
                name = f"Game {i:04d} (1990)(Pub).adf"
                (root / (f"g{i}.adf" if i % 2 else name)).write_bytes(data)
                roms.append(_rom_for(name, name[:-4], data))
            dat = DatFile("perf", "", "", roms)
            cache = Path(d) / "hashes.sqlite"
            scanner.scan(root, dat, cache_path=cache)
            t0 = time.perf_counter()
            with mock.patch.object(scanner, "hash_file", side_effect=AssertionError("cached")):
                res = scanner.scan(root, dat, cache_path=cache)
            elapsed = time.perf_counter() - t0
            s = res.summary()
            self.assertEqual((s["have"], s["correctly_named"], s["to_rename"]), (2000, 1000, 1000))
            self.assertLess(elapsed, 3.0)


class DuplicateGroupTests(unittest.TestCase):
    """scanner.duplicate_groups: the single source of truth for summary()['duplicates'] and the organiser."""

    DAT = "Commodore Amiga - Games - [ADF]"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "amiga"
        self.root.mkdir()
        self.data = _rand(700, 31)
        self.rom = _rom_for("Game (1990)(Pub).adf", "Game (1990)(Pub)", self.data)
        self.other = _rom_for("Other (1990)(Pub).adf", "Other (1990)(Pub)", _rand(700, 32))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def put(self, rel: str, data: bytes | None = None) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.data if data is None else data)
        return p

    def zip(self, rel: str, members: dict[str, bytes]) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(p, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return p

    def scan(self) -> scanner.ScanResult:
        with mock.patch.object(scanner, "find_7z", return_value=None):
            return scanner.scan(self.root, [DatFile(self.DAT, "", "", [self.rom, self.other])], use_cache=False)

    def groups(self) -> list[tuple[str, list[str]]]:
        return [(k.relative_to(self.root).as_posix(), sorted(x.relative_to(self.root).as_posix() for x in ls))
                for k, ls in scanner.duplicate_groups(self.scan())]

    def test_no_duplicates(self) -> None:
        self.put("a.adf")
        self.put("b.adf", self.other_data())
        self.assertEqual(self.groups(), [])
        self.assertEqual(self.scan().summary()["duplicates"], 0)

    def other_data(self) -> bytes:
        return _rand(700, 32)

    def test_loose_and_zip_of_one_rom_count_once(self) -> None:
        self.put("a.adf")
        self.zip("a.zip", {"x.adf": self.data})
        self.assertEqual(self.groups(), [("a.adf", ["a.zip"])])
        s = self.scan().summary()
        self.assertEqual((s["duplicates"], s["duplicate_groups"], s["duplicates_set_aside"]), (1, 1, 0))

    def test_keeper_order(self) -> None:
        # canonical path+name wins over everything, even a shorter path
        self.put(f"{self.DAT}/Game (1990)(Pub).adf")
        self.put("a.adf")
        self.assertEqual(self.groups(), [(f"{self.DAT}/Game (1990)(Pub).adf", ["a.adf"])])
        # no canonical copy: outside _unmatched/ first
        for f in list(self.root.rglob("*.adf")):
            f.unlink()
        shutil.rmtree(self.root / self.DAT)
        self.put("_unmatched/a.adf")
        self.put("deep/er/z.adf")
        self.assertEqual(self.groups(), [("deep/er/z.adf", ["_unmatched/a.adf"])])
        # then loose over archive, then shortest path, then alphabetical
        shutil.rmtree(self.root / "_unmatched")
        shutil.rmtree(self.root / "deep")
        self.zip("a.zip", {"x.adf": self.data})
        self.put("sub/loose.adf")
        self.assertEqual(self.groups(), [("sub/loose.adf", ["a.zip"])])
        self.put("sub/lo.adf")
        self.put("sub/aa.adf")
        self.assertEqual(self.groups(), [("sub/aa.adf", ["a.zip", "sub/lo.adf", "sub/loose.adf"])])
        (self.root / "sub" / "aa.adf").unlink()
        self.put("sub/ab.adf")
        self.assertEqual(self.groups(), [("sub/ab.adf", ["a.zip", "sub/lo.adf", "sub/loose.adf"])])

    def test_different_roms_do_not_group_and_set_aside_copies_are_not_counted(self) -> None:
        self.put("a.adf")
        self.put("b.adf", self.other_data())
        self.put("_duplicates/c.adf")
        res = self.scan()
        s = res.summary()
        self.assertEqual((s["duplicates"], s["duplicates_set_aside"], s["duplicate_groups"]), (0, 1, 1))
        self.assertEqual(s["to_rename"], 2)  # a.adf, b.adf; the set-aside copy keeps its own name
        self.assertEqual([k.name for k, _ in scanner.duplicate_groups(res)], ["a.adf"])

    def test_converted_originals_and_symlinks_never_take_part(self) -> None:
        self.put("a.adf")
        self.put("_converted_originals/orig.adf")
        link = self.root / "link.adf"
        try:
            os.symlink(self.root / "a.adf", link)
        except OSError:
            link = None
        self.assertEqual(self.groups(), [])
        self.assertEqual(self.scan().summary()["duplicates"], 0)
        self.assertTrue(link is None or link.is_symlink())

    def test_summary_equals_the_organise_plan(self) -> None:
        from romorg import organiser

        self.put("a.adf")
        self.put("b/c.adf")
        self.zip("z.zip", {"m.adf": self.data})
        self.put("_duplicates/old.adf")
        self.put("o1.adf", self.other_data())
        self.put("o2.adf", self.other_data())
        res = self.scan()
        ops = organiser.plan_renames(res)
        self.assertEqual(res.summary()["duplicates"], organiser.plan_counts(ops)["to_duplicates"])
        # a.adf kept; b/c.adf and z.zip move; old.adf is already set aside; o1/o2: one moves
        self.assertEqual((res.summary()["duplicates"], res.summary()["duplicates_set_aside"]), (3, 1))


if __name__ == "__main__":
    unittest.main()
