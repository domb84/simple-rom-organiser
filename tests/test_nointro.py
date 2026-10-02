"""Tests for romorg.nointro (libretro No-Intro DAT download; network mocked)."""

from __future__ import annotations

import email.message
import io
import os
import tempfile
import threading
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from romorg import nointro, paths, tosec

GBA = "Nintendo - Game Boy Advance"
NES = "Nintendo - Nintendo Entertainment System"


def dat_text(name: str, version: str = "2026.08.01", games: int = 2) -> bytes:
    out = [f'clrmamepro (\n\tname "{name}"\n\tdescription "{name}"\n\tversion "{version}"\n)\n']
    for i in range(games):
        out.append(f'game (\n\tname "G{i} (USA)"\n\trom ( name "G{i} (USA).gba" size 4 crc {i:08X} '
                   f'sha1 {i:040X} )\n)\n')
    return "".join(out).encode()


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200, etag: str = '"e1"', length: bool = True) -> None:
        super().__init__(data)
        self.status = status
        self.headers = email.message.Message()
        if etag:
            self.headers["ETag"] = etag
        if length:
            self.headers["Content-Length"] = str(len(data))


class FakeServer:
    """opener(request) -> FakeResponse; honours If-None-Match; records requests."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.etags = {k: f'"{k}-1"' for k in files}
        self.requests: list[urllib.request.Request] = []
        self.fail: set[str] = set()

    def __call__(self, req):
        self.requests.append(req)
        url = req.full_url
        name = next((n for n in self.files if url == nointro.dat_url(n)), None)
        if name in self.fail or (name is None and "*" in self.fail):
            raise urllib.error.URLError("network unreachable")
        if name is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None)
        if req.get_header("If-none-match") == self.etags[name]:
            raise urllib.error.HTTPError(url, 304, "Not Modified", email.message.Message(), None)
        return FakeResponse(self.files[name], etag=self.etags[name])


class NoIntroTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = FakeServer({GBA: dat_text(GBA), NES: dat_text(NES, games=3)})

    def test_url_and_storage(self) -> None:
        self.assertEqual(
            nointro.dat_url(GBA),
            "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/no-intro/"
            "Nintendo%20-%20Game%20Boy%20Advance.dat")
        self.assertEqual(paths.nointro_dir(), Path(self.tmp.name) / "nointro")
        self.assertNotEqual(paths.nointro_dir(), paths.dats_dir())
        self.assertIn("simple-rom-organiser/", nointro.USER_AGENT)

    def test_download_then_unchanged_then_force(self) -> None:
        res = nointro.download_dat(GBA, opener=self.server)
        self.assertEqual(res, {"name": GBA, "version": "2026.08.01", "status": "downloaded"})
        target = paths.nointro_dir() / f"{GBA}.dat"
        self.assertEqual(target.read_bytes(), dat_text(GBA))
        self.assertFalse(target.with_name(target.name + ".part").exists())
        req = self.server.requests[-1]
        self.assertIn("simple-rom-organiser", req.get_header("User-agent"))
        self.assertIsNone(req.get_header("If-none-match"))
        man = nointro.read_manifest()
        self.assertEqual(man[GBA]["etag"], f'"{GBA}-1"')
        self.assertEqual(man[GBA]["version"], "2026.08.01")
        self.assertEqual(man[GBA]["size"], len(dat_text(GBA)))
        self.assertEqual(len(man[GBA]["sha1"]), 40)
        self.assertEqual(man[GBA]["url"], nointro.dat_url(GBA))

        res = nointro.download_dat(GBA, opener=self.server)
        self.assertEqual(res["status"], "unchanged")
        self.assertEqual(self.server.requests[-1].get_header("If-none-match"), f'"{GBA}-1"')

        self.server.files[GBA] = dat_text(GBA, version="2026.09.01")
        self.assertEqual(nointro.download_dat(GBA, opener=self.server)["status"], "unchanged")  # same etag
        res = nointro.download_dat(GBA, opener=self.server, force=True)
        self.assertEqual(res, {"name": GBA, "version": "2026.09.01", "status": "downloaded"})
        self.assertIsNone(self.server.requests[-1].get_header("If-none-match"))
        self.assertEqual(nointro.find_dat(GBA).version, "2026.09.01")

    def test_304_status_response(self) -> None:
        nointro.download_dat(GBA, opener=self.server)
        res = nointro.download_dat(GBA, opener=lambda r: FakeResponse(b"", status=304))
        self.assertEqual(res["status"], "unchanged")

    def test_validation_keeps_old_file(self) -> None:
        nointro.download_dat(GBA, opener=self.server)
        target = paths.nointro_dir() / f"{GBA}.dat"
        good = target.read_bytes()
        bad_bodies = {
            "html": b"<html>rate limited</html>",
            "garbage": b"hello",
            "no roms": b'clrmamepro ( name "Nintendo - Game Boy Advance" )\n',
            "wrong name": dat_text("Nintendo - Something Else"),
            "truncated": dat_text(GBA)[:-3],
        }
        for label, body in bad_bodies.items():
            with self.subTest(label):
                with self.assertRaises(nointro.NoIntroError):
                    nointro.download_dat(GBA, force=True, opener=lambda r, b=body: FakeResponse(b))
                self.assertEqual(target.read_bytes(), good)
                self.assertEqual(list(paths.nointro_dir().glob("*.part")), [])
        # Content-Length says more than was sent
        resp = FakeResponse(dat_text(GBA))
        resp.headers.replace_header("Content-Length", "999999")
        with self.assertRaisesRegex(nointro.NoIntroError, "incomplete"):
            nointro.download_dat(GBA, force=True, opener=lambda r: resp)
        with self.assertRaisesRegex(nointro.NoIntroError, "HTTP 500"):
            nointro.download_dat(GBA, force=True, opener=lambda r: FakeResponse(b"x", status=500))
        self.assertEqual(target.read_bytes(), good)

    def test_cancel_removes_part(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(tosec.Cancelled):
            nointro.download_dat(GBA, cancel=cancel, opener=self.server)
        self.assertEqual(list(paths.nointro_dir().iterdir()), [])
        with self.assertRaises(tosec.Cancelled):
            nointro.update_dats(cancel=cancel, opener=self.server)

    def test_update_all_and_progress(self) -> None:
        seen: list[str] = []
        res = nointro.update_dats([GBA, NES], progress=lambda d, t, m: seen.append(m),
                                  opener=self.server)
        self.assertEqual(res["source"], "nointro")
        self.assertEqual((res["downloaded"], res["unchanged"], res["failed"], res["count"]), (2, 0, 0, 2))
        self.assertEqual([d["status"] for d in res["dats"]], ["downloaded", "downloaded"])
        self.assertTrue(any(m.startswith("[2/2]") for m in seen))
        self.assertEqual([d.name for d in nointro.list_dats()], [GBA, NES])
        res = nointro.update_dats([GBA, NES], opener=self.server)
        self.assertEqual((res["downloaded"], res["unchanged"]), (0, 2))

    def test_update_default_names_and_partial_failure(self) -> None:
        self.server.fail = {"*"}  # SNES/N64 not served -> network error for those
        res = nointro.update_dats(opener=self.server)
        self.assertEqual([d["name"] for d in res["dats"]], list(nointro.NOINTRO_DATS))
        by = {d["name"]: d for d in res["dats"]}
        self.assertEqual(by[GBA]["status"], "downloaded")
        self.assertEqual(by["Nintendo - Nintendo 64"]["status"], "error")
        self.assertIn("could not download", by["Nintendo - Nintendo 64"]["error"])
        self.assertEqual((res["failed"], res["count"]), (2, 2))

    def test_offline(self) -> None:
        self.server.fail = {GBA, NES}
        with self.assertRaisesRegex(nointro.NoIntroError, "offline"):
            nointro.update_dats([GBA, NES], opener=self.server)
        # with a local copy, being offline is not fatal
        self.server.fail = set()
        nointro.download_dat(GBA, opener=self.server)
        self.server.fail = {GBA, NES}
        res = nointro.update_dats([GBA, NES], opener=self.server)
        self.assertEqual((res["failed"], res["count"]), (2, 1))
        self.assertEqual(res["dats"][0]["version"], "2026.08.01")

    def test_incomplete_read_is_wrapped_and_part_removed(self) -> None:
        import http.client

        class Broken(FakeResponse):
            def read(self, n=-1):
                raise http.client.IncompleteRead(b"", 5)

        server = self.server
        with self.assertRaises(nointro.NoIntroError) as cm:
            nointro.download_dat(GBA, opener=lambda r: Broken(server.files[GBA]))
        self.assertIsInstance(cm.exception.__cause__, http.client.IncompleteRead)
        self.assertEqual(list(paths.nointro_dir().iterdir()), [])
        # one DAT blowing up unexpectedly must not skip the others
        calls = []
        real = nointro.download_dat

        def flaky(name, *a, **kw):
            calls.append(name)
            if name == GBA:
                raise RuntimeError("boom")
            return real(name, *a, **kw)
        with mock.patch.object(nointro, "download_dat", flaky):
            res = nointro.update_dats([GBA, NES], opener=self.server)
        self.assertEqual(calls, [GBA, NES])
        self.assertEqual([d["status"] for d in res["dats"]], ["error", "downloaded"])

    def test_disk_full_cause_survives_when_everything_fails(self) -> None:
        import errno

        def opener(req):
            raise OSError(errno.ENOSPC, "No space left on device")
        with self.assertRaises(nointro.NoIntroError) as cm:
            nointro.update_dats([GBA], opener=opener)
        self.assertEqual(cm.exception.__cause__.errno, errno.ENOSPC)

    def test_list_dats_without_manifest(self) -> None:
        folder = paths.nointro_dir()
        (folder / f"{GBA}.dat").write_bytes(dat_text(GBA, version="2025.01.01"))
        (folder / f"{GBA}.dat.part").write_bytes(b"partial")
        (folder / "notes.txt").write_text("x")
        infos = nointro.list_dats()
        self.assertEqual([(d.name, d.version) for d in infos], [(GBA, "2025.01.01")])
        self.assertIsNone(nointro.find_dat(NES))
        self.assertEqual(nointro.header_version(folder / "notes.txt"), "")
        self.assertEqual(nointro.header_version(folder / "missing.dat"), "")
        self.assertEqual(nointro.list_dats(folder / "nope"), [])
        (folder / nointro.MANIFEST).write_text("{broken")
        self.assertEqual(nointro.read_manifest(), {})

    def test_survives_tosec_extraction(self) -> None:
        nointro.download_dat(GBA, opener=self.server)
        pack = Path(self.tmp.name) / "pack.zip"
        with zipfile.ZipFile(pack, "w") as zf:
            zf.writestr("TOSEC/Commodore Amiga - Firmware (TOSEC-v2025-01-03_CM).dat",
                        '<?xml version="1.0"?><datafile><header><name>Commodore Amiga - Firmware'
                        '</name></header></datafile>')
        tosec.extract_dats(pack, paths.dats_dir())
        self.assertEqual(len(list(paths.dats_dir().glob("*.dat"))), 1)
        self.assertTrue((paths.nointro_dir() / f"{GBA}.dat").is_file())
        self.assertEqual([d.name for d in nointro.list_dats()], [GBA])
        self.assertEqual([d.name for d in tosec.list_dats()], ["Commodore Amiga - Firmware"])

class CheckUpdatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = FakeServer({GBA: dat_text(GBA), NES: dat_text(NES)})

    def head(self, req):
        self.assertEqual(req.get_method(), "HEAD")
        name = next(n for n in self.server.files if req.full_url == nointro.dat_url(n))
        if name in self.server.fail:
            raise urllib.error.URLError("network unreachable")
        return FakeResponse(b"", etag=self.server.etags[name])

    def by_name(self, rows):
        return {r["name"]: r for r in rows}

    def test_statuses(self) -> None:
        rows = self.by_name(nointro.check_updates([GBA, NES], opener=self.head))
        self.assertEqual(rows[GBA]["status"], "missing")
        self.assertIsNone(rows[GBA]["installed"])
        nointro.download_dat(GBA, opener=self.server)
        rows = self.by_name(nointro.check_updates([GBA, NES], opener=self.head))
        self.assertEqual(rows[GBA]["status"], "up_to_date")
        self.assertEqual(rows[GBA]["installed"], "2026.08.01")
        self.server.etags[GBA] = '"new"'
        self.assertEqual(self.by_name(nointro.check_updates([GBA], opener=self.head))[GBA]["status"],
                         "update_available")
        # weak etag prefix is ignored
        self.server.etags[GBA] = f'W/"{GBA}-1"'
        self.assertEqual(self.by_name(nointro.check_updates([GBA], opener=self.head))[GBA]["status"],
                         "up_to_date")

    def test_unknown_without_stored_etag_and_errors(self) -> None:
        folder = paths.nointro_dir()
        (folder / f"{GBA}.dat").write_bytes(dat_text(GBA))
        rows = self.by_name(nointro.check_updates([GBA], opener=self.head))
        self.assertEqual(rows[GBA]["status"], "unknown")
        self.server.fail.add(GBA)
        rows = self.by_name(nointro.check_updates([GBA], opener=self.head))
        self.assertEqual(rows[GBA]["status"], "error")
        self.assertIn("network unreachable", rows[GBA]["error"])
        self.assertEqual(rows[GBA]["installed"], "2026.08.01")

    def test_http_error_row(self) -> None:
        def opener(req):
            raise urllib.error.HTTPError(req.full_url, 404, "x", email.message.Message(), None)
        rows = nointro.check_updates([GBA], opener=opener)
        self.assertEqual((rows[0]["status"], rows[0]["error"]), ("error", "HTTP 404"))


if __name__ == "__main__":
    unittest.main()
