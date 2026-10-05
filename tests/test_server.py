"""Tests for romorg.server: HTTP layer, security checks, jobs and endpoints.

The core modules (tosec, platforms, datfile, scanner, organiser, m3u,
kickstart) are replaced with fakes in ``sys.modules`` so these tests only
exercise the server itself, against the AMENDMENT 1 interface contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chdtestlib import find_bash  # noqa: E402
from romorg import server  # noqa: E402
# Import the real library module before any test swaps fakes into sys.modules (it is not faked here:
# it only holds the profile data classes), so the server always finds it.
from romorg import library as _library  # noqa: E402,F401

GAMES = "Commodore Amiga - Games - [ADF]"
WB = "Commodore Amiga - Operating Systems - Workbench"
KICKDISKS = "Commodore Amiga - Kickstart-Disks"
FIRMWARE = "Commodore Amiga - Firmware"


# ----------------------------------------------------------------- fake data

@dataclass(frozen=True)
class Rom:
    name: str
    size: int
    crc: str
    md5: str = ""
    sha1: str = ""
    game: str = ""
    dat: str = ""


@dataclass
class Entry:
    path: Path
    member: str | None
    size: int
    crc: str
    sha1: str | None = None


@dataclass
class Match:
    entry: Entry
    roms: list[Rom]
    primary: list[Rom] = field(default_factory=list)


@dataclass
class ScanResult:
    root: Path
    dat_names: list[str]
    matched: list[Match] = field(default_factory=list)
    unmatched: list[Entry] = field(default_factory=list)
    unsupported: list[Path] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    missing: list[Rom] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        per_dat = {}
        for name in self.dat_names:
            have = sum(1 for m in self.matched if m.primary and m.primary[0].dat == name)
            miss = sum(1 for r in self.missing if r.dat == name)
            per_dat[name] = {"dat_total": have + miss, "have": have, "missing": miss,
                             "matched_files": have, "correctly_placed": 0}
        return {"dat_total": len(self.matched) + len(self.missing), "have": len(self.matched),
                "missing": len(self.missing), "matched_files": len(self.matched),
                "unmatched_files": len(self.unmatched), "duplicates": 0,
                "unsupported": len(self.unsupported), "errors": len(self.errors),
                "correctly_named": 0, "to_rename": len(self.matched), "per_dat": per_dat}


@dataclass
class RenameOp:
    src: Path
    dst: Path
    status: str
    reason: str = ""
    rom_name: str = ""
    kind: str = "move"


@dataclass
class M3UOp:
    path: Path
    lines: list[str]
    status: str
    reason: str = ""


@dataclass
class KickOp:
    target: Path
    source: Entry | None
    status: str
    reason: str = ""
    description: str = ""


@dataclass
class DatInfo:
    name: str
    version: str
    path: Path


@dataclass(frozen=True)
class Platform:
    name: str
    dats: tuple[str, ...]
    m3u_dats: tuple[str, ...]
    kickstart_dat: str | None
    latest_dats: tuple[str, ...] = ()
    best_variant_dats: tuple[str, ...] = ()


AMIGA = Platform("Commodore Amiga", (GAMES, WB, KICKDISKS, FIRMWARE), (GAMES, WB, KICKDISKS), FIRMWARE,
                 latest_dats=(GAMES,), best_variant_dats=(GAMES,))
OTHER = Platform("Atari ST", ("Atari ST - Games",), ("Atari ST - Games",), None)


class UpdateError(Exception):
    """Stand-in for ``autoupdate.UpdateError`` (the server matches it by class name)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class FakeUpdates:
    """Stand-in for ``autoupdate.UpdateManager`` (no network, no threads)."""

    enabled = True
    dat_lock = None

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.ensure_error: Exception | None = None
        self.started = False
        self.running = False
        self.stale = False

    def start_background(self) -> None:
        self.started = True

    def check(self, force: bool = False) -> bool:
        self.calls.append(("check", force))
        return not self.running

    def cancel(self) -> bool:
        self.calls.append(("cancel",))
        return self.running

    def ensure(self, platform: Any, progress: Any = None, cancel: Any = None) -> None:
        self.calls.append(("ensure", platform.name))
        if progress:
            progress(1, 2, "Downloading DATs")
        if self.ensure_error is not None:
            raise self.ensure_error

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "state": "downloading" if self.running else "idle", "running": self.running,
                "offline": False, "notice": "", "last_checked": "2026-10-02T14:02:00", "scan_stale": self.stale,
                "progress": {"done": 0, "total": 0, "message": "", "source": ""}, "error": None,
                "tosec": {"installed": "2025-03-13", "latest": "2025-03-13", "status": "up_to_date",
                          "checked_at": None},
                "nointro": {"installed": "2026.08.01", "latest": None, "status": "up_to_date", "dats": [],
                            "checked_at": None}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerTestCase(unittest.TestCase):
    """Starts a server on a random port with fake core modules."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-server-test-"))
        self.data = self.tmp / "data"
        self.roms = (self.tmp / "roms").resolve()
        (self.roms / "sub").mkdir(parents=True)
        (self.roms / ".hidden").mkdir()
        (self.roms / "file.txt").write_text("x")
        self.bios = self.tmp / "bios"
        self.bios.mkdir()
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        self.dats_dir = self.data / "dats"
        self.dats_dir.mkdir(parents=True)
        self.dat_paths = {
            GAMES: self.dats_dir / f"{GAMES} (TOSEC-v2025-01-30_CM).dat",
            FIRMWARE: self.dats_dir / f"{FIRMWARE} (TOSEC-v2025-01-03_CM).dat",
            WB: self.dats_dir / f"{WB} (TOSEC-v2023-05-21_CM).dat",
        }  # Kickstart-Disks deliberately not "downloaded"
        for path in self.dat_paths.values():
            path.write_text("<datafile/>")
        (self.dats_dir / "release.json").write_text(json.dumps({"release": "2025-03-13"}))
        self._install_fakes()

        self.srv = server.make_server("127.0.0.1", 0, token="test-token", auto_update=False)
        self.port = self.srv.port
        self.updates = FakeUpdates()
        self.srv.app.updates = self.updates
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- fakes

    def _install_fakes(self) -> None:
        root = self.roms
        games_dir = root / GAMES
        a1 = Rom("Game (1990)(Pub)(Disk 1 of 2).adf", 10, "00000001", game="Game (1990)(Pub)(Disk 1 of 2)", dat=GAMES)
        a2 = Rom("Game (1990)(Pub)(Disk 2 of 2).adf", 10, "00000002", game="Game (1990)(Pub)(Disk 2 of 2)", dat=GAMES)
        wb = Rom("Workbench v1.3 (1988)(Commodore).adf", 10, "00000004", game="Workbench v1.3", dat=WB)
        kick = Rom("Kickstart v1.3 r34.5 (1987)(Commodore)(A500).rom", 10, "00000005",
                   md5="82a21c1890cae844b3df741f2762d48d", game="Kickstart v1.3", dat=FIRMWARE)
        miss = Rom("Other (1991)(Pub).adf", 10, "00000003", game="Other (1991)(Pub)", dat=GAMES)
        miss_wb = Rom("Workbench v2.0 (1990)(Commodore).adf", 10, "00000006", game="Workbench v2.0", dat=WB)
        # The Workbench disk is also listed in Games (lower priority -> primary is Games? no:
        # primary = highest priority DAT among matches, which is Games here).
        self.kick_entry = Entry(root / "kick13.rom", None, 10, "00000005")
        self.result = ScanResult(
            root=root, dat_names=[GAMES, WB, FIRMWARE],
            matched=[
                Match(Entry(games_dir / a1.name, None, 10, "00000001"), [a1], [a1]),
                Match(Entry(root / "sub" / "pack.zip", "g2.adf", 10, "00000002"), [a2], [a2]),
                Match(Entry(root / "wb13.adf", None, 10, "00000004"), [wb], [wb]),
                Match(self.kick_entry, [kick], [kick]),
            ],
            unmatched=[Entry(root / "junk.bin", None, 4, "deadbeef")],
            unsupported=[root / "x.7z"],
            errors=[(root / "bad.adf", "Permission denied")],
            missing=[miss, miss_wb],
        )
        self.scan_gate: threading.Event | None = None
        self.calls: dict[str, list[Any]] = {"scan": [], "apply": [], "undo": [], "write": [], "m3u_plan": [],
                                            "load": [], "kick_plan": [], "kick_apply": []}
        test = self

        def scan(root_: Path, dats: Any, recursive: bool = True, progress: Any = None, cancel: Any = None) -> ScanResult:
            test.calls["scan"].append((root_, dats, recursive))
            if progress:
                progress(1, 2, "g1.adf")
            if test.scan_gate is not None:
                while not test.scan_gate.wait(0.02):
                    if cancel is not None and cancel():
                        raise RuntimeError("cancelled")
            return test.result

        self.undo_log = root / ".romorg-undo-20250101-000000.json"
        self.undo_log.write_text(json.dumps({"moves": [{"src": "a", "dst": "b"}, {"src": "c", "dst": "d"}],
                                             "removed_dirs": []}))

        tosec = types.ModuleType("romorg.tosec")
        tosec.DEFAULT_DAT_NAME = GAMES
        tosec.list_dats = lambda directory=None: sorted(
            (DatInfo(name, path.name.split("TOSEC-v")[1][:10], path) for name, path in test.dat_paths.items()),
            key=lambda d: d.name)
        tosec.update_dats = mock.Mock(return_value={"release": "2025-03-13", "count": 3})

        platforms = types.ModuleType("romorg.platforms")
        platforms.PLATFORMS = {AMIGA.name: AMIGA, OTHER.name: OTHER}
        platforms.list_platforms = lambda: [AMIGA, OTHER]

        def get_platform(name: str) -> Platform:
            return platforms.PLATFORMS[name]

        def load_platform_dats(platform: Platform, directory: Any = None) -> tuple[list[Any], list[str]]:
            test.calls["load"].append(platform.name)
            present = [n for n in platform.dats if n in test.dat_paths]
            return ([types.SimpleNamespace(name=n) for n in present],
                    [n for n in platform.dats if n not in test.dat_paths])

        platforms.get_platform = get_platform
        platforms.load_platform_dats = load_platform_dats

        scanner = types.ModuleType("romorg.scanner")
        scanner.scan = scan

        organiser = types.ModuleType("romorg.organiser")
        organiser.safe_filename = lambda name: name.replace(":", "_")
        organiser.plan_renames = lambda result: [
            RenameOp(games_dir / a1.name, games_dir / a1.name, "ok", rom_name=a1.name),
            RenameOp(root / "sub" / "pack.zip", games_dir / "Game (1990)(Pub)(Disk 2 of 2).zip", "conflict",
                     reason="target exists"),
            RenameOp(root / "wb13.adf", root / WB / wb.name, "move", rom_name=wb.name),
            RenameOp(root / "junk.bin", root / "_unmatched" / "junk.bin", "move"),
            RenameOp(root / "kick13.rom", root / FIRMWARE / kick.name, "move", rom_name=kick.name),
        ]

        def apply_renames(ops: list[RenameOp], root_: Path, progress: Any = None) -> dict[str, Any]:
            test.calls["apply"].append(ops)
            return {"moved": 3, "failed": [], "undo_log": test.undo_log}

        organiser.apply_renames = apply_renames
        organiser.list_undo_logs = lambda root_: [test.undo_log]

        def undo(log: Path) -> dict[str, Any]:
            test.calls["undo"].append(log)
            return {"restored": 3}

        organiser.undo = undo

        m3u = types.ModuleType("romorg.m3u")

        def plan_m3us(result: Any, savedisk: bool = False, labels: bool = True,
                      m3u_dats: Any = None) -> list[M3UOp]:
            test.calls["m3u_plan"].append(m3u_dats)
            return [
                M3UOp(games_dir / "Game (1990)(Pub).m3u",
                      ["# Generated by simple-rom-organiser", "g1.adf|Disk 1", "sub/pack.zip|Disk 2"]
                      + (["#SAVEDISK:"] if savedisk else []), "write"),
                M3UOp(games_dir / "Other (1991)(Pub).m3u", [], "incomplete", "missing disk 2"),
            ]

        m3u.plan_m3us = plan_m3us

        def write_m3us(ops: list[M3UOp]) -> dict[str, Any]:
            test.calls["write"].append(ops)
            return {"written": sum(op.status == "write" for op in ops)}

        m3u.write_m3us = write_m3us

        kickstart = types.ModuleType("romorg.kickstart")
        kickstart.detect_system_dirs = lambda: [
            {"path": str(self.tmp / "nope" / "bios"), "label": "RetroDECK", "exists": False},
            {"path": str(self.bios), "label": "EmuDeck", "exists": True},
        ]

        def plan_kickstarts(result: Any, dest: Path) -> list[KickOp]:
            test.calls["kick_plan"].append(dest)
            target = dest / "kick34005.A500"
            copied = target.exists()
            return [
                KickOp(dest / "kick40068.A1200", None, "missing", "not found locally", "Kickstart v3.1 r40.068 (A1200)"),
                KickOp(target, test.kick_entry, "ok" if copied else "copy", "", "Kickstart v1.3 r34.5 (A500)"),
            ]

        def apply_kickstarts(ops: list[KickOp]) -> dict[str, Any]:
            test.calls["kick_apply"].append(ops)
            copied = 0
            for op in ops:
                if op.status == "copy":
                    op.target.write_bytes(b"kick")
                    copied += 1
            return {"copied": copied, "failed": []}

        kickstart.plan_kickstarts = plan_kickstarts
        kickstart.apply_kickstarts = apply_kickstarts

        patcher = mock.patch.dict(sys.modules, {
            "romorg.tosec": tosec, "romorg.platforms": platforms, "romorg.scanner": scanner,
            "romorg.organiser": organiser, "romorg.m3u": m3u, "romorg.kickstart": kickstart,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    # ----------------------------------------------------------- helpers

    def request(self, method: str, path: str, body: Any = None, token: str | None = "test-token",
                host: str | None = None) -> tuple[int, Any, Any]:
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = "application/json"
            if token is not None:
                headers["X-Romorg-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw, status, hdrs = resp.read(), resp.status, resp.headers
        except urllib.error.HTTPError as err:
            raw, status, hdrs = err.read(), err.code, err.headers
        ctype = hdrs.get("Content-Type", "")
        payload = json.loads(raw) if ctype.startswith("application/json") and raw else raw
        return status, payload, hdrs

    def get(self, path: str) -> Any:
        status, payload, _ = self.request("GET", path)
        self.assertEqual(status, 200, payload)
        return payload

    def post(self, path: str, body: Any = None) -> Any:
        status, payload, _ = self.request("POST", path, body)
        self.assertEqual(status, 200, payload)
        return payload

    def wait_job(self, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get("/api/job")
            if job and job["status"] != "running":
                return job
            time.sleep(0.02)
        self.fail("job did not finish")

    def scan(self, platform: str = "Commodore Amiga") -> dict[str, Any]:
        self.post("/api/scan", {"path": str(self.roms), "platform": platform})
        job = self.wait_job()
        self.assertEqual(job["status"], "done", job)
        return job

    def q(self, value: str) -> str:
        return urllib.request.quote(value)


class StaticAndSecurityTests(ServerTestCase):
    def test_index_has_token_and_security_headers(self) -> None:
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn('content="test-token"', html)
        self.assertNotIn(server.TOKEN_PLACEHOLDER, html)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        for element_id in ("home-groups", "sys-tabs", "tab-browse", "dat-cards", "organise-dest-filters", "kick-dest", "kick-apply-btn"):
            self.assertIn(f'id="{element_id}"', html)

    def test_static_assets(self) -> None:
        for path, ctype in (("/static/app.js", "text/javascript"), ("/static/style.css", "text/css")):
            status, body, headers = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(ctype))
            self.assertGreater(len(body), 100)

    def test_ui_ids_exist(self) -> None:
        """Every $("id") used by app.js must exist in index.html."""
        import re
        js = server.read_static("app.js").decode()
        html = server.read_static("index.html").decode()
        ids = set(re.findall(r'\$\("([\w-]+)"\)', js))
        missing = sorted(i for i in ids if f'id="{i}"' not in html)
        self.assertEqual(missing, [])

    def test_unknown_paths_404(self) -> None:
        for path in ("/static/../server.py", "/static/server.py", "/nope", "/api/nope"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)

    def test_bad_host_rejected(self) -> None:
        status, body, _ = self.request("GET", "/api/status", host=f"evil.example:{self.port}")
        self.assertEqual(status, 403)
        self.assertIn("Host", body["error"])
        status, _, _ = self.request("GET", "/", host="127.0.0.1:1")
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/status", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)

    def test_post_requires_token(self) -> None:
        for path in ("/api/job/cancel", "/api/kickstart/apply", "/api/kickstart/plan", "/api/quit"):
            status, _, _ = self.request("POST", path, token=None)
            self.assertEqual(status, 403, path)
            status, _, _ = self.request("POST", path, token="wrong")
            self.assertEqual(status, 403, path)
        status, body, _ = self.request("POST", "/api/job/cancel")
        self.assertEqual((status, body), (200, {"cancelled": False}))

    def test_wrong_method(self) -> None:
        for path in ("/api/scan", "/api/kickstart/plan"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 405, path)

    def test_invalid_json_body(self) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/scan", data=b"{nope", method="POST",
                                     headers={"X-Romorg-Token": "test-token"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)


class EndpointTests(ServerTestCase):
    def test_status(self) -> None:
        status = self.get("/api/status")
        self.assertEqual(status["release"], "2025-03-13")
        self.assertEqual(status["dats_count"], 3)
        self.assertEqual(status["default_platform"], "Commodore Amiga")
        self.assertIsNone(status["scan"])
        self.assertEqual(status["folders"], {})
        for key in ("version", "last_dir", "last_platform", "kickstart_dest", "has_7z", "dialog_available"):
            self.assertIn(key, status)

    def test_platforms(self) -> None:
        plats = self.get("/api/platforms")
        self.assertEqual([p["name"] for p in plats], ["Commodore Amiga", "Atari ST"])
        amiga = plats[0]
        self.assertEqual([d["name"] for d in amiga["dats"]], list(AMIGA.dats))  # priority order kept
        by_name = {d["name"]: d for d in amiga["dats"]}
        self.assertEqual(by_name[GAMES]["version"], "2025-01-30")
        self.assertTrue(by_name[GAMES]["present"])
        self.assertTrue(by_name[GAMES]["m3u"])
        self.assertEqual(by_name[KICKDISKS], {"name": KICKDISKS, "source": "tosec", "version": None,
                                              "present": False, "file": None,
                                              "folder": KICKDISKS, "m3u": True, "kickstart": False})
        self.assertEqual((amiga["source"], amiga["layout"], amiga["convertible"], amiga["latest_only"]),
                         ("tosec", "per_dat", False, True))  # library defaults: all rules on
        self.assertEqual(amiga["library"]["best_variant"], True)
        self.assertEqual(amiga["library"]["exclude"], sorted(amiga["library"]["exclude"]))
        self.assertIsNone(amiga["folder"])
        self.assertTrue(by_name[FIRMWARE]["kickstart"])
        self.assertFalse(amiga["complete"])
        self.assertEqual(amiga["kickstart_dat"], FIRMWARE)
        self.assertFalse(plats[1]["complete"])

    def test_dats_list_and_filter(self) -> None:
        dats = self.get("/api/dats")
        self.assertEqual(len(dats), 3)
        self.assertEqual(len(self.get("/api/dats?q=firmware")), 1)

    def test_updates_endpoints(self) -> None:
        status = self.get("/api/updates")
        self.assertEqual((status["state"], status["tosec"]["installed"], status["nointro"]["installed"]),
                         ("idle", "2025-03-13", "2026.08.01"))
        self.assertIs(self.get("/api/status")["updates"]["running"], False)
        res = self.post("/api/updates/check", {"force": True})
        self.assertEqual((res["started"], res["updates"]["state"]), (True, "idle"))
        self.assertEqual(self.updates.calls[-1], ("check", True))
        res = self.post("/api/dats/update", {})  # old name = alias, no job any more
        self.assertEqual(set(res), {"started", "updates"})
        self.assertEqual(self.updates.calls[-1], ("check", False))
        self.assertIsNone(self.get("/api/job"))
        self.updates.running = True
        self.assertFalse(self.post("/api/updates/check", {})["started"])  # one update at a time
        self.assertEqual(self.post("/api/updates/cancel"), {"cancelled": True})
        self.updates.running = False
        status, _, _ = self.request("POST", "/api/updates/check", {}, token=None)
        self.assertEqual(status, 403)

    def test_scan_stale_after_dat_update(self) -> None:
        self.scan()
        self.assertFalse(self.get("/api/updates")["scan_stale"])
        path = self.dat_paths[GAMES]
        os.utime(path, ns=(1, 1))  # a newer DAT was installed behind the scan's back
        self.assertTrue(self.get("/api/updates")["scan_stale"])

    def test_scan_stale_clears_after_rescan(self) -> None:
        self.scan()
        os.utime(self.dat_paths[GAMES], ns=(1, 1))
        self.assertTrue(self.get("/api/updates")["scan_stale"])
        self.scan()  # a fresh scan has parsed the new DATs
        self.assertFalse(self.get("/api/updates")["scan_stale"])
        self.updates.stale = True  # the manager-level flag alone does not make a scan stale
        self.assertFalse(self.get("/api/updates")["scan_stale"])

    def test_app_starts_background_update(self) -> None:
        srv = server.make_server("127.0.0.1", 0, token="x", auto_update=False)
        self.addCleanup(srv.server_close)
        self.assertFalse(srv.app.updates.enabled)
        with mock.patch.object(server.App, "_make_updates", return_value=FakeUpdates()):
            srv2 = server.make_server("127.0.0.1", 0, token="x")
        self.addCleanup(srv2.server_close)
        self.assertTrue(srv2.app.updates.started)

    def test_fs_list(self) -> None:
        listing = self.get(f"/api/fs/list?path={self.q(str(self.roms))}")
        self.assertEqual(listing["path"], str(self.roms))
        self.assertEqual([d["name"] for d in listing["dirs"]], ["sub"])  # no hidden, no files
        self.assertEqual(listing["parent"], str(self.roms.parent))
        self.assertTrue(any(p["name"] == "Home" for p in listing["places"]))
        listing = self.get(f"/api/fs/list?hidden=1&path={self.q(str(self.roms))}")
        self.assertEqual([d["name"] for d in listing["dirs"]], [".hidden", "sub"])
        self.assertEqual(self.get("/api/fs/list")["path"], str(Path.home().resolve()))

    def test_fs_list_rejects_bad_paths(self) -> None:
        for bad in ("relative/dir", str(self.roms / "missing"), str(self.roms / "file.txt")):
            status, body, _ = self.request("GET", f"/api/fs/list?path={self.q(bad)}")
            self.assertEqual(status, 400, bad)
            self.assertIn("error", body)

    def test_fs_pick_without_dialog(self) -> None:
        with mock.patch.object(server, "_dialog_command", return_value=None):
            status, _, _ = self.request("POST", "/api/fs/pick", {})
        self.assertEqual(status, 501)

    def test_fs_pick_kdialog(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout=f"{self.roms}\n", stderr="")
        with mock.patch.object(server, "_dialog_command", return_value="kdialog"), \
                mock.patch.object(server.subprocess, "run", return_value=done) as run:
            self.assertEqual(self.post("/api/fs/pick", {"title": "Pick BIOS"})["path"], str(self.roms))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "kdialog")
        self.assertEqual(cmd[cmd.index("--title") + 1], "Pick BIOS")
        cancelled = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with mock.patch.object(server, "_dialog_command", return_value="zenity"), \
                mock.patch.object(server.subprocess, "run", return_value=cancelled):
            self.assertEqual(self.post("/api/fs/pick", {}), {"cancelled": True})

    def test_results_before_scan(self) -> None:
        status, _, _ = self.request("GET", "/api/scan/results?kind=matched")
        self.assertEqual(status, 409)
        for path in ("/api/organise/plan", "/api/kickstart/plan", "/api/kickstart/apply"):
            status, _, _ = self.request("POST", path, {"dest": str(self.bios)})
            self.assertEqual(status, 409, path)

    def test_scan_validation(self) -> None:
        status, _, _ = self.request("POST", "/api/scan", {"path": str(self.roms / "nope"), "platform": ""})
        self.assertEqual(status, 400)
        status, body, _ = self.request("POST", "/api/scan", {"path": str(self.roms), "platform": "Sega Saturn"})
        self.assertEqual(status, 400)
        self.assertIn("Unknown platform", body["error"])

    def test_scan_without_any_dats_fails(self) -> None:
        with mock.patch("traceback.print_exc"):
            self.post("/api/scan", {"path": str(self.roms), "platform": "Atari ST"})
            job = self.wait_job()
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error_code"], "no_dats")
        self.assertEqual(self.updates.calls[-1], ("ensure", "Atari ST"))  # the scan asked for the update first

    @mock.patch("traceback.print_exc")
    def test_scan_waits_for_update_and_reports_offline(self, _tb: Any) -> None:
        job = self.scan()  # DATs present: ensure() is still consulted, progress is forwarded
        self.assertEqual(self.updates.calls[-1], ("ensure", "Commodore Amiga"))
        self.updates.ensure_error = UpdateError("offline")
        self.post("/api/scan", {"path": str(self.roms), "platform": "Commodore Amiga"})
        job = self.wait_job()
        self.assertEqual((job["status"], job["error_code"]), ("error", "offline_no_dats"))
        self.assertIn("Retry", job["error"])
        self.updates.ensure_error = UpdateError("failed", "HTTP 500")
        self.post("/api/scan", {"path": str(self.roms), "platform": "Commodore Amiga"})
        job = self.wait_job()
        self.assertEqual((job["status"], job["error_code"]), ("error", "update_failed"))
        self.updates.ensure_error = UpdateError("cancelled")
        self.post("/api/scan", {"path": str(self.roms), "platform": "Commodore Amiga"})
        job = self.wait_job()
        self.assertEqual((job["status"], job["error_code"]), ("error", "update_cancelled"))

    def test_scan_and_results(self) -> None:
        job = self.scan()
        self.assertEqual(job["kind"], "scan")
        self.assertEqual(job["result"]["have"], 4)
        self.assertEqual(job["result"]["missing_dats"], [KICKDISKS])
        self.assertIn(GAMES, job["result"]["per_dat"])
        self.assertEqual(job["progress"]["total"], 2)
        root_, dats, recursive = self.calls["scan"][0]
        self.assertEqual(root_, self.roms)
        self.assertEqual([d.name for d in dats], [GAMES, WB, FIRMWARE])  # priority order
        self.assertTrue(recursive)
        # Parsed DATs are cached between scans of the same platform.
        self.scan()
        self.assertEqual(self.calls["load"], ["Commodore Amiga"])

        status = self.get("/api/status")
        self.assertEqual(status["scan"]["root"], str(self.roms))
        self.assertEqual(status["scan"]["platform"], "Commodore Amiga")
        self.assertEqual(status["scan"]["dat_names"], [GAMES, WB, FIRMWARE])
        self.assertEqual(status["scan"]["missing_dats"], [KICKDISKS])
        self.assertEqual(status["last_platform"], "Commodore Amiga")
        self.assertEqual(status["last_dir"], str(self.roms))
        self.assertEqual(status["folders"], {"Commodore Amiga": str(self.roms)})

        matched = self.get("/api/scan/results?kind=matched")
        self.assertEqual(matched["total"], 4)
        first = matched["items"][0]
        self.assertEqual(first["file"], f"{GAMES}/Game (1990)(Pub)(Disk 1 of 2).adf")
        self.assertEqual(first["dat"], GAMES)
        self.assertTrue(first["placed_ok"] and first["named_ok"])
        second = matched["items"][1]
        self.assertEqual(second["file"], "sub/pack.zip::g2.adf")
        self.assertFalse(second["placed_ok"])
        self.assertEqual(self.get("/api/scan/results?kind=matched&q=DISK+2")["total"], 1)
        page = self.get("/api/scan/results?kind=matched&offset=1&limit=1")
        self.assertEqual((page["total"], len(page["items"]), page["offset"]), (4, 1, 1))
        # DAT filter
        only_wb = self.get(f"/api/scan/results?kind=matched&dat={self.q(WB)}")
        self.assertEqual([i["file"] for i in only_wb["items"]], ["wb13.adf"])
        missing = self.get("/api/scan/results?kind=missing")
        self.assertEqual(missing["total"], 2)
        self.assertEqual(missing["items"][1]["dat"], WB)
        self.assertEqual(self.get(f"/api/scan/results?kind=missing&dat={self.q(GAMES)}")["items"][0]["name"],
                         "Other (1991)(Pub).adf")
        self.assertEqual(self.get("/api/scan/results?kind=unmatched")["items"][0]["file"], "junk.bin")
        self.assertEqual(self.get("/api/scan/results?kind=unsupported")["items"][0]["file"], "x.7z")
        self.assertEqual(self.get("/api/scan/results?kind=errors")["items"][0]["error"], "Permission denied")
        self.assertEqual(self.get("/api/scan/results?kind=rename")["total"], 5)
        self.assertEqual(self.get("/api/scan/results?kind=rename&dat=_unmatched")["total"], 1)
        self.assertEqual(self.get("/api/scan/results?kind=m3u")["total"], 2)
        status, _, _ = self.request("GET", "/api/scan/results?kind=bogus")
        self.assertEqual(status, 400)

    def test_folder_remembered_per_platform(self) -> None:
        self.scan()
        other = self.tmp / "atari"
        other.mkdir()
        self.dat_paths["Atari ST - Games"] = self.dats_dir / "Atari ST - Games (TOSEC-v2025-01-01).dat"
        self.dat_paths["Atari ST - Games"].write_text("<datafile/>")
        self.post("/api/scan", {"path": str(other), "platform": "Atari ST"})
        self.assertEqual(self.wait_job()["status"], "done")
        status = self.get("/api/status")
        self.assertEqual(status["folders"], {"Commodore Amiga": str(self.roms), "Atari ST": str(other.resolve())})
        self.assertEqual(status["last_platform"], "Atari ST")
        # An empty platform means "last used"
        self.post("/api/scan", {"path": str(self.roms)})
        self.assertEqual(self.wait_job()["status"], "done")
        self.assertEqual(self.get("/api/status")["scan"]["platform"], "Atari ST")

    def test_organise_plan_apply_undo(self) -> None:
        self.scan()
        plan = self.post("/api/organise/plan", {})
        self.assertEqual(plan["counts"], {"ok": 1, "conflict": 1, "move": 3})
        self.assertEqual(plan["actionable"], 3)
        self.assertEqual(plan["to_unmatched"], 1)
        self.assertEqual(plan["by_dest"], {WB: 1, "_unmatched": 1, FIRMWARE: 1})
        # grouped by status: moves first, then conflicts, ..., ok last
        self.assertEqual([i["status"] for i in plan["items"]], ["move", "move", "move", "conflict", "ok"])
        first = plan["items"][0]
        self.assertEqual((first["from"], first["to"]), ("wb13.adf", f"{WB}/Workbench v1.3 (1988)(Commodore).adf"))
        self.assertEqual(first["dest"], WB)
        self.assertEqual(plan["items"][1]["to"], "_unmatched/junk.bin")
        only = self.post("/api/organise/plan", {"status": "conflict"})
        self.assertEqual((only["total"], only["all"]), (1, 5))
        self.assertEqual(only["items"][0]["from"], "sub/pack.zip")
        dest = self.post("/api/organise/plan", {"dest": "_unmatched"})
        self.assertEqual([i["from"] for i in dest["items"]], ["junk.bin"])

        scans_before = len(self.calls["scan"])
        self.post("/api/organise/apply", {})
        job = self.wait_job()
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["moved"], 3)
        self.assertEqual(job["result"]["undo_log"], str(self.undo_log))
        self.assertIn("summary", job["result"])
        self.assertEqual(len(self.calls["apply"]), 1)
        self.assertEqual(len(self.calls["scan"]), scans_before + 1)  # automatic re-scan

        logs = self.get("/api/organise/undo-logs")["logs"]
        self.assertEqual(logs[0]["name"], self.undo_log.name)
        self.assertEqual(logs[0]["count"], 2)  # dict-style log {"moves": [...]}

        status, _, _ = self.request("POST", "/api/organise/undo", {"log": str(self.roms / "other.json")})
        self.assertEqual(status, 400)
        self.post("/api/organise/undo", {"log": logs[0]["log"]})
        job = self.wait_job()
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(self.calls["undo"], [self.undo_log])
        self.assertEqual(job["result"]["restored"], 3)

    def test_m3u_plan_and_apply(self) -> None:
        self.scan()
        plan = self.post("/api/m3u/plan", {"savedisk": True, "labels": True})
        self.assertEqual(plan["counts"], {"write": 1, "incomplete": 1})
        self.assertEqual(plan["m3u_dats"], list(AMIGA.m3u_dats))
        self.assertEqual(self.calls["m3u_plan"][-1], list(AMIGA.m3u_dats))  # passed through when accepted
        first = plan["items"][0]
        self.assertEqual(first["name"], "Game (1990)(Pub).m3u")
        self.assertEqual(first["dir"], GAMES)
        self.assertEqual(first["disks"], 2)
        self.assertEqual(first["lines"][-1], "#SAVEDISK:")
        self.assertEqual(plan["items"][1]["reason"], "missing disk 2")
        self.post("/api/m3u/apply", {"savedisk": False})
        job = self.wait_job()
        self.assertEqual(job["kind"], "m3u")
        self.assertEqual(job["result"], {"written": 1})
        self.assertNotIn("#SAVEDISK:", self.calls["write"][0][0].lines)

    def test_kickstart_dirs(self) -> None:
        data = self.get("/api/kickstart/dirs")
        self.assertEqual([d["label"] for d in data["dirs"]], ["EmuDeck", "RetroDECK"])  # existing first
        self.assertTrue(data["dirs"][0]["exists"])
        self.assertIsNone(data["last"])
        self.assertEqual(data["kickstart_dat"], FIRMWARE)
        self.assertIsNone(self.get(f"/api/kickstart/dirs?platform={self.q('Atari ST')}")["kickstart_dat"])

    def test_kickstart_plan_and_apply(self) -> None:
        self.scan()
        plan = self.post("/api/kickstart/plan", {"dest": str(self.bios)})
        self.assertEqual(plan["counts"], {"missing": 1, "copy": 1})
        self.assertEqual(plan["dest"], str(self.bios.resolve()))
        self.assertTrue(plan["dest_exists"])
        first = plan["items"][0]  # copy sorts before missing
        self.assertEqual(first, {"file": "kick34005.A500", "target": str(self.bios.resolve() / "kick34005.A500"),
                                 "source": "kick13.rom", "status": "copy", "reason": "",
                                 "description": "Kickstart v1.3 r34.5 (A500)", "rom_name": ""})
        self.assertIsNone(plan["items"][1]["source"])
        self.assertEqual(self.post("/api/kickstart/plan", {"dest": str(self.bios), "status": "missing"})["total"], 1)

        self.post("/api/kickstart/apply", {"dest": str(self.bios)})
        job = self.wait_job()
        self.assertEqual(job["kind"], "kickstart")
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["copied"], 1)
        self.assertEqual(job["result"]["counts"], {"ok": 1, "missing": 1})
        self.assertTrue((self.bios / "kick34005.A500").is_file())
        self.assertEqual(self.get("/api/status")["kickstart_dest"], str(self.bios.resolve()))
        self.assertEqual(self.get("/api/kickstart/dirs")["last"], str(self.bios.resolve()))

    def test_kickstart_new_dest_created(self) -> None:
        self.scan()
        dest = self.tmp / "newbios"
        plan = self.post("/api/kickstart/plan", {"dest": str(dest)})
        self.assertFalse(plan["dest_exists"])
        self.post("/api/kickstart/apply", {"dest": str(dest)})
        self.assertEqual(self.wait_job()["status"], "done")
        self.assertTrue((dest / "kick34005.A500").is_file())

    def test_kickstart_dest_validation(self) -> None:
        self.scan()
        for bad in ("", "relative/bios", str(self.roms / "file.txt"), str(self.tmp / "a" / "b" / "c")):
            status, body, _ = self.request("POST", "/api/kickstart/plan", {"dest": bad})
            self.assertEqual(status, 400, bad)
            status, _, _ = self.request("POST", "/api/kickstart/apply", {"dest": bad})
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.calls["kick_apply"], [])


class JobTests(ServerTestCase):
    def test_single_job_and_cancel(self) -> None:
        self.scan_gate = threading.Event()
        self.post("/api/scan", {"path": str(self.roms), "platform": ""})
        status, body, _ = self.request("POST", "/api/scan", {"path": str(self.roms)})
        self.assertEqual(status, 409)
        self.assertIn("already running", body["error"])
        running = self.get("/api/job")
        self.assertEqual((running["kind"], running["status"]), ("scan", "running"))
        self.assertTrue(self.post("/api/job/cancel")["cancelled"])
        job = self.wait_job()
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(self.get("/api/status")["scan"])  # cancelled scans are not stored

    def test_job_error_reported(self) -> None:
        self.updates.ensure_error = OSError("network down")
        with mock.patch("traceback.print_exc"):
            self.post("/api/scan", {"path": str(self.roms)})
            job = self.wait_job()
        self.assertEqual(job["status"], "error")
        self.assertIn("network down", job["error"])
        self.assertIsNone(job["error_code"])

    def test_report_parsing(self) -> None:
        job = server.Job(1, "scan")
        job.report(3, 10, "file.adf")
        self.assertEqual(job.progress, {"done": 3, "total": 10, "message": "file.adf"})
        job.report("Extracting")
        self.assertEqual(job.progress["message"], "Extracting")
        job.report(5, 10, Path("/x/y.adf"))
        self.assertEqual(job.progress, {"done": 5, "total": 10, "message": "y.adf"})
        self.assertTrue(callable(job.cancel) and not job.cancel())
        job.cancel.set()
        self.assertTrue(job.cancel())


class SortRowsTests(unittest.TestCase):
    ROWS = [({"name": n, "rating": r, "votes": v}, "") for n, r, v in
            [("b", 7.0, 10), ("a", None, None), ("c", 9.0, 5), ("d", 7.0, 50), ("e", None, None)]]

    def order(self, sort: str) -> str:
        return "".join(r[0]["name"] for r in server._sort_rows(self.ROWS, sort, lambda i: i["name"]))

    def test_rating_both_ways_keeps_unrated_last(self) -> None:
        self.assertEqual(self.order("rating_desc"), "cdbae")
        self.assertEqual(self.order("rating"), "cdbae")
        self.assertEqual(self.order("rating_asc"), "bdcae")

    def test_year_and_size_sorts_put_the_unknown_last(self) -> None:
        rows = [({"name": n, "year": y, "size": z}, "") for n, y, z in
                [("a", 1992, 30), ("b", None, None), ("c", 1990, 10), ("d", 1991, None), ("e", None, 20)]]
        order = lambda sort: "".join(r[0]["name"] for r in server._sort_rows(rows, sort, lambda i: i["name"]))  # noqa: E731
        self.assertEqual(order("year_asc"), "cdabe")
        self.assertEqual(order("year_desc"), "adcbe")
        self.assertEqual(order("size_desc"), "aecbd")
        self.assertEqual(order("size_asc"), "cb"[0] + "e" + "a" + "bd")

    def test_name_both_ways_and_unknown_keeps_order(self) -> None:
        self.assertEqual(self.order("name_asc"), "abcde")
        self.assertEqual(self.order("name_desc"), "edcba")
        self.assertEqual(self.order(""), "bacde")


class HelperTests(unittest.TestCase):
    def test_call_drops_unknown_kwargs(self) -> None:
        def old(a: int, labels: bool = True) -> tuple[int, bool]:
            return a, labels

        def new(a: int, **kw: Any) -> dict[str, Any]:
            return kw

        self.assertEqual(server._call(old, 1, labels=False, m3u_dats=["x"]), (1, False))
        self.assertEqual(server._call(new, 1, m3u_dats=["x"]), {"m3u_dats": ["x"]})

    def test_primary_fallback_uses_dat_priority(self) -> None:
        low = Rom("a", 1, "1", dat="B")
        high = Rom("b", 1, "1", dat="A")
        match = types.SimpleNamespace(entry=None, roms=[low, high])  # no .primary attribute
        self.assertEqual(server._primary(match, ["A", "B"]), [high])
        self.assertEqual(server._primary(Match(None, [low, high], [low]), ["A", "B"]), [low])  # type: ignore[arg-type]

    def test_undo_log_count(self) -> None:
        self.assertEqual(server._undo_log_count([1, 2]), 2)
        self.assertEqual(server._undo_log_count({"moves": [1], "removed_dirs": ["x"]}), 1)
        self.assertIsNone(server._undo_log_count({"x": 1}))


class RealModulesIntegrationTests(unittest.TestCase):
    """End-to-end over HTTP with the real core modules and synthetic DATs / files."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-integ-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data")})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.tmp / "amiga"
        (self.root / "incoming" / "deep").mkdir(parents=True)
        self.bios = self.tmp / "bios"
        self.bios.mkdir()
        rng = random.Random(1234)
        dats_dir = self.tmp / "data" / "dats"
        dats_dir.mkdir(parents=True)

        def blob(size: int) -> bytes:
            return bytes(rng.getrandbits(8) for _ in range(size))

        self.files: dict[str, bytes] = {}
        entries: dict[str, list[tuple[str, bytes]]] = {GAMES: [], WB: [], FIRMWARE: []}
        for disk in (1, 2):
            data = blob(2048)
            entries[GAMES].append((f"Game (1990)(Pub)(Disk {disk} of 2)", data))
            self.files[f"incoming/game_d{disk}.adf"] = data
        entries[GAMES].append(("Missing Game (1991)(Pub)", blob(2048)))  # not present locally
        wb = blob(1024)
        entries[WB].append(("Workbench v1.3 (1988)(Commodore)", wb))
        self.files["incoming/deep/wb.adf"] = wb
        kick = blob(512)
        entries[FIRMWARE].append(("Kickstart v1.3 r34.5 (1987)(Commodore)(A500)", kick))
        self.files["kick.rom"] = kick
        self.files["incoming/readme.txt"] = b"not a rom"
        for rel, data in self.files.items():
            (self.root / rel).write_bytes(data)

        versions = {GAMES: "2025-01-30", WB: "2023-05-21", FIRMWARE: "2025-01-03"}
        for dat_name, games in entries.items():
            ext = ".rom" if dat_name == FIRMWARE else ".adf"
            lines = ['<?xml version="1.0"?>', "<datafile>",
                     f"<header><name>{dat_name}</name><description>{dat_name}</description>"
                     f"<version>{versions[dat_name]}</version></header>"]
            for game, data in games:
                # The firmware entry claims the PUAE Kickstart 1.3 md5 (the DAT md5 is what
                # kickstart matching uses); the real bytes differ, so copying must refuse.
                md5 = "82a21c1890cae844b3df741f2762d48d" if dat_name == FIRMWARE else hashlib.md5(data).hexdigest()
                lines.append(f'<game name="{game}"><description>{game}</description>'
                             f'<rom name="{game}{ext}" size="{len(data)}" crc="{zlib.crc32(data):08x}" '
                             f'md5="{md5}" sha1="{hashlib.sha1(data).hexdigest()}"/></game>')
            lines.append("</datafile>")
            (dats_dir / f"{dat_name} (TOSEC-v{versions[dat_name]}_CM).dat").write_text("\n".join(lines))

        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.port = self.srv.port
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers.update({"Content-Type": "application/json", "X-Romorg-Token": "t"})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            self.fail(f"{path}: {err.code} {err.read()!r}")

    def job(self, path: str, body: Any) -> dict[str, Any]:
        self.call("POST", path, body)
        deadline = time.time() + 10
        while time.time() < deadline:
            job = self.call("GET", "/api/job")
            if job["status"] != "running":
                self.assertEqual(job["status"], "done", job)
                return job
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_full_workflow(self) -> None:
        plats = self.call("GET", "/api/platforms")
        amiga = next(p for p in plats if p["name"] == "Commodore Amiga")
        self.assertEqual([d["present"] for d in amiga["dats"]], [True, True, False, True])

        summary = self.job("/api/scan", {"path": str(self.root), "platform": "Commodore Amiga"})["result"]
        self.assertEqual(summary["missing_dats"], [KICKDISKS])
        self.assertEqual(summary["matched_files"], 4)
        self.assertEqual(summary["per_dat"][GAMES]["have"], 2)
        self.assertEqual(summary["per_dat"][GAMES]["missing"], 1)
        matched = self.call("GET", f"/api/scan/results?kind=matched&dat={urllib.request.quote(WB)}")
        self.assertEqual([i["file"] for i in matched["items"]], ["incoming/deep/wb.adf"])

        plan = self.call("POST", "/api/organise/plan", {"limit": 100})
        self.assertEqual(plan["actionable"], 5, plan)
        self.assertEqual(plan["to_unmatched"], 1)
        moves = {i["from"]: i["to"] for i in plan["items"]}
        self.assertEqual(moves["incoming/deep/wb.adf"], f"{WB}/Workbench v1.3 (1988)(Commodore).adf")
        self.assertEqual(moves["incoming/readme.txt"], "_unmatched/incoming/readme.txt")

        result = self.job("/api/organise/apply", {})["result"]
        self.assertFalse(result.get("failed"), result)
        self.assertTrue((self.root / GAMES / "Game (1990)(Pub)(Disk 1 of 2).adf").is_file())
        self.assertTrue((self.root / FIRMWARE / "Kickstart v1.3 r34.5 (1987)(Commodore)(A500).rom").is_file())
        self.assertTrue((self.root / "_unmatched" / "incoming" / "readme.txt").is_file())
        self.assertFalse((self.root / "incoming").exists())  # emptied by our moves -> removed
        self.assertEqual(result["summary"]["matched_files"], 4)  # re-scanned
        replan = self.call("POST", "/api/organise/plan", {})
        self.assertEqual(replan["actionable"], 0, replan)

        m3u = self.call("POST", "/api/m3u/plan", {"labels": True})
        self.assertEqual(m3u["counts"], {"write": 1})
        self.assertEqual(m3u["items"][0]["dir"], GAMES)
        self.job("/api/m3u/apply", {"labels": True})
        self.assertTrue((self.root / GAMES / "Game (1990)(Pub).m3u").is_file())

        kick = self.call("POST", "/api/kickstart/plan", {"dest": str(self.bios), "status": "copy"})
        self.assertEqual([i["file"] for i in kick["items"]], ["kick34005.A500"])
        self.assertEqual(kick["items"][0]["source"], f"{FIRMWARE}/Kickstart v1.3 r34.5 (1987)(Commodore)(A500).rom")
        res = self.job("/api/kickstart/apply", {"dest": str(self.bios)})["result"]
        self.assertEqual(res["copied"], 0)  # fake bytes fail the md5 check: never copied blindly
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(list(self.bios.iterdir()), [])

        logs = self.call("GET", "/api/organise/undo-logs")["logs"]
        self.assertEqual(len(logs), 1)
        self.job("/api/organise/undo", {"log": logs[0]["log"]})
        for rel, data in self.files.items():
            self.assertEqual((self.root / rel).read_bytes(), data, rel)


