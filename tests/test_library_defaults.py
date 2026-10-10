"""Your library defaults (Settings) and a system's own overrides: layering, the endpoints, and an older config being brought over."""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.parse

from tests.servercase import ServerCase, GBA, SNES  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)

from romorg import library, paths, platforms, server


def q(name: str) -> str:
    return urllib.parse.quote(name)


class LayeringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snes = platforms.PLATFORMS[SNES]

    def test_a_system_follows_your_defaults_until_it_overrides_a_rule(self) -> None:
        cfg: dict = {}
        base = library.load_profile(cfg, self.snes)
        self.assertEqual(base, library.default_profile(self.snes))
        cfg["library_defaults"] = {"exclude": ["demo"], "min_rating": 4.0}
        mine = library.load_profile(cfg, self.snes)
        self.assertEqual(sorted(mine.exclude), ["demo"])
        self.assertEqual(mine.min_rating, 4.0)
        library.set_override(cfg, self.snes, ["exclude"], True)              # starts at what the system uses now
        self.assertEqual(library.overridden_fields(cfg, self.snes), ["exclude"])
        cfg["library_defaults"]["exclude"] = ["demo", "virus"]               # a later default no longer reaches it
        self.assertEqual(sorted(library.load_profile(cfg, self.snes).exclude), ["demo"])
        self.assertEqual(library.load_profile(cfg, self.snes).min_rating, 4.0)
        library.set_override(cfg, self.snes, ["exclude"], False)
        self.assertEqual(sorted(library.load_profile(cfg, self.snes).exclude), ["demo", "virus"])
        self.assertNotIn("library_overrides", cfg)

    def test_a_rule_the_system_cannot_use_is_neither_applied_nor_overridable(self) -> None:
        cfg = {"library_defaults": {"keep_flags": ["cr"], "rescue_only_dump": True}}       # (TOSEC rules; SNES is No-Intro)
        self.assertNotIn("keep_flags", library.applicable_fields(self.snes))
        self.assertEqual(library.load_profile(cfg, self.snes).keep_flags, library.default_profile(self.snes).keep_flags)
        library.set_override(cfg, self.snes, ["keep_flags"], True)
        self.assertEqual(library.overridden_fields(cfg, self.snes), [])

    def test_reset_keeps_the_choices_made_for_single_games(self) -> None:
        cfg: dict = {}
        library.set_override(cfg, self.snes, ["latest_only", "exclude"], True)
        prof = library.load_profile(cfg, self.snes)
        library.store_profile(cfg, self.snes, library.LibraryProfile.from_dict(
            {**prof.to_dict(), "overrides": [["Nintendo - Super Nintendo Entertainment System", "Alpha (USA)", "keep"]]}, prof))
        library.reset_overrides(cfg, self.snes)
        self.assertEqual(library.overridden_fields(cfg, self.snes), [])
        self.assertEqual([o[1] for o in library.load_profile(cfg, self.snes).overrides], ["Alpha (USA)"])

    def test_an_older_config_keeps_its_behaviour(self) -> None:
        cfg = {"collection": {"global": {"exclude": ["demo"], "one_per_game": False}},
               "library": {SNES: {"exclude": ["demo", "virus"], "latest_only": False, "one_per_game": False}},
               "latest_only": {GBA: False}}
        before = {p.name: library.load_profile(cfg, p).to_dict() for p in (self.snes, platforms.PLATFORMS[GBA])}
        self.assertTrue(library.migrate_config(cfg))
        self.assertNotIn("collection", cfg)
        self.assertNotIn("library", cfg)
        self.assertEqual(cfg["library_defaults"]["exclude"], ["demo"])                    # the Collection's rules are your defaults
        self.assertEqual(library.overridden_fields(cfg, self.snes), ["exclude", "latest_only"])    # only what differs is an override
        after = {p.name: library.load_profile(cfg, p).to_dict() for p in (self.snes, platforms.PLATFORMS[GBA])}
        self.assertEqual(before[SNES]["exclude"], after[SNES]["exclude"])
        self.assertEqual(before[SNES]["latest_only"], after[SNES]["latest_only"])
        self.assertFalse(library.migrate_config(cfg))                                      # (and once only)


