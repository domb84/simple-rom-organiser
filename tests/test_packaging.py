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
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from types import ModuleType

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

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_shell_scripts_parse(self) -> None:
        for name in ("build_appimage.sh", "build_pyz.sh", "install.sh", "smoke_test.sh"):
            script = PKG / name
            with self.subTest(script=name):
                self.assertIn("set -euo pipefail", script.read_text(encoding="utf-8"))
                subprocess.run(["bash", "-n", str(script)], check=True)
        subprocess.run(["sh", "-n", str(PKG / "AppRun")], check=True)

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
        self.assertEqual(len(pins), 4)
        for file, directory, sha in pins:
            self.assertIn(sha, doc, file)                       # the document lists the same checksums
            self.assertIn(f"{directory}/{file}", doc)
        for needle in ("chdman", "libFLAC", "libogg", "utf8proc", "BSD-3-Clause", "GPL-2.0", "MIT",
                       "Corresponding source", "gitlab.archlinux.org", "github.com/mamedev/mame"):
            self.assertIn(needle, doc)
        # the build verifies and fails on a mismatch, and ships the document inside the image
        self.assertIn("sha256sum -c", script)
        self.assertIn("SHA-256 mismatch", script)
        self.assertIn('licenses/THIRD_PARTY.md', script)
        self.assertIn("--self-check", script)
        self.assertIn("ALLOW_NO_CHDMAN", (PKG / "smoke_test.sh").read_text(encoding="utf-8"))

    def test_gitignore(self) -> None:
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        for entry in ("dist/", "build/", "packaging/.cache/", "__pycache__/"):
            self.assertIn(entry, lines)


class BuiltArtifactTests(unittest.TestCase):
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
            self.assertTrue(os.access(APPDIR / "tools" / "chdman", os.X_OK))
            for lib in ("libFLAC.so.14", "libogg.so.0", "libutf8proc.so.3"):
                self.assertTrue((APPDIR / "tools" / "lib" / lib).exists(), lib)
            self.assertEqual((APPDIR / "licenses" / "THIRD_PARTY.md").read_text(encoding="utf-8"),
                             (ROOT / "docs" / "THIRD_PARTY.md").read_text(encoding="utf-8"))
            for pkg in ("mame-tools", "libutf8proc", "flac", "libogg"):
                self.assertTrue(any((APPDIR / "licenses" / pkg).iterdir()), f"licence text of {pkg}")


if __name__ == "__main__":
    unittest.main()
