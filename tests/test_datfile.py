import binascii
import hashlib
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from xml.sax.saxutils import quoteattr

from romorg.datfile import (DatFile, Rom, archive_stem, detect_format, parse_clrmamepro, parse_dat,
                            unit_key)

# Real-data tests skip when their DATs are absent. Override the Steam Deck defaults with environment variables:
#   ROMORG_REAL_SCRATCH  folder holding dats/TOSEC, nointro and dom (default: the Deck scratch folder)
SCRATCH = Path(os.environ.get("ROMORG_REAL_SCRATCH") or
               "/tmp/claude-1000/-home-deck-Dev-simple-rom-organiser/cfc5ea37-4261-428c-9422-29acd65cac97/scratchpad")
REAL_DAT = SCRATCH / "dats/TOSEC/Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat"
NOINTRO = SCRATCH / "nointro"
DOM_GBA = SCRATCH / "dom/Nintendo - Game Boy Advance (20260929-130236).dat"
# name -> (rom blocks, distinct game names, sets, sets with alternates) measured on 2026.08.01
REAL_NOINTRO = {
    "Nintendo - Game Boy Advance": (3692, 3691, 3692, 0),
    "Nintendo - Nintendo 64": (1435, 1241, 1257, 178),
    "Nintendo - Nintendo Entertainment System": (14132, 7053, 7070, 7062),
    "Nintendo - Super Nintendo Entertainment System": (4268, 4267, 4268, 0),
}

CMP_HEADER = ('clrmamepro (\n\tname "Nintendo - Test"\n\tdescription "Nintendo - Test"\n'
              '\tversion "2026.08.01"\n\thomepage "http://example.invalid"\n)\n\n')


def cmp_rom(name: str, data: bytes, extra: str = "") -> str:
    crc = format(binascii.crc32(data), "08X")
    return (f'\trom ( name "{name}" size {len(data)} crc {crc} md5 {hashlib.md5(data).hexdigest().upper()} '
            f'sha1 {hashlib.sha1(data).hexdigest().upper()}{extra} )\n')


