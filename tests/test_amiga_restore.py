"""The Amiga end to end: exclude a game, build (it is archived), restore it with /api/archive/restore, build again. A restored game
lands where a build puts it (a folder of its title below ``Commodore Amiga - Games``), is not archived twice, and two games of one
title (``library.KEEP_APART``: a publisher decides) get folders of their own instead of one shared folder."""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(__file__))

from servercase import ServerCase, put, tree  # noqa: E402

PLAT = "Commodore Amiga"
GAMES = "Commodore Amiga - Games - [ADF]"
GAMES_DIR = "Commodore Amiga - Games"
ZAP = "Zap Quest (1990)(Acme)"
OV1 = "Overlord (1990)(Virgin Mastertronic)"
OV2 = "Overlord (1991)(Virgin)"
NAMES = [ZAP, OV1, OV2, "Solo Game (1993)(Pub)"]


class AmigaRestore(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.data: dict[str, bytes] = {}
        lines = ['<?xml version="1.0"?><datafile>', f"<header><name>{GAMES}</name><version>2025-01-30</version></header>"]
        for i, name in enumerate(NAMES):
            d = bytes((i * 37 + j) % 251 for j in range(1024)) + name.encode()
            self.data[name] = d
            lines.append(f'<game name="{name}"><description>{name}</description><rom name="{name}.adf" size="{len(d)}" '
                         f'crc="{zlib.crc32(d):08x}" md5="{hashlib.md5(d).hexdigest()}" sha1="{hashlib.sha1(d).hexdigest()}"/></game>')
        (self.tmp / "data" / "dats").mkdir(parents=True, exist_ok=True)
        (self.tmp / "data" / "dats" / f"{GAMES} (TOSEC-v2025-01-30_CM).dat").write_text("\n".join(lines) + "</datafile>")
        self.root = self.tmp / "amiga"
        for n in NAMES:
            put(self.root / f"{n}.adf", self.data[n])
        self.scan()

    def scan(self) -> None:
        self.job("/api/scan", {"path": str(self.root), "platform": PLAT})

    def build(self) -> dict:
        self.scan()
        return self.job("/api/library/apply", {"aside_to": str(self.tmp / "rom-archive")})

    def override(self, action: str, *names: str) -> None:
        self.call("POST", "/api/library/override", {"platform": PLAT, "action": action,
                                                    "games": [{"dat": GAMES, "game": n + ".adf"} for n in names]})

    def archived(self) -> list[str]:
        return [t for t in tree(self.tmp / "rom-archive") if not t.endswith("/")]

    def adfs(self, word: str) -> list[str]:
        return [t for t in tree(self.root) if word in t and t.endswith(".adf")]

    def test_a_restored_game_lands_in_its_folder_and_is_not_archived_twice(self) -> None:
        self.override("exclude", ZAP)
        self.build()
        self.assertEqual(self.adfs("Zap Quest"), [])
        archived = [t for t in self.archived() if "Zap Quest" in t]
        self.assertEqual(len(archived), 1, self.archived())
        res = self.job("/api/archive/restore", {"platform": PLAT, "paths": [archived[0].split("/", 1)[1]]})
        self.assertEqual((res["restored"], res["placed"], res["failed"]), (1, 1, []))
        want = f"{GAMES_DIR}/Zap Quest/{ZAP}.adf"
        self.assertEqual(self.adfs("Zap Quest"), [want])
        self.assertEqual((self.root / want).read_bytes(), self.data[ZAP])
        self.override("clear", ZAP)
        before = self.archived()
        self.build()
        self.assertEqual(self.adfs("Zap Quest"), [want])                 # kept now: it stays
        self.assertEqual(self.archived(), before)                         # and nothing is archived a second time

    def test_a_restored_game_that_is_still_excluded_is_not_archived_twice(self) -> None:
        self.override("exclude", ZAP)
        self.build()
        rel = [t for t in self.archived() if "Zap Quest" in t][0].split("/", 1)[1]
        self.job("/api/archive/restore", {"platform": PLAT, "paths": [rel]})
        before = self.archived()
        self.build()
        self.assertEqual(self.adfs("Zap Quest"), [])
        self.assertEqual(self.archived(), before)

    def test_games_of_one_title_get_separate_folders_on_restore(self) -> None:
        self.override("exclude", OV1, OV2)
        self.build()
        self.assertEqual(self.adfs("Overlord"), [])
        rels = [t.split("/", 1)[1] for t in self.archived() if "Overlord" in t]
        self.assertEqual(len(rels), 2, self.archived())
        res = self.job("/api/archive/restore", {"platform": PLAT, "paths": rels})
        self.assertEqual((res["restored"], res["placed"], res["failed"]), (2, 2, []))
        adfs = self.adfs("Overlord")
        self.assertEqual(len(adfs), 2, tree(self.root))
        self.assertEqual(len({a.rsplit("/", 1)[0] for a in adfs}), 2, adfs)          # one folder each
        self.override("clear", OV1, OV2)                                              # a build with both kept names the folders as it always does
        before = self.archived()
        self.build()
        self.assertEqual(self.archived(), before)
        adfs = self.adfs("Overlord")
        self.assertEqual(len(adfs), 2)
        self.assertEqual(len({a.rsplit("/", 1)[0] for a in adfs}), 2, adfs)

    def test_one_of_two_games_restored_beside_the_other_does_not_share_its_folder(self) -> None:
        self.override("exclude", OV2)
        self.build()
        self.assertEqual(len(self.adfs("Overlord")), 1, tree(self.root))
        rel = [t.split("/", 1)[1] for t in self.archived() if "Overlord" in t][0]
        self.job("/api/archive/restore", {"platform": PLAT, "paths": [rel]})
        adfs = self.adfs("Overlord")
        self.assertEqual(len(adfs), 2)
        self.assertEqual(len({a.rsplit("/", 1)[0] for a in adfs}), 2, adfs)


if __name__ == "__main__":
    unittest.main()