class ReviewFixTests(ServerTestCase):
    @unittest.skipUnless(os.name == "posix", "undecodable (bytes) file names only exist on POSIX")
    def test_undecodable_filenames_serialise(self) -> None:
        bad = os.fsdecode(b"bad\xffname.adf")
        self.result.unmatched.append(Entry(self.roms / bad, None, 1, "00000000"))
        self.result.errors.append((self.roms / bad, "x"))
        self.scan()
        for kind in ("unmatched", "errors", "rename"):
            page = self.get(f"/api/scan/results?kind={kind}")
            self.assertEqual(page["kind"], kind)
        items = self.get("/api/scan/results?kind=unmatched")["items"]
        self.assertIn(bad, [i["file"] for i in items])  # round-trips losslessly
        self.post("/api/organise/plan", {})
        if sys.platform.startswith("linux"):
            try:
                (self.roms / bad).mkdir()
            except OSError:
                return
            dirs = self.get(f"/api/fs/list?path={self.q(str(self.roms))}")["dirs"]
            self.assertIn(bad, [d["name"] for d in dirs])

    def test_plan_passes_missing_dats_and_ignores_move_unmatched(self) -> None:
        seen: list[dict[str, Any]] = []
        organiser = sys.modules["romorg.organiser"]
        old = organiser.plan_renames

        def plan(result: Any, missing_dats: Any = ()) -> list[RenameOp]:
            seen.append({"missing": list(missing_dats)})
            return old(result)
        organiser.plan_renames = plan
        self.scan()
        a = self.post("/api/organise/plan", {})
        b = self.post("/api/organise/plan", {"move_unmatched": False})   # stale client: accepted and ignored
        self.assertEqual(seen, [{"missing": [KICKDISKS]}])               # the second plan is the cached first
        self.assertNotIn("move_unmatched", a)
        self.assertEqual(a["actionable"], b["actionable"])
        self.assertEqual(a["missing_dats"], [KICKDISKS])
        self.assertEqual(a["warnings"], [])

    def test_plan_warns_about_other_systems(self) -> None:
        organiser = sys.modules["romorg.organiser"]
        root = self.roms
        organiser.plan_renames = lambda result: [
            RenameOp(root / sys_ / f"g{i}.bin", root / "_unmatched" / sys_ / f"g{i}.bin", "move")
            for sys_ in ("snes", "psx") for i in range(40)]
        self.scan()
        warnings = self.post("/api/organise/plan", {})["warnings"]
        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("snes", warnings[1])

    def test_scan_refuses_too_broad_folders(self) -> None:
        for path in (Path.home().anchor or "/", str(Path.home())):
            status, body, _ = self.request("POST", "/api/scan", {"path": path, "platform": "Commodore Amiga"})
            self.assertEqual(status, 400, (path, body))
            self.assertIn("platform folder", body["error"])
        status, _, _ = self.request("POST", "/api/scan", {"path": str(self.data), "platform": "Commodore Amiga"})
        self.assertEqual(status, 400)

    def test_rescan_failure_keeps_apply_result(self) -> None:
        self.scan()
        scanner = sys.modules["romorg.scanner"]

        def broken(*a: Any, **k: Any) -> Any:
            raise NotADirectoryError("folder vanished")
        scanner.scan = broken
        with mock.patch("traceback.print_exc"):
            self.post("/api/organise/apply", {})
            job = self.wait_job()
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["undo_log"], str(self.undo_log))
        self.assertIn("vanished", job["result"]["rescan_error"])
        self.assertEqual(job["result"]["action"], "apply")

    def test_fs_pick_timeout_and_game_mode(self) -> None:
        with mock.patch.object(server, "_dialog_command", return_value="kdialog"), \
                mock.patch.object(server.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired(["kdialog"], 300)) as run:
            self.assertEqual(self.post("/api/fs/pick", {}), {"cancelled": True, "timeout": True})
        self.assertEqual(run.call_args.kwargs["timeout"], server.DIALOG_TIMEOUT)
        if os.name == "nt":  # the Windows dialog is always available; there is no Steam game mode
            return
        with mock.patch.dict(os.environ, {"DISPLAY": ":0", "SteamGamepadUI": "1"}), \
                mock.patch.object(server.shutil, "which", return_value="/usr/bin/kdialog"):
            self.assertIsNone(server._dialog_command())

    def test_stalled_request_body_times_out(self) -> None:
        self.assertEqual(server.Handler.timeout, 30)
        with mock.patch.object(server.Handler, "timeout", 0.3):
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
                sock.sendall(b"POST /api/job/cancel HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                             b"X-Romorg-Token: test-token\r\nContent-Length: 100\r\n\r\n{}" % self.port)
                t = time.time()
                try:
                    sock.recv(1000)
                except OSError:
                    pass
                self.assertLess(time.time() - t, 4)

    def test_stop_jobs_waits_for_running_job(self) -> None:
        app = self.srv.app
        stopped = threading.Event()

        def work(job: Any) -> Any:
            while not job.cancel.is_set():
                time.sleep(0.01)
            stopped.set()
            return {"moved": 1}
        app.jobs.start("organise", work, cancellable=False)
        self.assertTrue(app.stop_jobs(5))
        self.assertTrue(stopped.is_set())


SNES_DAT = "Nintendo - Super Nintendo Entertainment System"
SNES = "Super Nintendo Entertainment System"


@dataclass(frozen=True)
class NRom:
    name: str
    size: int
    crc: str
    md5: str = ""
    sha1: str = ""
    game: str = ""
    dat: str = ""
    set_name: str = ""


@dataclass
class NMatch:
    entry: Entry
    roms: list[Any]
    primary: list[Any] = field(default_factory=list)
    matched_via: str = "raw"
    header: int = 0
    byte_order: str = ""


@dataclass
class NRenameOp(RenameOp):
    superseded_by: str = ""


@dataclass
class ConvertOp:
    src: Path
    member: str | None
    dst: Path
    original_dst: Path
    status: str
    reason: str = ""
    rom_name: str = ""
    via: str = ""


@dataclass(frozen=True)
class NPlatform:
    name: str
    dats: tuple[str, ...]
    m3u_dats: tuple[str, ...]
    kickstart_dat: str | None
    source: str = "tosec"
    layout: str = "per_dat"
    extensions: tuple[str, ...] = ()
    alt_hashes: tuple[str, ...] = ()
    convertible: bool = False
    folder_hint: str = ""


SNES_PLATFORM = NPlatform(SNES, (SNES_DAT,), (), None, source="nointro", layout="flat",
                          extensions=(".sfc", ".smc", ".zip"), alt_hashes=("snes_header",), convertible=True,
                          folder_hint="snes")


def fake_tags(name: str, style: str = "nointro") -> dict[str, Any]:
    """Tiny stand-in for tags.parse_name: (USA)/(Europe)/(Japan), (En,Fr), (Rev N), (Beta), [b]."""
    import re
    groups = re.findall(r"\(([^)]*)\)", name)
    regions = [g for g in groups if g in ("USA", "Europe", "Japan", "World")]
    langs = [p for g in groups if re.fullmatch(r"[A-Z][a-z](,[A-Z][a-z])*", g) for p in g.split(",")]
    video = sorted({"PAL" if r == "Europe" else "NTSC" for r in regions if r != "World"}
                   | ({"NTSC", "PAL"} if "World" in regions else set()))
    status = next((g for g in groups if g.startswith("Beta")), "")
    version = next((g for g in groups if g.startswith("Rev")), "")
    flags = [g for g in groups if g not in regions and g != status and g != version
             and not re.fullmatch(r"[A-Z][a-z](,[A-Z][a-z])*", g)]
    return {"regions": regions, "languages": langs or (["En"] if "USA" in regions else []),
            "languages_implied": not langs, "version": version, "status": status, "flags": flags,
            "dump_flags": ["b"] if "[b]" in name else [], "bad": "[b]" in name, "bios": False, "video": video}


class NoIntroServerTests(ServerTestCase):
    """AMENDMENT 4: per-console folders, No-Intro DATs, games view, tags, latest-only, convert."""

    def setUp(self) -> None:
        super().setUp()
        test = self
        self.snes_root = (self.tmp / "snes").resolve()
        self.snes_root.mkdir()
        self.nointro_dir = self.data / "nointro"
        self.nointro_dir.mkdir()
        self.nointro_present = {SNES_DAT: "2026.08.01"}
        self.calls.update({"nointro_update": [], "snes_scan": [], "plan_kw": [], "convert_plan": [],
                           "convert_apply": []})

        platforms = sys.modules["romorg.platforms"]
        platforms.PLATFORMS[SNES] = SNES_PLATFORM
        platforms.list_platforms = lambda: [AMIGA, OTHER, SNES_PLATFORM]

        def locate_dats(platform: Any, directory: Any = None, nointro_directory: Any = None) -> dict[str, Any]:
            if getattr(platform, "source", "tosec") == "nointro":
                return {n: DatInfo(n, v, self.nointro_dir / f"{n}.dat")
                        for n, v in test.nointro_present.items() if n in platform.dats}
            return {n: DatInfo(n, "2025-01-01", p) for n, p in test.dat_paths.items() if n in platform.dats}

        platforms.locate_dats = locate_dats
        old_load = platforms.load_platform_dats

        def load_platform_dats(platform: Any, directory: Any = None, nointro_directory: Any = None) -> Any:
            if getattr(platform, "source", "tosec") != "nointro":
                return old_load(platform, directory)
            present = [n for n in platform.dats if n in test.nointro_present]
            return ([types.SimpleNamespace(name=n) for n in present],
                    [n for n in platform.dats if n not in test.nointro_present])

        platforms.load_platform_dats = load_platform_dats

        nointro = types.ModuleType("romorg.nointro")
        nointro.NOINTRO_DATS = (SNES_DAT, "Nintendo - Game Boy Advance")
        nointro.list_dats = lambda directory=None: [DatInfo(n, v, self.nointro_dir / f"{n}.dat")
                                                    for n, v in test.nointro_present.items()]

        def update_dats(names: Any = None, progress: Any = None, cancel: Any = None, force: bool = False,
                        opener: Any = None) -> dict[str, Any]:
            test.calls["nointro_update"].append({"names": names, "force": force})
            if progress:
                progress(1, 2, "Downloading x")
            return {"source": "nointro", "dats": [{"name": SNES_DAT, "version": "2026.08.01", "status": "unchanged"}],
                    "downloaded": 0, "unchanged": 1, "failed": 0, "count": 1}

        nointro.update_dats = update_dats

        tags = types.ModuleType("romorg.tags")
        tags.parse_name = fake_tags
        tags.to_json = lambda t: t

        paths_mod = sys.modules.get("romorg.paths")
        self.assertIsNotNone(paths_mod)

        root = self.snes_root
        self.r_usa = NRom("Mario (USA).sfc", 8, "00000011", game="Mario (USA)", dat=SNES_DAT, set_name="Mario (USA)")
        self.r_usa1 = NRom("Mario (USA) (Rev 1).sfc", 8, "00000012", game="Mario (USA) (Rev 1)", dat=SNES_DAT,
                           set_name="Mario (USA) (Rev 1)")
        self.r_eur = NRom("Zelda (Europe) (En,Fr).sfc", 8, "00000013", game="Zelda (Europe) (En,Fr)", dat=SNES_DAT,
                          set_name="Zelda (Europe) (En,Fr)")
        self.r_beta = NRom("Kirby (Japan) (Beta).sfc", 8, "00000014", game="Kirby (Japan) (Beta)", dat=SNES_DAT,
                           set_name="Kirby (Japan) (Beta)")
        self.snes_result = ScanResult(
            root=root, dat_names=[SNES_DAT],
            matched=[
                NMatch(Entry(root / "mario.sfc", None, 8, "00000011"), [self.r_usa], [self.r_usa]),
                NMatch(Entry(root / "sub" / "mario r1.smc", None, 520, "deadbeef"), [self.r_usa1], [self.r_usa1],
                       matched_via="headerless", header=512),
                NMatch(Entry(root / "Zelda (Europe) (En,Fr).sfc", None, 8, "00000013"), [self.r_eur], [self.r_eur]),
            ],
            unmatched=[Entry(root / "junk.txt", None, 1, "0")],
            missing=[self.r_beta],
        )
        self.snes_result.layout = "flat"  # type: ignore[attr-defined]

        scanner = sys.modules["romorg.scanner"]
        old_scan = scanner.scan

        def scan(root_: Path, dats: Any, recursive: bool = True, progress: Any = None, cancel: Any = None,
                 alt_hashes: Any = (), layout: str = "per_dat") -> Any:
            if Path(root_) == test.snes_root:
                test.calls["snes_scan"].append({"alt_hashes": tuple(alt_hashes), "layout": layout,
                                                "dats": [d.name for d in dats]})
                return test.snes_result
            return old_scan(root_, dats, recursive, progress, cancel)

        scanner.scan = scan

        def game_status(result: Any) -> list[dict[str, Any]]:
            rows = []
            for rom in (self.r_usa, self.r_usa1, self.r_eur, self.r_beta):
                files = [m.entry.path for m in result.matched if rom in m.roms]
                rows.append({"dat": SNES_DAT, "name": rom.set_name, "have": bool(files), "roms": [rom.name],
                             "files": files, "rom": rom})
            return rows

        scanner.game_status = game_status

        organiser = sys.modules["romorg.organiser"]
        old_plan = organiser.plan_renames

        def plan_renames(result: Any, missing_dats: Any = (),
                         latest_only: bool = False, layout: Any = None) -> list[Any]:
            if result is not test.snes_result:
                return old_plan(result)
            test.calls["plan_kw"].append({"latest_only": latest_only, "layout": layout})
            ops = [NRenameOp(root / "mario.sfc", root / self.r_usa.name, "move", rom_name=self.r_usa.name),
                   NRenameOp(root / "sub" / "mario r1.smc", root / "Mario (USA) (Rev 1).smc", "move",
                             rom_name=self.r_usa1.name),
                   NRenameOp(root / self.r_eur.name, root / self.r_eur.name, "ok", rom_name=self.r_eur.name),
                   NRenameOp(root / "junk.txt", root / "_unmatched" / "junk.txt", "move")]
            if latest_only:
                ops[0] = NRenameOp(root / "mario.sfc", root / "_superseded" / self.r_usa.name, "move",
                                   reason="older version - superseded by Mario (USA) (Rev 1).sfc",
                                   rom_name=self.r_usa.name, superseded_by=self.r_usa1.name)
            return ops

        organiser.plan_renames = plan_renames
        organiser.target_filename = lambda rom, src, archive_ext=None, matched_via="raw", byte_order="": (
            rom.name if matched_via == "raw" else rom.set_name + ".smc")

        convert = types.ModuleType("romorg.convert")

        def plan_conversions(result: Any, layout: Any = None, latest_only: bool = False) -> list[ConvertOp]:
            test.calls["convert_plan"].append({"layout": layout, "latest_only": latest_only})
            return [
                ConvertOp(root / "zip.zip", "a.smc", root / "zip.zip", root / "_unmatched" / "x", "skip",
                          "extract the archive first"),
                ConvertOp(root / "sub" / "mario r1.smc", None, root / self.r_usa1.name,
                          root / "_converted_originals" / "sub" / "mario r1.smc", "convert",
                          rom_name=self.r_usa1.name, via="headerless"),
            ]

        def apply_conversions(ops: list[ConvertOp], root_: Path, progress: Any = None, cancel: Any = None) -> dict:
            test.calls["convert_apply"].append([op.status for op in ops])
            return {"converted": 1, "failed": [], "removed_dirs": [], "undo_log": test.undo_log,
                    "cancelled": False, "error": None}

        convert.plan_conversions = plan_conversions
        convert.apply_conversions = apply_conversions
        convert.convert_counts = lambda ops: _count(ops)

        sys.modules.update({"romorg.nointro": nointro, "romorg.tags": tags, "romorg.convert": convert})
        for name in ("romorg.nointro", "romorg.tags", "romorg.convert"):
            self.addCleanup(sys.modules.pop, name, None)

    def scan_snes(self) -> dict[str, Any]:
        self.post("/api/scan", {"path": str(self.snes_root), "platform": SNES})
        job = self.wait_job()
        self.assertEqual(job["status"], "done", job)
        return job

    def test_platforms_and_status(self) -> None:
        plats = {p["name"]: p for p in self.get("/api/platforms")}
        snes = plats[SNES]
        self.assertEqual((snes["source"], snes["layout"], snes["convertible"], snes["folder_hint"]),
                         ("nointro", "flat", True, "snes"))
        self.assertEqual(snes["extensions"], [".sfc", ".smc", ".zip"])
        self.assertEqual(snes["dats"][0]["source"], "nointro")
        self.assertEqual(snes["dats"][0]["version"], "2026.08.01")
        self.assertEqual(snes["dats"][0]["folder"], "")  # flat: everything in the console folder
        self.assertTrue(snes["complete"])
        self.assertEqual((snes["m3u_dats"], snes["kickstart_dat"]), ([], None))
        self.assertEqual(plats["Commodore Amiga"]["dats"][0]["present"], True)

        status = self.get("/api/status")
        nointro = status["nointro"]
        self.assertEqual(nointro["count"], 1)
        self.assertEqual(nointro["dats"], [{"name": SNES_DAT, "version": "2026.08.01", "present": True},
                                           {"name": "Nintendo - Game Boy Advance", "version": None,
                                            "present": False}])
        self.assertEqual(status["dats_count"], 3)  # TOSEC count unchanged

    def test_folders_endpoint(self) -> None:
        res = self.post("/api/folders", {"platform": SNES, "path": str(self.snes_root)})
        self.assertEqual(res["folders"], {SNES: str(self.snes_root)})
        self.post("/api/folders", {"platform": "Commodore Amiga", "path": str(self.roms)})
        plats = {p["name"]: p for p in self.get("/api/platforms")}
        self.assertEqual(plats[SNES]["folder"], str(self.snes_root))
        self.assertEqual(self.get("/api/status")["folders"], {SNES: str(self.snes_root),
                                                              "Commodore Amiga": str(self.roms)})
        res = self.post("/api/folders", {"platform": SNES, "path": ""})  # forget
        self.assertEqual(res["folders"], {"Commodore Amiga": str(self.roms)})
        for body in ({"platform": SNES, "path": str(self.snes_root / "nope")},
                     {"platform": SNES, "path": "relative"},
                     {"platform": SNES, "path": "/"},
                     {"platform": "Sega Saturn", "path": str(self.snes_root)}):
            status, _, _ = self.request("POST", "/api/folders", body)
            self.assertEqual(status, 400, body)

    def test_scan_without_nointro_dats_waits_for_update(self) -> None:
        self.nointro_present.clear()
        with mock.patch("traceback.print_exc"):
            self.post("/api/scan", {"path": str(self.snes_root), "platform": SNES})
            job = self.wait_job()
        # the update manager was asked to install the console's DAT first (progress goes to the job)
        self.assertEqual(self.updates.calls[-1], ("ensure", SNES))
        self.assertEqual((job["status"], job["error_code"]), ("error", "no_dats"))
        self.assertIn(SNES, job["error"])

    def test_scan_games_tags_and_filters(self) -> None:
        self.scan_snes()
        self.assertEqual(self.calls["snes_scan"], [{"alt_hashes": ("snes_header",), "layout": "flat",
                                                    "dats": [SNES_DAT]}])
        self.assertEqual(self.get("/api/status")["scan"]["layout"], "flat")
        matched = self.get("/api/scan/results?kind=matched")
        by_file = {i["file"]: i for i in matched["items"]}
        mario = by_file["mario.sfc"]
        self.assertEqual((mario["via"], mario["placed_ok"], mario["named_ok"]), ("raw", True, False))
        self.assertEqual(mario["tags"]["regions"], ["USA"])
        headered = by_file["sub/mario r1.smc"]
        self.assertEqual((headered["via"], headered["header"], headered["placed_ok"]), ("headerless", 512, False))
        zelda = by_file["Zelda (Europe) (En,Fr).sfc"]
        self.assertTrue(zelda["placed_ok"] and zelda["named_ok"])  # flat layout: the root is home
        self.assertEqual(matched["facets"]["regions"], {"USA": 2, "Europe": 1})
        self.assertEqual(matched["facets"]["video"], {"NTSC": 2, "PAL": 1})

        pal = self.get("/api/scan/results?kind=matched&video=PAL")
        self.assertEqual([i["file"] for i in pal["items"]], ["Zelda (Europe) (En,Fr).sfc"])
        self.assertEqual(pal["facets"]["regions"], {"USA": 2, "Europe": 1})  # facets ignore tag filters
        self.assertEqual(self.get("/api/scan/results?kind=matched&language=fr")["total"], 1)
        self.assertEqual(self.get("/api/scan/results?kind=matched&region=USA&language=Fr")["total"], 0)

        games = self.get("/api/scan/results?kind=games")
        self.assertEqual(games["total"], 4)
        self.assertEqual([g["have"] for g in games["items"]], [True, True, True, False])
        self.assertEqual(games["items"][1]["files"], ["sub/mario r1.smc"])
        self.assertEqual(games["items"][3]["tags"]["status"], "Beta")
        self.assertEqual(self.get("/api/scan/results?kind=games&have=0")["items"][0]["name"], "Kirby (Japan) (Beta)")
        self.assertEqual(self.get("/api/scan/results?kind=games&have=1")["total"], 3)
        self.assertEqual(self.get("/api/scan/results?kind=games&flag=beta")["total"], 1)
        self.assertEqual(self.get("/api/scan/results?kind=games&flag=Beta&have=1")["total"], 0)
        self.assertEqual(self.get("/api/scan/results?kind=games&have=1")["facets"]["flags"], {})
        missing = self.get("/api/scan/results?kind=missing")
        self.assertEqual(missing["items"][0]["set_name"], "Kirby (Japan) (Beta)")
        self.assertEqual(missing["items"][0]["tags"]["regions"], ["Japan"])
        self.assertEqual(missing["facets"]["flags"], {"Beta": 1})
        self.assertNotIn("facets", self.get("/api/scan/results?kind=unmatched"))

    def test_games_fallback_without_game_status(self) -> None:
        del sys.modules["romorg.scanner"].game_status
        self.scan_snes()
        games = self.get("/api/scan/results?kind=games")
        self.assertEqual([(g["name"], g["have"]) for g in games["items"]],
                         [("Kirby (Japan) (Beta)", False), ("Mario (USA)", True), ("Mario (USA) (Rev 1)", True),
                          ("Zelda (Europe) (En,Fr)", True)])

    def test_tags_missing_module_is_harmless(self) -> None:
        sys.modules.pop("romorg.tags")
        with mock.patch.object(server, "_optional_mod", side_effect=lambda n: None if n == "tags" else server._mod(n)):
            self.scan_snes()
            item = self.get("/api/scan/results?kind=matched")["items"][0]
        self.assertIsNone(item["tags"])

    def test_platform_options_saved_right_away(self) -> None:
        # ticking "Latest version only" must survive a scan / DAT download before the next preview
        res = self.post("/api/platforms/options", {"platform": SNES, "latest_only": True})
        self.assertEqual(res, {"platform": SNES, "latest_only": True})
        self.assertTrue({p["name"]: p for p in self.get("/api/platforms")}[SNES]["latest_only"])
        self.scan_snes()
        self.assertTrue({p["name"]: p for p in self.get("/api/platforms")}[SNES]["latest_only"])
        self.assertTrue(self.post("/api/organise/plan", {})["latest_only"])
        self.post("/api/platforms/options", {"platform": SNES, "latest_only": False})
        self.assertFalse(self.post("/api/organise/plan", {})["latest_only"])
        for body in ({"platform": SNES}, {"platform": "Sega Saturn", "latest_only": True}):
            status, _, _ = self.request("POST", "/api/platforms/options", body)
            self.assertEqual(status, 400, body)

    def test_flag_facet_leaves_out_free_text(self) -> None:
        def row(flags, dump=(), status=""):
            return ({"tags": {"regions": [], "languages": [], "video": [], "status": status, "flags": list(flags),
                              "dump_flags": list(dump), "bad": "b" in dump, "bios": False}}, "")
        rows = [row(["Ocean", "Disk 1 of 2", "AGA"], ["cr Fairlight", "a2"]),
                row(["Psygnosis", "Disk 2 of 2", "1990-05-01"], ["!"]),
                row(["Unl", "Virtual Console"], ["b"], "Beta")]
        import importlib.util
        spec = importlib.util.spec_from_file_location("_real_tags", Path(server.__file__).with_name("tags.py"))
        real_tags = importlib.util.module_from_spec(spec)
        sys.modules["_real_tags"] = real_tags  # dataclasses look their module up
        self.addCleanup(sys.modules.pop, "_real_tags", None)
        spec.loader.exec_module(real_tags)
        with mock.patch.object(server, "_optional_mod", side_effect=lambda n: real_tags if n == "tags" else None):
            flags = server._facets(rows)["flags"]
        self.assertEqual(set(flags), {"AGA", "[cr]", "[a]", "[!]", "Unl", "Virtual Console", "bad", "Beta"})
        # a filter on a value outside the facet still works
        self.assertEqual(len(server._tag_filter(rows, {"flag": "Ocean"})), 1)
        self.assertEqual(len(server._tag_filter(rows, {"flag": "[cr]"})), 1)

    def test_organise_latest_only_remembered(self) -> None:
        self.scan_snes()
        plan = self.post("/api/organise/plan", {})
        self.assertFalse(plan["latest_only"])
        self.assertEqual(plan["layout"], "flat")
        self.assertEqual(self.calls["plan_kw"][-1], {"latest_only": False, "layout": "flat"})
        self.assertEqual(plan["by_dest"], {"": 2, "_unmatched": 1})
        root_only = self.post("/api/organise/plan", {"dest": "."})  # "." = the console folder itself
        self.assertEqual([i["from"] for i in root_only["items"]],
                         ["mario.sfc", "sub/mario r1.smc", "Zelda (Europe) (En,Fr).sfc"])

        plan = self.post("/api/organise/plan", {"latest_only": True})
        self.assertEqual(self.calls["plan_kw"][-1]["latest_only"], True)
        self.assertTrue(plan["latest_only"])
        self.assertEqual(plan["to_superseded"], 1)
        self.assertEqual(plan["superseded_dir"], "_superseded")
        superseded = [i for i in plan["items"] if i["superseded_by"]]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["dest"], "_superseded")
        self.assertEqual(superseded[0]["superseded_by"], "Mario (USA) (Rev 1).sfc")
        self.assertEqual(plan["warnings"], [])

        # remembered per platform: the next plan / the platforms list default to it
        self.assertTrue({p["name"]: p for p in self.get("/api/platforms")}[SNES]["latest_only"])
        # Amiga keeps its own (default: on) library profile; SNES' choice does not leak into it
        self.assertEqual({p["name"]: p for p in self.get("/api/platforms")}["Commodore Amiga"]["library"]["latest_only"], True)
        self.assertTrue(self.post("/api/organise/plan", {})["latest_only"])
        self.assertEqual(self.get("/api/scan/results?kind=rename")["total"], 4)

        self.post("/api/organise/apply", {})
        job = self.wait_job()
        self.assertEqual(job["status"], "done", job)
        applied = self.calls["apply"][-1]
        self.assertEqual(applied[0].superseded_by, "Mario (USA) (Rev 1).sfc")
        self.assertFalse(self.post("/api/organise/plan", {"latest_only": False})["latest_only"])
        self.assertFalse({p["name"]: p for p in self.get("/api/platforms")}[SNES]["latest_only"])

    def test_convert_plan_apply(self) -> None:
        self.scan_snes()
        plan = self.post("/api/convert/plan", {})
        self.assertTrue(plan["available"])
        self.assertEqual(plan["counts"], {"skip": 1, "convert": 1})
        self.assertEqual(plan["items"][0], {
            "from": "sub/mario r1.smc", "to": "Mario (USA) (Rev 1).sfc",
            "original_to": "_converted_originals/sub/mario r1.smc", "status": "convert",
            "reason": "", "rom_name": "Mario (USA) (Rev 1).sfc", "via": "headerless"})
        self.assertEqual(plan["items"][1]["from"], "zip.zip::a.smc")
        self.assertEqual(self.post("/api/convert/plan", {"status": "skip"})["total"], 1)
        self.assertEqual(self.calls["convert_plan"], [{"layout": "flat", "latest_only": False}])  # cached
        self.post("/api/convert/plan", {"latest_only": True})
        self.assertEqual(self.calls["convert_plan"][-1]["latest_only"], True)

        scans = len(self.calls["snes_scan"])
        self.post("/api/convert/apply", {"latest_only": False})
        job = self.wait_job()
        self.assertEqual((job["kind"], job["status"]), ("convert", "done"), job)
        self.assertTrue(job["cancellable"])
        self.assertEqual(job["result"]["action"], "convert")
        self.assertEqual(job["result"]["converted"], 1)
        self.assertIn("summary", job["result"])
        self.assertEqual(len(self.calls["snes_scan"]), scans + 1)  # re-scanned
        self.assertEqual(self.calls["convert_apply"], [["convert", "skip"]])

    def test_convert_unavailable_for_amiga(self) -> None:
        self.scan()
        plan = self.post("/api/convert/plan", {})
        self.assertEqual((plan["available"], plan["total"], plan["counts"]), (False, 0, {}))
        status, body, _ = self.request("POST", "/api/convert/apply", {})
        self.assertEqual(status, 409)
        self.assertEqual(self.calls["convert_plan"], [])

    def test_kickstart_refused_without_kickstart_dat(self) -> None:
        self.scan_snes()
        for path in ("/api/kickstart/plan", "/api/kickstart/apply"):
            status, body, _ = self.request("POST", path, {"dest": str(self.bios)})
            self.assertEqual(status, 409, path)
            self.assertIn("no Kickstart DAT", body["error"])

    def test_undo_log_count_includes_created_files(self) -> None:
        organiser = sys.modules["romorg.organiser"]
        organiser.read_undo_log = lambda log: {"moves": [1, 2], "created_files": [{"path": "x"}], "steps": []}
        self.scan_snes()
        self.assertEqual(self.get(f"/api/organise/undo-logs?path={self.q(str(self.roms))}")["logs"][0]["count"], 3)

    def test_rescan_skipped_only_when_closing(self) -> None:
        app = self.srv.app
        self.scan_snes()
        state = app._scan
        job = server.Job(99, "convert", cancellable=True)
        job.cancel.set()  # user-cancelled convert: still re-scans
        res = app._rescan_into(job, state, {})
        self.assertIn("summary", res)
        app.closing.set()
        self.assertIn("rescan_error", app._rescan_into(job, state, {}))


