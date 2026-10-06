import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from email.message import Message
from pathlib import Path
from unittest import mock

from romorg import tosec

FIXTURES = Path(__file__).parent / "fixtures"
# ROMORG_REAL_SCRATCH: folder holding pack.zip, the TOSEC DAT pack (default: the Steam Deck scratch folder)
REAL_PACK = Path(os.environ.get("ROMORG_REAL_SCRATCH") or "/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/"
                 "cfc5ea37-4261-428c-9422-29acd65cac97/scratchpad") / "pack.zip"
CAT_URL = "https://www.tosecdev.org/downloads/category/59-2025-03-13"
PACK_URL = CAT_URL + "?download=117:tosec-dat-pack-complete-4743-tosec-v2025-03-13"
PACK_NAME = "TOSEC - DAT Pack - Complete (4743) (TOSEC-v2025-03-13).zip"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse(io.BytesIO):
    """Minimal stand-in for an http.client.HTTPResponse."""

    def __init__(self, data: bytes, filename: str | None = PACK_NAME,
                 length: int | None = None, status: int = 200) -> None:
        super().__init__(data)
        self.status = status
        self.headers = Message()
        if filename:
            self.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        self.headers["Content-Length"] = str(len(data) if length is None else length)
        self.requests: list = []


def fake_fetch(pages: dict[str, str]):
    def fetch(url: str) -> str:
        return pages[url]
    return fetch


