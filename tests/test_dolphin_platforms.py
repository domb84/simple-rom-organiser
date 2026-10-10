"""GameCube and Wii as platforms with Dolphin's saves, through the real HTTP server: scan, the games list, the Library plan with
the saves choice (keep / archive the saves with the game), undo."""

from __future__ import annotations

import hashlib
import os
import sys
import zlib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import rvztestlib as R  # noqa: E402
import test_dc_server as base  # noqa: E402
from test_nintendo import dolphin_tree, iso, wux  # noqa: E402
from romorg import gametdb, paths  # noqa: E402

GC, GC_DAT = "Nintendo GameCube", "Nintendo - GameCube"
WII, WII_DAT = "Nintendo Wii", "Nintendo - Wii"
WIIU = "Nintendo Wii U"


def write_dat(name: str, games: list) -> None:
    rows = []
    for game, data in games:
        rows.append(f'<game name="{game}"><category>Games</category><description>{game}</description>'
                    f'<rom name="{game}.iso" size="{len(data)}" crc="{zlib.crc32(data) & 0xFFFFFFFF:08x}" '
                    f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game>')
    folder = paths.redump_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.dat").write_text(f'<?xml version="1.0"?><datafile><header><name>{name}</name><version>2026-06-15 03-13-28</version>'
                                        "</header>" + "".join(rows) + "</datafile>", encoding="utf-8")


def write_mirror_dat(name: str, games: list) -> None:
    """A DAT as libretro's mirror of Redump has it: clrmamepro text, with each disc's serial (``(name, data, serial)``)."""
    out = [f'clrmamepro (\n\tname "{name}"\n\tversion "2026.10.07"\n)\n']
    for game, data, serial in games:
        out.append(f'game (\n\tname "{game}"\n\tserial "{serial}"\n\trom ( name "{game}.iso" size {len(data)} crc {zlib.crc32(data) & 0xFFFFFFFF:08X} '
                   f'md5 {hashlib.md5(data).hexdigest()} sha1 {hashlib.sha1(data).hexdigest()} serial "{serial}" )\n)\n')
    folder = paths.nointro_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.dat").write_text("".join(out), encoding="utf-8")


class DolphinCase(base.DcServerCase):
    def setUp(self) -> None:
        super().setUp()
        home = self.base / "home"
        home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(home), "APPDATA": "", "USERPROFILE": ""})
        env.start()
        self.addCleanup(env.stop)
        self.dolphin = self.base / "dolphin"
        dolphin_tree(self.dolphin)                       # a Wii save (RMGE), a GameCube save (GMSE) and a memory card image
        self.call("/api/settings/archive", {"dir": str(self.base / "archive")})

        # a GameCube disc as an .rvz (hashed as the ISO it stands for)
        self.gc_iso = bytearray(R.make_disc([("data", b"GMSE01" + bytes(0x20 - 6) + b"\xc2\x33\x9f\x3d" + b"Sunshine".ljust(0x40, b"\0") + bytes(0x2000))])[0])
        self.gc_iso[0:6] = b"GMSE01"
        self.gc_iso[0x1C:0x20] = bytes.fromhex("c2339f3d")
        self.gc_iso[0x20:0x20 + 8] = b"Sunshine"
        self.gc_iso = bytes(self.gc_iso)
        self.gc_alt = bytearray(self.gc_iso)
        self.gc_alt[3] = ord("P")                         # the European edition: no save of its own
        self.gc_alt = bytes(self.gc_alt)
        write_dat(GC_DAT, [("Super Mario Sunshine (USA)", self.gc_iso), ("Super Mario Sunshine (Europe)", self.gc_alt)])
        self.gc = self.base / "gc"
        self.gc.mkdir()
        R.build_rvz(self.gc / "sunshine.rvz", self.gc_iso, compression=R.NONE)
        R.build_rvz(self.gc / "sunshine eu.rvz", self.gc_alt, compression=R.NONE)

        self.wii = self.base / "wii"
        self.wii.mkdir()
        write_mirror_dat(WII_DAT, [("Super Mario Galaxy (USA) (En,Fr,Es)", b"galaxy", "RVL-RMGE-USA")])
        data = bytearray(iso("RMGE01", "Super Mario Galaxy"))
        R.build_rvz(self.wii / "whatever.rvz", bytes(data), compression=R.NONE, disc_type=2)

    def call(self, path: str, body: dict | None = None) -> dict:
        return super().call(path, body)

    def scan(self, platform: str, folder: Path) -> dict:
        return self.run_job("/api/scan", {"path": str(folder), "platform": platform})

    def games(self) -> dict:
        return {g["name"]: g for g in self.call("/api/scan/results?kind=games")["items"]}


