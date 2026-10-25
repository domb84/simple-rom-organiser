"""WHDLoad installs (``romorg.whdinstalls``): the list of installs, matching a game to its install, the download next to its disks."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from romorg import scanner, whdinstalls
from tests.test_lha import level0

ROW = ('<TR><TD ALIGN=CENTER><A HREF="{arc}">{title}</A></TD><TD><A HREF="x.html">Info</A></TD><TD ALIGN=RIGHT>{size}</TD>'
       '<TD ALIGN=CENTER>2021-04-07</TD><TD ALIGN=RIGHT>15000</TD><TD ALIGN=CENTER><A HREF="/cgi-bin/wbug.php?name=x">Wepl</A></TD>'
       '<TD ALIGN=CENTER><A HREF="http://hol.abime.net/{hol}">{hol}</A></TD><TD></TD><TD></TD><TD></TD></TR>')
INSTALLS = [("Turrican.lha", "Turrican"), ("Pushover.lha", "Pushover"), ("SensibleSoccer.lha", "Sensible Soccer"),
            ("SensibleSoccerCD32.lha", "Sensible Soccer CD&sup3;&sup2;"), ("Exile.lha", "Exile"), ("Cadaver.lha", "Cadaver"),
            ("ReturnOfTheJedi.lha", "Return of the Jedi"), ("StarWars.lha", "Star Wars"),
            ("PrinceOfPersia.lha", "Prince of Persia / Prince de Perse"), ("CaptainFizz.lha", "Captain Fizz"), ("Lemmings.lha", "Lemmings"),
            ("ClassicArcadia.lha", "Classic Arcadia"), ("KickOff2.lha", "Kick Off 2"), ("KickOff2FinalWhistle.lha", "Kick Off 2 - The Final Whistle"),
            ("AlienBreed2.lha", "Alien Breed II"), ("Xenon2.lha", "Xenon 2"), ("MegaBlast.lha", "MegaBlast"),
            ("AstroMarineCorps.lha", "Astro Marine Corps"), ("DungeonMaster.lha", "Dungeon Master"), ("Legend.lha", "Legend"),
            ("ConquestsOfTheLongbow.lha", "Conquests Of The Longbow")]
PAGE = "<TABLE>" + "".join(ROW.format(arc=a, title=t, size=90000 + n, hol=100 + n) for n, (a, t) in enumerate(INSTALLS)) + "</TABLE>"


class _Resp(io.BytesIO):
    pass


class CatalogueAndMatching(unittest.TestCase):
    def setUp(self) -> None:
        self.installs = whdinstalls.parse_catalogue(PAGE)
        self.m = whdinstalls.Matcher(self.installs)

    def test_the_list_is_read(self) -> None:
        self.assertEqual(len(self.installs), len(INSTALLS))
        first = self.installs[0]
        self.assertEqual((first["archive"], first["title"], first["size"], first["date"], first["hol"]),
                         ("Turrican.lha", "Turrican", 90000, "2021-04-07", "100"))
        self.assertEqual(self.installs[3]["title"], "Sensible Soccer CD³²")

    def match(self, title: str):
        r = self.m.match(title)
        return (r[0]["archive"], r[1]) if r else None

    def test_a_game_finds_its_install(self) -> None:
        self.assertEqual(self.match("Turrican"), ("Turrican.lha", "title"))
        self.assertEqual(self.match("Push-Over"), ("Pushover.lha", "spelling"))
        self.assertEqual(self.match("Sensible Soccer"), ("SensibleSoccer.lha", "title"))          # (not the CD32 one)
        self.assertEqual(self.match("Cadaver - The Last Supper"), ("Cadaver.lha", "main title"))
        self.assertEqual(self.match("Star Wars - Return of the Jedi"), ("StarWars.lha", "main title"))     # (both a game: no OpenRetro data,
        from romorg.alttitles import node                                                             #  the main title)
        with mock.patch.object(self.m, "_hols", return_value={node("Star Wars - Return of the Jedi"): {"106"}}):   # (OpenRetro's page of it)
            self.assertEqual(self.match("Star Wars - Return of the Jedi"), ("ReturnOfTheJedi.lha", "Hall of Light"))
            self.assertEqual(self.match("Turrican"), ("Turrican.lha", "title"))                  # (no page known for it)

    def test_a_subtitle_that_is_another_games_title(self) -> None:
        self.assertEqual(self.match("Xenon 2 - Megablast"), ("Xenon2.lha", "main title"))           # (MegaBlast: L.K. Avalon's, 1995)
        self.assertEqual(self.match("A.M.C. - Astro Marine Corps"), ("AstroMarineCorps.lha", "subtitle"))   # (the initials)
        self.assertEqual(self.match("Legend of Robin Hood - Conquests of the Longbow"), ("ConquestsOfTheLongbow.lha", "subtitle"))
        self.assertIsNone(self.match("Zork III - The Dungeon Master"))                              # (a numbered game's subtitle)
        self.assertIsNone(self.match("Sir Fred - The Legend"))                                      # (one word)
        self.assertIsNone(self.match("Dreadnoughts Data Disk - Bismarck"))

    def test_a_compilation_and_a_game_of_another_year(self) -> None:
        self.assertIsNone(self.match("Alien Breed - Tower Assault & Alien Breed II - The Horror Continues"))   # (two games)
        m = whdinstalls.Matcher(whdinstalls.parse_catalogue("<TABLE>" + ROW.format(arc="Hanse.lha", title="Hanse", size=1, hol=1)
                                                               + ROW.format(arc="Lemmings.lha", title="Lemmings", size=1, hol=2)
                                                               + ROW.format(arc="LemmingsXmas91.lha", title="Lemmings - X-Mas 1991", size=1, hol=3) + "</TABLE>"))
        self.assertIsNone(m.match("Hanse - Die Expedition"))                                        # (1994: not Ariolasoft's of 1988)
        self.assertEqual(m.match("Lemmings - Xmas Lemmings 1991")[0]["archive"], "LemmingsXmas91.lha")
        self.assertEqual(m.match("Hanse")[0]["archive"], "Hanse.lha")

    def test_another_title_of_the_install_and_a_short_title(self) -> None:
        self.assertEqual(self.match("Prince de Perse"), ("PrinceOfPersia.lha", "title"))
        self.assertEqual(self.match("Captain Fizz Meets the Blaster-Trons"), ("CaptainFizz.lha", "short title"))
        self.assertIsNone(self.match("Lemmings and Friends"))                                     # (one word: too short to tell)
        self.assertIsNone(self.match("Classic Arcadia & Baby Arcadia"))                          # (two games on one disk)

    def test_a_result_goes_by_the_exact_game_not_its_main_title(self) -> None:
        self.assertEqual(self.match("Kick Off 2 - KOA Competition"), ("KickOff2.lha", "main title"))     # (a suggestion to download)
        self.assertIsNone(self.m.match("Kick Off 2 - KOA Competition", strict=True))                   # (but no result is its)
        others = {"kick off 2", "kick off 2 koa competition"}                                           # (the games there are)
        self.assertIsNone(self.m.match("Kick Off 2 - KOA Competition", strict=True, others=others))    # (Kick Off 2 is a game alone)
        full = self.m.match("Alien Breed II - The Horror Continues", strict=True, others=others)       # (Alien Breed II is not)
        self.assertEqual((full[0]["archive"], full[1]), ("AlienBreed2.lha", "main title"))
        self.assertEqual([i["archive"] for i in self.m.exact("Kick Off 2")], ["KickOff2.lha"])         # (not the Final Whistle's)
        self.assertEqual([i["archive"] for i in self.m.exact("Kick Off 2 - The Final Whistle")], ["KickOff2FinalWhistle.lha"])
        self.assertEqual([i["archive"] for i in self.m.exact("Captain Fizz Meets the Blaster-Trons")], ["CaptainFizz.lha"])

    def test_an_add_on_disk_and_an_unknown_game_have_none(self) -> None:
        self.assertIsNone(self.match("Exile - Discovery Disk"))
        self.assertIsNone(self.match("Hole-In-One - Course Disk #3"))
        self.assertIsNone(self.match("Nonesuch"))


class InstallsThatAreNotTheGame(unittest.TestCase):
    """A demo's, a preview's, an Arcadia board's or another game's install is never a game's, by its name or by its Hall of Light page."""

    INSTALLS = [{"archive": "ZapPreview.lha", "title": "Zap (Preview)", "hol": "500"}, {"archive": "Xenon.lha", "title": "Xenon - Arcadia", "hol": "77"},
                {"archive": "BlobDemo.lha", "title": "Blob Demo", "hol": "88"}, {"archive": "HuntForRedOctober.lha", "title": "Hunt for Red October", "hol": "9"},
                {"archive": "Lotus2.lha", "title": "Lotus 2", "hol": "12"}, {"archive": "XmasLemmings91.lha", "title": "Lemmings - X-Mas 1991", "hol": "13"}]

    def test_by_name(self) -> None:
        from unittest import mock
        m = whdinstalls.Matcher(self.INSTALLS)
        with mock.patch.object(m, "_hols", return_value={}):
            for title in ("Zap", "Xenon", "Blob", "Hunt for Red October"):
                self.assertIsNone(m.match(title, strict=True), title)
            self.assertEqual(m.match("Xenon - Arcadia", strict=True)[0]["archive"], "Xenon.lha")
            self.assertEqual(m.match("Zap (Preview)", strict=True)[0]["archive"], "ZapPreview.lha")
            self.assertEqual(m.match("Lotus Turbo Challenge 2", strict=True)[0]["archive"], "Lotus2.lha")             # (_ALIASES)
            self.assertEqual(m.match("Lemmings - Xmas Lemmings 1991", strict=True)[0]["archive"], "XmasLemmings91.lha")

    def test_by_its_hall_of_light_page(self) -> None:
        from unittest import mock
        from romorg.alttitles import node
        m = whdinstalls.Matcher(self.INSTALLS)
        with mock.patch.object(m, "_hols", return_value={node("Zap"): {"500"}, node("Xenon"): {"77"}, node("Hunt for Red October"): {"9"}}):
            for title in ("Zap", "Xenon", "Hunt for Red October"):
                self.assertIsNone(m.match(title, strict=True), title)


