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


# Real sets (optional, read-only): the writer on real discs against chdman's own CHDs of them.
#   ROMORG_REAL_SETS          a folder with one subfolder per set holding its .gdi / .cue (and / or .iso)
#   ROMORG_REAL_CHDMAN_CHDS   a folder with chdman 0.289's CHDs of those sets: "<set>.chd" or "<set>_cd.chd" =
#                             createcd of the set's .gdi / .cue (else its .iso), "<set>_dvd.chd" = createdvd of its .iso
# Every pair is written to a temporary folder (a CD takes seconds, a full DVD about half a minute here).
REAL_SETS = os.environ.get("ROMORG_REAL_SETS", "")
REAL_CHDMAN_CHDS = os.environ.get("ROMORG_REAL_CHDMAN_CHDS", "")


@unittest.skipUnless(REAL_SETS and REAL_CHDMAN_CHDS, "set ROMORG_REAL_SETS and ROMORG_REAL_CHDMAN_CHDS (real sets)")
class RealSetTest(unittest.TestCase):
    def pairs(self):
        for ref in sorted(Path(REAL_CHDMAN_CHDS).glob("*.chd")):
            stem, mode = ref.stem, "createcd"
            if stem.endswith("_dvd"):
                stem, mode = stem[:-4], "createdvd"
            elif stem.endswith("_cd"):
                stem = stem[:-3]
            folder = Path(REAL_SETS) / stem
            kinds = ("*.iso",) if mode == "createdvd" else ("*.gdi", "*.cue", "*.iso")
            src = next((p for k in kinds for p in sorted(folder.glob(k))), None)
            if src is not None:
                yield ref, src, mode

    def test_the_writer_writes_chdmans_sha1(self) -> None:
        pairs = list(self.pairs())
        if not pairs:
            self.skipTest("no set of ROMORG_REAL_SETS has a CHD in ROMORG_REAL_CHDMAN_CHDS")
        with tempfile.TemporaryDirectory() as tmp:
            for ref, src, mode in pairs:
                with self.subTest(ref.name):
                    with chd.Chd(ref, load_map=False) as c:
                        want = (c.sha1, c.raw_sha1, c.logical_bytes)
                    target = Path(tmp) / ref.name
                    info = chdwrite.write_chd(target, cdimage.open_image(src, mode))
                    with chd.Chd(target, load_map=False) as c:
                        self.assertEqual((c.sha1, c.raw_sha1, c.logical_bytes), want)
                    self.assertEqual(info["sha1"], want[0])
                    target.unlink()


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

    def test_huffman_lengths_stay_within_8_bits(self) -> None:
        fib = [1, 1]
        while len(fib) < 16:
            fib.append(fib[-1] + fib[-2])
        lengths = chdwrite._huffman_lengths(fib, 8)
        self.assertLessEqual(max(lengths), 8)
        self.assertEqual(sum(1 << (8 - ln) for ln in lengths if ln), 256)       # a complete tree
        self.assertEqual(sorted(chdwrite._huffman_lengths([0, 5] + [0] * 14, 8)), [0] * 14 + [1, 1])


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


