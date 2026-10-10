"""The databases the app uses, one row each, with where they come from and which systems need them (the Databases page).

Everything the app knows about a game - what it is called, which region and languages it has, which files make it complete - comes from
one of these lists. The state (installed version, newest known, last checked) is the updater's (``autoupdate``); this module adds what the
updater does not carry: the address, the systems that use the list, and the lists that ship with the app.
"""

from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote

from . import biosdata, platforms

__all__ = ["describe"]

GROUP_DAT = "Game lists (DATs)"
GROUP_NAMES = "Names and IDs (made into the catalogues of the Wii U and Switch)"
GROUP_OTHER = "Other"

TOSEC_URL = "https://www.tosecdev.org/downloads"
NOINTRO_URL = "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/no-intro/"
NOINTRO_HOME = "https://no-intro.org/"
WHDLOAD_URL = "https://github.com/MrV2K/WHDLoad-Database"
REDUMP_URL = "http://redump.org/datfile/"
REDUMP_SLUG = {"Sega - Dreamcast": "dc", "Sony - PlayStation": "psx", "Sony - PlayStation 2": "ps2", "Nintendo - GameCube": "gc",
               "Nintendo - Wii": "wii"}
TITLEDB_URL = "https://github.com/blawar/titledb"
GAMETDB_URL = "https://www.gametdb.com/"
GAMETDB_WIIU = "https://www.gametdb.com/wiiutdb.zip"
NOINTRO_WIIU = NOINTRO_URL + "Nintendo%20-%20Wii%20U%20(Digital).dat"
PCSX2_INDEX = "https://github.com/PCSX2/pcsx2/blob/master/bin/resources/GameIndex.yaml"
LAUNCHBOX_URL = "https://gamesdb.launchbox-app.com/"
SYSTEM_DAT = "https://github.com/libretro/libretro-database/blob/master/dat/System.dat"


def _used_by() -> Dict[str, List[str]]:
    """DAT name -> the systems that use it."""
    out: Dict[str, List[str]] = {}
    for p in platforms.list_platforms():
        for dat in p.dats:
            out.setdefault(dat, []).append(p.name)
    return out


def _row(group: str, source: str, name: str, url: str, used: List[str], block: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"group": group, "source": source, "name": name, "url": url, "used_by": used,
            "installed": block.get("installed") or block.get("version"), "latest": block.get("latest"),
            "status": block.get("status") or "unknown", "checked_at": block.get("checked_at"), **extra}


def describe(u: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The rows, from the updater's status ``u`` (``autoupdate.UpdateManager.status()``)."""
    used = _used_by()
    rows: List[Dict[str, Any]] = []
    t = u.get("tosec") or {}
    rows.append(_row(GROUP_DAT, "TOSEC", "Full DAT pack (all TOSEC systems)", TOSEC_URL, used.get("Commodore Amiga - Games - [ADF]", []), t,
                     note="about 100 MB; fetched only when its release date changes"))
    for key, label, url_of in (("nointro", "No-Intro", lambda n: NOINTRO_URL + quote(n) + ".dat"),
                               ("whdload", "WHDLoad", lambda n: WHDLOAD_URL),
                               ("redump", "Redump", lambda n: REDUMP_URL + REDUMP_SLUG.get(n, "") + "/" if n in REDUMP_SLUG else REDUMP_URL)):
        b = u.get(key) or {}
        for d in b.get("dats") or ():
            rows.append(_row(GROUP_DAT, label, d["name"], url_of(d["name"]), used.get(d["name"], []),
                             {**d, "installed": d.get("version"), "checked_at": b.get("checked_at"),
                              "latest": d.get("latest") or (d.get("version") if d.get("status") == "up_to_date" else None)},
                             note=("No-Intro DATs come from the libretro-database mirror of " + NOINTRO_HOME) if key == "nointro" else ""))
    sw, gt = u.get("switchdb") or {}, u.get("gametdb") or {}
    sw_installed = sw.get("installed")
    rows.append(_row(GROUP_NAMES, "titledb", "Nintendo Switch titles: names, languages, stores, NCA content IDs, every update version",
                     TITLEDB_URL, ["Nintendo Switch"], sw, note="about 100 MB over the wire; US, GB and JP store lists, cnmts.json, versions.json"))
    for dat in ("Nintendo - Switch", "Nintendo - Switch (Updates)", "Nintendo - Switch (DLC)"):
        rows.append(_row(GROUP_DAT, "made from titledb", dat, TITLEDB_URL, used.get(dat, []), {**sw, "installed": sw_installed},
                         note="no checksums exist: matched by title ID (and version)"))
    rows.append(_row(GROUP_NAMES, "GameTDB", "Wii and Wii U: disc titles by game ID", GAMETDB_URL + "wiitdb.txt", ["Nintendo Wii", "Nintendo Wii U"], gt,
                     note="wiitdb.txt, wiiutdb.txt"))
    rows.append(_row(GROUP_NAMES, "GameTDB", "Wii U: disc games with region and languages", GAMETDB_WIIU, ["Nintendo Wii U"], gt))
    rows.append(_row(GROUP_NAMES, "No-Intro", "Wii U (Digital): the spelling and languages of the Wii U names", NOINTRO_WIIU, ["Nintendo Wii U"], gt,
                     note="fetched with GameTDB's lists; optional"))
    rows.append(_row(GROUP_NAMES, "PCSX2", "PlayStation 2: game names by serial (GameIndex.yaml)", PCSX2_INDEX, ["Sony PlayStation 2"], gt,
                     note="fetched with GameTDB's lists; names unmatched saves"))
    rows.append(_row(GROUP_DAT, "made from GameTDB", "Nintendo - Wii U", GAMETDB_WIIU, used.get("Nintendo - Wii U", []), gt,
                     note="no checksums exist: matched by the product code"))
    r = u.get("ratings") or {}
    rows.append(_row(GROUP_OTHER, "LaunchBox", f"Community ratings{' (%s games)' % r['games'] if r.get('games') else ''}", LAUNCHBOX_URL,
                     [p.name for p in platforms.list_platforms()], r, note="optional: fetched when a rating filter needs it"))
    rows.append({"group": GROUP_OTHER, "source": "libretro", "name": "BIOS and firmware checksums (System.dat)", "url": SYSTEM_DAT,
                 "used_by": ["every system"], "installed": biosdata.VERSION, "latest": None, "status": "bundled", "checked_at": None,
                 "note": "ships with the app (names and checksums only): a BIOS file is kept with the ROMs"})
    return rows