class WordsInAnotherOrder(unittest.TestCase):
    """A nickname in quotes, a possessive, the same words in another order (TOSEC's article at the end of a title)."""

    def test_found(self) -> None:
        m = whdinstalls.Matcher([{"archive": a, "title": t} for a, t in (
            ("SuperOffroad.lha", "Ivan Stewart's Super Offroad"), ("ArcherMacleanPool.lha", "Archer Maclean Pool"),
            ("LemmingsOhNoMore.lha", "Lemmings - Oh! No More"), ("AventuraEspacial.lha", "La Aventura Espacial"),
            ("Mercenary2.lha", "Mercenary II: Damocles"), ("HeroQuest.lha", "HeroQuest"),
            ("QuestForGlory.lha", "Hero's Quest / Quest For Glory"))])
        got = lambda t: (m.match(t) or ({"archive": None}, ""))[0]["archive"]
        self.assertEqual(got("Ivan 'Ironman' Stewart's Super Off Road"), "SuperOffroad.lha")
        self.assertEqual(got("Archer Maclean's Pool"), "ArcherMacleanPool.lha")
        self.assertEqual(got("Oh No! More Lemmings"), "LemmingsOhNoMore.lha")
        self.assertEqual(got("Aventura Espacial, La"), "AventuraEspacial.lha")
        self.assertEqual(got("Damocles - Mercenary II"), "Mercenary2.lha")
        self.assertEqual(got("Hero's Quest - So You Want to Be a Hero"), "QuestForGlory.lha")   # (not the game HeroQuest)
        self.assertIsNone(got("Lemmings More Oh"))                                                 # (too few words: no)


