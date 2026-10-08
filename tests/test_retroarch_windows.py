"""RetroArch on Windows (pass 4): finding the installs, the process check, ``retroarch.cfg`` byte for byte, moves that
never leave a file twice, read-only and open files, case-insensitive names. Most of it runs on every platform (the
Windows layouts are built in a temporary folder); what needs Windows itself is marked."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")       # no test ever works on the user's own RetroArch

from romorg import platforms, retroarch as ra  # noqa: E402

WINDOWS = os.name == "nt"

# what RetroArch for Windows itself writes (a cut of a real portable install's file): LF line ends, no byte order mark,
# ":" for the folder of retroarch.exe, backslashes
REAL_CFG = (b'assets_directory = ":\\assets"\n'
            b'cache_directory = "C:\\Users\\U\\AppData\\Local\\Temp"\n'
            b'cheat_database_path = ":\\cheats"\n'
            b'libretro_info_path = ":\\info"\n'
            b'playlist_directory = ":\\playlists"\n'
            b'rgui_browser_directory = "default"\n'
            b'savefile_directory = ":\\saves"\n'
            b'savefiles_in_content_dir = "false"\n'
            b'savestate_directory = ":\\states"\n'
            b'savestates_in_content_dir = "false"\n'
            b'sort_savefiles_enable = "true"\n'
            b'sort_savestates_enable = "true"\n'
            b'system_directory = ":\\system"\n'
            b'video_fullscreen = "false"\n')


def case_insensitive(folder: Path) -> bool:
    probe = folder / "CaseProbe.tmp"
    probe.write_bytes(b"")
    try:
        return (folder / "caseprobe.TMP").exists()
    finally:
        probe.unlink()


class Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        self.home = Path(self._tmp.name).resolve()
        mock.patch.object(ra, "is_running", return_value=False).start()
        self.addCleanup(mock.patch.stopall)

    def _cleanup(self) -> None:
        for p in self.home.rglob("*"):                        # read-only files do not stop the clean-up
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if p.is_dir() else 0))
            except OSError:
                pass
        self._tmp.cleanup()

    def portable(self, name: str = "RetroArch-Win64", cfg: bytes = REAL_CFG, exe: bool = True) -> ra.Install:
        base = self.home / name
        base.mkdir(parents=True, exist_ok=True)
        (base / "retroarch.cfg").write_bytes(cfg)
        if exe:
            (base / "retroarch.exe").write_bytes(b"")
        return ra.Install("portable:0", "portable", "RA", base / "retroarch.cfg", base)

    def with_saves(self, inst: ra.Install, names=("Flycast/Sonic (USA).srm", "PCSX-ReARMed/Crash (USA).srm")) -> Path:
        saves = inst.base / "saves"
        for rel in names:
            p = saves / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(rel.encode())
        (inst.base / "states").mkdir(exist_ok=True)
        return saves


class FakeSystem:
    def __init__(self, uninstall=(), steam=(), drives=(), running=()) -> None:
        self._u, self._s, self._d, self._r = list(uninstall), list(steam), list(drives), list(running)

    def uninstall_dirs(self): return self._u
    def steam_roots(self): return self._s
    def drives(self): return self._d
    def running_dirs(self): return self._r


class DetectWindows(Tmp):
    """The layouts are real, the drive is a temporary folder: ``detect_installs`` only looks where ``home``, ``env`` and
    ``system`` point, so this runs on Linux too."""

    def make(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "retroarch.cfg").write_bytes(REAL_CFG)
        (folder / "retroarch.exe").write_bytes(b"")
        return folder

    def find(self, env=None, system=None, home=None) -> list:
        return ra.detect_installs(home=home or self.home / "Users" / "u", env=env or {}, platform="win32", system=system)

    def test_a_made_up_home_and_environment_never_reach_the_real_machine(self) -> None:
        with mock.patch.object(ra, "WinSystem", side_effect=AssertionError("the real machine was asked")):
            self.assertEqual(self.find(), [])
            self.assertEqual(ra.detect_installs(home=self.home, env={}, platform="win32"), [])

    def test_the_test_suite_switch_turns_the_search_of_this_machine_off(self) -> None:
        with mock.patch.dict(os.environ, {"ROMORG_RETROARCH_DETECT": "0"}), \
                mock.patch.object(ra, "WinSystem", side_effect=AssertionError("the real machine was asked")):
            self.assertEqual(ra.detect_installs(), [])
            d = self.make(self.home / "mine")
            self.assertEqual([i.cfg for i in ra.detect_installs(custom=[str(d)])], [d / "retroarch.cfg"])

    def test_chocolatey_tools_folder(self) -> None:
        d = self.make(self.home / "tools" / "RetroArch-Win64")
        found = self.find(env={"ChocolateyToolsLocation": str(self.home / "tools")})
        self.assertEqual([(i.kind, i.cfg, i.base) for i in found], [("portable", d / "retroarch.cfg", d)])
        # without the variable: <SystemDrive>\tools (Chocolatey's default); no drive letter is ever made up
        places = [str(folder) for _k, _l, folder in ra._windows_candidates(self.home, {"SystemDrive": "Q:"}, None)]
        for want in ("tools/RetroArch-Win64", "tools/RetroArch", "RetroArch-Win64", "RetroArch", "RetroBat/emulators/retroarch"):
            self.assertIn(str(Path("Q:\\") / want), places)
        nowhere = [str(folder) for _k, _l, folder in ra._windows_candidates(self.home, {}, None)]
        self.assertTrue(all(p.startswith(str(self.home)) for p in nowhere), nowhere)

    def test_scoop_per_user_and_global_and_a_shim_on_path(self) -> None:
        user = self.make(self.home / "Users" / "u" / "scoop" / "apps" / "retroarch" / "current")
        glob = self.make(self.home / "ProgramData" / "scoop" / "apps" / "retroarch" / "current")
        moved = self.make(self.home / "elsewhere" / "apps" / "retroarch" / "1.19.1")
        shims = self.home / "elsewhere" / "shims"
        shims.mkdir()
        (shims / "retroarch.exe").write_bytes(b"MZ shim")
        (shims / "retroarch.shim").write_text(f'path = "{moved / "retroarch.exe"}"\n')
        found = self.find(env={"ProgramData": str(self.home / "ProgramData"), "PATH": os.pathsep.join(["", str(shims), str(self.home / "none")])})
        self.assertEqual({i.base for i in found}, {user, glob, moved})

    def test_a_folder_on_path_with_the_program_and_its_config(self) -> None:
        d = self.make(self.home / "apps" / "ra")
        shim_only = self.home / "chocolatey" / "bin"            # Chocolatey's shim: the exe alone, no config next to it
        shim_only.mkdir(parents=True)
        (shim_only / "retroarch.exe").write_bytes(b"MZ")
        found = self.find(env={"Path": os.pathsep.join([str(shim_only), f'"{d}"'])})      # the name is "Path" on Windows
        self.assertEqual([i.base for i in found], [d])

    def test_the_installer_appdata_and_its_uninstall_entry(self) -> None:
        roaming = self.make(self.home / "Roaming" / "RetroArch")
        chosen = self.make(self.home / "Games" / "RA")
        found = self.find(env={"APPDATA": str(self.home / "Roaming")}, system=FakeSystem(uninstall=[chosen]))
        self.assertEqual([(i.kind, i.base) for i in found], [("windows", roaming), ("windows", chosen)])
        # no APPDATA in the environment: the usual place under the home folder
        usual = self.make(self.home / "Users" / "u" / "AppData" / "Roaming" / "RetroArch")
        self.assertEqual([i.base for i in self.find()], [usual])

    def test_registry_values_name_the_folder(self) -> None:
        f = ra._dir_of_registry_value
        self.assertEqual(str(f(r"C:\RetroArch-Win64")), str(Path(r"C:\RetroArch-Win64")))
        self.assertEqual(str(f("C:\\RetroArch-Win64\\")), str(Path(r"C:\RetroArch-Win64")))
        self.assertEqual(str(f(r'"C:\Program Files\RetroArch\uninstall.exe" /S')), str(Path(r"C:\Program Files\RetroArch")))
        self.assertEqual(str(f(r"D:\Games\RetroArch\uninstall.exe")), str(Path(r"D:\Games\RetroArch")))
        self.assertEqual(str(f(r"D:\Games\RetroArch\retroarch.exe,0")), str(Path(r"D:\Games\RetroArch")))
        self.assertIsNone(f("   "))

    def test_steam_libraries_on_any_drive_from_the_registry_path(self) -> None:
        steam = self.home / "c" / "Program Files (x86)" / "Steam"
        lib = self.home / "e" / "SteamLibrary"
        (steam / "steamapps").mkdir(parents=True)
        escaped = str(lib).replace("\\", "\\\\")
        (steam / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n'
            % (str(steam).replace("\\", "\\\\"), escaped))
        d = self.make(lib / "steamapps" / "common" / "RetroArch")
        found = self.find(system=FakeSystem(steam=[steam]))
        self.assertEqual([(i.kind, i.base) for i in found], [("steam", d)])
        # the same Steam folder through Program Files (no registry)
        found = self.find(env={"ProgramFiles(x86)": str(steam.parent)})
        self.assertEqual([(i.kind, i.base) for i in found], [("steam", d)])

    def test_drive_roots_front_ends_and_a_running_one_each_once(self) -> None:
        drive = self.home / "f"
        a = self.make(drive / "RetroArch-Win64")
        b = self.make(drive / "RetroBat" / "emulators" / "retroarch")
        c = self.make(self.home / "Users" / "u" / "LaunchBox" / "Emulators" / "RetroArch")
        d = self.make(self.home / "odd place")
        e = self.make(self.home / "Roaming" / "EmuDeck" / "Emulators" / "RetroArch")
        found = self.find(env={"APPDATA": str(self.home / "Roaming")}, system=FakeSystem(drives=[drive], running=[d, a]))
        self.assertEqual(sorted(i.base for i in found), sorted([a, b, c, d, e]))            # a: found twice, listed once
        self.assertEqual(len({i.id for i in found}), 5)
        self.assertTrue(all(i.label == f"RetroArch ({i.base})" for i in found))

    def test_a_program_or_any_other_file_stands_for_its_folder_and_never_becomes_the_config(self) -> None:
        d = self.make(self.home / "ra")
        (d / "notes.txt").write_text("x")
        for pick in (d / "retroarch.exe", d / "notes.txt", d, d / "retroarch.cfg"):
            found = ra.detect_installs(platform="none", custom=[str(pick)])
            self.assertEqual([i.cfg for i in found], [d / "retroarch.cfg"], pick)
        other = self.home / "empty"
        other.mkdir()
        (other / "notes.txt").write_text("x")
        self.assertEqual(ra.detect_installs(platform="none", custom=[str(other / "notes.txt"), str(other), "", "\0bad"]), [])

    @unittest.skipUnless(WINDOWS, "the real registry, drives and process list")
    def test_the_real_machine_can_be_asked(self) -> None:
        w = ra.WinSystem()
        self.assertTrue(any(str(d).upper().startswith(os.environ.get("SystemDrive", "C:").upper()) for d in w.drives()))
        self.assertIsInstance(w.uninstall_dirs(), list)
        self.assertIsInstance(w.steam_roots(), list)
        self.assertIsInstance(w.running_dirs(), list)
        mine = Path(ra._win_process_path(os.getpid()))
        self.assertTrue(mine.is_file() and mine.name.lower().startswith("python"), mine)


class Running(unittest.TestCase):
    def test_image_names(self) -> None:
        for name in ("retroarch.exe", "RetroArch.exe", "RETROARCH.EXE", "retroarch_debug.exe", r"C:\x\retroarch.exe"):
            self.assertTrue(ra._is_retroarch_image(name), name)
        for name in ("RetroArch-Win64-setup.exe", "retroarch", "notretroarch.exe", "retroarch.exe.bak", "retroarch-cli.exe", ""):
            self.assertFalse(ra._is_retroarch_image(name), name)

    @unittest.skipUnless(WINDOWS, "the Windows process list")
    def test_windows_reads_the_process_list_and_starts_no_program(self) -> None:
        with mock.patch("subprocess.run", side_effect=AssertionError("a child process (a console window)")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("a child process (a console window)")):
            with mock.patch.object(ra, "_win_processes", return_value=[(4, "System"), (99, "RetroArch.exe")]):
                self.assertTrue(ra.is_running())
            with mock.patch.object(ra, "_win_processes", return_value=[(4, "System"), (9, "RetroArch-1.19-setup.exe")]):
                self.assertFalse(ra.is_running())
            with mock.patch.object(ra, "_win_processes", side_effect=OSError("no snapshot")):
                self.assertFalse(ra.is_running())
            names = [n.lower() for _pid, n in ra._win_processes()]              # the real list: this Python is in it
            self.assertIn(os.getpid(), [pid for pid, _n in ra._win_processes()])
            self.assertTrue(any(n.startswith("py") for n in names))
            self.assertIsInstance(ra.is_running(), bool)


class CfgBytes(Tmp):
    def test_a_real_windows_cfg_is_read_and_resolved(self) -> None:
        inst = self.portable()
        s = ra.settings_of(inst, self.home)
        self.assertEqual((s["savefile_path"], s["savestate_path"], s["system_path"], s["content_path"]),
                         (str(inst.base / "saves"), str(inst.base / "states"), str(inst.base / "system"), ""))
        self.assertTrue(s["sort_savefiles_enable"])
        self.assertEqual(ra.resolve(":", inst), inst.base)
        self.assertEqual(ra.resolve(":/saves/x", inst), inst.base / "saves" / "x")
        self.assertIsNone(ra.resolve("default", inst))

    def test_only_the_changed_line_changes_lf(self) -> None:
        inst = self.portable()
        ra.write_cfg(inst.cfg, {"savefile_directory": ":\\saves"})                  # the value it has: not one byte differs
        self.assertEqual(inst.cfg.read_bytes(), REAL_CFG)
        ra.write_cfg(inst.cfg, {"savefile_directory": "D:\\saves", "sort_savefiles_enable": False})
        self.assertEqual(inst.cfg.read_bytes(), REAL_CFG.replace(b'savefile_directory = ":\\saves"', b'savefile_directory = "D:\\saves"')
                         .replace(b'sort_savefiles_enable = "true"', b'sort_savefiles_enable = "false"'))

    def test_crlf_line_ends_stay_crlf_also_for_a_new_key(self) -> None:
        crlf = REAL_CFG.replace(b"\n", b"\r\n")
        inst = self.portable(cfg=crlf)
        ra.write_cfg(inst.cfg, {"video_fullscreen": True, "brand_new": "x"})
        self.assertEqual(inst.cfg.read_bytes(), crlf.replace(b'video_fullscreen = "false"', b'video_fullscreen = "true"') + b'brand_new = "x"\r\n')
        self.assertEqual(ra.read_cfg(inst.cfg)["brand_new"], "x")

    def test_a_last_line_without_a_line_end_and_a_removed_key(self) -> None:
        inst = self.portable(cfg=b'a = "1"\nb = "2"')
        ra.write_cfg(inst.cfg, {"c": "3"})
        self.assertEqual(inst.cfg.read_bytes(), b'a = "1"\nb = "2"\nc = "3"\n')
        ra.write_cfg(inst.cfg, {"b": None, "zz": None})
        self.assertEqual(inst.cfg.read_bytes(), b'a = "1"\nc = "3"\n')

    def test_a_byte_order_mark_and_bytes_that_are_not_utf8_survive(self) -> None:
        data = b"\xef\xbb\xbf" + b'savefile_directory = ":\\saves"\nodd = "caf\xe9"\n'
        inst = self.portable(cfg=data)
        self.assertEqual(ra.read_cfg(inst.cfg)["savefile_directory"], ":\\saves")          # the first key, not "\ufeffsave..."
        ra.write_cfg(inst.cfg, {"savefile_directory": "E:\\s"})
        self.assertEqual(inst.cfg.read_bytes(), b"\xef\xbb\xbf" + b'savefile_directory = "E:\\s"\nodd = "caf\xe9"\n')

    def test_two_changes_in_one_second_keep_two_backups_with_legal_names(self) -> None:
        inst = self.portable()
        with mock.patch.object(ra.time, "strftime", return_value="20261008-120000"):
            one = ra.write_cfg(inst.cfg, {"video_fullscreen": True})
            two = ra.write_cfg(inst.cfg, {"video_fullscreen": False})
        self.assertNotEqual(one, two)
        self.assertEqual(one.read_bytes(), REAL_CFG)                                      # the first backup is the original
        self.assertIn(b'video_fullscreen = "true"', two.read_bytes())
        real = ra.write_cfg(inst.cfg, {"video_fullscreen": True})
        self.assertRegex(real.name, r'^retroarch\.cfg\.romorg-backup-\d{8}-\d{6}(-\d+)?$')
        self.assertFalse(set(real.name) & set('<>:"/\\|?*'))

    @unittest.skipUnless(WINDOWS, "how RetroArch for Windows writes folders")
    def test_windows_writes_a_folder_inside_the_install_the_way_retroarch_does(self) -> None:
        inst = self.portable()
        self.assertEqual(ra.to_cfg_path(inst.base / "saves" / "new", self.home, inst), ":\\saves\\new")
        self.assertEqual(ra.to_cfg_path(Path(str(inst.base).upper()) / "Saves", self.home, inst), ":\\Saves")
        self.assertEqual(ra.to_cfg_path(Path(str(inst.base).replace("\\", "/") + "/saves"), self.home, inst), ":\\saves")
        outside = self.home / "elsewhere" / "saves"
        self.assertEqual(ra.to_cfg_path(outside, self.home, inst), str(outside))            # no "~" on Windows
        self.assertEqual(ra.to_cfg_path(Path(str(inst.base) + "2") / "saves", self.home, inst), str(inst.base) + "2\\saves")
        self.assertEqual(ra.to_cfg_path(Path(r"\\server\share\saves"), self.home, inst), r"\\server\share\saves")
        no_exe = self.portable("custom-cfg", exe=False)                  # ":" would be another folder: the full path
        self.assertEqual(ra.to_cfg_path(no_exe.base / "saves", self.home, no_exe), str(no_exe.base / "saves"))
        # and the value written is read back as the same folder
        self.assertEqual(ra.resolve(ra.to_cfg_path(inst.base / "saves" / "new", self.home, inst), inst), inst.base / "saves" / "new")

    @unittest.skipIf(WINDOWS, "the form used everywhere else")
    def test_elsewhere_the_home_folder_is_still_written_as_a_tilde(self) -> None:
        inst = self.portable()
        self.assertEqual(ra.to_cfg_path(inst.base / "saves", self.home, inst), "~/RetroArch-Win64/saves")
        self.assertEqual(ra.to_cfg_path(Path("/mnt/sd/saves"), self.home, inst), "/mnt/sd/saves")

    @unittest.skipUnless(WINDOWS, "Windows spelling of folders")
    def test_relocation_inside_a_portable_install_keeps_it_portable(self) -> None:
        inst = self.portable()
        self.with_saves(inst)
        new = Path(str(inst.base).upper()) / "SAVES"                    # the folder it has, typed another way: nothing moves
        rel = ra.plan_relocation(inst, new, inst.base / "states", True, True, self.home)
        self.assertEqual((rel.counts()["move"], rel.counts()["ok"]), (0, 2))
        rel = ra.plan_relocation(inst, inst.base / "data" / "saves", inst.base / "data" / "states", True, True, self.home)
        res = ra.apply_relocation(inst, rel, self.home / "j")
        self.assertEqual((res["moved"], res["failed"]), (2, []))
        cfg = ra.read_cfg(inst.cfg)
        self.assertEqual((cfg["savefile_directory"], cfg["savestate_directory"]), (":\\data\\saves", ":\\data\\states"))
        self.assertEqual(inst.cfg.read_bytes().count(b"\r"), 0)
        self.assertTrue(ra.undo_relocation(Path(res["journal"]))["cfg_restored"])
        self.assertEqual(inst.cfg.read_bytes(), REAL_CFG)


class Moves(Tmp):
    def relocate(self, inst: ra.Install, **kw):
        new = self.home / "new"
        rel = ra.plan_relocation(inst, new / "saves", new / "states", True, True, self.home)
        return new, ra.apply_relocation(inst, rel, self.home / "j", **kw)

    def test_another_drive_copies_and_a_source_that_cannot_be_removed_leaves_no_second_copy(self) -> None:
        inst = self.portable()
        saves = self.with_saves(inst)
        real_unlink = os.unlink
        stuck = saves / "Flycast" / "Sonic (USA).srm"

        def unlink(path, *a, **k):
            if os.path.normcase(str(path)) == os.path.normcase(str(stuck)):
                raise PermissionError(errno.EACCES, "in use by another process", str(path))
            return real_unlink(path, *a, **k)

        other_drive = OSError(errno.EXDEV, "not the same device")
        with mock.patch.object(ra.os, "link", side_effect=other_drive), mock.patch.object(ra.os, "rename", side_effect=other_drive), \
                mock.patch.object(ra.os, "unlink", side_effect=unlink):
            new, res = self.relocate(inst)
        self.assertEqual(res["moved"], 1)
        self.assertEqual([Path(f["path"]) for f in res["failed"]], [stuck])
        self.assertTrue(stuck.is_file())
        self.assertFalse((new / "saves" / "Flycast" / "Sonic (USA).srm").exists())          # not in both places
        self.assertEqual((new / "saves" / "PCSX-ReARMed" / "Crash (USA).srm").read_bytes(), b"PCSX-ReARMed/Crash (USA).srm")
        self.assertFalse((saves / "PCSX-ReARMed").exists())
        self.assertIsNone(res["cfg_backup"])                                               # a failure: the config stays
        self.assertEqual(inst.cfg.read_bytes(), REAL_CFG)
        self.assertEqual(ra.undo_relocation(Path(res["journal"]))["restored"], 1)

    def test_a_rename_that_fails_on_the_same_drive_is_not_turned_into_a_copy(self) -> None:
        inst = self.portable()
        saves = self.with_saves(inst)
        denied = PermissionError(errno.EACCES, "denied")
        with mock.patch.object(ra.os, "link", side_effect=OSError(errno.EPERM, "no hard links here")), \
                mock.patch.object(ra.os, "rename", side_effect=denied), \
                mock.patch.object(ra.shutil, "copy2", side_effect=AssertionError("copied")), \
                mock.patch.object(ra.shutil, "move", side_effect=AssertionError("shutil.move copies and may leave both")):
            new, res = self.relocate(inst)
        self.assertEqual((res["moved"], len(res["failed"])), (0, 2))
        self.assertTrue((saves / "Flycast" / "Sonic (USA).srm").is_file())
        self.assertEqual([p for p in new.rglob("*") if p.is_file()], [])

    def test_no_hard_links_but_one_drive_is_a_plain_rename(self) -> None:
        inst = self.portable()
        self.with_saves(inst)
        with mock.patch.object(ra.os, "link", side_effect=OSError(errno.EPERM, "no hard links here")):
            new, res = self.relocate(inst)
        self.assertEqual((res["moved"], res["failed"]), (2, []))
        self.assertTrue((new / "saves" / "Flycast" / "Sonic (USA).srm").is_file())

    @unittest.skipUnless(WINDOWS, "a file held open blocks its removal only on Windows")
    def test_a_save_open_in_another_program_fails_alone_and_is_not_duplicated(self) -> None:
        inst = self.portable()
        saves = self.with_saves(inst)
        held = saves / "Flycast" / "Sonic (USA).srm"
        with open(held, "rb"):
            new, res = self.relocate(inst, backup_zip=self.home / "bk" / "b.zip")
            self.assertEqual(res["moved"], 1)
            self.assertEqual([Path(f["path"]) for f in res["failed"]], [held])
            self.assertTrue(held.is_file())
            self.assertFalse((new / "saves" / "Flycast" / "Sonic (USA).srm").exists())
            ops = ra.plan_follow(inst, [("Sonic (USA)", "Sonic 1")], "move", self.home)
            out = ra.apply_follow(ops, self.home / "j", install=inst)
            self.assertEqual((out["followed"], len(out["failed"])), (0, 1))
            self.assertEqual(sorted(p.name for p in held.parent.iterdir()), ["Sonic (USA).srm"])
        self.assertEqual(inst.cfg.read_bytes(), REAL_CFG)

    @unittest.skipUnless(WINDOWS, "the read-only attribute stops a delete only on Windows")
    def test_read_only_saves_move_and_stay_read_only_and_come_back(self) -> None:
        inst = self.portable()
        saves = self.with_saves(inst)
        ro = saves / "Flycast" / "Sonic (USA).srm"
        os.chmod(ro, stat.S_IREAD)
        new, res = self.relocate(inst)
        self.assertEqual((res["moved"], res["failed"]), (2, []))
        moved = new / "saves" / "Flycast" / "Sonic (USA).srm"
        self.assertFalse(ro.exists())
        self.assertFalse(os.access(moved, os.W_OK))
        out = ra.undo_relocation(Path(res["journal"]))
        self.assertEqual((out["restored"], out["skipped"]), (2, []))
        self.assertFalse(os.access(ro, os.W_OK))
        # a copy made for a build elsewhere is removed again by undo even when it is read-only
        ops = ra.plan_follow(inst, [("Sonic (USA)", "Sonic 1")], "copy", self.home)
        out = ra.apply_follow(ops, self.home / "j", install=inst)
        self.assertEqual(out["copied"], 1)
        self.assertEqual(ra.undo_relocation(Path(out["journal"]))["skipped"], [])
        self.assertFalse((saves / "Flycast" / "Sonic 1.srm").exists())

    def test_the_backup_zip_takes_old_time_stamps_long_and_non_ascii_names(self) -> None:
        inst = self.portable()
        odd = "Pok\u00e9mon \u2013 \u30dd\u30b1\u30e2\u30f3 (\u65e5\u672c) " + "x" * 120 + ".srm"
        saves = self.with_saves(inst, ("Flycast/Sonic (USA).srm", "mGBA/" + odd))
        os.utime(saves / "Flycast" / "Sonic (USA).srm", (86400, 86400))                    # 1970: before what a zip can say
        new, res = self.relocate(inst, backup_zip=self.home / "bk" / "b.zip")
        self.assertEqual((res["moved"], res["failed"]), (2, []))
        with zipfile.ZipFile(res["backup"]) as z:
            names = z.namelist()
            manifest = json.loads(z.read("manifest.json"))
            self.assertIn(odd, [n.rsplit("/", 1)[-1] for n in names])
            self.assertEqual(z.read(next(n for n in names if n.endswith(odd))), ("mGBA/" + odd).encode())
        self.assertEqual(sorted(m["name"] for m in manifest), sorted(["Sonic (USA).srm", odd]))
        self.assertTrue((new / "saves" / "mGBA" / odd).is_file())

    def test_a_second_backup_in_the_same_second_does_not_replace_the_first(self) -> None:
        inst = self.portable()
        self.with_saves(inst)
        z = self.home / "bk" / "saves-20261008-120000.zip"
        z.parent.mkdir()
        z.write_bytes(b"the earlier backup")
        new, res = self.relocate(inst, backup_zip=z)
        self.assertEqual(z.read_bytes(), b"the earlier backup")
        self.assertNotEqual(Path(res["backup"]), z)
        self.assertTrue(zipfile.is_zipfile(res["backup"]))

    def test_two_changes_in_one_second_keep_two_journals(self) -> None:
        inst = self.portable()
        self.with_saves(inst)
        with mock.patch.object(ra.time, "strftime", return_value="20261008-120000"):
            a = ra.apply_follow(ra.plan_follow(inst, [("Sonic (USA)", "Sonic 1")], "move", self.home), self.home / "j", install=inst)
            b = ra.apply_follow(ra.plan_follow(inst, [("Crash (USA)", "Crash 1")], "move", self.home), self.home / "j", install=inst)
            new, c = self.relocate(inst)
            new2 = self.home / "again"
            d = ra.apply_relocation(inst, ra.plan_relocation(inst, new2, new2, True, True, self.home), self.home / "j")
        journals = [a["journal"], b["journal"], c["journal"], d["journal"]]
        self.assertEqual(len(set(journals)), 4)
        self.assertEqual(len(ra.list_undo(self.home / "j")), 4)
        self.assertEqual(len(json.loads(Path(a["journal"]).read_text())["moves"]), 1)
        for i, j in enumerate(journals):                                    # written in this order, a moment apart
            os.utime(j, (1_700_000_000 + i, 1_700_000_000 + i))
        self.assertEqual([x["journal"] for x in ra.list_undo(self.home / "j")], journals[::-1])       # the newest first
        older = self.home / "j" / "saves-20250101-000000.json"
        older.write_text(json.dumps({"install": str(inst.cfg), "moves": [], "undone": False}))
        self.assertEqual(ra.list_undo(self.home / "j")[-1]["name"], older.name)           # whatever its file time says
        (self.home / "j" / "saves-20250101-000001.json").write_text("[1, 2]")              # not a journal: left out
        self.assertEqual(len(ra.list_undo(self.home / "j")), 5)

    def test_files_moved_before_the_config_could_not_be_written_can_still_be_undone(self) -> None:
        inst = self.portable()
        saves = self.with_saves(inst)
        with mock.patch.object(ra, "write_cfg", side_effect=PermissionError(errno.EACCES, "retroarch.cfg is read-only")):
            new, res = self.relocate(inst)
        self.assertEqual(res["moved"], 2)
        self.assertIn("read-only", res["cfg_error"])
        self.assertEqual([f["path"] for f in res["failed"]], [str(inst.cfg)])
        self.assertTrue(Path(res["journal"]).is_file())
        out = ra.undo_relocation(Path(res["journal"]))
        self.assertEqual((out["restored"], out["skipped"]), (2, []))
        self.assertTrue((saves / "Flycast" / "Sonic (USA).srm").is_file())

    def test_an_undo_that_was_stopped_half_way_can_be_finished(self) -> None:
        inst = self.portable()
        saves = self.with_saves(inst)
        new, res = self.relocate(inst)
        blocker = saves / "Flycast" / "Sonic (USA).srm"
        blocker.parent.mkdir(parents=True)
        blocker.write_bytes(b"in the way")
        out = ra.undo_relocation(Path(res["journal"]))
        self.assertEqual((out["restored"], len(out["skipped"]), out["cfg_restored"]), (1, 1, False))
        blocker.unlink()
        out = ra.undo_relocation(Path(res["journal"]))                       # the file that is back already is no obstacle
        self.assertEqual((out["restored"], out["skipped"], out["cfg_restored"]), (1, [], True))
        self.assertEqual(inst.cfg.read_bytes(), REAL_CFG)
        self.assertEqual(ra.list_undo(self.home / "j"), [])

    def test_undo_keeps_what_retroarch_saved_in_the_config_since(self) -> None:
        inst = self.portable()
        self.with_saves(inst)
        new, res = self.relocate(inst)
        ra.write_cfg(inst.cfg, {"video_fullscreen": True})                    # RetroArch ran and saved another setting
        out = ra.undo_relocation(Path(res["journal"]))
        self.assertTrue(out["cfg_restored"])
        cfg = ra.read_cfg(inst.cfg)
        self.assertEqual(cfg["video_fullscreen"], "true")
        self.assertEqual((cfg["savefile_directory"], cfg["savestate_directory"]), (":\\saves", ":\\states"))
        self.assertEqual(inst.cfg.read_bytes(), REAL_CFG.replace(b'video_fullscreen = "false"', b'video_fullscreen = "true"'))

    def test_an_untouched_config_is_put_back_byte_for_byte_by_undo(self) -> None:
        inst = self.portable(cfg=b'savefile_directory = ":\\saves"\nsavestate_directory = ":\\states"\n')    # keys get added
        self.with_saves(inst)
        new, res = self.relocate(inst)
        self.assertIn("sort_savefiles_enable", ra.read_cfg(inst.cfg))
        ra.undo_relocation(Path(res["journal"]))
        self.assertEqual(inst.cfg.read_bytes(), b'savefile_directory = ":\\saves"\nsavestate_directory = ":\\states"\n')

    def test_a_new_name_that_differs_only_by_case(self) -> None:
        inst = self.portable()
        saves = self.with_saves(inst, ("Flycast/sonic (usa).srm", "Flycast/sonic (usa).state1"))
        ops = ra.plan_follow(inst, [("sonic (usa)", "Sonic (USA)")], "move", self.home)
        self.assertEqual([o.status for o in ops], ["move", "move"])                       # never "a file is in the way": itself
        res = ra.apply_follow(ops, self.home / "j", install=inst)
        self.assertEqual((res["followed"], res["failed"]), (2, []))
        self.assertEqual(sorted(os.listdir(saves / "Flycast")), ["Sonic (USA).srm", "Sonic (USA).state1"])
        out = ra.undo_relocation(Path(res["journal"]))
        self.assertEqual((out["restored"], out["skipped"]), (2, []))
        self.assertEqual(sorted(os.listdir(saves / "Flycast")), ["sonic (usa).srm", "sonic (usa).state1"])
        if case_insensitive(self.home):
            self.assertEqual(ra.plan_follow(inst, [("sonic (usa)", "Sonic (USA)")], "copy", self.home), [])   # nothing to copy

    def test_on_a_file_system_that_tells_case_apart_another_case_is_another_file(self) -> None:
        if case_insensitive(self.home):
            self.skipTest("this file system ignores case")
        inst = self.portable()
        saves = self.with_saves(inst, ("Flycast/sonic.srm", "Flycast/Sonic.srm"))
        ops = ra.plan_follow(inst, [("sonic", "Sonic")], "move", self.home)
        self.assertEqual([o.status for o in ops], ["conflict"])
        self.assertEqual((saves / "Flycast" / "Sonic.srm").read_bytes(), b"Flycast/Sonic.srm")


class SharedPaths(Tmp):
    def test_a_folder_whose_name_starts_like_the_install_is_not_inside_it(self) -> None:
        other = self.home / "RetroArch-Win64-data"
        (other / "playlists").mkdir(parents=True)
        (other / "cht").mkdir()
        inst = self.portable(cfg=REAL_CFG.replace(b'":\\playlists"', b'"%s"' % str(other / "playlists").encode())
                             .replace(b'":\\cheats"', b'"%s"' % str(other / "cheats-old").encode()))
        self.assertEqual(ra.shared_base(inst, self.home), str(other))
        rows = {r["key"]: r for r in ra.shared_folders(inst, str(other), self.home)}
        self.assertEqual(rows["playlist_directory"]["status"], "ok")
        self.assertEqual(rows["cheat_database_path"]["status"], "set")             # points elsewhere, outside RetroArch
        self.assertFalse(ra._is_within(other, inst.base))
        self.assertTrue(ra._is_within(inst.base / "saves" / "x", inst.base))
        self.assertTrue(ra._is_within(inst.base, inst.base))

    @unittest.skipUnless(WINDOWS, "Windows spelling of folders")
    def test_the_same_folder_in_another_spelling_is_the_same_folder(self) -> None:
        assets = self.home / "RA" / "Assets"
        (assets / "cheats").mkdir(parents=True)
        (assets / "playlists").mkdir()
        slashed = str(assets / "cheats").replace("\\", "/")
        slashed = slashed[0].lower() + slashed[1:].upper()                          # c:/USERS/.../CHEATS
        inst = self.portable(cfg=REAL_CFG.replace(b'":\\cheats"', b'"%s"' % slashed.encode())
                             .replace(b'":\\playlists"', b'"%s\\"' % str(assets / "playlists").encode()))
        rows = {r["key"]: r for r in ra.shared_folders(inst, str(assets).lower(), self.home)}
        self.assertEqual(rows["cheat_database_path"]["status"], "ok")
        self.assertEqual(rows["playlist_directory"]["status"], "ok")                # a trailing separator
        res = ra.apply_shared(inst, list(rows.values()), ["cheat_database_path", "playlist_directory"], self.home / "j", self.home)
        self.assertEqual(res["changed"], [])
        local = self.portable("Local", cfg=REAL_CFG.replace(b'":\\cheats"', b'"%s"' % str(self.home / "LOCAL" / "cheats").upper().encode()))
        rows = {r["key"]: r for r in ra.shared_folders(local, str(assets), self.home)}
        self.assertEqual(rows["cheat_database_path"]["status"], "unset")            # RetroArch's own folder, upper case


INFO_PS = ('display_name = "Sony - PlayStation (Beetle PSX)"\ncorename = "Beetle PSX"\nsupported_extensions = "cue|chd|pbp"\n'
           'firmware_count = 2\nfirmware0_desc = "scph5501.bin (PS1 US BIOS)"\nfirmware0_path = "scph5501.bin"\nfirmware0_opt = "false"\n'
           'firmware1_desc = "dc"\nfirmware1_path = "dc\\dc_boot.bin"\nfirmware1_opt = "true"\n'
           'notes = "(!) scph5501.bin (md5): %s|(!) dc_boot.bin (md5): %s"\n')
DISPLAYS = {"snes9x": ("Nintendo - SNES / SFC (Snes9x - Current)", "smc|sfc|swc|fig|bs|st"),
            "bsnes": ("Nintendo - SNES / SFC / Game Boy / Color (bsnes)", "sfc|smc|gb|gbc|bs"),
            "ppsspp": ("Sony - PlayStation Portable (PPSSPP)", "elf|iso|cso|prx|pbp|chd"),
            "gpsp": ("Nintendo - Game Boy Advance (gpSP)", "gba|bin"),
            "mgba": ("Nintendo - Game Boy Advance (mGBA)", "gb|gbc|gba"),
            "gambatte": ("Nintendo - Game Boy / Color (Gambatte)", "gb|gbc|dmg"),
            "swanstation": ("Sony - PlayStation (SwanStation)", "exe|psexe|cue|bin|img|iso|chd|pbp|ecm|mds|psf|m3u"),
            "pcsx2": ("Sony - PlayStation 2 (LRPS2)", "elf|iso|ciso|cue|bin|gz|chd"),
            "flycast": ("Sega - Dreamcast/Naomi (Flycast)", "chd|cdi|elf|bin|cue|gdi|lst|zip|dat|7z|m3u")}


class BiosWindows(Tmp):
    def setUp(self) -> None:
        super().setUp()
        self.inst = self.portable()
        self.a, self.b = b"PS-BIOS", b"DC-BOOT"
        (self.inst.base / "info").mkdir()
        (self.inst.base / "info" / "mednafen_psx_libretro.info").write_text(
            INFO_PS % (hashlib.md5(self.a).hexdigest(), hashlib.md5(self.b).hexdigest()))
        (self.inst.base / "info" / "pcsx2_libretro.info").write_text(
            'display_name = "Sony - PlayStation 2 (LRPS2)"\ncorename = "LRPS2"\nsupported_extensions = "iso|chd"\nfirmware_count = 1\n'
            'firmware0_path = "pcsx2/bios"\nfirmware0_opt = "false"\n')
        self.system = self.inst.base / "system"
        self.system.mkdir()
        self.roms = self.home / "roms"
        self.roms.mkdir()
        self.psx = platforms.get_platform("Sony PlayStation")

    def infos(self) -> list:
        return [{"core": k, "display": v[0], "extensions": v[1].split("|"), "firmware": [], "file": k + "_libretro.info"}
                for k, v in DISPLAYS.items()]

    def names(self, platform: str) -> list:
        return sorted(c["core"] for c in ra.cores_for_platform(self.infos(), platforms.get_platform(platform)))

    def test_the_info_folder_named_in_the_cfg_with_a_backslash_is_read_once(self) -> None:
        infos = ra.core_infos(self.inst, self.home)                                       # libretro_info_path = ":\info"
        self.assertEqual([c["file"] for c in infos], ["mednafen_psx_libretro.info", "pcsx2_libretro.info"])
        fw = {f["path"]: f for f in infos[0]["firmware"]}
        self.assertEqual(sorted(fw), ["dc/dc_boot.bin", "scph5501.bin"])                    # either slash in the .info
        self.assertEqual(fw["dc/dc_boot.bin"]["md5"], hashlib.md5(self.b).hexdigest())

    def test_cores_are_matched_to_the_right_system(self) -> None:
        self.assertEqual(self.names("Super Nintendo Entertainment System"), ["bsnes", "snes9x"])
        self.assertEqual(self.names("Sony PlayStation"), ["swanstation"])                 # not PlayStation 2, not the PSP
        self.assertEqual(self.names("Sony PlayStation 2"), ["pcsx2"])
        self.assertEqual(self.names("Nintendo Game Boy"), ["bsnes", "gambatte", "mgba"])  # mGBA plays .gb; gpSP does not
        self.assertEqual(self.names("Nintendo Game Boy Advance"), ["gpsp", "mgba"])
        self.assertEqual(self.names("Sega Dreamcast"), ["flycast"])

    def test_a_firmware_entry_that_is_a_folder_is_there_when_the_folder_is(self) -> None:
        cores = [c for c in ra.core_infos(self.inst, self.home) if c["core"] == "LRPS2"]
        r = ra.check_bios_cores(self.inst, cores, [self.roms], self.home)
        self.assertEqual((r["cores"][0]["firmware"][0]["status"], r["required_missing"]), ("missing", 1))
        (self.system / "pcsx2" / "bios").mkdir(parents=True)
        r = ra.check_bios_cores(self.inst, cores, [self.roms], self.home)
        self.assertEqual((r["cores"][0]["firmware"][0]["status"], r["required_missing"]), ("present", 0))

    def test_a_bios_in_a_sub_folder_is_found_under_any_case_and_placed_under_the_exact_name(self) -> None:
        (self.roms / "Sony").mkdir()
        src1 = self.roms / "Sony" / "SCPH5501.BIN"
        src1.write_bytes(self.a)
        os.chmod(src1, stat.S_IREAD)                                                    # read-only, as from a set on disc
        (self.roms / "DC_BOOT.BIN").write_bytes(self.b)
        r = ra.check_bios(self.inst, self.psx, [self.roms], self.home)
        items = r["cores"][0]["firmware"]
        self.assertEqual([i["status"] for i in items], ["found", "found"])
        self.assertEqual(Path(items[1]["target"]), self.system / "dc" / "dc_boot.bin")
        res = ra.apply_bios(self.inst, [{"target": i["target"], "source": i["source"]} for i in items], self.home / "j", "move", self.home)
        self.assertEqual((res["placed"], res["failed"]), (2, []))
        self.assertEqual(os.listdir(self.system / "dc"), ["dc_boot.bin"])                  # the name the core asks for
        self.assertIn("scph5501.bin", os.listdir(self.system))
        self.assertFalse(src1.exists())
        again = ra.check_bios(self.inst, self.psx, [self.roms], self.home)
        self.assertEqual([i["status"] for i in again["cores"][0]["firmware"]], ["ok", "ok"])
        out = ra.undo_relocation(Path(res["journal"]))
        self.assertEqual((out["restored"], out["skipped"]), (2, []))
        self.assertTrue(src1.is_file())

    def test_a_bios_already_in_place_under_another_case(self) -> None:
        (self.system / "SCPH5501.BIN").write_bytes(self.a)
        r = ra.check_bios(self.inst, self.psx, [self.roms], self.home)
        status = r["cores"][0]["firmware"][0]["status"]
        # Windows (and any file system that ignores case): RetroArch opens it, so it is there. Where case counts
        # (Linux) RetroArch does not find it under that name, and neither does the check.
        self.assertEqual(status, "ok" if case_insensitive(self.home) else "missing")

    def test_only_the_system_folder_whatever_the_spelling(self) -> None:
        (self.roms / "x.bin").write_bytes(b"x")
        outside = str(self.system) + "-other"
        res = ra.apply_bios(self.inst, [{"target": str(Path(outside) / "x.bin"), "source": str(self.roms / "x.bin")}],
                            self.home / "j", "copy", self.home)
        self.assertEqual(res["placed"], 0)
        target = self.system / "x.bin"
        spelled = str(target).upper().replace("\\", "/") if WINDOWS else str(target)
        res = ra.apply_bios(self.inst, [{"target": spelled, "source": str(self.roms / "x.bin")}], self.home / "j", "copy", self.home)
        self.assertEqual((res["placed"], res["failed"]), (1, []))


if __name__ == "__main__":
    unittest.main()