# --------------------------------------------------------------------------- sheet syntax, as chdman 0.289 reads it
def sheet_cases() -> dict:
    """``name -> (sheet path, text, {file: bytes}, options, expected)``. ``expected`` is a key of
    ``SheetSyntaxTest.CHDMAN`` (chdman 0.289 takes the sheet and writes that SHA-1, and so must the writer),
    ``"refused"`` (chdman and the writer both refuse it), ``"ours:<key>"`` (chdman 0.289 refuses it, the writer
    deliberately takes it: a UTF-8 byte order mark, a sheet in the Windows ANSI code page) or ``"chdman"`` (chdman
    takes it with a layout the writer does not reproduce, so the writer refuses it rather than write another CHD)."""
    d, a, d3 = T.make_data_track(32, 1), T.make_audio_track(32, 2), T.make_data_track(40, 3)
    cases: dict = {}

    def case(name, sheet, text, files, expected, enc="utf-8", bom=False, nl="\n"):
        cases[name] = (sheet, text, files, {"enc": enc, "bom": bom, "nl": nl}, expected)

    t1 = "  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n"
    for name, line, names, expected in (
            ("cue_quoted_space", 'FILE "a b.bin" BINARY', ["a b.bin"], "one"),
            ("cue_unquoted", "FILE ab.bin BINARY", ["ab.bin"], "one"),
            ("cue_quoted_apostrophe", "FILE \"Tony's Game.bin\" BINARY", ["Tony's Game.bin"], "one"),
            ("cue_quoted_two_apostrophes", "FILE \"Tony's 'Game.bin\" BINARY", ["Tony's 'Game.bin"], "one"),
            ("cue_quoted_double_space", 'FILE "a  b.bin" BINARY', ["a  b.bin", "a b.bin"], "one"),
            ("cue_single_quotes", "FILE 'a b.bin' BINARY", ["a b.bin"], "one"),
            ("cue_quote_inside_word", 'FILE a"b c".bin BINARY', ["ab c.bin"], "one"),
            ("cue_utf8_name", 'FILE "Pokémon ü.bin" BINARY', ["Pokémon ü.bin"], "one"),
            ("cue_cjk_name", 'FILE "ゲーム.bin" BINARY', ["ゲーム.bin"], "one"),
            ("cue_junk_after_binary", 'FILE "a.bin" BINARY junk', ["a.bin"], "one"),
            ("cue_quoted_keyword", '"FILE" "a.bin" BINARY', ["a.bin"], "one"),
            ("cue_unquoted_apostrophe", "FILE Tony's.bin BINARY", ["Tony's.bin"], "refused"),
            ("cue_unquoted_space", "FILE a b.bin BINARY", ["a b.bin", "a"], "refused"),
            ("cue_empty_name", 'FILE "" BINARY', ["a.bin"], "refused"),
            ("cue_no_space_after_quote", 'FILE "a b.bin"BINARY', ["a b.bin"], "refused"),
            ("cue_lowercase_binary", 'FILE "a b.bin" binary', ["a b.bin"], "refused"),
            ("cue_lowercase_file", 'file "a b.bin" BINARY', ["a b.bin"], "refused")):
        case(name, "x.cue", line + "\n" + t1, {n: d for n in names}, expected)
    ab = {"a b.bin": d}
    case("cue_tabs", "x.cue", 'FILE\t"a b.bin"\tBINARY\n\tTRACK\t01\tMODE1/2352\n\t\tINDEX\t01\t00:00:00\n', ab, "one")
    case("cue_trailing_white_space", "x.cue",
         'FILE "a b.bin" BINARY   \n  TRACK 01 MODE1/2352 \t\n    INDEX 01 00:00:00  \n\n', ab, "one")
    case("cue_crlf", "x.cue", 'FILE "a b.bin" BINARY\n' + t1, ab, "one", nl="\r\n")
    case("cue_bom", "x.cue", 'FILE "a b.bin" BINARY\n' + t1, ab, "ours:one", bom=True)
    case("cue_bom_before_rem", "x.cue", 'REM x\nFILE "a b.bin" BINARY\n' + t1, ab, "one", bom=True)
    case("cue_rem_catalog_unknown", "x.cue", 'REM GENRE Game\nCATALOG 0000000000000\nREM FILE "zz.bin" BINARY\n'
         'FILE "a b.bin" BINARY\n  TRACK 01 MODE1/2352\n    FOO bar\n    CDTEXT x\n    INDEX 01 00:00:00\n', ab, "one")
    case("cue_lowercase_everything", "x.cue", 'file "a b.bin" binary\n  track 01 mode1/2352\n    index 01 00:00:00\n',
         ab, "refused")
    case("cue_lowercase_mode", "x.cue", 'FILE "a b.bin" BINARY\n  TRACK 01 mode1/2352\n    INDEX 01 00:00:00\n', ab,
         "refused")
    case("cue_lowercase_index", "x.cue", 'FILE "a b.bin" BINARY\n  TRACK 01 MODE1/2352\n    index 01 00:00:00\n', ab,
         "refused")
    case("cue_track_and_index_garbage", "x.cue", 'FILE "a.bin" BINARY\n  TRACK 01x MODE1/2352\n    INDEX 01 00:00:00x\n'
         '    INDEX 02 00:00:05\n', {"a.bin": d}, "one")
    case("cue_subfolder_slash", "x.cue", 'FILE "sub/a.bin" BINARY\n' + t1, {"sub/a.bin": d}, "one")
    if os.name == "nt":
        case("cue_subfolder_backslash", "x.cue", 'FILE "sub\\a.bin" BINARY\n' + t1, {"sub/a.bin": d}, "one")
        case("cue_trailing_space_in_quotes", "x.cue", 'FILE "a.bin " BINARY\n' + t1, {"a.bin": d}, "one")
    case("cue_iso", "x.cue", 'FILE "a.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n',
         {"a.iso": T.make_iso(32, 5)}, "iso")
    gaps = ('REM GENRE Game\nFILE "d.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
            'FILE "a.bin" BINARY\n  TRACK 02 AUDIO\n    FLAGS DCP\n    INDEX 00 00:00:00\n    INDEX 01 00:00:05\n'
            'FILE "a2.bin" BINARY\n  TRACK 03 AUDIO\n    PREGAP 00:02:00\n    INDEX 01 00:00:00\n    POSTGAP 00:01:00\n')
    three = {"d.bin": d, "a.bin": a, "a2.bin": T.make_audio_track(20, 4)}
    case("cue_flags_pregap_postgap_index00", "x.cue", gaps, three, "gaps")
    case("cue_lowercase_gap_commands", "x.cue", gaps.replace("PREGAP", "pregap").replace("POSTGAP", "postgap")
         .replace("FLAGS", "flags"), three, "nogaps")                # not commands in lowercase: ignored
    two = 'FILE "a.bin" BINARY\n  TRACK %s MODE1/2352\n    INDEX 01 %s\n  TRACK %s AUDIO\n    INDEX 01 %s\n'
    for name, args, expected in (("cue_two_tracks", ("01", "00:00:00", "02", "00:00:32"), "two"),
                                 ("cue_time_as_frames", ("01", "0", "02", "32"), "two"),
                                 ("cue_time_two_parts", ("01", "0:0", "02", "0:0:32"), "two"),
                                 ("cue_time_spaces_and_sign", ("01", "00:00:00", "02", '"00: 00:+32"'), "two"),
                                 ("cue_numbered_from_2", ("02", "00:00:00", "03", "00:00:32"), "refused"),
                                 ("cue_numbers_skip", ("01", "00:00:00", "03", "00:00:32"), "refused"),
                                 ("cue_numbers_descend", ("02", "00:00:00", "01", "00:00:32"), "chdman")):
        case(name, "x.cue", two % args, {"a.bin": d + a}, expected)
    gd = ('REM SINGLE-DENSITY AREA\nFILE "t1.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
          'FILE "t2.bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 00 00:00:00\n    INDEX 01 00:00:02\n'
          'REM HIGH-DENSITY AREA\nFILE "t3.bin" BINARY\n  TRACK 03 MODE1/2352\n    INDEX 01 00:00:00\n')
    case("cue_gd_rom_markers", "x.cue", gd, {"t1.bin": d, "t2.bin": a, "t3.bin": d3}, "chdman")
    # ---- gdi: track 1 data (32 frames) at 0, track 2 audio (32) at 40, track 3 data (40) at 80
    g = {"t1.bin": d, "t2.raw": a, "t3.bin": d3}

    def gdi(n1, n2="t2.raw", n3="t3.bin", sep=" "):
        return "3\n" + "\n".join(sep.join(r) for r in (
            ["1", "0", "4", "2352", n1, "0"], ["2", "40", "0", "2352", n2, "0"], ["3", "80", "4", "2352", n3, "0"])) + "\n"

    def first(name, data=d):
        return {name: data, "t2.raw": a, "t3.bin": d3}

    plain = gdi("t1.bin")
    case("gdi_plain", "x.gdi", plain, g, "gdi")
    case("gdi_quoted", "x.gdi", gdi('"t1.bin"', '"t2.raw"', '"t3.bin"'), g, "gdi")
    case("gdi_quoted_space", "x.gdi", gdi('"a b.bin"'), first("a b.bin"), "gdi")
    case("gdi_quoted_double_space", "x.gdi", gdi('"a  b.bin"'), {**first("a  b.bin"), "a b.bin": d3}, "gdi")
    case("gdi_quoted_apostrophe", "x.gdi", gdi('"Tony\'s Game.bin"'), first("Tony's Game.bin"), "gdi")
    case("gdi_single_quotes", "x.gdi", gdi("'a b.bin'"), first("a b.bin"), "gdi")
    case("gdi_quote_inside_word", "x.gdi", gdi('a"b c".bin'), first("ab c.bin"), "gdi")
    case("gdi_leading_space_in_quotes", "x.gdi", gdi('" lead.bin"'), first(" lead.bin"), "gdi")
    case("gdi_tabs", "x.gdi", gdi("t1.bin", sep="\t"), g, "gdi")
    case("gdi_many_spaces", "x.gdi", gdi("t1.bin", sep="   "), g, "gdi")
    case("gdi_crlf", "x.gdi", plain, g, "gdi", nl="\r\n")
    case("gdi_trailing_white_space", "x.gdi", plain.replace("\n", "  \n") + "\n\n", g, "gdi")
    case("gdi_leading_white_space", "x.gdi", "".join("  " + ln + "\n" for ln in plain.splitlines()), g, "gdi")
    case("gdi_blank_line_inside", "x.gdi", plain.replace("\n2 ", "\n\n2 "), g, "gdi")
    case("gdi_bom", "x.gdi", plain, g, "ours:gdi", bom=True)
    case("gdi_relative_up", "sheet/x.gdi", gdi("../data/t1.bin"),
         {"data/t1.bin": d, "sheet/t2.raw": a, "sheet/t3.bin": d3}, "gdi")
    case("gdi_relative_quoted_odd_folders", "sheet/x.gdi", gdi('"../my  data/Tony\'s Pokémon/t1.bin"'),
         {"my  data/Tony's Pokémon/t1.bin": d, "sheet/t2.raw": a, "sheet/t3.bin": d3}, "gdi")
    case("gdi_utf8_name", "x.gdi", gdi('"Pokémon ü.bin"'), first("Pokémon ü.bin"), "gdi")
    case("gdi_cjk_name", "x.gdi", gdi('"ゲーム.bin"'), first("ゲーム.bin"), "gdi")
    case("gdi_numbers_read_with_atoi", "x.gdi", "3 tracks\n" + plain[2:].replace("2 40 0 2352", "2 040x 0x 2352")
         .replace("t1.bin 0", "t1.bin 0x0"), g, "gdi")
    case("gdi_lba_moves_the_tracks", "x.gdi", plain.replace("2 40 0", "2 32 0"), g, "gdi32")
    for name, off in (("gdi_offset_ignored_hex", "0x930"), ("gdi_offset_ignored", "2352")):
        case(name, "x.gdi", plain.replace("t1.bin 0", "t1.bin " + off), first("t1.bin", bytes(2352) + d), "gdipad")
    case("gdi_unquoted_apostrophe", "x.gdi", gdi("Tony's.bin"), first("Tony's.bin"), "refused")
    case("gdi_unquoted_space", "x.gdi", gdi("a b.bin"), first("a b.bin"), "refused")
    case("gdi_count_too_high", "x.gdi", plain.replace("3\n", "4\n", 1), g, "refused")
    case("gdi_count_too_low", "x.gdi", plain.replace("3\n", "2\n", 1), g, "refused")
    case("gdi_empty_name", "x.gdi", gdi('""'), g, "refused")
    case("gdi_sector_2336", "x.gdi", plain.replace("1 0 4 2352", "1 0 4 2336"), g, "refused")
    case("gdi_blank_first_line", "x.gdi", "\n" + plain, g, "refused")
    case("gdi_text_after_tracks", "x.gdi", plain + "junk line here\n", g, "refused")
    case("gdi_out_of_order", "x.gdi", "3\n3 80 4 2352 t3.bin 0\n1 0 4 2352 t1.bin 0\n2 40 0 2352 t2.raw 0\n", g,
         "chdman")
    if os.name == "nt" and "é".encode("mbcs", "replace") == b"\xe9":
        # a sheet in the ANSI code page (Western European here): chdman 0.289 cannot open the names it reads from it
        case("cue_ansi_name", "x.cue", 'FILE "Pokémon ü.bin" BINARY\n' + t1, {"Pokémon ü.bin": d}, "ours:one",
             enc="cp1252")
        case("gdi_ansi_name", "x.gdi", gdi('"Pokémon ü.bin"'), first("Pokémon ü.bin"), "ours:gdi", enc="cp1252")
    elif os.name != "nt":
        # on POSIX the bytes of a name that is not UTF-8 are the file name, as chdman opens it
        case("cue_ansi_name", "x.cue", 'FILE "Pokémon.bin" BINARY\n' + t1, {os.fsdecode(b"Pok\xe9mon.bin"): d}, "one",
             enc="cp1252")
    return cases


