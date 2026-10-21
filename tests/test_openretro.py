"""The OpenRetro game database (``romorg.openretro``): the sync pages, what is kept, a release by its disks, a game's titles joining
TOSEC's (``romorg.alttitles``); and what a release needs (``romorg.amigahw``)."""

from __future__ import annotations

import io
import json
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from romorg import alttitles, amigahw, openretro


def page(records: list[tuple[int, str, dict | None]]) -> bytes:
    out = b""
    for sid, uid, data in records:
        body = zlib.compress(json.dumps(data).encode()) if data is not None else b""
        out += struct.pack(">I", sid) + bytes.fromhex(uid) + struct.pack(">I", len(body)) + body
    return out


GAME = "11111111111111111111111111111111"
OTHER = "22222222222222222222222222222222"


def variant(n: int, parent: str, name: str, sha1: str, sid: int = 0, **extra: str) -> tuple[int, str, dict]:
    return (sid or n, f"{n:032x}", {"_type": "2", "parent_uuid": parent[:8] + "-" + parent[8:12] + "-" + parent[12:16] + "-" + parent[16:20] + "-" + parent[20:],
                            "x_name": name, "variant_name": "ADF",
                            "file_list": json.dumps([{"name": "Disk1.adf", "sha1": sha1, "size": 901120},
                                                     {"name": "Game.info", "sha1": "f" * 40, "size": 900}]), **extra})


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "openretro" / "amiga.json.gz"
        first = [(1, GAME, {"_type": "1", "game_name": "Der Clou!", "year": "1994", "publisher": "Neo",
                            "hol_url": "http://hol.abime.net/1"}),
                 (2, OTHER, {"_type": "1", "game_name": "Pinball Fantasies [AGA]"}),
                 variant(3, GAME, "Clue! (Amiga, TOSEC, ADF, 1994, Neo)", "a" * 40, kickstart="1.3", amiga_model="A500"),
                 variant(4, OTHER, "Pinball Fantasies (Amiga, TOSEC, ADF, 1993, 21st Century, AGA)", "b" * 40, chipset="AGA")]
        first += [variant(10 + i, GAME, f"Filler {i} (Amiga, TOSEC, ADF)", f"{i:040x}") for i in range(1000)]
        self.pages = {0: page(first), 1009: b""}

    def opener(self, req):
        sync = int(req.full_url.rsplit("sync=", 1)[1])
        return io.BytesIO(self.pages.get(sync, b""))

    def test_all_then_what_changed(self) -> None:
        r = openretro.download_and_build(out=self.out, opener=self.opener, pause=0)
        self.assertEqual((r["games"], r["variants"]), (2, 1002))
        db = openretro.database(self.out)
        rel = db.release(["A" * 40])
        self.assertEqual((rel["kickstart"], rel["model"], rel["game_fields"]["name"]), ("1.3", "A500", "Der Clou!"))
        self.assertEqual(rel["sha1"], ["a" * 40])                                     # (the disks only, not the icon)
        self.assertIsNone(db.release(["c" * 40]))
        titles = set(db.titles())
        self.assertIn(("Clue!", GAME), titles)
        self.assertIn(("Pinball Fantasies", OTHER), titles)                          # ("[AGA]" off)
        # later: a release changed, one deleted - only the pages after the last sync id are asked for
        self.pages = {1009: page([variant(3, GAME, "Clue! (Amiga, TOSEC, ADF)", "a" * 40, sid=1010, kickstart="2.0+"),
                                  (1011, f"{4:032x}", None)]),
                      0: b"wrong: asked from the start", 1011: b""}
        r = openretro.download_and_build(out=self.out, opener=self.opener, pause=0)
        self.assertEqual(r["variants"], 1001)
        db = openretro.database(self.out)
        self.assertEqual(db.release(["a" * 40])["kickstart"], "2.0+")
        self.assertIsNone(db.release(["b" * 40]))

    def test_age(self) -> None:
        self.assertEqual(openretro.check_update(path=self.out)["status"], "missing")
        openretro.download_and_build(out=self.out, opener=self.opener, pause=0)
        self.assertEqual(openretro.check_update(path=self.out)["status"], "up_to_date")
        self.assertEqual(openretro.check_update(path=self.out, gate=False)["status"], "update_available")

    def test_a_broken_page(self) -> None:
        with self.assertRaises(openretro.OpenRetroError):
            openretro.parse_page(struct.pack(">I", 1) + bytes(16) + struct.pack(">I", 99) + b"short")

    def test_the_titles_of_one_game_join(self) -> None:
        openretro.download_and_build(out=self.out, opener=self.opener, pause=0)
        names = openretro.database(self.out).titles() + [("APB & Xybots", OTHER)]
        with mock.patch.object(alttitles, "_openretro_names",
                               side_effect=lambda p, o: [(t, g) for t, g in names if "&" not in t] if p == "Commodore Amiga" else []):
            groups = alttitles._build("", "Commodore Amiga", None, None, self.out)
        joined = next(v for v in groups.values() if alttitles.node("Clue!") in v)
        self.assertIn(alttitles.node("Der Clou!"), joined)
        self.assertFalse(any(alttitles.node("APB & Xybots") in v for v in groups.values()))


