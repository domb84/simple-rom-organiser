"""Tests for romorg.convert (synthetic fixtures only)."""

from __future__ import annotations

import hashlib
import json
import random
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from romorg import convert, organiser, scanner
from romorg.datfile import DatFile, Rom

SNES = "Nintendo - Super Nintendo Entertainment System"
N64 = "Nintendo - Nintendo 64"
NES = "Nintendo - Nintendo Entertainment System"


def _rand(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


def swap16(data: bytes) -> bytes:
    out = bytearray(len(data))
    out[0::2] = data[1::2]
    out[1::2] = data[0::2]
    return bytes(out)


def swap32(data: bytes) -> bytes:
    out = bytearray(len(data))
    for i in range(4):
        out[i::4] = data[3 - i::4]
    return bytes(out)


def ni_rom(name: str, data: bytes, dat: str, sha1: bool = True) -> Rom:
    stem = name.rsplit(".", 1)[0]
    return Rom(name, len(data), f"{zlib.crc32(data) & 0xFFFFFFFF:08x}", hashlib.md5(data).hexdigest(),
               hashlib.sha1(data).hexdigest() if sha1 else "", stem, dat, stem)


def sha1(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def tree(root: Path) -> dict[str, str]:
    """rel path -> sha1 of every file (undo logs excluded)."""
    return {p.relative_to(root).as_posix(): sha1(p) for p in sorted(root.rglob("*"))
            if p.is_file() and not p.name.startswith(organiser.UNDO_PREFIX)}


class TransformStreamTests(unittest.TestCase):
    def test_transforms(self) -> None:
        import io
        data = scanner.N64_Z64_MAGIC + _rand(3 * 1024 * 1024 + 4, 1)  # > one chunk
        for transform, src, want in (("swap16", swap16(data), data), ("swap32", swap32(data), data),
                                     ("strip512", _rand(512, 2) + data, data)):
            out = io.BytesIO()
            crc, h, n = convert.transform_stream(io.BytesIO(src), out, transform)
            self.assertEqual(out.getvalue(), want, transform)
            self.assertEqual((crc, h, n), (f"{zlib.crc32(want) & 0xFFFFFFFF:08x}",
                                           hashlib.sha1(want).hexdigest(), len(want)))


class ConvertTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.cache = self.base / "hashes.sqlite"
        self.s_a = _rand(8 * 1024, 10)
        self.s_b = _rand(8 * 1024, 11)
        self.s_c = _rand(4 * 1024, 12)
        self.s_old = _rand(4 * 1024, 13)
        self.s_new = _rand(4 * 1024, 14)
        self.snes = DatFile(SNES, "", "", [
            ni_rom("Alpha (USA).sfc", self.s_a, SNES),
            ni_rom("Bravo (Europe).sfc", self.s_b, SNES),
            ni_rom("Charlie (Japan).sfc", self.s_c, SNES),
            ni_rom("Delta (USA).sfc", self.s_old, SNES),
            ni_rom("Delta (USA) (Rev 1).sfc", self.s_new, SNES),
        ])
        self.z_a = scanner.N64_Z64_MAGIC + _rand(8188, 20)
        self.z_b = scanner.N64_Z64_MAGIC + _rand(8188, 21)
        self.n64 = DatFile(N64, "", "", [
            ni_rom("Echo (USA).z64", self.z_a, N64),
            ni_rom("Foxtrot (Europe).z64", self.z_b, N64),
        ])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _root(self, name: str) -> Path:
        d = self.base / name
        d.mkdir()
        return d

    def _scan(self, root: Path, dat: DatFile, alt: tuple[str, ...]):
        with mock.patch.object(scanner, "find_7z", return_value=None):
            return scanner.scan(root, dat, cache_path=self.cache, alt_hashes=alt, layout="flat")

    def _snes_root(self) -> Path:
        r = self._root("snes")
        (r / "sub").mkdir()
        (r / "sub" / "alpha.smc").write_bytes(_rand(512, 1) + self.s_a)
        with zipfile.ZipFile(r / "bravo.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bravo.smc", _rand(512, 2) + self.s_b)
        with zipfile.ZipFile(r / "multi.zip", "w") as zf:
            zf.writestr("charlie.smc", _rand(512, 3) + self.s_c)
            zf.writestr("readme.txt", b"hi")
        (r / "_unmatched").mkdir()
        (r / "_unmatched" / "delta.smc").write_bytes(_rand(512, 4) + self.s_old)
        (r / "Delta (USA) (Rev 1).smc").write_bytes(_rand(512, 5) + self.s_new)
        return r

    def test_plan(self) -> None:
        r = self._snes_root()
        res = self._scan(r, self.snes, ("snes_header",))
        ops = {op.src.relative_to(r).as_posix(): op for op in convert.plan_conversions(res)}
        self.assertEqual(sorted(ops), ["Delta (USA) (Rev 1).smc", "_unmatched/delta.smc", "bravo.zip",
                                       "multi.zip", "sub/alpha.smc"])
        a = ops["sub/alpha.smc"]
        self.assertEqual((a.status, a.transform, a.via, a.rom_name), ("convert", "strip512", "headerless",
                                                                      "Alpha (USA).sfc"))
        self.assertEqual(a.dst, r / "Alpha (USA).sfc")  # flat layout
        self.assertEqual(a.original_dst, r / "_converted_originals" / "sub" / "alpha.smc")
        self.assertEqual((a.expected_sha1, a.expected_size), (hashlib.sha1(self.s_a).hexdigest(), len(self.s_a)))
        b = ops["bravo.zip"]
        self.assertEqual((b.status, b.member, b.dst), ("convert", "bravo.smc", r / "Bravo (Europe).zip"))
        self.assertEqual(ops["multi.zip"].status, "skip")
        self.assertIn("2 members", ops["multi.zip"].reason)
        # leading _unmatched/ is dropped from the originals path
        self.assertEqual(ops["_unmatched/delta.smc"].original_dst,
                         r / "_converted_originals" / "delta.smc")
        self.assertEqual(convert.convert_counts(ops.values()), {"convert": 4, "conflict": 0, "skip": 1})
        # per_dat layout puts the clean file into the DAT folder
        per = {op.src.name: op for op in convert.plan_conversions(res, layout="per_dat")}
        self.assertEqual(per["alpha.smc"].dst, r / organiser.dat_folder_name(SNES) / "Alpha (USA).sfc")

    def test_files_set_aside_by_build_library_are_not_converted_back(self) -> None:
        r = self._root("snes_reasons")
        for i, reason in enumerate(("_excluded", "_superseded", "_incomplete", "_duplicates")):
            d = r / "_unmatched" / reason / "x"
            d.mkdir(parents=True)
            (d / f"a{i}.smc").write_bytes(_rand(512, 30 + i) + self.s_a)
        (r / "plain.smc").write_bytes(_rand(512, 40) + self.s_b)
        res = self._scan(r, self.snes, ("snes_header",))
        ops = convert.plan_conversions(res)
        self.assertEqual([op.src.name for op in ops], ["plain.smc"])
        self.assertEqual(res.summary()["convertible"], 1)

    def test_plan_latest_only_and_conflicts(self) -> None:
        r = self._snes_root()
        (r / "Alpha (USA).sfc").write_bytes(b"something else")    # clean name taken
        res = self._scan(r, self.snes, ("snes_header",))
        ops = {op.src.name: op for op in convert.plan_conversions(res, latest_only=True)}
        self.assertEqual(ops["alpha.smc"].status, "conflict")
        try:
            from romorg import tags  # noqa: F401
        except ImportError:
            return
        self.assertEqual(ops["delta.smc"].status, "skip")
        self.assertIn("superseded by Delta (USA) (Rev 1).sfc", ops["delta.smc"].reason)
        self.assertEqual(ops["Delta (USA) (Rev 1).smc"].status, "convert")
        # two headered copies of one rom -> the second conflicts
        (r / "_unmatched" / "delta.smc").unlink()
        (r / "alpha2.smc").write_bytes(_rand(512, 9) + self.s_a)
        (r / "Alpha (USA).sfc").unlink()
        res = self._scan(r, self.snes, ("snes_header",))
        st = sorted((op.src.name, op.status) for op in convert.plan_conversions(res)
                    if op.rom_name == "Alpha (USA).sfc")
        self.assertEqual(st, [("alpha.smc", "conflict"), ("alpha2.smc", "convert")])

    def test_apply_and_undo_snes(self) -> None:
        r = self._snes_root()
        before = tree(r)
        res = self._scan(r, self.snes, ("snes_header",))
        ops = convert.plan_conversions(res)
        progress = []
        out = convert.apply_conversions(ops, r, progress=lambda *a: progress.append(a))
        self.assertEqual((out["converted"], out["failed"], out["cancelled"], out["error"]), (4, [], False, None))
        self.assertEqual(progress[-1], (4, 4, ""))
        self.assertEqual(sha1(r / "Alpha (USA).sfc"), hashlib.sha1(self.s_a).hexdigest())
        self.assertEqual(sha1(r / "Delta (USA) (Rev 1).sfc"), hashlib.sha1(self.s_new).hexdigest())
        with zipfile.ZipFile(r / "Bravo (Europe).zip") as zf:
            self.assertEqual(zf.namelist(), ["Bravo (Europe).sfc"])
            self.assertEqual(zf.read("Bravo (Europe).sfc"), self.s_b)
        orig = r / "_converted_originals"
        self.assertEqual(sha1(orig / "sub" / "alpha.smc"), before["sub/alpha.smc"])  # original kept, untouched
        self.assertEqual(sha1(orig / "bravo.zip"), before["bravo.zip"])
        self.assertTrue((r / "multi.zip").exists())
        self.assertFalse((r / "sub").exists())  # emptied by the move
        self.assertEqual(out["removed_dirs"], [str(r / "sub")])
        # the undo log records move then create for each file
        lines = [json.loads(x) for x in Path(out["undo_log"]).read_text().splitlines()]
        ops_seq = [x.get("op") for x in lines[1:]]
        self.assertEqual(ops_seq.count("create"), 4)
        first = [x for x in lines if x.get("op") in ("move", "create")][:2]
        self.assertEqual([x["op"] for x in first], ["move", "create"])
        self.assertTrue(all("sha1" in x and "size" in x for x in lines if x.get("op") == "create"))

        # re-scan: clean files match raw, originals are not offered again
        res2 = self._scan(r, self.snes, ("snes_header",))
        self.assertEqual([op.status for op in convert.plan_conversions(res2)], ["skip"])  # multi.zip
        self.assertEqual(res2.summary()["convertible"], 0)  # only multi.zip, which is skipped
        via = {m.entry.rel: m.matched_via for m in res2.matched}
        self.assertEqual(via["Alpha (USA).sfc"], "raw")
        # the kept originals are not duplicates / renames / alternate matches to act on
        s2 = res2.summary()
        self.assertEqual(s2["converted_originals"], 4)
        self.assertEqual((s2["duplicates"], s2["to_rename"]), (0, 0))
        self.assertEqual(s2["matched_via"]["headerless"], 1)  # only multi.zip's member
        self.assertEqual(s2["correctly_placed"], s2["matched_files"] - 1)  # multi.zip can't be placed

        undo = organiser.undo(Path(out["undo_log"]))
        self.assertEqual(undo.get("created_removed"), 4)
        self.assertEqual(tree(r), before)

    def test_apply_n64_both_orders(self) -> None:
        r = self._root("n64")
        (r / "Echo (USA).v64").write_bytes(swap16(self.z_a))
        with zipfile.ZipFile(r / "fox.zip", "w") as zf:
            zf.writestr("fox.n64", swap32(self.z_b))
        before = tree(r)
        res = self._scan(r, self.n64, ("n64_byteorder",))
        ops = convert.plan_conversions(res)
        self.assertEqual(sorted((op.src.name, op.transform) for op in ops),
                         [("Echo (USA).v64", "swap16"), ("fox.zip", "swap32")])
        out = convert.apply_conversions(ops, r)
        self.assertEqual(out["converted"], 2, out)
        self.assertEqual((r / "Echo (USA).z64").read_bytes(), self.z_a)
        with zipfile.ZipFile(r / "Foxtrot (Europe).zip") as zf:
            self.assertEqual(zf.read("Foxtrot (Europe).z64"), self.z_b)
        self.assertEqual(organiser.undo(Path(out["undo_log"])).get("created_removed"), 2)
        self.assertEqual(tree(r), before)

    def test_raw_v64_of_dat_converts_to_z64(self) -> None:
        # the N64 DAT lists both "<set>.z64" and "<set>.v64" for some sets
        dat = DatFile(N64, "", "", [ni_rom("Echo (USA).z64", self.z_a, N64),
                                    ni_rom("Echo (USA).v64", swap16(self.z_a), N64),
                                    ni_rom("Foxtrot (Europe).v64", swap16(self.z_b), N64)])  # no .z64
        r = self._root("rawv64")
        (r / "Echo (USA).v64").write_bytes(swap16(self.z_a))
        (r / "Foxtrot (Europe).v64").write_bytes(swap16(self.z_b))
        res = self._scan(r, dat, ("n64_byteorder",))
        self.assertEqual({m.matched_via for m in res.matched}, {"raw"})
        ops = convert.plan_conversions(res)
        self.assertEqual([(op.src.name, op.dst.name, op.via, op.transform, op.status) for op in ops],
                         [("Echo (USA).v64", "Echo (USA).z64", "raw", "swap16", "convert")])
        self.assertEqual(res.summary()["convertible"], 1)
        out = convert.apply_conversions(ops, r)
        self.assertEqual(out["converted"], 1, out)
        self.assertEqual((r / "Echo (USA).z64").read_bytes(), self.z_a)

    def test_raw_v64_latest_only(self) -> None:
        dat = DatFile(N64, "", "", [ni_rom("Echo (USA).z64", self.z_a, N64),
                                    ni_rom("Echo (USA).v64", swap16(self.z_a), N64),
                                    ni_rom("Echo (USA) (Rev 1).z64", self.z_b, N64),
                                    ni_rom("Echo (USA) (Rev 1).v64", swap16(self.z_b), N64)])
        r = self._root("rawv64latest")
        (r / "Echo (USA).v64").write_bytes(swap16(self.z_a))
        (r / "Echo (USA) (Rev 1).v64").write_bytes(swap16(self.z_b))
        ops = {op.src.name: op for op in convert.plan_conversions(self._scan(r, dat, ("n64_byteorder",)),
                                                                   latest_only=True)}
        self.assertEqual(ops["Echo (USA) (Rev 1).v64"].status, "convert")
        self.assertEqual((ops["Echo (USA).v64"].status, ops["Echo (USA).v64"].reason),
                         ("skip", "older version - superseded by Echo (USA) (Rev 1).v64"))

    def test_output_hashed_once_more_only(self) -> None:
        # source read + one verifying re-read of the written file; no extra hash for the journal
        r = self._root("io")
        (r / "alpha.smc").write_bytes(_rand(512, 1) + self.s_a)
        ops = convert.plan_conversions(self._scan(r, self.snes, ("snes_header",)))
        real = scanner.hash_file
        with mock.patch.object(scanner, "hash_file", side_effect=real) as h:
            self.assertEqual(convert.apply_conversions(ops, r)["converted"], 1)
        self.assertEqual(h.call_count, 1)

    def test_interrupted_convert_leftover(self) -> None:
        import os
        r = self._root("killed")
        data = _rand(512, 1) + self.s_a
        (r / "alpha.smc").write_bytes(data)
        ops = convert.plan_conversions(self._scan(r, self.snes, ("snes_header",)))
        real_write = convert._write_loose

        def dying_write(op, original, tmp):  # the process dies half-way through the write
            tmp.write_bytes(self.s_a[:1000])
            os._exit(0)

        pid = os.fork()
        if pid == 0:  # child
            try:
                with mock.patch.object(convert, "_write_loose", side_effect=dying_write):
                    convert.apply_conversions(ops, r)
            finally:
                os._exit(1)
        os.waitpid(pid, 0)
        self.assertIs(convert._write_loose, real_write)
        leftovers = [p for p in r.iterdir() if convert.CONVERT_TEMP_MARKER in p.name]
        self.assertEqual(len(leftovers), 1)
        self.assertTrue(leftovers[0].name.startswith("Alpha (USA).sfc"))
        # the scan explains it (never "rename it back")
        res = self._scan(r, self.snes, ("snes_header",))
        msg = dict((p.name, m) for p, m in res.errors)[leftovers[0].name]
        self.assertIn("interrupted convert", msg)
        self.assertNotIn("rename it", msg)
        # the case-only-rename recovery never takes a convert temp for an original
        self.assertFalse(organiser._restore_from_temp(r / "Alpha (USA).sfc"))
        logs = organiser.list_undo_logs(r)
        self.assertEqual(len(logs), 1)
        self.assertEqual(len(organiser.read_undo_log(logs[0])["temp_files"]), 1)
        u = organiser.undo(logs[0])
        self.assertEqual((u["restored"], u["temps_removed"], u["failed"]), (1, 1, []))
        self.assertEqual(tree(r), {"alpha.smc": hashlib.sha1(data).hexdigest()})

    def test_same_name_in_place(self) -> None:
        # A headered file already named like the clean rom: original moves away, clean file takes the name.
        r = self._root("inplace")
        data = _rand(512, 7) + self.s_a
        (r / "Alpha (USA).sfc").write_bytes(data)
        res = self._scan(r, self.snes, ("snes_header",))
        ops = convert.plan_conversions(res)
        self.assertEqual([(op.status, op.dst) for op in ops], [("convert", r / "Alpha (USA).sfc")])
        out = convert.apply_conversions(ops, r)
        self.assertEqual(out["converted"], 1)
        self.assertEqual((r / "Alpha (USA).sfc").read_bytes(), self.s_a)
        self.assertEqual((r / "_converted_originals" / "Alpha (USA).sfc").read_bytes(), data)
        organiser.undo(Path(out["undo_log"]))
        self.assertEqual((r / "Alpha (USA).sfc").read_bytes(), data)
        self.assertFalse((r / "_unmatched").exists() and any((r / "_unmatched").rglob("*.sfc")))

    def test_hash_mismatch_aborts_op_and_restores_original(self) -> None:
        r = self._root("bad")
        (r / "alpha.smc").write_bytes(_rand(512, 1) + self.s_a)
        (r / "echo.v64").write_bytes(swap16(self.z_a))
        before = tree(r)
        res = self._scan(r, self.snes, ("snes_header",))
        res_n64 = self._scan(r, self.n64, ("n64_byteorder",))
        ops = convert.plan_conversions(res) + convert.plan_conversions(res_n64)
        self.assertEqual(len(ops), 2)
        ops[0].expected_sha1 = "0" * 40  # pretend the DAT says something else
        out = convert.apply_conversions(ops, r)
        self.assertEqual(out["converted"], 1)
        self.assertEqual(len(out["failed"]), 1)
        self.assertIn("does not match the DAT", out["failed"][0]["error"])
        self.assertEqual(sha1(r / "alpha.smc"), before["alpha.smc"])  # original back in place
        self.assertFalse((r / "Alpha (USA).sfc").exists())
        self.assertEqual([p.name for p in r.iterdir() if scanner.TEMP_MARKER in p.name], [])
        self.assertFalse((r / "_converted_originals" / "alpha.smc").exists())
        organiser.undo(Path(out["undo_log"]))
        self.assertEqual(tree(r), before)

    def test_written_file_verified_from_disk(self) -> None:
        r = self._root("disk")
        (r / "alpha.smc").write_bytes(_rand(512, 1) + self.s_a)
        before = tree(r)
        res = self._scan(r, self.snes, ("snes_header",))
        ops = convert.plan_conversions(res)
        real = scanner.hash_file
        with mock.patch.object(scanner, "hash_file", side_effect=lambda p: ("00000000", "f" * 40)
                               if convert.CONVERT_TEMP_MARKER in Path(p).name else real(p)):
            out = convert.apply_conversions(ops, r)
        self.assertEqual(out["converted"], 0)
        self.assertIn("written file", out["failed"][0]["error"])
        self.assertEqual(tree(r), before)
        self.assertIsNotNone(out["undo_log"])  # the round trip is logged

    def test_crc_only_dat(self) -> None:
        r = self._root("crc")
        (r / "alpha.smc").write_bytes(_rand(512, 1) + self.s_a)
        dat = DatFile(SNES, "", "", [ni_rom("Alpha (USA).sfc", self.s_a, SNES, sha1=False)])
        res = self._scan(r, dat, ("snes_header",))
        ops = convert.plan_conversions(res)
        self.assertEqual([op.status for op in ops], ["convert"])
        out = convert.apply_conversions(ops, r)
        self.assertEqual(out["converted"], 1)

    def test_nes_and_7z_not_converted(self) -> None:
        r = self._root("nes")
        body = _rand(4096, 3)
        hdr = b"NES\x1a" + bytes(12)
        dat = DatFile(NES, "", "", [ni_rom("Hotel (USA).nes", b"NES\x1a" + bytes([1]) + bytes(11) + body, NES),
                                    ni_rom("Hotel (USA).unh", body, NES)])
        (r / "hotel.nes").write_bytes(hdr + body)
        res = self._scan(r, dat, ("nes_header",))
        self.assertEqual([m.matched_via for m in res.matched], ["headerless"])
        self.assertEqual(convert.plan_conversions(res), [])
        # a 7z member matched headerless -> skip ("extract the archive first")
        r2 = self._root("seven")
        (r2 / "alpha.7z").write_bytes(b"7z")
        e = scanner.Entry(r2 / "alpha.7z", "alpha.smc", 512 + len(self.s_a), "00000000", None, r2)
        m = scanner.Match(e, [self.snes.roms[0]], matched_via="headerless", header=512)
        res7 = scanner.ScanResult(r2, [SNES], [m], [], [], [], [], layout="flat")
        ops = convert.plan_conversions(res7)
        self.assertEqual([(op.status, op.reason) for op in ops],
                         [("skip", "extract the archive first (only .zip archives are converted)")])

    def test_cancel_between_files(self) -> None:
        r = self._snes_root()
        res = self._scan(r, self.snes, ("snes_header",))
        ops = convert.plan_conversions(res)
        calls = []

        def cancel() -> bool:
            calls.append(1)
            return len(calls) > 1

        out = convert.apply_conversions(ops, r, cancel=cancel)
        self.assertTrue(out["cancelled"])
        self.assertEqual(out["converted"], 1)
        self.assertIsNotNone(out["undo_log"])

    def test_nothing_to_do(self) -> None:
        r = self._root("empty")
        out = convert.apply_conversions([], r)
        self.assertEqual((out["converted"], out["undo_log"]), (0, None))


if __name__ == "__main__":
    unittest.main()
