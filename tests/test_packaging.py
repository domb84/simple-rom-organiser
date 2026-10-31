"""Lightweight checks for the packaging files (AppImage / pyz).

Checks on the built artifacts are skipped unless packaging/build_appimage.sh has run.
"""

from __future__ import annotations

import configparser
import importlib.util
import os
import re
import struct
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from types import ModuleType
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

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
        self.assertEqual(len(pins), 3)                          # libFLAC + libogg + libzstd; chdman is no longer shipped
        for file, directory, sha in pins:
            self.assertIn(sha, doc, file)                       # the document lists the same checksums
            self.assertIn(f"{directory}/{file}", doc)
        self.assertNotIn("mame-tools", script)
        self.assertNotIn("tools/chdman", script)
        for needle in ("chdman is not shipped", "libFLAC", "libogg", "libzstd", "BSD-3-Clause", "GPL-2.0",
                       "Corresponding source", "gitlab.archlinux.org", "github.com/mamedev/mame"):
            self.assertIn(needle, doc)
        # the build verifies and fails on a mismatch, and ships the document inside the image
        self.assertIn("sha256sum -c", script)
        self.assertIn("SHA-256 mismatch", script)
        self.assertIn('licenses/THIRD_PARTY.md', script)
        self.assertIn("--self-check", script)
        self.assertIn("the writer", (PKG / "smoke_test.sh").read_text(encoding="utf-8"))

    def test_shipped_bytecode_does_not_record_the_builders_path(self) -> None:
        # compileall stores the source path it was given in every .pyc (tracebacks): -d replaces the build directory
        appimage = (PKG / "build_appimage.sh").read_text(encoding="utf-8")
        self.assertRegex(appimage, r'compileall [^\n]*-d "/usr/lib/python\$PYVER"')

    def test_gitignore(self) -> None:
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        for entry in ("dist/", "build/", "packaging/.cache/", "__pycache__/"):
            self.assertIn(entry, lines)


