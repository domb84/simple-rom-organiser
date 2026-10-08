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
        # (where case matters SNES/ is not the system's folder: what.sfc is then an unknown file of a loose folder)
        what = "snes/_unmatched/what.sfc" if os.name == "nt" else "_unmatched/snes/_unmatched/what.sfc"
        self.assertEqual(lower([x for x in tree(archive) if not x.endswith("/")]),
                         sorted(["_unmatched/snes/loose-unknown.sfc", "snes/_duplicates/alpha again.sfc",
                                 "snes/_duplicates/alpha copy.sfc", what]))
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


NASTY = ["CON", "NUL", "aux", "Ends with dot.", "Ends with space ", "What? (USA)", "A: B <C> (USA)", "com1.v2 (USA)"]


class MixedFolder(CollectionCase):
    """The manual run of the handover: a mixed folder with odd names, a duplicate, an unknown ROM and a picture."""
    GAMES = {"snes": ["Alpha (USA)", "Beta (USA)", "Delta Dash (USA)", "ゼルダの伝説 (Japan)", "Pokémon – Édition (France)"] + NASTY,
             "gba": ["Run (USA)", "Bash & Smash 100% (USA)", "It's Here (USA)"],
             "gb": ["Tetra (World)"]}

    def make(self) -> Path:
        root = self.tmp / "My ROMs & stuff 100% (it's here)"
        r = self.rom
        put(root / "Dump one" / "alpha.sfc", r["Alpha (USA)"])
        put(root / "Dump one" / "alpha copy.sfc", r["Alpha (USA)"])
        put(root / "Dump one" / "Sub & more" / "beta's.sfc", r["Beta (USA)"])
        put(root / "Dump one" / "日本語 フォルダ" / "ぜるだ.sfc", r["ゼルダの伝説 (Japan)"])
        put(root / "Dump one" / "accentué" / "pokemon.sfc", r["Pokémon – Édition (France)"])
        put(root / "Odd % name" / "run.gba", r["Run (USA)"])
        put(root / "Odd % name" / "bash.gba", r["Bash & Smash 100% (USA)"])
        put(root / "Odd % name" / "its.gba", r["It's Here (USA)"])
        put(root / "gb stuff" / "tetra.gb", r["Tetra (World)"])
        put(root / "Dump one" / "mystery.gba", b"not in any DAT")
        put(root / "Dump one" / "cover art.png", b"\x89PNG picture")
        put(root / "Dump one" / "delta.smc", bytes(512) + r["Delta Dash (USA)"])       # a copier header in front
        for i, name in enumerate(NASTY):                       # names from a DAT that Windows refuses as they are
            put(root / "nasty" / f"n{i}.sfc", r[name])
        return root

    KEPT = ["gb/Tetra (World).gb", "gba/Bash & Smash 100% (USA).gba", "gba/It's Here (USA).gba", "gba/Run (USA).gba",
            "snes/A_ B _C_ (USA).sfc", "snes/Alpha (USA).sfc", "snes/Beta (USA).sfc", "snes/Ends with dot..sfc",
            "snes/Ends with space .sfc", "snes/Pokémon – Édition (France).sfc", "snes/What_ (USA).sfc", "snes/_CON.sfc",
            "snes/_NUL.sfc", "snes/_aux.sfc", "snes/_com1.v2 (USA).sfc", "snes/ゼルダの伝説 (Japan).sfc"]
    ARCHIVED = ["_other/Dump one/cover art.png", "_unmatched/Dump one/mystery.gba", "snes/_duplicates/alpha copy.sfc"]

    def files(self, folder: Path) -> list[str]:
        return [x for x in tree(folder) if not x.endswith("/")]

    def test_scan_preview_build_rescan_undo(self) -> None:
        root = self.make()
        archive = root.with_name(root.name + "-archive")
        before = tree(root)
        scan = self.scan(root)
        self.assertEqual([(s["hint"], s["games"], s["files"], Path(s["folder"]).name) for s in scan["systems"]],
                         [("gb", 1, 1, "gb"), ("gba", 3, 3, "gba"), ("snes", 13, 14, "snes")])
        self.assertEqual((scan["unmatched"], scan["other"]), (1, 1))
        plan = self.job("/api/collection/plan")
        self.assertEqual(plan["sort"]["counts"], {SNES: 14, GB: 1, GBA: 3, "_unmatched": 1, "_other": 1})
        self.assertEqual(tree(root), before)                       # a preview moves nothing
        res = self.job("/api/collection/apply")
        self.assert_clean(res)
        self.assertEqual(res["sort"]["result"]["moved"], 20)       # every file once (not one more for the duplicate)
        self.assertEqual(self.files(root), sorted(self.KEPT + ["snes/Delta Dash (USA).smc"]))
        self.assertEqual(self.files(archive), self.ARCHIVED)
        self.assertEqual([d.name for d in root.iterdir() if d.is_dir()], ["gb", "gba", "snes"])     # the old folders are gone
        self.assert_one_move_per_file(root)
        self.assertEqual(len(self.moves(root)), 20)
        logs = [p.name for p in root.glob(".romorg-undo-*.json")]
        self.assertEqual(len(logs), 3)
        scan2 = self.scan(root)                                    # the undo logs in the root are not files of the collection
        self.assertEqual((scan2["files"], scan2["unmatched"], scan2["other"]), (17, 0, 0))
        again = self.job("/api/collection/plan")
        self.assertEqual((again["sort"]["total"], again["totals"]["actionable"]), (0, 0))
        self.assertEqual(sorted(p.name for p in root.glob(".romorg-undo-*.json")), sorted(logs))
        self.undo()
        self.assertEqual(tree(root), before)
        self.assertEqual(self.files(archive), [])

    def test_convert_first_cleans_the_headered_dump_from_where_it_lies(self) -> None:
        root = self.make()
        before = tree(root)
        res = self.build(root, convert=True)
        self.assert_clean(res)
        self.assertEqual(self.files(root), sorted(self.KEPT + ["snes/Delta Dash (USA).sfc", "snes/_converted_originals/Dump one/delta.smc"]))
        self.assertEqual((root / "snes" / "Delta Dash (USA).sfc").read_bytes(), self.rom["Delta Dash (USA)"])
        self.assertEqual(self.files(root.with_name(root.name + "-archive")), self.ARCHIVED)
        self.assert_one_move_per_file(root)
        self.undo()
        self.assertEqual(tree(root), before)

    def test_read_only_files_and_folders(self) -> None:
        root = self.make()
        marked = [root / "Dump one" / n for n in ("alpha.sfc", "alpha copy.sfc", "mystery.gba", "cover art.png", "delta.smc")] \
            + [root / "Odd % name" / "run.gba"]
        for p in marked:
            os.chmod(p, stat.S_IREAD)
        if os.name == "nt":                                    # (elsewhere a folder one cannot write to is another matter)
            os.chmod(root / "Odd % name", stat.S_IREAD)
        before = tree(root)
        res = self.build(root, convert=True)
        self.assert_clean(res)
        self.assertEqual(self.files(root), sorted(self.KEPT + ["snes/Delta Dash (USA).sfc", "snes/_converted_originals/Dump one/delta.smc"]))
        self.assertEqual(self.files(root.with_name(root.name + "-archive")), self.ARCHIVED)
        self.assertFalse(os.access(root / "snes" / "Alpha (USA).sfc", os.W_OK))        # still read-only
        self.undo()
        self.assertEqual(tree(root), before)
        self.assertEqual([p for p in marked if os.access(p, os.W_OK)], [])