class GameCubeTests(DolphinCase):
    def test_saves_belong_to_the_game_by_its_id_and_the_other_edition_has_none(self) -> None:
        self.call("/api/emulators/config", {"source": "dolphin", "folder": str(self.dolphin)})
        self.scan(GC, self.gc)
        games = self.games()
        self.assertEqual(games["Super Mario Sunshine (USA)"]["saves"]["total"], 1)
        self.assertEqual(games["Super Mario Sunshine (Europe)"]["saves"]["total"], 0)
        info = self.call("/api/status")
        self.assertEqual(info["scan"]["saves"]["sources"], ["Dolphin"])
        self.assertFalse(info["scan"]["saves"]["renames"])

    def test_without_dolphin_there_are_no_saves_at_all(self) -> None:
        self.call("/api/emulators/config", {"source": "dolphin", "folder": str(self.dolphin), "enabled": False})
        self.scan(GC, self.gc)
        self.assertNotIn("saves", self.call("/api/status")["scan"])
        self.assertNotIn("saved_games", self.call("/api/library/profile?platform=Nintendo%20GameCube")["profile"])

    def test_the_game_with_a_save_is_kept_and_its_save_can_go_with_it(self) -> None:
        self.call("/api/emulators/config", {"source": "dolphin", "folder": str(self.dolphin)})
        self.scan(GC, self.gc)
        plan = self.call("/api/library/plan", {"platform": GC})
        self.assertEqual(plan["saves"]["kept"], 1)                          # (the rules would keep one edition; this one has a save)
        self.assertTrue(list((self.dolphin / "GC" / "USA" / "Card A").glob("*.gci")))


class WiiTests(DolphinCase):
    def test_a_wii_rvz_is_identified_by_its_header_and_has_its_save(self) -> None:
        self.call("/api/emulators/config", {"source": "dolphin", "folder": str(self.dolphin)})
        self.scan(WII, self.wii)
        games = self.games()
        self.assertEqual(games["Super Mario Galaxy (USA) (En,Fr,Es)"]["have"], True)
        self.assertEqual(games["Super Mario Galaxy (USA) (En,Fr,Es)"]["saves"]["total"], 1)
        # a Wii platform does not see the GameCube saves, nor the GameCube platform the Wii ones
        sets = self.call("/api/scan/results?kind=saves")["items"]
        self.assertEqual({s["game"] for s in sets if s["match"] == "rom"}, {"Super Mario Galaxy (USA) (En,Fr,Es)"})


class WiiUTests(base.DcServerCase):
    """The Wii U has no checksummed DAT: the catalogue is made from GameTDB's list, discs are told by their product code, and Cemu's
    own list of games says which title ID (and so which saves) a file has."""

    def setUp(self) -> None:
        super().setUp()
        home = self.base / "home"
        home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(home), "APPDATA": "", "USERPROFILE": ""})
        env.start()
        self.addCleanup(env.stop)
        xml = self.base / "wiiutdb.xml"
        xml.write_text("<datafile>" + "".join(
            f'<game name="{n}"><id>{i}</id><type>{t}</type><region>X</region><languages>EN</languages></game>'
            for n, i, t in (("Star Fox Zero (USA) (EN)", "AFXE01", "WiiU"), ("Star Fox Zero (Europe) (EN,FR,DE)", "AFXP01", "WiiU"),
                            ("Splatoon (USA) (EN)", "ALAE01", "WiiU"), ("Some eShop game (USA) (EN)", "WAAE", "eShop"))) + "</datafile>")
        gametdb.build({"wiiu_xml": xml}, gametdb.db_path())
        self.assertEqual(gametdb.build_dat(), 3)                    # (the eShop game is not a disc)
        self.games = self.base / "wiiu"
        self.games.mkdir()
        (self.games / "sfz.wux").write_bytes(wux("AFXE"))
        (self.games / "mystery.wux").write_bytes(wux("ZZZE"))
        cemu = self.base / "cemu"
        save = cemu / "mlc01" / "usr" / "save" / "00050000" / "101b0400" / "user" / "80000001"
        save.mkdir(parents=True)
        (save / "game.sav").write_bytes(b"save")
        (cemu / "title_list_cache.xml").write_text(
            '<?xml version="1.0"?><title_list><title titleId="00050000101b0400" version="16"><region>2</region><name>Star Fox Zero</name>'
            f"<path>{self.games / 'sfz.wux'}</path></title></title_list>")
        self.cemu = cemu
        self.call("/api/settings/archive", {"dir": str(self.base / "archive")})

    def test_the_catalogue_the_games_found_and_their_saves(self) -> None:
        self.call("/api/emulators/config", {"source": "cemu", "folder": str(self.cemu)})
        plat = next(p for p in self.call("/api/platforms") if p["name"] == WIIU)
        self.assertTrue(plat["dats"][0]["present"])
        self.run_job("/api/scan", {"path": str(self.games), "platform": WIIU})
        games = {g["name"]: g for g in self.call("/api/scan/results?kind=games")["items"]}
        self.assertEqual({n: g["have"] for n, g in games.items()}, {"Star Fox Zero (USA) (En)": True, "Star Fox Zero (Europe) (En,Fr,De)": False, "Splatoon (USA) (En)": False})
        self.assertEqual(games["Star Fox Zero (USA) (En)"]["saves"]["total"], 1)
        unmatched = [u["file"] for u in self.call("/api/scan/results?kind=unmatched")["items"]]
        self.assertEqual(unmatched, ["mystery.wux"])
        self.assertEqual(self.call("/api/status")["scan"]["saves"]["sources"], ["Cemu"])