class SelfCheckTests(unittest.TestCase):
    """What the self-check walks and when it fails."""

    def test_self_check_knows_every_module_and_every_file_of_the_ui(self) -> None:
        """The lists the self-check walks are the source tree's: a module or UI file added later cannot be forgotten."""
        from romorg import selfcheck, server
        modules = sorted(p.stem for p in (ROOT / "romorg").glob("*.py") if p.stem not in ("__init__", "__main__"))
        self.assertEqual(sorted(selfcheck.APP_MODULES), modules)
        static = sorted(p.name for p in (ROOT / "romorg" / "static").iterdir() if p.is_file())
        self.assertEqual(sorted(selfcheck.STATIC_FILES), static)
        self.assertEqual(sorted(server.STATIC_TYPES), static)                  # and the server serves each of them
        page = (ROOT / "romorg" / "static" / "next.html").read_text(encoding="utf-8")
        for part in selfcheck.UI_PARTS:
            self.assertIn(f'id="{part}"', page)
        for name in ("libexport", "meter", "retroarch", "sortroot"):   # the modules v0.2 added
            self.assertIn(name, selfcheck.APP_MODULES)
        ok, text = selfcheck.check_app()
        self.assertTrue(ok, text)
        self.assertIn(f"{len(modules)} modules import", text)

    def test_self_check_fails_on_a_missing_module_or_ui_file(self) -> None:
        import contextlib
        import importlib
        import io
        from unittest import mock

        from romorg import bundle, selfcheck, server
        real_import, real_read = importlib.import_module, server.read_static

        def no_retroarch(name: str, *args: object) -> object:
            if name == "romorg.retroarch":
                raise ModuleNotFoundError("No module named 'romorg.retroarch'")
            return real_import(name, *args)

        with mock.patch.object(importlib, "import_module", no_retroarch):
            ok, text = selfcheck.check_app()
        self.assertFalse(ok)
        self.assertIn("module retroarch", text)

        def no_script(name: str) -> bytes:
            if name == "next.js":
                raise FileNotFoundError(name)
            return real_read(name)

        with mock.patch.object(server, "read_static", no_script):
            ok, text = selfcheck.check_app()
        self.assertFalse(ok)
        self.assertIn("static/next.js", text)
        old_page = real_read("next.html").replace(b'id="strip"', b'id="old"')
        with mock.patch.object(server, "read_static", lambda name: old_page if name == "next.html" else real_read(name)):
            ok, text = selfcheck.check_app()
        self.assertFalse(ok)
        self.assertIn("the page part strip", text)
        wrong_ref = real_read("next.html").replace(b"/static/next.css", b"/static/gone.css")
        with mock.patch.object(server, "read_static", lambda name: wrong_ref if name == "next.html" else real_read(name)):
            ok, text = selfcheck.check_app()
        self.assertFalse(ok)
        self.assertIn("static/gone.css (next.html asks for it", text)
        # an incomplete app fails the whole self-check, in a source tree too
        with mock.patch.object(bundle, "bundle_root", return_value=None), \
                mock.patch.object(selfcheck, "check_chdman", return_value=("SKIP", "-")), \
                mock.patch.object(selfcheck, "check_zstd", return_value=("OK", "z")), \
                mock.patch.object(selfcheck, "check_flac", return_value=(True, "f")), \
                mock.patch.object(selfcheck, "check_scheduler", return_value=(True, "ok")), \
                mock.patch.object(selfcheck, "check_writer", return_value=(True, "ok")), \
                mock.patch.object(selfcheck, "check_rvz", return_value=(True, "ok")), \
                mock.patch.object(selfcheck, "check_app", return_value=(False, "the app is incomplete: x")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(selfcheck.main([]), 1)
        self.assertIn("FAIL  the app is incomplete", out.getvalue())

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


class BuiltArtifactTests(unittest.TestCase):
    def test_appimage(self) -> None:
        path = ROOT / "dist" / f"Simple_ROM_Organiser-{_version()}-x86_64.AppImage"
        if not path.is_file():
            self.skipTest("AppImage not built (run packaging/build_appimage.sh)")
        with path.open("rb") as fh:
            head = fh.read(16)
        self.assertEqual(head[:4], b"\x7fELF")
        self.assertEqual(head[8:11], b"AI\x02", "type 2 AppImage magic")
        self.assertTrue(os.access(path, os.X_OK))

    def test_appdir(self) -> None:
        if not APPDIR.is_dir():
            self.skipTest("AppDir not built (run packaging/build_appimage.sh)")
        apprun = APPDIR / "AppRun"
        self.assertTrue(apprun.is_file())
        self.assertTrue(os.access(apprun, os.X_OK))
        self.assertTrue((APPDIR / "simple-rom-organiser.desktop").is_file())
        self.assertEqual((APPDIR / "simple-rom-organiser.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue((APPDIR / "usr" / "python" / "bin" / "python3").exists())
        stdlibs = list((APPDIR / "usr" / "python" / "lib").glob("python3.*"))
        self.assertEqual(len(stdlibs), 1)
        stdlib = stdlibs[0]
        self.assertTrue((stdlib / "site-packages" / "romorg" / "static" / "next.html").is_file())
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


class DevToolsTest(unittest.TestCase):
    def test_fetch_real_dats_help_does_not_touch_the_disk_or_the_network(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, str(ROOT / "tools" / "fetch_real_dats.py"), "--help"],
                               cwd=d, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0)          # --help is not an error (any other option is: exit 2)
            self.assertIn("Usage", r.stdout)
            self.assertEqual(os.listdir(d), [])




class DeveloperToolArgumentTests(unittest.TestCase):
    """Bad arguments to the developer tools and the self-check are refused, not acted on."""

    def test_selfcheck_rejects_unknown_arguments(self) -> None:
        import contextlib
        import io

        from romorg import selfcheck
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(selfcheck.main(["--require-nativ"]), 2)
        self.assertIn("--require-nativ", err.getvalue())

    def test_fetch_real_dats_does_not_treat_options_as_a_folder(self) -> None:
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as cwd:
            for arg, rc in (("--help", 0), ("--bogus", 2)):
                r = subprocess.run([sys.executable, str(ROOT / "tools" / "fetch_real_dats.py"), arg], cwd=cwd,
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, rc, r.stderr)
                self.assertEqual(os.listdir(cwd), [])          # nothing created, nothing downloaded

    def test_bench_chdwrite_bad_preset_is_a_usage_error(self) -> None:
        import subprocess
        import sys
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "bench_chdwrite.py"), "--preset", "bogus", "x.cue"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