class Failures(CollectionCase):
    """One file that cannot be moved is reported; the job goes on, nothing is duplicated, and undo still works."""

    def make(self) -> Path:
        root = self.tmp / "roms"
        put(root / "in use" / "alpha.sfc", self.rom["Alpha (USA)"])
        put(root / "in use" / "beta.sfc", self.rom["Beta (USA)"])
        put(root / "in use" / "unknown.gba", b"unknown 1")
        put(root / "in use" / "unknown2.gba", b"unknown 2")
        put(root / "cwd here" / "run.gba", self.rom["Run (USA)"])
        put(root / "free" / "tetra.gb", self.rom["Tetra (World)"])
        return root

    def files(self, folder: Path) -> list[str]:
        return [x for x in tree(folder) if not x.endswith("/")]

    def test_files_held_open_and_a_folder_in_use(self) -> None:
        import subprocess
        import sys
        root = self.make()
        archive = self.tmp / "roms-archive"
        before = tree(root)
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdout.write('up'); sys.stdout.flush(); sys.stdin.read()"],
                                cwd=str(root / "cwd here"), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.read(2), b"up")
            self.scan(root)
            with open(root / "in use" / "alpha.sfc", "rb"), open(root / "in use" / "unknown.gba", "rb"), \
                    mock.patch.object(sortroot, "RETRY_SLEEP", 0.01):
                res = self.job("/api/collection/apply")
            snes = next(r for r in res["systems"] if r["platform"] == SNES)
            if os.name == "nt":                                # held open: neither renamed nor deleted nor copied
                self.assertEqual([Path(f["path"]).name for f in res["sort"]["result"]["failed"]], ["unknown.gba"])
                self.assertEqual([Path(f["src"]).name for f in snes["failed"]], ["alpha.sfc"])
                self.assertEqual(self.files(root), ["gb/Tetra (World).gb", "gba/Run (USA).gba", "in use/alpha.sfc",
                                                    "in use/unknown.gba", "snes/Beta (USA).sfc"])
                self.assertEqual(self.files(archive), ["_unmatched/in use/unknown2.gba"])
                self.assertTrue((root / "cwd here").is_dir())          # in use: stays, silently
                self.assertFalse((root / "free").exists())
            else:
                self.assert_clean(res)
                self.assertEqual(self.files(root), ["gb/Tetra (World).gb", "gba/Run (USA).gba", "snes/Alpha (USA).sfc", "snes/Beta (USA).sfc"])
        finally:
            proc.communicate(b"")
            proc.stdout.close()
        plan = self.job("/api/collection/plan")                    # released: the next build finishes the job
        self.assertEqual(plan["sort"]["total"], 2 if os.name == "nt" else 0)
        self.undo()
        self.assertEqual(tree(root), before)
        self.assertEqual(self.files(archive), [])

    def test_a_move_the_system_refuses_fails_for_that_file_only(self) -> None:
        root = self.make()
        before = tree(root)
        self.scan(root)
        real = os.rename

        def rename(src: Any, dst: Any, *a: Any, **k: Any) -> None:
            if Path(src).name in ("alpha.sfc", "unknown.gba"):     # what Windows answers for a path over 260 characters
                raise OSError(2, "The system cannot find the path specified", str(src), 3, str(dst))
            real(src, dst, *a, **k)

        from romorg import organiser
        with mock.patch.object(sortroot.os, "rename", rename), mock.patch.object(organiser.os, "rename", rename), \
                mock.patch.object(organiser.os, "link", side_effect=OSError(1, "no hard links here")):
            res = self.job("/api/collection/apply")
        snes = next(r for r in res["systems"] if r["platform"] == SNES)
        self.assertEqual([Path(f["path"]).name for f in res["sort"]["result"]["failed"]], ["unknown.gba"])
        self.assertEqual([Path(f["src"]).name for f in snes["failed"]], ["alpha.sfc"])
        self.assertEqual(self.files(root), ["gb/Tetra (World).gb", "gba/Run (USA).gba", "in use/alpha.sfc", "in use/unknown.gba",
                                            "snes/Beta (USA).sfc"])
        self.undo()
        self.assertEqual(tree(root), before)

    @unittest.skipUnless(os.name == "nt", "the 260 character limit is a Windows matter")
    def test_windows_a_path_that_is_too_long_gets_the_hint(self) -> None:
        from romorg import organiser
        deep = self.tmp / ("d" * 120) / ("e" * 120)
        a = put(self.tmp / "in" / "a.sfc", b"a")
        b = put(self.tmp / "in" / "b.sfc", b"b")

        def rename(src: Any, dst: Any, *args: Any, **k: Any) -> None:
            raise OSError(2, "The system cannot find the path specified", str(src), 3, str(dst))

        with mock.patch.object(sortroot.os, "rename", rename), mock.patch.object(organiser.os, "rename", rename), \
                mock.patch.object(organiser.os, "link", side_effect=OSError(1, "no hard links here")):
            res = sortroot.apply_moves([sortroot.SMove(a, deep / "a.sfc", "x")], self.tmp / "j", "sort")
            res2 = organiser.apply_renames([organiser.RenameOp(b, deep / "b.sfc", "move", "", "", "move")], self.tmp)
        self.assertIn("LongPathsEnabled", res["failed"][0]["error"])
        self.assertIn("LongPathsEnabled", res2["failed"][0]["error"])
        self.assertTrue(a.is_file() and b.is_file())

    def test_cancel_in_the_middle_keeps_a_journal_and_undo_restores(self) -> None:
        root = self.tmp / "roms"
        for i in range(12):
            put(root / "Dump" / f"mystery{i:02}.gba", b"not in any DAT %d" % i)
        put(root / "Dump" / "alpha.sfc", self.rom["Alpha (USA)"])
        before = tree(root)
        self.scan(root)
        real, n = sortroot.move_path, [0]

        def counting(src: Path, dst: Path) -> None:
            n[0] += 1
            if n[0] == 5:
                self.call("POST", "/api/job/cancel", {})
            real(src, dst)

        with mock.patch.object(sortroot, "move_path", counting):
            self.call("POST", "/api/collection/apply", {})
            job = self.wait()
        self.assertEqual(job["status"], "cancelled")
        archive = self.tmp / "roms-archive"
        self.assertEqual(len([x for x in tree(archive) if x.endswith(".gba")]), 5)
        self.assertTrue((root / "Dump" / "alpha.sfc").is_file())       # the systems were not started
        last = self.call("GET", "/api/collection")["last"]
        self.assertEqual(len(json.loads(Path(last["sort"]).read_text(encoding="utf-8"))["moves"]), 5)
        self.assertEqual(self.undo()["restored"], 5)
        self.assertEqual(tree(root), before)
        self.assertEqual(tree(archive), [])

    def test_a_build_that_dies_is_still_the_one_undo_takes_back(self) -> None:
        # the journal is written move by move and the build is remembered when it starts, not when it ends
        root = self.tmp / "roms"
        for i in range(6):
            put(root / "Dump" / f"mystery{i}.gba", b"not in any DAT %d" % i)
        put(root / "Dump" / "alpha.sfc", self.rom["Alpha (USA)"])
        before = tree(root)
        self.scan(root)
        real, n = sortroot.move_path, [0]
        seen: dict[str, Any] = {}

        def dying(src: Path, dst: Path) -> None:
            n[0] += 1
            if n[0] == 4:                                          # as if the app were killed here: what is on disk now?
                seen["last"] = self.call("GET", "/api/collection")["last"]
                seen["journal"] = sortroot.read_journal(Path(seen["last"]["sort"]))
                raise SystemExit("killed")
            real(src, dst)

        with mock.patch.object(sortroot, "move_path", dying), mock.patch("threading.excepthook", lambda *a: None):
            self.call("POST", "/api/collection/apply", {})
            self.srv.app.jobs.current.thread.join(30)
        self.assertTrue(seen["journal"]["partial"])
        self.assertEqual(len(seen["journal"]["moves"]), 4)             # three made, the fourth written down and not made
        self.assertEqual(len([x for x in tree(self.tmp / "roms-archive") if x.endswith(".gba")]), 3)

    def test_a_build_stopped_after_convert_first_can_undo_the_conversion(self) -> None:
        root = self.tmp / "roms"
        put(root / "Dump" / "delta.smc", bytes(512) + self.rom["Delta (USA)"])         # a copier header in front
        put(root / "Dump" / "alpha.sfc", self.rom["Alpha (USA)"])
        before = tree(root)
        self.scan(root, convert=True)
        app = self.srv.app
        real = app._collection_layout

        def stop_here(*a: Any, **k: Any) -> Any:                   # the conversion is done; the user presses Stop
            app.jobs.cancel()
            return real(*a, **k)

        with mock.patch.object(app, "_collection_layout", stop_here):
            self.call("POST", "/api/collection/apply", {})
            self.assertEqual(self.wait()["status"], "cancelled")
        self.assertEqual((root / "snes" / "Delta (USA).sfc").read_bytes(), self.rom["Delta (USA)"])
        self.assertTrue((root / "snes" / "_converted_originals" / "Dump" / "delta.smc").is_file())
        self.assertTrue((root / "Dump" / "alpha.sfc").is_file())       # the rest was not started
        self.undo()
        self.assertEqual(tree(root), before)

    def test_undo_and_restore_wait_for_a_running_job(self) -> None:
        root = self.make()
        self.build(root)                                           # a first build: there is something to undo
        put(root / "new" / "mono.gb", self.rom["Mono (World)"])
        put(root / "new" / "what.gba", b"unknown 3")
        self.scan(root)
        gate, entered = threading.Event(), threading.Event()
        real = sortroot.move_path

        def slow(src: Path, dst: Path) -> None:
            entered.set()
            gate.wait(20)
            real(src, dst)

        with mock.patch.object(sortroot, "move_path", slow):
            self.call("POST", "/api/collection/apply", {})
            self.assertTrue(entered.wait(20))
            try:
                for path in ("/api/collection/undo", "/api/collection/aside/restore", "/api/collection/apply", "/api/collection/scan"):
                    code, out = self.http("POST", path, {})
                    self.assertEqual(code, 409, (path, out))
                code, _out = self.http("POST", "/api/library/export/undo", {"dest": str(self.tmp / "nowhere")})
                self.assertEqual(code, 409)
            finally:
                gate.set()
            self.assertEqual(self.wait()["status"], "done")
        self.assertTrue((root / "gb" / "Mono (World).gb").is_file())
        self.undo()
        self.assertTrue((root / "new" / "mono.gb").is_file())