def _count(ops: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for op in ops:
        out[op.status] = out.get(op.status, 0) + 1
    return out


@dataclass
class LRenameOp(RenameOp):
    code: str = ""
    keeper: str = ""
    missing: tuple = ()
    flags_text: str = ""
    superseded_by: str = ""


@dataclass
class LIncomplete:
    dat: str
    name: str
    total: int
    present: dict
    missing: tuple


@dataclass
class LPlan:
    ops: list
    playlists: list
    selection: Any
    profile: Any


class LibraryServerTests(ServerTestCase):
    """AMENDMENT 6: library profile, Build library preview / apply / undo, tag rule filters."""

    def setUp(self) -> None:
        super().setUp()
        test = self
        root, games = self.roms, self.roms / GAMES
        self.calls.update({"plan_library": [], "library_apply": []})
        organiser = sys.modules["romorg.organiser"]

        def plan_library(result: Any, profile: Any, missing_dats: Any = (),
                         layout: Any = None, savedisk: bool = False, labels: bool = True,
                         platform: Any = None) -> LPlan:
            test.calls["plan_library"].append({"profile": profile, "savedisk": savedisk, "labels": labels,
                                               "platform": getattr(platform, "name", None)})
            ops = [
                LRenameOp(root / "a.adf", games / "A (1990)(Pub).adf", "rename", kind="rename"),
                LRenameOp(root / "bad.adf", root / "_excluded/bad.adf", "move", code="excluded",
                          flags_text="[b corrupt file]", reason="excluded: bad dump [b corrupt file]"),
                LRenameOp(root / "old.adf", root / "_superseded/old.adf", "move", code="superseded",
                          superseded_by="New (1991)(Pub)"),
                LRenameOp(root / "d2.adf", root / "_incomplete/d2.adf", "move", code="incomplete",
                          missing=(1, 3)),
                LRenameOp(root / "x copy.adf", root / "_duplicates/x copy.adf", "move", code="duplicate",
                          keeper=f"{GAMES}/X (1990)(Pub).adf"),
                LRenameOp(root / "junk.bin", root / "_unmatched/junk.bin", "move"),
                LRenameOp(games / "Old.m3u", games / "Old.m3u", "delete", reason="outdated playlist"),
            ]
            playlists = [M3UOp(games / "ABC v1.1 (1991)(Pub)[cr SR].m3u", ["# Generated by simple-rom-organiser", "a.adf", "b.adf"], "write")]
            sel = types.SimpleNamespace(incomplete=[LIncomplete(GAMES, "Lost (1990)(Pub)", 3, {2: 1}, (1, 3))])
            return LPlan(ops, playlists, sel, profile)

        def reason_counts(plan: Any) -> dict[str, int]:
            return {"kept": 1, "renamed": 1, "moved": 0, "excluded": 1, "superseded": 1, "incomplete": 1,
                    "duplicates": 1, "unmatched": 1, "conflict": 0, "skip": 0, "playlists_write": 1,
                    "playlists_ok": 0, "playlists_remove": 1, "playlists_conflict": 0}

        def apply_renames(ops: list, root_: Path, progress: Any = None, cancel: Any = None,
                          playlists: Any = ()) -> dict[str, Any]:
            test.calls["library_apply"].append((list(ops), list(playlists)))
            return {"moved": 5, "failed": [], "undo_log": test.undo_log, "playlists_written": len(playlists)}

        organiser.plan_library = plan_library
        organiser.reason_counts = reason_counts
        organiser.apply_renames = apply_renames

    def test_profile_defaults_save_and_reset(self) -> None:
        info = self.get("/api/library/profile?platform=Commodore%20Amiga")
        self.assertEqual(info["platform"], "Commodore Amiga")
        self.assertTrue(all(r["on"] for r in info["rules"]))
        self.assertEqual([r["key"] for r in info["rules"]][:2], ["bad_dump", "virus"])
        self.assertTrue(all(isinstance(r["label"], str) and r["label"] for r in info["rules"]))
        self.assertEqual({k: info["available"][k] for k in ("latest_only", "best_variant", "complete_only")},
                         {"latest_only": True, "best_variant": True, "complete_only": True})
        self.assertEqual(info["scopes"]["latest_dats"], [GAMES])
        self.assertEqual(info["scopes"]["exclude_dats"], list(AMIGA.dats))
        self.assertEqual(info["profile"], info["defaults"])
        # other systems: only what applies
        other = self.get("/api/library/profile?platform=Atari%20ST")
        self.assertEqual({k: other["available"][k] for k in ("latest_only", "best_variant", "complete_only")},
                         {"latest_only": False, "best_variant": False, "complete_only": True})

        res = self.post("/api/library/profile", {"platform": "Commodore Amiga", "exclude": ["bad_dump", "demo"],
                                                 "best_variant": False})
        self.assertEqual(res["profile"]["exclude"], ["bad_dump", "demo"])
        self.assertFalse(res["profile"]["best_variant"])
        self.assertEqual([r["key"] for r in res["rules"] if r["on"]], ["bad_dump", "demo"])
        saved = json.loads((self.data / "config.json").read_text())
        self.assertEqual(saved["library"]["Commodore Amiga"]["exclude"], ["bad_dump", "demo"])
        plats = {p["name"]: p for p in self.get("/api/platforms")}
        self.assertEqual(plats["Commodore Amiga"]["library"]["exclude"], ["bad_dump", "demo"])
        self.assertEqual(plats["Atari ST"]["library"]["exclude"], sorted(plats["Atari ST"]["library"]["exclude"]))
        res = self.post("/api/library/profile", {"platform": "Commodore Amiga", "reset": True})
        self.assertEqual(res["profile"], res["defaults"])
        for body in ({"platform": "Commodore Amiga"},                                   # nothing to save
                     {"platform": "Commodore Amiga", "exclude": ["nope"]},              # unknown rule
                     {"platform": "Commodore Amiga", "exclude": "bad_dump"},            # wrong type
                     {"platform": "Sega Saturn", "latest_only": True}):                 # unknown platform
            status, _, _ = self.request("POST", "/api/library/profile", body)
            self.assertEqual(status, 400, body)
        status, _, _ = self.request("POST", "/api/library/profile", {"platform": "Atari ST", "latest_only": True}, token=None)
        self.assertEqual(status, 403)

    # ---- AMENDMENT 8: catalog, languages, keep-flags, regions, vanish report

    def test_profile_info_has_catalog_and_new_fields(self) -> None:
        info = self.get("/api/library/profile?platform=Commodore%20Amiga")
        self.assertEqual(info["style"], "tosec")
        kinds = {e["kind"] for e in info["catalog"] if e.get("group") != "ratings"}
        self.assertEqual(kinds, {"exclude", "keep_flag", "option"})
        ids = [e["id"] for e in info["catalog"] if e["kind"] == "keep_flag"]
        self.assertEqual(ids, ["cr", "h", "t", "a", "f", "tr"])
        pre = next(e for e in info["catalog"] if e["id"] == "pre_release")
        self.assertIn("(pre-release)", pre["tokens"])
        bad = next(e for e in info["catalog"] if e["id"] == "bad_dump")
        self.assertIn("[b ...]", bad["tokens"])
        self.assertEqual(info["profile"]["languages"], [] if not info["available"]["languages"] else ["En"])
        self.assertEqual(sorted(info["profile"]["keep_flags"]), ["a", "cr", "f", "h", "t", "tr"])
        self.assertFalse(info["profile"]["rescue_only_dump"])
        self.assertIn("available_languages", info)
        self.assertIn("regions", info)
        self.assertNotIn("one_per_game", [e["id"] for e in info["catalog"]])  # consoles only

    def test_profile_save_new_fields_and_validation(self) -> None:
        res = self.post("/api/library/profile", {
            "platform": "Commodore Amiga", "languages": ["En", "De"], "keep_flags": ["h", "t"],
            "rescue_only_dump": True, "region_priority": ["Japan", "USA"], "one_per_game": False})
        prof = res["profile"]
        self.assertEqual(prof["languages"], ["En", "De"])      # order is the priority
        self.assertEqual(prof["keep_flags"], ["h", "t"])
        self.assertTrue(prof["rescue_only_dump"])
        self.assertEqual(prof["region_priority"], ["Japan", "USA"])
        saved = json.loads((self.data / "config.json").read_text())["library"]["Commodore Amiga"]
        self.assertEqual(saved["languages"], ["En", "De"])
        res = self.post("/api/library/profile", {"platform": "Commodore Amiga", "languages": ["De", "En", "De"]})
        self.assertEqual(res["profile"]["languages"], ["De", "En"])
        self.assertEqual(res["profile"]["keep_flags"], ["h", "t"])  # untouched by the partial update
        self.assertEqual(self.post("/api/library/profile", {"platform": "Commodore Amiga", "languages": []})["profile"]["languages"], [])
        info = self.get("/api/library/profile?platform=Commodore%20Amiga")
        self.assertTrue(info["profile"]["borrow_other_editions"])           # default on (Amendment 12)
        self.assertTrue(info["available"]["borrow_editions"])
        self.assertIn("borrow_editions", [e["id"] for e in info["catalog"]])
        res = self.post("/api/library/profile", {"platform": "Commodore Amiga", "borrow_other_editions": False})
        self.assertFalse(res["profile"]["borrow_other_editions"])
        saved = json.loads((self.data / "config.json").read_text())["library"]["Commodore Amiga"]
        self.assertFalse(saved["borrow_other_editions"])
        self.assertTrue(self.post("/api/library/profile", {"platform": "Commodore Amiga", "reset": True})
                        ["profile"]["borrow_other_editions"])
        for body in ({"languages": ["Xx"]}, {"languages": "En"}, {"keep_flags": ["zz"]}, {"keep_flags": "cr"},
                     {"region_priority": ["Atlantis"]}, {"region_priority": "USA"}):
            status, _, _ = self.request("POST", "/api/library/profile", {"platform": "Commodore Amiga", **body})
            self.assertEqual(status, 400, body)
        res = self.post("/api/library/profile", {"platform": "Commodore Amiga", "reset": True})
        self.assertEqual(res["profile"], res["defaults"])

    def test_old_saved_profile_loads_with_new_defaults(self) -> None:
        (self.data / "config.json").write_text(json.dumps({"library": {"Commodore Amiga": {
            "exclude": ["demo"], "latest_only": True, "best_variant": False, "complete_only": True, "bogus": 1}}}))
        info = self.get("/api/library/profile?platform=Commodore%20Amiga")
        self.assertEqual(info["profile"]["exclude"], ["demo"])
        self.assertFalse(info["profile"]["best_variant"])
        self.assertEqual(sorted(info["profile"]["keep_flags"]), ["a", "cr", "f", "h", "t", "tr"])
        self.assertFalse(info["profile"]["rescue_only_dump"])

    def test_available_languages_from_the_installed_dats(self) -> None:
        platforms = sys.modules["romorg.platforms"]
        # a platform object that also carries the language scope (the real platforms.Platform has it)
        lang = types.SimpleNamespace(name="Lang Test", dats=(GAMES,), m3u_dats=(GAMES,), kickstart_dat=None,
                                     latest_dats=(GAMES,), best_variant_dats=(GAMES,), language_dats=(GAMES,))
        platforms.PLATFORMS["Lang Test"] = lang
        names = ["A (1990)(Pub).adf", "A (1990)(Pub)(de).adf", "B (1991)(Pub)(de-en).adf", "C (1991)(Pub)(fr).adf"]
        roms = [Rom(n, 1, f"{i:08x}", game=n[:-4], dat=GAMES) for i, n in enumerate(names)]
        platforms.load_platform_dats = lambda platform, *a, **k: (
            [types.SimpleNamespace(name=GAMES, roms=roms)], [])
        info = self.get("/api/library/profile?platform=Lang%20Test")
        langs = {r["code"]: r for r in info["available_languages"]}
        self.assertEqual(list(info["available_languages"][0].keys()), ["code", "name", "count", "games"])
        self.assertEqual(info["available_languages"][0]["code"], "En")      # English first
        self.assertEqual(langs["En"]["count"], 2)                           # untagged + (de-en)
        self.assertEqual(langs["De"]["count"], 2)
        self.assertEqual(langs["Fr"]["count"], 1)
        self.assertEqual(info["profile"]["languages"], ["En"])              # only English ticked by default
        self.assertTrue(info["ranking"].startswith("Preference: cracked"))
        self.assertEqual(self.get("/api/library/profile?platform=Atari%20ST")["available_languages"], [])
        self.assertEqual(self.get("/api/library/profile?platform=Atari%20ST")["ranking"], "")

    def test_nointro_profile_has_regions_and_one_per_game(self) -> None:
        platforms = sys.modules["romorg.platforms"]
        ni = types.SimpleNamespace(name="NI Test", dats=("NI",), m3u_dats=(), kickstart_dat=None, latest_dats=("NI",),
                                   best_variant_dats=(), language_dats=("NI",), region_dats=("NI",), source="nointro")
        platforms.PLATFORMS["NI Test"] = ni
        info = self.get("/api/library/profile?platform=NI%20Test")
        self.assertEqual(info["style"], "nointro")
        self.assertTrue(info["available"]["one_per_game"])
        self.assertEqual(info["regions"][:4], ["Europe", "USA", "World", "Japan"])
        self.assertTrue(info["profile"]["one_per_game"])
        self.assertEqual(info["ranking"], "")
        self.assertIn("one_per_game", [e["id"] for e in info["catalog"]])
        self.assertNotIn("keep_flag", {e["kind"] for e in info["catalog"]})
        res = self.post("/api/library/profile", {"platform": "NI Test", "region_priority": ["Japan", "Europe"], "one_per_game": False})
        self.assertEqual(res["profile"]["region_priority"], ["Japan", "Europe"])
        self.assertEqual(res["regions"][:3], ["Japan", "Europe", "Argentina"])
        self.assertFalse(res["profile"]["one_per_game"])

    def test_region_priority_full_reorder_round_trips(self) -> None:
        platforms = sys.modules["romorg.platforms"]
        ni = types.SimpleNamespace(name="NI Test", dats=("NI",), m3u_dats=(), kickstart_dat=None, latest_dats=("NI",),
                                   best_variant_dats=(), language_dats=("NI",), region_dats=("NI",), source="nointro")
        platforms.PLATFORMS["NI Test"] = ni
        full = self.get("/api/library/profile?platform=NI%20Test")["regions"]
        self.assertGreater(len(full), 40)
        moved = ["Taiwan"] + [r for r in full if r != "Taiwan"]            # what a drag of the last row to the top sends
        res = self.post("/api/library/profile", {"platform": "NI Test", "region_priority": moved})
        self.assertEqual(res["regions"], moved)
        self.assertEqual(res["profile"]["region_priority"], moved)
        saved = json.loads((self.data / "config.json").read_text())["library"]["NI Test"]
        self.assertEqual(saved["region_priority"], moved)
        self.assertEqual(self.get("/api/library/profile?platform=NI%20Test")["regions"], moved)   # after a reload
        # duplicates collapse (first wins), a short list keeps the others after it alphabetically
        res = self.post("/api/library/profile", {"platform": "NI Test", "region_priority": ["Japan", "USA", "Japan"]})
        self.assertEqual(res["profile"]["region_priority"], ["Japan", "USA"])
        self.assertEqual(res["regions"][:2], ["Japan", "USA"])
        self.assertEqual(sorted(res["regions"]), sorted(full))              # nothing lost
        # unknown regions are refused, the saved order is untouched
        status, _, _ = self.request("POST", "/api/library/profile", {"platform": "NI Test", "region_priority": ["Japan", "Atlantis"]})
        self.assertEqual(status, 400)
        self.assertEqual(self.get("/api/library/profile?platform=NI%20Test")["regions"][:2], ["Japan", "USA"])

    def _vanish_plan(self) -> None:
        self.scan()
        van = [
            _library.Vanished(GAMES, "Anstoss", "Anstoss (1993)(Blue Byte)(de)", "language", ("language",), ("De",), 1),
            _library.Vanished(GAMES, "Zork", "Zork (1990)(Pub)(fr)", "language", ("language",), ("Fr", "De"), 2),
            _library.Vanished(GAMES, "Zool", "Zool (1992)(Gremlin)[cr FLT]", "flag_cr", ("flag_cr",), (), 1),
        ]
        sel = types.SimpleNamespace(incomplete=[], vanished=van, vanish_summary=lambda: _library.Selection(vanished=van).vanish_summary())
        organiser = sys.modules["romorg.organiser"]
        inner = organiser.plan_library

        def plan_library(result: Any, profile: Any, **kw: Any) -> Any:
            plan = inner(result, profile, **kw)
            plan.selection = sel
            plan.ops[1].reasons = ("flag_cr", "language")
            plan.exclusion_counts = lambda: {"exclusive": {"flag_cr": 1}, "any": {"flag_cr": 1, "language": 1}}
            plan.vanish_summary = sel.vanish_summary
            return plan

        organiser.plan_library = plan_library

    def test_plan_has_exclusion_counts_vanish_and_why_filter(self) -> None:
        self._vanish_plan()
        plan = self.post("/api/library/plan", {"limit": 100})
        self.assertEqual(plan["exclusions"], {"exclusive": {"flag_cr": 1}, "any": {"flag_cr": 1, "language": 1}})
        self.assertEqual(plan["vanish"]["titles"], 3)
        self.assertEqual(plan["vanish"]["by_reason"], {"language": 2, "flag_cr": 1})
        excluded = [i for i in plan["items"] if i.get("code") == "excluded"]
        self.assertEqual(excluded[0]["reasons"], ["flag_cr", "language"])
        self.assertEqual(self.post("/api/library/plan", {"why": "flag_cr"})["total"], 1)
        self.assertEqual(self.post("/api/library/plan", {"why": "language"})["total"], 0)  # primary reason only
        status, _, _ = self.request("POST", "/api/library/plan", {"why": "nonsense"})
        self.assertEqual(status, 400)

    def test_plan_rows_carry_rating_checksum_refs_and_sort_by_name(self) -> None:
        self._vanish_plan()
        plan = self.post("/api/library/plan", {"limit": 100, "checksums": True})
        files = [i for i in plan["items"] if i.get("item") == "file"]
        self.assertTrue(all("rating" in i and "cs_kind" in i for i in files))
        self.assertTrue(any(i.get("checksums") for i in files if i["cs_kind"]))
        names = [i["to_name"].casefold() for i in files]
        up = self.post("/api/library/plan", {"limit": 100, "sort": "name_asc"})["items"]
        down = self.post("/api/library/plan", {"limit": 100, "sort": "name_desc"})["items"]
        key = lambda i: (i.get("to_name") or "").casefold()
        self.assertEqual([key(i) for i in up if i.get("item") == "file"], sorted(names))
        self.assertEqual([key(i) for i in down if i.get("item") == "file"], sorted(names, reverse=True))

    def test_plan_counts_every_row_per_category(self) -> None:
        self._vanish_plan()
        plan = self.post("/api/library/plan", {"limit": 200})
        rows: dict[str, int] = {}
        for item in plan["items"]:
            rows[item["category"]] = rows.get(item["category"], 0) + 1
        self.assertEqual(plan["categories"], rows)
        self.assertEqual(sum(plan["categories"].values()), plan["total"])

    def test_override_endpoint_stores_validates_and_clears(self) -> None:
        self.scan()
        game = {"dat": GAMES, "game": "Alpha (1990)(P)"}
        info = self.post("/api/library/override", {"action": "exclude", "games": [game]})
        self.assertEqual(info["profile"]["overrides"], [[GAMES, "Alpha (1990)(P)", "exclude"]])
        self.assertEqual(self.get("/api/library/profile")["profile"]["overrides"], info["profile"]["overrides"])
        info = self.post("/api/library/override", {"action": "keep", "games": [game]})
        self.assertEqual(info["profile"]["overrides"], [[GAMES, "Alpha (1990)(P)", "keep"]])
        info = self.post("/api/library/override", {"action": "clear", "games": [game]})
        self.assertEqual(info["profile"]["overrides"], [])
        for bad in ({"action": "nope", "games": [game]}, {"action": "keep", "games": []},
                    {"action": "keep", "games": [{"dat": GAMES}]}, {"action": "keep"}):
            status, _, _ = self.request("POST", "/api/library/override", bad)
            self.assertEqual(status, 400, bad)

    def test_vanished_endpoint_sorts_by_title_both_ways(self) -> None:
        self._vanish_plan()
        up = [i["title"] for i in self.post("/api/library/vanished", {"sort": "name_asc"})["items"]]
        down = [i["title"] for i in self.post("/api/library/vanished", {"sort": "name_desc"})["items"]]
        self.assertEqual(up, ["Anstoss", "Zool", "Zork"])
        self.assertEqual(down, ["Zork", "Zool", "Anstoss"])

    def test_vanished_endpoint_pages_filters_and_searches(self) -> None:
        self._vanish_plan()
        res = self.post("/api/library/vanished", {})
        self.assertEqual((res["total"], res["titles"]), (3, 3))
        self.assertEqual(res["by_language"], {"De": 2, "Fr": 1})
        self.assertEqual(res["items"][0]["text"], "no version in selected languages (has De)")
        only = self.post("/api/library/vanished", {"reason": "flag_cr"})
        self.assertEqual([i["title"] for i in only["items"]], ["Zool"])
        self.assertEqual(only["by_language"], {"De": 2, "Fr": 1})   # hints stay stable under a filter
        self.assertEqual(self.post("/api/library/vanished", {"q": "zork"})["total"], 1)
        self.assertEqual(self.post("/api/library/vanished", {"q": "fr"})["total"], 1)
        page = self.post("/api/library/vanished", {"limit": 2, "offset": 2})
        self.assertEqual((page["total"], len(page["items"])), (3, 1))
        status, _, _ = self.request("POST", "/api/library/vanished", {}, token=None)
        self.assertEqual(status, 403)

    def test_platform_options_write_the_profile(self) -> None:
        self.post("/api/platforms/options", {"platform": "Commodore Amiga", "latest_only": False})
        info = self.get("/api/library/profile?platform=Commodore%20Amiga")
        self.assertFalse(info["profile"]["latest_only"])
        self.assertTrue(info["profile"]["best_variant"])  # untouched

    def test_profile_change_drops_cached_plans(self) -> None:
        self.scan()
        self.post("/api/library/plan", {})
        self.post("/api/library/plan", {})
        self.assertEqual(len(self.calls["plan_library"]), 1)  # cached
        self.post("/api/library/profile", {"platform": "Commodore Amiga", "exclude": []})
        self.post("/api/library/plan", {})
        self.assertEqual(len(self.calls["plan_library"]), 2)
        self.assertEqual(self.calls["plan_library"][-1]["profile"].exclude, frozenset())

    def test_library_plan_shape_and_filters(self) -> None:
        status, body, _ = self.request("POST", "/api/library/plan", {})
        self.assertEqual(status, 409)  # needs a scan
        self.scan()
        plan = self.post("/api/library/plan", {"limit": 100})
        self.assertEqual(plan["total"], 8)  # 7 file rows + 1 playlist
        self.assertEqual(plan["reasons"]["excluded"], 1)
        self.assertEqual(plan["playlists"], {"write": 1, "ok": 0, "remove": 1, "conflict": 0})
        self.assertEqual(plan["incomplete_sets"], [{"name": "Lost (1990)(Pub)", "dat": GAMES, "total": 3,
                                                    "missing": [1, 3], "present": [2]}])
        self.assertEqual(plan["profile"]["exclude"], sorted(plan["profile"]["exclude"]))
        self.assertFalse(plan["empty"])
        self.assertEqual(plan["actionable"], 7 + 1 - 0)  # six moves/renames + one delete + one playlist write
        by_cat = {}
        for item in plan["items"]:
            by_cat.setdefault(item["category"], []).append(item)
        self.assertEqual(set(by_cat), {"kept", "excluded", "superseded", "incomplete", "duplicate", "unmatched", "playlist"})
        excluded = by_cat["excluded"][0]
        self.assertEqual((excluded["code"], excluded["flags_text"], excluded["item"]), ("excluded", "[b corrupt file]", "file"))
        self.assertEqual(by_cat["incomplete"][0]["missing"], [1, 3])
        self.assertTrue(by_cat["duplicate"][0]["keeper"].endswith("X (1990)(Pub).adf"))
        self.assertEqual(by_cat["superseded"][0]["superseded_by"], "New (1991)(Pub)")
        playlist = [i for i in by_cat["playlist"] if i["item"] == "playlist"][0]
        self.assertEqual((playlist["status"], playlist["disks"], playlist["name"]),
                         ("write", 2, "ABC v1.1 (1991)(Pub)[cr SR].m3u"))
        self.assertEqual(playlist["notes"], [])
        self.assertEqual(plan["borrowed"], {"sets": 0, "disks": 0, "by_difference": {}})
        for reason, expected in (("excluded", 1), ("kept", 1), ("duplicate", 1), ("unmatched", 1), ("playlist", 2)):
            self.assertEqual(self.post("/api/library/plan", {"reason": reason})["total"], expected, reason)
        self.assertEqual(self.post("/api/library/plan", {"reason": "excluded", "q": "bad"})["total"], 1)
        status, _, _ = self.request("POST", "/api/library/plan", {"reason": "bogus"})
        self.assertEqual(status, 400)

    def test_library_plan_passes_options(self) -> None:
        self.scan()
        self.post("/api/library/plan", {"savedisk": True, "labels": False})
        self.assertEqual({k: v for k, v in self.calls["plan_library"][-1].items() if k != "profile"},
                         {"savedisk": True, "labels": False, "platform": "Commodore Amiga"})

    def test_library_apply_and_undo_are_single_jobs(self) -> None:
        self.scan()
        self.post("/api/library/apply", {})
        job = self.wait_job()
        self.assertEqual((job["kind"], job["status"]), ("library", "done"))
        self.assertFalse(job["cancellable"])
        self.assertEqual(job["result"]["action"], "library")
        self.assertEqual(job["result"]["playlists_written"], 1)
        self.assertIn("summary", job["result"])  # re-scanned
        ops, playlists = self.calls["library_apply"][-1]
        self.assertEqual(len(ops), 7)
        self.assertEqual([p.status for p in playlists], ["write"])
        self.post("/api/library/undo", {"log": str(self.undo_log)})
        job = self.wait_job()
        self.assertEqual((job["kind"], job["result"]["action"]), ("library", "undo"))
        self.assertEqual(self.calls["undo"], [self.undo_log])
        status, _, _ = self.request("POST", "/api/library/undo", {"log": "/etc/passwd"})
        self.assertEqual(status, 400)  # only logs the organiser lists

    def test_library_plan_empty_when_nothing_to_do(self) -> None:
        self.scan()
        organiser = sys.modules["romorg.organiser"]
        organiser.plan_library = lambda result, profile, **kw: LPlan(
            [LRenameOp(self.roms / "a.adf", self.roms / "a.adf", "ok")], [M3UOp(self.roms / "a.m3u", [], "ok")],
            types.SimpleNamespace(incomplete=[]), profile)
        plan = self.post("/api/library/plan", {})
        self.assertTrue(plan["empty"])
        self.assertEqual(plan["actionable"], 0)

    def test_scan_summary_carries_duplicates_and_bad_flags(self) -> None:
        # the summary comes from the scanner as is (duplicates == the plan's duplicate moves is the scanner's job)
        summary = self.scan()["result"]
        self.assertIn("duplicates", summary)

    def test_rule_filter_facets_and_bad_flag_values(self) -> None:
        rows = [({"tags": {"regions": [], "languages": [], "video": [], "status": "", "flags": [], "dump_flags": ["b corrupt file"],
                           "bad": True, "bios": False, "bad_flags": ["[b corrupt file]"],
                           "excluded_by": [{"rule": "bad_dump", "text": "[b corrupt file]"}]}}, ""),
                ({"tags": {"regions": [], "languages": [], "video": [], "status": "", "flags": [], "dump_flags": ["m baddump"],
                           "bad": False, "bios": False, "bad_flags": [],
                           "excluded_by": [{"rule": "modified", "text": "[m baddump]"}]}}, ""),
                ({"tags": {"regions": [], "languages": [], "video": [], "status": "", "flags": [], "dump_flags": ["!"],
                           "bad": False, "bios": False, "bad_flags": [], "excluded_by": []}}, "")]
        facets = server._facets(rows)
        self.assertEqual(facets["rules"], {"bad_dump": 1, "modified": 1})
        self.assertEqual(facets["flags"].get("bad"), 1)  # only the real [b]; [m baddump] is "modified"
        self.assertEqual(len(server._tag_filter(rows, {"rule": "modified"})), 1)
        self.assertEqual(len(server._tag_filter(rows, {"flag": "bad"})), 1)
        self.assertEqual(len(server._tag_filter(rows, {"rule": "bad_dump", "flag": "bad"})), 1)
        self.assertEqual(len(server._tag_filter(rows, {"rule": "virus"})), 0)

    def test_ui_has_no_manual_download_and_has_library_step(self) -> None:
        html = server.read_static("index.html").decode()
        js = server.read_static("app.js").decode()
        for gone in ("update-tosec-btn", "update-nointro-btn", "force-download", "release-info", "nointro-info"):
            self.assertNotIn(gone, html)
            self.assertNotIn(gone, js)
        for present in ("updates-btn", "updates-line", "lib-plan-btn", "lib-apply-btn", "lib-undo-btn", "library-rules",
                        "advanced-box", "lib-incomplete"):
            self.assertIn(f'id="{present}"', html)
        self.assertNotIn("/api/dats/update", js)
        self.assertIn("/api/library/apply", js)
        self.assertIn("/api/updates/check", js)
        # AMENDMENT 8: panel rendered from the catalog, vanish list, no hard-coded rule lists
        for present in ("lib-vanish-box", "lib-vanish-table", "lib-why-filters"):
            self.assertIn(f'id="{present}"', html)
        self.assertIn("/api/library/vanished", js)
        self.assertIn("TOSEC convention", js)
        self.assertIn("info.catalog", js)
        for hard_coded in ("Pre-release / beta", "demo-playable"):
            self.assertNotIn(hard_coded, js)


SNES_DAT = "Nintendo - Super Nintendo Entertainment System"
N64_DAT = "Nintendo - Nintendo 64"


def _hashes(data: bytes) -> dict[str, Any]:
    return {"size": len(data), "crc32": f"{zlib.crc32(data) & 0xffffffff:08x}",
            "md5": hashlib.md5(data).hexdigest(), "sha1": hashlib.sha1(data).hexdigest()}


class ChecksumServerTests(unittest.TestCase):
    """Amendment 15: DAT vs local checksums per result row, last-scan records, routes data (real core modules)."""

    def setUp(self) -> None:
        import array
        import zipfile
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-cs-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1"})
        env.start()
        self.addCleanup(env.stop)
        rng = random.Random(7)
        blob = lambda n: bytes(rng.getrandbits(8) for _ in range(n))  # noqa: E731
        self.snes = self.tmp / "snes"
        self.snes.mkdir()
        self.plain, self.hdr, self.zipped = blob(2048), blob(2048), blob(1024)
        (self.snes / "Plain (USA).sfc").write_bytes(self.plain)
        (self.snes / "weird.smc").write_bytes(b"\0" * 512 + self.hdr)           # 512-byte copier header
        with zipfile.ZipFile(self.snes / "pack.zip", "w") as z:
            z.writestr("Zipped (USA).sfc", self.zipped)
        self.stray = blob(300)
        (self.snes / "stray.bin").write_bytes(self.stray)
        self.n64 = self.tmp / "n64"
        self.n64.mkdir()
        z64 = b"\x80\x37\x12\x40" + blob(4092)
        a = array.array("H")
        a.frombytes(z64)
        a.byteswap()
        (self.n64 / "dump.v64").write_bytes(a.tobytes())
        self.z64 = z64
        missing = blob(1024)
        self.missing = missing

        def cmp_dat(name: str, games: list[tuple[str, bytes]]) -> None:
            out = [f'clrmamepro (\n\tname "{name}"\n\tdescription "{name}"\n\tversion "2026.08.01"\n)\n']
            for game, data in games:
                h = _hashes(data)
                out.append(f'game (\n\tname "{game}"\n\trom ( name "{game}.sfc" size {h["size"]} crc {h["crc32"].upper()} '
                           f'md5 {h["md5"].upper()} sha1 {h["sha1"].upper()} )\n)\n')
            folder = self.tmp / "data" / "nointro"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{name}.dat").write_text("".join(out))

        cmp_dat(SNES_DAT, [("Plain (USA)", self.plain), ("Headered (USA)", self.hdr), ("Zipped (USA)", self.zipped),
                           ("Absent (USA)", missing)])
        cmp_dat(N64_DAT, [("Swapped (USA)", z64)])
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.port = self.srv.port
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, path: str, body: Any = None, expect: int = 200) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers={"Content-Type": "application/json", "X-Romorg-Token": "t"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                self.assertEqual(resp.status, expect)
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, expect, err.read())
            return {}

    def scan(self, platform: str, folder: Path) -> dict[str, Any]:
        self.call("/api/scan", {"platform": platform, "path": str(folder)})
        end = time.time() + 30
        while time.time() < end:
            job = self.call("/api/job")
            if job["status"] != "running":
                self.assertEqual(job["status"], "done", job)
                return job
            time.sleep(0.02)
        self.fail("scan timeout")

    def rows(self, kind: str, **extra: Any) -> dict[str, Any]:
        query = "&".join(f"{k}={v}" for k, v in {"kind": kind, "limit": 100, "checksums": 1, **extra}.items())
        return self.call(f"/api/scan/results?{query}")

    def row(self, kind: str, needle: str) -> dict[str, Any]:
        return next(i for i in self.rows(kind)["items"] if needle in json.dumps(i))

    SNES_PLATFORM = "Super Nintendo Entertainment System"

    def test_plain_match_local_equals_dat(self) -> None:  # case 1
        self.scan(self.SNES_PLATFORM, self.snes)
        cs = self.row("matched", "Plain (USA).sfc")["checksums"]
        h = _hashes(self.plain)
        self.assertEqual(cs["kind"], "file")
        self.assertEqual(cs["source"], "nointro")
        self.assertEqual((cs["dat"][0]["crc32"], cs["dat"][0]["md5"], cs["dat"][0]["sha1"]), (h["crc32"], h["md5"], h["sha1"]))
        local = cs["local"][0]
        self.assertEqual((local["raw"]["crc32"], local["raw"]["sha1"]), (h["crc32"], h["sha1"]))
        self.assertIsNone(local["raw"]["md5"])                       # MD5 is not computed while scanning: never invented
        self.assertEqual(local["equal"], {"crc32": True, "md5": None, "sha1": True})
        self.assertEqual((local["via"], local["normalised"], local["archive"]), ("raw", None, False))

    def test_copier_header_shows_both_hashes_and_why(self) -> None:  # case 2 (SNES)
        self.scan(self.SNES_PLATFORM, self.snes)
        cs = self.row("matched", "weird.smc")["checksums"]
        local = cs["local"][0]
        h = _hashes(self.hdr)
        raw_all = _hashes(b"\0" * 512 + self.hdr)
        self.assertEqual(local["via"], "headerless")
        self.assertIn("512", local["via_text"])
        self.assertEqual(local["raw"]["crc32"], raw_all["crc32"])    # the file's own hash differs from the DAT ...
        self.assertNotEqual(local["raw"]["crc32"], cs["dat"][0]["crc32"])
        self.assertEqual((local["normalised"]["crc32"], local["normalised"]["sha1"]), (h["crc32"], h["sha1"]))
        self.assertEqual(local["normalised"]["size"], h["size"])
        self.assertEqual(local["equal"]["crc32"], True)              # ... the normalised one equals it
        self.assertEqual(local["equal"]["sha1"], True)

    def test_byteswapped_n64(self) -> None:  # case 2 (N64)
        self.scan("Nintendo 64", self.n64)
        local = self.row("matched", "dump.v64")["checksums"]["local"][0]
        self.assertEqual(local["via"], "byteswapped")
        self.assertIn("byte order", local["via_text"])
        self.assertNotEqual(local["raw"]["crc32"], local["normalised"]["crc32"])
        self.assertEqual(local["normalised"]["crc32"], _hashes(self.z64)["crc32"])
        self.assertTrue(local["equal"]["crc32"])

    def test_zip_member_only_crc(self) -> None:  # case 3
        self.scan(self.SNES_PLATFORM, self.snes)
        cs = self.row("matched", "Zipped (USA)")["checksums"]
        local = cs["local"][0]
        self.assertTrue(local["archive"])
        self.assertEqual(local["member"], "Zipped (USA).sfc")
        self.assertEqual(local["raw"], {"crc32": _hashes(self.zipped)["crc32"], "md5": None, "sha1": None})
        self.assertEqual(local["equal"], {"crc32": True, "md5": None, "sha1": None})

    def test_missing_has_only_dat_checksums(self) -> None:  # case 6
        self.scan(self.SNES_PLATFORM, self.snes)
        for kind in ("missing", "games"):
            row = self.row(kind, "Absent (USA)")
            cs = row["checksums"]
            self.assertEqual(cs["local"], [])
            self.assertEqual(cs["dat"][0]["sha1"], _hashes(self.missing)["sha1"])
            self.assertEqual(cs["kind"], "missing")

    def test_games_row_has_dat_and_local(self) -> None:
        self.scan(self.SNES_PLATFORM, self.snes)
        cs = self.row("games", "Plain (USA)")["checksums"]
        self.assertEqual(cs["kind"], "file")
        self.assertEqual(cs["local"][0]["equal"]["sha1"], True)

    def test_unmatched_local_checksums(self) -> None:  # case 7
        self.scan(self.SNES_PLATFORM, self.snes)
        cs = self.row("unmatched", "stray.bin")["checksums"]
        self.assertEqual(cs["kind"], "unmatched")
        self.assertEqual(cs["dat"], [])
        self.assertEqual(cs["local"][0]["raw"]["crc32"], _hashes(self.stray)["crc32"])
        self.assertEqual(cs["local"][0]["raw"]["sha1"], _hashes(self.stray)["sha1"])
        self.assertEqual(cs["local"][0]["equal"], {"crc32": None, "md5": None, "sha1": None})

    def test_checksums_only_on_request_and_per_row_endpoint(self) -> None:
        self.scan(self.SNES_PLATFORM, self.snes)
        plain = self.call("/api/scan/results?kind=matched")
        self.assertTrue(all("checksums" not in i for i in plain["items"]))
        item = plain["items"][0]
        self.assertIn("id", item)
        detail = self.call(f"/api/scan/checksums?kind=matched&id={item['id']}")
        self.assertEqual(detail["kind"], "file")
        with_cs = self.rows("matched")["items"]
        self.assertEqual(with_cs[item["id"]]["checksums"], detail)
        self.call("/api/scan/checksums?kind=matched&id=999", expect=404)
        self.call("/api/scan/checksums?kind=errors&id=0", expect=400)
        # asking for checksums never pollutes the cached rows
        self.assertTrue(all("checksums" not in i for i in self.call("/api/scan/results?kind=matched")["items"]))

    def test_multi_disk_amiga_set_lists_every_disk(self) -> None:  # case 5
        games = "Commodore Amiga - Games - [ADF]"
        rng = random.Random(3)
        disks = {n: bytes(rng.getrandbits(8) for _ in range(512)) for n in (1, 3)}
        lines = ['<?xml version="1.0"?>', "<datafile>",
                 f"<header><name>{games}</name><description>{games}</description><version>2025-01-30</version></header>"]
        for n in (1, 2, 3):
            data = disks.get(n, bytes(512))
            name = f"Set (1990)(Pub)(Disk {n} of 3)"
            lines.append(f'<game name="{name}"><description>{name}</description><rom name="{name}.adf" size="512" '
                         f'crc="{zlib.crc32(data):08x}" md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game>')
        lines.append("</datafile>")
        dats = self.tmp / "data" / "dats"
        dats.mkdir(parents=True, exist_ok=True)
        (dats / f"{games} (TOSEC-v2025-01-30_CM).dat").write_text("\n".join(lines))
        root = self.tmp / "amiga"
        root.mkdir()
        for n, data in disks.items():
            (root / f"Set (1990)(Pub)(Disk {n} of 3).adf").write_bytes(data)
        self.scan("Commodore Amiga", root)
        cs = self.row("matched", "Disk 1 of 3")["checksums"]
        d = cs["disks"]
        self.assertEqual((d["total"], d["complete"]), (3, False))
        by = {x["number"]: x for x in d["disks"]}
        self.assertEqual(sorted(by), [1, 2, 3])
        self.assertTrue(by[2]["missing"])
        self.assertTrue(by[1]["this"] and not by[3]["this"])
        self.assertEqual(by[3]["local"]["equal"]["sha1"], True)
        self.assertEqual(by[3]["dat"]["sha1"], hashlib.sha1(disks[3]).hexdigest())

    def test_last_scan_record_is_persisted_per_system(self) -> None:
        rows = {p["name"]: p for p in self.call("/api/platforms")}
        self.assertIsNone(rows[self.SNES_PLATFORM]["last_scan"])
        self.assertEqual(rows[self.SNES_PLATFORM]["slug"], "super-nintendo-entertainment-system")
        self.scan(self.SNES_PLATFORM, self.snes)
        rows = {p["name"]: p for p in self.call("/api/platforms")}
        rec = rows[self.SNES_PLATFORM]["last_scan"]
        self.assertEqual((rec["have"], rec["total"], rec["missing"], rec["count_by"]), (3, 4, 1, "game"))
        self.assertEqual(rec["folder"], str(self.snes))
        self.assertEqual(rec["pct"], 75.0)
        self.assertRegex(rec["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertIsNone(rows["Nintendo 64"]["last_scan"])
        saved = json.loads((self.tmp / "data" / "config.json").read_text())["scan_records"]
        self.assertEqual(set(saved), {self.SNES_PLATFORM})
        self.assertLess(len(json.dumps(saved)), 1000)                # a small record, never result data
        # a new server on the same data dir still knows it (cards survive a restart)
        srv2 = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        try:
            again = {p["name"]: p for p in srv2.app.platforms_list({}, None)}
            self.assertEqual(again[self.SNES_PLATFORM]["last_scan"]["have"], 3)
        finally:
            srv2.server_close()

    def test_job_carries_its_platform(self) -> None:
        job = self.scan(self.SNES_PLATFORM, self.snes)
        self.assertEqual(job["platform"], self.SNES_PLATFORM)

    def test_scan_record_helper_and_slugs(self) -> None:
        rec = server.scan_record({"count_by": "rom", "dat_total": 10, "have": 4, "missing": 6, "matched_files": 5,
                                  "unmatched_files": 1, "duplicates": 1, "chd_files": 2, "identified": 1, "verified": 1,
                                  "raw": 0}, "/x", ["D"], now=0)
        self.assertEqual((rec["total"], rec["have"], rec["pct"], rec["identified"], rec["dats"]), (10, 4, 40.0, 1, ["D"]))
        self.assertNotIn("chd_files", server.scan_record({"dat_total": 1}, "/x"))
        self.assertEqual(server._slug("Commodore Amiga - WHDLoad"), "commodore-amiga-whdload")
        slugs = [p["slug"] for p in self.call("/api/platforms")]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertTrue(all(s and s == s.lower() and "/" not in s and " " not in s for s in slugs))


class UiStructureTests(unittest.TestCase):
    """The system-cards + per-system-tabs layout (Amendment 15): ids, routes and what must not be hard-coded."""

    def test_html_has_home_tabs_and_global_header(self) -> None:
        html = server.read_static("index.html").decode()
        for element_id in ("topbar", "updates-line", "updates-btn", "quit-btn", "job-bar", "view-home", "home-groups",
                           "view-system", "back-link", "sys-tabs", "tabbtn-overview", "tabbtn-library", "tabbtn-browse",
                           "tabbtn-tools", "tab-overview", "tab-library", "tab-browse", "tab-tools", "folder-input",
                           "scan-btn", "summary-cards", "library-rules-summary", "tool-convert", "tool-verify",
                           "tool-kickstart", "browse-gate", "lib-gate", "result-table"):
            self.assertIn(f'id="{element_id}"', html)
        for gone in ("stepnav", "step-systems", "step-scan", "platform-select", "systems-body", 'class="step-num"'):
            self.assertNotIn(gone, html)
        self.assertEqual(html.count('role="tab"'), 4)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))      # no external resources
        self.assertNotIn("https://", html)

    def test_js_routes_and_lazy_browse(self) -> None:
        js = server.read_static("app.js").decode()
        for needle in ("#/system/", "const Route", "parse(hash)", 'addEventListener("hashchange"', "function applyRoute",
                       "function renderBrowse", "browseDirty", "history.replaceState", "/api/scan/checksums", "checksums:",
                       "function checksumPanel", "Show checksums", "ArrowRight", "function systemCard", "p.last_scan"):
            self.assertIn(needle, js)
        for system in ("Dreamcast", "PlayStation", "Nintendo 64", "WHDLoad\"", "Game Boy"):   # systems come from /api/platforms
            self.assertNotIn(f'"{system}', js.replace('"WHDLoad"', ""))
        self.assertNotIn("src=\"http", js)       # (a docs hyperlink is fine; no external script / image is loaded)
        css = server.read_static("style.css").decode()
        self.assertNotIn("url(http", css)
        self.assertNotIn("@import", css)
        self.assertIn(":focus-visible", css)