def build_pack(path: Path, extra: dict[str, bytes] | None = None) -> None:
    files = {
        "TOSEC/Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat": b"<datafile/>",
        "TOSEC/Commodore Amiga - Demos - [ADF] (TOSEC-v2024-01-01_CM).dat": b"<datafile/>",
        "TOSEC-ISO/Sony PlayStation - Games (TOSEC-v2023-02-02_CM).dat": b"<datafile/>",
        "TOSEC-PIX/Amiga - Magazines (TOSEC-v2022-03-03_CM).dat": b"<datafile/>",
        "TOSEC/sub/Nested Thing (TOSEC-v2020-01-01_CM).dat": b"<datafile/>",
        "CUEs/foo/bar.cue": b"cue",
        "Scripts/x.dat": b"no",
        "readme.txt": b"hi",
        "TOSEC/notes.txt": b"no",
        "../evil.dat": b"evil",
        "TOSEC/../../evil2.dat": b"evil",
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


class HtmlParsingTest(unittest.TestCase):
    def test_categories_from_real_page(self) -> None:
        cats = tosec.parse_release_categories(fixture("tosec_downloads.html"))
        self.assertEqual(cats[0], ("2025-03-13", CAT_URL))
        dates = [d for d, _ in cats]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertTrue(all("toseciso-wip" not in u and "datfiles" not in u for _, u in cats))

    def test_categories_ignore_non_dates(self) -> None:
        html = ('<a href="/downloads/category/22-datfiles">x</a>'
                '<a href="/downloads/category/4-2009-07-12-toseciso-wip">x</a>'
                '<a href="/downloads/category/9-2030-13-45">bad date</a>'
                '<a href="https://www.tosecdev.org/downloads/category/60-2026-01-02">new</a>'
                '<a href="/downloads/category/59-2025-03-13?download=1:x">dl</a>')
        cats = tosec.parse_release_categories(html)
        self.assertEqual(cats, [("2026-01-02", "https://www.tosecdev.org/downloads/category/60-2026-01-02")])

    def test_pack_link_from_real_page(self) -> None:
        self.assertEqual(tosec.parse_pack_link(fixture("tosec_category.html"), CAT_URL), PACK_URL)
        self.assertIsNone(tosec.parse_pack_link("<a href='/x?download=1:other'>x</a>", CAT_URL))

    def test_find_latest_release(self) -> None:
        fetch = fake_fetch({tosec.DOWNLOADS_URL: fixture("tosec_downloads.html"),
                            CAT_URL: fixture("tosec_category.html")})
        info = tosec.find_latest_release(fetch)
        self.assertEqual(info, tosec.ReleaseInfo("2025-03-13", CAT_URL, PACK_URL))

    def test_find_latest_release_errors(self) -> None:
        with self.assertRaises(tosec.TosecError):
            tosec.find_latest_release(fake_fetch({tosec.DOWNLOADS_URL: "<html></html>"}))

    def test_content_disposition(self) -> None:
        f = tosec._filename_from_disposition
        self.assertEqual(f(f'attachment; filename="{PACK_NAME}"'), PACK_NAME)
        self.assertEqual(f("attachment; filename=pack.zip"), "pack.zip")
        self.assertEqual(f("attachment; filename*=UTF-8''a%20b.zip"), "a b.zip")
        self.assertIsNone(f(None))


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name)
        self.info = tosec.ReleaseInfo("2025-03-13", CAT_URL, PACK_URL)
        self.data = b"PK\x03\x04" + os.urandom(600_000)

    def test_download(self) -> None:
        seen: list = []
        calls: list[tuple[int, int]] = []

        def opener(req):
            seen.append(req)
            return FakeResponse(self.data)

        path = tosec.download_pack(self.info, self.dest, progress=lambda d, t: calls.append((d, t)),
                                   opener=opener)
        self.assertEqual(path, self.dest / PACK_NAME)
        self.assertEqual(path.read_bytes(), self.data)
        self.assertIn("simple-rom-organiser", seen[0].get_header("User-agent"))
        self.assertEqual(calls[-1], (len(self.data), len(self.data)))
        self.assertFalse(list(self.dest.glob("*.part")))

        # Second call: same size present -> reused without reading the body.
        resp = FakeResponse(self.data)
        positions: list[int] = []
        resp.close = lambda: positions.append(resp.tell())  # type: ignore[method-assign]
        path2 = tosec.download_pack(self.info, self.dest, opener=lambda r: resp)
        self.assertEqual(path2, path)
        self.assertEqual(positions, [0])

    def test_cancel_removes_part(self) -> None:
        ev = threading.Event()
        ev.set()
        with self.assertRaises(tosec.Cancelled):
            tosec.download_pack(self.info, self.dest, cancel=ev,
                                opener=lambda r: FakeResponse(self.data))
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_truncated_download_is_resumed(self) -> None:
        cut = 250_000
        with self.assertRaises(tosec.TosecError):
            tosec.download_pack(self.info, self.dest,
                                opener=lambda r: FakeResponse(self.data[:cut], length=len(self.data)))
        part = self.dest / (PACK_NAME + ".part")
        self.assertEqual(part.stat().st_size, cut)  # kept for resuming
        ranges: list = []

        def opener(req):
            rng = req.get_header("Range")
            ranges.append(rng)
            if rng:
                start = int(rng.split("=")[1].rstrip("-"))
                resp = FakeResponse(self.data[start:], status=206)
                resp.headers["Content-Range"] = f"bytes {start}-{len(self.data) - 1}/{len(self.data)}"
                return resp
            return FakeResponse(self.data)
        path = tosec.download_pack(self.info, self.dest, opener=opener)
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(ranges, [None, f"bytes={cut}-"])
        self.assertFalse(part.exists())

    def test_range_ignored_restarts(self) -> None:
        part = self.dest / (PACK_NAME + ".part")
        part.write_bytes(b"garbage")
        path = tosec.download_pack(self.info, self.dest, opener=lambda r: FakeResponse(self.data))
        self.assertEqual(path.read_bytes(), self.data)

    def test_non_zip_download_is_discarded(self) -> None:
        with self.assertRaises(tosec.TosecError):
            tosec.download_pack(self.info, self.dest,
                                opener=lambda r: FakeResponse(b"<html>portal</html>" * 10))
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_cached_non_zip_is_not_reused(self) -> None:
        (self.dest / PACK_NAME).write_bytes(b"<html>" + b"x" * (len(self.data) - 6))
        path = tosec.download_pack(self.info, self.dest, opener=lambda r: FakeResponse(self.data))
        self.assertEqual(path.read_bytes(), self.data)

    def test_stale_extract_dirs_recovered(self) -> None:
        out = self.dest / "dats"
        old = self.dest / ".dats-old-1-2"
        old.mkdir()
        (old / "a.dat").write_text("x")
        (self.dest / ".dats-new-abc").mkdir()
        tosec.recover_dats_dir(out)
        self.assertTrue((out / "a.dat").is_file())  # killed between the two renames
        self.assertEqual(sorted(p.name for p in self.dest.iterdir() if p.name != "update.lock"), ["dats"])

    def test_unsafe_and_missing_filename(self) -> None:
        p = tosec.download_pack(self.info, self.dest,
                                opener=lambda r: FakeResponse(b"PK\x03\x04x", filename="../../etc/a:b.zip"))
        self.assertEqual(p, self.dest / "a_b.zip")
        p = tosec.download_pack(self.info, self.dest, opener=lambda r: FakeResponse(b"PK\x03\x04x", filename=None))
        self.assertEqual(p.parent, self.dest)
        self.assertIn("2025-03-13", p.name)


class ExtractAndListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_extract_filters_and_swaps(self) -> None:
        pack = self.root / "pack.zip"
        build_pack(pack)
        out = self.root / "data" / "dats"
        out.mkdir(parents=True)
        (out / "Old (TOSEC-v2000-01-01_CM).dat").write_text("old")
        n = tosec.extract_dats(pack, out)
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(n, 5)
        self.assertEqual(names, sorted([
            "Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat",
            "Commodore Amiga - Demos - [ADF] (TOSEC-v2024-01-01_CM).dat",
            "Sony PlayStation - Games (TOSEC-v2023-02-02_CM).dat",
            "Amiga - Magazines (TOSEC-v2022-03-03_CM).dat",
            "Nested Thing (TOSEC-v2020-01-01_CM).dat",
        ]))
        self.assertFalse((self.root / "evil.dat").exists())
        self.assertFalse((self.root / "data" / "evil2.dat").exists())
        # no leftover temp dirs
        self.assertEqual(sorted(p.name for p in (self.root / "data").iterdir() if p.name != "update.lock"), ["dats"])

    def test_extract_failure_keeps_old(self) -> None:
        out = self.root / "dats"
        out.mkdir()
        (out / "Keep (TOSEC-v2000-01-01).dat").write_text("x")
        bad = self.root / "bad.zip"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(zipfile.BadZipFile):
            tosec.extract_dats(bad, out)
        self.assertEqual([p.name for p in out.iterdir()], ["Keep (TOSEC-v2000-01-01).dat"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name != "update.lock"), ["bad.zip", "dats"])

    def test_pack_without_dats_keeps_installed(self) -> None:
        out = self.root / "dats"
        out.mkdir()
        (out / "Keep (TOSEC-v2000-01-01).dat").write_text("x")
        (out / "release.json").write_text('{"release": "2020-01-01"}')
        pack = self.root / "other.zip"
        with zipfile.ZipFile(pack, "w") as zf:
            zf.writestr("Other/foo.dat", "x")
        with self.assertRaises(tosec.TosecError):
            tosec.extract_dats(pack, out, release={"release": "2026-01-01"})
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["Keep (TOSEC-v2000-01-01).dat", "release.json"])
        self.assertEqual(tosec.installed_release(out), "2020-01-01")

    def test_update_lock_is_exclusive_across_descriptors(self) -> None:
        import threading as _t
        with tosec.update_lock(directory=self.root):
            with tosec.update_lock(directory=self.root):  # re-entrant in the same thread
                pass
            errors: list = []

            def other() -> None:  # another "process": a different thread has its own depth
                try:
                    with tosec.update_lock(directory=self.root):
                        pass
                except tosec.Busy as exc:
                    errors.append(exc)
            th = _t.Thread(target=other)
            th.start()
            th.join()
            self.assertEqual(len(errors), 1)
        with tosec.update_lock(directory=self.root):  # released afterwards
            pass

    def test_recover_skips_while_another_process_updates(self) -> None:
        out = self.root / "dats"
        (self.root / ".dats-new-live").mkdir()
        import threading as _t
        done = _t.Event()
        with tosec.update_lock(directory=self.root):
            th = _t.Thread(target=lambda: (tosec.recover_dats_dir(out), done.set()))
            th.start()
            th.join()
            self.assertTrue((self.root / ".dats-new-live").is_dir())  # live temp dir untouched
        tosec.recover_dats_dir(out)
        self.assertFalse((self.root / ".dats-new-live").exists())

    def test_list_follows_the_folder_without_a_stale_answer(self) -> None:
        d = self.root / "dats2"
        d.mkdir()
        (d / "A (TOSEC-v2024-01-01_CM).dat").write_text("x")
        first = tosec.list_dats(d)
        self.assertEqual([(i.name, i.version) for i in first], [("A", "2024-01-01")])
        self.assertEqual(tosec.list_dats(d)[0], first[0])                       # an unchanged folder: the same answer
        (d / "A (TOSEC-v2025-01-01_CM).dat").write_text("x")                    # a newer DAT of the same name
        (d / "B (TOSEC-v2024-01-01_CM).dat").write_text("x")
        (d / "sub.dat").mkdir()                                                  # a folder is never a DAT
        self.assertEqual([(i.name, i.version) for i in tosec.list_dats(d)], [("A", "2025-01-01"), ("B", "2024-01-01")])
        (d / "B (TOSEC-v2024-01-01_CM).dat").unlink()
        self.assertEqual([i.name for i in tosec.list_dats(d)], ["A"])

    def test_list_and_find_latest(self) -> None:
        d = self.root / "dats"
        d.mkdir()
        for name in [
            "Commodore Amiga - Games - [ADF] (TOSEC-v2024-01-01_CM).dat",
            "Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat",
            "Commodore Amiga - Games - [ADF] (TOSEC-v2023-05-05).dat",
            "Commodore Amiga - Games - [ADF] - Compilations (TOSEC-v2026-01-01_CM).dat",
            "Atari ST & STE - Games - [STX] (TOSEC-v2022-01-01_CM).dat",
            "Weird (Name) [x] (TOSEC-v2021-02-03_CM).dat",
            "custom.dat",
            "release.json",
        ]:
            (d / name).write_text("x")
        infos = tosec.list_dats(d)
        self.assertEqual([i.name for i in infos], [
            "Atari ST & STE - Games - [STX]", "Commodore Amiga - Games - [ADF]",
            "Commodore Amiga - Games - [ADF] - Compilations", "custom", "Weird (Name) [x]"])
        amiga = tosec.find_latest_dat(directory=d)
        assert amiga is not None
        self.assertEqual(amiga.version, "2025-01-30")
        self.assertEqual(amiga.path.name, "Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat")
        self.assertIsNone(tosec.find_latest_dat("Nope", d))
        self.assertEqual(tosec.list_dats(self.root / "missing"), [])

    @unittest.skipUnless(REAL_PACK.is_file(), "real TOSEC pack not available")
    def test_real_pack(self) -> None:
        out = self.root / "dats"
        t = time.perf_counter()
        try:
            n = tosec.extract_dats(REAL_PACK, out)
        except OSError as exc:
            if exc.errno == 28:   # ENOSPC: tmpfs too small for 4743 DATs
                self.skipTest("not enough temp space for the real pack")
            raise
        print(f"\n  real pack: {n} dats extracted in {time.perf_counter() - t:.2f}s", end=" ")
        self.assertEqual(n, 4743)
        info = tosec.find_latest_dat(directory=out)
        assert info is not None
        self.assertEqual(info.name, tosec.DEFAULT_DAT_NAME)
        self.assertEqual(info.version, "2025-01-30")
        self.assertEqual(len(tosec.list_dats(out)), 4743)


class UpdateDatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        pack = Path(self.tmp.name) / "src.zip"
        build_pack(pack)
        self.pack_bytes = pack.read_bytes()
        self.fetch = fake_fetch({tosec.DOWNLOADS_URL: fixture("tosec_downloads.html"),
                                 CAT_URL: fixture("tosec_category.html")})
        self.downloads = 0

    def opener(self, req):
        self.downloads += 1
        return FakeResponse(self.pack_bytes)

    def test_pipeline_and_skip(self) -> None:
        msgs: list[str] = []
        res = tosec.update_dats(progress=lambda d, t, m: msgs.append(m), fetch=self.fetch,
                                opener=self.opener)
        self.assertEqual(res, {"release": "2025-03-13", "count": 5, "skipped": False})
        rel = tosec.read_release()
        assert rel is not None
        self.assertEqual(rel["release"], "2025-03-13")
        self.assertEqual(rel["pack"], PACK_NAME)
        self.assertIn("downloaded_at", rel)
        self.assertIsNotNone(tosec.find_latest_dat())
        self.assertFalse(any(tosec.paths.cache_dir().glob("*.zip")))
        self.assertTrue(any("Downloading" in m for m in msgs))

        res = tosec.update_dats(fetch=self.fetch, opener=self.opener)
        self.assertTrue(res["skipped"])
        self.assertEqual(self.downloads, 1)

        res = tosec.update_dats(fetch=self.fetch, opener=self.opener, force=True)
        self.assertFalse(res["skipped"])
        self.assertEqual(self.downloads, 2)
        self.assertEqual(json.loads((tosec.paths.dats_dir() / "release.json").read_text())["release"],
                         "2025-03-13")

    def test_check_latest_and_installed_release(self) -> None:
        info = tosec.check_latest(self.fetch)
        self.assertEqual(info.date, "2025-03-13")
        self.assertIsNone(tosec.installed_release())
        tosec.update_dats(fetch=self.fetch, opener=self.opener, info=info)
        self.assertEqual(tosec.installed_release(), "2025-03-13")
        self.assertEqual(self.downloads, 1)
        # same release -> no second download, discovery skipped (no fetch needed)
        res = tosec.update_dats(fetch=lambda u: 1 / 0, opener=self.opener, info=info)
        self.assertTrue(res["skipped"])
        self.assertEqual(self.downloads, 1)
        # release.json without any dat is not "installed"
        for f in tosec.paths.dats_dir().glob("*.dat"):
            f.unlink()
        self.assertIsNone(tosec.installed_release())

    def test_release_json_swaps_with_the_dats(self) -> None:
        zip_path = Path(self.tmp.name) / "p.zip"
        zip_path.write_bytes(self.pack_bytes)
        out = tosec.paths.dats_dir()
        seen: list[bool] = []

        class Lock:
            def __enter__(s):
                seen.append((out / "release.json").exists())

            def __exit__(s, *a):
                return False

        tosec.extract_dats(zip_path, out, release={"release": "2025-03-13"}, commit_lock=Lock())
        self.assertEqual(seen, [False])  # lock taken only around the swap, old dir had none
        self.assertEqual(tosec.read_release()["release"], "2025-03-13")

    def test_cancel_before_swap_keeps_old_dats(self) -> None:
        out = tosec.paths.dats_dir()
        tosec.update_dats(fetch=self.fetch, opener=self.opener)
        before = sorted(p.name for p in out.iterdir())
        ev = threading.Event()
        ev.set()
        with self.assertRaises(tosec.Cancelled):
            tosec.update_dats(fetch=self.fetch, opener=self.opener, force=True, cancel=ev)
        self.assertEqual(sorted(p.name for p in out.iterdir()), before)
        self.assertFalse([p for p in out.parent.iterdir() if p.name.startswith(".dats-new")])


if __name__ == "__main__":
    unittest.main()
