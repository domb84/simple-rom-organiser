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

from . import ratings, tags

__all__ = ["node", "table", "groups", "learn", "forget", "dat_map", "group", "merged", "signature", "clear_cache", "STYLES"]

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[Any, dict[str, str]]] = {}


def node(title: str) -> str:
    """The form a title is compared in; ``""`` for a title in another script (Japanese, Korean, Chinese, Cyrillic ...): its normal
    form keeps only the Latin letters and digits (``"大逆転裁判1＆2"`` would be ``"1 2"``), which would join unrelated games."""
    title = _OTHER_SCRIPT_PART_RE.sub(" ", title or "").strip()     # ("ABZÛ (アブズ)": the Japanese name in brackets is a gloss)
    s = unicodedata.normalize("NFKD", title)
    if any(_other_script(c) for c in s):
        return ""
    return ratings.roman_key(ratings.norm_title(title))


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


def _lb_names(lb: str, path: Any) -> list[tuple[str, int]]:
    """``(name, dbid)`` of every name of the system in the ratings index (main and alternate names)."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return [(k, int(d)) for k, d in con.execute("SELECT key, dbid FROM entry WHERE plat=? AND dbid IS NOT NULL", (lb,))]
        finally:
            con.close()
    except (sqlite3.Error, ValueError, TypeError):
        return []


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


def _build(lb: str, platform_name: str, rpath: Any, gpath: Any, opath: Any = None) -> dict[str, list[str]]:
    """``{group key: [normal titles]}`` of the groups of two or more titles."""
    owners: dict[str, set] = {}                         # normal title -> the games (("lb", id) / ("gt", code) / ("or", id)) it names
    for name, dbid in _lb_names(lb, rpath) if lb else ():
        owners.setdefault(node(name), set()).add(("lb", dbid))
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


def learn(platform: Any, dats: Any) -> None:
    """Take in the titles of the system's DATs (``platforms.load_platform_dats`` does it for every DAT it loads)."""
    from . import library, totals
    name = getattr(platform, "name", platform)
    games = set(ratings.rated_dats(platform))            # (the DATs of games: a system's updates, add-ons and firmware have no other titles)
    dats = [d for d in dats if not games or getattr(d, "name", "") in games]
    per: dict[str, dict[str, tuple[dict, int]]] = {getattr(d, "name", ""): {} for d in dats}
    for it in totals.target_items(platform, dats):
        if library._style(it) not in STYLES:
            continue
        t = library._tags_of(it)
        d = per.setdefault(it.dat, {})
        cf = t.title.casefold()
        rest = library.combined_key("", tags.game_key(t), True)[2:]     # (the release's kind: status, product tags ...)
        places, n = d.get(cf, ({}, 0))
        if not tags.unlicensed(t) and not any(tags.is_edition(f) for f in t.flags):
            places[rest] = places.get(rest, frozenset()) | _places(t)  # (where the title was released: a re-release or a copy tells nothing)
        d[cf] = (places, n + 1)
    with _LOCK:
        for dat, titles in per.items():
            _DATS[dat] = (name, titles)
            _MAPS.pop(dat, None)


def _places(t: Any) -> frozenset:
    """Where a release is for: its regions; an Amiga (TOSEC) name its countries and languages, and ``-`` for an untagged one (the
    English / European release, no known region)."""
    if t.style in ("nointro", "redump"):
        return frozenset(t.regions)
    out = set(t.regions) | set(() if t.languages_implied else t.languages)
    return frozenset(out or {"-"})


def forget(dat: Optional[str] = None) -> None:
    with _LOCK:
        if dat is None:
            _DATS.clear()
            _MAPS.clear()
        else:
            _DATS.pop(dat, None)
            _MAPS.pop(dat, None)


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
    by_group: dict[str, list[tuple[str, dict, int]]] = {}
    for cf, (places, n) in titles.items():
        nd = node(cf)
        if not nd:
            continue
        by_group.setdefault(tbl.get(nd) or "\x01" + nd, []).append((cf, places, n))
    out: dict[str, str] = {}
    for members in by_group.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (-m[2], m[0]))
        joined = [members[0]]
        for cand in members[1:]:
            if all(not _two_games(cand, other) for other in joined):
                joined.append(cand)
        if len(joined) > 1:
            for cf, _p, _n in joined:
                out[cf] = "\x00" + joined[0][0]
    with _LOCK:
        _MAPS[dat] = (sig, out)
    return out


def _two_games(a: tuple, b: tuple) -> bool:
    """Whether titles ``a`` and ``b`` (``(title, {release kind: places}, releases)``) are two games: a release of the same kind for the
    same region under both titles. Titles spelt alike, or one the other with a subtitle, are one game."""
    na, nb = node(a[0]), node(b[0])
    if na == nb or na.startswith(nb + " ") or nb.startswith(na + " "):
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
