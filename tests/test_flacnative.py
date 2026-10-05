"""libFLAC through ctypes (romorg.flacnative): bit-identical to the pure-Python decoder, error handling, discovery."""

from __future__ import annotations

import os
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
from romorg import bundle, flacdec, flacnative  # noqa: E402

NATIVE = flacnative.available()
needs_native = unittest.skipUnless(NATIVE, "libFLAC is not installed here (set ROMORG_LIBFLAC)")


def pcm(n: int, seed: int = 3) -> bytes:
    rnd = random.Random(seed)
    out = bytearray()
    for i in range(n):
        out += struct.pack("<hh", int(5000 * (i % 50 - 25) / 25) + rnd.randint(-60, 60), rnd.randint(-3000, 3000))
    return bytes(out)


@needs_native
class NativeEqualsPythonTest(unittest.TestCase):
    def both(self, stream: bytes, samples: int):
        a = flacdec.decode_frames(stream, 0, samples)
        b = flacnative.decode_frames(stream, 0, samples)
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[1], b[1])
        return b

    def test_every_stereo_mode_and_predictor(self) -> None:
        data = pcm(1500)
        for stereo in ("indep", "left_side", "side_right", "mid_side"):
            for kinds in (("fixed2", "fixed2"), ("verbatim", "fixed1"), ("constant", "constant")):
                for block in (400, 1176, 4096):
                    with self.subTest(stereo=stereo, kinds=kinds, block=block):
                        src = struct.pack("<hh", 5, -9) * 1500 if kinds[0] == "constant" else data
                        stream = T.flac_stream(src, block=block, kinds=kinds, stereo=stereo)
                        got, end = self.both(stream + b"SUBCODE" * 5, len(src) // 4)
                        self.assertEqual(got.tobytes(), src)
                        self.assertEqual(end, len(stream))                 # consumed exactly the frames

    def test_big_endian_output_is_the_chd_hunk_order(self) -> None:
        data = pcm(588 * 8)
        stream = T.flac_stream(data, block=588 * 8)
        be, end = flacnative.decode_pcm(stream, 0, len(data) // 4, big_endian=True)
        le, _ = flacnative.decode_pcm(stream, 0, len(data) // 4, big_endian=False)
        self.assertEqual(bytes(le), data)
        swapped = bytearray(data)
        swapped[0::2], swapped[1::2] = data[1::2], data[0::2]
        self.assertEqual(bytes(be), bytes(swapped))

    def test_start_offset_and_buffer_types(self) -> None:
        data = pcm(800)
        stream = b"\x11" * 7 + T.flac_stream(data, block=400)
        for buf in (stream, bytearray(stream), memoryview(stream)):
            got, end = flacnative.decode_frames(buf, 7, 800)
            self.assertEqual(got.tobytes(), data)
            self.assertEqual(end, len(stream))

    def test_decoders_are_per_thread(self) -> None:
        import threading
        data = pcm(2000)
        stream = T.flac_stream(data, block=500)
        out = []

        def run() -> None:
            for _ in range(20):
                out.append(flacnative.decode_frames(stream, 0, 2000)[0].tobytes() == data)
        ts = [threading.Thread(target=run) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(out, [True] * 80)

    def test_damaged_data_raises_flac_error(self) -> None:
        data = pcm(800)
        stream = T.flac_stream(data, block=400)
        with self.assertRaises(flacnative.FlacError):                          # truncated
            flacnative.decode_frames(stream[:30], 0, 800)
        with self.assertRaises(flacnative.FlacError):                          # no frames at all
            flacnative.decode_frames(b"\x00" * 64, 0, 400)
        with self.assertRaises(flacnative.FlacError):                          # more samples than the stream has
            flacnative.decode_frames(stream, 0, 1200)
        with self.assertRaises(flacnative.FlacError):                          # the frame is larger than the hunk
            flacnative.decode_frames(stream, 0, 300)
        bad = bytearray(stream)
        bad[40] ^= 0x55                                                        # a frame CRC the pure decoder never checks
        with self.assertRaises(flacnative.FlacError):
            flacnative.decode_frames(bytes(bad), 0, 800)
        got = flacnative.decode_frames(stream, 0, 800)[0].tobytes()            # and the decoder still works afterwards
        self.assertEqual(got, data)

    def test_speed_is_far_above_the_python_decoder(self) -> None:
        import time
        data = pcm(588 * 8 * 40)
        stream = T.flac_stream(data, block=588 * 8)
        t = time.perf_counter()
        for _ in range(3):
            flacnative.decode_pcm(stream, 0, len(data) // 4)
        native = (time.perf_counter() - t) / 3
        t = time.perf_counter()
        flacdec.decode_frames(stream, 0, len(data) // 4)
        py = time.perf_counter() - t
        self.assertLess(native * 10, py)                                       # in practice ~100x


class DiscoveryTest(unittest.TestCase):
    def tearDown(self) -> None:
        flacnative.reload()

    @needs_native
    def test_bundled_library_comes_before_the_system_one(self) -> None:
        cands = [Path(c) for c in flacnative._candidates() if os.path.isabs(c) and Path(c).exists()]
        if not cands:
            self.skipTest("no absolute path of libFLAC to link to")
        real = cands[0].resolve()
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "tools" / "lib"
            lib.mkdir(parents=True)
            (lib / "libFLAC.so.14").symlink_to(real)
            with mock.patch.dict(os.environ, {bundle.ENV_BUNDLE: tmp}):
                os.environ.pop(flacnative.ENV_LIB, None)
                flacnative.reload()
                self.assertTrue(flacnative.library_path().startswith(str(lib)))
                self.assertTrue(flacnative.available())
                self.assertEqual(flacnative.status()["library"], flacnative.library_path())

    @needs_native
    def test_env_override_wins_and_a_bad_one_is_skipped(self) -> None:
        real = flacnative.library_path()
        with mock.patch.dict(os.environ, {flacnative.ENV_LIB: "/nonexistent/libFLAC.so.14"}):
            flacnative.reload()
            self.assertTrue(flacnative.available())                           # falls through to the next candidate
        with mock.patch.dict(os.environ, {flacnative.ENV_LIB: real}):
            flacnative.reload()
            self.assertEqual(flacnative.library_path(), real)

    def test_without_libflac_libsndfile_decodes_the_audio_when_the_end_is_not_needed(self) -> None:
        from romorg import nativeflac
        if not nativeflac.available():
            self.skipTest("no libsndfile on this machine")
        data = pcm(800)
        stream = T.flac_stream(data, block=400)
        frames = stream[stream.index(b"\xff\xf8"):] if stream[:4] == b"fLaC" else stream
        with mock.patch("romorg.flacnative._candidates", return_value=[]):
            flacnative.reload()
            self.assertTrue(flacnative.available())
            self.assertEqual(flacnative.status()["library"], "libsndfile")
            with mock.patch("romorg.flacdec.decode_frames", side_effect=AssertionError("the Python decoder ran")):
                le, end = flacnative.decode_pcm(frames, 0, 800, need_end=False)
                be, _ = flacnative.decode_pcm(frames, 0, 800, big_endian=True, need_end=False)
            self.assertEqual((bytes(le), end), (data, 0))
            self.assertEqual(bytes(be[0:2]), bytes([data[1], data[0]]))
            got, end = flacnative.decode_pcm(frames, 0, 800)                    # the end offset: the Python decoder
            self.assertEqual(bytes(got), data)
            self.assertGreater(end, 0)
        flacnative.reload()

    def test_without_libflac_the_python_decoder_does_the_work(self) -> None:
        data = pcm(800)
        stream = T.flac_stream(data, block=400)
        with mock.patch("romorg.flacnative._candidates", return_value=[]), \
                mock.patch("romorg.nativeflac._get", return_value=None):      # no libsndfile stand-in either
            flacnative.reload()
            self.assertFalse(flacnative.available())
            st = flacnative.status()
            self.assertFalse(st["native"])
            self.assertIn("not found", st["note"])
            got, end = flacnative.decode_frames(stream, 0, 800)
            self.assertEqual(got.tobytes(), data)
            be, _ = flacnative.decode_pcm(stream, 0, 800, big_endian=True)
            self.assertEqual(bytes(be[0:2]), bytes([data[1], data[0]]))

    def test_disabled_by_environment(self) -> None:
        with mock.patch.dict(os.environ, {"ROMORG_NO_NATIVE_FLAC": "1"}):
            flacnative.reload()
            self.assertFalse(flacnative.available())
            self.assertIn("disabled", flacnative.status()["note"])


if __name__ == "__main__":
    unittest.main()
