"""Memory cards: which games' saves a card holds (PlayStation, Dreamcast VMU, GameCube, PlayStation 2) and how the save index shows them."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from romorg import memcards, saveindex

from tests.test_nintendo import ps2_card_image  # noqa: E402


def ps1_card(saves: list[tuple[str, str, int]], header: bytes = b"") -> bytes:
    """A PlayStation card with ``(file name, title, blocks)`` saves; each save's first block starts with a header frame."""
    data = bytearray(131072)
    data[0:2] = b"MC"
    for i in range(1, 16):
        data[i * 128] = 0xA0
    block = 1
    for name, title, blocks in saves:
        frame = bytearray(128)
        frame[0] = 0x51
        struct.pack_into("<I", frame, 4, blocks * 8192)
        frame[10:10 + len(name)] = name.encode("ascii")
        data[block * 128:(block + 1) * 128] = frame
        data[block * 8192:block * 8192 + 2] = b"SC"
        data[block * 8192 + 4:block * 8192 + 4 + len(title.encode("shift_jis"))] = title.encode("shift_jis")
        for extra in range(1, blocks):
            data[(block + extra) * 128] = 0x52 if extra < blocks - 1 else 0x53
        block += blocks
    return header + bytes(data)


def vmu_card(files: list[tuple[str, str, str]]) -> bytes:
    """A Dreamcast VMU with ``(file name, description, long description)`` data files."""
    data = bytearray(131072)
    root = 0xFF * 512
    data[root:root + 16] = b"\x55" * 16
    struct.pack_into("<HHHHHH", data, root + 0x46, 254, 1, 253, 13, 0, 200)
    for k, (name, short, long) in enumerate(files):
        first = 3 + k * 2
        e = 253 * 512 + k * 32
        data[e] = 0x33
        struct.pack_into("<H", data, e + 2, first)
        data[e + 4:e + 16] = name.encode("ascii").ljust(12, b" ")
        struct.pack_into("<HH", data, e + 0x18, 2, 0)
        data[first * 512:first * 512 + 16] = short.encode("ascii").ljust(16, b" ")
        data[first * 512 + 16:first * 512 + 48] = long.encode("ascii").ljust(32, b" ")
    return bytes(data)


def gc_card(saves: list[tuple[str, str]], size: int = 0x80000) -> bytes:
    """A GameCube card with ``(game code, file name)`` saves in the newer of its two directories (the other is torn)."""
    data = bytearray(size)

    def directory(entries: list[tuple[str, str]], counter: int, valid: bool) -> bytes:
        block = bytearray(b"\xff" * 0x2000)
        for k, (code, name) in enumerate(entries):
            e = bytearray(b"\xff" * 64)
            e[0:4] = code.encode("ascii")
            e[4:6] = b"01"
            e[8:40] = name.encode("latin-1").ljust(32, b"\0")
            struct.pack_into(">H", e, 0x38, 3)
            block[k * 64:(k + 1) * 64] = e
        struct.pack_into(">H", block, 0x1FFA, counter)
        a, b = memcards._gc_checksum(bytes(block), 0x1FFC)
        struct.pack_into(">HH", block, 0x1FFC, a if valid else a ^ 1, b)
        return bytes(block)

    data[0x2000:0x4000] = directory(saves, 6, True)
    data[0x4000:0x6000] = directory(saves[:1], 5, True)
    return bytes(data)