class DefaultsServerTests(ServerCase):
    def profile(self, name: str = SNES) -> dict:
        return self.call("GET", f"/api/library/profile?platform={q(name)}")

    def test_the_defaults_answer_looks_like_a_systems_and_a_change_reaches_every_system(self) -> None:
        info = self.call("GET", "/api/library/defaults")
        self.assertEqual(info["profile"], info["defaults"])
        self.assertEqual(info["changed"], [])
        self.assertTrue(info["catalog"])
        res = self.call("POST", "/api/library/defaults", {"exclude": ["demo"], "one_per_game": False})
        self.assertEqual(res["profile"]["exclude"], ["demo"])
        self.assertEqual(sorted(res["changed"]), ["exclude", "one_per_game"])
        for name in (SNES, GBA):
            got = self.profile(name)
            self.assertEqual(got["profile"]["exclude"], ["demo"])
            self.assertFalse(got["profile"]["one_per_game"])
            self.assertEqual(got["inherit"]["overridden"], [])
        self.call("POST", "/api/library/defaults", {"reset": True})
        self.assertEqual(self.call("GET", "/api/library/defaults")["changed"], [])
        self.assertTrue(self.profile()["profile"]["one_per_game"])

    def test_overriding_a_rule_on_a_system_leaves_the_others_on_the_defaults(self) -> None:
        self.call("POST", "/api/library/defaults", {"exclude": ["demo"]})
        got = self.call("POST", "/api/library/profile", {"platform": SNES, "override": {"fields": ["exclude"], "on": True}})
        self.assertEqual(got["inherit"]["overridden"], ["exclude"])
        self.assertEqual(got["profile"]["exclude"], ["demo"])                       # it starts at what the system used
        got = self.call("POST", "/api/library/profile", {"platform": SNES, "exclude": ["demo", "virus"]})
        self.assertEqual(got["profile"]["exclude"], ["demo", "virus"])
        self.call("POST", "/api/library/defaults", {"exclude": []})
        self.assertEqual(self.profile()["profile"]["exclude"], ["demo", "virus"])          # not moved by the default
        self.assertEqual(self.profile(GBA)["profile"]["exclude"], [])                       # followed it
        rows = {p["name"]: p for p in self.call("GET", "/api/platforms")}
        self.assertEqual(rows[SNES]["rule_overrides"], ["exclude"])
        self.assertEqual(rows[GBA]["rule_overrides"], [])
        got = self.call("POST", "/api/library/profile", {"platform": SNES, "override": {"fields": ["exclude"], "on": False}})
        self.assertEqual(got["inherit"]["overridden"], [])
        self.assertEqual(got["profile"]["exclude"], [])

    def test_reset_all_returns_the_system_to_the_defaults(self) -> None:
        self.call("POST", "/api/library/profile", {"platform": SNES, "latest_only": True, "exclude": ["demo"]})
        self.assertTrue(self.profile()["inherit"]["overridden"])
        got = self.call("POST", "/api/library/profile", {"platform": SNES, "reset": True})
        self.assertEqual(got["inherit"]["overridden"], [])
        self.assertEqual(got["profile"], got["inherit"]["defaults"])

    def test_bad_requests_are_refused(self) -> None:
        for path, body in (("/api/library/defaults", {}), ("/api/library/defaults", {"exclude": ["nope"]}),
                           ("/api/library/profile", {"platform": SNES, "override": {"fields": "exclude"}}),
                           ("/api/library/profile", {"platform": SNES, "override": []})):
            code, _ = self.http("POST", path, body)
            self.assertEqual(code, 400, (path, body))

    def test_a_changed_default_drops_the_cached_plans(self) -> None:
        dropped: list[int] = []
        real = self.srv.app._drop_all_plans
        self.srv.app._drop_all_plans = lambda: (dropped.append(1), real())[1]
        try:
            self.call("POST", "/api/library/defaults", {"exclude": ["demo"]})
        finally:
            self.srv.app._drop_all_plans = real
        self.assertTrue(dropped)


class MigrationServerTests(ServerCase):
    def test_a_server_started_on_an_older_config_migrates_it(self) -> None:
        cfg_path = paths.config_path()
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        cfg["collection"] = {"global": {"exclude": ["demo"]}}
        cfg["library"] = {SNES: {"exclude": ["demo", "virus"]}}
        cfg_path.write_text(json.dumps(cfg))
        srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        saved = json.loads(cfg_path.read_text())
        self.assertNotIn("collection", saved)
        self.assertNotIn("library", saved)
        self.assertEqual(saved["library_defaults"]["exclude"], ["demo"])
        self.assertEqual(saved["library_overrides"][SNES]["exclude"], ["demo", "virus"])


if __name__ == "__main__":
    unittest.main()
