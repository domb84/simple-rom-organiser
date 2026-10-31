#!/usr/bin/env python3
"""Make ``romorg/whdexpansions.json``: the expansions a game's WHDLoad install takes in with the game (see ``romorg/whdexpansions.py``).

The SEED below is what was found by installing each package on real disks and booting the result (expansion disks appended after the
game's, the expansion's questions answered yes): only expansions that installed and booted are in it. What this tool adds is the check
that each answer still matches a question the package's Install script asks - a package that was updated on whdload.de and no longer
asks it is reported, and the table is not written. The packages come from the app's cache or whdload.de (``ROMORG_DATA_DIR`` for the cache;
``ROMORG_OFFLINE=1`` for the cache alone).

    python tools/gen_whdexpansions.py            # check and write romorg/whdexpansions.json
    python tools/gen_whdexpansions.py --check    # check only (exit 1 when an answer matches no question)
    python tools/gen_whdexpansions.py --list PKG...    # the questions of install packages (to write a new SEED entry)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from romorg import amigainstall as ai, whdinstalls  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "romorg" / "whdexpansions.json"


def bool_(prompt: str) -> dict[str, str]:
    return {"kind": "bool", "prompt": prompt}


def options(prompt: str, choice: str) -> dict[str, str]:
    return {"kind": "options", "prompt": prompt, "choice": choice}


def choice(choice_: str, prompt: str = "version") -> dict[str, str]:
    return {"kind": "choice", "prompt": prompt, "choice": choice_}


def exp(*titles: str, asks: list) -> dict[str, Any]:
    return {"titles": list(titles), "asks": asks}


SEED_MERGE: list[dict[str, Any]] = [
    {"archive": "Cadaver.lha", "base": ["Cadaver"],
     "expansions": [exp("Cadaver - The Pay Off", asks=[options("data disk", "payoff")])]},
    # the Witch Lord install wants the blank save disk between the game's disk and its own; the slave boots the [cr CABAL] copy only
    {"archive": "HeroQuest.lha", "base": ["HeroQuest"], "save_disk": True, "base_pick": r"\[cr CABAL\]",
     "expansions": [exp("HeroQuest - Return of the Witch Lord", asks=[bool_("Expansion Disk")])]},
    {"archive": "Falcon.lha", "base": ["Falcon"],
     "expansions": [exp("Falcon - Mission Disk Volume 1 - Operation Counterstrike", asks=[bool_("Counterstrike")]),
                    exp("Falcon - Mission Disk Volume II - Operation Firefight", asks=[bool_("Firefight")])]},
    # (assume: the shipped list has no working result for the kept ADFs alone; with the data disks the install ran and booted)
    {"archive": "BattleIsle.lha", "base": ["Battle Isle"], "assume": True,
     "expansions": [exp("Battle Isle - Scenario Disk Volume 1 - Air-Land-Sea Strategy", asks=[bool_("First Data Disk")]),
                    exp("Battle Isle '93 - The Moon of Chromos", "Battle Isle '93 - Scenario Disk Volume 2 - The Moon of Chromos",
                        asks=[bool_("Second Data Disk")])]},
    {"archive": "EarlWeaverBaseball.lha", "base": ["Earl Weaver Baseball"],
     "expansions": [exp("Earl Weaver Baseball - Commissioner's Disk", asks=[bool_("Commissioner")])]},
    {"archive": "Corporation.lha", "base": ["Corporation"], "assume": True,
     "expansions": [exp("Corporation - Mission Disk", asks=[choice("Mission Disk Release")])]},
    {"archive": "LordsOfChaos.lha", "base": ["Lords of Chaos"],
     "expansions": [exp("Lords of Chaos - Expansion Kit One", asks=[choice(r"\+ Expansion Disk")])]},
    {"archive": "SpaceCrusade.lha", "base": ["Space Crusade"],
     "expansions": [exp("Space Crusade Expansion - The Voyage Beyond", asks=[options("data disk", "Expansion disk")])]},
    {"archive": "PGATourGolf.lha", "base": ["PGA Tour Golf"],
     "expansions": [exp("PGA Tour Golf - Tournament Course Disk", asks=[choice("Game and Tournament")])]},
    {"archive": "TestDrive2.lha", "base": ["Test Drive II - The Duel"], "assume": True,
     "expansions": [exp("Test Drive II - California Challenge", asks=[options("install/add", "California")]),
                    exp("Test Drive II - European Challenge", asks=[options("install/add", "European")]),
                    exp("Test Drive II - The Muscle Cars", asks=[options("install/add", "Muscle")]),
                    exp("Test Drive II - The Supercars", asks=[options("install/add", "Supercars")])]},
    {"archive": "WCLeaderboardGolf.lha", "base": ["World Class Leader Board"],
     "expansions": [exp("World Class Leader Board - Extra Courses", asks=[options("install", r"courses disk #1")]),
                    exp("World Class Leader Board - Extra Courses 2", asks=[options("install", r"courses disk #2")])]},
    # Hole-In-One (the Files version: the script sets the version, no version question) asks "install data #1 (Menagerie)?" then "data #2
    # (Holidays)?", Yes / No each, asking for the disk by a file on it ("Intermediate", then "Holidays"): data #1 is Course Disk #2
    # (Intermediate & Menagerie), data #2 is Course Disk #3 (Alphabet Soup, Out of this World, Holidays). The game is TOSEC's "Hole-In-One"
    # (program disk) and the Disc Company's "Hole-In-One - Miniature Golf" (Starter Kit). Mean 18's course disks are another game.
    # (Data #2's disk was not at hand to try: it is in by the user's decision, assumed complete and installable.)
    {"archive": "HoleInOne.lha", "base": ["Hole-In-One", "Hole-In-One - Miniature Golf"],
     "expansions": [exp("Hole-In-One - Course Disk #2 - Intermediate & Menagerie", "Hole-In-One - Course Disk - Intermediate & Menagerie",
                        asks=[bool_("Menagerie")]),
                    exp("Hole-In-One - Course Disk #3 - Alphabet Soup & Out of this World & Holidays", asks=[bool_("Holidays")])]},
]

# expansions with an install of their own (built from their disks alone), and the install of one disk of a package of two
SEED_STANDALONE: list[dict[str, Any]] = [
    {"title": "Kick Off 2 - Return to Europe", "archive": "KickOff2ReturnToEurope.lha", "level": 3},
    {"title": "Kick Off 2 - The Final Whistle", "archive": "KickOff2FinalWhistle.lha", "level": 3},
    {"title": "PowerMonger - World War 1 Edition", "archive": "PowermongerWW1.lha", "level": 3},
    {"title": "Kick Off 3 - European Challenge", "archive": "KickOff3EC.lha", "level": 3},
    # (Graham Gooch's Second Innings is not here: its install from the expansion disk alone stops at a WHDLoad requester - it wants the
    #  game's disk too, which this table's merge cannot give it, the expansion being the one with the install)
    # installs but no boot seen with the copies at hand (a crack or variant the slave refuses is likely): built unless the shipped list
    # has the very disks as not booting
    {"title": "Fighter Bomber - Advanced Mission Disk", "archive": "FighterBomberAM.lha", "level": 1},
    {"title": "Chaos Strikes Back", "archive": "ChaosStrikesBack.lha", "level": 1},
    # Bloodwych's package installs "Bloodwych" or "Bloodwych Extended Levels" (BloodwychExt.Slave): the Extended Levels disk alone,
    # the copy that holds C/BEXT (the cracked [cr Defjam - CCS] one fails with DOS error 205 'BEXT')
    {"title": "Bloodwych - The Extended Levels", "archive": "Bloodwych.lha", "level": 3,
     "asks": [{"kind": "choice", "prompt": "install", "choice": "Extended Levels"}], "prefer_disk": "C/BEXT"},
]


# --------------------------------------------------------------------------- what a package asks

_I = ai._Interp(None, "x", None)


def _ev(x: Any) -> Any:
    try:
        return _I.ev(x)
    except Exception:  # noqa: BLE001 - a prompt that needs the running script: not known here
        return ""


def questions(script: str) -> list[tuple[str, str, list[str]]]:
    """``[(command, prompt, [choices])]`` of the ask commands of an Install script (a prompt built with ``cat`` and plain variables is
    evaluated; one that needs the running script is empty)."""
    out: list[tuple[str, str, list[str]]] = []

    def walk(f: Any) -> None:
        if not isinstance(f, list):
            return
        if f and isinstance(f[0], ai._Sym) and f[0].name in ("askbool", "askoptions", "askchoice"):
            o: dict[str, list] = {}
            for a in f[1:]:
                if isinstance(a, list) and a and isinstance(a[0], ai._Sym):
                    o[a[0].name] = [x if isinstance(x, (str, int)) else _ev(x) for x in a[1:]]
            out.append((f[0].name, " ".join(str(p) for p in o.get("prompt", [])), [str(c) for c in o.get("choices", [])]))
        for x in f:
            walk(x)
    walk(ai._parse(script))
    return out


def package_questions(archive: str) -> list[tuple[str, str, list[str]]]:
    with ai.Package(whdinstalls.package({"archive": archive})[0]) as pkg:
        out: list[tuple[str, str, list[str]]] = []
        for s in pkg.scripts:
            out += questions(s.read_text(encoding="latin-1"))
        if len(pkg.scripts) > 1:                         # (version names too: "Standard + Mission Disk Release")
            pass
        out.append(("askchoice", "version", list(pkg.names.values())))
        return out


def matches(ask: dict[str, str], qs: list[tuple[str, str, list[str]]]) -> bool:
    kind = ask["kind"]
    for cmd, prompt, choices in qs:
        if ask.get("prompt") and not re.search(ask["prompt"], prompt, re.IGNORECASE) and not (kind == "choice" and prompt == "version"
                                                                                           and ask["prompt"] == "version"):
            continue
        if kind == "bool" and cmd in ("askbool", "askchoice"):
            return True
        if kind == "options" and cmd == "askoptions" and any(re.search(ask["choice"], c, re.IGNORECASE) for c in choices):
            return True
        if kind == "choice" and cmd == "askchoice" and any(re.search(ask["choice"], c, re.IGNORECASE) for c in choices):
            return True
    return False


def check(seed: list[dict[str, Any]], standalone: list[dict[str, Any]]) -> list[str]:
    problems = []
    for m in seed:
        qs = package_questions(m["archive"])
        for e in m["expansions"]:
            for a in e["asks"]:
                if not matches(a, qs):
                    problems.append(f"{m['archive']}: no question matches {a} (expansion {e['titles'][0]})")
    for r in standalone:
        for a in r.get("asks") or ():
            if not matches(a, package_questions(r["archive"])):
                problems.append(f"{r['archive']}: no question matches {a} ({r['title']})")
        try:
            whdinstalls.package({"archive": r["archive"]})
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{r['archive']}: {exc}")
    return problems


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--list":
        for arc in argv[1:]:
            print("#####", arc)
            for cmd, prompt, choices in package_questions(arc):
                print("  ", cmd, repr(prompt)[:120], [c[:40] for c in choices if c][:8])
        return 0
    problems = check(SEED_MERGE, SEED_STANDALONE)
    for p in problems:
        print("PROBLEM:", p, file=sys.stderr)
    if problems:
        return 1
    if argv[:1] == ["--check"]:
        print(f"ok: {len(SEED_MERGE)} installs take expansions, {len(SEED_STANDALONE)} expansions have their own")
        return 0
    OUT.write_text(json.dumps({"merge": SEED_MERGE, "standalone": SEED_STANDALONE}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