class ArchiveFolder(CollectionCase):
    def test_the_default_shown_is_the_one_the_build_uses_also_through_a_link(self) -> None:
        import subprocess
        root = self.tmp / "real" / "roms"
        put(root / "Dump" / "notes.txt", b"notes")
        put(root / "Dump" / "alpha.sfc", self.rom["Alpha (USA)"])
        link = self.tmp / "link"
        try:
            if os.name == "nt":                                # a junction needs no privilege
                made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(root)], capture_output=True).returncode == 0
            else:
                os.symlink(root, link)
                made = True
        except OSError:
            made = False
        if not made:
            self.skipTest("cannot make a link here")
        self.addCleanup(lambda: os.path.lexists(link) and (os.rmdir(link) if os.name == "nt" else os.unlink(link)))
        info = self.call("POST", "/api/collection/save", {"root": str(link)})
        self.assertEqual(info["aside_default"], str(self.tmp / "real" / "roms-archive"))
        self.assert_clean(self.build(link))
        self.assertTrue((self.tmp / "real" / "roms-archive" / "_other" / "Dump" / "notes.txt").is_file())
        self.assertFalse((self.tmp / "link-archive").exists())
        self.undo()

    def test_a_folder_inside_the_rom_folder_is_refused_in_any_spelling(self) -> None:
        root = self.tmp / "Roms"
        put(root / "Dump" / "alpha.sfc", self.rom["Alpha (USA)"])
        self.scan(root)
        inside = [str(root / "archive"), str(root) + os.sep, str(root / "Dump" / ".." / "x")]
        if os.name == "nt":
            inside += [str(root).upper() + "\\ARCHIVE", str(root).swapcase(), str(root).replace("\\", "/") + "/a"]
        for aside in inside:
            self.call("POST", "/api/collection/save", {"aside": aside})
            code, out = self.http("POST", "/api/collection/plan", {})
            self.assertEqual((code, out.get("code")), (400, "bad_aside"), aside)
        self.call("POST", "/api/collection/save", {"aside": "", "place": "elsewhere", "dest": inside[0]})
        self.call("POST", "/api/collection/plan", {})
        job = self.wait()
        self.assertEqual((job["status"], job["error_code"]), ("error", "bad_destination"))