class TitlesThatDiffer(unittest.TestCase):
    """TOSEC's title and the install's, the same game: an article, an umlaut, the install's long title, an edition's mark."""

    def setUp(self) -> None:
        self.m = whdinstalls.Matcher([{"archive": a, "title": t} for a, t in (
            ("BatmanTheMovie.lha", "Batman the Movie"), ("LotharMatthaus.lha", "Lothar Matthäus"),
            ("Populous2.lha", "Populous II: The Challenge Games"), ("HareRaisingHavoc.lha", "Roger Rabbit In Hare Raising Havoc"),
            ("SWOS.lha", "Sensible World of Soccer"), ("Gloom.lha", "Gloom"), ("ProjectXSE.lha", "Project X - Special Edition"),
            ("LeisureSuitLarry.lha", "Leisure Suit Larry"), ("OutToLunch.lha", "Out To Lunch"), ("FIFASoccer.lha", "FIFA Soccer"),
            ("Ultima4.lha", "Ultima IV: Quest for the Avatar"), ("ConflictEurope.lha", "Conflict: Europe"),
            ("FootballManagerWCE.lha", "Football Manager: World Cup Edition"), ("Arena.lha", "Arena"), ("SimCity.lha", "Sim City"),
            ("Starflight.lha", "Starflight"), ("DylanDog.lha", "Dylan Dog: The Murderers"))])
        self.others = {"sensible world of soccer", "gloom", "starflight", "leisure suit larry in the land of the lounge lizards",
                       "football manager", "arena", "sim city"}

    def match(self, title: str):
        r = self.m.match(title, strict=True, others=self.others)
        return r[0]["archive"] if r else None

    def test_the_same_game(self) -> None:
        for title, arc in (("Batman - The Movie", "BatmanTheMovie.lha"),
                           ("Lothar Matthaeus - Die Interaktive Fussballsimulation", "LotharMatthaus.lha"),
                           ("Populous II - Trials of the Olympian Gods", "Populous2.lha"), ("Hare Raising Havoc", "HareRaisingHavoc.lha"),
                           ("Sensible World of Soccer '95-'96", "SWOS.lha"),
                           ("Sensible World of Soccer '95-'96 - European Championship Edition", "SWOS.lha"),
                           ("Gloom Deluxe", "Gloom.lha"), ("Project-X - Special Edition 93", "ProjectXSE.lha"),
                           ("Leisure Suit Larry 1 - In the Land of the Lounge Lizards", "LeisureSuitLarry.lha"),
                           ("Pierre le Chef is... Out to Lunch", "OutToLunch.lha"), ("FIFA International Soccer", "FIFASoccer.lha"),
                           ("Ultima IV - Quest of the Avatar", "Ultima4.lha")):
            self.assertEqual(self.match(title), arc, title)

    def test_other_games(self) -> None:
        for title in ("Conflict - Korea", "Football Manager", "Arena 2000", "SimCity 2000", "Starflight II - Trade Routes of the Cloud Nebula",
                      "Dylan Dog 01 - La Regina delle Tenebre"):
            self.assertIsNone(self.match(title), title)