class Readers(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def put(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_a_playstation_card_names_the_games_on_it(self) -> None:
        data = ps1_card([("BASLUS-01234SMACK2", "Ｓｍａｃｋｄｏｗｎ！２", 2), ("BESLES-00001FF7", "FINAL FANTASY VII", 1)])
        card = memcards.read(self.put("Game.srm", data))
        self.assertEqual(card.kind, memcards.PS1)
        self.assertEqual([(e.code, e.title, e.blocks) for e in card.entries], [("SLUS-01234", "Smackdown!2", 2), ("SLES-00001", "FINAL FANTASY VII", 1)])
        self.assertEqual(card.games(), ["Smackdown!2 (SLUS-01234)", "FINAL FANTASY VII (SLES-00001)"])
        self.assertEqual(card.free_blocks, 12)
        for extra, name in ((3904, "a.gme"), (128, "b.vmp")):                                    # DexDrive and PSP wrappers
            self.assertEqual(len(memcards.read(self.put(name, ps1_card([("BASLUS-01234X", "T", 1)], b"\0" * extra))).entries), 1, name)
        self.assertEqual(memcards.read(self.put("empty.mcr", ps1_card([]))).entries, [])        # an empty card is a card with no games

    def test_a_dreamcast_vmu_names_what_is_saved(self) -> None:
        card = memcards.read(self.put("MK-51000.A1.bin", vmu_card([("SONICADV_INT", "SONICADV", "SONIC ADVENTURE / Main Save File"), ("SOUL.001", "", "Legacy of Kain")])))
        self.assertEqual(card.kind, memcards.DREAMCAST)
        self.assertEqual(card.games(), ["SONIC ADVENTURE / Main Save File", "Legacy of Kain"])
        self.assertEqual([e.name for e in card.entries], ["SONICADV_INT", "SOUL.001"])

    def test_a_gamecube_card_gives_the_game_codes_and_uses_the_newer_valid_directory(self) -> None:
        card = memcards.read(self.put("MemoryCardA.USA.raw", gc_card([("GALE", "Super Smash Bros. Melee"), ("GM4E", "Mario Kart")])))
        self.assertEqual(card.kind, memcards.GAMECUBE)
        self.assertEqual([(e.code, e.title) for e in card.entries], [("GALE", "Super Smash Bros. Melee"), ("GM4E", "Mario Kart")])
        torn = bytearray(gc_card([("GALE", "A"), ("GM4E", "B")]))
        torn[0x1FFC + 0x2000] ^= 0xFF                                                          # the newer directory is damaged: the backup counts
        self.assertEqual([e.code for e in memcards.read(self.put("torn.raw", bytes(torn))).entries], ["GALE"])
        self.assertIsNone(memcards.read(self.put("nothing.raw", b"\0" * 0x80000)))

    def test_a_playstation_2_card_names_the_serials(self) -> None:
        path = self.put("Mcd001.ps2", ps2_card_image({"BASLUS-20946GTA50000": {"a": b"x" * 10}, "BADATA-SYSTEM": {"icon.sys": b"i"}}))
        card = memcards.read(path)
        self.assertEqual((card.kind, [e.code for e in card.entries]), (memcards.PS2, ["SLUS-20946"]))          # (the console's own data is no game)

    def test_what_is_no_card_is_none(self) -> None:
        self.assertIsNone(memcards.read(self.put("a.srm", b"\0" * 8192)))                         # a cartridge save
        self.assertIsNone(memcards.read(self.put("b.srm", b"x" * 131072)))                        # the size of a card, not one
        self.assertIsNone(memcards.read(self.dir / "missing.srm"))
        self.assertTrue(memcards.sniff_size(131072) and not memcards.sniff_size(32768))


class InTheSaveIndex(unittest.TestCase):
    def test_a_card_shows_its_games_and_an_unmatched_one_says_what_it_looks_like(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saves = Path(tmp)
            (saves / "MK-51000.A1.bin").write_bytes(vmu_card([("SONICADV_INT", "SONICADV", "SONIC ADVENTURE / Main Save File")]))
            (saves / "Other.A1.bin").write_bytes(vmu_card([("X.001", "", "Something Else")]))
            (saves / "Sonic Adventure (USA).A1.bin").write_bytes(vmu_card([("SONICADV_INT", "SONICADV", "SONIC ADVENTURE")]))
            ref = ("Sega - Dreamcast", "Sonic Adventure (USA)", "Sonic Adventure")
            entries = [(f, saves, True) for f in sorted(saves.iterdir())]
            report = saveindex.build(entries, {}, {"Sonic Adventure (USA)": ref})
            by = {s.name: s for s in report.sets.values()}
            self.assertEqual(by["MK-51000"].match, saveindex.NONE)
            self.assertEqual(by["MK-51000"].guess, "Sonic Adventure")                              # its card says which title it holds
            self.assertEqual(by["Other"].guess, "")                                                # a title the DAT does not have: no guess
            self.assertEqual(by["Sonic Adventure (USA)"].cards[0]["games"], ["SONIC ADVENTURE"])
            self.assertEqual(report.totals()["cards"], 3)
            self.assertEqual(report.totals()["guessed_sets"], 1)
            row = [r for r in report.rows() if r["name"] == "MK-51000"][0]
            self.assertEqual((row["guess"], row["cards"][0]["label"]), ("Sonic Adventure", "Dreamcast VMU"))


    def test_a_save_whose_name_is_a_title_read_loosely_says_which(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saves = Path(tmp)
            names = ["2375 - Sonic Advance (J) (M2) (v1.1).srm", "Sonic Advance (USA).srm", "Yoshi (USA) (Rev 9).state1", "Nothing At All.srm"]
            for n in names:
                (saves / n).write_bytes(b"s")
            refs = {"Sonic Advance (USA)": ("Nintendo - Game Boy Advance", "Sonic Advance (USA)", "Sonic Advance"),
                    "Yoshi (USA)": ("Nintendo - Game Boy Advance", "Yoshi (USA)", "Yoshi")}
            report = saveindex.build([(saves / n, saves, True) for n in names], {}, refs)
            by = {s.name: s for s in report.sets.values()}
            self.assertEqual((by["Sonic Advance (USA)"].match, by["Sonic Advance (USA)"].guess), (saveindex.DAT, ""))      # an exact name is a match, no guess
            self.assertEqual((by["2375 - Sonic Advance (J) (M2) (v1.1)"].match, by["2375 - Sonic Advance (J) (M2) (v1.1)"].guess), (saveindex.NONE, "Sonic Advance"))
            self.assertEqual(by["Yoshi (USA) (Rev 9)"].guess, "Yoshi")
            self.assertEqual(by["Nothing At All"].guess, "")
            self.assertEqual((report.totals()["unmatched_sets"], report.totals()["guessed_sets"]), (3, 2))


    def test_a_save_of_another_system_the_core_also_plays_is_not_counted_here(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saves = Path(tmp)
            names = ["Alex Kidd (Europe).srm", "Shitou (Japan) (Rev 2).srm", "Mystery.srm", "Mine (USA).srm"]
            for n in names:
                (saves / n).write_bytes(b"s")
            mine = {"Mine (USA)": ("Sega - Master System", "Mine (USA)", "Mine")}
            report = saveindex.build([(saves / n, saves, True) for n in names], {}, mine)
            self.assertEqual(report.totals()["unmatched_sets"], 3)
            other_titles = {"Sega Mega Drive - Genesis": (saveindex.title_table(["Shitou", "Alex Kidd"]), "Sega - Mega Drive - Genesis")}
            marked = report.mark_foreign({"Alex Kidd (Europe)": "Sega Mega Drive - Genesis"}, other_titles)
            self.assertEqual(marked, 2)                                             # by its exact name, and by its title read loosely
            by = {s.name: s for s in report.sets.values()}
            self.assertEqual((by["Alex Kidd (Europe)"].other, by["Shitou (Japan) (Rev 2)"].other, by["Mystery"].other), ("Sega Mega Drive - Genesis",) * 2 + ("",))
            self.assertEqual((report.totals()["sets"], report.totals()["unmatched_sets"]), (2, 1))       # only what is (or may be) this system's
            self.assertEqual(sorted(r["name"] for r in report.rows()), ["Mine (USA)", "Mystery"])


if __name__ == "__main__":
    unittest.main()
