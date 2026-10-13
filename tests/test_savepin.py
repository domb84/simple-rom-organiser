"""Saves lying in a ROM folder are set aside under the game they belong to: ``<archive>/<system>/_saves/<Title>/<file>``."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.servercase import ServerCase, GBA, tree  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)
from tests.test_memcards import gc_card, vmu_card

from romorg import savepin, server, sortroot
from romorg.datfile import DatFile, Rom
from romorg.organiser import RenameOp


def rom(name: str, serial: str = "", dat: str = "Test") -> Rom:
    return Rom(name + ".iso", 1, "0", "", "", name, dat, name, serial=serial)


class Pinning(unittest.TestCase):
    def setUp(self) -> None:
        roms = [rom("Super Smash Bros. Melee (USA)", "DL-DOL-GALE-USA"), rom("Mario Kart: Double Dash!! (USA)", "DL-DOL-GM4E-USA"),
                rom("Sonic Adventure (USA)"), rom("Sonic Adventure (Europe)"), rom("Legacy of Kain - Soul Reaver (USA)")]
        self.pin = savepin.Pinner([DatFile("Test", "Test", "1", roms, format="clrmamepro")])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def put(self, name: str, data: bytes = b"x") -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_a_save_is_pinned_by_the_name_of_its_game_and_the_editions_share_a_title(self) -> None:
        self.assertEqual(self.pin.titles(self.put("Sonic Adventure (USA).srm")), ["Sonic Adventure"])
        self.assertEqual(self.pin.titles(self.put("Sonic Adventure (Europe).state1")), ["Sonic Adventure"])
        self.assertEqual(self.pin.titles(self.put("Sonic Adventure (USA).A1.bin", vmu_card([]))), ["Sonic Adventure"])
        self.assertEqual(self.pin.titles(self.put("Sonic Adventure (USA).zip")), [])             # not a save
        self.assertEqual(self.pin.titles(self.put("Nothing Like It.srm")), [])

    def test_a_gamecube_card_is_pinned_to_every_game_it_holds(self) -> None:
        card = self.put("MemoryCardA.raw", gc_card([("GALE", "Smash"), ("GM4E", "Kart"), ("XXXX", "Unknown game")]))
        self.assertEqual(self.pin.titles(card), ["Super Smash Bros. Melee", "Mario Kart: Double Dash!!"])

    def test_a_dreamcast_card_is_pinned_by_a_title_only_one_game_has(self) -> None:
        vmu = self.put("T36802N.A1.bin", vmu_card([("SOUL.001", "", "Legacy of Kain: Soul Reaver")]))
        self.assertEqual(self.pin.titles(vmu), ["Legacy of Kain - Soul Reaver"])
        self.assertEqual(self.pin.titles(self.put("X.A1.bin", vmu_card([("A.001", "", "Something Else")]))), [])

    def test_the_other_files_of_a_save_set_follow_the_one_that_names_the_game(self) -> None:
        self.put("MK-51000.A1.bin", vmu_card([("SONICADV_INT", "", "SONIC ADVENTURE / Main Save File")]))
        empty = self.put("MK-51000.B1.bin", vmu_card([]))
        state = self.put("MK-51000.state1", b"state")
        self.assertEqual(self.pin.titles(empty), ["Sonic Adventure"])          # the empty card of slot B belongs to the same game
        self.assertEqual(self.pin.titles(state), ["Sonic Adventure"])
        self.assertEqual(self.pin.titles(self.put("MK-51000X.B1.bin", vmu_card([]))), [])      # another name: another set

    def test_a_name_read_loosely_is_pinned_when_it_is_one_title_and_a_leading_number_is_kept_when_the_title_has_one(self) -> None:
        roms = [rom("Sonic Advance (USA)"), rom("Sonic Advance (Japan)"), rom("007 - The World Is Not Enough (USA)"), rom("World Is Not Enough, The (USA)"),
                rom("Rat Attack (Europe)"), rom("Rat Attack! (USA)"), rom("Tony Hawk's Pro Skater 2 (USA)")]
        pin = savepin.Pinner([DatFile("Test", "Test", "1", roms, format="clrmamepro")])
        for name, want in (("2375 - Sonic Advance (J) (M2) (v1.1).srm", ["Sonic Advance"]),              # a scene number and GoodTools codes
                           ("Sonic Advance (USA) (Rev 1).state", ["Sonic Advance"]),                       # a revision the DAT does not list
                           ("Tony Hawk's Pro Skater 2 (USA) - Copy.srm", ["Tony Hawk's Pro Skater 2"]),
                           ("007 - The World Is Not Enough (Europe).srm", ["007 - The World Is Not Enough"]),   # (the number is the title's)
                           ("Rat Attack! (Germany).srm", ["Rat Attack!"]),                                 # two titles but for a "!": the exact one
                           ("rat attack (Germany).srm", []),                                               # ... and neither when that does not settle it
                           ("Sonic (USA).srm", [])):
            self.assertEqual(pin.titles(self.put(name)), want, name)


class Archive(unittest.TestCase):
    def test_a_save_of_several_games_is_moved_under_the_first_and_copied_under_the_others_and_undo_takes_both_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, aside = Path(tmp) / "gc", Path(tmp) / "archive"
            root.mkdir()
            card = root / "MemoryCardA.raw"
            card.write_bytes(b"card")
            op = RenameOp(card, root / "_saves" / "Smash" / card.name, "move", "a save of Smash (and 1 more)", "", "move", "", True)
            op.copies = ("Kart",)
            plan = SimpleNamespace(ops=[op])
            keep, arch = server.App._archive_pass(root, plan, aside)
            self.assertEqual(keep, [])
            res = sortroot.apply_moves(arch, Path(tmp) / "j", "libsweep", keep=[root, aside])
            self.assertEqual((res["moved"], res["failed"]), (2, []))
            self.assertEqual(tree(aside), ["gc/", "gc/_saves/", "gc/_saves/Kart/", "gc/_saves/Kart/MemoryCardA.raw", "gc/_saves/Smash/",
                                           "gc/_saves/Smash/MemoryCardA.raw"])
            self.assertFalse(card.exists())                                                   # the original left the ROM folder
            back = sortroot.undo_moves(Path(res["journal"]), keep=[root, aside])
            self.assertEqual((back["restored"], back["skipped"]), (2, []))
            self.assertEqual(card.read_bytes(), b"card")
            self.assertEqual(tree(aside), [])                                                  # and the copy is gone


class Flat(ServerCase):
    def test_a_save_in_a_roms_folder_goes_to_the_archive_under_its_game(self) -> None:
        root = self.tmp / "gba-roms"
        root.mkdir()
        (root / "Run (USA).gba").write_bytes(self.rom["Run (USA)"])
        for name in ("Run (USA).srm", "Run (USA).state1", "Bash (USA).srm", "stray.srm", "notes.txt"):
            (root / name).write_bytes(b"save " + name.encode())
        self.call("POST", "/api/scan", {"path": str(root), "platform": GBA})
        self.wait()
        archive = self.tmp / "rom-archive"
        plan = self.call("POST", "/api/library/plan", {"platform": GBA, "limit": 50, "aside_to": str(archive)})
        by = {Path(r["from"]).name: r for r in plan["items"]}
        self.assertEqual((by["Run (USA).srm"]["to"], by["Run (USA).srm"]["category"]), ("_saves/Run/Run (USA).srm", "saves"))
        self.assertEqual(by["Run (USA).state1"]["to"], "_saves/Run/Run (USA).state1")
        self.assertEqual(by["Bash (USA).srm"]["to"], "_saves/Bash/Bash (USA).srm")             # a game you have no ROM for, but the DAT knows
        self.assertEqual((by["stray.srm"]["to"], by["stray.srm"]["category"]), ("_unmatched/stray.srm", "unmatched"))       # no game: just unmatched
        self.assertEqual(by["notes.txt"]["category"], "unmatched")
        self.call("POST", "/api/library/apply", {"platform": GBA, "aside_to": str(archive), "plan_id": plan["plan_id"]})
        self.wait()
        got = tree(archive)
        for want in ("gba-roms/_saves/Run/Run (USA).srm", "gba-roms/_saves/Run/Run (USA).state1", "gba-roms/_saves/Bash/Bash (USA).srm",
                     "gba-roms/_unmatched/stray.srm"):
            self.assertIn(want, got)
        self.assertEqual(sorted(p for p in tree(root) if "." in p), ["Run (USA).gba"])           # the ROM folder holds the ROM and nothing else
        again = self.call("POST", "/api/library/plan", {"platform": GBA, "limit": 50, "refresh": True})
        self.assertEqual([r for r in again["items"] if r["status"] == "move"], [])                # (nothing left to do)


class Trees(ServerCase):
    def tree(self, side: str, path: str = "") -> dict:
        return self.call("GET", f"/api/tree?platform=Nintendo%20Game%20Boy%20Advance&side={side}&path={path}")

    def test_the_trees_show_where_every_file_ends_up_and_change_nothing(self) -> None:
        root = self.tmp / "gba-roms"
        root.mkdir()
        (root / "run.gba").write_bytes(self.rom["Run (USA)"])                                   # a ROM under another name: it is renamed
        for name in ("Run (USA).srm", "stray.bin"):
            (root / name).write_bytes(b"x" * 10)
        archive = self.tmp / "rom-archive"
        (archive / "gba-roms" / "_excluded").mkdir(parents=True)
        (archive / "gba-roms" / "_excluded" / "old.gba").write_bytes(b"o" * 7)                    # (an earlier build left this)
        self.assertFalse(self.call("GET", "/api/tree?platform=Nintendo%20Game%20Boy%20Advance&side=library")["ready"])      # no folder, no scan yet
        self.call("POST", "/api/scan", {"path": str(root), "platform": GBA})
        self.wait()
        before = (tree(root), tree(archive))
        systems = self.call("GET", "/api/tree/systems")
        mine = [s for s in systems["systems"] if s["platform"] == GBA][0]
        self.assertEqual((mine["folder"], mine["archive"], mine["scanned"]), (str(root), str(archive / "gba-roms"), True))
        lib = self.tree("library")
        self.assertEqual({f["name"]: f["state"] for f in lib["files"]}, {"Bash (USA)": "missing", "Run (USA).gba": "renamed"})   # (the game the folder lacks, listed where it would be)
        mine_row = [f for f in lib["files"] if f["name"] == "Run (USA).gba"][0]
        self.assertEqual((mine_row["from"], lib["dirs"], lib["totals"]["changed"], lib["totals"]["files"], lib["totals"]["missing"]), ("run.gba", [], 1, 1, 1))
        self.assertEqual((mine_row["row"]["category"], mine_row["row"]["game_ref"]["game"], mine_row["row"]["tags"]["regions"]), ("kept", "Run (USA)", ["USA"]))   # its row of the build
        self.assertEqual(lib["sides"], {"library": 1, "archive": 3})
        self.assertEqual(lib["facets"]["show"], {"": 2, "have": 1, "missing": 1, "bios": 0, "unmatched": 0, "saves": 0})
        only = self.call("GET", "/api/tree?platform=Nintendo%20Game%20Boy%20Advance&side=library&show=missing")
        self.assertEqual([f["name"] for f in only["files"]], ["Bash (USA)"])
        found = self.call("GET", "/api/tree?platform=Nintendo%20Game%20Boy%20Advance&side=archive&q=RUN")
        self.assertEqual((found["totals"]["files"], [d["name"] for d in found["dirs"]]), (1, ["_saves"]))        # the search narrows the tree and its totals
        self.assertEqual(self.call("GET", "/api/tree?platform=Nintendo%20Game%20Boy%20Advance&side=archive&state=existing")["totals"]["files"], 1)
        self.assertEqual(self.http("GET", "/api/tree?platform=Nintendo%20Game%20Boy%20Advance&show=nonsense")[0], 400)
        arch = self.tree("archive")
        self.assertEqual({d["name"]: (d["files"], d["changed"]) for d in arch["dirs"]}, {"_excluded": (1, 0), "_saves": (1, 1), "_unmatched": (1, 1)})
        saves = self.tree("archive", "_saves/Run")
        self.assertEqual([(f["name"], f["state"], f["from"]) for f in saves["files"]], [("Run (USA).srm", "incoming", "Run (USA).srm")])
        self.assertEqual(self.tree("archive", "_excluded")["files"][0]["state"], "existing")
        code, _ = self.http("GET", "/api/tree?platform=Nintendo%20Game%20Boy%20Advance&side=archive&path=nowhere")
        self.assertEqual(code, 404)
        self.assertEqual((tree(root), tree(archive)), before)                                   # a preview: nothing moved


class SeveralGames:
    """A card that holds the saves of two games is under both after a build, and an undo takes all of it back."""

    def build_and_undo(self, aside: bool) -> None:
        root = self.tmp / "gba-roms"
        root.mkdir()
        (root / "Run (USA).gba").write_bytes(self.rom["Run (USA)"])
        card = vmu_card([("RUN.001", "", "Run"), ("BASH.001", "", "Bash")])
        (root / "card.bin").write_bytes(card)
        self.call("POST", "/api/scan", {"path": str(root), "platform": GBA})
        self.wait()
        before = (tree(root), tree(self.tmp / "rom-archive"))
        extra = {"aside_to": str(self.tmp / "rom-archive")} if aside else {}
        plan = self.call("POST", "/api/library/plan", {"platform": GBA, "limit": 50, **extra})
        row = [r for r in plan["items"] if r["from"] == "card.bin"][0]
        self.assertEqual((row["to"], row["category"]), ("_saves/Run/card.bin", "saves"))             # the first title: the card's first save
        self.call("POST", "/api/library/apply", {"platform": GBA, "plan_id": plan["plan_id"], **extra})
        job = self.wait()
        self.assertEqual(job["status"], "done", job)
        base = (self.tmp / "rom-archive" / "gba-roms") if aside else root
        self.assertEqual((base / "_saves" / "Run" / "card.bin").read_bytes(), card)
        self.assertEqual((base / "_saves" / "Bash" / "card.bin").read_bytes(), card)                 # ... and a copy under the other
        self.assertFalse((root / "card.bin").exists())
        logs = self.call("GET", f"/api/organise/undo-logs?path={root}")["logs"]
        self.call("POST", "/api/organise/undo", {"log": logs[0]["log"]})
        self.assertEqual(self.wait()["status"], "done")
        self.assertEqual((tree(root), tree(self.tmp / "rom-archive")), before)                      # the card is back, both copies are gone


class SeveralGamesIntoTheArchive(SeveralGames, ServerCase):
    def test_moved_under_one_copied_under_the_other_and_undone(self) -> None:
        self.build_and_undo(aside=True)


class SeveralGamesInTheFolder(SeveralGames, ServerCase):
    ARCHIVE = False

    def test_moved_under_one_copied_under_the_other_and_undone(self) -> None:
        self.build_and_undo(aside=False)


class TreesWithoutAnArchive(ServerCase):
    ARCHIVE = False

    def test_what_is_set_aside_stays_in_the_rom_folders_tree(self) -> None:
        root = self.tmp / "gba-roms"
        root.mkdir()
        (root / "Run (USA).gba").write_bytes(self.rom["Run (USA)"])
        (root / "Run (USA).srm").write_bytes(b"s")
        (root / "stray.bin").write_bytes(b"j")
        (root / ".nomedia").write_bytes(b"n")                                          # (a hidden file: a scan does not look at it)
        (root / "...GAMELIST.txt").write_bytes(b"list")                                # (several dots: no hidden file, a stray one)
        self.call("POST", "/api/scan", {"path": str(root), "platform": GBA})
        self.wait()
        lib = self.call("GET", "/api/tree?platform=Nintendo%20Game%20Boy%20Advance&side=library")
        self.assertEqual({d["name"]: d["changed"] for d in lib["dirs"]}, {"_saves": 1, "_unmatched": 2})
        self.assertEqual([(f["name"], f["state"]) for f in lib["files"]], [(".nomedia", "ignored"), ("Bash (USA)", "missing"), ("Run (USA).gba", "same")])   # every file of the folder
        self.assertEqual(lib["totals"], {"files": 5, "bytes": 2048 + 3 + 4, "changed": 3, "missing": 1})
        gone = self.call("GET", "/api/tree?platform=Nintendo%20Game%20Boy%20Advance&side=library&path=_unmatched")
        self.assertEqual(sorted(f["name"] for f in gone["files"]), ["...GAMELIST.txt", "stray.bin"])
        arch = self.call("GET", "/api/tree?platform=Nintendo%20Game%20Boy%20Advance&side=archive")
        self.assertTrue(arch["ready"] and arch["none"])
        self.assertEqual(self.call("GET", "/api/tree/systems")["systems"][0]["archive"], "")


if __name__ == "__main__":
    unittest.main()