class EmptyFolders(CollectionCase):
    def test_undo_also_brings_back_the_folders_that_were_empty_before(self) -> None:
        root = self.tmp / "roms"
        put(root / "Dump" / "alpha.sfc", self.rom["Alpha (USA)"])
        (root / "ps2").mkdir()                                     # empty, e.g. made by a frontend for a system without games
        (root / "saves" / "slot 1").mkdir(parents=True)
        before = tree(root)
        self.assert_clean(self.build(root))
        self.assertEqual(tree(root), ["snes/", "snes/Alpha (USA).sfc"])
        self.undo()
        self.assertEqual(tree(root), before)


class UndoAgain(CollectionCase):
    def test_what_an_undo_could_not_take_back_can_be_undone_later(self) -> None:
        root = self.tmp / "roms"
        put(root / "Dump" / "alpha.sfc", self.rom["Alpha (USA)"])
        put(root / "Dump" / "beta.sfc", self.rom["Beta (USA)"])
        put(root / "Dump" / "unknown.gba", b"unknown 1")
        put(root / "Dump" / "notes.txt", b"notes")
        before = tree(root)
        self.assert_clean(self.build(root))
        put(root / "Dump" / "alpha.sfc", b"in the way")            # the old places of one ROM and of one archived file are taken
        put(root / "Dump" / "unknown.gba", b"in the way")
        out = self.call("POST", "/api/collection/undo", {})
        self.assertEqual(out["restored"], 2)
        self.assertEqual(len(out["skipped"]), 2, out)
        self.assertTrue((root / "snes" / "Alpha (USA).sfc").is_file())                    # not lost, not overwritten
        self.assertEqual((root / "Dump" / "alpha.sfc").read_bytes(), b"in the way")
        last = self.call("GET", "/api/collection")["last"]
        self.assertTrue(last.get("sort") and last.get("runs"), "the rest of the build can still be undone")
        os.unlink(root / "Dump" / "alpha.sfc")
        os.unlink(root / "Dump" / "unknown.gba")
        out = self.call("POST", "/api/collection/undo", {})
        self.assertEqual((out["restored"], out["skipped"], out["errors"]), (2, [], []))
        self.assertEqual(tree(root), before)
        self.assertEqual(self.call("GET", "/api/collection")["last"], {})
        self.assertEqual(self.http("POST", "/api/collection/undo", {})[0], 409)

    def test_a_move_back_that_fails_is_reported_and_kept_for_later(self) -> None:
        from romorg import organiser
        root = self.tmp / "roms"
        put(root / "Dump" / "alpha.sfc", self.rom["Alpha (USA)"])
        put(root / "Dump" / "unknown.gba", b"unknown 1")
        before = tree(root)
        self.assert_clean(self.build(root))

        def in_use(src: Any, dst: Any, *a: Any, **k: Any) -> None:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process", str(src))

        with mock.patch.object(organiser, "_rename_no_overwrite", in_use), mock.patch.object(sortroot, "move_path", in_use):
            out = self.call("POST", "/api/collection/undo", {})
        self.assertEqual(out["restored"], 0)
        self.assertEqual(len(out["skipped"]), 2, out)                  # both are told, none is dropped silently
        self.assertTrue((root / "snes" / "Alpha (USA).sfc").is_file())
        out = self.call("POST", "/api/collection/undo", {})            # free again
        self.assertEqual((out["restored"], out["skipped"], out["errors"]), (2, [], []))
        self.assertEqual(tree(root), before)