class RegionSortableUiTests(unittest.TestCase):
    """The drag-and-drop region priority list (Amendment 16): markup / script contract; behaviour is checked in a browser."""

    def setUp(self) -> None:
        self.js = server.read_static("app.js").decode()
        self.css = server.read_static("style.css").decode()

    def test_pointer_drag_keyboard_and_buttons_exist(self) -> None:
        for needle in ("function sortableList", "data-drag-handle", '"pointerdown"', "pointermove", "pointerup", "pointercancel",
                       "setPointerCapture", "requestAnimationFrame", "aria-live", 'role: "status"', "aria-label", '"Move to top"',
                       "row-top", "row-up", "row-down", 'key === "Escape"', 'key === "ArrowUp"', 'key === "ArrowDown"',
                       'type: "search"', "Find a ${noun}", "region_priority: order", "Could not save the region priority"):
            self.assertIn(needle, self.js, needle)
        self.assertNotIn("draggable", self.js)              # HTML5 drag and drop is unreliable on touch screens
        self.assertNotIn("dragstart", self.js)

    def test_css_touch_action_only_on_the_handle_and_targets(self) -> None:
        self.assertRegex(self.css, r"\.drag-handle\s*\{[^}]*touch-action:\s*none")
        self.assertEqual(self.css.count("touch-action: none"), 1)
        self.assertRegex(self.css, r"\.sortable-list\s*\{[^}]*height:\s*min\(360px,\s*60vh\)")
        self.assertRegex(self.css, r"\.drag-handle\s*\{[^}]*width:\s*44px")
        self.assertIn(".sortable-row:focus-visible", self.css)
        self.assertIn(".sr-only", self.css)
        self.assertIn(".sortable-divider", self.css)

    def test_no_hard_coded_region_list_and_no_move_unmatched_option(self) -> None:
        for region in ("United Kingdom", "Argentina", "Scandinavia", "Hong Kong", "New Zealand"):
            self.assertNotIn(region, self.js)
        html = server.read_static("index.html").decode()
        for gone in ("lib-move-unmatched", "organise-move-unmatched"):
            self.assertNotIn(gone, html)
            self.assertNotIn(gone, self.js)
        self.assertNotIn("move_unmatched", self.js)
        self.assertNotIn("Move unmatched", html)


