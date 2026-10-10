"""The Switch as a system, through the real HTTP server: games, updates and add-ons in one folder are matched by title ID and version
against the catalogue made from the title database, and the ordinary Library rules (latest update only ...) work on them."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import test_dc_server as base  # noqa: E402
from test_switch import APP, UPDATE, game_nsp, nca_named, pfs0, small_db  # noqa: E402
from romorg import switchdb  # noqa: E402

SW = "Nintendo Switch"


class SwitchPlatformCase(base.DcServerCase):
    def setUp(self) -> None:
        super().setUp()
        home = self.base / "home"
        home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(home), "APPDATA": "", "USERPROFILE": ""})
        env.start()
        self.addCleanup(env.stop)
        path = switchdb.db_path()
        small_db(path)
        (self.base / "versions.json").write_text('{"%s": {"65536": "2020-01-01", "131072": "2020-02-01"}}' % APP)
        switchdb.build(path.with_name("t.json.gz"), path.with_name("c.json"), path, versions_json=self.base / "versions.json")
        switchdb.build_dat()
        self.call("/api/settings/archive", {"dir": str(self.base / "archive")})
        self.games = self.base / "switch"
        self.games.mkdir()
        (self.games / "Some Game [v0].nsp").write_bytes(game_nsp(APP, 0))
        (self.games / "update 1.nsp").write_bytes(game_nsp(UPDATE, 65536))
        (self.games / "update 2.nsp").write_bytes(game_nsp(UPDATE, 131072))
        (self.games / "mystery.nsp").write_bytes(game_nsp("0100FFFF0BBB0000", 0))

    def scan(self) -> dict:
        return self.run_job("/api/scan", {"path": str(self.games), "platform": SW})


class SwitchLibraryTests(SwitchPlatformCase):
    def test_the_catalogue_lists_the_game_and_each_update_and_files_match_by_title_id_and_version(self) -> None:
        plat = next(p for p in self.call("/api/platforms") if p["name"] == SW)
        self.assertEqual([d["name"] for d in plat["dats"]], list(switchdb.SWITCH_DATS[:2]) + [switchdb.SWITCH_DLC_DAT])
        self.assertTrue(all(d["present"] for d in plat["dats"][:2]))
        self.scan()
        matched = {m["file"] for m in self.call("/api/scan/results?kind=matched")["items"]}
        self.assertEqual(matched, {"Some Game [v0].nsp", "update 1.nsp", "update 2.nsp"})
        self.assertEqual([u["file"] for u in self.call("/api/scan/results?kind=unmatched")["items"]], ["mystery.nsp"])

    def test_the_library_keeps_the_game_and_the_latest_update_and_archives_the_rest(self) -> None:
        self.scan()
        self.call("/api/library/profile", {"platform": SW, "latest_only": True})
        plan = self.call("/api/library/plan", {"platform": SW})
        by_file = {r["file"] if "file" in r else r["from"]: r for r in plan["items"]}
        text = str(plan["items"])
        self.assertIn("superseded", text)
        self.assertIn("Some Game", text)
        self.assertEqual(plan["reasons"].get("superseded"), 1)


class SwitchChecksumOptionTests(SwitchPlatformCase):
    def test_a_scan_also_checks_every_file_when_the_option_is_on(self) -> None:
        name, body = nca_named(b"original")
        (self.games / "update 2.nsp").unlink()
        (self.games / "update 2 [v131072].nsp").write_bytes(pfs0({name: body[:-1] + b"X", f"{UPDATE}{'0' * 16}.tik": b"t"}))      # one byte changed
        self.scan()
        self.assertNotIn("checks", self.call("/api/status")["scan"]["summary"])                                       # (off: nothing is read)
        self.assertNotIn("checks", self.call("/api/scan/results?kind=matched")["items"][0])
        self.call("/api/switch/config", {"verify_scan": True})
        self.assertTrue(self.call("/api/switch")["config"]["verify_scan"])
        self.scan()
        checks = self.call("/api/status")["scan"]["summary"]["checks"]
        self.assertEqual((checks["ok"], checks["damaged"]), (2, 1))
        by = {m["file"]: m["checks"] for m in self.call("/api/scan/results?kind=matched")["items"]}
        self.assertEqual(by["Some Game [v0].nsp"], "ok")
        self.assertEqual(by["update 2 [v131072].nsp"], "damaged")


if __name__ == "__main__":
    unittest.main()
