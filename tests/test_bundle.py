"""The AppImage's bundled libraries (libFLAC, libzstd): found ahead of the system's, and REQUIRED by the self-check."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from romorg import bundle, flacnative, selfcheck, zstdnative  # noqa: E402


def fake_bundle(tmp: str, names=("libFLAC.so.14", "libzstd.so.1", "libzstd.so.1.5.7")) -> Path:
    lib = Path(tmp) / "tools" / "lib"
    lib.mkdir(parents=True)
    for n in names:
        (lib / n).write_bytes(b"not a real library")
    (Path(tmp) / "licenses").mkdir()
    (Path(tmp) / "licenses" / "THIRD_PARTY.md").write_text("x")
    return Path(tmp)


class ZstdDiscoveryTest(unittest.TestCase):
    def test_bundled_libzstd_is_tried_before_the_system_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_bundle(tmp)
            env = {k: v for k, v in os.environ.items() if k != zstdnative.ENV_LIB}
            env[bundle.ENV_BUNDLE] = str(root)
            with mock.patch.dict(os.environ, env, clear=True):
                cands = zstdnative._candidates()
            lib = root / "tools" / "lib"
            self.assertEqual(cands[:2], [str(lib / "libzstd.so.1"), str(lib / "libzstd.so.1.5.7")])
            self.assertIn("libzstd.so.1", cands[2:])                # the system's bare name comes after them

    def test_a_bundled_library_that_does_not_load_falls_through_to_the_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_bundle(tmp)
            with mock.patch.dict(os.environ, {bundle.ENV_BUNDLE: str(root)}), \
                    mock.patch.object(zstdnative, "_stdlib", return_value=None):
                os.environ.pop(zstdnative.ENV_LIB, None)
                os.environ.pop(zstdnative.ENV_OFF, None)
                zstdnative.reload()
                try:
                    st = zstdnative.status()
                    self.assertNotIn(str(root), st["library"] or "")        # garbage bytes never load
                    self.assertEqual(zstdnative.decompress(bytes.fromhex("28b52ffd2005290000") + b"romor", 5), b"romor")
                finally:
                    zstdnative.reload()


class BundledLibraryCheckTest(unittest.TestCase):
    def check(self, root: Path, flac, zstd_status):
        with mock.patch.object(flacnative, "library_path", return_value=flac), \
                mock.patch.object(flacnative, "status", return_value={"native": bool(flac), "library": flac, "note": "n/a"}), \
                mock.patch.object(zstdnative, "status", return_value=zstd_status):
            return selfcheck.check_bundled_libraries(root)

    def test_both_from_the_bundle_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_bundle(tmp)
            lib = root / "tools" / "lib"
            res = self.check(root, str(lib / "libFLAC.so.14"),
                             {"native": True, "library": str(lib / "libzstd.so.1"), "note": ""})
            self.assertEqual([s for s, _ in res], ["OK", "OK"])
            self.assertTrue(res[1][1].startswith(f"libzstd {lib}"))

    def test_the_system_libzstd_fails_even_though_it_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_bundle(tmp)
            lib = root / "tools" / "lib"
            res = self.check(root, str(lib / "libFLAC.so.14"),
                             {"native": True, "library": "/usr/lib/libzstd.so.1", "note": ""})
            self.assertEqual([s for s, _ in res], ["OK", "FAIL"])
            self.assertIn("/usr/lib/libzstd.so.1", res[1][1])

    def test_the_system_libflac_or_none_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_bundle(tmp)
            lib = root / "tools" / "lib"
            z = {"native": True, "library": str(lib / "libzstd.so.1"), "note": ""}
            self.assertEqual(self.check(root, "/usr/lib/libFLAC.so.14", z)[0][0], "FAIL")
            self.assertEqual(self.check(root, None, z)[0][0], "FAIL")

    def test_no_zstd_library_at_all_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_bundle(tmp)
            res = self.check(root, str(root / "tools" / "lib" / "libFLAC.so.14"),
                             {"native": False, "library": None, "note": "libzstd not found"})
            self.assertEqual(res[1][0], "FAIL")
            self.assertIn("libzstd not found", res[1][1])

    def test_a_python_with_compression_zstd_needs_no_libzstd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_bundle(tmp)
            res = self.check(root, str(root / "tools" / "lib" / "libFLAC.so.14"),
                             {"native": True, "library": "compression.zstd", "note": ""})
            self.assertEqual([s for s, _ in res], ["OK", "OK"])


class SelfcheckMainInBundleTest(unittest.TestCase):
    def run_main(self, root: Path, lines):
        import contextlib
        import io
        with mock.patch.object(bundle, "bundle_root", return_value=root), \
                mock.patch.object(selfcheck, "check_bundled_libraries", return_value=lines), \
                mock.patch.object(selfcheck, "check_chdman", return_value=("SKIP", "-")), \
                mock.patch.object(selfcheck, "check_zstd", return_value=("OK", "z")), \
                mock.patch.object(selfcheck, "check_flac", return_value=(True, "f")), \
                mock.patch.object(selfcheck, "check_scheduler", return_value=(True, "ok")), \
                mock.patch.object(selfcheck, "check_writer", return_value=(True, "ok")), \
                mock.patch.object(selfcheck, "check_rvz", return_value=(True, "ok")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = selfcheck.main([])
        return code, out.getvalue()

    def test_inside_a_bundle_a_system_library_fails_the_self_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_bundle(tmp)
            code, out = self.run_main(root, [("OK", "libFLAC b"), ("FAIL", "libzstd was not loaded from the bundle")])
            self.assertEqual(code, 1)
            self.assertIn("SELF-CHECK FAILED", out)
            code, out = self.run_main(root, [("OK", "libFLAC b"), ("OK", "libzstd b")])
            self.assertEqual(code, 0)

    def test_a_bundle_without_libzstd_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_bundle(tmp, names=("libFLAC.so.14",))
            code, out = self.run_main(root, [])
            self.assertEqual(code, 1)
            self.assertIn("tools/lib/libzstd.so*", out)


if __name__ == "__main__":
    unittest.main()
