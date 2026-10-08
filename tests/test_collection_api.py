"""The Collection page end to end: a real server over HTTP, real folders on disk, tiny synthetic No-Intro DATs.

Written for the Windows pass 4 (case-insensitive paths, read-only files, files in use, a second drive), but every test runs
on every platform: where Windows and POSIX differ, the test says what each must do.
"""

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


class CollectionCase(unittest.TestCase):
    GAMES = {"snes": ["Alpha (USA)", "Beta (USA)", "Gamma (USA)", "Delta (USA)"],
             "gba": ["Run (USA)", "Bash (USA)"],
             "gb": ["Tetra (World)", "Mono (World)"]}
    BASE: str | None = None                       # the folder the scratch folder is made in (default: the temp folder)

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

    def scan(self, root: Path, **save: Any) -> dict[str, Any]:
        self.call("POST", "/api/collection/save", {"root": str(root), **save})
        return self.job("/api/collection/scan")["scan"]

    def build(self, root: Path, **save: Any) -> dict[str, Any]:
        self.scan(root, **save)
        return self.job("/api/collection/apply")

    def undo(self) -> dict[str, Any]:
        out = self.call("POST", "/api/collection/undo", {})
        self.assertEqual((out["errors"], out["skipped"]), ([], []))
        return out

    def assert_clean(self, res: dict[str, Any]) -> None:
        """Nothing failed anywhere in a build's answer."""
        self.assertEqual((res["sort"].get("result") or {}).get("failed", []), [])
        self.assertEqual((res.get("aside") or {}).get("failed", []), [])
        self.assertEqual([(r["platform"], r.get("error"), r.get("failed")) for r in res["systems"]
                          if r["status"] != "ok" or r.get("failed")], [])

    def moves(self, root: Path) -> list[tuple[str, str]]:
        """Every move of the last build: the undo logs in the root plus the journals in the data folder."""
        steps: list[tuple[str, str]] = []
        for log in root.glob(".romorg-undo-*.json"):
            for line in log.read_text(encoding="utf-8").splitlines()[1:]:
                rec = json.loads(line)
                if rec.get("op") == "move":
                    steps.append((os.path.normcase(str(root / rec["src"])), os.path.normcase(str(root / rec["dst"]))))
        last = self.call("GET", "/api/collection")["last"]
        for key in ("sort", "sweep"):
            if last.get(key):
                steps += [(os.path.normcase(m["from"]), os.path.normcase(m["to"])) for m in sortroot.read_journal(Path(last[key]))["moves"]]
        return steps

    def assert_one_move_per_file(self, root: Path) -> None:
        steps = self.moves(root)
        self.assertEqual(len({a for a, _b in steps}), len(steps), "a file was moved twice")
        self.assertFalse({a for a, _b in steps} & {b for _a, b in steps}, "a file was moved on from where a move had put it")


