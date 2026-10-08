"""GameCube support: the RVZ reader, and a flat Redump platform whose files may be ``.iso`` or ``.rvz``.

The padding generator is checked against ``rvztestlib.reference_junk`` (written from the spec, sharing no code with
it); the container against files made by ``rvztestlib.build_rvz``; and, when the file is there, against a real
Dolphin-made RVZ whose rebuilt ISO must equal the Redump entry (``ROMORG_REAL_GC_DIR`` names the folder).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import struct
import sys
import tempfile
import threading
import unittest
import urllib.request
import zlib
from pathlib import Path
from unittest import mock
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

sys.path.insert(0, os.path.dirname(__file__))

import rvztestlib as R  # noqa: E402
from romorg import chdsched, organiser, paths, platforms, rvz, scanner, server, zstdnative  # noqa: E402

REAL_DIR = Path(os.environ.get("ROMORG_REAL_GC_DIR") or "/home/deck/MEGA/Emulation/roms/wii and gamecube/gamecube")
SUNSHINE = REAL_DIR / "Super Mario Sunshine (USA, Canada).rvz"
SUNSHINE_REDUMP = ("771ad977", "8d094f2c5c112aba9660f0478b16c7f2caf63cbf", 1459978240)
PLAT = "Nintendo GameCube"
DAT = "Nintendo - GameCube"
CHUNK = 0x20000
needs_zstd = unittest.skipUnless(zstdnative.can_compress(), "no Zstandard library")


def disc(seed: int, tail: int = 0x3000):
    """A 0x83000-byte disc with data, a zero chunk, padding runs (inside one 32 KiB block each) and a short last chunk."""
    rnd = random.Random(seed)
    regions = [("data", rnd.randbytes(CHUNK)), ("zero", CHUNK), ("data", rnd.randbytes(0x1000)),
               ("junk", R.seed_for(seed * 10 + 1), 0x7000), ("data", rnd.randbytes(0x8000)),
               ("junk", R.seed_for(seed * 10 + 2), 0x8000), ("junk", R.seed_for(seed * 10 + 3), 0x8000),
               ("data", rnd.randbytes(0x100)), ("junk", R.seed_for(seed * 10 + 4), 0x7F00),
               ("data", rnd.randbytes(0x8000)), ("zero", 0x10000), ("data", rnd.randbytes(tail))]
    return R.make_disc(regions)


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class SelfCheckTest(unittest.TestCase):
    def test_the_selfcheck_image(self) -> None:
        from romorg import selfcheck
        self.assertEqual(selfcheck.check_rvz()[0], True)
        with mock.patch.dict(os.environ, {zstdnative.ENV_OFF: "1"}):
            zstdnative.reload()
            try:
                ok, text = selfcheck.check_rvz()
            finally:
                zstdnative.reload()
        self.assertTrue(ok, text)
        self.assertIn("Python Zstandard decoder", text)


class JunkTest(unittest.TestCase):
    def test_the_fast_generator_equals_the_one_written_from_the_spec(self) -> None:
        rnd = random.Random(1)
        for _ in range(40):
            seed = rnd.randbytes(68)
            off = rnd.choice([0, 0x8000, 0x10000, 0x7FFF, 1, rnd.randrange(0, 1 << 30)])
            size = rnd.choice([1, 3, 4, 5, 2083, 2084, 2085, 4168, 20000, 0x8000, 70000])
            self.assertEqual(rvz.junk(seed, off, size), R.reference_junk(seed, off, size), (off, size))

    def test_offset_only_skips(self) -> None:
        seed = R.seed_for(5)
        whole = rvz.junk(seed, 0, 0x9000)
        self.assertEqual(rvz.junk(seed, 0x123, 100), whole[0x123:0x123 + 100])
        self.assertEqual(rvz.junk(seed, 0x8000 + 0x123, 100), whole[0x123:0x123 + 100])    # the block, not the offset

    def test_seed_length(self) -> None:
        with self.assertRaises(rvz.RvzError):
            rvz.junk(b"x" * 67, 0, 10)

    def test_unpack(self) -> None:
        seed = R.seed_for(9)
        packed = struct.pack(">I", 3) + b"abc" + struct.pack(">I", 10 | 0x80000000) + seed + struct.pack(">I", 2) + b"zz"
        want = b"abc" + R.reference_junk(seed, 0x8000 + 3, 10) + b"zz"
        self.assertEqual(rvz.unpack(packed, 0x8000, len(want)), want)
        for bad, why in ((packed[:-1], "truncated"), (packed[:9], "truncated"), (packed + b"\0", "truncated")):
            with self.assertRaises(rvz.RvzError, msg=why):
                rvz.unpack(bad, 0, len(want))
        with self.assertRaises(rvz.RvzError):
            rvz.unpack(packed, 0, len(want) + 1)


class ContainerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.iso, self.runs = disc(1)

    def build(self, name="a.rvz", **kw) -> Path:
        p = self.dir / name
        R.build_rvz(p, self.iso, self.runs, chunk=kw.pop("chunk", CHUNK), **kw)
        return p

    def check_roundtrip(self, p: Path) -> None:
        with rvz.Rvz(p) as r:
            self.assertEqual((r.iso_size, r.disc_type, r.pieces), (len(self.iso), 1, 5))
            self.assertEqual([(o, n) for o, n, _g in r.layout], [(i * CHUNK, min(CHUNK, len(self.iso) - i * CHUNK))
                                                                 for i in range(5)])
            self.assertEqual(b"".join(r.iter_image()), self.iso)
            for i in (3, 0, 4, 2, 1):                     # any piece on its own
                o, n, _g = r.layout[i]
                self.assertEqual(r.piece(i), self.iso[o:o + n], i)
            self.assertEqual(r.read_pieces(1, 3), self.iso[CHUNK:4 * CHUNK])
        self.assertEqual(b"".join(rvz.iter_image(p)), self.iso)

    def test_stored_without_compression(self) -> None:
        stats = R.build_rvz(self.dir / "n.rvz", self.iso, self.runs, chunk=CHUNK, compression=R.NONE)
        self.assertEqual((stats["zero"], stats["packed"] > 0, stats["plain"] > 0), (1, True, True))
        self.check_roundtrip(self.dir / "n.rvz")

    @needs_zstd
    def test_zstandard_groups_and_tables(self) -> None:
        self.check_roundtrip(self.build())

    @needs_zstd
    def test_groups_stored_plain_inside_a_zstandard_file(self) -> None:
        self.check_roundtrip(self.build(store_compressed=False))

    @needs_zstd
    def test_other_chunk_sizes(self) -> None:
        for chunk in (0x8000, 0x40000, 0x200000):
            with self.subTest(chunk=chunk):
                iso, runs = disc(2)
                p = self.dir / f"c{chunk}.rvz"
                R.build_rvz(p, iso, runs, chunk=chunk)
                with rvz.Rvz(p) as r:
                    self.assertEqual(b"".join(r.iter_image()), iso)
                    self.assertEqual(r.pieces, -(-len(iso) // chunk))

    def test_header_bytes_come_from_the_disc_struct(self) -> None:
        p = self.build(compression=R.NONE)
        with rvz.Rvz(p) as r:
            self.assertEqual(r.dhead, self.iso[:0x80])
            self.assertEqual(r.piece(0)[:0x80], self.iso[:0x80])

    def test_other_compression_methods_decode(self) -> None:
        import bz2
        import lzma
        data = random.Random(4).randbytes(5000) + bytes(5000)
        self.assertEqual(rvz._decompress(rvz.BZIP2, bz2.compress(data), len(data), b""), data)
        filt = [{"id": lzma.FILTER_LZMA1, "lc": 3, "lp": 0, "pb": 2, "dict_size": 1 << 16}]
        comp = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=filt)
        blob = comp.compress(data) + comp.flush()
        props = bytes([(2 * 5 + 0) * 9 + 3]) + struct.pack("<I", 1 << 16)
        self.assertEqual(rvz._decompress(rvz.LZMA, blob, len(data), props), data)
        filt2 = [{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 16}]
        comp = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=filt2)
        blob = comp.compress(data) + comp.flush()
        self.assertEqual(rvz._decompress(rvz.LZMA2, blob, len(data), bytes([2 * 6 - 1 + 1 - 1 - 0])), data)
        with self.assertRaises(rvz.RvzUnsupported):
            rvz._decompress(1, b"", 0, b"")
        with self.assertRaises(rvz.RvzError):
            rvz._decompress(rvz.BZIP2, b"not bzip2", 10, b"")

    # -- hashing
    def test_hash_image_sequential_and_with_worker_processes(self) -> None:
        p = self.build(compression=R.NONE)
        want = ("%08x" % (zlib.crc32(self.iso) & 0xFFFFFFFF), sha1(self.iso), len(self.iso))
        seen = []
        self.assertEqual(rvz.hash_image(p, workers=1, progress=lambda d, t: seen.append((d, t))), want)
        self.assertEqual(seen[-1], (len(self.iso), len(self.iso)))
        with mock.patch.object(rvz, "SLICE_BYTES", CHUNK):               # one worker request per chunk
            self.assertEqual(rvz.hash_image(p, workers=3), want)

    def test_a_worker_that_cannot_start_or_dies_is_replaced_by_this_process(self) -> None:
        p = self.build(compression=R.NONE)
        want = ("%08x" % (zlib.crc32(self.iso) & 0xFFFFFFFF), sha1(self.iso), len(self.iso))
        real = chdsched.spawn_worker

        def dying():
            proc = real()
            proc.stdin.close()
            return proc
        with mock.patch.object(rvz, "SLICE_BYTES", CHUNK):
            with mock.patch.object(chdsched, "spawn_worker", side_effect=chdsched.PoolError("no")):
                self.assertEqual(rvz.hash_image(p, workers=2), want)
            with mock.patch.object(chdsched, "spawn_worker", side_effect=dying):
                self.assertEqual(rvz.hash_image(p, workers=2), want)

    def test_cancel(self) -> None:
        p = self.build(compression=R.NONE)
        with self.assertRaises(InterruptedError):
            rvz.hash_image(p, workers=1, cancel=lambda: True)
        with mock.patch.object(rvz, "SLICE_BYTES", CHUNK):
            with self.assertRaises(InterruptedError):
                rvz.hash_image(p, workers=2, cancel=lambda: True)

    def test_a_damaged_group_is_an_error_in_every_mode(self) -> None:
        p = self.build(compression=R.NONE, store_compressed=False)
        raw = bytearray(p.read_bytes())
        with rvz.Rvz(p) as r:
            off4, _size, _packed = r.groups[r.layout[2][2]]          # the packed chunk
        raw[off4 * 4 + 5] ^= 0xFF                                    # inside the first literal run's size field
        raw[off4 * 4 + 1] ^= 0x7F
        (self.dir / "bad.rvz").write_bytes(bytes(raw))
        with self.assertRaises(rvz.RvzError):
            rvz.hash_image(self.dir / "bad.rvz", workers=1)
        with mock.patch.object(rvz, "SLICE_BYTES", CHUNK):
            with self.assertRaises(rvz.RvzError):
                rvz.hash_image(self.dir / "bad.rvz", workers=2)


class RefusalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.iso, self.runs = disc(1)

    def test_not_an_rvz(self) -> None:
        (self.dir / "x.rvz").write_bytes(b"hello" * 100)
        self.assertFalse(rvz.is_rvz(self.dir / "x.rvz"))
        with self.assertRaises(rvz.RvzError):
            rvz.Rvz(self.dir / "x.rvz")
        self.assertFalse(rvz.is_rvz(self.dir / "missing.rvz"))

    def test_implausible_sizes_are_refused_before_any_table_is_read(self) -> None:
        p = self.dir / "ok.rvz"
        R.build_rvz(p, self.iso, self.runs, compression=R.NONE)
        with mock.patch.object(rvz, "MAX_ISO", len(self.iso) - 1), self.assertRaises(rvz.RvzError):
            rvz.Rvz(p)
        with mock.patch.object(rvz, "MAX_CHUNK", CHUNK // 2), self.assertRaises(rvz.RvzError):
            rvz.Rvz(p)
        with mock.patch.object(rvz.Rvz, "_table", side_effect=AssertionError("read a table")):
            with mock.patch.object(rvz, "MAX_ISO", 1):
                with self.assertRaises(rvz.RvzError):
                    rvz.Rvz(p)
        rvz.Rvz(p).close()

    def test_wii_wia_and_damage(self) -> None:
        R.build_rvz(self.dir / "wii.rvz", self.iso, self.runs, compression=R.NONE, disc_type=2)
        with self.assertRaises(rvz.RvzUnsupported) as cm:
            rvz.Rvz(self.dir / "wii.rvz")
        self.assertIn("Wii", str(cm.exception))
        R.build_rvz(self.dir / "wia.rvz", self.iso, self.runs, compression=R.NONE, magic=b"WIA\x01")
        with self.assertRaises(rvz.RvzUnsupported):
            rvz.Rvz(self.dir / "wia.rvz")
        R.build_rvz(self.dir / "ok.rvz", self.iso, self.runs, compression=R.NONE)
        raw = (self.dir / "ok.rvz").read_bytes()
        for name, blob in (("short", raw[:100]), ("table", raw[:len(raw) - 30]),
                           ("head", raw[:0x30] + bytes([raw[0x30] ^ 1]) + raw[0x31:]),
                           ("disc", raw[:0x60] + bytes([raw[0x60] ^ 1]) + raw[0x61:])):
            (self.dir / f"{name}.rvz").write_bytes(blob)
            with self.assertRaises(rvz.RvzError, msg=name):
                rvz.Rvz(self.dir / f"{name}.rvz")

    def test_ranges_must_add_up(self) -> None:
        R.build_rvz(self.dir / "ok.rvz", self.iso, self.runs, compression=R.NONE)
        raw = bytearray((self.dir / "ok.rvz").read_bytes())
        raw[0x24:0x2C] = struct.pack(">Q", len(self.iso) + 0x8000)        # the header lies about the disc size ...
        raw[0x34:0x48] = hashlib.sha1(raw[:0x34]).digest()                # ... and its checksum is right
        (self.dir / "size.rvz").write_bytes(bytes(raw))
        with self.assertRaises(rvz.RvzError):
            rvz.Rvz(self.dir / "size.rvz")


@unittest.skipUnless(SUNSHINE.is_file(), "the real Super Mario Sunshine RVZ is not here (ROMORG_REAL_GC_DIR)")
class RealFileTest(unittest.TestCase):
    def test_the_rebuilt_iso_is_the_redump_entry(self) -> None:
        with rvz.Rvz(SUNSHINE) as r:
            self.assertEqual((r.disc_type, r.compression, r.chunk_size, r.iso_size), (1, rvz.ZSTD, 131072, SUNSHINE_REDUMP[2]))
            self.assertEqual(r.dhead[:6], b"GMSE01")
        self.assertEqual(rvz.hash_image(SUNSHINE), (SUNSHINE_REDUMP[0], SUNSHINE_REDUMP[1], SUNSHINE_REDUMP[2]))

    def test_one_process_gives_the_same_answer(self) -> None:
        self.assertEqual(rvz.hash_image(SUNSHINE, workers=1)[1], SUNSHINE_REDUMP[1])


# --------------------------------------------------------------------------- the platform
def write_dat(folder: Path, games: list) -> None:
    rows = []
    for name, data in games:
        rows.append(f'<game name="{name}"><category>Games</category><description>{name}</description>'
                    f'<rom name="{name}.iso" size="{len(data)}" crc="{zlib.crc32(data) & 0xFFFFFFFF:08x}" '
                    f'md5="{hashlib.md5(data).hexdigest()}" sha1="{sha1(data)}"/></game>')
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{DAT}.dat").write_text(
        '<?xml version="1.0"?><datafile><header><name>' + DAT + '</name><version>2026-06-13 18-14-01</version></header>'
        + "".join(rows) + "</datafile>", encoding="utf-8")


class PlatformTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        p = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.base / "data"), "ROMORG_OFFLINE": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.root = self.base / "gc"
        (self.root / "sub").mkdir(parents=True)
        self.a, self.a_runs = disc(1)
        self.b, _ = disc(2)
        self.c, self.c_runs = disc(3)
        self.g, _ = disc(4)
        write_dat(paths.redump_dir(), [("Alpha (USA)", self.a), ("Beta (Europe) (En,Fr,De)", self.b),
                                       ("Gamma (Japan)", self.g)])
        R.build_rvz(self.root / "a.rvz", self.a, self.a_runs, compression=R.NONE)
        (self.root / "sub" / "b.iso").write_bytes(self.b)
        R.build_rvz(self.root / "c.rvz", self.c, self.c_runs, compression=R.NONE)
        R.build_rvz(self.root / "wii.rvz", self.a, self.a_runs, compression=R.NONE, disc_type=2)
        raw = (self.root / "a.rvz").read_bytes()
        (self.root / "cut.rvz").write_bytes(raw[:len(raw) - 40])
        (self.root / "fake.rvz").write_bytes(b"x" * 5000)
        self.plat = platforms.get_platform(PLAT)

    def tree(self) -> dict:
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and not p.name.startswith(".")}

    def scan(self, **kw):
        dats, missing = platforms.load_platform_dats(self.plat)
        self.assertEqual((missing, [d.name for d in dats]), ([], [DAT]))
        kw.setdefault("use_cache", False)
        return scanner.scan(self.root, dats, alt_hashes=self.plat.alt_hashes, layout=self.plat.layout,
                            containers=self.plat.containers, **kw)

    def test_container_and_plain_hashes_do_not_share_cache_rows(self) -> None:
        dats, _ = platforms.load_platform_dats(self.plat)
        cache = self.base / "hashes.sqlite"

        def run(containers):
            return scanner.scan(self.root, dats, alt_hashes=self.plat.alt_hashes, layout=self.plat.layout,
                                containers=containers, use_cache=True, cache_path=cache)
        run(())                                                     # e.g. the folder was scanned under another system
        r = run(("rvz",))
        self.assertIn("a.rvz", [m.unit.rel if hasattr(m, "unit") else Path(m.entry.path).name for m in r.matched]
                      + [Path(m.entry.path).name for m in r.matched])
        r = run(())
        self.assertNotIn("a.rvz", [Path(m.entry.path).name for m in r.matched])

    def test_a_plain_scan_does_not_poison_the_container_scan(self) -> None:
        cache = self.base / "hash.db"
        plain = scanner.scan(self.root, platforms.load_platform_dats(self.plat)[0], containers=(),
                             use_cache=True, cache_path=cache)
        self.assertNotIn("a.rvz", [m.entry.path.name for m in plain.matched])
        res = self.scan(use_cache=True, cache_path=cache)
        self.assertIn("a.rvz", [m.entry.path.name for m in res.matched])
        again = scanner.scan(self.root, platforms.load_platform_dats(self.plat)[0], containers=(),
                             use_cache=True, cache_path=cache)
        self.assertNotIn("a.rvz", [m.entry.path.name for m in again.matched])

    def test_the_platform(self) -> None:
        p = self.plat
        self.assertEqual((p.source, p.layout, p.convertible, p.folder_hint, p.containers),
                         ("redump", "flat", False, "gc", ("rvz",)))
        self.assertEqual(p.extensions, (".rvz", ".iso", ".gcm"))
        from romorg import redump
        self.assertEqual(redump.dat_url(DAT), "http://redump.org/datfile/gc/")

    def test_scan(self) -> None:
        res = self.scan()
        by = {m.entry.path.name: m for m in res.matched}
        self.assertEqual(sorted(by), ["a.rvz", "b.iso"])
        self.assertEqual((by["a.rvz"].matched_via, by["a.rvz"].container), ("container", "rvz"))
        self.assertEqual(by["a.rvz"].entry.size, len(self.a))
        self.assertEqual(by["b.iso"].matched_via, "raw")
        self.assertEqual(sorted(e.path.name for e in res.unmatched), ["c.rvz", "fake.rvz"])   # fake.rvz: hashed as a file
        self.assertEqual([p.name for p in res.unsupported], ["wii.rvz"])
        self.assertEqual([(p.name, m.split(":")[0]) for p, m in res.errors], [("cut.rvz", "RvzError")])
        s = res.summary()
        self.assertEqual((s["games_total"], s["games_have"], s["games_missing"]), (3, 2, 1))
        self.assertEqual(s["matched_via"], {"raw": 1, "headerless": 0, "byteswapped": 0, "container": 1})
        self.assertEqual([r.name for r in res.missing], ["Gamma (Japan).iso"])
        self.assertEqual(scanner.count_convertible(res), 0)

    def test_the_cache_keeps_the_file_hash_and_the_iso_hash_apart(self) -> None:
        """The same .rvz scanned as a plain file and as a container (any order) must not share one cache row."""
        def names(res):
            return sorted(m.entry.path.name for m in res.matched)
        dats, _ = platforms.load_platform_dats(self.plat)
        for n, order in enumerate((((), ("rvz",)), (("rvz",), ()))):
            for kind in order:
                res = scanner.scan(self.root, dats, alt_hashes=self.plat.alt_hashes, layout=self.plat.layout,
                                   containers=kind, use_cache=True,
                                   cache_path=self.base / f"h{n}.sqlite")
                self.assertEqual(names(res), ["a.rvz", "b.iso"] if kind else ["b.iso"], (order, kind))

    def test_a_cached_scan_does_not_decode_again(self) -> None:
        cache = self.base / "hashes.sqlite"
        self.scan(use_cache=True, cache_path=cache)
        with mock.patch.object(rvz, "hash_image", side_effect=AssertionError("decoded twice")):
            res = self.scan(use_cache=True, cache_path=cache)
        self.assertEqual(sorted(m.entry.path.name for m in res.matched), ["a.rvz", "b.iso"])

    def test_cancel(self) -> None:
        with self.assertRaises(scanner.ScanCancelled):
            self.scan(cancel=lambda: True)
        with mock.patch.object(rvz, "hash_image", side_effect=InterruptedError("cancelled")):
            with self.assertRaises(scanner.ScanCancelled):
                self.scan()

    def test_rename_keeps_the_extension_and_undo_restores_everything(self) -> None:
        for junk in ("cut.rvz", "fake.rvz", "wii.rvz", "c.rvz"):
            (self.root / junk).unlink()
        before = self.tree()
        res = self.scan()
        ops = organiser.plan_renames(res, latest_only=True)
        dst = {op.src.relative_to(self.root).as_posix(): op.dst.relative_to(self.root).as_posix() for op in ops}
        self.assertEqual(dst, {"a.rvz": "Alpha (USA).rvz", "sub/b.iso": "Beta (Europe) (En,Fr,De).iso"})
        reasons = {op.src.name: op.reason for op in ops}
        self.assertIn("compressed disc image", reasons["a.rvz"])
        out = organiser.apply_renames(ops, self.root, dat_names=[DAT])
        self.assertEqual(out["failed"], [])
        res2 = self.scan()
        again = [o for o in organiser.plan_renames(res2, latest_only=True) if o.status not in ("ok", "skip")]
        self.assertEqual(again, [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if not p.name.startswith(".")),
                         ["Alpha (USA).rvz", "Beta (Europe) (En,Fr,De).iso"])
        organiser.undo(Path(out["undo_log"]))
        self.assertEqual(self.tree(), before)


class ServerTest(unittest.TestCase):
    """The same through the HTTP server: platform list, scan, rows, plan, apply."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        p = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.base / "data"), "ROMORG_OFFLINE": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.root = self.base / "gc"
        self.root.mkdir()
        self.a, runs = disc(1)
        self.b, _ = disc(2)
        write_dat(paths.redump_dir(), [("Alpha (USA)", self.a), ("Beta (Europe)", self.b)])
        R.build_rvz(self.root / "x.rvz", self.a, runs, compression=R.NONE)
        (self.root / "y.iso").write_bytes(self.b)
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))

    def call(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Romorg-Token": "t"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def job(self):
        import time
        end = time.time() + 120
        while time.time() < end:
            j = self.call("/api/job")
            if j and j["status"] != "running":
                self.assertEqual(j["status"], "done", j)
                return j["result"]
            time.sleep(0.05)
        raise AssertionError("job timeout")

    def test_the_library_plan_keeps_each_files_own_extension(self) -> None:
        self.call("/api/scan", {"path": str(self.root), "platform": PLAT})
        self.job()
        prof = self.call("/api/library/profile?platform=Nintendo%20GameCube")
        self.assertEqual(prof["style"], "redump")
        plan = self.call("/api/library/plan", {})
        by = {i["from"]: i["to"] for i in plan["items"]}
        self.assertEqual(sorted(by), ["x.rvz", "y.iso"])
        self.assertEqual(sorted(Path(t).suffix for t in by.values()), [".iso", ".rvz"])     # one is set aside, as it is
        self.assertTrue(by["x.rvz"].endswith("x.rvz") or by["x.rvz"].endswith("Alpha (USA).rvz"))

    def test_scan_plan_apply(self) -> None:
        row = {p["name"]: p for p in self.call("/api/platforms")}[PLAT]
        self.assertEqual((row["source"], row["layout"], row["folder_hint"], row["convertible"]),
                         ("redump", "flat", "gc", False))
        self.assertEqual(row["dats"][0]["name"], DAT)
        self.assertTrue(row["dats"][0]["present"])
        self.call("/api/scan", {"path": str(self.root), "platform": PLAT})
        res = self.job()
        self.assertEqual((res["matched_files"], res["unmatched_files"]), (2, 0))
        rows = self.call("/api/scan/results?kind=matched&limit=50")
        via = {i["file"]: i["via"] for i in rows["items"]}
        self.assertEqual(via, {"x.rvz": "container", "y.iso": "raw"})
        plan = self.call("/api/library/plan", {})
        self.assertEqual(sorted(i["to"] for i in plan["items"] if i["status"] != "ok"),
                         ["Alpha (USA).rvz", "Beta (Europe).iso"])
        self.call("/api/library/apply", {})
        self.job()
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file() and not p.name.startswith(".")),
                         ["Alpha (USA).rvz", "Beta (Europe).iso"])


if __name__ == "__main__":
    unittest.main()
