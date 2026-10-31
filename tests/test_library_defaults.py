"""Your library defaults (Settings) and a system's own overrides: layering, the endpoints, and an older config being brought over."""

from __future__ import annotations

import dataclasses
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

    def test_a_settings_file_that_still_carries_keep_extras_loads(self) -> None:
        """The option "Keep other files that sit with a game" is gone (Amendment 113): an older config still names it, as a default and as a
        system's own rule, and a profile saved by an older version too."""
        ps = platforms.PLATFORMS["Sony PlayStation"]
        cfg = {"library_defaults": {"keep_extras": True, "exclude": ["demo"]}, "library_overrides": {ps.name: {"keep_extras": True, "min_rating": 4.0}}}
        profile = library.load_profile(cfg, ps)
        self.assertEqual((sorted(profile.exclude), profile.min_rating), (["demo"], 4.0))
        self.assertNotIn("keep_extras", profile.to_dict())
        self.assertFalse(hasattr(profile, "keep_extras"))
        self.assertNotIn("keep_extras", library.overridden_fields(cfg, ps))
        self.assertNotIn("keep_extras", library.LibraryProfile.from_dict({"keep_extras": True, "latest_only": False}).to_dict())

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
        added = [r for v, rs in sorted(library._RULES_ADDED.items()) for r in rs]           # (rules newer than the config: on, as for any older list)
        self.assertEqual(cfg["library_defaults"]["exclude"], ["demo", *added])           # the Collection's rules are your defaults
        self.assertEqual(library.overridden_fields(cfg, self.snes), ["exclude", "latest_only"])    # only what differs is an override
        after = {p.name: library.load_profile(cfg, p).to_dict() for p in (self.snes, platforms.PLATFORMS[GBA])}
        self.assertEqual(sorted({*before[SNES]["exclude"], *added}), sorted(after[SNES]["exclude"]))
        self.assertEqual(before[SNES]["latest_only"], after[SNES]["latest_only"])
        self.assertFalse(library.migrate_config(cfg))                                      # (and once only)


class RegionTieLayeringTests(unittest.TestCase):
    def test_ties_follow_the_defaults_unless_the_system_has_its_own_region_order(self) -> None:
        plat = platforms.get_platform("Nintendo 64")
        mine = {"region_priority": ["Europe", "USA"], "region_ties": ["USA"]}
        self.assertEqual(library.effective_profile(plat, mine, {}).region_ties, ("USA",))
        own = library.effective_profile(plat, mine, {"region_priority": ["USA", "Europe"]})
        self.assertEqual((own.region_priority[:2], own.region_ties), (("USA", "Europe"), ()))      # (the ties named another order)
        both = library.effective_profile(plat, mine, {"region_priority": ["USA", "Europe"], "region_ties": ["Europe"]})
        self.assertEqual(both.region_ties, ("Europe",))


    def test_a_system_with_its_own_region_order_has_its_own_ties_too(self) -> None:
        plat = platforms.get_platform("Nintendo 64")
        cfg: dict = {"library_defaults": {"region_priority": ["Europe", "USA", "Japan"], "region_ties": ["USA"]}}
        prof = library.load_profile(cfg, plat)
        self.assertEqual(prof.region_ties, ("USA",))
        import dataclasses
        library.store_profile(cfg, plat, dataclasses.replace(prof, region_priority=("Japan", "Europe", "USA")))   # only the order was moved
        ov = cfg["library_overrides"]["Nintendo 64"]
        self.assertEqual((ov["region_priority"][:3], ov["region_ties"]), (["Japan", "Europe", "USA"], ["USA"]))   # the ties shown stay
        self.assertEqual(library.load_profile(cfg, plat).region_ties, ("USA",))
        library.set_override(cfg, plat, ["region_priority"], False)                                              # back to the defaults: both
        self.assertNotIn("library_overrides", cfg)
        library.set_override(cfg, plat, ["region_ties"], True)
        self.assertEqual(sorted(cfg["library_overrides"]["Nintendo 64"]), ["region_priority", "region_ties"])