def make_dat(path: Path, games: list[tuple[str, list[tuple[str, bytes]]]], extra: str = "") -> None:
    """Write a mini Logiqx DAT for (game, [(rom name, data)])."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<!DOCTYPE datafile PUBLIC "-//Logiqx//DTD ROM Management Datafile//EN" '
           '"http://www.logiqx.com/Dats/datafile.dtd">',
           "<datafile>", "<header><name>Test - Games - [ADF]</name>",
           "<description>Test - Games - [ADF] (TOSEC-v2025-01-30)</description>",
           "<version>2025-01-30</version></header>"]
    for game, roms in games:
        out.append(f"<game name={quoteattr(game)}><description>{game.replace('&', '&amp;')}</description>")
        for name, data in roms:
            crc = format(binascii.crc32(data), "08x")
            out.append(f'<rom name={quoteattr(name)} size="{len(data)}" crc="{crc.upper()}" '
                       f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/>')
        out.append("</game>")
    out.append(extra)
    out.append("</datafile>")
    path.write_text("\n".join(out), encoding="utf-8")


class DatFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        rnd = random.Random(1)
        self.d1 = rnd.randbytes(901120)
        self.d2 = rnd.randbytes(1024)
        self.path = Path(self.tmp.name) / "t.dat"
        make_dat(self.path, [
            ("Game & Co (1990)(Pub)(Disk 1 of 2)", [("Game & Co (1990)(Pub)(Disk 1 of 2).adf", self.d1)]),
            ("Game & Co (1990)(Pub)(Disk 2 of 2)", [("Game & Co (1990)(Pub)(Disk 2 of 2).adf", self.d2)]),
            ("Game & Co (1990)(Pub)(Disk 2 of 2)[a]", [("Game & Co (1990)(Pub)(Disk 2 of 2)[a].adf", self.d2)]),
        ], extra='<game name="Tiny"><rom name="Tiny.adf" size="3" crc="1f"/></game>')

    def test_parse(self) -> None:
        dat = parse_dat(self.path)
        self.assertEqual(dat.name, "Test - Games - [ADF]")
        self.assertEqual(dat.version, "2025-01-30")
        self.assertIn("TOSEC-v2025-01-30", dat.description)
        self.assertEqual(len(dat.roms), 4)
        r0 = dat.roms[0]
        self.assertEqual(r0.game, "Game & Co (1990)(Pub)(Disk 1 of 2)")
        self.assertEqual(r0.size, 901120)
        self.assertEqual(r0.crc, format(binascii.crc32(self.d1), "08x"))
        self.assertEqual(r0.sha1, hashlib.sha1(self.d1).hexdigest())
        tiny = dat.roms[-1]
        self.assertEqual((tiny.crc, tiny.md5, tiny.sha1), ("0000001f", "", ""))

    def test_indexes(self) -> None:
        dat = parse_dat(self.path)
        dup = dat.by_sha1()[hashlib.sha1(self.d2).hexdigest()]
        self.assertEqual(len(dup), 2)
        self.assertNotIn("", dat.by_sha1())
        key = (format(binascii.crc32(self.d1), "08x"), len(self.d1))
        self.assertEqual([r.name for r in dat.by_crc_size()[key]],
                         ["Game & Co (1990)(Pub)(Disk 1 of 2).adf"])
        self.assertIs(dat.by_sha1(), dat.by_sha1())  # cached
        self.assertEqual(len(dat.games()), 4)

    def test_rom_dat_field(self) -> None:
        dat = parse_dat(self.path)
        self.assertTrue(all(r.dat == "Test - Games - [ADF]" for r in dat.roms))
        hash(dat.roms[0])  # still hashable
        # no header name -> taken from the TOSEC filename
        named = Path(self.tmp.name) / "Commodore Amiga - Firmware (TOSEC-v2025-01-03_CM).dat"
        named.write_text('<?xml version="1.0"?><datafile><header><description>x</description></header>'
                         '<game name="K"><rom name="K.rom" size="1" crc="0"/></game></datafile>')
        d2 = parse_dat(named)
        self.assertEqual((d2.name, d2.roms[0].dat), ("Commodore Amiga - Firmware",) * 2)

    def test_constructible(self) -> None:
        dat = DatFile("n", "d", "v", [Rom("a.adf", 1, "00000000", "", "", "a")])
        self.assertEqual(dat.games(), {"a": [dat.roms[0]]})

    @unittest.skipUnless(REAL_DAT.is_file(), "real TOSEC DAT not available")
    def test_real_dat_speed(self) -> None:
        t = time.perf_counter()
        dat = parse_dat(REAL_DAT)
        elapsed = time.perf_counter() - t
        print(f"\n  real DAT: {len(dat.roms)} roms parsed in {elapsed:.2f}s", end=" ")
        self.assertEqual(dat.name, "Commodore Amiga - Games - [ADF]")
        self.assertEqual(len(dat.roms), 34410)
        self.assertLess(elapsed, 3.0)


class ClrmameproTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.nes = b"NES\x1a" + bytes(12) + b"prg" * 100
        self.unh = self.nes[16:]
        self.other = b"other rom"

    def write(self, text: str, name: str = "t.dat") -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_parse_and_sets(self) -> None:
        text = (CMP_HEADER
                + 'game (\n\tname "Game (USA)"\n\tregion "USA"\n\treleaseyear 1990\n'
                + cmp_rom("Game (USA).nes", self.nes, " serial NES-XY") + ")\n"
                + 'game (\n\tname "Game (USA)"\n' + cmp_rom("Game (USA).unh", self.unh) + ")\n"
                + 'game (\n\tname "Pac (USA) (Tengen)"\n'
                + cmp_rom("Pac (USA) (Tengen) (Unl).nes", self.other) + ")\n"
                + 'resource (\n\tname "ignored"\n\trom ( name "x.bin" size 1 crc 0 )\n)\n'
                + 'machine (\n\tname "Q \\"quoted\\" \\\\ C:\\path"\n'
                + '\tdisk ( name "d" sha1 00 )\n' + cmp_rom("Q.bin", b"q") + ")\n")
        path = self.write(text)
        self.assertEqual(detect_format(path), "clrmamepro")
        dat = parse_dat(path)
        self.assertEqual((dat.name, dat.description, dat.version, dat.homepage, dat.format),
                         ("Nintendo - Test", "Nintendo - Test", "2026.08.01", "http://example.invalid",
                          "clrmamepro"))
        self.assertEqual([r.name for r in dat.roms],
                         ["Game (USA).nes", "Game (USA).unh", "Pac (USA) (Tengen) (Unl).nes", "Q.bin"])
        r0 = dat.roms[0]
        self.assertEqual(r0.game, "Game (USA)")
        self.assertEqual(r0.set_name, "Game (USA)")
        self.assertEqual(r0.dat, "Nintendo - Test")
        self.assertEqual(r0.size, len(self.nes))
        self.assertEqual(r0.crc, format(binascii.crc32(self.nes), "08x"))
        self.assertEqual(r0.sha1, hashlib.sha1(self.nes).hexdigest())
        self.assertEqual(r0.md5, hashlib.md5(self.nes).hexdigest())
        self.assertEqual(dat.roms[3].game, 'Q "quoted" \\ C:\\path')  # \" and \\ unescaped, \p kept
        sets = dat.sets()
        self.assertEqual(list(sets), ["Game (USA)", "Pac (USA) (Tengen) (Unl)", "Q"])
        self.assertEqual(len(sets["Game (USA)"]), 2)
        self.assertIs(dat.sets(), sets)
        self.assertEqual(len(dat.games()), 3)  # games() still keyed by game name
        self.assertEqual(dat.count_by, "game")
        self.assertEqual(unit_key(dat.roms[1]), ("Nintendo - Test", "Game (USA)"))
        self.assertEqual(archive_stem(dat.roms[2]), "Pac (USA) (Tengen) (Unl)")
        self.assertEqual(dat.roms[2].tags.flags, ("Tengen", "Unl"))
        self.assertEqual(dat.roms[0].tags.regions, ("USA",))
        self.assertEqual(len(dat.by_sha1()), 4)
        # set_names=False keeps the TOSEC-style counting
        plain = parse_clrmamepro(path, set_names=False)
        self.assertTrue(all(r.set_name == "" for r in plain.roms))
        self.assertEqual(plain.count_by, "rom")

    def test_label_from_filename_without_header(self) -> None:
        path = self.write("game ( name G rom ( name G.gba size 1 crc 1 ) )",
                          "Nintendo - Foo.dat")
        dat = parse_dat(path)
        self.assertEqual((dat.name, dat.roms[0].dat, dat.roms[0].crc), ("Nintendo - Foo",) * 2 + ("00000001",))
        self.assertEqual(dat.roms[0].md5, "")

    def test_errors_have_line_numbers(self) -> None:
        bad = {
            "unbalanced": CMP_HEADER + 'game (\n\tname "G"\n\trom ( name "G.gba" size 1\n',
            "missing value": CMP_HEADER + 'game (\n\tname\n)\n',
            "stray close": CMP_HEADER + ')\n',
            "unterminated": CMP_HEADER + 'game (\n\tname "G\n)\n',
            "no paren": CMP_HEADER + 'game name "G"\n',
        }
        for label, text in bad.items():
            with self.subTest(label):
                path = self.write(text)
                with self.assertRaisesRegex(ValueError, r"line \d+"):
                    parse_dat(path)
        with self.assertRaisesRegex(ValueError, "line 9"):
            parse_dat(self.write(CMP_HEADER + 'game (\n\tname\n)\n'))

    def test_detect_format(self) -> None:
        self.assertEqual(detect_format(self.write("\ufeff\n  <?xml version='1.0'?><datafile/>")), "logiqx")
        self.assertEqual(detect_format(self.write("\ufeff\r\nclrmamepro (\n)")), "clrmamepro")
        self.assertEqual(detect_format(self.write("game ( name x )")), "clrmamepro")
        with self.assertRaisesRegex(ValueError, "unknown DAT format"):
            detect_format(self.write("hello world"))
        with self.assertRaises(ValueError):
            parse_dat(self.write(""))

    def test_logiqx_set_names_and_defaults(self) -> None:
        path = self.dir / "x.dat"
        make_dat(path, [("A (USA)", [("A (USA).gba", b"a")])])
        self.assertEqual(parse_dat(path).roms[0].set_name, "")
        self.assertEqual(parse_dat(path).count_by, "rom")
        self.assertEqual(parse_dat(path).format, "logiqx")
        named = parse_dat(path, set_names=True)
        self.assertEqual(named.roms[0].set_name, "A (USA)")
        self.assertEqual(named.count_by, "game")
        tosec_rom = Rom("G (1990)(P).adf", 1, "0" * 8, "", "", "G (1990)(P)", "D")
        self.assertEqual(tosec_rom.set_name, "")
        self.assertEqual(unit_key(tosec_rom), ("D", "G (1990)(P).adf"))
        self.assertEqual(archive_stem(tosec_rom), "G (1990)(P)")
        self.assertEqual(tosec_rom.tags.style, "tosec")
        self.assertEqual(tosec_rom.tags.date, "1990")


@unittest.skipUnless(NOINTRO.is_dir(), "real No-Intro DATs not available")
class RealNoIntroTest(unittest.TestCase):
    def test_counts_and_speed(self) -> None:
        for name, (blocks, games, sets, alts) in REAL_NOINTRO.items():
            path = NOINTRO / f"{name}.dat"
            if not path.is_file():
                continue
            with self.subTest(name):
                t = time.perf_counter()
                dat = parse_dat(path)
                elapsed = time.perf_counter() - t
                print(f"\n  {name}: {len(dat.roms)} roms, {len(dat.sets())} games in {elapsed:.2f}s", end=" ")
                self.assertEqual(dat.name, name)
                self.assertEqual(dat.version, "2026.08.01")
                self.assertEqual(len(dat.roms), blocks)
                self.assertEqual(len(dat.games()), games)
                self.assertEqual(len(dat.sets()), sets)
                self.assertEqual(sum(1 for v in dat.sets().values() if len(v) > 1), alts)
                self.assertTrue(all(r.sha1 and len(r.crc) == 8 for r in dat.roms))
                self.assertFalse(any(r.size % 1024 == 512 for r in dat.roms))
                self.assertLess(elapsed, 1.5 if "Entertainment" in name else 1.0)

    @unittest.skipUnless(DOM_GBA.is_file(), "DAT-o-MATIC GBA DAT not available")
    def test_dat_o_matic_matches_libretro(self) -> None:
        lr_path = NOINTRO / "Nintendo - Game Boy Advance.dat"
        if not lr_path.is_file():
            self.skipTest("libretro GBA DAT not available")
        self.assertEqual(detect_format(DOM_GBA), "logiqx")
        dom = parse_dat(DOM_GBA, set_names=True)
        lr = parse_dat(lr_path)
        self.assertEqual(dom.name, "Nintendo - Game Boy Advance")
        a = {r.sha1: r for r in dom.roms}
        b = {r.sha1: r for r in lr.roms}
        shared = set(a) & set(b)
        self.assertGreater(len(shared), 3400)
        for sha in shared:
            self.assertEqual((a[sha].size, a[sha].crc, a[sha].md5), (b[sha].size, b[sha].crc, b[sha].md5))
        renamed = [s for s in shared if a[s].name != b[s].name]
        self.assertLessEqual(len(renamed), 5)  # 2 on 2026-09-29 (later DAT-o-MATIC renames)


if __name__ == "__main__":
    unittest.main()
