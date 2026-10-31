"""The other titles of a game: the names it was released under in other regions (``Biohazard - Code - Veronica`` and ``Resident Evil -
Code - Veronica``, ``Eternal Arcadia`` and ``Skies of Arcadia``, ``Zelda no Densetsu`` and ``The Legend of Zelda``).

The DATs know a game by the title of each release, so a game with a title per region is several games to them. The rule "Combine
alternate titles" makes it one: of its releases one is kept (by your regions and languages) and it counts once.

Where the titles come from (no download of its own, both are there for other things):

* the ratings index (LaunchBox): a game has a database id, its name and its alternate names (``alt`` rows); every name of one id is one
  game;
* GameTDB's Wii U list: a disc id is a game code, a region letter and a publisher (``ALZE01`` / ``ALZJ01`` / ``ALZP01``); the ids that
  differ only in the region letter are one game;
* the OpenRetro database (the Amiga): a game and the releases it was made of, by disk checksum - the titles TOSEC files them under
  ("Der Clou!" / "Clue!", "Detroit" / "Rüsselsheim") are one game. A compilation's title ("APB & Xybots") joins nothing.

A title is compared in the ratings' normal form (case, punctuation, articles, accents and roman numerals do not count). A name two games
share in the index (two ids) joins nothing. A group of titles is known by a key of its own (``\\x00`` + its first title), so it can never
be taken for a DAT title. :func:`table` is ``{normal title: group key}`` of the titles that have another one, per system (cached).
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import unicodedata
from typing import Any, Optional

from . import kana, ratings, tags

__all__ = ["SAME_GAME", "node", "link", "series_title", "hand_games", "notice", "table", "groups", "learn", "forget", "dat_map", "group", "merged", "signature", "clear_cache", "STYLES"]

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[Any, dict[str, str]]] = {}


def node(title: str) -> str:
    """The form a title is compared in. A title of another script (Japanese, Korean, Chinese, Cyrillic ...) is compared by its own letters
    (``ratings.native_key``, ``~`` + them): its Latin letters alone (``"大逆転裁判1＆2"`` would be ``"1 2"``) would join unrelated games."""
    title = _OTHER_SCRIPT_PART_RE.sub(" ", title or "").strip()     # ("ABZÛ (アブズ)": the Japanese name in brackets is a gloss)
    if ratings.has_other_script(title):
        return ratings.native_key(title)
    return ratings.roman_key(ratings.norm_title(title))


def link(title: str) -> str:
    """:func:`node` but for a title of another script that has Latin words with it (``"Astroneer -アストロニーア-"``, ``"Fit Boxing 体験版"``):
    those words, as the title an English release would have. Such a title joins an English release of that title (and no other title in
    Japanese script: ``"G-MODE ... Vol.3"`` is a Latin remainder of many games)."""
    n = node(title)
    if not n.startswith("~"):
        return n
    s = unicodedata.normalize("NFKD", title or "")
    runs = re.split(r"\0+", "".join("\0" if _other_script(c) else c for c in s))
    at = [i for i, r in enumerate(runs) if re.search(r"[A-Za-z]{2}", r)]
    if len(at) != 1 or (at[0] > 0 and at[0] < len(runs) - 1):     # (Japanese on both sides of the words - "クラシック レーシング パック：Moto
        return n                                                   #  Roader MC + バイク..." - is another game: a pack)
    other = "".join(c for c in title if _other_script(c))
    # (a long Japanese text after the words is a subtitle or another game: "Fit Boxing 北斗の拳 ～お前はもう痩せている～"; an art book, a
    #  soundtrack, a bonus app of a game is no release of it)
    if _EXTRAS_RE.search(other):
        return n
    if len(other) > (20 if at[0] > 0 else 10) and not kana.gloss(other, runs[at[0]]):
        return n                        # (unless the Japanese text is the Latin words in katakana: "Skyline Bowling - スカイライン・ボウリング")
    words = [runs[at[0]]]
    rest = ratings.roman_key(ratings.norm_title(words[0]))
    if len(rest) < 4 or not any(w not in _FILLER for w in re.findall(r"[a-z]{4,}", rest)):
        return n                        # ("Vol.2", "Part 2", "Demo" tell nothing)
    return rest


_FILLER = frozenset("vol volume part disc demo trial edition version pack special deluxe digital collection nintendo switch".split())
_EXTRAS_RE = re.compile("デジタル|サウンドトラック|アートブック|アートワーク|特典|追加|コンテンツ|セット|パック|コレクション|壁紙|ボーナス")


def _other_script(c: str) -> bool:
    return c.isalpha() and ord(c) > 0x24F and not unicodedata.combining(c)


# a bracketed part of a title that holds a letter of another script: "(アブズ)", "（ダークサイダーズ ウォーマスターエディション）"
_OTHER_SCRIPT_PART_RE = re.compile(r"[(（][^()（）]*[^\x00-\u024f()（）\s][^()（）]*[)）]")


def _signature(paths: list[Any]) -> tuple:
    out = []
    for p in paths:
        try:
            st = p.stat()
            out.append((str(p), st.st_mtime_ns, st.st_size, st.st_ino))
        except OSError:
            out.append((str(p), None))
    return tuple(out)


_NAMES_FILE = "alttitles.json"
_NAMES_MEM: dict[str, tuple[Any, dict[str, Any]]] = {}


def _names_file(index: Any) -> Any:
    return index.parent / _NAMES_FILE


def _remembered(index: Any) -> dict[str, Any]:
    """The names of the titles of the last ratings index that was there (``{LaunchBox platform: {"names": [[key, dbid]]}}``): what the
    alternate titles are made of when the index is gone (deleted, not downloaded again yet, a data folder copied without it). Without
    it every game of one title in other regions would be a game of its own again, quietly."""
    f = _names_file(index)
    try:
        st = f.stat()
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _NAMES_MEM.get(str(f))
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
        import json
        data = json.loads(f.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    with _LOCK:
        _NAMES_MEM[str(f)] = (sig, data)
    return data


def _remember(index: Any, lb: str, names: list[tuple[str, int]]) -> None:
    """Keep ``names`` (this index's) for when the index is not there; written only when they changed (never an error)."""
    try:
        import json
        import os
        data = dict(_remembered(index))
        try:
            st = index.stat()
            stamp = [st.st_mtime_ns, st.st_size]
        except OSError:
            return
        if data.get(lb, {}).get("stamp") == stamp:
            return
        data[lb] = {"stamp": stamp, "names": [[k, d] for k, d in names]}
        f = _names_file(index)
        tmp = f.with_name(f.name + ".part")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, f)
    except (OSError, ValueError, TypeError):
        pass