class DefaultsServerTests(ServerCase):
    COMPRESS_7Z = True                            # (the app's own defaults, untouched)
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

    def test_region_ties_round_trip_and_the_defaults_page_has_the_region_list(self) -> None:
        res = self.call("POST", "/api/library/defaults", {"region_priority": ["Europe", "USA", "Japan"], "region_ties": ["USA"]})
        self.assertEqual((res["profile"]["region_priority"], res["profile"]["region_ties"]), (["Europe", "USA", "Japan"], ["USA"]))
        self.assertTrue(res["available"]["region_priority"])
        self.assertIn("region_priority", [e["id"] for e in res["catalog"]])             # (Settings builds its panel from this)
        self.assertEqual(self.call("GET", "/api/library/defaults")["regions"][:3], ["Europe", "USA", "Japan"])
        code, _ = self.http("POST", "/api/library/defaults", {"region_ties": ["Atlantis"]})
        self.assertEqual(code, 400)

    def test_settings_offers_every_rule_any_system_has(self) -> None:
        info = self.call("GET", "/api/library/defaults")
        ids = {e["id"] for e in info["catalog"]}
        fields = {e["field"] for e in info["catalog"]}
        for rule in ("region_priority", "other_language", "keep_bios", "one_per_game", "latest_only", "best_variant"):
            self.assertIn(rule, ids, rule)
        missing = [f for f in library.RULE_FIELDS if f not in fields and f not in ("exclude", "keep_flags", "region_ties", "saved_games", "prefer_saved")]
        self.assertEqual(missing, [])                                    # a rule of the profile that Settings could not show
        self.assertNotIn("keep_extras", ids | fields)                    # (gone: Amendment 113)
        out = self.call("POST", "/api/library/defaults", {"keep_bios": False})
        self.assertFalse(out["profile"]["keep_bios"])
        self.assertIn("keep_bios", out["changed"])

    def test_a_config_with_keep_extras_still_serves_defaults_and_profiles(self) -> None:
        self.srv.app._config_update(library_defaults={"keep_extras": True, "keep_bios": False},
                                    library_overrides={GBA: {"keep_extras": True}})
        info = self.call("GET", "/api/library/defaults")
        self.assertNotIn("keep_extras", info["profile"])
        self.assertFalse(info["profile"]["keep_bios"])
        prof = self.call("GET", f"/api/library/profile?platform={q(GBA)}")
        self.assertNotIn("keep_extras", prof["profile"])
        self.assertNotIn("keep_extras", prof.get("overridden", []))

    def test_the_tiles_of_the_system_pages_are_one_list_for_every_system(self) -> None:
        info = self.call("GET", "/api/settings/tiles")
        self.assertEqual((info["shown"], info["max"]), (info["default"], 10))
        self.assertEqual(self.call("GET", "/api/status")["tiles"], info["default"])
        self.assertEqual(sorted(info["order"]), sorted(t["id"] for t in info["catalog"]))
        self.assertEqual(len(info["catalog"]), 16)
        out = self.call("POST", "/api/settings/tiles", {"shown": ["space", "have_all", "space", "regions"]})
        self.assertEqual(out["shown"], ["space", "have_all", "regions"])                      # in the order given, once each
        self.assertEqual(out["order"][:3], ["space", "have_all", "regions"])
        self.assertEqual(self.call("GET", "/api/status")["tiles"], ["space", "have_all", "regions"])
        self.assertEqual(self.call("POST", "/api/settings/tiles", {"shown": []})["shown"], [])   # an empty page is a choice too
        for bad in ({"shown": ["nope"]}, {"shown": "have_all"}, {"shown": [t["id"] for t in info["catalog"]]}):
            code, _ = self.http("POST", "/api/settings/tiles", bad)
            self.assertEqual(code, 400, bad)
        self.assertEqual(self.call("POST", "/api/settings/tiles", {"reset": True})["shown"], info["default"])


class MigrationServerTests(ServerCase):
    COMPRESS_7Z = True                            # (the app's own defaults, untouched)
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


