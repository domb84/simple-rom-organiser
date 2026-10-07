"""Damaged or hostile files must end as the format's own error, quickly and without a crash: a CHD whose header
claims 56 million hunks, a mono FLAC frame, an RVZ whose 4-byte padding size says 2 GiB, a cue sheet with a number
of 5000 digits. Every scan opens every file it finds, so one such file must not stall, kill or abort it.
"""

from __future__ import annotations

import io
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
import rvztestlib as R  # noqa: E402
import test_rvz  # noqa: E402
from romorg import cdimage, chd, flacnative, rvz  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "chd"
ROOT = Path(__file__).resolve().parent.parent


def mono_frame(samples: int = 3528) -> bytes:
    """One CRC-valid FLAC frame with a single channel (CD audio is always two)."""
    b = T._Bits()
    b.put(0b11111111111110, 14)
    b.put(0, 1)
    b.put(0, 1)
    b.put(7, 4)
    b.put(9, 4)
    b.put(0, 4)                                  # channel code 0: mono
    b.put(4, 3)
    b.put(0, 1)
    b.put(0, 8)
    b.put(samples - 1, 16)
    b.put(T._flac_crc8(b.tobytes()), 8)
    T._subframe(b, [0] * samples, "verbatim", 16)
    b.align()
    b.put(T._flac_crc16(b.tobytes()), 16)
    return b.tobytes()


class FlacTest(unittest.TestCase):
    def test_a_mono_frame_is_an_error_not_a_crash(self) -> None:
        # libFLAC hands a mono frame one buffer; the binding used to read the second (NULL) and kill the process on
        # Linux. Run in a child process so that a crash is a failure of this test, not of the whole run.
        code = (
            "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
            "import test_damaged_formats as t\n"
            "from romorg import chd\n"
            "try:\n"
            "    chd._flac_raw(b'B' + t.mono_frame(), 3528 * 4)\n"
            "except chd.ChdError:\n"
            "    print('ChdError')\n" % (str(ROOT), str(Path(__file__).parent)))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ChdError"), r.stderr[-400:])

    @unittest.skipUnless(flacnative.available(), "no libFLAC")
    def test_the_binding_refuses_it_itself(self) -> None:
        with self.assertRaises(flacnative.FlacError):
            flacnative.decode_pcm(mono_frame(), 0, 3528)


class ChdTest(unittest.TestCase):
    def cd(self) -> bytearray:
        return bytearray((FIX / "cd_default.chd").read_bytes())

    def open(self, data) -> chd.Chd:
        return chd.Chd("damaged.chd", fileobj=io.BytesIO(bytes(data)))

    def test_a_hunk_count_the_file_cannot_hold(self) -> None:
        for off, mask in ((34, 0x01), (36, 0xFF), (32, 0xFF)):      # 56 M hunks, 56 M hunks, 9e18 bytes
            d = self.cd()
            d[off] ^= mask
            t = time.time()
            with self.assertRaises(chd.ChdError, msg=f"byte {off}"):
                self.open(d)
            self.assertLess(time.time() - t, 5)

    def test_a_metadata_number_that_is_not_one(self) -> None:
        d = self.cd()
        i = d.find(b"PREGAP:0")
        d[i + 7] = ord("x")
        with self.assertRaises(chd.ChdError):
            self.open(d)

    def test_an_offset_beyond_the_file(self) -> None:
        d = self.cd()
        d[48:56] = b"\xff" * 8                                        # the metadata offset
        with self.assertRaises(chd.ChdError):
            self.open(d)

    def test_a_track_longer_than_the_hunks(self) -> None:
        d = self.cd()
        i = d.find(b"FRAMES:24")
        d[i + 7:i + 9] = b"99"
        with self.open(d) as c:
            with self.assertRaises(chd.ChdError):
                c.read_track_range(c.tracks[0], 90, 1)

    def test_two_chds_that_name_each_other_as_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "loop.chd"
            d = bytearray((FIX / "child.chd").read_bytes())
            d[104:124] = d[84:104]                                    # its own SHA-1 as its parent's
            p.write_bytes(bytes(d))
            with chd.Chd(p) as c:
                c._ensure_map()
                idx = [i for i, t in enumerate(c._ctype) if t == chd._T_PARENT]
                self.assertTrue(idx)
                with self.assertRaises((chd.ChdError, chd.ChdUnsupported)):
                    c.read_hunk_raw(idx[0])


class RvzTest(unittest.TestCase):
    def test_a_padding_run_longer_than_its_group_costs_nothing(self) -> None:
        data = struct.pack(">I", 0x80000000 | 0x7FFFFFFF) + bytes(68)
        t = time.time()
        with self.assertRaises(rvz.RvzError):
            rvz.unpack(data, 0, 0x20000)
        self.assertLess(time.time() - t, 2)

    def test_offsets_and_sizes_from_the_file_are_checked(self) -> None:
        iso, runs = test_rvz.disc(1)
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "g.rvz")
            R.build_rvz(p, iso, runs, compression=R.NONE)
            with rvz.Rvz(p) as r:
                for off, n in ((1 << 63, 4), (1 << 62, 4), (0, 0xFFFFFFFF), (-1, 4)):
                    with self.assertRaises(rvz.RvzError):
                        r._blob(off, n, "x")
                gi = next(i for i, (o, s, pk) in enumerate(r.groups) if pk)
                piece = next(i for i, (_o, _l, g) in enumerate(r.layout) if g == gi)
                o, s, _pk = r.groups[gi]
                r.groups[gi] = (o, s | 0x80000000, 0xFFFFFFF0)      # a packed size of 4 GiB
                t = time.time()
                with self.assertRaises(rvz.RvzError):
                    r.piece(piece)
                self.assertLess(time.time() - t, 2)


class CueTest(unittest.TestCase):
    def test_numbers_and_names_that_are_not_sane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.bin").write_bytes(bytes(2352))
            cases = {
                "big.cue": 'FILE "a.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 %s:00:00\n' % ("9" * 5000),
                "nul.cue": 'FILE "a\x00.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n',
            }
            for name, text in cases.items():
                Path(tmp, name).write_text(text)
                with self.assertRaises(cdimage.ImageError, msg=name):
                    cdimage.parse_cue(Path(tmp, name))


if __name__ == "__main__":
    unittest.main()