def slave(flags: int, exp: int = 0) -> bytes:
    code = b"\x70\xff\x4e\x75" + b"WHDLOADS" + struct.pack(">HHI", 17, flags, 0x80000) + bytes(12) + struct.pack(">i", exp) + bytes(28)
    n = len(code) // 4
    return struct.pack(">IIIIII", 0x3F3, 0, 1, 0, 0, n) + struct.pack(">II", 0x3E9, n) + code + struct.pack(">I", 0x3F2)


class RequirementsTests(unittest.TestCase):
    def test_from_the_names(self) -> None:
        r = amigahw.requirements(["Pinball Fantasies (1992)(21st Century)(AGA)(Disk 1 of 4).adf"])
        self.assertEqual((r["chipset"], r["machine"], r["whdload"]), ("AGA", "A1200", "A1200"))
        self.assertEqual(amigahw.requirements(["Lemmings (1991)(Psygnosis).adf"])["machine"], "A500")
        self.assertEqual(amigahw.requirements(["Zool (1993)(Gremlin)(ECS).adf"])["machine"], "A600")
        self.assertEqual(amigahw.requirements(["Body Blows (1993)(Team17)(OCS-AGA).adf"])["machine"], "A500")
        cd = amigahw.requirements(["Chaos Engine, The (1994)(Renegade)(CD32).iso"])
        self.assertEqual((cd["machine"], cd["whdload"], cd["chipset"]), ("CD32", "CD32", "AGA"))

    def test_from_openretro_and_the_slave(self) -> None:
        r = amigahw.requirements(["Game (1992)(Pub).adf"], release={"kickstart": "2.0+", "video": "NTSC"})
        self.assertEqual((r["machine"], r["video"]), ("A600", "NTSC"))
        flags = amigahw.slave_flags(slave(0x30))
        self.assertEqual((flags["needs_aga"], flags["needs_68020"], flags["version"]), (True, True, 17))
        r = amigahw.requirements(["Game (1993)(Pub).adf"], slave=slave(0x20))
        self.assertEqual((r["chipset"], r["machine"]), ("AGA", "A1200"))
        self.assertIn("WHDLoad slave: needs AGA", r["sources"])
        self.assertIsNone(amigahw.slave_flags(b"not a slave"))
        self.assertEqual(amigahw.slave_flags(slave(0, -0x100000))["exp_optional"], True)
        r = amigahw.requirements(["Worms - The Director's Cut (1997)(Team 17)(AGA).adf"], slave=slave(0x20, 12 << 20))
        self.assertEqual((r["whdload"], r["fast_memory"]), ("A1200", 12 << 20))         # (what the slave asks for: information)
        self.assertEqual(amigahw.requirements(["G (1993)(P)(AGA).adf"], slave=slave(0x20, -(4 << 20)))["fast_memory"], 0)
