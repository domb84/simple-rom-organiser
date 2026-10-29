"""'Games you have' counts games, not database entries, on every system (Amendment 90)."""

from __future__ import annotations

import unittest

from tests.servercase import SNES, ServerCase, put  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)


class CartridgeGames(ServerCase):
    # (No-Intro: an entry a region / revision; the regions and revisions of a game are one game)
    GAMES = {**ServerCase.GAMES,
             "snes": ["Alpha (USA)", "Alpha (Europe)", "Alpha (Japan)", "Beta (USA)", "Beta (USA) (Rev 1)", "Gamma (USA)"]}

    def test_the_regions_and_revisions_of_a_game_are_one_game(self) -> None:
        root = self.tmp / "snes"
        put(root / "a.sfc", self.rom["Alpha (Europe)"])
        put(root / "b.sfc", self.rom["Beta (USA)"])
        self.call("POST", "/api/scan", {"path": str(root), "platform": SNES})
        self.wait()
        s = self.call("GET", "/api/status")["scan"]["summary"]
        self.assertEqual((s["games_have"], s["games_total"], s["games_missing"], s["count_by"]), (2, 3, 1, "game"))   # (Alpha, Beta, Gamma: you have Alpha and Beta)
        self.assertEqual(s["dat_total"], 6)                                                    # (the database's entries are still there)


if __name__ == "__main__":
    unittest.main()