def _read_names(lb: str, path: Any) -> Optional[list[tuple[str, int]]]:
    """``(name, dbid)`` of every name of the system in the ratings index (main and alternate names); None: no index to read."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return [(k, int(d)) for k, d in con.execute("SELECT key, dbid FROM entry WHERE plat=? AND dbid IS NOT NULL", (lb,))]
        finally:
            con.close()
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _lb_names(lb: str, path: Any) -> list[tuple[str, int]]:
    """``(name, dbid)`` of every name of the system in the ratings index (main and alternate names); the remembered ones of the last
    index when there is none (:func:`notice` says so)."""
    names = _read_names(lb, path) if path is not None and path.is_file() else None
    if names is None:
        known = _remembered(path).get(lb, {}).get("names", ()) if path is not None else ()
        return [(str(k), int(d)) for k, d in known]
    if names:
        _remember(path, lb, names)
    return names


def notice(platform: Any) -> str:
    """Text for the user when "Combine alternate titles" has less to go on than it should for this system, else "": the ratings data
    (LaunchBox's alternate names) is not installed. The remembered names of the last index are used if there are any; otherwise only
    GameTDB, OpenRetro and the hand-checked pairs are, and a game released under another title in other regions counts twice."""
    name = getattr(platform, "name", platform)
    lb = ratings.lb_platform(name)
    if not lb:
        return ""
    path = ratings.index_path(ratings.default_store().directory)
    if path.is_file() and _read_names(lb, path):
        return ""
    last = _remembered(path).get(lb, {}).get("names")
    if last:
        return (f"The ratings data is not installed: the alternate titles of {name} are those of the index that was there before. "
                "Download the ratings data (Ratings rules) to bring them up to date.")
    return (f"The ratings data is not installed: games released under other titles in other regions (Biohazard / Resident Evil) "
            f"are not combined for {name} until it is (Ratings rules: Download ratings).")


def _gametdb_names(kinds: tuple[str, ...], path: Any) -> list[tuple[str, str]]:
    """``(name, game)`` of GameTDB's ids of these kinds, a game = the id without its region letter."""
    from . import tags
    out = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(f"SELECT id, name FROM names WHERE kind IN ({','.join('?' * len(kinds))})", kinds).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    for gid, name in rows:
        if len(gid) < 4 or not name:
            continue
        title = tags.parse_name(name, tags.STYLE_NOINTRO).title if "(" in name else name     # (the disc list has No-Intro names)
        out.append((title, gid[:3] + gid[4:]))
    return out


# Systems whose GameTDB ids carry the region (the kinds of ``names`` to read)
_GAMETDB_KINDS = {"Nintendo Wii U": ("wiiu", "wiiu_disc")}


def _openretro_names(platform_name: str, opath: Any) -> list[tuple[str, str]]:
    """``(title, game id)`` of OpenRetro's games (the Amiga's), without compilations' titles."""
    if platform_name != "Commodore Amiga" or opath is None:
        return []
    from . import openretro
    db = openretro.database(opath)
    if db is None:
        return []
    return [(t, g) for t, g in db.titles() if not re.search(r"\s(?:&|\+|and)\s", t, re.IGNORECASE)]


# Titles of one game that no database joins, checked by hand (the duplicate sweep of 2026-10-23): a game known with and without its
# subtitle, an edition named in the title, a pirate copy with a longer name. A title here names every release under it, so a short one is
# added only when the DAT has no other game of that title (the Amiga's "Myth" is Magnetic Scrolls', not System 3's "Myth - History in the
# Making"). An edition word alone proves nothing: "Jeopardy! - Sports Edition" and "Pac-Man - Championship Edition" are other games.
SAME_GAME: dict[str, list[tuple[str, ...]]] = {
    "Commodore Amiga": [
        ("Speedball 2", "Speedball 2 - Brutal Deluxe"), ("Rainbow Islands", "Rainbow Islands - The Story of Bubble Bobble 2"),
        ("Journey", "Journey - The Quest Begins"), ("Brian the Lion", "Brian the Lion Starring in Rumble in the Jungle"),
        ("Enemy - Tempest of Violence", "Enemy - Tempest of Violence v1.04 EasyPlay"), ("Gloom", "Gloom Deluxe", "Gloom - Special Edition 97"),
        ("Harpoon", "Harpoon - Signature Edition"), ("PGA Tour Golf", "PGA Tour Golf - Platinum Edition"),
        ("Championship Manager '94", "Championship Manager '94 - End of Season Edition"),
        ("World Class Rugby", "World Class Rugby - Five Nations Edition"),
        ("Hibernated 1 - This Place is Death", "Hibernated 1 - This Place is Death - Director's Cut"),
        ("A320 Airbus", "A320 Airbus - Edition Europa", "A320 Airbus - Edition USA"), ("Brutal Football", "Brutal Football - Deluxe Edition"),
        ("Sensible World of Soccer '95-'96", "Sensible World of Soccer '95-'96 - European Championship Edition"),
        ("Sensible Soccer", "Sensible Soccer - International Edition", "Sensible Soccer - European Champions - 92-93 Season"),
        ("Eishockey Manager", "Eishockey Manager - Limited Edition")],
    "Super Nintendo Entertainment System": [
        ("SimAnt", "SimAnt - The Electronic Ant Colony"), ("Angelique", "Angelique - Premium Box"),
        ("Donkey Kong Country", "Donkey Kong Country - Competition Cartridge"), ("Super Bomberman 5", "Super Bomberman 5 - Caravan Event Ban"),
        ("Super Fire Pro Wrestling X", "Super Fire Pro Wrestling X Premium")],
    "Nintendo Entertainment System": [
        ("Kid Kool", "Kid Kool and the Quest for the Seven Wonder Herbs"), ("Harry's Legend", "Harry's Legend - Hali Chuanqi"),
        ("Fengshenbang", "Fengshenbang - Myth Struggle"), ("Sanguozhi - Yingjie Zhuan", "Sanguozhi - Yingjie Zhuan - Hero Legend"),
        ("Shuihu Zhuan", "Shuihu Zhuan - Marsh Outlaws"), ("Tiny Toon 3", "Tiny Toon 3 - Crazy Castle")],
    "Nintendo Game Boy Advance": [
        ("Yggdra Union", "Yggdra Union - We'll Never Fight Alone"), ("Hi Hi Puffy AmiYumi", "Hi Hi Puffy AmiYumi - Kaznapped!"),
        ("Pokemon - Sapphire Version", "Pokemon - Sapphire Version - Gotta Catch 'em All!"),
        ("Strawberry Shortcake - Summertime Adventure", "Strawberry Shortcake - Summertime Adventure - Special Edition")],
    "Nintendo 64": [("NFL Blitz", "NFL Blitz - Special Edition"), ("77a", "77a Special Edition")],
    "Sega Mega Drive - Genesis": [("Sonic 3D Blast", "Sonic 3D Blast - Director's Cut")],
    # (two titles of one game released in ONE region - the same-region veto of ``dat_map`` would keep them apart; the hand wins over it)
    "Sega Master System": [("Minesweeper", "Mine Sweeper")],
    "Sony PlayStation 2": [("Adiboo and the Energy Thieves", "Energy Thieves, The")],
    "Nintendo Game Boy Color": [("O'Leary Manager 2000", "Barca Total 2000")],
    # (titles that differ only by a number are two games in a region that has both - ``_numbered`` - unless checked here; each pair of the
    #  first two is also in ``library.KEEP_APART_PLATFORMS``, which takes the Japanese release of the shared title out of the game)
    "Nintendo Game Boy": [("Bomberman GB", "Bomberman GB 2")],
    "Sony PlayStation": [
        ("King's Field II", "King's Field III"),
        ("Motor Toon Grand Prix", "Motor Toon Grand Prix 2", "Motor Toon Grand Prix - USA Edition"),     # (the USA edition: 571.6 MB, the USA game 569.0)
        # (a two-disc game: the USA names it once, Europe names each disc after its game)
        ("Final Fantasy Origins", "Final Fantasy Origins - Final Fantasy", "Final Fantasy Origins - Final Fantasy II")],
}
SAME_GAME["Super Nintendo Entertainment System"].append(("Cannondale Cup", "Exertainment Mountain Bike Rally"))
# (the ratings data of 2026-10-30 lost these names, which the data of 2026-10-14 had: the same game, checked in the DATs)
SAME_GAME["Nintendo Game Boy Advance"].append(("Acrobat Kid", "Aero the Acro-Bat"))                          # (4 MB each, Japan / USA, Europe)
SAME_GAME["Nintendo Entertainment System"].append(("1943", "1943 - The Battle of Midway", "1943 - The Battle of Valhalla"))
SAME_GAME["Sony PlayStation 2"].extend([                                      # (SpongeBob: one game under the title of each language)
    ("Nickelodeon Bob Esponja - La Pelicula", "Nickelodeon SpongeBob - Il Film", "Nickelodeon SpongeBob Schwammkopf - Der Film"),
    ("Nickelodeon Bob Esponja - Luces, Camara, Esponja!", "Nickelodeon Bob l'Eponge - Silence on Tourne !", "Nickelodeon SpongeBob - Ciak si Gira!",
     "Nickelodeon SpongeBob Schwammkopf - Film Ab!", "Nickelodeon SpongeBob SquarePants - Lights, Camera, Pants!"),
    ("Nickelodeon Bob l'Eponge - La Bataille de Bikini Bottom", "Nickelodeon SpongeBob Schwammkopf - Schlacht um Bikini Bottom",
     "Nickelodeon SpongeBob SquarePants - Battle for Bikini Bottom")])


def hand_games(platform_name: str) -> dict[str, frozenset]:
    """``{normal title: the hand-checked games (SAME_GAME) it is a title of}``: two titles of one game here are one game whatever else
    says otherwise (:func:`_two_games` never splits them)."""
    out: dict[str, set] = {}
    for n, titles in enumerate(SAME_GAME.get(platform_name, ())):
        for title in titles:
            out.setdefault(node(title), set()).add(n)
    return {k: frozenset(v) for k, v in out.items()}


# The Japanese budget series: a game re-released as "SuperLite 1500 Series - Ring of Sias", "Simple 1500 Series Vol. 93 - The Puzzle
# Bobble - Puzzle Bobble 4" is the game of the bare title ("Ring of Sias", "Puzzle Bobble 4"), not a game of its own.
_SERIES_RE = re.compile(
    r"^(?:super ?lite (?:\d{3,4}|gold|3in1)(?: series)?|simple \d{4}(?: [a-z]+)?(?: series)?|value \d{4}|world greatest hits series|"
    r"arcade hits)(?:\s+vol\.?\s*\d+)?\s+-\s+", re.IGNORECASE)


def series_title(title: str) -> str:
    """``title`` without the name of a budget series in front of it ("" prefix: the title itself)."""
    return _SERIES_RE.sub("", title or "", count=1)


def _build(lb: str, platform_name: str, rpath: Any, gpath: Any, opath: Any = None) -> dict[str, list[str]]:
    """``{group key: [normal titles]}`` of the groups of two or more titles."""
    owners: dict[str, set] = {}                         # normal title -> the games (("lb", id) / ("gt", code) / ("or", id)) it names
    for n, titles in enumerate(SAME_GAME.get(platform_name, ())):
        for title in titles:
            owners.setdefault(node(title), set()).add(("hand", n))
    for name, dbid in _lb_names(lb, rpath) if lb else ():
        owners.setdefault(name if name.startswith("~") else node(name), set()).add(("lb", dbid))
    for name, gid in _openretro_names(platform_name, opath):
        owners.setdefault(node(name), set()).add(("or", gid))
    kinds = _GAMETDB_KINDS.get(platform_name)
    if kinds:
        for name, code in _gametdb_names(kinds, gpath):
            owners.setdefault(node(name), set()).add(("gt", code))
    parent: dict[Any, Any] = {}

    def find(x: Any) -> Any:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for key, games in owners.items():
        if len(key) < 2:
            continue
        lbs = [g for g in games if g[0] == "lb"]
        if len(lbs) > 1:                                # (a name two LaunchBox games share joins nothing)
            continue
        if len([g for g in games if g[0] == "or"]) > 1:   # (one two OpenRetro games share: OpenRetro's say about it does not count)
            games = {g for g in games if g[0] != "or"}
        first = find(("t", key))
        for g in games:
            parent[find(g)] = first
    comps: dict[Any, list[str]] = {}
    for key in owners:
        if ("t", key) in parent:
            comps.setdefault(find(("t", key)), []).append(key)
    out: dict[str, list[str]] = {}
    for keys in comps.values():
        if len(keys) > 1:
            keys.sort()
            out["\x00" + keys[0]] = keys
    return out


def groups(platform: Any) -> dict[str, list[str]]:
    """``{group key: [normal titles]}`` of the system: every title of a group names one game."""
    from . import gametdb
    name = getattr(platform, "name", platform)
    lb = ratings.lb_platform(name)
    if not lb and name not in _GAMETDB_KINDS:
        return {}
    from . import openretro
    rpath, gpath = ratings.index_path(ratings.default_store().directory), gametdb.db_path()
    return _build(lb, name, rpath, gpath, openretro.db_path())


def table(platform: Any) -> dict[str, str]:
    """``{normal title: group key}`` of the system's titles that have another title (empty: none known)."""
    from . import gametdb
    name = getattr(platform, "name", platform)
    lb = ratings.lb_platform(name)
    if not lb and name not in _GAMETDB_KINDS:
        return {}
    from . import openretro
    rpath = ratings.index_path(ratings.default_store().directory)
    sig = _signature([rpath, gametdb.db_path(), openretro.db_path()])
    ck = f"{rpath}|{name}"
    with _LOCK:
        hit = _CACHE.get(ck)
        if hit is not None and hit[0] == sig:
            return hit[1]
    out = {t: g for g, ts in groups(name).items() for t in ts}
    with _LOCK:
        _CACHE[ck] = (sig, out)
    return out


def _sig_now() -> tuple:
    """The signature of the title sources, looked at no more than twice a second (game keys are asked for in thousands)."""
    global _SIG
    from . import gametdb
    now = time.monotonic()
    if _SIG is not None and now - _SIG[0] < 0.5:
        return _SIG[1]
    from . import openretro
    sig = _signature([ratings.index_path(ratings.default_store().directory), gametdb.db_path(), openretro.db_path()])
    _SIG = (now, sig)
    return sig


def signature(platform: Any = None) -> str:
    """Changes when the titles change (a new ratings index or GameTDB list)."""
    return repr(_sig_now())


# --------------------------------------------------------------------------- the titles of the DATs

STYLES = ("nointro", "redump", "tosec")
_DATS: dict[str, tuple[str, dict[str, tuple[frozenset, int]]]] = {}    # DAT -> (system, {title (casefold): (its regions, releases)})
_MAPS: dict[str, tuple[Any, dict[str, str]]] = {}
_SIG: Optional[tuple[float, tuple]] = None
_GEN = [0]                     # bumped whenever the registered titles change (what a cached game key depends on, with the sources)


def learn(platform: Any, dats: Any) -> None:
    """Take in the titles of the system's DATs (``platforms.load_platform_dats`` does it for every DAT it loads)."""
    from . import library, totals
    name = getattr(platform, "name", platform)
    games = set(ratings.rated_dats(platform))            # (the DATs of games: a system's updates, add-ons and firmware have no other titles)
    # a DAT whose games are another's (the Amiga's SPS / IPF releases and CD32 / CDTV discs are games of its floppy DAT: library.game_dat)
    # is learned with it, under its name: a title spelt otherwise there ("Fire & Ice" / "Fire and Ice"), or known by another name, is one game
    dats = [d for d in dats if not games or library.game_dat(getattr(d, "name", "")) in games]
    per: dict[str, dict[str, tuple[dict, int]]] = {library.game_dat(getattr(d, "name", "")): {} for d in dats}
    for it in totals.target_items(platform, dats):
        if library._style(it) not in STYLES:
            continue
        t = library._tags_of(it)
        d = per.setdefault(library.game_dat(it.dat), {})
        cf = t.title.casefold()
        rest = library.combined_key("", tags.game_key(t), True)[2:]     # (the release's kind: status, product tags ...)
        places, n = d.get(cf, ({}, 0))
        if not tags.unlicensed(t) and not any(tags.is_edition(f) for f in t.flags) and library.game_dat(it.dat) == it.dat:
            # (where the title was released: a re-release or a copy tells nothing, nor an SPS image or a CD of a game - the same release
            #  as a floppy of it, or one more of the game's: never a second game for the same places)
            places[rest] = places.get(rest, frozenset()) | _places(t)
        d[cf] = (places, n + 1)
    with _LOCK:
        for dat, titles in per.items():
            _DATS[dat] = (name, titles)
            _MAPS.pop(dat, None)
        _GEN[0] += 1


def _places(t: Any) -> frozenset:
    """Where a release is for: its regions; an Amiga (TOSEC) name its countries and languages, and ``-`` for an untagged one (the
    English / European release, no known region)."""
    if t.style in ("nointro", "redump"):
        return frozenset(t.regions)
    out = set(t.regions) | set(() if t.languages_implied else t.languages)
    return frozenset(out or {"-"})


def forget(dat: Optional[str] = None) -> None:
    with _LOCK:
        _GEN[0] += 1
        if dat is None:
            _DATS.clear()
            _MAPS.clear()
        else:
            _DATS.pop(dat, None)
            _MAPS.pop(dat, None)


_YEARS: dict[str, tuple[Any, dict[str, frozenset]]] = {}


def release_years(system: str) -> dict[str, frozenset]:
    """``{title (casefold): {years}}`` the catalogue of the system knows its releases by (empty: it has no release dates). Only the
    Switch has them (the eShop lists the title database is made of); the other systems' DATs name no year."""
    if system != "Nintendo Switch":
        return {}
    from . import switchdb
    path = switchdb.db_path()
    sig = _signature([path])
    with _LOCK:
        hit = _YEARS.get(system)
        if hit is not None and hit[0] == sig:
            return hit[1]
    out: dict[str, set] = {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT c.label, t.released FROM catalogue c JOIN titles t ON t.id = c.id WHERE c.dat = ? AND t.released > 0",
                               (switchdb.SWITCH_DAT,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        rows = []
    for label, released in rows:
        out.setdefault(tags.parse_name(label + ".nsp", tags.STYLE_NOINTRO).title.casefold(), set()).add(int(released) // 10000)
    frozen = {t: frozenset(y) for t, y in out.items()}
    with _LOCK:
        _YEARS[system] = (sig, frozen)
    return frozen


def _phonetic(titles: dict, years: dict[str, frozenset]) -> dict[str, str]:
    """``{title in katakana: the English title it is}`` of the titles whose katakana read as one English title of the same game year
    (within one year) and no other: ``リトルインフェルノ`` / ``Little Inferno``. A skeleton of fewer than six consonants, two English
    titles or two titles of katakana with it, or no year for either side, joins nothing."""
    if not years:
        return {}
    english: dict[str, list[str]] = {}
    native: dict[str, list[str]] = {}
    for cf in titles:
        if node(cf).startswith("~"):
            k = kana.ja_key(cf)
            if len(k) >= 6:
                native.setdefault(k, []).append(cf)
        elif cf in years:
            english.setdefault(kana.en_key(cf), []).append(cf)
    out: dict[str, str] = {}
    for k, ts in native.items():
        if len(ts) != 1 or len(english.get(k, ())) != 1:
            continue
        t, e = ts[0], english[k][0]
        if t in years and any(abs(a - b) <= 1 for a in years[t] for b in years[e]):
            out[t] = e
    return out


def dat_map(dat: str) -> dict[str, str]:
    """``{title (casefold): group key}`` of the DAT's titles that are one game with another title of it.

    Titles are one game when the ratings data / GameTDB say so, or when they are spelt alike (``Rock 'n' Roll Racing`` / ``Rock n' Roll
    Racing``, ``Streets of Rage 2`` / ``Streets of Rage II``). The title with the most releases leads; another joins it unless the two have
    releases of the same kind (status, product tags) for the very same regions: then they are two games (``Life is Strange Remastered`` and
    ``Life is Strange - Before the Storm Remastered``, both USA, Europe, are the halves of one collection to LaunchBox). A title that is
    the other one with a subtitle (``Super Mario World`` / ``Super Mario World - Super Mario Bros. 4``) always joins."""
    entry = _DATS.get(dat)
    if entry is None:
        return {}
    sig = _sig_now()
    hit = _MAPS.get(dat)
    if hit is not None and hit[0] == sig:
        return hit[1]
    system, titles = entry
    tbl = table(system)
    hand = hand_games(system)
    apart = not_same(system)
    latin = {n for t in titles for n in (node(t), node(series_title(t))) if n and not n.startswith("~")}
    latin_titles = {node(t) for t in titles}
    phonetic = _phonetic(titles, release_years(system))
    natives: dict[str, set] = {}            # (the words of the titles of another script: more than one game of them is a series, not a title)
    for t in titles:
        if node(series_title(t)).startswith("~"):
            natives.setdefault(link(series_title(t)), set()).add(node(series_title(t)))

    def key_of(cf: str) -> str:
        """The group a title is in: its ratings / GameTDB group, else the title itself without a budget series in front of it, and a title
        of another script with Latin words (``"Astroneer -アストロニーア-"``) is the title of an English release of those words."""
        bare = series_title(cf)
        nd, nb = node(cf), node(bare)
        hit = tbl.get(nb) or tbl.get(nd)
        if hit or not nb:
            return hit or ""
        if bare != cf and nb not in latin_titles:     # ("Simple 1500 Series Vol. 93 - The Puzzle Bobble - Puzzle Bobble 4": its last part is a title)
            parts = bare.split(" - ")
            for i in range(1, len(parts)):
                tail = node(" - ".join(parts[i:]))
                if tail in latin_titles:
                    return tbl.get(tail) or "\x01" + tail
        if cf in phonetic:                            # (katakana of one English title of the same year: "リトルインフェルノ")
            e = node(phonetic[cf])
            return tbl.get(e) or "\x01" + e
        if nb.startswith("~"):
            rest = link(bare)
            if rest != nb and (rest in latin or rest in tbl) and len(natives.get(rest, ())) < 2:
                return tbl.get(rest) or "\x01" + rest
        return "\x01" + nb

    by_group: dict[str, list[tuple[str, dict, int]]] = {}
    for cf, (places, n) in titles.items():
        key = key_of(cf)
        if key:
            by_group.setdefault(key, []).append((cf, places, n))
    out: dict[str, str] = {}
    for members in by_group.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (-m[2], m[0]))
        joined = [members[0]]
        for cand in members[1:]:
            if all(not _two_games(cand, other, hand, apart) for other in joined):
                joined.append(cand)
        if len(joined) > 1:
            for cf, _p, _n in joined:
                out[cf] = "\x00" + joined[0][0]
    with _LOCK:
        _MAPS[dat] = (sig, out)
    return out


def not_same(platform_name: str) -> frozenset:
    """``{frozenset({normal title, normal title})}`` of ``library.NOT_SAME_GAME``: two titles the data joins that are two games."""
    from . import library
    return frozenset(frozenset((node(a), node(b))) for a, b in library.NOT_SAME_GAME.get(platform_name, ()))


def _numbered(na: str, nb: str) -> bool:
    """Whether the normal titles ``na`` and ``nb`` are the same words with different numbers (``bomberman gb`` / ``bomberman gb 2``,
    ``kings field 2`` / ``kings field 3``, ``winter sports 2009 next challenge`` / ``winter sports 2 next challenge``)."""
    wa, wb = na.split(), nb.split()
    ra, rb = [w for w in wa if not w.isdigit()], [w for w in wb if not w.isdigit()]
    return bool(ra) and ra == rb and [w for w in wa if w.isdigit()] != [w for w in wb if w.isdigit()]


def _two_games(a: tuple, b: tuple, hand: Optional[dict] = None, apart: frozenset = frozenset()) -> bool:
    """Whether titles ``a`` and ``b`` (``(title, {release kind: places}, releases)``) are two games: a release of the same kind for the
    same region under both titles. Titles spelt alike, or one the other with a subtitle, are one game, and so are two titles of one
    hand-checked game (``SAME_GAME``): the hand wins over the veto. A title with the name of a budget series in front of it
    (``SuperLite 1500 Series - Ring of Sias``) is compared without it (and may be a longer form of the other title: ``Simple 1500 Series
    Vol. 93 - The Puzzle Bobble - Puzzle Bobble 4``)."""
    if hand and hand.get(node(a[0]), frozenset()) & hand.get(node(b[0]), frozenset()):
        return False
    if apart and frozenset((node(a[0]), node(b[0]))) in apart:
        return True
    sa, sb = series_title(a[0]), series_title(b[0])
    na, nb = node(sa), node(sb)
    if not (na.startswith("~") and nb.startswith("~")):         # (two titles of another script are the same game by their own letters only)
        na, nb = link(sa), link(sb)
    if _numbered(na, nb):
        # (titles that differ by a sequel number, a roman numeral or a year are not "a title with a subtitle": a region that has both -
        #  "King's Field II (Japan, USA)" and "King's Field III (Japan)" - has two games. A region of only one of them is a rename.)
        return any(p - {"-"} & q - {"-"} for p in a[1].values() for q in b[1].values())
    if na == nb or na.startswith(nb + " ") or nb.startswith(na + " "):
        return False
    if (sa != a[0] and na.endswith(" " + nb)) or (sb != b[0] and nb.endswith(" " + na)):
        return False
    # (the same regions under both titles: "Daimakaimura (Japan)" and "Ghouls'n Ghosts (Japan, USA)" are one game, "Life is Strange
    # Remastered (USA, Europe)" and "... Before the Storm Remastered (USA, Europe)" two; "-": untagged, no known region)
    return any(a[1][k] - {"-"} and a[1][k] - {"-"} == b[1][k] - {"-"} for k in a[1].keys() & b[1].keys())


def group(dat: str, title: str) -> Optional[str]:
    """The key of the game ``title`` (casefold) is one title of in ``dat``; None: it has no other title."""
    return dat_map(dat).get(title) if dat in _DATS else None


def merged(dat: str) -> list[list[str]]:
    """The titles of the DAT that are combined, a list per game (its leading title first)."""
    out: dict[str, list[str]] = {}
    for cf, g in dat_map(dat).items():
        out.setdefault(g, []).append(cf)
    return [sorted(v, key=lambda t: (t != g[1:], t)) for g, v in sorted(out.items())]


def clear_cache() -> None:
    global _SIG
    with _LOCK:
        _CACHE.clear()
        _MAPS.clear()
        _SIG = None
        _GEN[0] += 1


def state() -> tuple:
    """What the groups depend on: the registered titles and the title sources. A game key worked out under another state is stale."""
    return (_GEN[0], _sig_now())
