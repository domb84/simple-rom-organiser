"""Tests for romorg.platforms (synthetic DATs + optional real TOSEC DATs)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from romorg import platforms
from romorg.platforms import (DatSource, get_platform, list_platforms, load_platform_dats, locate_dats,
                              platform_dat_status)

# ROMORG_REAL_SCRATCH: folder holding dats/TOSEC and nointro (default: the Steam Deck scratch folder)
_SCRATCH = Path(os.environ.get("ROMORG_REAL_SCRATCH") or
                "/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/cfc5ea37-4261-428c-9422-29acd65cac97/scratchpad")
REAL_DATS = _SCRATCH / "dats" / "TOSEC"
REAL_NOINTRO = _SCRATCH / "nointro"
CONSOLES = {
    "Nintendo Game Boy Advance": ("Nintendo - Game Boy Advance", (), False, "gba"),
    "Nintendo 64": ("Nintendo - Nintendo 64", ("n64_byteorder",), True, "n64"),
    "Nintendo Entertainment System": ("Nintendo - Nintendo Entertainment System", ("nes_header",), False, "nes"),
    "Super Nintendo Entertainment System": ("Nintendo - Super Nintendo Entertainment System",
                                            ("snes_header",), True, "snes"),
    "Nintendo Game Boy": ("Nintendo - Game Boy", (), False, "gb"),
    "Nintendo Game Boy Color": ("Nintendo - Game Boy Color", (), False, "gbc"),
    "Nintendo DS": ("Nintendo - Nintendo DS", (), False, "nds"),
    "Sega Mega Drive - Genesis": ("Sega - Mega Drive - Genesis", (), False, "megadrive"),
    "Sega Master System": ("Sega - Master System - Mark III", (), False, "mastersystem"),
    "Sega Game Gear": ("Sega - Game Gear", (), False, "gamegear"),
    "Sega 32X": ("Sega - 32X", (), False, "sega32x"),
    "Atari Lynx": ("Atari - Lynx", (), False, "atarilynx"),
}


def _cmp_dat(path: Path, name: str, roms: list[str]) -> None:
    games = "".join(f'game (\n\tname "{r[:-4]}"\n\trom ( name "{r}" size 1 crc {i:08X} )\n)\n'
                    for i, r in enumerate(roms))
    path.write_text(f'clrmamepro (\n\tname "{name}"\n\tversion "2026.08.01"\n)\n{games}', encoding="utf-8")


def _dat(path: Path, name: str, roms: list[str]) -> None:
    games = "".join(f'<game name="{r[:-4]}"><rom name="{r}" size="1" crc="{i:08x}"/></game>'
                    for i, r in enumerate(roms))
    path.write_text(f'<?xml version="1.0"?><datafile><header><name>{name}</name></header>{games}</datafile>',
                    encoding="utf-8")


class PlatformTests(unittest.TestCase):
    def test_registry(self) -> None:
        amiga = get_platform("Commodore Amiga")
        self.assertEqual(amiga.dats[0], "Commodore Amiga - Games - [ADF]")
        self.assertEqual(amiga.kickstart_dat, "Commodore Amiga - Firmware")
        self.assertNotIn("Commodore Amiga - Firmware", amiga.m3u_dats)
        self.assertTrue(set(amiga.m3u_dats) <= set(amiga.dats))
        self.assertIn(amiga, list_platforms())
        self.assertIn("Commodore Amiga - Kickstart-Disks", platforms.all_dat_names())
        with self.assertRaises(KeyError):
            get_platform("Atari ST")

    def test_library_scopes_and_source_of(self) -> None:
        amiga = get_platform("Commodore Amiga")
        games = "Commodore Amiga - Games - [ADF]"
        self.assertEqual(amiga.latest_dats, (games,))
        self.assertEqual(amiga.best_variant_dats, (games,))
        self.assertEqual(platforms.source_of(amiga), "tosec")
        for p in list_platforms():
            if p is amiga:
                continue
            if p.name == platforms.SWITCH_PLATFORM:
                self.assertEqual(platforms.source_of(p), "redump")
                continue
            if p.name in (platforms.DREAMCAST_PLATFORM, platforms.PSX_PLATFORM, platforms.PS2_PLATFORM,
                          platforms.GAMECUBE_PLATFORM, platforms.WII_PLATFORM, platforms.WIIU_PLATFORM):   # its own source: checked in test_dreamcast.py / test_rvz.py
                self.assertEqual(platforms.source_of(p), "redump")
                self.assertEqual((p.latest_dats, p.best_variant_dats), (p.dats, ()))
                continue
            self.assertEqual(platforms.source_of(p), "nointro")
            self.assertEqual(p.latest_dats, p.dats)
            self.assertEqual(p.best_variant_dats, ())

    def test_load_priority_missing_and_exact_names(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            _dat(folder / "Commodore Amiga - Firmware (TOSEC-v2025-01-03_CM).dat",
                 "Commodore Amiga - Firmware", ["K.rom"])
            _dat(folder / "Commodore Amiga - Firmware (TOSEC-v2020-01-01_CM).dat",
                 "Commodore Amiga - Firmware", ["Old.rom"])
            _dat(folder / "Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat",
                 "Commodore Amiga - Games - [ADF]", ["G.adf", "H.adf"])
            _dat(folder / "Commodore Amiga - Operating Systems - AMIX (TOSEC-v2025-01-01_CM).dat",
                 "Commodore Amiga - Operating Systems - AMIX", ["A.adf"])
            _dat(folder / "Commodore Amiga - Operating Systems - Workbench (TOSEC-v2023-05-21_CM).dat",
                 "Header Name Differs", ["W.adf"])
            amiga = get_platform("Commodore Amiga")
            loaded, missing = load_platform_dats(amiga, folder)
            self.assertEqual([x.name for x in loaded], [
                "Commodore Amiga - Games - [ADF]", "Commodore Amiga - Operating Systems - Workbench",
                "Commodore Amiga - Firmware"])
            self.assertEqual(missing, ["Commodore Amiga - Kickstart-Disks", "Commodore Amiga - Games - SPS",
                                       "Commodore Amiga - Games - [Unofficial IPF]", "Commodore Amiga CD32 - Games - [ISO]", "Commodore Amiga CD32 - Games - [IMG]", "Commodore Amiga CD32 - Games - [BIN]", "Commodore Amiga CDTV - Games - [ISO]", "Commodore Amiga CDTV - Games - [IMG]", "Commodore Amiga CDTV - Games - [BIN]", "Commodore Amiga CDTV - Games - [MDF]"])
            self.assertEqual([r.name for r in loaded[2].roms], ["K.rom"])  # newest version
            self.assertEqual(loaded[1].roms[0].dat, "Commodore Amiga - Operating Systems - Workbench")
            status = platform_dat_status(amiga, folder)
            self.assertEqual([(s["name"], s["version"], s["present"]) for s in status], [
                ("Commodore Amiga - Games - [ADF]", "2025-01-30", True),
                ("Commodore Amiga - Operating Systems - Workbench", "2023-05-21", True),
                ("Commodore Amiga - Kickstart-Disks", None, False),
                ("Commodore Amiga - Firmware", "2025-01-03", True),
                ("Commodore Amiga - Games - SPS", None, False),
                ("Commodore Amiga - Games - [Unofficial IPF]", None, False),
                *((name, None, False) for name in ("Commodore Amiga CD32 - Games - [ISO]", "Commodore Amiga CD32 - Games - [IMG]", "Commodore Amiga CD32 - Games - [BIN]", "Commodore Amiga CDTV - Games - [ISO]", "Commodore Amiga CDTV - Games - [IMG]", "Commodore Amiga CDTV - Games - [BIN]", "Commodore Amiga CDTV - Games - [MDF]")),
            ])

    def test_the_sps_images_are_named_by_game_and_only_the_images_count(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sps = ('<?xml version="1.0"?><datafile><header><name>Commodore Amiga - Games - SPS</name><version>2025-01-13</version></header>'
                   '<game name="1869 (1992)(Flair)(AGA)(DE)[1653]"><rom name="1869_DiskA.ipf" size="9" crc="00000001"/>'
                   '<rom name="1869_DiskB.ipf" size="9" crc="00000002"/><rom name="1869.jpg" size="3" crc="00000003"/></game>'
                   '<game name="1869 - Erlebte Geschichte Teil I (1992)(Flair)(DE)[0417]"><rom name="1869_DiskA.ipf" size="9" crc="00000004"/>'
                   '<rom name="info.xml" size="3" crc="00000005"/></game></datafile>')
            (Path(d) / "Commodore Amiga - Games - SPS (TOSEC-v2025-01-13_CM).dat").write_text(sps)
            loaded, _missing = load_platform_dats(get_platform("Commodore Amiga"), d)
            names = [r.name for r in loaded[0].roms]
            self.assertEqual(names, ["1869 (1992)(Flair)(AGA)(DE)[1653] - 1869_DiskA.ipf", "1869 (1992)(Flair)(AGA)(DE)[1653] - 1869_DiskB.ipf",
                                     "1869 - Erlebte Geschichte Teil I (1992)(Flair)(DE)[0417] - 1869_DiskA.ipf"])      # (pictures and info files left out)
            self.assertEqual(len(set(names)), 3)                                                                    # (the shared 1869_DiskA.ipf: two files)
            self.assertIn(".ipf", get_platform("Commodore Amiga").extensions)

    def test_console_registry(self) -> None:
        amiga = get_platform("Commodore Amiga")
        self.assertEqual((amiga.source, amiga.layout, amiga.folder_hint, amiga.convertible),
                         ("tosec", "per_dat", "amiga", False))
        self.assertEqual(amiga.alt_hashes, ())
        for name, (dat, alt, convertible, hint) in CONSOLES.items():
            with self.subTest(name):
                p = get_platform(name)
                self.assertEqual(p.dats, (dat,))
                self.assertEqual((p.source, p.layout), (DatSource.NOINTRO, platforms.LAYOUT_FLAT))
                self.assertEqual((p.m3u_dats, p.kickstart_dat), ((), None))
                self.assertEqual((p.alt_hashes, p.convertible, p.folder_hint), (alt, convertible, hint))
                self.assertTrue(all(e.startswith(".") and e == e.lower() for e in p.extensions))
                self.assertIn(".zip", p.extensions)
        self.assertEqual(DatSource.NOINTRO, "nointro")
        names = [p.name for p in list_platforms()]
        self.assertEqual(names, sorted(names, key=str.lower))
        # Amiga, Dreamcast, PlayStation (2), GameCube, Wii, Wii U, Switch and the cartridge systems of CONSOLES
        self.assertEqual(len(names), 8 + len(CONSOLES))
        self.assertEqual(platforms.DEFAULT_PLATFORM, "Commodore Amiga")
        self.assertIn("Nintendo - Nintendo 64", platforms.all_dat_names())
        # positional construction (old signature) still works with the defaults
        p = platforms.Platform("X", ("A",), (), None)
        self.assertEqual((p.source, p.layout, p.extensions), ("tosec", "per_dat", ()))

    def test_every_nointro_platform_is_wired_up(self) -> None:
        """A typo in a DAT name, folder name or LaunchBox name would fail silently in the app, so check them all."""
        from romorg import nointro, ratings
        from romorg.server import OTHER_SYSTEM_DIRS
        plats = [p for p in list_platforms() if p.source == DatSource.NOINTRO]
        self.assertEqual(len(plats), len(CONSOLES))
        hints = [p.folder_hint for p in plats]
        self.assertEqual(len(set(hints)), len(hints), "two systems share a folder name")
        self.assertEqual(len({p.name for p in plats}), len(plats))
        self.assertEqual(sorted(d for p in plats for d in p.dats), sorted(nointro.NOINTRO_DATS))
        for p in plats:
            with self.subTest(p.name):
                self.assertIn(p.folder_hint, OTHER_SYSTEM_DIRS)          # the "this is the whole roms folder" check
                self.assertIn(p.name, ratings.LB_PLATFORMS)
                self.assertEqual(p.latest_dats, p.dats)
                self.assertEqual(p.language_dats, p.dats)
                self.assertEqual(p.region_dats, p.dats)
                self.assertEqual(p.extensions[-2:], (".zip", ".7z"))
        # LaunchBox's own platform names (Platforms.xml of Metadata.zip, read 2026-10-06)
        self.assertEqual(ratings.LB_PLATFORMS["Sega Mega Drive - Genesis"], "Sega Genesis")
        self.assertEqual(len(set(ratings.LB_PLATFORMS.values())), len(set(ratings.LB_PLATFORMS.values())))

    def test_nointro_dats_separate_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tosec_dir, ni_dir = Path(d) / "dats", Path(d) / "nointro"
            tosec_dir.mkdir()
            ni_dir.mkdir()
            # a same-named file in the TOSEC folder must not be picked up for a No-Intro platform
            _cmp_dat(tosec_dir / "Nintendo - Nintendo Entertainment System.dat",
                     "Nintendo - Nintendo Entertainment System", ["Wrong (USA).nes"])
            _cmp_dat(ni_dir / "Nintendo - Nintendo Entertainment System.dat",
                     "Nintendo - Nintendo Entertainment System",
                     ["A (USA).nes", "A (USA).unh", "B (Japan).nes"])
            nes = get_platform("Nintendo Entertainment System")
            located = locate_dats(nes, tosec_dir, ni_dir)
            self.assertEqual(list(located), ["Nintendo - Nintendo Entertainment System"])
            self.assertEqual(located["Nintendo - Nintendo Entertainment System"].path.parent, ni_dir)
            loaded, missing = load_platform_dats(nes, tosec_dir, ni_dir)
            self.assertEqual(missing, [])
            self.assertEqual([r.set_name for r in loaded[0].roms], ["A (USA)", "A (USA)", "B (Japan)"])
            self.assertEqual(len(loaded[0].sets()), 2)
            self.assertEqual(platform_dat_status(nes, tosec_dir, ni_dir), [{
                "name": "Nintendo - Nintendo Entertainment System", "version": "2026.08.01",
                "present": True, "file": "Nintendo - Nintendo Entertainment System.dat",
                "source": "nointro"}])
            gba = get_platform("Nintendo Game Boy Advance")
            self.assertEqual(load_platform_dats(gba, tosec_dir, ni_dir), ([], ["Nintendo - Game Boy Advance"]))
            self.assertEqual(platform_dat_status(gba, tosec_dir, ni_dir)[0]["present"], False)
            self.assertEqual(locate_dats(get_platform("Commodore Amiga"), tosec_dir, ni_dir), {})
            self.assertEqual(platform_dat_status(get_platform("Commodore Amiga"), tosec_dir)[0]["source"],
                             "tosec")

    @unittest.skipUnless(REAL_NOINTRO.is_dir(), "real No-Intro DATs not available")
    def test_real_nointro(self) -> None:
        for name in CONSOLES:
            p = get_platform(name)
            loaded, missing = load_platform_dats(p, nointro_directory=REAL_NOINTRO)
            if missing:
                continue
            self.assertEqual(loaded[0].count_by, "game")
        nes, _ = load_platform_dats(get_platform("Nintendo Entertainment System"),
                                    nointro_directory=REAL_NOINTRO)
        if nes:
            self.assertEqual(len(nes[0].sets()), 7070)

    @unittest.skipUnless(REAL_DATS.is_dir(), "real TOSEC DATs not available")
    def test_real_dats(self) -> None:
        loaded, missing = load_platform_dats(get_platform("Commodore Amiga"), REAL_DATS)
        self.assertEqual(missing, [])
        counts = {d.name: len(d.roms) for d in loaded}
        self.assertEqual(counts["Commodore Amiga - Firmware"], 169)
        self.assertEqual(counts["Commodore Amiga - Kickstart-Disks"], 43)
        self.assertEqual(counts["Commodore Amiga - Operating Systems - Workbench"], 222)
        self.assertTrue(all(r.dat == d.name for d in loaded for r in d.roms[:50]))


class LibraryScopes(unittest.TestCase):
    """Which DATs the language filter / region priority cover (library profile scopes)."""

    def test_scopes(self) -> None:
        amiga = get_platform("Commodore Amiga")
        self.assertEqual(amiga.language_dats, ("Commodore Amiga - Games - [ADF]", "Commodore Amiga - Games - SPS",
                                               "Commodore Amiga - Games - [Unofficial IPF]"))     # (the SPS releases' languages count too)
        self.assertEqual(amiga.region_dats, ())
        for p in list_platforms():
            if p.name == "Commodore Amiga":
                continue
            if p.name == platforms.SWITCH_PLATFORM:    # regions on the games, languages on games and updates, "latest" on the updates
                self.assertEqual((p.language_dats, p.region_dats, p.latest_dats),
                                 ((platforms.SWITCH_DAT, platforms.SWITCH_UPDATES_DAT), (platforms.SWITCH_DAT,), (platforms.SWITCH_UPDATES_DAT,)))
                continue
            self.assertEqual((p.language_dats, p.region_dats), (p.dats, p.dats), p.name)


if __name__ == "__main__":
    unittest.main()
