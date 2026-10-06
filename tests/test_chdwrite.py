"""The built-in CHD writer: for the same input it must write what chdman writes.

"What chdman writes" is ``tests/fixtures/chdwrite/chdman_reference.json``: the header SHA-1, data SHA-1 and
metadata chdman 0.289 produced for the generated sets of ``layouts.py`` (see there how to regenerate it). The header
SHA-1 covers the data and the metadata, so equal SHA-1s mean the same disc image down to the last padding frame,
whatever the compressed bytes are. The tests need no chdman.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "chdwrite"))

import chdtestlib as T  # noqa: E402
import layouts  # noqa: E402
from romorg import cdecc, cdimage, chd, chdsched, chdwrite, flacenc, flacnative, zstdnative  # noqa: E402

REF = json.loads((Path(layouts.HERE) / "chdman_reference.json").read_text())


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class Inputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.dir = Path(cls._tmp.name)
        layouts.write_inputs(cls.dir)

    def write(self, name: str, out: str = "", **kw):
        src, mode = layouts.source(self.dir, name)
        target = self.dir / (out or f"out-{name}-{len(os.listdir(self.dir))}.chd")
        kw.setdefault("processes", False)
        info = chdwrite.write_chd(target, cdimage.open_image(src, mode), **kw)
        return target, info


class SameAsChdmanTest(Inputs):
    def test_every_layout_has_chdmans_sha1_and_metadata(self) -> None:
        self.assertEqual(sorted(REF), sorted(layouts.NAMES))
        for name in layouts.NAMES:
            want = REF[name]
            target, info = self.write(name)
            with chd.Chd(target) as c:
                meta = [[tag.decode("latin-1"), body.decode("latin-1")] for tag, body in c.metadata]
                self.assertEqual(meta, want["metadata"], name)
                self.assertEqual((c.sha1, c.raw_sha1), (want["sha1"], want["raw_sha1"]), name)
                self.assertEqual((c.logical_bytes, c.hunk_bytes, c.unit_bytes, c.version),
                                 (want["logical_bytes"], want["hunk_bytes"], want["unit_bytes"], 5), name)
                self.assertEqual(c.verify(), {"raw": True, "overall": True, "raw_sha1": want["raw_sha1"]}, name)
                self.assertEqual((info["sha1"], info["size"]), (want["sha1"], target.stat().st_size))

    def test_size_is_close_to_chdmans(self) -> None:
        # tiny inputs exaggerate every byte of difference; real discs are within 0.5 % (docs/ARCHITECTURE.md).
        # Audio is only as small as chdman's with libFLAC: without it the writer stores audio hunks with LZMA (valid,
        # same SHA-1, larger), so the layouts with audio tracks need the encoder; the data-only one is always checked.
        for name in ("redump", "audio_only", "gd5", "m2336"):
            with self.subTest(name):
                src, mode = layouts.source(self.dir, name)
                if any(t.audio for t in cdimage.open_image(src, mode).tracks) and not flacenc.available():
                    self.skipTest("libFLAC is not installed here (set ROMORG_LIBFLAC): audio tracks are stored "
                                  "with LZMA, larger than chdman's FLAC")
                target, _info = self.write(name)
                self.assertLess(target.stat().st_size, REF[name]["size"] * 1.02, name)

    def test_tracks_come_back_as_the_files(self) -> None:
        target, _ = self.write("redump")
        with chd.Chd(target) as c:
            got = [chd.hash_track(c, t).sha1 for t in c.tracks]
        self.assertEqual(got, [sha1((self.dir / n).read_bytes()) for n in ("m2.bin", "a1.bin", "a2.bin")])
        target, _ = self.write("gd5")
        with chd.Chd(target) as c:
            self.assertTrue(c.is_gd)
            got = [chd.hash_track(c, t).sha1 for t in c.tracks]
        self.assertEqual(got, [sha1((self.dir / n).read_bytes())
                               for n in ("d1.bin", "a1.raw", "m2x.bin", "a2.raw", "d1b.bin")])
        target, _ = self.write("iso_dvd")
        with chd.Chd(target) as c:
            self.assertTrue(c.is_dvd)
            self.assertEqual(c.raw_sha1, sha1((self.dir / "x.iso").read_bytes()))

    def test_codecs_and_the_reasons_for_them(self) -> None:
        _t, info = self.write("redump")
        self.assertEqual(info["codecs"], ["cdlz", "cdzl", "cdfl"])
        self.assertEqual(info["stored"]["cdfl"] > 0, flacenc.available())       # audio goes to FLAC when there is one
        self.assertGreater(info["stored"]["cdlz"], 0)
        _t, info = self.write("gd")                                               # the pad between the areas: zeros
        self.assertGreater(info["stored"]["self"], 5000)
        self.assertEqual(sum(info["stored"].values()), info["hunks"])
        _t, info = self.write("iso_dvd")
        self.assertEqual(info["codecs"], ["lzma", "zlib"])

    def test_generated_gdi_of_a_redump_cue(self) -> None:
        # what the app does for a Dreamcast set that only has a .cue: the GDI text exists in memory only
        text = '3\n1 0 4 2352 "ignored-1" 0\n2 600 0 2352 "ignored-2" 0\n3 45000 4 2352 "ignored-3" 0\n'
        files = [self.dir / "d1.bin", self.dir / "a1.raw", self.dir / "m2x.bin"]
        target = self.dir / "gen.chd"
        chdwrite.write_chd(target, cdimage.open_image(self.dir / "redump.cue", gdi_text=text, gdi_files=files),
                           processes=False)
        with chd.Chd(target) as c:
            self.assertEqual((c.sha1, c.is_gd), (REF["gd"]["sha1"], True))


class LongPathTest(unittest.TestCase):
    def test_sets_deeper_than_260_characters(self) -> None:
        """Windows' old MAX_PATH: sheets and track files in a folder whose path is over 260 characters long are
        found and read (with the LongPathsEnabled setting; without it the folder cannot even be made)."""
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp)
            while len(str(deep)) < 300:
                deep = deep / ("a long folder name for a game set " + str(len(str(deep))))
            try:
                deep.mkdir(parents=True)
                layouts.write_inputs(deep)
            except OSError as exc:
                self.skipTest(f"this system cannot make paths over 260 characters ({exc})")
            for name in ("redump", "gd5", "iso_dvd"):
                src, mode = layouts.source(deep, name)
                self.assertGreater(len(str(src)), 260)
                target = deep / f"{name}.chd"
                chdwrite.write_chd(target, cdimage.open_image(src, mode), threads=2, processes=True)
                with chd.Chd(target) as c:
                    self.assertEqual(c.sha1, REF[name]["sha1"], name)


class EngineTest(Inputs):
    def test_worker_processes_threads_and_one_thread_write_the_same_file(self) -> None:
        a, ia = self.write("gd5", threads=1)
        b, ib = self.write("gd5", threads=3)
        c, ic = self.write("gd5", threads=2, processes=True)
        self.assertEqual((ia["engine"], ib["engine"], ic["engine"]), ("threads", "threads", "processes"))
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(a.read_bytes(), c.read_bytes())

    def test_a_worker_that_cannot_start_or_dies_is_replaced_by_this_process(self) -> None:
        ref, _ = self.write("redump", threads=1)
        with mock.patch.object(chdsched, "spawn_worker", side_effect=chdsched.PoolError("no")):
            t, info = self.write("redump", threads=2, processes=True)
        self.assertEqual((t.read_bytes(), info["engine"]), (ref.read_bytes(), "threads"))
        real = chdsched.spawn_worker

        def dying():
            proc = real()
            proc.stdin.close()                  # the first request hits a closed pipe
            return proc
        with mock.patch.object(chdsched, "spawn_worker", side_effect=dying):
            t, info = self.write("redump", threads=2, processes=True)
        self.assertEqual(t.read_bytes(), ref.read_bytes())
        self.assertEqual(info["engine"], "threads")              # a broken pool is reported, not only slow
        self.assertGreater(info["fallbacks"], 0)

    def test_cancel_and_errors_leave_no_file(self) -> None:
        src, mode = layouts.source(self.dir, "gd")
        target = self.dir / "cancelled.chd"
        calls = []

        def cancel() -> bool:
            calls.append(1)
            return len(calls) > 1
        with self.assertRaises(chdwrite.Cancelled):
            chdwrite.write_chd(target, cdimage.open_image(src, mode), cancel=cancel, processes=False, threads=1)
        self.assertFalse(target.exists())
        image = cdimage.open_image(self.dir / "redump.cue")
        with mock.patch.object(cdimage.CdImage, "_pieces", side_effect=cdimage.ImageError("gone")):
            with self.assertRaises(cdimage.ImageError):
                chdwrite.write_chd(target, image, processes=False)
        self.assertFalse(target.exists())

    def test_an_existing_file_is_never_overwritten(self) -> None:
        target, _ = self.write("cooked")
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            chdwrite.write_chd(target, cdimage.open_image(self.dir / "cooked.cue"), processes=False)
        self.assertEqual(target.read_bytes(), before)

    def test_progress_reaches_the_end(self) -> None:
        seen = []
        self.write("gd", progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertEqual(seen, sorted(seen))

    def test_without_flac_audio_is_stored_another_way(self) -> None:
        with mock.patch.object(flacenc, "available", return_value=False):
            target, info = self.write("audio_only")
        self.assertEqual(info["stored"]["cdfl"], 0)
        with chd.Chd(target) as c:
            self.assertEqual(c.sha1, REF["audio_only"]["sha1"])
            self.assertTrue(c.verify_raw_sha1())

    def test_zstandard_preset(self) -> None:
        if not chdwrite.zstd_available():
            self.skipTest("no Zstandard library")
        for name, first in (("redump", "cdzs"), ("iso_dvd", "zstd")):
            target, info = self.write(name, preset="zstd")
            self.assertEqual(info["codecs"][0], first)
            self.assertGreater(info["stored"][first], 0)
            with chd.Chd(target) as c:
                self.assertEqual((c.sha1, c.verify()["overall"]), (REF[name]["sha1"], True))
        with mock.patch.object(zstdnative, "can_compress", return_value=False):
            with self.assertRaises(chdwrite.ChdWriteError):
                chdwrite.default_codecs(True, "zstd")
        with self.assertRaises(chdwrite.ChdWriteError):
            chdwrite.default_codecs(True, "brotli")


class CompressorTest(unittest.TestCase):
    def test_incompressible_data_skips_lzma_and_may_be_stored(self) -> None:
        import random
        rnd = random.Random(4)
        comp = chdwrite._Compressor(("lzma", "zlib", "", ""), 4096, False)
        noise = bytes(rnd.randrange(256) for _ in range(4096))
        with mock.patch.object(chdwrite.lzma, "compress", side_effect=AssertionError("not for noise")):
            self.assertEqual(comp(noise, "data"), (chdwrite._T_NONE, noise))
        slot, blob = comp(b"compressible text " * 228, "data")
        self.assertEqual(slot, 0)
        self.assertLess(len(blob), 400)

    def test_ecc_is_dropped_only_where_it_is_the_standard_one(self) -> None:
        data = bytearray(T.make_data_track(8, 3))
        data[5 * 2352 + 2100] ^= 1                                  # one sector with a damaged parity byte
        comp = chdwrite._Compressor(("cdlz", "cdzl", "cdfl", ""), 8 * 2448, True)
        bitmap, bare, count = comp._strip_ecc(bytes(data))
        self.assertEqual((bitmap, count), (bytes([0xFF & ~(1 << 5)]), 7))
        self.assertEqual(bare[5 * 2352:6 * 2352], data[5 * 2352:6 * 2352])
        self.assertEqual(bare[0:12] + bare[2076:2352], bytes(288))
        pre = comp.strip_many([b"".join(bytes(data[i * 2352:(i + 1) * 2352]) + bytes(96) for i in range(8))], ["data"])
        self.assertEqual((pre[0][2], pre[0][3], pre[0][4]), (bitmap, bare, count))

    def test_valid_agrees_with_generate(self) -> None:
        d = T.make_data_track(6, 1)
        m = T.make_mode2_track(6, 2)
        sectors = [bytearray(d[i * 2352:(i + 1) * 2352]) for i in range(6)] + \
                  [bytearray(m[i * 2352:(i + 1) * 2352]) for i in range(6)] + [bytearray(T.make_audio_track(1, 1))]
        sectors[1][500] ^= 0x10
        sectors[2][2300] ^= 0x01
        want = []
        for s in sectors:
            c = bytearray(s)
            cdecc.generate([c])
            want.append(c == s)
        self.assertEqual(cdecc.valid(sectors), want)
        self.assertEqual((want[0], want[1], want[2], want[-1]), (True, False, False, False))
        self.assertEqual(cdecc.valid([]), [])


class MapTest(unittest.TestCase):
    def roundtrip(self, hunks: list) -> None:
        """A DVD-style image of the given 4096-byte hunks, written and read back."""
        class Image:
            cd = gd = False
            hunk_bytes, unit_bytes = 4096, 2048
            metadata = [(b"DVD ", b"\0")]
            logical_bytes = len(hunks) * 4096 - 2048            # the last hunk is only half data

            def batches(self, n):
                for i in range(0, len(hunks), n):
                    part = hunks[i:i + n]
                    yield b"".join(part), ["data"] * len(part)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.chd"
            chdwrite.write_chd(p, Image(), processes=False, threads=1)
            with chd.Chd(p) as c:
                data = b"".join(c.iter_raw())
                self.assertEqual(data, b"".join(hunks)[:Image.logical_bytes])
                self.assertEqual(c.verify()["overall"], True)

    def test_runs_copies_and_single_kinds(self) -> None:
        import random
        rnd = random.Random(9)
        text = [((b"hunk %d " % i) * 600)[:4096] for i in range(40)]
        noise = [bytes(rnd.randrange(256) for _ in range(4096)) for _ in range(6)]
        zero = bytes(4096)
        self.roundtrip([zero] * 700)                                    # one hunk and 699 copies: long runs
        self.roundtrip(list(text))                        # one codec only
        self.roundtrip(noise)                                           # stored only
        mixed = []
        for i in range(300):
            mixed.append(text[i % 40] if i % 3 else noise[i % 6] if i % 2 else zero)
        self.roundtrip(mixed + text * 3)            # copies that follow each other: SELF_1 runs

    def test_the_map_is_byte_for_byte_the_per_entry_reference(self) -> None:
        """``_build_map`` works on whole arrays at once; it must give exactly what the plain per-entry loop gives
        (kept here as the reference) for every mix of kinds, runs and copies."""
        import random
        from array import array
        rnd = random.Random(2026)
        cases = [([], [], [], []), ([0], [100], [124], [7]), ([4], [4096], [124], [9]), ([5, 5], [0, 0], [0, 0], [0, 0])]
        for n in (5, 19, 23, 300, 4000):
            for style in ("random", "runs", "copies", "copies of zero"):
                types, lens, offs, crcs = array("B"), array("I"), array("Q"), array("H")
                pos, t = 124, 0
                for i in range(n):
                    if style == "runs" and rnd.random() < 0.1 or style != "runs":
                        t = rnd.choice((0, 0, 1, 2, 3, 4, 5, 5)) if style != "random" else rnd.choice((0, 1, 4, 5))
                    if t == 5 and i:
                        o = 0 if style == "copies of zero" else rnd.choice((rnd.randrange(i), i - 1, offs[-1] + 1,
                                                                            offs[-1]))
                        types.append(5), lens.append(0), offs.append(min(o, i - 1)), crcs.append(0)
                        continue
                    t = t if t != 5 else 0
                    ln = 4096 if t == 4 else rnd.randint(1, 70000)
                    types.append(t), lens.append(ln), offs.append(pos), crcs.append(rnd.randrange(65536))
                    pos += ln
                cases.append((types, lens, offs, crcs))
                cases.append((list(types), list(lens), list(offs), list(crcs)))
        for types, lens, offs, crcs in cases:
            first = rnd.randrange(1 << 40)
            self.assertEqual(chdwrite._build_map(types, lens, offs, crcs, first),
                             _reference_map(types, lens, offs, crcs, first), (len(types), list(types)[:30]))

    def test_huffman_lengths_stay_within_8_bits(self) -> None:
        fib = [1, 1]
        while len(fib) < 16:
            fib.append(fib[-1] + fib[-2])
        lengths = chdwrite._huffman_lengths(fib, 8)
        self.assertLessEqual(max(lengths), 8)
        self.assertEqual(sum(1 << (8 - ln) for ln in lengths if ln), 256)       # a complete tree
        self.assertEqual(sorted(chdwrite._huffman_lengths([0, 5] + [0] * 14, 8)), [0] * 14 + [1, 1])


def _reference_map(types, lens, offs, crcs, first_offset) -> bytes:
    """The v5 map built entry by entry: how ``chdwrite._build_map`` was first written (checked against chdman)."""
    import binascii
    W = chdwrite
    n = len(types)
    kinds = []
    last_self = -2
    for i in range(n):
        t = types[i]
        if t == W._T_SELF:
            o = offs[i]
            if o == last_self:
                t = W._T_SELF0
            elif o == last_self + 1:
                t = W._T_SELF1
            last_self = o
        kinds.append(t)
    symbols = []
    i = 0
    while i < n:
        t = kinds[i]
        run = 1
        while i + run < n and kinds[i + run] == t:
            run += 1
        symbols.append(t)
        rest = run - 1
        while rest >= 3:
            if rest >= 19:
                k = min(rest - 19, 255)
                symbols += (W._T_RLE_LARGE, k >> 4, k & 15)
                rest -= 19 + k
            else:
                symbols += (W._T_RLE_SMALL, rest - 3)
                rest = 0
        symbols += [t] * rest
        i += run
    counts = [0] * 16
    for s in symbols:
        counts[s] += 1
    lengths = W._huffman_lengths(counts, 8)
    codes = W._canonical(lengths)
    bits = ["".join("00010001" if ln == 1 else format(ln, "04b") for ln in lengths)]
    bits.append("".join([codes[s] for s in symbols]))
    lengthbits = max((lens[i] for i in range(n) if types[i] <= 3), default=0).bit_length()
    selfbits = max((offs[i] for i in range(n) if types[i] == W._T_SELF), default=0).bit_length()
    fields = []
    raw = bytearray()
    for i in range(n):
        t, ln, off, crc = types[i], lens[i], offs[i], crcs[i]
        if t <= 3:
            fields.append(format((ln << 16) | crc, f"0{lengthbits + 16}b"))
        elif t == W._T_NONE:
            fields.append(format(crc, "016b"))
        elif kinds[i] == W._T_SELF and selfbits:
            fields.append(format(off, f"0{selfbits}b"))
        if t == W._T_SELF:
            ln = crc = 0
        raw += struct.pack(">BBHHIH", t, ln >> 16, ln & 0xFFFF, off >> 32, off & 0xFFFFFFFF, crc)
    bits.append("".join(fields))
    stream = "".join(bits)
    stream += "0" * (-len(stream) % 8)
    body = int(stream, 2).to_bytes(len(stream) // 8, "big") if stream else b""
    head = struct.pack(">I", len(body)) + first_offset.to_bytes(6, "big") + struct.pack(
        ">H", binascii.crc_hqx(bytes(raw), 0xFFFF)) + bytes([lengthbits, selfbits, 0, 0])
    return head + body


class ImageErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "t.bin").write_bytes(T.make_data_track(4, 1))

    def cue(self, text: str) -> Path:
        p = self.dir / "x.cue"
        p.write_text(text)
        return p

    def test_layouts_that_are_refused_instead_of_guessed(self) -> None:
        good = 'FILE "t.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
        self.assertEqual(cdimage.open_image(self.cue(good)).total_frames, 4)
        for text, word in (
                (good.replace("BINARY", "WAVE"), "BINARY"),
                (good.replace("MODE1/2352", "CDG"), "mode"),
                (good.replace("t.bin", "missing.bin"), "missing.bin"),
                (good.replace("00:00:00", "00:00:02"), "beginning"),
                (good.replace("TRACK 01", "TRACK 02"), "numbered"),
                (good.replace("    INDEX 01 00:00:00\n", ""), "INDEX 01"),
                (good + "  TRACK 02 AUDIO\n    INDEX 01 00:00:09\n", "outside"),
                (good.replace("MODE1/2352", "MODE1/2048"), "sectors"),
                ("CDTEXT nonsense\n" + good, "unknown"),
                ("", "no tracks")):
            with self.assertRaises(cdimage.ImageError, msg=text) as cm:
                cdimage.open_image(self.cue(text))
            self.assertIn(word, str(cm.exception))

    def test_bad_gdi_and_iso(self) -> None:
        (self.dir / "a.gdi").write_text("2\n1 0 4 2352 t.bin 0\n")
        (self.dir / "b.gdi").write_text("2\n1 0 4 2352 t.bin 0\n2 2 4 2352 t.bin 0\n")       # track 1 overlaps 2
        (self.dir / "c.gdi").write_text("1\n1 0 9 2352 t.bin 0\n")
        for name in ("a.gdi", "b.gdi", "c.gdi"):
            with self.assertRaises(cdimage.ImageError):
                cdimage.open_image(self.dir / name)
        (self.dir / "odd.iso").write_bytes(bytes(3000))
        for mode in ("createcd", "createdvd"):
            with self.assertRaises(cdimage.ImageError):
                cdimage.open_image(self.dir / "odd.iso", mode)
        with self.assertRaises(cdimage.ImageError):
            cdimage.open_image(self.dir / "t.bin")


@unittest.skipUnless(flacenc.available(), "no libFLAC")
class FlacEncoderTest(unittest.TestCase):
    def test_frames_decode_back_and_have_mames_block_size(self) -> None:
        le = T.make_audio_track(8, 5)
        be = bytearray(le)
        be[0::2], be[1::2] = le[1::2], le[0::2]
        frames = flacenc.encode(bytes(be))
        self.assertEqual(struct.unpack(">H", frames[5:7])[0] + 1, 2352)          # the block size in the frame header
        pcm, end = flacnative.decode_pcm(frames, 0, len(be) // 4, big_endian=True)
        self.assertEqual((bytes(pcm), end), (bytes(be), len(frames)))
        self.assertEqual((flacenc.block_size(18816), flacenc.block_size(4096, cd=False),
                          flacenc.block_size(19584 * 4, cd=False)), (2352, 1024, 1224))


if __name__ == "__main__":
    unittest.main()