class LibraryWithArchive(CollectionCase):
    """One system's *Build library* in place, with an archive folder for what the rules set aside."""

    def test_undo_brings_back_everything_the_build_archived(self) -> None:
        root = self.tmp / "GBA"
        put(root / "run.gba", self.rom["Run (USA)"])
        put(root / "run copy.gba", self.rom["Run (USA)"])                          # a duplicate: archived by this build
        put(root / "_excluded" / "old leftover.gba", b"left by an earlier build")   # swept out by this build as well
        put(root / "what.gba", b"unknown")
        before = tree(root)
        aside = self.tmp / "lib-archive"
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        plan = self.call("POST", "/api/library/plan", {"limit": 50, "aside_to": str(aside)})
        res = self.job("/api/library/apply", {"aside_to": str(aside), "plan_id": plan["plan_id"]})
        self.assertEqual((res["aside"]["moved"], res["aside"]["failed"]), (3, []))
        self.assertEqual(tree(root), ["Run (USA).gba"])
        self.assertEqual(lower([x for x in tree(aside) if not x.endswith("/")]),
                         ["_unmatched/gba/what.gba", "gba/_duplicates/run copy.gba", "gba/_excluded/old leftover.gba"])
        back = self.job("/api/library/undo", {"log": res["undo_log"]})
        self.assertEqual(back["aside_restored"], 3)
        self.assertEqual(tree(root), before)
        self.assertEqual([x for x in tree(aside) if not x.endswith("/")], [])


