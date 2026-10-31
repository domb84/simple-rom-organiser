"""Expansions of an Amiga game that its WHDLoad install takes in with the game (``romorg.whdexpansions``): the table, the forced answers of the
install (``amigainstall.Ask``), the plan (``library.merge_expansions``: one title), the disks the build gives the install and what it does
when that does not install (``whdbuild``)."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from romorg import amigainstall as ai, library, platforms, whdbuild, whdexpansions, whdinstalls, whdready
from romorg.datfile import Rom
from romorg.lha import LhaFile
from romorg.organiser import MatchedUnit, RenameOp
from tests.test_amigainstall import make_adf
from tests.test_library import AMIGA, GAMES, amiga_items

SLAVE = b"\x70\xff\x4e\x75WHDLOADS" + bytes(20)
ASK_BOOL = '(if (askbool (prompt "Do you want to install the Expansion Disk?") (help "h") (default 1)) ((askdisk (dest "ExpDisk")) (copyfiles (source "ExpDisk:exp") (dest "WORK:Zap/data"))))'
BASE = '(copyfiles (source "Zap.slave") (dest "WORK:Zap")) (askdisk (dest "ZapDisk")) (copyfiles (source "ZapDisk:game") (dest "WORK:Zap/data"))'

TABLE = {"merge": [{"archive": "Zap.lha", "base": ["Zap"], "save_disk": False,
                    "expansions": [{"titles": ["Zap - The Expansion"], "asks": [{"kind": "bool", "prompt": "Expansion Disk"}]},
                                   {"titles": ["Zap - Second Expansion"], "asks": [{"kind": "bool", "prompt": "Second"}]}]}],
         "standalone": [{"title": "Zap - Standalone Add-on", "archive": "ZapAddOn.lha", "level": 3}]}


@contextlib.contextmanager
def table(data: dict):
    """The expansions table ``data`` (instead of the shipped one)."""
    fns = (whdexpansions.merges, whdexpansions._by_base, whdexpansions._by_expansion, whdexpansions._standalone)
    with mock.patch.object(whdexpansions, "_table", return_value=data):
        for f in fns:
            f.cache_clear()
        try:
            yield
        finally:
            pass
    for f in fns:
        f.cache_clear()


class TableTests(unittest.TestCase):
    def test_the_shipped_table_is_made_by_the_tool_from_the_packages(self) -> None:
        spec = importlib.util.spec_from_file_location("gen_whdexpansions", Path(__file__).resolve().parent.parent / "tools" / "gen_whdexpansions.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        shipped = json.loads((Path(whdexpansions.__file__).with_name("whdexpansions.json")).read_text(encoding="utf-8"))
        self.assertEqual(shipped, {"merge": tool.SEED_MERGE, "standalone": tool.SEED_STANDALONE})

    def test_the_shipped_list_knows_which_kick_off_2_disks_boot(self) -> None:
        by = whdready.known()
        sps = "Kick Off 2 - Return to Europe v2.1e (1991)(Anco)[data disk][0169] - KickOff2_ReturnToEurope.ipf"
        self.assertEqual(by[sps][0], whdready.BOOTS)
        self.assertEqual(by["Kick Off 2 - Return to Europe v2.1e (1991)(Anco)[cr QTX].adf"][0], whdready.NO_BOOT)
        self.assertEqual(by["Kick Off 2 - The Final Whistle v1.9e (1991)(Anco)[cr CSL][data disk].adf"][0], whdready.NO_BOOT)

    def test_every_answer_is_a_pattern_and_a_title_belongs_to_one_install(self) -> None:
        seen: dict[str, str] = {}
        for m in whdexpansions.merges():
            for e in m.expansions:
                self.assertTrue(e.asks)
                for a in e.asks:
                    self.assertIn(a["kind"], ("bool", "options", "choice"))
                    for k in ("prompt", "choice"):
                        re.compile(a.get(k, ""))
                    if a["kind"] != "bool":
                        self.assertTrue(a.get("choice"))
                for t in e.titles:
                    self.assertNotIn(whdexpansions.norm(t), seen, t)
                    seen[whdexpansions.norm(t)] = m.archive
        self.assertNotIn("cadaver", seen)                     # (a base game is no expansion)
        self.assertGreaterEqual(len(whdexpansions.merges()), 10)
        self.assertIn(whdexpansions.norm("Cadaver - The Pay Off"), seen)

    def test_titles_are_found_as_the_library_writes_them(self) -> None:
        self.assertEqual(whdexpansions.merge_for_base("Cadaver")[0].archive, "Cadaver.lha")
        self.assertEqual(whdexpansions.merge_for_expansion("Battle Isle '93 - The Moon of Chromos")[0][0].archive, "BattleIsle.lha")
        self.assertEqual(whdexpansions.merge_for_expansion("Battle Isle '93 - Scenario Disk Volume 2 - The Moon of Chromos")[0][1].titles[0],
                         "Battle Isle '93 - The Moon of Chromos")
        self.assertEqual(whdexpansions.merge_for_expansion("Cadaver - The Last Supper"), [])          # (no expansion of the table)
        self.assertEqual(whdexpansions.standalone_row("Kick Off 2 - Return to Europe")["archive"], "KickOff2ReturnToEurope.lha")
        self.assertIsNone(whdexpansions.standalone_row("Kick Off 2"))

    def test_the_expansions_that_are_not_here_get_no(self) -> None:
        with table(TABLE):
            m = whdexpansions.merges()[0]
            asks = whdexpansions.asks_for(m, [m.expansions[1]])
            self.assertEqual([(a.expansion, a.want) for a in asks], [("Zap - The Expansion", False), ("Zap - Second Expansion", True)])

    def test_the_matcher_gives_an_expansion_with_an_install_of_its_own_that_install(self) -> None:
        installs = [{"archive": "KickOff2ReturnToEurope.lha", "title": "Kick Off 2: Return to Europe", "size": 1, "date": "", "hol": "", "lemon": ""},
                    {"archive": "KickOff2.lha", "title": "Kick Off 2", "size": 1, "date": "", "hol": "", "lemon": ""}]
        m = whdinstalls.Matcher(installs, AMIGA)
        self.assertEqual(m.match("Kick Off 2 - Return to Europe")[0]["archive"], "KickOff2ReturnToEurope.lha")
        self.assertEqual(m.match("Kick Off 2")[0]["archive"], "KickOff2.lha")


class ForcedAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = self.tmp / "ZapInstall"
        self.pkg.mkdir()
        (self.pkg / "Zap.slave").write_bytes(SLAVE)
        self.base = make_adf("ZapDisk", {"game": b"the game"})
        self.exp = make_adf("ExpDisk", {"exp": b"the expansion"})

    def script(self, text: str) -> None:
        (self.pkg / "Install").write_text(text)

    def test_a_forced_yes_installs_the_expansion_in_the_games_drawer(self) -> None:
        self.script(BASE + ASK_BOOL)
        res = ai.install(self.pkg, [self.base, self.exp], asks=(ai.Ask("bool", "Expansion Disk", expansion="Zap - The Expansion"),))
        self.assertEqual((res.files["Zap/data/game"], res.files["Zap/data/exp"]), (b"the game", b"the expansion"))
        self.assertEqual(res.forced, ["Zap - The Expansion"])
        self.assertEqual(sorted(k for k in res.files if k.endswith(".slave")), ["Zap/Zap.slave"])          # (one slave)

    def test_the_search_for_other_answers_never_undoes_a_forced_yes(self) -> None:
        self.script(BASE + ASK_BOOL.replace("(default 1)", "(default 0)"))
        asks = (ai.Ask("bool", "Expansion Disk", expansion="Zap - The Expansion"),)
        with self.assertRaisesRegex(ai.InstallError, "ExpDisk"):                           # (the expansion's disk is not there)
            ai.install(self.pkg, [self.base], asks=asks)
        res = ai.install(self.pkg, [self.base])                                          # (without the forced answer: the script's own, no)
        self.assertNotIn("Zap/data/exp", res.files)
        self.script(BASE + ASK_BOOL)                                                     # (a default of yes that fails: the search tries no)
        self.assertNotIn("Zap/data/exp", ai.install(self.pkg, [self.base]).files)
        with self.assertRaises(ai.InstallError):
            ai.install(self.pkg, [self.base], asks=asks)

    def test_an_expansion_that_is_not_here_is_answered_no(self) -> None:
        self.script(BASE + ASK_BOOL)
        res = ai.install(self.pkg, [self.base], asks=(ai.Ask("bool", "Expansion Disk", expansion="Zap - The Expansion", want=False),))
        self.assertEqual((res.forced, "Zap/data/exp" in res.files), ([], False))

    def test_a_question_of_yes_and_no_choices_takes_the_yes_wherever_it_is(self) -> None:
        for choices, flip in (('"Yes" "No"', 0), ('"No" "Yes"', 1)):
            self.script(BASE + '(if (= (askchoice (prompt "Do you want to install the Expansion Disk?") (choices %s) (default %d)) %d) '
                        '((askdisk (dest "ExpDisk")) (copyfiles (source "ExpDisk:exp") (dest "WORK:Zap/data"))))' % (choices, 1 - flip, flip))
            res = ai.install(self.pkg, [self.base, self.exp], asks=(ai.Ask("bool", "Expansion Disk", expansion="X"),))
            self.assertEqual(res.files["Zap/data/exp"], b"the expansion", choices)

    def test_the_options_an_expansion_needs_are_ticked_with_the_games_own_and_not_the_ones_that_say_only(self) -> None:
        self.script('(set #o (askoptions (prompt "Which data disk(s) do you like to install ?") (choices "Game disk" "The Expansion data disk" "Other data disk" "Third only") (default 1)))'
                    '(copyfiles (source "Zap.slave") (dest "WORK:Zap")) (set #m (+ (BITAND #o 1) (* 2 (BITAND #o 2))))'
                    '(if (BITAND #o 2) (askdisk (dest "ExpDisk")) ) (if (BITAND #o 2) (copyfiles (source "ExpDisk:exp") (dest "WORK:Zap/data")))'
                    '(textfile (dest "WORK:Zap/mask") (append (cat #o)))')
        asks = (ai.Ask("options", "data disk", "Expansion data", expansion="Zap - The Expansion"),
                ai.Ask("options", "data disk", "Other data", expansion="Zap - Other", want=False))
        res = ai.install(self.pkg, [self.exp], asks=asks)
        self.assertEqual(res.forced, ["Zap - The Expansion"])
        self.assertIn("Zap/data/exp", res.files)
        self.assertIn("options: Game disk, The Expansion data disk", " / ".join(res.log))      # (not "Other data disk", not "Third only")

    def test_a_version_the_install_names_after_the_expansion_is_the_only_one_tried(self) -> None:
        script = ('(set #multiple-versions "yes")\n(procedure P_SelectVersion (set #version (askchoice (prompt "Which version of Zap?") '
                  '(default 0) (choices "Standard (1 disk)" "Standard + Mission Disk Release (2 disks)"))))\n(P_SelectVersion)\n'
                  '(copyfiles (source "Zap.slave") (dest "WORK:Zap")) (askdisk (dest "ZapDisk")) (copyfiles (source "ZapDisk:game") (dest "WORK:Zap/data"))'
                  '(if (= #version 1) ((askdisk (dest "ExpDisk")) (copyfiles (source "ExpDisk:exp") (dest "WORK:Zap/data"))))')
        self.script(script)
        ai.write_lha(self.tmp / "p.lha", {f"ZapInstall/{p.name}": p.read_bytes() for p in self.pkg.iterdir()})
        asks = (ai.Ask("choice", "version", "Mission Disk Release", expansion="Zap - The Expansion"),)
        with ai.Package((self.tmp / "p.lha").read_bytes()) as pkg:
            self.assertEqual(pkg.tries(2, asks=asks), [1])                                # (even for one disk: the version that takes the expansion)
            self.assertEqual(pkg.tries(1), [0, None, 1][:0] or pkg.tries(1))              # (no forced answer: as before)
            res = pkg.install([self.base, self.exp], asks=asks)
            self.assertEqual((res.files["Zap/data/exp"], res.forced), (b"the expansion", ["Zap - The Expansion"]))

    def test_a_choice_names_the_slave_to_install(self) -> None:
        self.script('(set #w (askchoice (prompt "What would you like to install?") (choices "Zap" "Zap Extended Levels") (default 0)))'
                    '(select #w (copyfiles (source "Zap.slave") (dest "WORK:Zap")) (copyfiles (source "Zap.slave") (dest "WORK:Zap") (newname "ZapExt.Slave")))')
        res = ai.install(self.pkg, [], asks=(ai.Ask("choice", "install", "Extended Levels"),))
        self.assertEqual(sorted(res.files), ["Zap/ZapExt.Slave", "load"] if "load" in res.files else ["Zap/ZapExt.Slave"])

    def test_an_install_that_mounts_a_volume_is_unsupported_not_failed(self) -> None:
        self.script('(copyfiles (source "Zap.slave") (dest "WORK:Zap")) (run "mount WHDI: FROM T:WHDI.mountlist >CON:///1000//CLOSE/AUTO")')
        with self.assertRaises(ai.Unsupported) as cm:
            ai.install(self.pkg, [])
        self.assertIn("mounts a volume", str(cm.exception))
        self.assertTrue(issubclass(ai.Unsupported, ai.InstallError))
        ai.write_lha(self.tmp / "p.lha", {f"ZapInstall/{p.name}": p.read_bytes() for p in self.pkg.iterdir()})
        with ai.Package((self.tmp / "p.lha").read_bytes()) as pkg, self.assertRaises(ai.Unsupported):
            pkg.install([])


def make_plan(root: Path, names: list[str], discs: dict[str, bytes], profile=None) -> SimpleNamespace:
    """A library plan (what ``whdbuild.targets`` reads) of the disks ``names``: the files are written under ``root``, ``library.select`` decides."""
    items = amiga_items(names)
    owners = {}
    for it in items:
        p = root / (it.rom.name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(discs.get(it.rom.game, b"x"))
        it.path = p
        op = RenameOp(p, p, "ok")
        owners[it.key] = (op, MatchedUnit(p, op, it.rom, frozenset(), "", GAMES, True, False, False))
    sel = library.select(items, profile or library.default_profile(AMIGA), AMIGA)
    return SimpleNamespace(selection=sel, owners=owners, items=items)


BASE1, BASE2 = "Zap (1990)(Acme)(Disk 1 of 2)[cr FLT]", "Zap (1990)(Acme)(Disk 2 of 2)"
EXP = "Zap - The Expansion (1991)(Acme)[data disk]"
EXP2 = "Zap - Second Expansion (1992)(Acme)[data disk]"
LISTED = {f"{BASE1}.adf\n{BASE2}.adf": [3, "Zap.lha"]}


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(whdready, "known", return_value=dict(LISTED)), mock.patch.object(whdready, "checked", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        t = table(TABLE)
        t.__enter__()
        self.addCleanup(t.__exit__, None, None, None)

    def plan(self, names: list[str], profile=None) -> SimpleNamespace:
        return make_plan(self.tmp, names, {}, profile)

    def test_an_expansion_of_a_kept_game_with_the_install_is_one_title_with_it(self) -> None:
        plan = self.plan([BASE1, BASE2, EXP])
        sel, items = plan.selection, plan.items
        self.assertEqual([d.action for d in sel.decisions.values()], [library.KEEP] * 3)         # (nothing is dropped)
        keys = {library.counted_key(it, sel) for it in items}
        self.assertEqual(len(keys), 1)                                                         # (one game: the tile, the list, the counts)
        self.assertEqual(len({library.game_key(it) for it in items}), 2)                        # (two titles in the DAT)
        self.assertEqual(sel.decisions[3].merged_into, "Zap (1990)(Acme)(Disk 1 of 2)[cr FLT]")
        self.assertEqual(sel.decisions[1].includes, ("The Expansion",))                         # ("with The Expansion")
        self.assertEqual(sel.decisions[2].includes, ("The Expansion",))
        self.assertEqual(sel.decisions[3].includes, ())

    def test_every_expansion_the_install_takes_is_with_the_game(self) -> None:
        plan = self.plan([BASE1, BASE2, EXP, EXP2])
        self.assertEqual(plan.selection.decisions[1].includes, ("Second Expansion", "The Expansion"))
        self.assertEqual(len({library.counted_key(it, plan.selection) for it in plan.items}), 1)

    def test_the_games_folder_holds_the_expansion_s_files(self) -> None:
        from romorg import organiser
        plan = self.plan([BASE1, BASE2, EXP])
        folders: dict = {}
        games: dict[tuple, list[int]] = {}
        for key in plan.owners:
            games.setdefault(library.counted_key(organiser._item_of(key, plan.owners), plan.selection), []).append(key)
        folders = organiser._game_folder_names(games, plan.owners)
        self.assertEqual(list(folders.values()), ["Zap"])                                       # (named by the game, not "Zap - The Expansion")

    def test_without_the_game_the_expansion_is_a_game_of_its_own(self) -> None:
        plan = self.plan([EXP])
        self.assertEqual((plan.selection.merged, plan.selection.decisions[1].merged_into), ({}, ""))
        plan = self.plan([BASE1, EXP])                                                          # (the game's set is not complete: not kept)
        self.assertEqual(plan.selection.merged, {})
        self.assertEqual(plan.selection.decisions[2].action, library.KEEP)                      # (as before: a kept game of its own)

    def test_a_game_with_no_install_from_these_disks_takes_no_expansion(self) -> None:
        with mock.patch.object(whdready, "known", return_value={}):
            self.assertEqual(self.plan([BASE1, BASE2, EXP]).selection.merged, {})
        with mock.patch.object(whdready, "known", return_value={f"{BASE1}.adf\n{BASE2}.adf": [-1, "Zap.lha"]}):
            self.assertEqual(self.plan([BASE1, BASE2, EXP]).selection.merged, {})                # (installs but never boots: not a build)
        with mock.patch.object(whdready, "known", return_value={f"{BASE1}.adf\n{BASE2}.adf": [3, "Other.lha"]}):
            self.assertEqual(self.plan([BASE1, BASE2, EXP]).selection.merged, {})                # (another game's install)

    def test_an_assumed_install_takes_the_expansion_though_the_list_has_no_result(self) -> None:
        assumed = {"merge": [dict(TABLE["merge"][0], assume=True)], "standalone": []}
        with mock.patch.object(whdready, "known", return_value={}), table(assumed):
            self.assertEqual(len(self.plan([BASE1, BASE2, EXP]).selection.merged), 1)
        with mock.patch.object(whdready, "known", return_value={f"{BASE1}.adf\n{BASE2}.adf": [0, ""]}), table(assumed):
            self.assertEqual(len(self.plan([BASE1, BASE2, EXP]).selection.merged), 1)            # (a "0, none" result of the list: no install of another game)

    def test_an_expansion_that_is_not_complete_stays_as_it_is(self) -> None:
        plan = self.plan([BASE1, BASE2, "Zap - The Expansion (1991)(Acme)(Disk 1 of 2)[data disk]"])      # (disk 2 of the expansion is missing)
        self.assertEqual(plan.selection.merged, {})
        self.assertNotEqual(plan.selection.decisions[3].action, library.KEEP)

    def test_the_rule_off_or_another_system_is_as_before(self) -> None:
        self.assertEqual(self.plan([BASE1, BASE2, EXP], replace(library.default_profile(AMIGA), build_whdload=False)).selection.merged, {})
        n64 = platforms.get_platform("Nintendo 64")
        items = amiga_items([BASE1, BASE2, EXP])
        self.assertEqual(library.select(items, library.default_profile(n64), n64).merged, {})

    def test_the_target_counts_it_once_too(self) -> None:
        from romorg import totals
        items = amiga_items([BASE1, BASE2, EXP])
        sel = library.select(items, library.default_profile(AMIGA), AMIGA, report=False)
        self.assertEqual(len(totals._kept(items, sel)), 1)
        self.assertEqual(len(totals._names(items, totals._kept(items, sel), library.default_profile(AMIGA), sel)), 1)
        self.assertTrue(next(iter(totals._names(items, totals._kept(items, sel), library.default_profile(AMIGA), sel).values())).startswith("Zap ("))


def adf_for(game: str, files: dict[str, bytes], label: str) -> bytes:
    return make_adf(label, files)


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "amiga"
        self.root.mkdir()
        for p in (mock.patch.object(whdready, "known", return_value=dict(LISTED)), mock.patch.object(whdready, "checked", return_value=None),
                  mock.patch.object(whdready, "record")):
            self.mocks = p.start()
            self.addCleanup(p.stop)
        t = table(TABLE)
        t.__enter__()
        self.addCleanup(t.__exit__, None, None, None)
        self.discs = {BASE1: make_adf("ZapDisk", {"game": b"disk one"}), BASE2: make_adf("ZapDisk2", {"game2": b"disk two"}),
                      EXP: make_adf("ExpDisk", {"exp": b"the expansion"}), EXP2: make_adf("Exp2Disk", {"exp2": b"the second"})}
        self.script = (BASE + ASK_BOOL)

    def package(self, script: str, extra: dict[str, bytes] | None = None) -> bytes:
        files = {"ZapInstall/Zap.slave": SLAVE, "ZapInstall/Install": script.encode()}
        files.update(extra or {})
        ai.write_lha(self.tmp / "pkg.lha", files)
        return (self.tmp / "pkg.lha").read_bytes()

    def build(self, todo, pkg: bytes) -> dict:
        return whdbuild.build(todo, self.root, [{"archive": "Zap.lha", "title": "Zap", "date": "2024"}, {"archive": "ZapAddOn.lha", "title": "Z", "date": ""}],
                              fetch=lambda inst: pkg, decruncher=lambda: None)

    def plan(self, names: list[str]) -> SimpleNamespace:
        return make_plan(self.root, names, self.discs)

    def test_the_disks_go_to_the_install_the_game_s_first_and_the_expansion_s_after_them(self) -> None:
        todo = whdbuild.targets(self.plan([BASE1, BASE2, EXP, EXP2]), AMIGA)
        [t] = todo
        self.assertEqual([x.expansion.titles[0] for x in t.extra], ["Zap - The Expansion", "Zap - Second Expansion"])
        self.assertEqual(whdbuild._disk_data(t, t.extra), [self.discs[BASE1], self.discs[BASE2], self.discs[EXP], self.discs[EXP2]])
        self.assertEqual(whdbuild._disk_data(t), [self.discs[BASE1], self.discs[BASE2]])
        self.assertEqual(t.key()["source"], [f"{BASE1}.adf", f"{BASE2}.adf", f"+ {EXP}.adf", f"+ {EXP2}.adf"])   # (a changed set is made again)

    def test_a_blank_save_disk_goes_between_the_game_and_its_expansion(self) -> None:
        save = {"merge": [dict(TABLE["merge"][0], save_disk=True)], "standalone": []}
        with table(save):
            [t] = whdbuild.targets(self.plan([BASE1, BASE2, EXP]), AMIGA)
            self.assertEqual(whdbuild._disk_data(t, t.extra), [self.discs[BASE1], self.discs[BASE2], bytes(901120), self.discs[EXP]])
            self.assertEqual(len(whdbuild._disk_data(t)), 2)                                     # (none for the game alone)

    def test_one_archive_holds_the_game_and_the_expansion_and_says_so(self) -> None:
        plan = self.plan([BASE1, BASE2, EXP, EXP2])
        todo = whdbuild.targets(plan, AMIGA)
        pkg = self.package(BASE + '(copyfiles (source "ZapDisk2:game2") (dest "WORK:Zap/data"))' + ASK_BOOL
                           + ASK_BOOL.replace("Expansion Disk", "Second Expansion").replace('"ExpDisk"', '"Exp2Disk"').replace("ExpDisk:exp", "Exp2Disk:exp2"))
        out = self.build(todo, pkg)
        self.assertEqual((len(out["built"]), out["failed"], out["skipped"]), (1, [], []))
        self.assertEqual(out["expansions"], [{"title": "Zap", "installed": ["Zap - The Expansion", "Zap - Second Expansion"], "left_out": []}])
        arc = LhaFile(todo[0].dest.read_bytes())
        self.assertEqual({e.name for e in arc.entries if not e.is_dir} - {whdbuild.MARKER},
                         {"Zap/Zap.slave", "Zap/data/game", "Zap/data/game2", "Zap/data/exp", "Zap/data/exp2", "load"})      # (one drawer, one slave)
        marker = whdbuild.marker_of(todo[0].dest)
        self.assertEqual(marker["expansions"], ["Zap - The Expansion", "Zap - Second Expansion"])
        self.assertEqual(self.build(todo, pkg)["kept"], 1)                                      # (the same again: left as it is)
        again = whdbuild.targets(self.plan([BASE1, BASE2, EXP]), AMIGA)                            # (an expansion less: made again, one archive)
        self.assertEqual(self.build(again, pkg)["built"], [str(again[0].dest)])

    def test_the_expansion_is_no_game_to_build_of_its_own(self) -> None:
        with mock.patch.object(whdready, "known", return_value={**LISTED, f"{EXP}.adf": [3, "Zap.lha"]}):
            self.assertEqual([t.title for t in whdbuild.targets(self.plan([BASE1, BASE2, EXP]), AMIGA)], ["Zap"])

    def test_when_the_expansion_does_not_install_the_game_is_built_alone_and_the_result_says_why(self) -> None:
        todo = whdbuild.targets(self.plan([BASE1, BASE2, EXP]), AMIGA)
        pkg = self.package(BASE + '(copyfiles (source "ZapDisk2:game2") (dest "WORK:Zap/data"))'
                           '(if (askbool (prompt "Do you want to install the Expansion Disk?") (help "h") (default 0)) (abort "the expansion disk is broken"))')
        out = self.build(todo, pkg)
        self.assertEqual((len(out["built"]), out["failed"]), (1, []))
        [x] = out["expansions"]
        self.assertEqual(x["installed"], [])
        self.assertEqual(x["left_out"][0]["title"], "Zap - The Expansion")
        self.assertIn("expansion disk is broken", x["left_out"][0]["reason"])
        self.assertNotIn("Zap/data/exp", {e.name for e in LhaFile(todo[0].dest.read_bytes()).entries})
        self.assertEqual(whdbuild.marker_of(todo[0].dest)["expansions"], [])

    def test_an_install_that_never_asks_for_the_expansion_leaves_it_out(self) -> None:
        todo = whdbuild.targets(self.plan([BASE1, BASE2, EXP]), AMIGA)
        out = self.build(todo, self.package(BASE + '(copyfiles (source "ZapDisk2:game2") (dest "WORK:Zap/data"))'))
        self.assertEqual(out["expansions"][0]["left_out"], [{"title": "Zap - The Expansion", "reason": "the install did not ask for it"}])

    def test_an_install_this_app_cannot_run_is_skipped_with_its_reason_and_is_no_failure(self) -> None:
        todo = whdbuild.targets(self.plan([BASE1, BASE2]), AMIGA)
        pkg = self.package('(copyfiles (source "Zap.slave") (dest "WORK:Zap")) (run "mount WHDI: FROM T:WHDI.mountlist")')
        out = self.build(todo, pkg)
        self.assertEqual((out["built"], out["failed"]), ([], []))
        self.assertEqual(out["skipped"][0]["title"], "Zap")
        self.assertIn("mounts a volume", out["skipped"][0]["reason"])
        self.mocks.assert_not_called()                                                          # (nothing recorded: the disks are fine)
        self.assertFalse(todo[0].dest.exists())

    def test_the_versions_the_rules_set_aside_are_the_fallback_original_before_crack(self) -> None:
        crack = "Zap (1990)(Acme)(Disk 1 of 2)[cr QTX]"
        names = [BASE1, BASE2, crack, "Zap (1990)(Acme)(Disk 2 of 2)[cr QTX]"]
        with mock.patch.object(whdready, "known", return_value={f"{BASE1}.adf\n{BASE2}.adf": [3, "Zap.lha"]}):
            plan = self.plan([BASE1, BASE2, EXP, "Zap (1989)(Acme)(Disk 1 of 2)", "Zap (1989)(Acme)(Disk 2 of 2)"])
            [t] = whdbuild.targets(plan, AMIGA)
        self.assertEqual([[n for n in a.names][0] for a in t.alternates], ["Zap (1989)(Acme)(Disk 1 of 2).adf"])
        order = [c.names[0] for c in whdbuild._in_order(t)]
        self.assertEqual(order[0], "Zap (1989)(Acme)(Disk 1 of 2).adf")                         # (the original first: the kept one is a crack)
        self.assertEqual(len(order), 2)
        del names

    def test_a_version_that_fails_to_install_makes_the_next_one_the_game(self) -> None:
        self.discs["Zap (1989)(Acme)(Disk 1 of 2)"] = make_adf("ZapDisk", {"game": b"the other version"})
        self.discs["Zap (1989)(Acme)(Disk 2 of 2)"] = make_adf("ZapDisk2", {"game2": b"disk two"})
        with mock.patch.object(whdready, "known", return_value=dict(LISTED)):
            plan = self.plan([BASE1, BASE2, EXP, "Zap (1989)(Acme)(Disk 1 of 2)", "Zap (1989)(Acme)(Disk 2 of 2)"])
            [t] = whdbuild.targets(plan, AMIGA)
        pkg = self.package(BASE + '(copyfiles (source "ZapDisk2:game2") (dest "WORK:Zap/data"))' + ASK_BOOL)
        with mock.patch.object(whdbuild, "_in_order", return_value=[t.alternates[0], t]):
            res, used, left = whdbuild._install(t, ai.Package(pkg), lambda: None, [])
        self.assertIs(used, t.alternates[0])
        self.assertEqual(left, [])

    def test_the_kept_version_is_tried_first_and_the_build_records_which_one_it_was_made_from(self) -> None:
        [t] = whdbuild.targets(self.plan([BASE1, BASE2]), AMIGA)
        alt = whdbuild.Target(title="Zap", names=["Zap (1989)(Acme)(Disk 1 of 2)[cr QTX].adf"], archive="Zap.lha", level=3, dest=t.dest,
                              disks=[(self.root / "nothing.adf", None)])
        t.alternates = [alt]
        pkg = self.package(BASE + '(copyfiles (source "ZapDisk2:game2") (dest "WORK:Zap/data"))')
        self.assertEqual(whdbuild._in_order(t)[0], t)                                           # (the kept one is not a crack ahead of a crack)
        self.assertEqual(self.build([t], pkg)["failed"], [])
        self.assertNotIn("built_from", whdbuild.marker_of(t.dest))                              # (made from the kept version)


    def test_an_expansion_with_an_install_of_its_own_is_built_from_its_disks_alone(self) -> None:
        name = "Zap - Standalone Add-on (1991)(Acme)[data disk]"
        self.discs[name] = make_adf("AddOn", {"add": b"x"})
        plan = self.plan([name])
        with mock.patch.object(whdready, "known", return_value={}):
            [t] = whdbuild.targets(plan, AMIGA)
            self.assertEqual((t.archive, t.level, t.extra), ("ZapAddOn.lha", 3, []))
        with mock.patch.object(whdready, "known", return_value={f"{name}.adf": [-1, "Other.lha"]}):     # (the list has another game's install, no boot: ours)
            [t] = whdbuild.targets(plan, AMIGA)
            self.assertEqual((t.archive, t.level), ("ZapAddOn.lha", 3))
        unproven = {"merge": [], "standalone": [dict(TABLE["standalone"][0], level=1)]}
        with table(unproven), mock.patch.object(whdready, "known", return_value={f"{name}.adf": [-1, "ZapAddOn.lha"]}):
            self.assertEqual(whdbuild.targets(plan, AMIGA), [])                                  # (the list's "does not boot" stands for an unproven row)
        with table(unproven), mock.patch.object(whdready, "known", return_value={}):
            self.assertEqual(len(whdbuild.targets(plan, AMIGA)), 1)

    def test_kick_off_2_data_disks_are_built_only_from_a_version_the_list_boots(self) -> None:
        # (the slaves check the boot block against a few CRCs: a crack fails with "data/disk-files are damaged or unsupported version" -
        #  the shipped table's rows must not claim a boot the list says it does not have)
        shipped = json.loads(Path(whdexpansions.__file__).with_name("whdexpansions.json").read_text(encoding="utf-8"))
        for title, arc in (("Kick Off 2 - Return to Europe", "KickOff2ReturnToEurope.lha"),
                           ("Kick Off 2 - The Final Whistle", "KickOff2FinalWhistle.lha")):
            name = f"{title} v2.1e (1991)(Anco)[cr QTX][data disk]"
            self.discs[name] = make_adf("KO2", {"x": b"x"})
            plan = self.plan([name])
            with table(shipped):
                self.assertEqual((whdexpansions.standalone_row(title)["archive"], whdexpansions.standalone_row(title)["level"]), (arc, 0))
            with table(shipped), mock.patch.object(whdready, "known", return_value={f"{name}.adf": [-1, arc]}):
                self.assertEqual(whdbuild.targets(plan, AMIGA), [])                  # (a crack the slave refuses: no archive, a game of its own)
            with table(shipped), mock.patch.object(whdready, "known", return_value={}):
                self.assertEqual(whdbuild.targets(plan, AMIGA), [])                  # (nothing known of it: not claimed either)
            with table(shipped), mock.patch.object(whdready, "known", return_value={f"{name}.adf": [3, arc]}):
                [t] = whdbuild.targets(plan, AMIGA)                                   # (a version seen to boot: built)
                self.assertEqual((t.archive, t.level), (arc, 3))

    def test_the_disk_that_holds_the_file_the_install_needs_comes_first(self) -> None:
        good = make_adf("Ext", {"C/BEXT": b"x"})
        t = whdbuild.Target(title="Z", names=["a"], archive="Zap.lha", level=3, dest=self.root / "z.lha", prefer_disk="C/BEXT",
                            disks=[(self.root / "a.adf", None)])
        (self.root / "a.adf").write_bytes(make_adf("Ext", {"other": b"x"}))
        (self.root / "b.adf").write_bytes(good)
        t.alternates = [whdbuild.Target(title="Z", names=["b"], archive="Zap.lha", level=3, dest=t.dest, disks=[(self.root / "b.adf", None)])]
        self.assertEqual([c.names for c in whdbuild._in_order(t)], [["b"], ["a"]])


class HeroQuestShapeTests(unittest.TestCase):
    def test_the_version_the_slave_is_known_to_boot_comes_before_the_kept_one(self) -> None:
        merge = whdexpansions.merge_for_base("HeroQuest")[0]
        self.assertTrue(merge.save_disk)
        kept = whdbuild.Target(title="HeroQuest", names=["HeroQuest (1991)(Gremlin)(PAL)(M5)[cr AGL][m baddump].adf"], archive="HeroQuest.lha",
                               level=3, dest=Path("/x/h.lha"), merge=merge)
        kept.alternates = [whdbuild.Target(title="HeroQuest", names=["HeroQuest (1991)(Gremlin)(PAL)(M5)[cr CABAL].adf"], archive="HeroQuest.lha",
                                           level=3, dest=Path("/x/h.lha"), merge=merge)]
        self.assertTrue(whdbuild._in_order(kept)[0].names[0].endswith("[cr CABAL].adf"))


class HoleInOneTests(unittest.TestCase):
    """Hole-In-One's install (the Files version, no version question) asks for data #1 (Menagerie) then data #2 (Holidays), each disk found by
    a file on it: Course Disk #2 and Course Disk #3 are its expansions; Mean 18's course disks are another game."""

    def test_the_game_under_both_names_takes_both_course_disks(self) -> None:
        for base in ("Hole-In-One", "Hole-In-One - Miniature Golf"):
            m = whdexpansions.merge_for_base(base)
            self.assertEqual([x.archive for x in m], ["HoleInOne.lha"], base)
        for t in ("Hole-In-One - Course Disk #2 - Intermediate & Menagerie", "Hole-In-One - Course Disk - Intermediate & Menagerie"):
            (m, e), = whdexpansions.merge_for_expansion(t)
            self.assertEqual((m.archive, e.asks[0]["prompt"]), ("HoleInOne.lha", "Menagerie"))
        (m, e), = whdexpansions.merge_for_expansion("Hole-In-One - Course Disk #3 - Alphabet Soup & Out of this World & Holidays")
        self.assertEqual(e.asks[0]["prompt"], "Holidays")
        for t in ("Mean 18 - Ultimate Golf - Famous Courses I", "Mean 18 - Ultimate Golf - Misc Courses 3", "Mean 18 - Ultimate Golf"):
            self.assertEqual(whdexpansions.merge_for_expansion(t), [], t)
            self.assertEqual(whdexpansions.merge_for_base(t), [], t)

    def test_the_package_asks_data_1_before_data_2_and_each_answer_matches_a_question(self) -> None:
        spec = importlib.util.spec_from_file_location("gen_whdexpansions", Path(__file__).resolve().parent.parent / "tools" / "gen_whdexpansions.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        qs = [p for _c, p, _x in tool.package_questions("HoleInOne.lha")]
        self.assertLess(next(i for i, p in enumerate(qs) if "Menagerie" in p), next(i for i, p in enumerate(qs) if "Holidays" in p))
        for e in whdexpansions.merge_for_base("Hole-In-One")[0].expansions:
            for a in e.asks:
                self.assertTrue(tool.matches(a, tool.package_questions("HoleInOne.lha")), a)

    def test_the_real_install_takes_the_disks_in_order_and_names_the_courses_it_got(self) -> None:
        merge = whdexpansions.merge_for_base("Hole-In-One")[0]
        base = make_adf("Hole-In-One", {"putt": b"game", "s/startup": b"x", "course": b"cb"})          # (the game's own disk has "course" too)
        d1 = make_adf("Disk #2", {"Intermediate/H.00": b"i0", "Menagerie/H.00": b"m0", "course": b"c2", "Selection": b"s2",
                                  "Title.Music": b"t2", "Putt2": b"p2", "DigiTek2": b"d2"})
        d2 = make_adf("Disk #3", {"Holidays/H.00": b"h0", "AlphabetSoup/H.00": b"a0", "course": b"c3", "Selection": b"s3", "Title.Music": b"t3"})
        with ai.Package(whdinstalls.package({"archive": merge.archive})[0]) as pkg:
            both = pkg.install([base, d1, d2], cd=False, decruncher=lambda: None, asks=whdexpansions.asks_for(merge, merge.expansions))
            only1 = pkg.install([base, d1], cd=False, decruncher=lambda: None, asks=whdexpansions.asks_for(merge, merge.expansions[:1]))
            alone = pkg.install([base], cd=False, decruncher=lambda: None, asks=whdexpansions.asks_for(merge, ()))
        f = {k.split("/", 1)[1]: v for k, v in both.files.items() if "/" in k}
        self.assertEqual((f["data/course2"], f["data/course3"], f["data/Menagerie/H.00"], f["data/Holidays/H.00"]), (b"c2", b"c3", b"m0", b"h0"))
        self.assertEqual(f["data/course"], b"cb")                                       # (course2 / course3 are the course disks' own)
        self.assertEqual(sorted(both.forced), sorted(e.titles[0] for e in merge.expansions))
        g = {k.split("/", 1)[1]: v for k, v in only1.files.items() if "/" in k}
        self.assertEqual((g["data/course2"], "data/course3" in g, "data/Holidays/H.00" in g), (b"c2", False, False))
        self.assertEqual(only1.forced, [merge.expansions[0].titles[0]])
        self.assertFalse(any(k.endswith(("course2", "course3")) for k in alone.files))
        self.assertEqual(alone.forced, [])


if __name__ == "__main__":
    unittest.main()


SPS_DAT = "Commodore Amiga - Games - SPS"
SPS_BASE = "Zap (1990)(Acme)[0042]"
SPS_EXP = "Zap - The Expansion (1991)(Acme)[data disk][0043]"
SPS_EXP2 = "Zap - Second Expansion (1992)(Acme)[data disk][0044]"


class SpsReleaseTests(unittest.TestCase):
    """A game or an expansion kept as an SPS release (its images in a zip, as the library keeps them) merges and builds like a floppy set:
    its images are the install's disks, in the release's own order, whether the other side of the pair is an SPS release or an ADF set."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        listed = {f"{BASE1}.adf\n{BASE2}.adf": [3, "Zap.lha"]}
        rel = lambda name: [3, "Zap.lha"] if name == SPS_BASE else None          # noqa: E731
        for p in (mock.patch.object(whdready, "known", return_value=listed), mock.patch.object(whdready, "checked", return_value=None),
                  mock.patch.object(whdready, "release", side_effect=rel)):
            p.start()
            self.addCleanup(p.stop)
        t = table(TABLE)
        t.__enter__()
        self.addCleanup(t.__exit__, None, None, None)

    def sps(self, rel: str, images: list[str], key: int) -> tuple[list, dict]:
        """One SPS release as one zip of ``images`` (one unit a disk, as the scan reads a zip)."""
        z = self.tmp / f"{rel}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for n in reversed(images):                           # (written backwards: the order is the names')
                zf.writestr(n, rel.encode() + b"|" + n.encode())
        items, owners = [], {}
        for i, n in enumerate(images):
            rom = Rom(name=f"{rel} - {n}", size=1, crc="", md5="", sha1="", game=rel, dat=SPS_DAT)
            it = library.Item(key=key + i, dat=SPS_DAT, rom=rom, style="tosec", path=z, member=n)
            op = RenameOp(z, z, "ok")
            items.append(it)
            owners[it.key] = (op, MatchedUnit(z, op, rom, frozenset(), "", SPS_DAT, True, True, False, n))
        return items, owners

    def plan(self, base: list | None, exps: list, adf_base: bool = False) -> SimpleNamespace:
        items, owners = [], {}
        if adf_base:
            p = make_plan(self.tmp, [BASE1, BASE2], {})
            items, owners = list(p.items), dict(p.owners)
        elif base:
            items, owners = self.sps(SPS_BASE, base, 1)
        for n, (rel, images) in enumerate(exps):
            i, o = self.sps(rel, images, 20 + 10 * n)
            items += i
            owners.update(o)
        sel = library.select(items, library.default_profile(AMIGA), AMIGA)
        return SimpleNamespace(selection=sel, owners=owners, items=items)

    def test_an_sps_expansion_of_an_sps_game_is_one_title_with_it(self) -> None:
        plan = self.plan(["Zap_Disk1.ipf"], [(SPS_EXP, ["ZapExp.ipf"])])
        self.assertEqual([d.action for d in plan.selection.decisions.values()], [library.KEEP] * 2)      # (nothing is dropped)
        self.assertEqual(len({library.counted_key(it, plan.selection) for it in plan.items}), 1)
        self.assertEqual(plan.selection.decisions[20].merged_into, SPS_BASE)
        self.assertEqual(plan.selection.decisions[1].includes, ("The Expansion",))

    def test_the_install_is_given_the_games_images_then_the_expansions_in_order(self) -> None:
        plan = self.plan(["Zap_Disk2.ipf", "Zap_Disk1.ipf"], [(SPS_EXP, ["ZapExp_B.ipf", "ZapExp_A.ipf"])])
        [t] = whdbuild.targets(plan, AMIGA)
        self.assertEqual((t.sps, [x.sps for x in t.extra], [x.expansion.titles[0] for x in t.extra]), (True, [True], ["Zap - The Expansion"]))
        got = whdbuild._disk_data(t, t.extra)
        self.assertEqual([d.split(b"|")[1] for d in got], [b"Zap_Disk1.ipf", b"Zap_Disk2.ipf", b"ZapExp_A.ipf", b"ZapExp_B.ipf"])
        self.assertEqual(len(whdbuild._disk_data(t)), 2)                                           # (the game alone: its own two)
        self.assertEqual(t.key()["source"], [SPS_BASE, f"+ {SPS_EXP}"])                             # (a changed set is made again)

    def test_both_expansions_of_an_sps_game_follow_the_installs_order(self) -> None:
        plan = self.plan(["Zap_Disk1.ipf"], [(SPS_EXP2, ["Two.ipf"]), (SPS_EXP, ["One.ipf"])])
        [t] = whdbuild.targets(plan, AMIGA)
        self.assertEqual([x.expansion.titles[0] for x in t.extra], ["Zap - The Expansion", "Zap - Second Expansion"])
        self.assertEqual([d.split(b"|")[1] for d in whdbuild._disk_data(t, t.extra)], [b"Zap_Disk1.ipf", b"One.ipf", b"Two.ipf"])

    def test_an_sps_expansion_of_a_floppy_set_game_is_read_as_images(self) -> None:
        plan = self.plan(None, [(SPS_EXP, ["ZapExp.ipf"])], adf_base=True)
        [t] = whdbuild.targets(plan, AMIGA)
        self.assertEqual((t.sps, [x.sps for x in t.extra]), (False, [True]))
        got = whdbuild._disk_data(t, t.extra)
        self.assertEqual(len(got), 3)
        self.assertTrue(got[2].endswith(b"ZapExp.ipf"))                                            # (the zip's member, not the zip)

    def test_an_sps_expansion_without_its_game_is_a_game_of_its_own(self) -> None:
        plan = self.plan(None, [(SPS_EXP, ["ZapExp.ipf"])])
        self.assertEqual((plan.selection.merged, plan.selection.decisions[20].merged_into), ({}, ""))
