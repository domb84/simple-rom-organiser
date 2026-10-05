"""Everything chdman reads, the built-in reader reads: all codecs, parent files, the old CHD versions.

``tests/fixtures/chd`` holds small CHDs written by the real chdman 0.289 from generated content (see
``make_fixtures.py`` there; ``sums.json`` has the SHA-1 of what went in). They are the proof against the real
format; the synthetic files of ``chdtestlib`` cover what no current chdman writes any more (versions 1 to 4) and the
corners the real files do not reach.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from romorg import chd, chdhuff, chdsched, zstddec, zstdnative
from tests import chdtestlib as T

FIX = Path(__file__).resolve().parent / "fixtures" / "chd"
SUMS = json.loads((FIX / "sums.json").read_text())


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def codecs_used(c: chd.Chd) -> set:
    c._ensure_map()
    return {c.compressors[t] for t in c._ctype if t < 4}


class RealChdmanFilesTest(unittest.TestCase):
    """Files made by chdman 0.289."""

    def check(self, name: str, source: str, want_codecs=(), **kw) -> chd.Chd:
        with chd.Chd(FIX / name, **kw) as c:
            got = c.verify()
            self.assertEqual(got["raw_sha1"], SUMS[source], name)
            self.assertEqual((got["raw"], got["overall"]), (True, True), name)
            for codec in want_codecs:
                self.assertIn(codec, codecs_used(c), name)
            return c

    def test_default_compression_mixes_flac_huffman_and_deflate(self) -> None:
        for name in ("raw_default.chd", "hd_default.chd", "dvd_default.chd"):
            self.check(name, "src", ("flac", "huff", "zlib"))

    def test_kinds(self) -> None:
        kinds = {}
        for name in ("raw_default.chd", "hd_default.chd", "dvd_default.chd", "ld_mono.chd", "cd_default.chd"):
            with chd.Chd(FIX / name, load_map=False) as c:
                kinds[name] = c.describe()["kind"]
        self.assertEqual(kinds, {"raw_default.chd": "data", "hd_default.chd": "hd", "dvd_default.chd": "dvd",
                                 "ld_mono.chd": "laserdisc", "cd_default.chd": "cd"})

    def test_huffman_only(self) -> None:
        self.check("raw_huff.chd", "src", ("huff",))

    def test_huffman_through_zlib_equals_the_plain_loop(self) -> None:
        with chd.Chd(FIX / "raw_huff.chd") as c:
            fast = b"".join(c.iter_raw())
        with mock.patch.object(chdhuff, "_inflate_codes", return_value=None):
            with chd.Chd(FIX / "raw_huff.chd") as c:
                self.assertEqual(b"".join(c.iter_raw()), fast)
        self.assertEqual(sha1(fast), SUMS["src"])

    def test_flac_hunks_without_a_flac_library(self) -> None:
        T.disable_native_flac(self)
        self.check("raw_default.chd", "src", ("flac",))

    def test_zstandard_mix_with_and_without_a_library(self) -> None:
        self.check("raw_zstd_mix.chd", "src", ("zstd", "huff", "flac"))
        with mock.patch.dict(os.environ, {zstdnative.ENV_OFF: "1"}):
            zstdnative.reload()
            try:
                self.assertFalse(zstdnative.native())
                self.check("raw_zstd_mix.chd", "src", ("zstd",))
                with chd.Chd(FIX / "cd_zstd.chd") as c:
                    self.assertEqual([chd.hash_track(c, t).sha1 for t in c.tracks],
                                     [SUMS["cd_data"], SUMS["cd_audio"]])
                    self.assertTrue(c.verify_raw_sha1())
            finally:
                zstdnative.reload()

    def test_dvd_tracks_and_the_scheduler(self) -> None:
        with chd.Chd(FIX / "dvd_default.chd") as c:
            self.assertEqual(chd.hash_track(c, c.tracks[0]).sha1, SUMS["src"])
        with chd.Chd(FIX / "dvd_default.chd", load_map=False) as c, chdsched.Scheduler(2, chunk_bytes=16384) as s:
            self.assertEqual(s.hash_tracks(c, [0])[0]["sha1"], SUMS["src"])

    def test_cd_made_by_chdman(self) -> None:
        for name, codec in (("cd_default.chd", "cdzl"), ("cd_zstd.chd", "cdzs")):
            for threads in (1, 4):
                with mock.patch.object(chd.Chd, "threads", threads), chd.Chd(FIX / name) as c:
                    self.assertEqual([t.type for t in c.tracks], ["MODE1_RAW", "AUDIO"])
                    self.assertEqual([chd.hash_track(c, t).sha1 for t in c.tracks],
                                     [SUMS["cd_data"], SUMS["cd_audio"]], name)
                    self.assertEqual({"cdfl", codec}, codecs_used(c))
                    got = c.verify()
                    self.assertEqual((got["raw"], got["overall"]), (True, True))

    def test_decode_threads_of_several_readers_at_once_never_hang(self) -> None:
        """Readers on several threads at once, each with its own number of decode threads (the shared decode pool
        must neither deadlock nor be replaced under a reader that is using it)."""
        import threading
        errors: list = []
        results: dict = {}

        def read(k: int, threads: int) -> None:
            try:
                with chd.Chd(FIX / "cd_default.chd") as c:
                    c.threads = threads
                    results[k] = [chd.hash_track(c, t).sha1 for t in c.tracks]
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        workers = [threading.Thread(target=read, args=(k, (1, 2, 4, 8, 3, 6)[k % 6]), daemon=True)
                   for k in range(12)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=120)
        self.assertFalse([w for w in workers if w.is_alive()], "a reader hangs")
        self.assertEqual(errors, [])
        self.assertEqual(set(map(tuple, results.values())), {(SUMS["cd_data"], SUMS["cd_audio"])})

    def test_laserdisc(self) -> None:
        for name in ("ld_mono.chd", "ld_stereo.chd"):
            with chd.Chd(FIX / name) as c:
                self.assertEqual(codecs_used(c), {"avhu"})
                got = c.verify()
                self.assertEqual((got["raw"], got["overall"]), (True, True), name)
                frame = c.read_hunk_raw(0)
                self.assertEqual(frame[:4], b"chav")
                self.assertEqual(len(frame), c.hunk_bytes)

    # -- parents
    def test_child_finds_its_parent_in_the_folder(self) -> None:
        c = self.check("child.chd", "src2")
        self.assertTrue(c.has_parent)
        with chd.Chd(FIX / "child.chd") as c:
            c._ensure_map()
            self.assertIn(chd._T_PARENT, set(c._ctype))
            self.assertEqual(chd.find_parent(c.path, c.parent_sha1), str(FIX / "raw_default.chd"))

    def test_child_without_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alone = Path(tmp) / "child.chd"
            shutil.copy(FIX / "child.chd", alone)
            # a CHD of the same data but with hard disk metadata: another SHA-1, so not the parent. (raw_huff.chd
            # WOULD do: a CHD's SHA-1 names its content, however it is compressed.)
            shutil.copy(FIX / "hd_default.chd", Path(tmp) / "other.chd")
            with chd.Chd(alone) as c:
                self.assertEqual(c.describe()["kind"], "data")              # header and metadata are readable
                with self.assertRaises(chd.ChdUnsupported) as cm:
                    c.verify()
            self.assertFalse(cm.exception.needs_chdman)
            self.assertIn("parent", str(cm.exception))
            # given by hand (chdman's --inputparent)
            with chd.Chd(alone, parent=FIX / "raw_default.chd") as c:
                self.assertEqual(c.verify()["raw_sha1"], SUMS["src2"])
            # the wrong parent is refused, not decoded into garbage
            with chd.Chd(alone, parent=FIX / "hd_default.chd") as c, self.assertRaises(chd.ChdError):
                c.verify()
            shutil.copy(FIX / "raw_huff.chd", Path(tmp) / "same data.chd")
            with chd.Chd(alone) as c:
                self.assertEqual(c.verify()["raw_sha1"], SUMS["src2"])

    def test_uncompressed_files_have_no_checksum(self) -> None:
        with chd.Chd(FIX / "raw_none.chd") as c:
            got = c.verify()
            self.assertEqual((got["raw"], got["overall"], got["raw_sha1"]), (None, None, SUMS["src"]))
            self.assertFalse(c.verify_raw_sha1())
            self.assertIn(chd._T_ZERO, set(c._ctype))                       # hunks of zeros are not stored at all

    def test_uncompressed_child_takes_missing_hunks_from_the_parent(self) -> None:
        with chd.Chd(FIX / "child_none.chd") as c:
            self.assertEqual(sha1(b"".join(c.iter_raw())), SUMS["src2"])
            self.assertIn(chd._T_PARENT_BYTES, set(c._ctype))

    def test_child_of_a_parent_without_checksum_needs_it_named(self) -> None:
        with chd.Chd(FIX / "child_of_none.chd") as c, self.assertRaises(chd.ChdUnsupported):
            c.verify()
        self.check("child_of_none.chd", "src2", parent=FIX / "raw_none.chd")


class HuffmanTest(unittest.TestCase):
    def roundtrip(self, data: bytes, **kw) -> bytes:
        comp = T.huff_compress(data, **kw)
        self.assertEqual(chdhuff.huff_decode(comp, len(data)), data)
        return comp

    def test_shapes_of_data(self) -> None:
        rnd = random.Random(11)
        skew = bytes(rnd.choices(range(256), weights=[1000 / (i + 1) ** 1.3 for i in range(256)], k=4096))
        for data in (skew, bytes(4096), b"ab" * 2048, bytes(rnd.randrange(256) for _ in range(4096)),
                     bytes(range(256)) * 16):
            for rle in (True, False):
                self.roundtrip(data, rle=rle)

    def test_a_flat_tree_falls_back_to_the_loop_midway(self) -> None:
        rnd = random.Random(5)
        data = bytes(rnd.choice(b"ABCDEFGH") for _ in range(4096))      # every symbol common: restart after restart
        with mock.patch.object(chdhuff.Huffman, "decode_bytes", autospec=True,
                               side_effect=chdhuff.Huffman.decode_bytes) as loop:
            self.roundtrip(data)
        self.assertTrue(loop.called)

    def test_codes_longer_than_deflate_allows(self) -> None:
        fib = [1, 1]
        while len(fib) < 16:
            fib.append(fib[-1] + fib[-2])
        data = b"".join(bytes([i]) * n for i, n in enumerate(fib))
        data += bytes([15]) * (4096 - len(data))
        lengths = T.huff_lengths([data.count(bytes([i])) for i in range(256)])
        self.assertGreater(max(lengths), 14)
        comp = self.roundtrip(data)
        self.assertIsNone(chdhuff._inflate_codes(bytes(lengths), comp, 0, 16))

    def test_damage_is_an_error(self) -> None:
        rnd = random.Random(2)
        data = bytes(rnd.choices(range(256), weights=[1000 / (i + 1) ** 1.3 for i in range(256)], k=4096))
        comp = T.huff_compress(data)
        with self.assertRaises(chdhuff.HuffError):
            chdhuff.huff_decode(comp[:len(comp) // 2], 4096)
        with self.assertRaises(chdhuff.HuffError):
            chdhuff.huff_decode(b"\xff" * 40, 4096)

    def test_huff_and_flac_hunks_in_a_dvd_chd(self) -> None:
        iso = T.make_iso(24, 6) + T.make_audio_track(4, 3)[:8192]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "mix.chd"
            T.build_dvd_chd(p, iso, pick=lambda h: ("lzma", "zlib", "huff", "flac")[h % 4], hunk_sectors=2)
            with chd.Chd(p) as c:
                self.assertEqual(codecs_used(c), {"lzma", "zlib", "huff", "flac"})
                self.assertEqual(b"".join(c.iter_track(c.tracks[0])), iso)
                self.assertTrue(c.verify_raw_sha1())


class AvHuffTest(unittest.TestCase):
    W, H = 16, 8

    def picture(self) -> bytes:
        top = bytes((x * 7 + y * 3) & 0xFF if x % 2 == 0 else 128 for y in range(self.H // 2)
                    for x in range(self.W * 2))
        return top + bytes(self.W * self.H)                     # the lower half: long runs

    def test_huffman_and_raw_audio(self) -> None:
        channels = [[1000 * ((i % 7) - 3) for i in range(40)], [i * 11 - 200 for i in range(40)]]
        for audio in ("huffman", "raw"):
            comp = T.avhuff_compress(b"meta!", channels, self.W, self.H, self.picture(), audio=audio)
            want = T.avhuff_raw(b"meta!", channels, self.W, self.H, self.picture(), 1024)
            self.assertEqual(chdhuff.avhuff_decode(comp, 1024), want, audio)

    def test_run_codes(self) -> None:
        self.assertEqual([chdhuff._rle_count(c) for c in (0x100, 0x101, 0x107, 0x108, 0x109, 0x10F)],
                         [8, 9, 15, 16, 32, 2048])

    def test_video_only_and_damage(self) -> None:
        comp = T.avhuff_compress(b"", [], self.W, self.H, self.picture())
        self.assertEqual(chdhuff.avhuff_decode(comp, 512), T.avhuff_raw(b"", [], self.W, self.H, self.picture(), 512))
        with self.assertRaises(chdhuff.HuffError):
            chdhuff.avhuff_decode(comp[:len(comp) - 9], 512)
        with self.assertRaises(chdhuff.HuffError):
            chdhuff.avhuff_decode(comp, 100)                    # the frame does not fit the hunk

    def test_v4_laserdisc_chd(self) -> None:
        hunk = 600
        frames = []
        for f in range(3):
            pic = bytes((b + f) & 0xFF for b in self.picture())
            ch = [[(i * 13 + f * 100) % 3000 - 1500 for i in range(30)]]
            frames.append((T.avhuff_compress(b"m", ch, self.W, self.H, pic), T.avhuff_raw(b"m", ch, self.W, self.H, pic, hunk)))
        data = b"".join(raw for _c, raw in frames)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ld4.chd"
            T.build_old_chd(p, 4, data, hunk, compression=3, avhu=lambda h: frames[h][0], kinds=lambda h: "zlib",
                            metadata=[(b"AVAV", b"FPS:29.970 WIDTH:16 HEIGHT:8 INTERLACED:0 CHANNELS:1 SAMPLERATE:48000\0")])
            with chd.Chd(p) as c:
                self.assertEqual((c.version, c.compressors[0], c.describe()["kind"]), (4, "avhu", "laserdisc"))
                self.assertEqual(c.verify(), {"raw": True, "overall": True, "raw_sha1": sha1(data)})


class OldVersionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def disk(self, hunks: int = 12, hunk_bytes: int = 2048) -> bytes:
        rnd = random.Random(8)
        words = [bytes(rnd.choice(b"abcdefgh ") for _ in range(rnd.randrange(3, 9))) for _ in range(40)]
        text = b"".join(rnd.choice(words) for _ in range(hunks * hunk_bytes // 4))[:(hunks - 4) * hunk_bytes]
        pattern = b"\xde\xad\xbe\xef\x01\x02\x03\x04" * (hunk_bytes // 8)
        noise = bytes(rnd.randrange(256) for _ in range(hunk_bytes))
        return text[:hunk_bytes * 2] + pattern + text[hunk_bytes * 2:] + noise + pattern + text[:hunk_bytes]

    def test_v3_and_v4_entry_types(self) -> None:
        data = self.disk()
        hb = 2048

        def kinds(h: int) -> str:
            raw_h = data[h * hb:(h + 1) * hb]
            if raw_h == raw_h[:8] * (hb // 8):
                return "mini"
            if h == 9:
                return "raw"
            return "self" if data.index(raw_h) < h * hb and data.index(raw_h) % hb == 0 else "zlib"
        for version in (3, 4):
            p = self.dir / f"v{version}.chd"
            meta = [(b"GDDD", b"CYLS:3,HEADS:2,SECS:8,BPS:512\0")]
            info = T.build_old_chd(p, version, data, hb, kinds=kinds, metadata=meta)
            with chd.Chd(p) as c:
                self.assertEqual((c.version, c.hunk_bytes, c.unit_bytes, c.compressors[0]), (version, hb, 512, "zlib"))
                self.assertEqual(c.describe()["kind"], "hd")
                self.assertEqual(b"".join(c.iter_raw()), data)
                self.assertEqual({chd._T_MINI, chd._T_SELF, chd._T_NONE, 0}, set(c._ctype))
                self.assertEqual(c.verify(), {"raw": True, "overall": True, "raw_sha1": info["raw_sha1"]})
                self.assertEqual(c.read_bytes(hb - 5, 20), data[hb - 5:hb + 15])
            # one flipped byte of the data is noticed
            raw = bytearray(p.read_bytes())
            with chd.Chd(p) as c:
                off = c._coff[9]
            raw[off + 7] ^= 1
            p.write_bytes(bytes(raw))
            with chd.Chd(p) as c:
                self.assertEqual(c.verify()["raw"], False)

    def test_v1_and_v2_hard_disks(self) -> None:
        data = self.disk(hunks=12, hunk_bytes=2048)             # 48 sectors of 512 bytes
        for version in (1, 2):
            p = self.dir / f"v{version}.chd"
            info = T.build_old_chd(p, version, data, 2048, geometry=(3, 2, 8))
            with chd.Chd(p) as c:
                self.assertEqual((c.version, c.logical_bytes, c.unit_bytes, c.sha1), (version, len(data), 512, ""))
                self.assertEqual(c.describe()["kind"], "hd")
                self.assertEqual(b"".join(c.iter_raw()), data)
                got = c.verify()
                self.assertEqual((got["raw"], got["raw_sha1"]), (True, info["md5"]))

    def test_v4_parent_by_hunk_number(self) -> None:
        base = self.disk()
        hb = 2048
        child = bytearray(base)
        child[3 * hb:4 * hb] = bytes(reversed(base[3 * hb:4 * hb]))
        child[7 * hb + 9] ^= 0xFF
        pinfo = T.build_old_chd(self.dir / "base.chd", 4, base, hb, kinds=lambda h: "zlib")
        T.build_old_chd(self.dir / "delta.chd", 4, bytes(child), hb, parent=pinfo,
                        kinds=lambda h: "zlib" if h in (3, 7) else "parent")
        with chd.Chd(self.dir / "delta.chd") as c:
            self.assertTrue(c.has_parent)
            self.assertEqual(b"".join(c.iter_raw()), bytes(child))
            self.assertEqual(c.verify()["raw"], True)
        (self.dir / "base.chd").rename(self.dir / "elsewhere.bin")
        with chd.Chd(self.dir / "delta.chd") as c, self.assertRaises(chd.ChdUnsupported):
            c.read_hunk_raw(0)

    def cd(self) -> tuple:
        data = T.make_data_track(10, 4)
        audio = T.make_audio_track(6, 9)                        # as in a .bin: little-endian samples
        swapped = bytearray(audio)
        swapped[0::2], swapped[1::2] = audio[1::2], audio[0::2]
        frames = bytearray()
        for sectors, count in ((data, 10), (bytes(2 * 2352), 2), (bytes(swapped), 6), (bytes(2 * 2352), 2)):
            for i in range(count):
                frames += sectors[i * 2352:(i + 1) * 2352] + bytes(96)
        return data, audio, bytes(frames)

    def test_cd_of_version_4_with_text_track_metadata(self) -> None:
        data, audio, frames = self.cd()
        meta = [(b"CHTR", b"TRACK:1 TYPE:MODE1_RAW SUBTYPE:NONE FRAMES:10\0"),
                (b"CHTR", b"TRACK:2 TYPE:AUDIO SUBTYPE:NONE FRAMES:6\0")]
        p = self.dir / "cd4.chd"
        T.build_old_chd(p, 4, frames, 4 * 2448, metadata=meta)
        with chd.Chd(p) as c:
            self.assertEqual((c.describe()["kind"], c.unit_bytes, c.frames_per_hunk), ("cd", 2448, 4))
            self.assertEqual([b"".join(c.iter_track(t)) for t in c.tracks], [data, audio])
            self.assertEqual(c.verify()["overall"], True)
        with chd.Chd(p, load_map=False) as c, chdsched.Scheduler(2, chunk_bytes=8192) as s:
            got = s.hash_tracks(c, [0, 1])
            self.assertEqual([got[0]["sha1"], got[1]["sha1"]], [sha1(data), sha1(audio)])

    def test_cd_of_version_3_with_binary_track_metadata(self) -> None:
        data, audio, frames = self.cd()
        for order in ("<", ">"):                                # written by a little- and by a big-endian machine
            p = self.dir / f"cd3{ord(order)}.chd"
            T.build_old_chd(p, 3, frames, 4 * 2448, metadata=[(b"CHCD", T.old_cd_metadata([(1, 10), (7, 6)], order))])
            with chd.Chd(p) as c:
                self.assertEqual([(t.number, t.type, t.frames, t.start) for t in c.tracks],
                                 [(1, "MODE1_RAW", 10, 0), (2, "AUDIO", 6, 12)])
                self.assertEqual([b"".join(c.iter_track(t)) for t in c.tracks], [data, audio])

    def test_bad_old_headers(self) -> None:
        p = self.dir / "v4.chd"
        T.build_old_chd(p, 4, self.disk(), 2048)
        raw = bytearray(p.read_bytes())
        for patch, error in (((20, struct.pack(">I", 9)), chd.ChdUnsupported),      # unknown compression
                             ((8, struct.pack(">I", 99)), chd.ChdError),             # wrong header length
                             ((12, struct.pack(">I", 0)), chd.ChdUnsupported)):      # version 0
            bad = bytearray(raw)
            bad[patch[0]:patch[0] + 4] = patch[1]
            q = self.dir / "bad.chd"
            q.write_bytes(bytes(bad))
            with self.assertRaises(error):
                chd.Chd(q)
        (self.dir / "bad.chd").write_bytes(bytes(raw[:200]))                       # the map is cut off
        with self.assertRaises(chd.ChdError):
            chd.Chd(self.dir / "bad.chd")


class V5MapTest(unittest.TestCase):
    """Things chdman 0.289 writes that the fixtures do not happen to contain."""

    def test_self_reference_loop_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "loop.chd"
            T.build_old_chd(p, 4, bytes(4096), 2048, kinds=lambda h: "zlib")
            raw = bytearray(p.read_bytes())
            raw[108:124] = struct.pack(">QIHBB", 1, 0, 0, 0, 4)        # hunk 0 = hunk 1
            raw[124:140] = struct.pack(">QIHBB", 0, 0, 0, 0, 4)        # hunk 1 = hunk 0
            p.write_bytes(bytes(raw))
            with chd.Chd(p) as c, self.assertRaises(chd.ChdError):
                c.read_hunk_raw(0)

    def test_repeated_hunks_are_decoded_once(self) -> None:
        iso = T.make_iso(4, 2) * 6
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rep.chd"
            T.build_dvd_chd(p, iso, pick=lambda h: "huff")
            with chd.Chd(p) as c:
                with mock.patch.object(chdhuff, "huff_decode", side_effect=chdhuff.huff_decode) as dec:
                    self.assertEqual(b"".join(c.iter_raw()), iso)
                unique = len(set(iso[i:i + 4096] for i in range(0, len(iso), 4096)))
                self.assertLess(unique * 2, c.hunk_count)
                self.assertLessEqual(dec.call_count, unique * 2)      # itself, and once more for all its copies

    def test_unknown_codec_is_the_one_thing_left_for_chdman(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "new.chd"
            T.build_dvd_chd(p, T.make_iso(8, 1), codecs=("lzma", "zlib", "wxyz", ""), pick=lambda h: "wxyz")
            with chd.Chd(p) as c, self.assertRaises(chd.ChdUnsupported) as cm:
                c.read_hunk_raw(0)
            self.assertTrue(cm.exception.needs_chdman)


class ZstdDecoderTest(unittest.TestCase):
    """The pure-Python Zstandard decoder against the real library (skipped without one)."""

    def setUp(self) -> None:
        if T.zstd_compress(b"x") is None:
            self.skipTest("no Zstandard library to compress with")

    def test_frames_of_many_shapes(self) -> None:
        rnd = random.Random(21)
        source = Path(chd.__file__).read_bytes()
        cases = [b"", b"a", bytes(5000), bytes(rnd.randrange(256) for _ in range(3000)), source[:20000],
                 source[:150000] * 2,                            # more than one block
                 bytes(rnd.choices(range(256), weights=[1000 / (i + 1) ** 1.3 for i in range(256)], k=19584)),
                 (b"abcabcabd" * 3000)[:19584], T.make_audio_track(8, 3), T.make_iso(9, 4)]
        for data in cases:
            self.assertEqual(zstddec.decompress(T.zstd_compress(data)), data, len(data))

    def test_not_a_frame(self) -> None:
        for bad in (b"", b"nope" * 4, T.zstd_compress(b"hello" * 100)[:-3]):
            with self.assertRaises(zstddec.ZstdDecodeError):
                zstddec.decompress(bad)

    def test_selfcheck_frame(self) -> None:
        self.assertEqual(zstddec.decompress(bytes.fromhex("28b52ffd2005290000") + b"romor"), b"romor")


if __name__ == "__main__":
    unittest.main()
