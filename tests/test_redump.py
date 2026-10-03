"""The Redump DAT source (``romorg.redump``): version from Content-Disposition, HTTP only, zip -> DAT."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import chdtestlib as T  # noqa: E402
from romorg import datfile, paths, redump  # noqa: E402
from romorg.tosec import Cancelled  # noqa: E402

NAME = "Sega - Dreamcast"


def make_zip(dat_text: str, member: str = "Sega - Dreamcast - Datfile (1) (2026-06-14 18-25-41).dat") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, dat_text)
    return buf.getvalue()


class FakeResp:
    def __init__(self, body: bytes = b"", status: int = 200, filename: str = "") -> None:
        self.body, self.status = io.BytesIO(body), status
        self.headers = {"Content-Length": str(len(body))}
        if filename:
            self.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        self.closed = False
        self.read_calls = 0

    def read(self, n: int = -1) -> bytes:
        self.read_calls += 1
        return self.body.read(n)

    def close(self) -> None:
        self.closed = True


class RedumpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "redump"
        self.dir.mkdir()
        d = T.Disc("Game (USA)", 1)
        self.dat_path = T.write_dat(Path(self.tmp.name) / "src.dat", [("Game (USA)", "Games", d.bins)])
        self.dat_text = self.dat_path.read_text()
        self.fname = "Sega - Dreamcast - Datfile (1) (2026-06-14 18-25-41).zip"
        self.requests: list = []

    def opener(self, version_file=None, body=None, status=200, head_status=200, fail=None):
        def open_fn(req):
            self.requests.append((req.get_method(), req.full_url, req.get_header("User-agent")))
            if fail:
                raise fail
            if req.get_method() == "HEAD":
                if head_status != 200:
                    raise urllib.error.HTTPError(req.full_url, head_status, "no", {}, None)
                return FakeResp(b"", status, version_file or self.fname)
            return FakeResp(body if body is not None else make_zip(self.dat_text), status, version_file or self.fname)
        return open_fn

    def test_paths_and_http_only(self) -> None:
        with mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": self.tmp.name}):
            self.assertEqual(paths.redump_dir(), Path(self.tmp.name) / "redump")
        self.assertTrue(redump.dat_url().startswith("http://redump.org/datfile/dc/"))
        self.assertFalse(redump.dat_url().startswith("https"))
        with self.assertRaises(redump.RedumpError):
            redump.dat_url("Unknown")

    def test_remote_version_from_content_disposition(self) -> None:
        v, f = redump.remote_version({"Content-Disposition": f'attachment; filename="{self.fname}"'})
        self.assertEqual((v, f), ("2026-06-14 18-25-41", self.fname))
        self.assertEqual(redump.remote_version({}), ("", ""))

    def test_check_missing_then_download_then_up_to_date(self) -> None:
        rows = redump.check_updates(names=[redump.DAT_NAME], opener=self.opener(), directory=self.dir)
        self.assertEqual((rows[0]["status"], rows[0]["latest"], rows[0]["installed"]),
                         ("missing", "2026-06-14 18-25-41", None))
        self.assertEqual(self.requests[0][0], "HEAD")
        self.assertEqual(self.requests[0][2], f"simple-rom-organiser/{redump.__version__}")
        res = redump.update_dats(names=[redump.DAT_NAME], opener=self.opener(), directory=self.dir)
        self.assertEqual((res["downloaded"], res["failed"], res["count"]), (1, 0, 1))
        self.assertTrue((self.dir / f"{NAME}.dat").is_file())
        self.assertFalse(list(self.dir.glob("*.part")))
        info = redump.find_dat(NAME, self.dir)
        self.assertEqual(info.version, "2026-06-14 18-25-41")
        self.assertEqual(redump.read_manifest(self.dir)[NAME]["filename"], self.fname)
        rows = redump.check_updates(names=[redump.DAT_NAME], opener=self.opener(), directory=self.dir)
        self.assertEqual(rows[0]["status"], "up_to_date")
        self.requests.clear()
        again = redump.download_dat(NAME, self.dir, opener=self.opener())
        self.assertEqual(again["status"], "unchanged")          # nothing downloaded
        self.assertEqual([r[0] for r in self.requests], ["GET"])

    def test_unchanged_download_does_not_read_the_body(self) -> None:
        redump.update_dats(names=[redump.DAT_NAME], opener=self.opener(), directory=self.dir)
        resp = FakeResp(make_zip(self.dat_text), 200, self.fname)
        redump.download_dat(NAME, self.dir, opener=lambda r: resp)
        self.assertTrue(resp.closed)
        self.assertEqual(resp.read_calls, 0)

    def test_newer_remote_is_an_update(self) -> None:
        redump.update_dats(names=[redump.DAT_NAME], opener=self.opener(), directory=self.dir)
        newer = "Sega - Dreamcast - Datfile (2) (2026-09-01 10-00-00).zip"
        rows = redump.check_updates(names=[redump.DAT_NAME], opener=self.opener(version_file=newer), directory=self.dir)
        self.assertEqual(rows[0]["status"], "update_available")
        older = "Sega - Dreamcast - Datfile (2) (2025-01-01 10-00-00).zip"
        rows = redump.check_updates(names=[redump.DAT_NAME], opener=self.opener(version_file=older), directory=self.dir)
        self.assertEqual(rows[0]["status"], "up_to_date")        # never "downgrade"

    def test_head_refused_falls_back_to_a_header_only_get(self) -> None:
        rows = redump.check_updates(names=[redump.DAT_NAME], opener=self.opener(head_status=405), directory=self.dir)
        self.assertEqual(rows[0]["status"], "missing")
        self.assertEqual([r[0] for r in self.requests], ["HEAD", "GET"])

    def test_atomic_replace_keeps_old_dat_on_a_broken_download(self) -> None:
        redump.update_dats(names=[redump.DAT_NAME], opener=self.opener(), directory=self.dir)
        before = (self.dir / f"{NAME}.dat").read_bytes()
        for body in (b"not a zip", make_zip("<html>nope</html>"), make_zip(self.dat_text.replace(NAME, "Other"))):
            with self.assertRaises(redump.RedumpError):
                redump.download_dat(NAME, self.dir, opener=self.opener(version_file="x (2027-01-01 00-00-00).zip",
                                                                       body=body), force=True)
            self.assertEqual((self.dir / f"{NAME}.dat").read_bytes(), before)
            self.assertFalse(list(self.dir.glob("*.part")))

    def test_only_a_single_dat_member_is_accepted(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.dat", self.dat_text)
            zf.writestr("b.dat", self.dat_text)
        with self.assertRaises(redump.RedumpError):
            redump.download_dat(NAME, self.dir, opener=self.opener(body=buf.getvalue()))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil.dat", self.dat_text)       # the name is never used as a path
        redump.download_dat(NAME, self.dir, opener=self.opener(body=buf.getvalue()))
        self.assertTrue((self.dir / f"{NAME}.dat").is_file())
        self.assertFalse((self.dir.parent.parent / "evil.dat").exists())

    def test_offline_with_cache_and_without(self) -> None:
        err = urllib.error.URLError("no route")
        rows = redump.check_updates(names=[redump.DAT_NAME], opener=self.opener(fail=err), directory=self.dir)
        self.assertEqual(rows[0]["status"], "error")
        with self.assertRaises(redump.RedumpError):
            redump.update_dats(names=[redump.DAT_NAME], opener=self.opener(fail=err), directory=self.dir)
        redump.update_dats(names=[redump.DAT_NAME], opener=self.opener(), directory=self.dir)
        res = redump.update_dats(names=[redump.DAT_NAME], opener=self.opener(fail=err), directory=self.dir, force=True)   # cached DAT stays
        self.assertEqual((res["failed"], res["count"]), (1, 1))
        self.assertTrue(redump.find_dat(NAME, self.dir))

    def test_cancel_removes_the_part_files(self) -> None:
        class Ev:
            def is_set(self_inner):  # noqa: N805
                return True
        with self.assertRaises(Cancelled):
            redump.download_dat(NAME, self.dir, opener=self.opener(), cancel=Ev())
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_parse_redump_sets_are_games(self) -> None:
        dat = datfile.parse_redump(self.dat_path)
        self.assertEqual({r.set_name for r in dat.roms}, {"Game (USA)"})
        self.assertEqual({r.category for r in dat.roms}, {"Games"})
        self.assertEqual(len(dat.sets()), 1)


if __name__ == "__main__":
    unittest.main()