class AmigaExclusions(unittest.TestCase):
    """The exclusion rules only the Amiga systems have are an override of their own (``exclude_amiga``) next to ``exclude``."""

    def test_the_amiga_only_rules_are_those_of_the_amiga_styles(self) -> None:
        self.assertEqual(library.amiga_exclusions(), frozenset({"virus", "bad_size", "faked", "modified", "unreleased", "cd_conversion"}))
        self.assertIn("exclude_amiga", library.applicable_fields(platforms.get_platform("Commodore Amiga")))
        self.assertNotIn("exclude_amiga", library.applicable_fields(platforms.get_platform("Super Nintendo Entertainment System")))

    def test_each_override_decides_its_own_rules(self) -> None:
        amiga = platforms.get_platform("Commodore Amiga")
        cfg = {"library_defaults": {"exclude": ["demo", "virus", "faked"]}}
        self.assertEqual(sorted(library.load_profile(cfg, amiga).exclude), ["demo", "faked", "virus"])
        library.set_override(cfg, amiga, ["exclude_amiga"], True)                      # (the Amiga rules are the system's own now, shared ones still follow you)
        prof = library.load_profile(cfg, amiga)
        library.store_profile(cfg, amiga, dataclasses.replace(prof, exclude=frozenset(set(prof.exclude) - {"virus"})))
        self.assertEqual(library.overridden_fields(cfg, amiga), ["exclude_amiga"])
        cfg["library_defaults"] = {"exclude": ["demo", "faked", "virus", "prototype"]}   # (you change the shared ones)
        self.assertEqual(sorted(library.load_profile(cfg, amiga).exclude), ["demo", "faked", "prototype"])
        library.set_override(cfg, amiga, ["exclude"], True)
        self.assertEqual(library.overridden_fields(cfg, amiga), ["exclude", "exclude_amiga"])
        library.set_override(cfg, amiga, ["exclude", "exclude_amiga"], False)
        self.assertNotIn("library_overrides", cfg)                                       # (nothing left behind, not even the marker)

    def test_an_override_made_when_there_was_one_list_still_decides_all_of_them(self) -> None:
        amiga = platforms.get_platform("Commodore Amiga")
        cfg = {"library_overrides": {amiga.name: {"exclude": ["demo"]}}}
        self.assertEqual(sorted(library.load_profile(cfg, amiga).exclude), ["demo"])
        self.assertEqual(library.overridden_fields(cfg, amiga), ["exclude", "exclude_amiga"])


class ConvertRules(unittest.TestCase):
    def platform(self, name: str):
        from romorg import platforms
        return platforms.get_platform(name) if hasattr(platforms, "get_platform") else next(p for p in platforms.list_platforms() if p.name == name)

    def test_the_rules_apply_where_a_system_can_convert(self) -> None:
        from romorg import library
        disc = library.applicable_fields(self.platform("Sony PlayStation 2"))
        cart = library.applicable_fields(self.platform("Super Nintendo Entertainment System"))
        none = library.applicable_fields(self.platform("Nintendo Game Boy Advance"))
        self.assertTrue({"convert_first", "delete_converted"} <= disc)
        self.assertIn("convert_first", cart)                                 # (clean up the dumps)
        self.assertNotIn("delete_converted", cart)                           # (originals are only deleted after a CHD was checked)
        self.assertFalse({"convert_first", "delete_converted"} & none)
        self.assertTrue(library.app_defaults().convert_first and library.app_defaults().delete_converted)   # on for every system that can

    def test_the_old_per_system_box_becomes_an_override_of_the_rule(self) -> None:
        from romorg import library
        psx, gba = self.platform("Sony PlayStation"), self.platform("Nintendo Game Boy Advance")
        cfg = {"convert_on_build": {"Sony PlayStation": True, "Nintendo Game Boy Advance": True, "Sega Dreamcast": False}}
        self.assertTrue(library.migrate_config(cfg))
        self.assertNotIn("convert_on_build", cfg)
        self.assertTrue(library.load_profile(cfg, psx).convert_first)
        self.assertEqual(library.overrides_of(cfg, psx).get("convert_first"), True)
        self.assertNotIn("convert_first", library.overrides_of(cfg, gba))   # (a system that cannot convert keeps nothing of it)
        self.assertTrue(library.load_profile(cfg, self.platform("Sega Dreamcast")).convert_first)   # (a box left off follows the default: on)
        library.store_global(cfg, library.LibraryProfile(convert_first=False))  # and your default is for every system that can
        self.assertFalse(library.load_profile(cfg, self.platform("Sega Dreamcast")).convert_first)
        self.assertTrue(library.load_profile(cfg, psx).convert_first)        # (but not one that overrides it)
