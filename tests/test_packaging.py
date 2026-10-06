"""Lightweight checks for the packaging files (AppImage / pyz).

Checks on the built artifacts are skipped unless packaging/build_appimage.sh has run.
"""

from __future__ import annotations

import configparser
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from types import ModuleType

sys.path.insert(0, os.path.dirname(__file__))
from chdtestlib import bash_env, find_bash  # noqa: E402

BASH = find_bash()
ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
APPDIR = ROOT / "build" / "appimage" / "AppDir"


def _load_make_icon() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_icon", PKG / "make_icon.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _version() -> str:
    text = (ROOT / "romorg" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.M)
    assert match
    return match.group(1)


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Split a PNG into (type, payload) chunks, verifying each CRC."""
    chunks = []
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise AssertionError(f"bad CRC in {kind!r} chunk")
        chunks.append((kind, payload))
        pos += 12 + length
    return chunks


class IconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_make_icon()

    def test_png_is_valid(self) -> None:
        size = 24  # small to keep the test fast; the build uses 256
        data = self.mod.make_png(size)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        chunks = _png_chunks(data)
        self.assertEqual([k for k, _ in chunks], [b"IHDR", b"IDAT", b"IEND"])
        width, height, depth, color = struct.unpack(">IIBB", chunks[0][1][:10])
        self.assertEqual((width, height, depth, color), (size, size, 8, 6))
        raw = zlib.decompress(chunks[1][1])
        self.assertEqual(len(raw), size * (size * 4 + 1))
        # Not blank: the centre pixel is opaque.
        row = raw[(size // 2) * (size * 4 + 1) + 1:][: size * 4]
        self.assertEqual(row[(size // 2) * 4 + 3], 255)

    def test_svg_is_xml(self) -> None:
        root = ET.fromstring(self.mod.make_svg())
        self.assertTrue(root.tag.endswith("svg"))
        self.assertEqual(root.get("width"), "256")


class SourceFileTests(unittest.TestCase):
    def test_apprun(self) -> None:
        apprun = PKG / "AppRun"
        text = apprun.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn('exec "$PY" -I', text)
        self.assertIn("-m romorg", text)
        self.assertIn("simple-rom-organiser\"\n", text)
        self.assertIn("/app.log", text)
        if os.name == "posix":
            self.assertTrue(os.access(apprun, os.X_OK), "packaging/AppRun must be executable")

    def test_desktop_entry(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # keep key case
        parser.read(PKG / "simple-rom-organiser.desktop", encoding="utf-8")
        entry = parser["Desktop Entry"]
        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Terminal"], "false")
        self.assertEqual(entry["StartupNotify"], "false")  # no window of its own: no launch spinner
        self.assertEqual(entry["Categories"], "Utility;Game;")
        self.assertEqual(entry["Icon"], "simple-rom-organiser")
        self.assertEqual(entry["Exec"], "@EXEC@")

    def test_metainfo_is_xml(self) -> None:
        root = ET.parse(PKG / "simple-rom-organiser.appdata.xml").getroot()
        self.assertEqual(root.findtext("id"), "simple-rom-organiser")

    @unittest.skipUnless(BASH, "bash not available")
    def test_shell_scripts_parse(self) -> None:
        for name in ("build_appimage.sh", "build_pyz.sh", "install.sh", "smoke_test.sh"):
            script = PKG / name
            with self.subTest(script=name):
                self.assertIn("set -euo pipefail", script.read_text(encoding="utf-8"))
                subprocess.run([BASH, "-n", script.as_posix()], check=True, env=bash_env(BASH))
        subprocess.run([BASH, "-n", (PKG / "AppRun").as_posix()], check=True, env=bash_env(BASH))

    def test_build_self_check_imports_new_modules(self) -> None:
        text = (PKG / "build_appimage.sh").read_text(encoding="utf-8")
        for module in ("romorg.library", "romorg.autoupdate", "romorg.tempspace"):
            self.assertIn(module, text)
            self.assertTrue((ROOT / "romorg" / (module.split(".")[1] + ".py")).is_file(), module)

    def test_smoke_test_never_downloads_dats(self) -> None:
        text = (PKG / "smoke_test.sh").read_text(encoding="utf-8")
        self.assertIn("ROMORG_OFFLINE=1", text)  # the startup update would fetch ~100 MB otherwise
        self.assertIn("/api/updates", text)
        self.assertIn("/api/library/profile", text)

    def test_third_party_document_matches_the_pinned_packages(self) -> None:
        doc = (ROOT / "docs" / "THIRD_PARTY.md").read_text(encoding="utf-8")
        script = (PKG / "build_appimage.sh").read_text(encoding="utf-8")
        pins = re.findall(r'^PKG_\w+="([^|]+)\|([^|]+)\|([0-9a-f]{64})"', script, re.M)
        self.assertEqual(len(pins), 2)                          # libFLAC + libogg; chdman is no longer shipped
        for file, directory, sha in pins:
            self.assertIn(sha, doc, file)                       # the document lists the same checksums
            self.assertIn(f"{directory}/{file}", doc)
        self.assertNotIn("mame-tools", script)
        self.assertNotIn("tools/chdman", script)
        for needle in ("chdman is not shipped", "libFLAC", "libogg", "BSD-3-Clause", "GPL-2.0",
                       "Corresponding source", "gitlab.archlinux.org", "github.com/mamedev/mame"):
            self.assertIn(needle, doc)
        # the build verifies and fails on a mismatch, and ships the document inside the image
        self.assertIn("sha256sum -c", script)
        self.assertIn("SHA-256 mismatch", script)
        self.assertIn('licenses/THIRD_PARTY.md', script)
        self.assertIn("--self-check", script)
        self.assertIn("the writer", (PKG / "smoke_test.sh").read_text(encoding="utf-8"))

    def test_gitignore(self) -> None:
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        for entry in ("dist/", "build/", "packaging/.cache/", "__pycache__/"):
            self.assertIn(entry, lines)


def _ps1_value(text: str, name: str) -> str:
    match = re.search(r'^\$' + name + r'\s*=\s*"([^"]+)"', text, re.M)
    assert match, name
    return match.group(1)


class WindowsPackagingTests(unittest.TestCase):
    """The Windows build scripts: Python 3.14, libFLAC from Xiph.Org (pinned), the self-check, no chdman."""

    def setUp(self) -> None:
        self.zip_ps1 = (PKG / "build_windows.ps1").read_text(encoding="utf-8")
        self.exe_ps1 = (PKG / "build_windows_exe.ps1").read_text(encoding="utf-8")
        self.flac_ps1 = (PKG / "fetch_flac.ps1").read_text(encoding="utf-8")

    def test_builds_use_python_3_14(self) -> None:
        self.assertRegex(self.zip_ps1, r'\[string\]\$PyVersion = "3\.14\.\d+"')   # has compression.zstd
        self.assertIn("python-$PyVersion-embed-amd64.zip", self.zip_ps1)
        self.assertIn('[string]$PySeries = "3.14"', self.exe_ps1)
        self.assertIn("venv-pyinstaller-$PySeries", self.exe_ps1)               # PyInstaller goes into a build venv
        self.assertIn("--python-version 3.14", (PKG / "fetch_sndfile.ps1").read_text(encoding="utf-8"))
        for text in (self.zip_ps1, self.exe_ps1):
            self.assertNotIn("3.13", text)

    def test_libflac_is_pinned_and_documented(self) -> None:
        notices = (PKG / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
        doc = (ROOT / "docs" / "THIRD_PARTY.md").read_text(encoding="utf-8")
        version = _ps1_value(self.flac_ps1, "FlacVersion")
        for name in ("FlacZipSha256", "DllSha256", "OggZipSha256"):
            sha = _ps1_value(self.flac_ps1, name)
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertIn(sha, doc, name)
        self.assertIn(_ps1_value(self.flac_ps1, "FlacZipSha256"), notices)
        self.assertIn("https://ftp.osuosl.org/pub/xiph/releases/flac/flac-$FlacVersion-win.zip", self.flac_ps1)
        self.assertIn(f"flac-{version}-win.zip", doc)
        self.assertIn("flac-$FlacVersion-win/Win64/libFLAC.dll", self.flac_ps1)
        for needle in ("libFLAC", "BSD", "FLAC-COPYING.Xiph.txt", "libogg-COPYING.txt"):
            self.assertIn(needle, notices)

    def test_both_builds_ship_libflac_and_run_the_self_check(self) -> None:
        self.assertIn('"$stage\\app\\native\\libFLAC.dll"', self.zip_ps1)
        self.assertIn('"--add-binary", "$flac;native"', self.exe_ps1)
        for text in (self.zip_ps1, self.exe_ps1):
            self.assertIn("fetch_flac.ps1", text)
            self.assertIn("FLAC-COPYING.Xiph.txt", text)
            self.assertIn("libogg-COPYING.txt", text)
            self.assertIn("--self-check", text)
            self.assertIn("--require-native", text)
            self.assertIn('Remove-Item "Env:$v"', text)    # ROMORG_LIBFLAC & co. never leak into the checks
        entry = (PKG / "windows_entry.py").read_text(encoding="utf-8")
        self.assertIn('"--self-check"', entry)
        self.assertIn('"--report"', entry)
        self.assertIn('"--chd-worker"', entry)

    def test_shipped_bytecode_does_not_record_the_builders_path(self) -> None:
        # compileall stores the source path it was given in every .pyc (tracebacks): -d replaces the build directory
        self.assertRegex(self.zip_ps1, r'compileall [^\n]*-d "app\\romorg"')
        appimage = (PKG / "build_appimage.sh").read_text(encoding="utf-8")
        self.assertRegex(appimage, r'compileall [^\n]*-d "/usr/lib/python\$PYVER"')

    def test_downloads_are_pinned_by_sha256(self) -> None:
        sndfile = (PKG / "fetch_sndfile.ps1").read_text(encoding="utf-8")
        self.assertIn('soundfile==$SoundfileVersion', sndfile)
        self.assertRegex(_ps1_value(sndfile, "WheelSha256").lower(), r"^[0-9a-f]{64}$")
        self.assertIn("Get-FileHash", sndfile)
        self.assertIn("Get-FileHash", self.zip_ps1)
        self.assertRegex(self.zip_ps1, r'"3\.14\.\d+" = "[0-9a-f]{64}"')

    def test_self_check_with_an_unwritable_report_exits_instead_of_raising(self) -> None:
        # in the windowed exe an uncaught exception is a blocking message box
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "no_such_folder", "report.txt")
            r = subprocess.run([sys.executable, str(PKG / "windows_entry.py"), "--self-check", "--report", bad],
                               cwd=ROOT, capture_output=True, text=True, timeout=120,
                               env={**os.environ, "ROMORG_DATA_DIR": tmp})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_no_chdman_in_the_windows_packages(self) -> None:
        for text in (self.zip_ps1, self.exe_ps1):
            self.assertNotIn("chdman.exe", text)
            self.assertNotIn("fetch_chdman", text)
        self.assertIn("chdman is not needed", self.zip_ps1)
        self.assertIn("chdman (NOT bundled)", (PKG / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8"))

    def test_libflac_is_found_in_a_frozen_bundle(self) -> None:
        import tempfile
        from unittest import mock

        from romorg import flacnative
        with tempfile.TemporaryDirectory() as tmp:
            dll = Path(tmp) / "native" / "libFLAC.dll"
            dll.parent.mkdir()
            dll.write_bytes(b"MZ")
            with mock.patch.object(sys, "platform", "win32"), \
                    mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "_MEIPASS", tmp, create=True), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(flacnative.ENV_LIB, None)
                self.assertIn(str(dll), flacnative._candidates())

    def test_require_native_makes_the_libraries_required(self) -> None:
        import contextlib
        import io
        from unittest import mock

        from romorg import bundle, selfcheck
        with mock.patch.object(bundle, "bundle_root", return_value=None), \
                mock.patch.object(selfcheck, "check_chdman", return_value=("SKIP", "-")), \
                mock.patch.object(selfcheck, "check_zstd", return_value=("WARN", "no zstd library")), \
                mock.patch.object(selfcheck, "check_flac", return_value=(False, "no libFLAC")), \
                mock.patch.object(selfcheck, "check_scheduler", return_value=(True, "ok")), \
                mock.patch.object(selfcheck, "check_writer", return_value=(True, "ok")) as writer, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(selfcheck.main([]), 0)                    # a dev tree: only warnings
            self.assertEqual(selfcheck.main(["--require-native"]), 1)  # a package: both are failures
        self.assertEqual(out.getvalue().count("FAIL "), 2)
        self.assertEqual(writer.call_args_list, [mock.call(False), mock.call(True)])

    def test_mingw_runtime_notices_ship_with_libflac(self) -> None:
        # libFLAC.dll (Xiph's MinGW build) links winpthreads and the MinGW-w64 runtime statically
        notices = (PKG / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
        doc = (ROOT / "docs" / "THIRD_PARTY.md").read_text(encoding="utf-8")
        for name in ("WinpthreadsSha256", "MingwRuntimeSha256"):
            sha = _ps1_value(self.flac_ps1, name)
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertIn(sha, doc, name)
        self.assertIn("mingw-w64-libraries/winpthreads/COPYING", self.flac_ps1)
        for text in (self.zip_ps1, self.exe_ps1):
            self.assertIn("winpthreads-COPYING.txt", text)
            self.assertIn("mingw-w64-runtime-COPYING.txt", text)
        for needle in ("winpthreads-COPYING.txt", "mingw-w64-runtime-COPYING.txt", "GCC Runtime Library Exception"):
            self.assertIn(needle, notices)
            self.assertIn(needle, doc)

    def test_exe_build_pins_pyinstaller_and_needs_the_python_licence(self) -> None:
        match = re.search(r'\[string\]\$PyInstallerVersion = "(\d+\.\d+\.\d+)"', self.exe_ps1)
        self.assertTrue(match, "PyInstaller is not pinned")
        pinned = match.group(1)
        self.assertIn('"pyinstaller==$PyInstallerVersion"', self.exe_ps1)
        self.assertIn(f"PyInstaller {pinned}", (PKG / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8"))
        self.assertNotIn("if (Test-Path $pyLicense) { Copy-Item", self.exe_ps1)   # missing licence: a hard error
        self.assertRegex(self.exe_ps1, r'if \(-not \(Test-Path \$pyLicense\)\) \{ throw')

    def test_check_flac_names_libsndfile(self) -> None:
        from unittest import mock

        from romorg import flacdec, flacnative, nativeflac, selfcheck
        sndfile = {"native": True, "library": "libsndfile", "note": "libFLAC not found"}
        ref = lambda data, n: flacdec.decode_frames(data, 0, n)[0]   # noqa: E731 - what libsndfile returns
        with mock.patch.object(flacnative, "status", return_value=sndfile), \
                mock.patch.object(nativeflac, "decode_frames", side_effect=ref):
            ok, text = selfcheck.check_flac()
        self.assertFalse(ok)                                          # a WARN outside packages, a FAIL inside
        self.assertIn("libsndfile", text)
        self.assertNotIn("pure-Python FLAC decoder is used", text)
        with mock.patch.object(flacnative, "status", return_value={"native": False, "library": None, "note": "-"}):
            self.assertIn("pure-Python", selfcheck.check_flac()[1])

    def test_windows_package_check(self) -> None:
        import tempfile
        from array import array
        from unittest import mock

        from romorg import flacenc, flacnative, nativeflac, selfcheck, zstdnative
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            (app / "native").mkdir()
            dll = app / "native" / "libFLAC.dll"
            dll.write_bytes(b"MZ")
            pcm = selfcheck._synthetic_pcm(588 * 4)
            decoded = (array("h", pcm if sys.byteorder == "little" else b""), 0)
            zstd = {"available": True, "native": True, "library": "compression.zstd", "note": ""}

            def run(lib, zstd_status=zstd, sndfile=True):
                with mock.patch.object(flacnative, "library_path", return_value=lib), \
                        mock.patch.object(flacenc, "encode", return_value=b"frames"), \
                        mock.patch.object(flacnative, "decode_frames", return_value=decoded), \
                        mock.patch.object(zstdnative, "status", return_value=zstd_status), \
                        mock.patch.object(nativeflac, "available", return_value=sndfile):
                    return dict((text.split(" ")[1] if text.startswith("package") else text, status)
                                for status, text in selfcheck.check_windows_package([app], True))

            good = selfcheck.check_windows_package
            self.assertTrue(callable(good))
            res = run(str(dll))
            self.assertEqual(sorted(res.values()), ["OK", "OK", "SKIP"])          # no libsndfile shipped: optional
            res = run(r"C:\elsewhere\libFLAC.dll")                                # a libFLAC from outside the package
            self.assertIn("FAIL", res.values())
            (app / "native" / "libsndfile-1.dll").write_bytes(b"MZ")
            res = run(str(dll), sndfile=False)                                     # shipped but does not load
            self.assertEqual(list(res.values()).count("FAIL"), 1)
            res = run(str(dll), zstd_status={"available": True, "native": False, "library": None, "note": "none"})
            self.assertEqual(list(res.values()).count("FAIL"), 1)                  # a package must bring Zstandard
            dll.unlink()
            with mock.patch.object(flacnative, "library_path", return_value=None), \
                    mock.patch.object(zstdnative, "status", return_value=zstd), \
                    mock.patch.object(nativeflac, "available", return_value=True):
                statuses = [s for s, _ in selfcheck.check_windows_package([app], False)]
                self.assertEqual(statuses[0], "WARN")                              # built with -NoFlac
                statuses = [s for s, _ in selfcheck.check_windows_package([app], True)]
                self.assertEqual(statuses[0], "FAIL")

    def test_package_check_replaces_the_appimage_skip(self) -> None:
        import contextlib
        import io
        from unittest import mock

        from romorg import bundle, selfcheck
        self.assertEqual(selfcheck.windows_package_dirs(), [])                     # a source tree is no package
        with mock.patch.object(bundle, "bundle_root", return_value=None), \
                mock.patch.object(selfcheck, "windows_package_dirs", return_value=[ROOT]), \
                mock.patch.object(selfcheck, "check_windows_package", return_value=[("FAIL", "no libFLAC")]) as pkg, \
                mock.patch.object(selfcheck, "check_chdman", return_value=("SKIP", "-")), \
                mock.patch.object(selfcheck, "check_zstd", return_value=("OK", "zstd")), \
                mock.patch.object(selfcheck, "check_flac", return_value=(True, "ok")), \
                mock.patch.object(selfcheck, "check_scheduler", return_value=(True, "ok")), \
                mock.patch.object(selfcheck, "check_writer", return_value=(True, "ok")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(selfcheck.main(["--require-native"]), 1)
        pkg.assert_called_once_with([ROOT], True)
        self.assertNotIn("not running from", out.getvalue())
        self.assertIn("FAIL  no libFLAC", out.getvalue())


class BuiltArtifactTests(unittest.TestCase):
    def test_windows_zip(self) -> None:
        path = ROOT / "dist" / f"Simple_ROM_Organiser-{_version()}-win64.zip"
        if not path.is_file():
            self.skipTest("Windows zip not built (run packaging/build_windows.ps1)")
        import hashlib
        import zipfile
        sha = _ps1_value((PKG / "fetch_flac.ps1").read_text(encoding="utf-8"), "DllSha256")
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            top = "Simple_ROM_Organiser/"
            for name in ("app/native/libFLAC.dll", "licenses/FLAC-COPYING.Xiph.txt", "licenses/libogg-COPYING.txt",
                         "licenses/winpthreads-COPYING.txt", "licenses/mingw-w64-runtime-COPYING.txt",
                         "licenses/python-LICENSE.txt", "THIRD_PARTY_NOTICES.txt", "python/python.exe", "python/python314.dll",
                         "python/_zstd.pyd", "app/romorg/chdwrite.py"):
                self.assertTrue(top + name in names, f"{name} is missing from {path.name}")
            self.assertEqual(hashlib.sha256(z.read(top + "app/native/libFLAC.dll")).hexdigest(), sha)
            self.assertFalse([n for n in names if n.lower().endswith("chdman.exe")])


    def test_appimage(self) -> None:
        path = ROOT / "dist" / f"Simple_ROM_Organiser-{_version()}-x86_64.AppImage"
        if not path.is_file():
            self.skipTest("AppImage not built (run packaging/build_appimage.sh)")
        with path.open("rb") as fh:
            head = fh.read(16)
        self.assertEqual(head[:4], b"\x7fELF")
        self.assertEqual(head[8:11], b"AI\x02", "type 2 AppImage magic")
        if os.name == "posix":
            self.assertTrue(os.access(path, os.X_OK))

    def test_appdir(self) -> None:
        if not APPDIR.is_dir():
            self.skipTest("AppDir not built (run packaging/build_appimage.sh)")
        apprun = APPDIR / "AppRun"
        self.assertTrue(apprun.is_file())
        if os.name == "posix":
            self.assertTrue(os.access(apprun, os.X_OK))
        self.assertTrue((APPDIR / "simple-rom-organiser.desktop").is_file())
        self.assertEqual((APPDIR / "simple-rom-organiser.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue((APPDIR / "usr" / "python" / "bin" / "python3").exists())
        stdlibs = list((APPDIR / "usr" / "python" / "lib").glob("python3.*"))
        self.assertEqual(len(stdlibs), 1)
        stdlib = stdlibs[0]
        self.assertTrue((stdlib / "site-packages" / "romorg" / "static" / "index.html").is_file())
        self.assertTrue((stdlib / "site-packages" / "romorg" / "__main__.py").is_file())
        for trimmed in ("tkinter", "idlelib", "test", "ensurepip", "site-packages/pip"):
            self.assertFalse((stdlib / trimmed).exists(), f"{trimmed} should be stripped")
        if (APPDIR / "tools").is_dir():                       # a build with BUNDLE_TOOLS=1 (the default)
            self.assertFalse((APPDIR / "tools" / "chdman").exists(), "chdman is no longer shipped")
            for lib in ("libFLAC.so.14", "libogg.so.0"):
                self.assertTrue((APPDIR / "tools" / "lib" / lib).exists(), lib)
            self.assertEqual((APPDIR / "licenses" / "THIRD_PARTY.md").read_text(encoding="utf-8"),
                             (ROOT / "docs" / "THIRD_PARTY.md").read_text(encoding="utf-8"))
            self.assertFalse((APPDIR / "licenses" / "mame-tools").exists())
            for pkg in ("flac", "libogg"):
                self.assertTrue(any((APPDIR / "licenses" / pkg).iterdir()), f"licence text of {pkg}")


if __name__ == "__main__":
    unittest.main()