class MainTests(unittest.TestCase):
    def test_refuses_non_loopback(self) -> None:
        with mock.patch("sys.stderr"):
            self.assertEqual(server.main(["--host", "0.0.0.0", "--no-browser"]), 2)

    def test_running_instance_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": tmp}):
            self.assertIsNone(server.running_instance())
            srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
            try:
                info = {"pid": os.getppid() if hasattr(os, "getppid") else 0, "host": "127.0.0.1", "port": srv.port}
                (Path(tmp) / server.INSTANCE_FILE).write_text(json.dumps(info))
                self.assertEqual(server.running_instance(), f"http://127.0.0.1:{srv.port}/")
                with mock.patch("builtins.print"):
                    self.assertEqual(server.main(["--no-browser"]), 0)  # hands over, no 2nd server
            finally:
                srv.shutdown()
                srv.server_close()
            self.assertIsNone(server.running_instance())  # nothing answers any more


BASH = find_bash()


@unittest.skipUnless(BASH, "needs bash to run build_pyz.sh")
class ZipappTests(unittest.TestCase):
    def test_pyz_serves_index(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="romorg-pyz-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pyz = tmp / "app.pyz"
        subprocess.run([BASH, (ROOT / "packaging" / "build_pyz.sh").as_posix(), pyz.as_posix()], check=True,
                       capture_output=True, timeout=60)
        self.assertTrue(pyz.is_file())
        port = free_port()
        env = dict(os.environ, ROMORG_DATA_DIR=str(tmp / "data"))
        proc = subprocess.Popen([sys.executable, str(pyz), "--no-browser", "--port", str(port)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(tmp))
        try:
            html = ""
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
                        html = resp.read().decode()
                    break
                except OSError:
                    time.sleep(0.1)
            self.assertIn("Simple ROM Organiser", html)
            self.assertNotIn(server.TOKEN_PLACEHOLDER, html)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/app.js", timeout=2) as resp:
                self.assertIn(b"X-Romorg-Token", resp.read())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.stdout:
                proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
