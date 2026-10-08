"""Tests for romorg.retroarch: finding installs, the config, moving saves and states."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")       # no test ever works on the user's own RetroArch

from romorg import retroarch as ra  # noqa: E402

CFG = '''# comment
savefile_directory = "~/MEGA/saves"
savestate_directory = "~/MEGA/saves"
sort_savefiles_enable = "true"
sort_savestates_enable = "true"
savefiles_in_content_dir = "false"
savestates_in_content_dir = "false"
system_directory = ":/system"
rgui_browser_directory = ""
video_fullscreen = "true"
'''


class World(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve()
        self.base = self.home / "ra"
        self.base.mkdir()
        (self.base / "retroarch.cfg").write_text(CFG)
        self.inst = ra.Install("custom:0", "custom", "RA", self.base / "retroarch.cfg", self.base)
        self.saves = self.home / "MEGA" / "saves"
        for rel in ("bsnes/Mario (USA).srm", "bsnes/Mario (USA).state1", "bsnes/Mario (USA).state1.png",
                    "Flycast/Sonic (USA).srm", "Flycast/Sonic (USA).state"):
            p = self.saves / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(rel.encode())
        mock.patch.object(ra, "is_running", return_value=False).start()
        self.addCleanup(mock.patch.stopall)

    def plan(self, save, state, ss=True, st=True):
        return ra.plan_relocation(self.inst, Path(save), Path(state), ss, st, self.home)


class Basics(World):
    def test_read_and_resolve(self) -> None:
        s = ra.settings_of(self.inst, self.home)
        self.assertEqual(s["savefile_path"], str(self.saves))
        self.assertTrue(s["sort_savefiles_enable"])
        self.assertEqual(s["system_path"], str(self.base / "system"))
        self.assertEqual(s["content_path"], "")

    def test_write_keeps_other_lines_and_backs_up(self) -> None:
        backup = ra.write_cfg(self.inst.cfg, {"sort_savefiles_enable": False, "brand_new": "x"})
        text = self.inst.cfg.read_text()
        self.assertIn('sort_savefiles_enable = "false"', text)
        self.assertIn('video_fullscreen = "true"', text)
        self.assertIn("# comment", text)
        self.assertIn('brand_new = "x"', text)
        self.assertEqual(backup.read_text(), CFG)

    def test_classify(self) -> None:
        for name in ("a.state", "a.state3", "a.state.auto", "a.state1.png"):
            self.assertEqual(ra.classify(name), "state", name)
        for name in ("a.srm", "a.sav", "a.rtc", "a.png"):
            self.assertEqual(ra.classify(name), "save", name)

    def test_detects_a_custom_cfg_and_nothing_else(self) -> None:
        found = ra.detect_installs(home=self.home / "nowhere", env={}, platform="linux", custom=[str(self.base)])
        self.assertEqual([i.kind for i in found], ["custom"])
        self.assertEqual(ra.detect_installs(home=self.home / "nowhere", env={}, platform="linux"), [])


class Relocate(World):
    def test_new_folders_keep_the_core_folders_and_split_states(self) -> None:
        new_saves, new_states = self.home / "new" / "saves", self.home / "new" / "states"
        rel = self.plan(new_saves, new_states)
        self.assertEqual(rel.counts()["move"], 5)
        res = ra.apply_relocation(self.inst, rel, self.home / "j", self.home / "bk" / "b.zip")
        self.assertEqual((res["moved"], res["failed"]), (5, []))
        self.assertTrue((new_saves / "bsnes" / "Mario (USA).srm").is_file())
        self.assertTrue((new_states / "bsnes" / "Mario (USA).state1.png").is_file())
        self.assertTrue((new_states / "Flycast" / "Sonic (USA).state").is_file())
        self.assertFalse(self.saves.exists())                              # emptied folders are removed
        s = ra.settings_of(self.inst, self.home)
        self.assertEqual((s["savefile_path"], s["savestate_path"]), (str(new_saves), str(new_states)))
        self.assertTrue(Path(res["backup"]).is_file())
        self.assertTrue(Path(res["cfg_backup"]).is_file())

    def test_flatten_and_undo(self) -> None:
        rel = self.plan(self.saves, self.saves, False, False)
        res = ra.apply_relocation(self.inst, rel, self.home / "j")
        self.assertEqual(res["moved"], 5)
        self.assertTrue((self.saves / "Mario (USA).srm").is_file())
        self.assertFalse((self.saves / "bsnes").exists())
        self.assertFalse(ra.settings_of(self.inst, self.home)["sort_savefiles_enable"])
        out = ra.undo_relocation(Path(res["journal"]))
        self.assertEqual((out["restored"], out["skipped"], out["cfg_restored"]), (5, [], True))
        self.assertTrue((self.saves / "bsnes" / "Mario (USA).srm").is_file())
        self.assertTrue(ra.settings_of(self.inst, self.home)["sort_savefiles_enable"])
        self.assertEqual(ra.list_undo(self.home / "j"), [])

    def test_flat_files_cannot_be_given_a_core(self) -> None:
        flat = self.home / "flat"
        flat.mkdir()
        (flat / "G.srm").write_bytes(b"x")
        self.inst.cfg.write_text(CFG.replace("~/MEGA/saves", "~/flat").replace('sort_savefiles_enable = "true"', 'sort_savefiles_enable = "false"')
                                 .replace('sort_savestates_enable = "true"', 'sort_savestates_enable = "false"'))
        rel = self.plan(flat, flat, True, True)
        self.assertEqual(rel.counts()["needs_core"], 1)
        self.assertTrue(rel.notes)

    def test_a_file_in_the_way_is_never_overwritten(self) -> None:
        dest = self.home / "new"
        (dest / "bsnes").mkdir(parents=True)
        (dest / "bsnes" / "Mario (USA).srm").write_bytes(b"mine")
        rel = self.plan(dest, dest)
        self.assertEqual(rel.counts()["conflict"], 1)
        res = ra.apply_relocation(self.inst, rel, self.home / "j")
        self.assertEqual((dest / "bsnes" / "Mario (USA).srm").read_bytes(), b"mine")
        self.assertTrue((self.saves / "bsnes" / "Mario (USA).srm").is_file())
        self.assertEqual(res["moved"], 4)

    def test_refuses_while_retroarch_runs(self) -> None:
        rel = self.plan(self.home / "new", self.home / "new")
        with mock.patch.object(ra, "is_running", return_value=True):
            with self.assertRaises(RuntimeError):
                ra.apply_relocation(self.inst, rel, self.home / "j")
        self.assertTrue((self.saves / "bsnes" / "Mario (USA).srm").is_file())

    def test_override_files_are_reported(self) -> None:
        (self.base / "config" / "bsnes").mkdir(parents=True)
        (self.base / "config" / "bsnes" / "bsnes.cfg").write_text('savefile_directory = "/elsewhere"\n')
        self.assertEqual(len(ra.override_warnings(self.inst)), 1)


class Follow(World):
    def test_split_and_pairs(self) -> None:
        self.assertEqual(ra.split_save("Mario (USA).state1.png"), ("Mario (USA)", ".state1.png", "state"))
        self.assertEqual(ra.split_save("Super Mario Bros. 3 (USA).srm"), ("Super Mario Bros. 3 (USA)", ".srm", "save"))
        self.assertEqual(ra.split_save("G.state.auto"), ("G", ".state.auto", "state"))
        self.assertIsNone(ra.split_save("noext"))
        self.assertEqual(ra.pairs_from_moves([(Path("/a/Mario.sfc"), Path("/a/Mario (USA).sfc")), (Path("/a/x.gba"), Path("/b/x.gba"))]),
                         [("Mario", "Mario (USA)")])

    def test_saves_and_states_get_the_new_name_in_their_own_core_folder(self) -> None:
        (self.saves / "bsnes" / "Mario.srm").write_bytes(b"1")
        (self.saves / "bsnes" / "Mario.state2").write_bytes(b"2")
        (self.saves / "Snes9x").mkdir()
        (self.saves / "Snes9x" / "Mario.srm").write_bytes(b"3")
        ops = ra.plan_follow(self.inst, [("Mario", "Mario World")], "move", self.home)
        self.assertEqual(len(ops), 3)
        res = ra.apply_follow(ops, self.home / "j", install=self.inst)
        self.assertEqual((res["followed"], res["failed"]), (3, []))
        self.assertTrue((self.saves / "bsnes" / "Mario World.state2").is_file())
        self.assertTrue((self.saves / "Snes9x" / "Mario World.srm").is_file())
        self.assertFalse((self.saves / "bsnes" / "Mario.srm").exists())
        out = ra.undo_relocation(Path(res["journal"]))
        self.assertEqual((out["restored"], out["skipped"]), (3, []))
        self.assertTrue((self.saves / "bsnes" / "Mario.srm").is_file())

    def test_copy_mode_keeps_both_and_undo_removes_the_copy(self) -> None:
        ops = ra.plan_follow(self.inst, [("Mario (USA)", "Mario - New")], "copy", self.home)
        res = ra.apply_follow(ops, self.home / "j", install=self.inst)
        self.assertEqual(res["copied"], 3)
        self.assertTrue((self.saves / "bsnes" / "Mario (USA).srm").is_file())
        self.assertTrue((self.saves / "bsnes" / "Mario - New.srm").is_file())
        ra.undo_relocation(Path(res["journal"]))
        self.assertFalse((self.saves / "bsnes" / "Mario - New.srm").exists())
        self.assertTrue((self.saves / "bsnes" / "Mario (USA).srm").is_file())

    def test_a_file_with_the_new_name_is_never_overwritten_and_running_retroarch_waits(self) -> None:
        (self.saves / "bsnes" / "Mario.srm").write_bytes(b"old")
        (self.saves / "bsnes" / "Mario (USA).srm").write_bytes(b"mine")
        ops = ra.plan_follow(self.inst, [("Mario", "Mario (USA)")], "move", self.home)
        self.assertEqual([o.status for o in ops], ["conflict"])
        ops = ra.plan_follow(self.inst, [("Sonic (USA)", "Sonic 1")], "move", self.home)
        with mock.patch.object(ra, "is_running", return_value=True):
            res = ra.apply_follow(ops, self.home / "j", install=self.inst)
        self.assertTrue(res["skipped_running"])
        self.assertTrue((self.saves / "Flycast" / "Sonic (USA).srm").is_file())


INFO = '''display_name = "Sony - PlayStation (Test Core)"
corename = "TestCore"
supported_extensions = "bin|cue|chd"
firmware_count = 3
firmware0_path = "scph5501.bin"
firmware0_opt = "false"
firmware1_path = "sub/scph5502.bin"
firmware1_opt = "true"
firmware2_path = "other.bin"
firmware2_opt = "true"
notes = "(!) scph5501.bin (md5): %s|(!) scph5502.bin (md5): %s"
'''


class Bios(World):
    def setUp(self) -> None:
        super().setUp()
        import hashlib
        self.good = b"BIOS-ONE"
        self.md5 = hashlib.md5(self.good).hexdigest()
        (self.base / "cores").mkdir()
        (self.base / "cores" / "test_libretro.info").write_text(INFO % (self.md5, "0" * 32))
        (self.base / "system").mkdir()
        self.roms = self.home / "roms"
        self.roms.mkdir()
        from romorg import platforms
        self.psx = platforms.get_platform("Sony PlayStation")

    def test_cores_are_matched_to_the_system_and_read(self) -> None:
        infos = ra.core_infos(self.inst)
        self.assertEqual([c["core"] for c in ra.cores_for_platform(infos, self.psx)], ["TestCore"])
        from romorg import platforms
        self.assertEqual(ra.cores_for_platform(infos, platforms.get_platform("Sony PlayStation 2")), [])
        fw = {f["path"]: f for f in infos[0]["firmware"]}
        self.assertEqual(fw["scph5501.bin"]["md5"], self.md5)
        self.assertFalse(fw["scph5501.bin"]["optional"])
        self.assertTrue(fw["sub/scph5502.bin"]["optional"])

    def test_check_finds_a_renamed_bios_by_checksum_and_places_it(self) -> None:
        (self.roms / "SCPH-5501.BIN").write_bytes(self.good)            # another name, right checksum
        (self.roms / "scph5502.bin").write_bytes(b"not it")             # right name, wrong checksum: not accepted
        r = ra.check_bios(self.inst, self.psx, [self.roms], self.home)
        st = {i["path"]: i["status"] for i in r["cores"][0]["firmware"]}
        self.assertEqual(st, {"scph5501.bin": "found", "sub/scph5502.bin": "missing", "other.bin": "missing"})
        self.assertEqual(r["required_missing"], 1)
        items = [{"target": i["target"], "source": i["source"]} for i in r["cores"][0]["firmware"] if i["status"] == "found"]
        res = ra.apply_bios(self.inst, items, self.home / "j", "move", self.home)
        self.assertEqual((res["placed"], res["failed"]), (1, []))
        self.assertEqual((self.base / "system" / "scph5501.bin").read_bytes(), self.good)
        self.assertFalse((self.roms / "SCPH-5501.BIN").exists())
        again = ra.check_bios(self.inst, self.psx, [self.roms], self.home)
        self.assertEqual(again["cores"][0]["firmware"][0]["status"], "ok")
        self.assertEqual(again["required_missing"], 0)
        ra.undo_relocation(Path(res["journal"]))
        self.assertTrue((self.roms / "SCPH-5501.BIN").is_file())

    def test_a_wrong_file_in_place_is_reported_and_never_replaced(self) -> None:
        (self.base / "system" / "scph5501.bin").write_bytes(b"corrupt")
        (self.roms / "scph5501.bin").write_bytes(self.good)
        r = ra.check_bios(self.inst, self.psx, [self.roms], self.home)
        self.assertEqual(r["cores"][0]["firmware"][0]["status"], "wrong")
        res = ra.apply_bios(self.inst, [{"target": str(self.base / "system" / "scph5501.bin"), "source": str(self.roms / "scph5501.bin")}],
                            self.home / "j", "move", self.home)
        self.assertEqual(res["placed"], 0)
        self.assertEqual((self.base / "system" / "scph5501.bin").read_bytes(), b"corrupt")

    def test_only_the_system_folder_is_ever_written(self) -> None:
        (self.roms / "x.bin").write_bytes(b"x")
        res = ra.apply_bios(self.inst, [{"target": str(self.home / "elsewhere.bin"), "source": str(self.roms / "x.bin")}],
                            self.home / "j", "move", self.home)
        self.assertEqual(res["placed"], 0)
        self.assertTrue((self.roms / "x.bin").exists())


class Shared(World):
    def written(self, rel: str) -> str:
        """How a folder under the home folder is written in the cfg: ``~/...``, but absolute on Windows (RetroArch
        there does not expand ``~``; see ``ra.to_cfg_path``)."""
        return str(self.home / rel) if os.name == "nt" else "~/" + rel

    def setUp(self) -> None:
        super().setUp()
        self.assets = self.home / "MEGA" / "assets"
        for n in ("cht", "playlists", "remaps", "thumbnails"):
            (self.assets / n).mkdir(parents=True)
        (self.assets / "cht" / "a.cht").write_text("x")
        (self.assets / "playlists" / "builtin").mkdir()
        (self.assets / "playlists" / "builtin" / "content_history.lpl").write_text("{}")
        self.inst.cfg.write_text(CFG + 'cheat_database_path = ":/cheats"\nplaylist_directory = "~/MEGA/assets/playlists"\n'
                                 'thumbnails_directory = "/somewhere/else"\ncontent_history_path = "default"\n')

    def test_states_are_told_apart(self) -> None:
        rows = {r["key"]: r for r in ra.shared_folders(self.inst, str(self.assets), self.home)}
        self.assertEqual(rows["cheat_database_path"]["status"], "unset")
        self.assertEqual(rows["playlist_directory"]["status"], "ok")
        self.assertEqual(rows["thumbnails_directory"]["status"], "set")
        self.assertEqual(rows["content_database_path"]["status"], "none")          # no rdb folder in the base
        self.assertEqual(rows["cheat_database_path"]["want_cfg"], self.written("MEGA/assets/cht"))
        self.assertEqual(ra.shared_base(self.inst, self.home), str(self.assets))

    def test_apply_changes_only_the_ticked_settings_and_undo_restores_them(self) -> None:
        rows = ra.shared_folders(self.inst, str(self.assets), self.home)
        res = ra.apply_shared(self.inst, rows, ["cheat_database_path", "content_database_path"], self.home / "j", self.home)
        self.assertEqual(res["changed"], ["cheat_database_path"])
        cfg = ra.read_cfg(self.inst.cfg)
        self.assertEqual(cfg["cheat_database_path"], self.written("MEGA/assets/cht"))
        self.assertEqual(cfg["thumbnails_directory"], "/somewhere/else")            # not ticked: untouched
        out = ra.undo_relocation(Path(res["journal"]))
        self.assertTrue(out["cfg_restored"])
        self.assertEqual(ra.read_cfg(self.inst.cfg)["cheat_database_path"], ":/cheats")

    def test_the_history_lists_follow_the_playlist_folder_and_running_retroarch_refuses(self) -> None:
        self.inst.cfg.write_text(CFG + 'playlist_directory = ":/playlists"\ncontent_history_path = ":/playlists/content_history.lpl"\n')
        rows = ra.shared_folders(self.inst, str(self.assets), self.home)
        with mock.patch.object(ra, "is_running", return_value=True):
            with self.assertRaises(RuntimeError):
                ra.apply_shared(self.inst, rows, ["playlist_directory"], self.home / "j", self.home)
        res = ra.apply_shared(self.inst, rows, ["playlist_directory"], self.home / "j", self.home)
        self.assertEqual(res["changed"], ["content_history_path", "playlist_directory"])
        self.assertEqual(ra.read_cfg(self.inst.cfg)["content_history_path"],
                         self.written("MEGA/assets/playlists/builtin/content_history.lpl"))


class BiosAll(Bios):
    def test_all_configured_systems_in_one_pass(self) -> None:
        from romorg import platforms
        infos = ra.core_infos(self.inst)
        cores = ra.cores_for_platforms(infos, [self.psx, platforms.get_platform("Sony PlayStation")])
        self.assertEqual([c["core"] for c in cores], ["TestCore"])                       # each core once
        (self.roms / "sub").mkdir()
        (self.roms / "sub" / "scph-5501.bin").write_bytes(self.good)
        r = ra.check_bios_cores(self.inst, cores, [self.roms, self.roms / "sub"], self.home, [self.psx])
        self.assertTrue(r["complete"])
        self.assertEqual(r["cores"][0]["serves"], ["Sony PlayStation"])
        self.assertEqual(r["cores"][0]["firmware"][0]["status"], "found")

    def test_nested_search_folders_are_read_once_and_a_non_bios_looking_file_is_not_hashed(self) -> None:
        (self.roms / "x").mkdir()
        self.assertEqual(ra._outermost([self.roms, self.roms / "x"]), [self.roms.resolve()])
        (self.roms / "Some Game (USA).gba").write_bytes(self.good)                  # right bytes, but not BIOS-like: skipped
        cores = ra.core_infos(self.inst)
        r = ra.check_bios_cores(self.inst, cores, [self.roms], self.home, [self.psx])
        self.assertEqual(r["cores"][0]["firmware"][0]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
