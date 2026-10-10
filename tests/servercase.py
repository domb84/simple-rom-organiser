"""A real server over HTTP on a scratch folder, with tiny synthetic No-Intro DATs: the base of the end-to-end tests."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any
from unittest import mock
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

from romorg import server, sortroot

DATS = {"snes": "Nintendo - Super Nintendo Entertainment System", "gba": "Nintendo - Game Boy Advance", "gb": "Nintendo - Game Boy"}
EXT = {"snes": ".sfc", "gba": ".gba", "gb": ".gb"}
SNES, GBA, GB = "Super Nintendo Entertainment System", "Nintendo Game Boy Advance", "Nintendo Game Boy"


def tree(folder: Path) -> list[str]:
    """Every file and folder under ``folder`` (folders end in ``/``), without the app's own undo logs."""
    folder = Path(folder)
    if not folder.exists():
        return []
    return sorted(p.relative_to(folder).as_posix() + ("/" if p.is_dir() else "") for p in folder.rglob("*")
                  if not p.name.startswith(".romorg"))


def lower(names: list[str]) -> list[str]:
    return sorted(n.casefold() for n in names)


def put(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class ServerCase(unittest.TestCase):
    GAMES = {"snes": ["Alpha (USA)", "Beta (USA)", "Gamma (USA)", "Delta (USA)"],
             "gba": ["Run (USA)", "Bash (USA)"],
             "gb": ["Tetra (World)", "Mono (World)"]}
    BASE: str | None = None                       # the folder the scratch folder is made in (default: the temp folder)
    ARCHIVE = True                                # set the archive folder of Settings to <scratch>/rom-archive

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-coll-", dir=self.BASE)).resolve()
        self.addCleanup(self._remove, self.tmp)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1"})
        env.start()
        self.addCleanup(env.stop)
        rng = random.Random(5)
        self.rom: dict[str, bytes] = {}
        for system, names in self.GAMES.items():
            lines = [f'clrmamepro (\n\tname "{DATS[system]}"\n\tdescription "{DATS[system]}"\n\tversion "20250101-000000"\n)\n']
            for name in names:
                data = bytes(rng.getrandbits(8) for _ in range(2048))
                self.rom[name] = data
                lines.append(f'game (\n\tname "{name}"\n\tdescription "{name}"\n\trom ( name "{name}{EXT[system]}" size {len(data)} '
                             f'crc {zlib.crc32(data):08x} md5 {hashlib.md5(data).hexdigest()} sha1 {hashlib.sha1(data).hexdigest()} )\n)\n')
            (self.tmp / "data" / "nointro").mkdir(parents=True, exist_ok=True)
            (self.tmp / "data" / "nointro" / f"{DATS[system]}.dat").write_text("\n".join(lines), encoding="utf-8")
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.port = self.srv.port
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        if self.ARCHIVE:                                   # there is no default archive folder: the tests choose one, like the user
            self.call("POST", "/api/settings/archive", {"dir": str(self.tmp / "rom-archive")})

    @staticmethod
    def _remove(folder: Path) -> None:
        def writable(fn: Any, path: str, _exc: Any) -> None:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            fn(path)
        shutil.rmtree(folder, onerror=writable)

    # ---- HTTP
    def http(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers.update({"Content-Type": "application/json", "X-Romorg-Token": "t"})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, json.loads(err.read() or b"{}")

    def call(self, method: str, path: str, body: Any = None) -> Any:
        code, out = self.http(method, path, body)
        self.assertEqual(code, 200, f"{path}: {out}")
        return out

    def wait(self, timeout: float = 60) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.call("GET", "/api/job")
            if job["status"] != "running":
                return job
            time.sleep(0.02)
        self.fail("job did not finish")

    def job(self, path: str, body: Any = None) -> dict[str, Any]:
        self.call("POST", path, body or {})
        job = self.wait()
        self.assertEqual(job["status"], "done", job.get("error"))
        return job["result"]