if __name__ == "__main__":
    unittest.main()


class CollectionTests(DolphinCase):
    """A Collection root with a GameCube disc: the Collection counts Dolphin's saves too, and keeps the game that has them."""

    def test_collection_counts_the_saves_of_an_emulator_system(self) -> None:
        self.call("/api/emulators/config", {"source": "dolphin", "folder": str(self.dolphin)})
        mixed = self.base / "mixed"
        (mixed / "Dump").mkdir(parents=True)
        R.build_rvz(mixed / "Dump" / "anything.rvz", self.gc_iso, compression=R.NONE)
        self.call("/api/collection/save", {"root": str(mixed)})
        scan = self.run_job("/api/collection/scan", {})["scan"]
        row = next(s for s in scan["systems"] if s["name"] == GC)
        self.assertEqual(row["saves"]["sources"], ["Dolphin"])
        self.assertEqual(row["saves"]["files"], 2)                       # (the game's save and the memory card image)
        self.assertEqual(scan["saves"]["sources"], ["Dolphin"])
        rows = self.call("/api/collection/saves", {})
        self.assertIn("items", rows)

    def test_without_the_emulator_the_collection_shows_no_saves(self) -> None:
        self.call("/api/emulators/config", {"source": "dolphin", "folder": str(self.dolphin), "enabled": False})
        mixed = self.base / "mixed"
        (mixed / "Dump").mkdir(parents=True)
        R.build_rvz(mixed / "Dump" / "anything.rvz", self.gc_iso, compression=R.NONE)
        self.call("/api/collection/save", {"root": str(mixed)})
        scan = self.run_job("/api/collection/scan", {})["scan"]
        self.assertNotIn("saves", next(s for s in scan["systems"] if s["name"] == GC))
        self.assertNotIn("saves", scan)


class CollectionWithSwitchTests(DolphinCase):
    """A Collection root with a GameCube disc, a Switch game and a portable Ryujinx inside it."""

    def setUp(self) -> None:
        super().setUp()
        from test_switch import APP, game_nsp, small_db
        from romorg import switchdb
        path = switchdb.db_path()
        small_db(path)
        switchdb.build_dat()
        self.mixed = self.base / "mixed"
        (self.mixed / "Dump").mkdir(parents=True)
        R.build_rvz(self.mixed / "Dump" / "anything.rvz", self.gc_iso, compression=R.NONE)
        self.portable = self.mixed / "switch" / "portable"
        (self.portable / "games").mkdir(parents=True)
        (self.portable / "games" / "game.nsp").write_bytes(game_nsp(APP, 0))
        (self.portable / "bis" / "user" / "save" / "0000000000000001" / "0").mkdir(parents=True)
        (self.portable / "bis" / "user" / "save" / "0000000000000001" / "0" / "progress.bin").write_bytes(b"save")
        (self.portable / "Config.json").write_text("{}")
        (self.mixed / "Dump" / "cover.jpg").write_bytes(b"jpg")
        self.call("/api/collection/save", {"root": str(self.mixed)})

    def test_every_system_is_still_found_when_the_switch_catalogue_is_installed(self) -> None:
        scan = self.run_job("/api/collection/scan", {})["scan"]
        self.assertEqual({s["name"]: s["games"] for s in scan["systems"]}, {GC: 1, "Nintendo Switch": 1})

    def test_a_wii_disc_is_still_identified_next_to_switch_games(self) -> None:
        R.build_rvz(self.mixed / "Dump" / "wii game.rvz", iso("RMGE01", "Super Mario Galaxy"), compression=R.NONE, disc_type=2)
        scan = self.run_job("/api/collection/scan", {})["scan"]
        self.assertEqual({s["name"]: s["games"] for s in scan["systems"]}, {GC: 1, WII: 1, "Nintendo Switch": 1})

    def test_the_data_of_an_emulator_inside_the_folder_is_left_alone(self) -> None:
        self.call("/api/emulators/config", {"source": "ryujinx", "folder": str(self.portable)})
        scan = self.run_job("/api/collection/scan", {})["scan"]
        self.assertEqual({s["name"]: s["games"] for s in scan["systems"]}, {GC: 1, "Nintendo Switch": 1})     # (the games in it are games)
        self.assertEqual((scan["unmatched"], scan["other"]), (0, 1))                                              # only the cover: no save, no Config.json
        self.assertTrue(any("Ryujinx" in n for n in scan["notes"]))
        plan = self.run_job("/api/collection/plan", {})
        self.assertNotIn("progress.bin", str(plan))