class Download(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.games = self.tmp / "Commodore Amiga - Games - [ADF]"
        self.games.mkdir()
        (self.games / "Turrican (1990)(Rainbow Arts)(Disk 1 of 2).adf").write_bytes(b"\0" * 1024)
        self.install = {"archive": "Turrican.lha", "title": "Turrican", "date": "2021-04-07"}
        script = b"(set #prefix @app-name)\n"
        self.archive = level0("TurricanInstall/Install", b"-lh0-", script, script) + level0("TurricanInstall/ReadMe", b"-lh0-", b"hi", b"hi") + b"\0"

    def test_it_unpacks_next_to_the_disks_with_its_marker(self) -> None:
        seen = []

        def opener(req):
            seen.append(req.full_url)
            return _Resp(self.archive)

        folder = whdinstalls.download_install(self.install, self.games, game="Turrican", opener=opener)
        self.assertEqual(seen, ["https://www.whdload.de/games/Turrican.lha"])
        self.assertEqual(folder.name, "Turrican (WHDLoad install)")
        self.assertEqual((folder / "TurricanInstall" / "ReadMe").read_bytes(), b"hi")
        found = whdinstalls.installed_in(self.games)
        self.assertEqual(list(found), ["Turrican.lha"])
        self.assertEqual(found["Turrican.lha"]["game"], "Turrican")
        # the scanner leaves the install alone: only the disk is a candidate
        files = scanner.collect_files(self.games.parent)
        self.assertEqual([f.name for f in files], ["Turrican (1990)(Rainbow Arts)(Disk 1 of 2).adf"])

    def test_the_files_tree_shows_the_install_as_such(self) -> None:
        from types import SimpleNamespace
        from romorg import treeview
        whdinstalls.download_install(self.install, self.games, opener=lambda req: _Resp(self.archive))
        entries = treeview.collect(self.tmp, SimpleNamespace(ops=[]))
        states = {e.parts[-1]: e.state for e in entries}
        self.assertEqual(states["ReadMe"], treeview.WHDLOAD)
        self.assertEqual(states["Turrican (1990)(Rainbow Arts)(Disk 1 of 2).adf"], treeview.IGNORED)    # (not in the plan: not looked at)

    def test_a_page_instead_of_the_archive_is_not_unpacked(self) -> None:
        with self.assertRaises(whdinstalls.WhdInstallError):
            whdinstalls.download_install(self.install, self.games, opener=lambda req: _Resp(b"<!DOCTYPE HTML><html>404</html>"))
        self.assertEqual(sorted(p.name for p in self.games.iterdir()), ["Turrican (1990)(Rainbow Arts)(Disk 1 of 2).adf"])

    def test_an_unsafe_path_in_the_archive_is_refused(self) -> None:
        evil = level0("../../evil", b"-lh0-", b"x", b"x") + b"\0"
        with self.assertRaises(whdinstalls.WhdInstallError):
            whdinstalls.download_install(self.install, self.games, opener=lambda req: _Resp(evil))
        self.assertFalse((self.tmp / "evil").exists())


class KeptForAWeek(unittest.TestCase):
    def test_fetched_when_missing_and_again_after_a_week(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        out = tmp / "installs.json"
        self.assertEqual(whdinstalls.check_update(path=out)["status"], "missing")
        page = "".join(ROW.format(arc=f"G{n}.lha", title=f"Game {n}", size=1, hol=n) for n in range(150))
        whdinstalls.download_and_build(out=out, opener=lambda req: _Resp(page.encode()))
        info = whdinstalls.db_info(out)
        self.assertEqual((info["available"], info["titles"]), (True, 150))
        self.assertEqual(whdinstalls.check_update(path=out)["status"], "up_to_date")
        self.assertEqual(whdinstalls.check_update(path=out, now=info["fetched_at"] + 8 * 86400)["status"], "update_available")


if __name__ == "__main__":
    unittest.main()


class AutomaticInstalls(unittest.TestCase):
    """The round the database updater runs (``App._sync_whd_installs``): every Amiga game you have that has an install gets it, an
    install whose date changed on whdload.de is downloaded again, the first network failure ends the round, a running job skips it."""

    def setUp(self) -> None:
        import sys
        import threading
        import time
        import urllib.request
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from uifixture import Fixture
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        folder = self.fx.data / "whdinstalls"
        folder.mkdir(parents=True, exist_ok=True)
        self.installs = [{"archive": a, "title": t, "size": 30000, "date": "2020-01-01", "hol": "", "lemon": ""}
                         for a, t in (("AlphaQuest.lha", "Alpha Quest"), ("BetaBlaster.lha", "Beta Blaster"))]
        self.write_list()
        req = urllib.request.Request(self.fx.url + "api/scan", method="POST", headers={"Content-Type": "application/json", "X-Romorg-Token": "t"},
                                     data=json.dumps({"path": str(self.fx.root), "platform": "Commodore Amiga"}).encode())
        urllib.request.urlopen(req).read()
        app = self.fx.server.app
        for _ in range(300):
            if app.jobs.current is None or app.jobs.current.status != "running":
                break
            time.sleep(0.05)
        self.app = app
        self.cancel = threading.Event()
        self.got: list[str] = []
        patch = mock.patch("time.sleep")              # (the pause between downloads)
        patch.start()
        self.addCleanup(patch.stop)

    def write_list(self) -> None:
        import time
        (self.fx.data / "whdinstalls" / "installs.json").write_text(json.dumps({"fetched": "2026-10-25", "fetched_at": time.time(), "installs": self.installs}))

    def fake_download(self, install, dest_dir, game="", cancel=None, opener=None):
        self.got.append(install["archive"])
        final = Path(dest_dir) / whdinstalls.folder_name(install)
        final.mkdir(parents=True, exist_ok=True)
        (final / whdinstalls.MARKER).write_text(json.dumps({"archive": install["archive"], "title": install["title"], "date": install["date"]}))
        return final

    def run_round(self) -> dict:
        with mock.patch.object(whdinstalls, "download_install", side_effect=self.fake_download):
            return self.app._sync_whd_installs(self.cancel, lambda d, t, m: None)

    def test_missing_installs_are_downloaded_then_left_alone(self) -> None:
        res = self.run_round()
        self.assertEqual((res["downloaded"], res["updated"], res["failed"]), (2, 0, []))
        self.assertEqual(sorted(self.got), ["AlphaQuest.lha", "BetaBlaster.lha"])
        self.assertTrue((self.fx.root / "Alpha Quest (WHDLoad install)" / whdinstalls.MARKER).is_file())
        self.got.clear()
        res = self.run_round()
        self.assertEqual((res["downloaded"], res["updated"], self.got), (0, 0, []))       # nothing to do: no download at all

    def test_an_install_whose_date_changed_is_downloaded_again(self) -> None:
        self.run_round()
        self.got.clear()
        self.installs[1]["date"] = "2026-10-01"
        self.write_list()
        res = self.run_round()
        self.assertEqual((res["downloaded"], res["updated"], self.got), (0, 1, ["BetaBlaster.lha"]))

    def test_a_network_failure_ends_the_round(self) -> None:
        import urllib.error

        def offline(*a, **k):
            self.got.append("try")
            raise urllib.error.URLError("no network")
        with mock.patch.object(whdinstalls, "download_install", side_effect=offline):
            res = self.app._sync_whd_installs(self.cancel, lambda d, t, m: None)
        self.assertEqual((self.got, len(res["failed"])), (["try"], 1))

    def test_installs_are_downloaded_several_at_a_time(self) -> None:
        import threading
        self.installs += [{"archive": f"G{n}.lha", "title": t, "size": 1, "date": "2020-01-01", "hol": "", "lemon": ""}
                          for n, t in enumerate(("Delta Force", "Epsilon", "Gamma Run", "Zeta Zone"))]
        self.write_list()
        live, most = [0], [0]
        lock = threading.Lock()

        def slow(install, dest_dir, game="", cancel=None, opener=None):
            with lock:
                live[0] += 1
                most[0] = max(most[0], live[0])
            threading.Event().wait(0.05)                                 # (long enough for the others to start)
            with lock:
                live[0] -= 1
            return self.fake_download(install, dest_dir, game)
        with mock.patch.object(whdinstalls, "download_install", side_effect=slow):
            res = self.app._sync_whd_installs(self.cancel, lambda d, t, m: None)
        self.assertGreaterEqual(res["downloaded"], 4)
        self.assertGreater(most[0], 1)                                   # several at once
        self.assertLessEqual(most[0], self.app.WHD_PARALLEL)             # never more than the limit

    def test_a_running_job_skips_it(self) -> None:
        busy = mock.Mock(status="running")
        with mock.patch.object(type(self.app.jobs), "current", new_callable=mock.PropertyMock, return_value=busy):
            self.assertEqual(self.run_round()["skipped"], "busy")
        self.assertEqual(self.got, [])
