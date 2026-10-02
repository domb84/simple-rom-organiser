"""Tests for romorg.autoupdate (fake tosec/nointro modules; no network)."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from romorg import autoupdate, paths, platforms
from romorg.nointro import NOINTRO_DATS, NoIntroError
from romorg.tosec import Cancelled, DatInfo, ReleaseInfo

GBA = "Nintendo - Game Boy Advance"
AMIGA_DAT = "Commodore Amiga - Games - [ADF]"


def wait_idle(mgr: autoupdate.UpdateManager, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if not mgr.status()["running"]:
            return
        time.sleep(0.01)
    raise AssertionError("update did not finish")


class FakeTosec:
    def __init__(self) -> None:
        self.latest = "2025-03-13"
        self.offline = False
        self.installed: str | None = None
        self.pack_downloads = 0
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def check_latest(self, fetch=None, timeout=None) -> ReleaseInfo:
        if self.offline:
            raise urllib.error.URLError("no route")
        return ReleaseInfo(self.latest, "cat", "dl")

    def installed_release(self, directory=None):
        return self.installed

    def update_dats(self, progress=None, cancel=None, force=False, info=None, commit_lock=None, **kw):
        assert info is not None and commit_lock is not None
        if not force and info.date == self.installed:
            return {"skipped": True}
        self.pack_downloads += 1
        self.started.set()
        if progress:
            progress(1, 2, f"Downloading TOSEC {info.date}")
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        with commit_lock:
            (paths.dats_dir() / f"{AMIGA_DAT} (TOSEC-v{info.date}_CM).dat").write_text("x")
            self.installed = info.date
        return {"release": info.date, "count": 1, "skipped": False}


class FakeNointro:
    def __init__(self, tosec_mod=None) -> None:
        self.remote_etag = {n: "a" for n in NOINTRO_DATS}
        self.local: dict[str, str] = {}   # name -> etag
        self.offline = False
        self.downloads: list[str] = []

    def check_updates(self, names=None, opener=None, directory=None, timeout=10):
        rows = []
        for n in (names if names is not None else NOINTRO_DATS):
            if self.offline:
                rows.append({"name": n, "installed": None, "status": "error", "error": "no route"})
            elif n not in self.local:
                rows.append({"name": n, "installed": None, "status": "missing"})
            else:
                st = "up_to_date" if self.local[n] == self.remote_etag[n] else "update_available"
                rows.append({"name": n, "installed": "2026.08.01", "status": st})
        return rows

    def list_dats(self, directory=None):
        return [DatInfo(n, "2026.08.01", paths.nointro_dir() / f"{n}.dat") for n in sorted(self.local)]

    def update_dats(self, names=None, progress=None, cancel=None, force=False, commit_lock=None, **kw):
        rows = []
        for n in names:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            self.downloads.append(n)
            with commit_lock:
                (paths.nointro_dir() / f"{n}.dat").write_text("x")
                changed = self.local.get(n) != self.remote_etag[n]
                self.local[n] = self.remote_etag[n]
            rows.append({"name": n, "version": "2026.08.01",
                         "status": "downloaded" if changed else "unchanged"})
        return {"dats": rows, "failed": 0}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("ROMORG_OFFLINE", None)
        self.tosec = FakeTosec()
        self.nointro = FakeNointro()
        self.clock = lambda: 1_700_000_000.0
        self.mgr = self.make()

    def make(self, **kw) -> autoupdate.UpdateManager:
        return autoupdate.UpdateManager(tosec=self.tosec, nointro=self.nointro, clock=self.clock, **kw)


class BackgroundTest(Base):
    def test_startup_downloads_everything_and_is_idempotent(self) -> None:
        self.mgr.start_background()
        self.mgr.start_background()
        wait_idle(self.mgr)
        st = self.mgr.status()
        self.assertEqual(st["state"], "idle")
        self.assertIsNone(st["error"])
        self.assertEqual(st["tosec"]["installed"], "2025-03-13")
        self.assertEqual(st["tosec"]["status"], "up_to_date")
        self.assertEqual(st["nointro"]["status"], "up_to_date")
        self.assertEqual(sorted(self.nointro.downloads), sorted(NOINTRO_DATS))
        self.assertEqual(st["last_checked"], "2023-11-14T22:13:20+00:00")
        self.assertFalse(st["scan_stale"])  # first install is not a replacement
        self.assertTrue(paths.updates_path().is_file())
        # persisted: a new manager remembers the check
        again = self.make().status()
        self.assertEqual(again["last_checked"], st["last_checked"])
        self.assertEqual(again["tosec"]["latest"], "2025-03-13")

    def test_same_release_means_no_pack_download(self) -> None:
        self.mgr.start_background()
        wait_idle(self.mgr)
        self.assertTrue(self.mgr.check())
        wait_idle(self.mgr)
        self.assertEqual(self.tosec.pack_downloads, 1)
        self.assertEqual(self.nointro.downloads.count(GBA), 1)  # etag unchanged -> not fetched again

    def test_newer_release_replaces_and_marks_scan_stale(self) -> None:
        self.mgr.start_background()
        wait_idle(self.mgr)
        self.tosec.latest = "2025-06-01"
        self.nointro.remote_etag[GBA] = "b"
        st = self.mgr.status()
        self.assertTrue(self.mgr.check())
        wait_idle(self.mgr)
        st = self.mgr.status()
        self.assertEqual(st["tosec"]["installed"], "2025-06-01")
        self.assertTrue(st["scan_stale"])
        self.assertEqual(self.nointro.downloads.count(GBA), 2)
        self.mgr.clear_stale()
        self.assertFalse(self.mgr.status()["scan_stale"])

    def test_disabled_does_nothing(self) -> None:
        mgr = autoupdate.UpdateManager(
            tosec=self.tosec, nointro=self.nointro, enabled=False)
        mgr.start_background()
        self.assertFalse(mgr.check())
        self.assertFalse(mgr.status()["running"])
        self.assertEqual(self.tosec.pack_downloads, 0)

    def test_forced_offline_env_disables(self) -> None:
        with mock.patch.dict(os.environ, {"ROMORG_OFFLINE": "1"}):
            self.assertFalse(self.make().enabled)
            self.assertTrue(paths.offline_forced())

    def test_check_refused_while_running_and_cancel(self) -> None:
        self.tosec.gate = threading.Event()
        self.assertTrue(self.mgr.check())
        self.assertTrue(self.tosec.started.wait(5))
        self.assertFalse(self.mgr.check())
        st = self.mgr.status()
        self.assertTrue(st["running"])
        self.assertEqual(st["tosec"]["status"], "updating")
        self.assertEqual(st["progress"]["source"], "tosec")
        self.assertTrue(self.mgr.cancel())
        wait_idle(self.mgr)
        st = self.mgr.status()
        self.assertEqual(st["state"], "idle")
        self.assertEqual(st["notice"], "Update cancelled")
        self.assertIsNone(st["tosec"]["installed"])  # never half-swapped
        self.assertFalse(self.mgr.cancel())


class OfflineTest(Base):
    def go_offline(self) -> None:
        self.tosec.offline = True
        self.nointro.offline = True

    def test_offline_with_cache_is_a_quiet_notice(self) -> None:
        self.mgr.start_background()
        wait_idle(self.mgr)
        self.go_offline()
        self.assertTrue(self.mgr.check())
        wait_idle(self.mgr)
        st = self.mgr.status()
        self.assertTrue(st["offline"])
        self.assertIsNone(st["error"])
        self.assertEqual(st["state"], "idle")
        self.assertEqual(st["notice"], "Offline - using the installed DATs (checked 2023-11-14)")
        # coming back online clears it
        self.tosec.offline = self.nointro.offline = False
        self.mgr.check()
        wait_idle(self.mgr)
        self.assertFalse(self.mgr.status()["offline"])
        self.assertIsNone(self.mgr.status()["notice"])

    def test_offline_nothing_cached_is_an_error(self) -> None:
        self.go_offline()
        self.mgr.start_background()
        wait_idle(self.mgr)
        st = self.mgr.status()
        self.assertEqual(st["state"], "error")
        self.assertEqual(st["error"]["code"], "offline")
        self.assertTrue(st["tosec"]["status"] in ("absent",))

    def test_disk_full_is_an_error_not_offline(self) -> None:
        import errno
        self.mgr.start_background()
        wait_idle(self.mgr)           # everything cached
        self.tosec.latest = "2026-01-01"

        def full(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")
        self.tosec.update_dats = full  # type: ignore[method-assign]
        self.assertTrue(self.mgr.check())
        wait_idle(self.mgr)
        st = self.mgr.status()
        self.assertFalse(st["offline"])
        self.assertIsNone(st["notice"])
        self.assertEqual(st["error"]["code"], "failed")
        self.assertIn("No space left", st["error"]["message"])

    def test_network_classification(self) -> None:
        import errno
        import http.client
        import socket
        net = autoupdate._is_network_error
        self.assertTrue(net(urllib.error.URLError("no route")))
        self.assertTrue(net(urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))))
        self.assertTrue(net(TimeoutError()))
        self.assertTrue(net(ConnectionResetError()))
        self.assertTrue(net(http.client.IncompleteRead(b"", 5)))
        self.assertTrue(net(OSError(errno.ENETUNREACH, "unreachable")))
        for code in (errno.ENOSPC, errno.EACCES, errno.EROFS, errno.ENOENT):
            self.assertFalse(net(OSError(code, "local")), code)
        self.assertFalse(net(FileNotFoundError("x")))
        self.assertFalse(net(urllib.error.URLError(OSError(errno.ENOSPC, "full"))))

    def test_busy_lock_is_a_notice(self) -> None:
        from romorg.tosec import Busy
        self.mgr.start_background()
        wait_idle(self.mgr)

        orig = self.tosec
        self.tosec.update_lock = lambda directory=None: (_ for _ in ()).throw(Busy("Another copy"))  # type: ignore[attr-defined]
        self.mgr.check()
        wait_idle(self.mgr)
        st = self.mgr.status()
        self.assertEqual(st["notice"], "Another copy")
        self.assertIsNone(st["error"])
        del orig.update_lock

    def test_partial_failure_keeps_other_source(self) -> None:
        self.tosec.offline = True   # tosecdev down, github up
        self.mgr.start_background()
        wait_idle(self.mgr)
        st = self.mgr.status()
        self.assertFalse(st["offline"])
        self.assertEqual(st["nointro"]["status"], "up_to_date")
        self.assertEqual(st["tosec"]["status"], "absent")


class EnsureTest(Base):
    def test_present_returns_immediately(self) -> None:
        (paths.nointro_dir() / f"{GBA}.dat").write_text("x")
        self.nointro.local[GBA] = "a"
        self.mgr.ensure(platforms.get_platform("Nintendo Game Boy Advance"))
        self.assertEqual(self.nointro.downloads, [])

    def test_missing_triggers_scoped_update_with_progress(self) -> None:
        msgs: list[str] = []
        self.mgr.ensure(platforms.get_platform("Nintendo Game Boy Advance"),
                        progress=lambda d, t, m: msgs.append(m))
        self.assertEqual(self.nointro.downloads, [GBA])    # only that platform's DAT
        self.assertEqual(self.tosec.pack_downloads, 0)
        self.assertFalse(self.mgr.status()["running"])

    def test_amiga_waits_for_running_background_update(self) -> None:
        self.tosec.gate = threading.Event()
        self.mgr.start_background()
        self.assertTrue(self.tosec.started.wait(5))
        msgs: list[str] = []
        done = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                self.mgr.ensure(platforms.get_platform("Commodore Amiga"),
                                progress=lambda d, t, m: msgs.append(m))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        time.sleep(0.5)
        self.assertFalse(done.is_set())
        self.tosec.gate.set()
        self.assertTrue(done.wait(5))
        self.assertEqual(errors, [])
        self.assertTrue(any("TOSEC" in m for m in msgs))
        self.assertEqual(self.tosec.pack_downloads, 1)   # waited, did not start a second download

    def test_offline_nothing_cached_raises(self) -> None:
        self.tosec.offline = self.nointro.offline = True
        with self.assertRaises(autoupdate.UpdateError) as cm:
            self.mgr.ensure(platforms.get_platform("Commodore Amiga"))
        self.assertEqual(cm.exception.code, "offline")

    def test_disabled_missing_raises_offline(self) -> None:
        mgr = autoupdate.UpdateManager(tosec=self.tosec, nointro=self.nointro, enabled=False)
        with self.assertRaises(autoupdate.UpdateError) as cm:
            mgr.ensure(platforms.get_platform("Nintendo 64"))
        self.assertEqual(cm.exception.code, "offline")

    def test_job_cancel_cancels_update(self) -> None:
        self.tosec.gate = threading.Event()
        cancel = threading.Event()
        errors: list[autoupdate.UpdateError] = []

        def run() -> None:
            try:
                self.mgr.ensure(platforms.get_platform("Commodore Amiga"), cancel=cancel)
            except autoupdate.UpdateError as exc:
                errors.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(self.tosec.started.wait(5))
        cancel.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors[0].code, "cancelled")
        self.assertIsNone(self.tosec.installed)

    def test_failed_download_raises_failed(self) -> None:
        def boom(*a, **k):
            raise NoIntroError("GBA: HTTP 500 from x")
        self.nointro.update_dats = boom
        with self.assertRaises(autoupdate.UpdateError) as cm:
            self.mgr.ensure(platforms.get_platform("Nintendo Game Boy Advance"))
        self.assertEqual(cm.exception.code, "failed")


class ScanRaceTest(Base):
    def test_commit_waits_for_dat_lock(self) -> None:
        """A scan holding dat_lock (loading DATs) delays the commit, never the download."""
        (paths.nointro_dir() / f"{GBA}.dat").write_text("x")
        self.nointro.local[GBA] = "a"
        self.nointro.remote_etag[GBA] = "b"
        with self.mgr.dat_lock:
            self.mgr.check()
            time.sleep(0.3)
            self.assertTrue(self.mgr.status()["running"])   # blocked on the commit
            self.assertEqual(self.nointro.local[GBA], "a")  # live DAT untouched
        wait_idle(self.mgr)
        self.assertEqual(self.nointro.local[GBA], "b")
        self.assertTrue(self.mgr.status()["scan_stale"])


if __name__ == "__main__":
    unittest.main()