class CaseInsensitivePaths(CollectionCase):
    """On Windows (and on exFAT cards) ``SNES`` and ``snes`` are one folder."""

    def test_folders_that_already_have_the_standard_name_in_capitals_are_left_alone(self) -> None:
        root = self.tmp / "roms"
        put(root / "SNES" / "Alpha (USA).sfc", self.rom["Alpha (USA)"])
        put(root / "GBA" / "Run (USA).gba", self.rom["Run (USA)"])
        before = tree(root)
        scan = self.scan(root)
        self.assertEqual([(s["hint"], Path(s["current"]).name) for s in scan["systems"]], [("gba", "GBA"), ("snes", "SNES")])
        plan = self.job("/api/collection/plan")
        if os.name == "nt":                                    # the same folder: nothing to do, and no "snes (2)"
            self.assertEqual((plan["sort"]["total"], plan["totals"]["actionable"], plan["renames"]), (0, 0, []))
            res = self.job("/api/collection/apply")
            self.assert_clean(res)
            self.assertEqual(tree(root), before)
            self.assertEqual(self.http("POST", "/api/collection/undo", {})[0], 409)          # nothing was done
        else:                                                  # two folders: the files go to the standard one
            self.assertEqual(plan["sort"]["total"], 2)
            self.assert_clean(self.job("/api/collection/apply"))
            self.assertEqual(tree(root), ["gba/", "gba/Run (USA).gba", "snes/", "snes/Alpha (USA).sfc"])
            self.undo()
            self.assertEqual(tree(root), before)

    def test_another_name_for_a_system_becomes_the_standard_folder(self) -> None:
        root = self.tmp / "roms"
        put(root / "Gameboy" / "tetra.gb", self.rom["Tetra (World)"])
        put(root / "SNES" / "alpha.sfc", self.rom["Alpha (USA)"])
        put(root / "Snes stuff" / "beta.SFC", self.rom["Beta (USA)"])
        before = tree(root)
        res = self.build(root)
        self.assert_clean(res)
        self.assertEqual(lower(tree(root)), ["gb/", "gb/tetra (world).gb", "snes/", "snes/alpha (usa).sfc", "snes/beta (usa).sfc"])
        self.assertIn("gb/", tree(root))                       # spelled the standard way where it is new
        self.assertEqual(len([d for d in root.iterdir() if d.is_dir()]), 2, "no second 'snes (2)' folder")
        self.assert_one_move_per_file(root)
        self.assertEqual(self.job("/api/collection/plan")["totals"]["actionable"], 0)
        self.undo()
        self.assertEqual(tree(root), before)

    def test_a_name_that_differs_from_the_standard_one_only_in_case_is_renamed(self) -> None:
        root = self.tmp / "roms"
        put(root / "snes" / "ALPHA (usa).SFC", self.rom["Alpha (USA)"])
        self.scan(root)
        self.assertEqual(self.job("/api/collection/plan")["totals"]["actionable"], 1)
        self.assert_clean(self.job("/api/collection/apply"))
        self.assertEqual(tree(root), ["snes/", "snes/Alpha (USA).sfc"])                    # exactly: case matters here
        self.assertEqual(self.job("/api/collection/plan")["totals"]["actionable"], 0)
        self.undo()
        self.assertEqual(tree(root), ["snes/", "snes/ALPHA (usa).SFC"])

    def test_copies_whose_names_differ_only_in_case_never_collide(self) -> None:
        root = self.tmp / "roms"
        put(root / "x" / "Game.sfc", self.rom["Alpha (USA)"])
        put(root / "y" / "game.SFC", self.rom["Alpha (USA)"])
        put(root / "z" / "GAME.sfc", self.rom["Alpha (USA)"])
        put(root / "u1" / "Mystery.sfc", b"unknown one")
        put(root / "u2" / "mystery.SFC", b"unknown two")
        before = tree(root)
        res = self.build(root)
        self.assert_clean(res)
        self.assertEqual(tree(root), ["snes/", "snes/Alpha (USA).sfc"])
        archive = self.tmp / "roms-archive"
        dups = [p for p in (archive / "snes" / "_duplicates").iterdir()]
        self.assertEqual(len(dups), 2)
        self.assertEqual(len({p.name.casefold() for p in dups}), 2)
        self.assertEqual(sorted(p.name for p in (archive / "_unmatched").rglob("*.*")), ["Mystery.sfc", "mystery.SFC"])
        self.assert_one_move_per_file(root)
        self.undo()
        self.assertEqual(tree(root), before)
        self.assertEqual([p for p in archive.rglob("*") if p.is_file()], [])

    def test_a_file_named_like_the_standard_folder_does_not_stop_the_build(self) -> None:
        root = self.tmp / "roms"
        put(root / "stuff" / "Tetra (World).gb", self.rom["Tetra (World)"])
        put(root / "GB", b"a file called GB")
        before = tree(root)
        self.assert_clean(self.build(root))
        self.assertEqual(tree(root), ["gb/", "gb/Tetra (World).gb"])
        self.assertEqual((self.tmp / "roms-archive" / "_other" / "GB").read_bytes(), b"a file called GB")
        self.undo()
        self.assertEqual(tree(root), before)

    def test_a_capital_system_folder_with_set_aside_folders_of_an_earlier_build(self) -> None:
        # Windows: SNES\ is snes\, but its files are spelled ...\SNES\...; the planning compares paths as text and
        # stopped the whole job with "path outside the folder" as soon as the rules set a file of that folder aside.
        root = self.tmp / "roms"
        put(root / "SNES" / "Alpha (USA).sfc", self.rom["Alpha (USA)"])
        put(root / "SNES" / "_duplicates" / "Alpha copy.sfc", self.rom["Alpha (USA)"])
        put(root / "SNES" / "Alpha again.sfc", self.rom["Alpha (USA)"])
        put(root / "SNES" / "_unmatched" / "what.sfc", b"no idea")
        put(root / "SNES" / "_excluded" / "Gamma (USA).sfc", self.rom["Gamma (USA)"])
        put(root / "SNES" / "loose-unknown.sfc", b"no idea either")
        before = tree(root)
        res = self.build(root)
        self.assert_clean(res)
        self.assertEqual(lower(tree(root)), ["snes/", "snes/alpha (usa).sfc", "snes/gamma (usa).sfc"])
        self.assertEqual(len([d for d in root.iterdir() if d.is_dir()]), 1)
        archive = self.tmp / "roms-archive"
        self.assertEqual(lower([x for x in tree(archive) if not x.endswith("/")]),
                         ["_unmatched/snes/loose-unknown.sfc", "snes/_duplicates/alpha again.sfc", "snes/_duplicates/alpha copy.sfc",
                          "snes/_unmatched/what.sfc"])
        self.assertEqual(self.job("/api/collection/plan")["totals"]["actionable"], 0)
        self.undo()
        self.assertEqual(tree(root), before)

    def test_the_root_typed_in_another_case(self) -> None:
        root = self.tmp / "roms"
        put(root / "Dump" / "run.gba", self.rom["Run (USA)"])
        put(root / "Dump" / "readme.txt", b"hello")
        before = tree(root)
        typed = Path(str(root).upper()) if os.name == "nt" else root
        self.assert_clean(self.build(typed))
        self.assertEqual(tree(root), ["gba/", "gba/Run (USA).gba"])
        self.assertTrue((self.tmp / "roms-archive" / "_other" / "Dump" / "readme.txt").is_file())
        self.assert_one_move_per_file(typed)
        self.undo()
        self.assertEqual(tree(root), before)


if __name__ == "__main__":
    unittest.main()
