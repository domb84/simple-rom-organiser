"""CHD v5 reader, FLAC decoder and ECC regeneration: synthetic discs (written by the test-only writer) and,
when available, the user's real Dreamcast CHDs (read-only)."""

from __future__ import annotations

import glob
import hashlib
import os
import random
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
from romorg import cdecc, chd, datfile, flacdec  # noqa: E402

SCRATCH = Path("/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/"
               "cfc5ea37-4261-428c-9422-29acd65cac97/scratchpad")
REAL_DAT = next(iter(glob.glob(str(SCRATCH / "redump" / "Sega - Dreamcast*.dat"))), "")
REAL_DIR = Path("/home/deck/MEGA/Emulation/roms/dreamcast")
REAL_TITLES = ["De La Jet Set Radio (Japan) (En,Ja,Fr,De,Es)",
               "Sonic Adventure (USA) (En,Ja,Fr,De,Es) (Rev A)",
               "Tony Hawk's Pro Skater 2 (USA)",
               "Toy Commander (USA) (En,Fr,De,Es)"]


class EccTest(unittest.TestCase):
    def test_vectorised_equals_reference(self) -> None:
        rnd = random.Random(5)
        for n in (1, 2, 3, 8, 17):
            secs = [bytearray(rnd.randbytes(2352)) for _ in range(n)]
            ref = [bytearray(s) for s in secs]
            for r in ref:
                cdecc.generate_reference(r)
            cdecc.generate(secs)
            self.assertEqual([bytes(s) for s in secs], [bytes(r) for r in ref], n)

    def test_works_on_memoryviews_and_fills_sync(self) -> None:
        buf = bytearray(random.Random(1).randbytes(2352 * 3))
        views = [memoryview(buf)[i * 2352:(i + 1) * 2352] for i in range(3)]
        cdecc.generate(views)
        for i in range(3):
            self.assertEqual(bytes(buf[i * 2352:i * 2352 + 12]), cdecc.SYNC)

    def test_known_mode1_sector_parity(self) -> None:
        # an all-zero MODE1 body has an all-zero parity except what the header contributes: stable value
        sec = bytearray(2352)
        sec[15] = 1
        cdecc.generate_reference(sec)
        again = bytearray(sec)
        cdecc.generate_reference(again)
        self.assertEqual(sec, again)
        copy = bytearray(sec)
        copy[2076:] = bytes(276)
        cdecc.generate([memoryview(copy)])
        self.assertEqual(copy, sec)