OTHER_DRIVE = os.environ.get("ROMORG_TEST_OTHER_DRIVE")


@unittest.skipUnless(OTHER_DRIVE, "set ROMORG_TEST_OTHER_DRIVE to a folder on another drive than the temp folder")
class OtherDrive(CollectionCase):
    """The archive folder and the library on a second drive: every move there is a copy and a delete."""

    def setUp(self) -> None:
        super().setUp()
        self.far = Path(tempfile.mkdtemp(prefix="romorg-far-", dir=OTHER_DRIVE)).resolve()
        self.addCleanup(self._remove, self.far)
        self.assertNotEqual(os.stat(self.far).st_dev, os.stat(self.tmp).st_dev, "ROMORG_TEST_OTHER_DRIVE is on the same drive")

    def make(self) -> Path:
        root = self.tmp / "roms"
        put(root / "Dump" / "alpha.sfc", self.rom["Alpha (USA)"])
        put(root / "Dump" / "alpha copy.sfc", self.rom["Alpha (USA)"])
        put(root / "Dump" / "deep" / "beta.sfc", self.rom["Beta (USA)"])
        put(root / "Other stuff" / "run.gba", self.rom["Run (USA)"])
        put(root / "Other stuff" / "tetra.gb", self.rom["Tetra (World)"])
        for i in range(12):
            put(root / "Dump" / f"mystery{i:02}.gba", b"not in any DAT %d" % i)
        os.chmod(put(root / "Dump" / "pic.png", b"\x89PNG...."), stat.S_IREAD)
        return root

    LIB = ["gb/Tetra (World).gb", "gba/Run (USA).gba", "snes/Alpha (USA).sfc", "snes/Beta (USA).sfc"]

    def files(self, folder: Path) -> list[str]:
        return [x for x in tree(folder) if not x.endswith("/")]

    def test_the_archive_on_another_drive(self) -> None:
        root, archive = self.make(), self.far / "archive"
        before = tree(root)
        res = self.build(root, aside=str(archive))
        self.assert_clean(res)
        self.assertEqual(self.files(root), self.LIB)
        self.assertEqual(len(self.files(archive)), 14)
        self.assertTrue((archive / "snes" / "_duplicates" / "alpha copy.sfc").is_file())
        self.assertFalse(os.access(archive / "_other" / "Dump" / "pic.png", os.W_OK))      # read-only: moved, still read-only
        self.assert_one_move_per_file(root)
        self.undo()
        self.assertEqual(tree(root), before)
        self.assertFalse(archive.exists())

    def test_cancel_while_moving_to_the_other_drive(self) -> None:
        root, archive = self.make(), self.far / "archive"
        before = tree(root)
        self.scan(root, aside=str(archive))
        real, n = sortroot.move_path, [0]

        def counting(src: Path, dst: Path) -> None:
            n[0] += 1
            if n[0] == 5:
                self.call("POST", "/api/job/cancel", {})
            real(src, dst)

        with mock.patch.object(sortroot, "move_path", counting):
            self.call("POST", "/api/collection/apply", {})
            self.assertEqual(self.wait()["status"], "cancelled")
        self.assertEqual(len(self.files(archive)), 5)
        self.assertEqual(len(self.files(root)) + 5, len([x for x in before if not x.endswith("/")]))     # each file in one place
        self.assertEqual(self.undo()["restored"], 5)
        self.assertEqual(tree(root), before)

    def test_build_elsewhere_copy_then_keep_in_sync(self) -> None:
        root, dest = self.make(), self.far / "library"
        before = tree(root)
        self.scan(root, place="elsewhere", dest=str(dest), mode="copy")
        plan = self.job("/api/collection/plan")
        self.assertEqual((plan["totals"]["bytes_copy"], plan["enough_space"]), (4 * 2048, True))
        res = self.job("/api/collection/apply")
        self.assertEqual([r for r in res["systems"] if r["status"] != "ok" or r.get("failed")], [])
        self.assertEqual(self.files(dest), sorted(self.LIB + [f"{d}/.romorg-library/library.sqlite" for d in ("gb", "gba", "snes")]))
        self.assertEqual(tree(root), before)                       # a copy: the source is untouched
        (root / "Other stuff" / "run.gba").unlink()
        put(root / "new" / "bash.gba", self.rom["Bash (USA)"])
        self.scan(root, sync=True)
        plan = self.job("/api/collection/plan")
        self.assertEqual(plan["totals"]["remove"], 1)
        self.job("/api/collection/apply")
        self.assertTrue((dest / "gba" / "Bash (USA).gba").is_file())
        self.assertFalse((dest / "gba" / "Run (USA).gba").exists())

    def test_build_elsewhere_move_and_undo(self) -> None:
        root, dest = self.make(), self.far / "library"
        os.chmod(root / "Dump" / "alpha.sfc", stat.S_IREAD)
        before = tree(root)
        self.scan(root, place="elsewhere", dest=str(dest), mode="move")
        plan = self.job("/api/collection/plan")
        self.assertIn("another drive", " ".join(plan["systems"][0]["notes"]))
        res = self.job("/api/collection/apply")
        self.assertEqual([(r["platform"], r.get("failed")) for r in res["systems"] if r["status"] != "ok" or r.get("failed")], [])
        self.assertEqual(sum(r["result"]["moved"] for r in res["systems"]), 4)
        self.assertEqual([x for x in self.files(dest) if ".romorg-library" not in x], self.LIB)
        self.assertEqual(len(self.files(root)), 14)                # the kept files left the source
        self.undo()
        self.assertEqual(tree(root), before)
        self.assertEqual([x for x in self.files(dest) if ".romorg-library" not in x], [])


if __name__ == "__main__":
    unittest.main()