class SheetSyntaxTest(unittest.TestCase):
    """Cue and gdi sheets are read exactly as chdman 0.289 reads them (``cdimage.tokenize`` & co): ``"`` and ``'``
    quoting, apostrophes inside quotes, double spaces, tabs, CR LF, trailing white space, case-sensitive keywords,
    REM / FLAGS / PREGAP / POSTGAP / INDEX 00, C ``atoi`` numbers, the GDI offset column that chdman ignores.

    ``CHDMAN`` is the header SHA-1 chdman 0.289 ``createcd`` wrote for each kind of accepted case (recorded on
    Windows with the real chdman; the tracks are generated, so the numbers are stable). With ``ROMORG_CHDMAN_ORACLE``
    set to a chdman executable, every case also goes through that chdman and its verdict is compared."""

    CHDMAN = {"one": "2431ddbbcbb7fd3ba1545b5624935217a28b6903", "two": "f9177f2adc2e06e0548a7fefee35c9e2eb2a87b8",
              "iso": "168e277ba33ed33bf93d3ac52375e5e9b91de85b", "gaps": "459df75fa1849c338d26c18f9da75320b0694a7c",
              "nogaps": "b6fed3c2c06ee8ae9af9186c2f3405d1c818d799", "gdi": "df9508cdefa3329189c0f510023d6d5218a366a0",
              "gdi32": "78443f68bf842368addc0707c18ebb6fcefaef42", "gdipad": "29ff949b678ba0d09e5bddbb085d3fb941d80d62"}

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.dir = Path(cls._tmp.name)
        cls.cases = sheet_cases()
        cls.sheets = {name: cls.build(name) for name in cls.cases}

    @classmethod
    def build(cls, name: str) -> Path:
        sheet, text, files, opt, _ = cls.cases[name]
        d = cls.dir / name
        for fn, data in files.items():
            (d / fn).parent.mkdir(parents=True, exist_ok=True)
            (d / fn).write_bytes(data)
        raw = text.replace("\n", opt["nl"]).encode(opt["enc"])
        (d / sheet).parent.mkdir(parents=True, exist_ok=True)
        (d / sheet).write_bytes((b"\xef\xbb\xbf" if opt["bom"] else b"") + raw)
        return d / sheet

    def test_the_writer_reads_every_sheet_as_chdman_does(self) -> None:
        for name, (*_, expected) in self.cases.items():
            with self.subTest(name):
                sheet = self.sheets[name]
                try:
                    image = cdimage.open_image(sheet)
                except cdimage.ImageError:
                    got = "refused"
                else:
                    got = chdwrite.write_chd(sheet.parent / "ours.chd", image, processes=False)["sha1"]
                if expected in ("refused", "chdman"):
                    self.assertEqual(got, "refused")
                else:
                    self.assertEqual(got, self.CHDMAN[expected.split(":")[-1]])

    @unittest.skipUnless(os.environ.get("ROMORG_CHDMAN_ORACLE"), "set ROMORG_CHDMAN_ORACLE to a chdman executable")
    def test_chdman_agrees(self) -> None:
        import subprocess
        exe = os.path.abspath(os.environ["ROMORG_CHDMAN_ORACLE"])
        seen = {}
        for name, (*_, expected) in self.cases.items():
            with self.subTest(name):
                sheet = self.sheets[name]
                out = sheet.parent / "chdman.chd"
                try:          # relative paths from the temporary folder: chdman is not long-path aware
                    p = subprocess.run([exe, "createcd", "-i", os.path.relpath(sheet, self.dir), "-o",
                                        os.path.relpath(out, self.dir), "-f"], cwd=self.dir, capture_output=True,
                                       timeout=60)
                    ok = p.returncode == 0 and out.exists()
                except subprocess.TimeoutExpired:
                    ok = False
                got = "refused"
                if ok:
                    with chd.Chd(out) as c:
                        got = c.sha1
                seen.setdefault(expected, set()).add(got)
                if expected == "refused" or expected.startswith("ours:"):
                    self.assertEqual(got, "refused")
                elif expected == "chdman":
                    self.assertNotEqual(got, "refused")
                else:
                    self.assertEqual(got, self.CHDMAN.get(expected, got))
        if os.environ.get("ROMORG_CHDMAN_ORACLE_PRINT"):
            print({k: sorted(v) for k, v in seen.items()})


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