class FlacTest(unittest.TestCase):
    def pcm(self, n: int, seed: int = 3) -> bytes:
        rnd = random.Random(seed)
        out = bytearray()
        for i in range(n):
            out += struct.pack("<hh", int(5000 * (i % 50 - 25) / 25) + rnd.randint(-60, 60), rnd.randint(-3000, 3000))
        return bytes(out)

    def check(self, pcm: bytes, **kw) -> None:
        stream = T.flac_stream(pcm, block=kw.pop("block", 400), **kw)
        got, end = flacdec.decode_frames(stream + b"TAIL", 0, len(pcm) // 4)
        self.assertEqual(got.tobytes(), pcm)
        self.assertEqual(stream + b"TAIL"[:0], (stream + b"TAIL")[:end])   # consumed exactly the frames

    def test_fixed_and_stereo_modes(self) -> None:
        pcm = self.pcm(1500)
        for stereo in ("indep", "left_side", "side_right", "mid_side"):
            with self.subTest(stereo):
                self.check(pcm, stereo=stereo)

    def test_verbatim_constant_fixed1(self) -> None:
        self.check(self.pcm(800), kinds=("verbatim", "fixed1"))
        flat = struct.pack("<hh", 123, -77) * 300
        self.check(flat, kinds=("constant", "constant"))

    def test_truncated_and_lost_sync(self) -> None:
        stream = T.flac_stream(self.pcm(800), block=400)
        with self.assertRaises(flacdec.FlacError):
            flacdec.decode_frames(stream[:30], 0, 800)
        with self.assertRaises(flacdec.FlacError):
            flacdec.decode_frames(b"\x00" * 64, 0, 400)
        with self.assertRaises(flacdec.FlacError):       # asks for more samples than the stream has
            flacdec.decode_frames(stream, 0, 1200)


class SyntheticChdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.disc = T.Disc("Synthetic (USA)", 11)
        # a disc with digital silence: identical hunks become SELF references, long runs use the RLE types
        silence = [{"type": "MODE1_RAW", "data": T.make_data_track(8, 90)},
                   {"type": "AUDIO", "data": bytes(2352 * 400), "pad": 40},
                   {"type": "MODE1_RAW", "data": T.make_data_track(16, 91, 500)}]
        cls.silent_tracks = silence

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def write(self, name: str, disc=None, **kw) -> str:
        p = str(Path(self.tmp.name) / name)
        (disc or self.disc).write_chd(p, **kw)
        return p

    def assert_disc(self, path: str, disc=None, gd: bool = True) -> None:
        disc = disc or self.disc
        with chd.Chd(path) as c:
            self.assertEqual(c.version, 5)
            self.assertEqual(len(c.tracks), 3)
            self.assertEqual(c.is_gd, gd)
            for t, data in zip(c.tracks, disc.bins):
                self.assertEqual(t.size, len(data))
                self.assertEqual(b"".join(c.iter_track(t)), data)
                h = chd.hash_track(c, t)
                self.assertEqual((h.size, h.sha1, h.md5, h.crc32),
                                 (len(data), hashlib.sha1(data).hexdigest(), hashlib.md5(data).hexdigest(),
                                  "%08x" % (zlib.crc32(data) & 0xFFFFFFFF)))
            self.assertTrue(c.verify_raw_sha1())

    def test_cdlz_and_cdfl_compressed_map(self) -> None:
        self.assert_disc(self.write("a.chd"))

    def test_cdzl_for_everything(self) -> None:
        self.assert_disc(self.write("b.chd", data_codec=1, audio_codec=1))

    def test_flac_modes_inside_hunks(self) -> None:
        self.assert_disc(self.write("c.chd", stereo="mid_side"))

    def test_generic_codecs(self) -> None:
        self.assert_disc(self.write("z.chd", codecs=("zlib", "lzma", "", ""), generic="zlib"))
        self.assert_disc(self.write("l.chd", codecs=("lzma", "zlib", "", ""), generic="lzma"))
        self.assert_disc(self.write("n.chd", codecs=("zlib", "", "", ""), generic="none"))

    def test_uncompressed_map(self) -> None:
        p = self.write("u.chd", codecs=("", "", "", ""), generic="none", compressed_map=False)
        with chd.Chd(p) as c:
            self.assertFalse(c.compressed)
        self.assert_disc(p)

    def test_cd_not_gd_metadata(self) -> None:
        disc = T.Disc("Plain CD", 5, pad=0)
        p = self.write("cd.chd", disc, gd=False)
        self.assert_disc(p, disc, gd=False)

    def test_runs_and_self_references(self) -> None:
        p = str(Path(self.tmp.name) / "s.chd")
        T.build_chd(p, self.silent_tracks)
        with chd.Chd(p) as c:
            kinds = set(c._ctype)
            self.assertIn(5, kinds)              # SELF
            self.assertGreater(c.hunk_count, 40)
            for t, tr in zip(c.tracks, self.silent_tracks):
                self.assertEqual(b"".join(c.iter_track(t)), tr["data"])
            self.assertTrue(c.verify_raw_sha1())

    def test_track_layout_and_pad(self) -> None:
        p = self.write("lay.chd")
        with chd.Chd(p) as c:
            t1, t2, t3 = c.tracks
            self.assertEqual((t1.frames, t1.pad, t1.start), (8, 0, 0))
            self.assertEqual((t2.frames, t2.pad, t2.start), (6 + 4, 4, 8))      # pad frames are inside FRAMES
            self.assertEqual(t2.data_frames, 6)
            self.assertEqual(t3.start, 8 + 12)                                     # 10 frames padded to a multiple of 4
            self.assertEqual(t2.size, len(self.disc.t2))

    def test_lazy_map_for_info_only(self) -> None:
        p = self.write("info.chd")
        with chd.Chd(p, load_map=False) as c:
            self.assertEqual(len(c.tracks), 3)
            self.assertFalse(c._map_loaded)
            with chd.Chd(p) as other:
                self.assertEqual(c.sha1, other.sha1)

    def test_errors(self) -> None:
        p = self.write("e.chd")
        raw = Path(p).read_bytes()
        bad = Path(self.tmp.name) / "bad.chd"
        bad.write_bytes(b"not a chd" + bytes(200))
        with self.assertRaises(chd.ChdError):
            chd.Chd(bad)
        v4 = bytearray(raw)
        v4[12:16] = struct.pack(">I", 4)
        bad.write_bytes(bytes(v4))
        with self.assertRaises(chd.ChdUnsupported):
            chd.Chd(bad)
        trunc = raw[:len(raw) - 40]                    # the map is cut off
        bad.write_bytes(trunc)
        with self.assertRaises(chd.ChdError):
            chd.Chd(bad)
        mapoff = struct.unpack(">Q", raw[40:48])[0]
        crc = bytearray(raw)
        crc[mapoff + 10] ^= 0xFF                        # map CRC
        bad.write_bytes(bytes(crc))
        with self.assertRaises(chd.ChdError):
            chd.Chd(bad)
        # a damaged hunk is an error, not silent garbage
        info = chd.Chd(p)
        off = info._coff[0]
        info.close()
        dmg = bytearray(raw)
        for i in range(off + 20, off + 60):
            dmg[i] ^= 0x55
        bad.write_bytes(bytes(dmg))
        with chd.Chd(bad) as c:
            with self.assertRaises(chd.ChdError):
                b"".join(c.iter_track(c.tracks[0]))

    def test_parent_chd_is_unsupported(self) -> None:
        p = self.write("par.chd")
        raw = bytearray(Path(p).read_bytes())
        raw[104:124] = bytes(range(1, 21))
        q = Path(self.tmp.name) / "parent.chd"
        q.write_bytes(bytes(raw))
        with self.assertRaises(chd.ChdUnsupported):
            chd.Chd(q)

    def test_cancel(self) -> None:
        p = self.write("cancel.chd")
        with chd.Chd(p) as c:
            with self.assertRaises(InterruptedError):
                chd.hash_track(c, c.tracks[2], cancel=lambda: True)


@unittest.skipUnless(REAL_DAT and REAL_DIR.is_dir(), "real Dreamcast CHDs / Redump DAT not available")
class RealChdTest(unittest.TestCase):
    """The user's real CHDs, read-only. The quick checks always run; ``ROMORG_REAL_CHD_FULL=1`` hashes every
    track of all four discs (about 8 minutes with the pure-Python FLAC decoder, 2.5 without audio)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dat = datfile.parse_redump(REAL_DAT)
        cls.games = cls.dat.games()

    def path(self, title: str) -> Path:
        p = REAL_DIR / title / f"{title}.chd"
        if not p.is_file():
            self.skipTest(f"{p} missing")
        return p

    def bins(self, title: str):
        roms = [r for r in self.games[title] if r.name.endswith(".bin")]
        roms.sort(key=lambda r: int(r.name.rsplit("(Track ", 1)[1].split(")")[0]))
        return roms

    def test_layout_equals_redump(self) -> None:
        for title in REAL_TITLES:
            with self.subTest(title):
                with chd.Chd(self.path(title), load_map=False) as c:
                    self.assertTrue(c.is_gd)
                    bins = self.bins(title)
                    self.assertEqual([t.size for t in c.tracks], [r.size for r in bins])

    def test_map_header_and_cd_metadata(self) -> None:
        with chd.Chd(self.path(REAL_TITLES[3])) as c:
            self.assertEqual(c.compressors[:3], ("cdlz", "cdzl", "cdfl"))
            self.assertEqual((c.hunk_bytes, c.unit_bytes), (19584, 2448))
            self.assertEqual(len(c.tracks), 15)
            self.assertGreater(c.hunk_count, 60000)

    def test_data_tracks_equal_redump(self) -> None:
        """The data tracks of the quickest disc, hashed completely (crc32 / md5 / sha1)."""
        title = REAL_TITLES[3]
        bins = self.bins(title)
        with chd.Chd(self.path(title)) as c:
            for t, rom in zip(c.tracks, bins):
                if t.is_audio:
                    continue
                h = chd.hash_track(c, t)
                self.assertEqual((h.size, h.crc32, h.md5, h.sha1), (rom.size, rom.crc, rom.md5, rom.sha1), t.number)

    def test_flac_audio_track_equals_redump(self) -> None:
        title = REAL_TITLES[3]
        bins = self.bins(title)
        with chd.Chd(self.path(title)) as c:
            t = c.tracks[13]               # track 14: 7.4 MB of FLAC audio
            self.assertTrue(t.is_audio)
            h = chd.hash_track(c, t)
            self.assertEqual((h.size, h.sha1, h.md5), (bins[13].size, bins[13].sha1, bins[13].md5))
            h2 = chd.hash_track(c, c.tracks[1])      # track 2: audio with 43k GD pad frames dropped
            self.assertEqual(h2.sha1, bins[1].sha1)

    def test_sidecar_md5_lists_agree_with_redump(self) -> None:
        import zipfile
        for title in REAL_TITLES:
            with self.subTest(title):
                z = REAL_DIR / title / f"{title}.zip"
                if not z.is_file():
                    self.skipTest("sidecar zip missing")
                with zipfile.ZipFile(z) as zf:
                    names = [n for n in zf.namelist() if n.lower().endswith(".md5")]
                    self.assertTrue(names)
                    text = zf.read(names[0]).decode("utf-16")
                digests = {line.split()[0].lower() for line in text.splitlines() if line.strip()}
                for rom in self.bins(title):
                    self.assertIn(rom.md5, digests, rom.name)

    @unittest.skipUnless(os.environ.get("ROMORG_REAL_CHD_FULL") == "1", "set ROMORG_REAL_CHD_FULL=1 (slow)")
    def test_every_track_of_every_title(self) -> None:
        for title in REAL_TITLES:
            t0 = time.time()
            bins = self.bins(title)
            with chd.Chd(self.path(title)) as c:
                for t, rom in zip(c.tracks, bins):
                    h = chd.hash_track(c, t)
                    self.assertEqual((h.size, h.crc32, h.md5, h.sha1), (rom.size, rom.crc, rom.md5, rom.sha1),
                                     f"{title} track {t.number}")
            print(f"\n  {title}: every track equals Redump ({time.time() - t0:.0f}s)", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
