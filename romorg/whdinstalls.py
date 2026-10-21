"""WHDLoad installs for the Amiga games you have: the list of installs on whdload.de, which of your games has one, and the download.

A WHDLoad *install* is a small package (a ``.slave``, an ``Install`` script, icons and a ReadMe - some hundred KB, no game data) that
installs a game from its disks to a hard drive. The list of every game install (``games/all.html``: title, archive, size, date, Hall of
Light id) is fetched and kept for a week like the other databases (``check_update`` / ``download_and_build`` / ``db_info``, driven by
:mod:`romorg.autoupdate`). Only install packages are ever downloaded - never the pre-installed packs, which hold the game itself.

A game is matched to an install by its title (:class:`Matcher`): the same title, the same letters (``Push-Over`` / ``Pushover``),
another title of the game (``Die Siedler`` / ``The Settlers``, :mod:`romorg.alttitles`), or its subtitle / main title (``Cadaver - The
Last Supper``); a data, course or scenario disk is never matched to the game's install. :func:`download_install` unpacks an install into
``<title> (WHDLoad install)/`` next to the game's disks, with a :data:`MARKER` file the scanner leaves alone.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from . import meter
from .folders import WHDLOAD_MARKER

BASE_URL = "https://www.whdload.de/games/"
CATALOGUE_URL = BASE_URL + "all.html"
USER_AGENT = "simple-rom-organiser"
MAX_AGE_DAYS = 7
MARKER = WHDLOAD_MARKER                  # in a downloaded install's folder: what it is (the scanner skips such a folder)
FOLDER_SUFFIX = " (WHDLoad install)"
ProgressFn = Callable[[int, int, str], None]

__all__ = ["CATALOGUE_URL", "MARKER", "parse_catalogue", "load", "db_info", "check_update", "download_and_build", "Matcher",
           "download_install", "installed_in", "package", "WhdInstallError"]


class WhdInstallError(Exception):
    """An install could not be downloaded or is not an install archive."""


def db_path() -> Path:
    from .paths import data_dir
    return data_dir() / "whdinstalls" / "installs.json"


# --------------------------------------------------------------------------- the list

_ROW_RE = re.compile(r'<TR><TD ALIGN=CENTER><A HREF="([^"/]+\.lha)">(.*?)</A></TD>(.*?)</TR>', re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<TD[^>]*>(.*?)</TD>", re.IGNORECASE | re.DOTALL)


def parse_catalogue(text: str) -> list[dict[str, Any]]:
    """The installs of whdload.de's ``games/all.html``: ``{archive, title, size, date, hol, lemon}``."""
    out = []
    for m in _ROW_RE.finditer(text):
        archive, title, rest = m.group(1), _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(), m.group(3)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in _CELL_RE.findall(rest)]
        size = next((int(c) for c in cells if c.isdigit()), 0)
        date = next((c for c in cells if re.fullmatch(r"\d{4}-\d\d-\d\d", c)), "")
        hol = re.search(r"hol\.abime\.net/(\d+)", rest)
        lemon = re.search(r"lemonamiga\.com/games/details\.php\?id=(\d+)", rest)
        out.append({"archive": archive, "title": title, "size": size, "date": date,
                    "hol": hol.group(1) if hol else "", "lemon": lemon.group(1) if lemon else ""})
    return out


_LOCK = threading.Lock()
_CACHE: dict[str, tuple[Any, Optional[dict[str, Any]]]] = {}


def load(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """``{fetched, fetched_at, installs}`` of the kept list, or None (not fetched yet)."""
    p = Path(path) if path else db_path()
    try:
        st = p.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _CACHE.get(str(p))
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data.get("installs"), list):
            data = None
    except (OSError, ValueError, AttributeError):
        data = None
    with _LOCK:
        _CACHE[str(p)] = (sig, data)
    return data


def db_info(path: Optional[Path] = None) -> dict[str, Any]:
    data = load(path)
    if data is None:
        return {"available": False, "titles": 0, "fetched": "", "fetched_at": 0.0}
    return {"available": True, "titles": len(data["installs"]), "fetched": data.get("fetched", ""),
            "fetched_at": float(data.get("fetched_at") or 0)}


def check_update(timeout: float = 10, gate: bool = True, path: Optional[Path] = None, now: Optional[float] = None) -> dict[str, Any]:
    """``missing`` (fetch), ``up_to_date`` (younger than :data:`MAX_AGE_DAYS`), else ``update_available``."""
    info = db_info(path)
    if not info["available"]:
        return {"status": "missing"}
    if gate and (now if now is not None else time.time()) - info["fetched_at"] < MAX_AGE_DAYS * 86400:
        return {"status": "up_to_date", "skipped": True}
    return {"status": "update_available"}


def _fetch(url: str, timeout: float = 60, opener: Optional[Callable[[Any], Any]] = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    resp = opener(req) if opener else urllib.request.urlopen(req, timeout=timeout)
    try:
        data = resp.read()
    finally:
        resp.close()
    meter.add(len(data))
    return data


def download_and_build(progress: Optional[ProgressFn] = None, cancel: Any = None, out: Optional[Path] = None,
                       opener: Optional[Callable[[Any], Any]] = None) -> dict[str, int]:
    """Fetch the list of installs and keep it (atomic replace)."""
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise InterruptedError("cancelled")
    if progress:
        progress(0, 0, "WHDLoad installs: fetching the list")
    text = _fetch(CATALOGUE_URL, opener=opener).decode("utf-8", "replace")
    installs = parse_catalogue(text)
    if len(installs) < 100:
        raise WhdInstallError("the list of WHDLoad installs could not be read (the page has changed?)")
    out = Path(out) if out else db_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    data = {"source": CATALOGUE_URL, "fetched": time.strftime("%Y-%m-%d", time.gmtime(now)), "fetched_at": now, "installs": installs}
    fd, tmp = tempfile.mkstemp(prefix=".installs-", dir=out.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, out)
    if progress:
        progress(1, 1, f"WHDLoad installs: {len(installs)} games")
    return {"installs": len(installs)}


# --------------------------------------------------------------------------- matching a game to its install

_SUFFIX_RE = re.compile(r"\s*(?:\((?:[^)]*)\)|-\s*(?:Arcadia|AGA|ECS|OCS|CD32|CDTV)|CD³²|CD32|AGA|CDTV)\s*$", re.IGNORECASE)
# a subtitle that makes a disk of the game something else: data, course, scenario, mission disks, editors ...
_ADDON_RE = re.compile(r"\b(?:disk|data|course|courses|scenario|scenarios|editor|maps?|levels?|missions?|expansion|construction|"
                       r"add-?on|multi-edit|tour)\b", re.IGNORECASE)


def _plain(title: str) -> str:
    """A title without the install list's variant suffixes ("(AGA)", "CD³²", "- Arcadia")."""
    for _ in range(3):
        new = _SUFFIX_RE.sub("", title)
        if new == title:
            break
        title = new
    return title


_JOIN_WORDS = {"meets", "in", "and", "versus", "vs"}   # (what joins a game's short title to the rest of a long one)
_ARTICLES = {"the", "a", "an"}
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "æ": "ae", "Æ": "Ae",
                          "ø": "oe", "Ø": "Oe", "å": "a", "Å": "A"})
# an edition's mark at the end of a title: the season or year ("'95-'96", "'93", "93", "88"; not "2000" - SimCity 2000 is a game
# of its own - nor "01", Dylan Dog 01 is), "Deluxe"; the install is the game's
_EDITION_RE = re.compile(r"(?:\s+(?:'\d{2}|[89]\d)(?:\s*[-/]\s*'?\d{2})?|\s+Deluxe)$", re.IGNORECASE)
_EDITION_SUB_RE = re.compile(r"\b(?:edition|version)$", re.IGNORECASE)
# renamed releases no rule finds (TOSEC's title -> the install's)
_ALIASES = {"fifa international soccer": "fifa soccer",
            "populous 2 trials of the olympian gods": "populous 2 challenge games",   # (that install is Populous 2 and its data disk)
            "lotus turbo challenge 2": "lotus 2",
            "lemmings xmas lemmings 1991": "lemmings x mas 1991", "lemmings xmas lemmings 1992": "lemmings x mas 1992",
            "lemmings holiday lemmings 1993": "lemmings holiday 1993 1994", "lemmings holiday lemmings 1994": "lemmings holiday 1993 1994"}
# games whose title is an install's but not its game (title -> the install that is another game's): Grandslam's Hunt for Red October
# of 1987 (German: Jagd auf Roter Oktober) is not the 1990 game of the film, the only one with an install
_NOT_ITS_INSTALL = {"hunt for red october": {"HuntForRedOctober.lha"}, "jagd auf roter oktober": {"HuntForRedOctober.lha"},
                    "hanse die expedition": {"Hanse.lha"},                       # (1994; the install is Ariolasoft's Hanse of 1988)
                    "d day": {"DDay.lha"},                                       # (the install is Impressions' D-Day: The Beginning of the End)
                    "gettysburg turning point": {"Gettysburg.lha"},              # (SSI's of 1986; the install is ARC's Gettysburg of 1990)
                    "space harrier return to the fantasy zone": {"SpaceHarrier.lha", "LiveAndLetDie.lha"},   # (another game)
                    "trivial pursuit": {"TrivialPursuit2.lha"}}                  # (Domark's of 1986; the install is A New Beginning, 1988)
_DEMO_RE = re.compile(r"\b(?:demo|preview)\b", re.IGNORECASE)
_NICKNAME_RE = re.compile(r"\s+['\"][A-Za-z][^'\"]{1,20}['\"](?=\s)")    # (a nickname in quotes: "Ivan 'Ironman' Stewart")
_POSSESSIVE_RE = re.compile(r"(?<=[a-z])'s\b", re.IGNORECASE)
_MINOR_WORDS = {"the", "a", "an", "of", "in", "for", "and", "to", "on", "der", "die", "das", "des", "le", "la", "les", "de", "el", "il"}
# a main title that is a data / scenario disk's ("Dreadnoughts Data Disk - Bismarck": not the game Bismarck)
_ADDON_HEAD_RE = re.compile(r"\b(?:data|scenario|mission|course|level)s?\s*disks?\b", re.IGNORECASE)
# a main title with a number of its own ("Xenon 2", "Zork III", "Mean 18", "Time Runners 03"): it names the game, the subtitle is
# only a subtitle - never the install of another game of that name ("Megablast", "The Dungeon Master", "Ultimate Golf")
_NUMBERED_RE = re.compile(r"(?:\s|^)(?:\d+|[IVX]+)$")
_NUMERAL_RE = re.compile(r"\s+(?:\d+|[IVXivx]+)$")


def _abbreviates(head: str, sub: str) -> bool:
    """The main title is the subtitle's initials: "A.M.C. - Astro Marine Corps", "ATF II - Advanced Tactical Fighter II",
    "F.O.F.T. - Federation of Free Traders"."""
    h = re.sub(r"[^a-z]", "", _NUMERAL_RE.sub("", head).casefold())
    words = re.findall(r"[a-z0-9]+", _NUMERAL_RE.sub("", sub).casefold())
    return len(h) >= 2 and h in ("".join(w[0] for w in words), "".join(w[0] for w in words if w not in _MINOR_WORDS))


class Matcher:
    """Which install a game title has (``platform``: whose alternate titles count, e.g. ``"Commodore Amiga"``)."""

    def __init__(self, installs: list[dict[str, Any]], platform: Any = None) -> None:
        from . import alttitles
        self._node = alttitles.node
        self.by: dict[str, list[dict[str, Any]]] = {}
        self.squashed: dict[str, list[dict[str, Any]]] = {}
        self.loose: dict[str, list[dict[str, Any]]] = {}      # without articles and spaces, umlauts both ways
        self.bag: dict[str, list[dict[str, Any]]] = {}        # its words in any order ("Lemmings - Oh! No More": "Oh No! More Lemmings")
        self.parts: dict[str, list[tuple[dict[str, Any], str]]] = {}   # an install's main title ("X: ...") or its long title's end
                                                              # ("... In X"), with what follows the main title
        for i in installs:
            for name in _plain(i["title"]).split(" / "):     # ("Prince of Persia / Prince de Perse": either title)
                k = self._node(name)
                if k:
                    self.by.setdefault(k, []).append(i)
                    self.squashed.setdefault(k.replace(" ", ""), []).append(i)
                    if len(k.split(" ")) >= 3:
                        self.bag.setdefault(" ".join(sorted(k.split(" "))), []).append(i)
                    for v in {k, self._node(name.translate(_UMLAUTS))}:
                        if v:
                            self.loose.setdefault(self._loose(v), []).append(i)
                    if ": " in name:                         # ("Centerbase: Science-Fiction Simulation" is Centerbase; not
                        head, sub = name.split(": ", 1)       #  "Football Manager: World Cup Edition", an edition of its own)
                        if not _EDITION_SUB_RE.search(sub):
                            self._part(head, i, sub)
                    m = re.search(r"\s(?:in|meets)\s(.+)$", name, re.IGNORECASE)
                    if m and len(self._node(m.group(1)).split()) >= 3:     # ("Roger Rabbit In Hare Raising Havoc")
                        self._part(m.group(1), i, "")
        self.byhol: dict[str, list[dict[str, Any]]] = {}       # the installs by their Hall of Light page
        for i in installs:
            if str(i.get("hol") or "").isdigit():
                self.byhol.setdefault(str(i["hol"]), []).append(i)
        self.groups: dict[str, list[dict[str, Any]]] = {}
        self.table = alttitles.table(platform) if platform is not None else {}
        for k, v in self.by.items():
            g = self.table.get(k)
            if g:
                self.groups.setdefault(g, []).extend(v)

    @staticmethod
    def _loose(k: str) -> str:
        return "".join(w for w in k.split(" ") if w not in _ARTICLES)

    def _part(self, name: str, install: dict[str, Any], sub: str) -> None:
        k = self._node(name)
        if len(k) > 3 and k not in self.by:                  # (never over an install of that very title)
            self.parts.setdefault(k, []).append((install, sub))

    def _words(self, t: str) -> set[str]:
        return {w for w in self._node(t.translate(_UMLAUTS)).split(" ") if len(w) > 2 and w not in _MINOR_WORDS}

    def _in_parts(self, k: str, sub: str = "") -> Optional[list[dict[str, Any]]]:
        """The installs whose main title is ``k``; for a title with a subtitle of its own only those whose subtitle shares a word
        with it ("Ultima IV - Quest of the Avatar": "Ultima IV: Quest for the Avatar"; "Conflict - Korea" is not "Conflict: Europe")."""
        got = [i for i, isub in self.parts.get(k, ()) if not sub or not isub or self._words(sub) & self._words(isub)]
        return got or None

    def _variants(self, t: str) -> list[tuple[str, str]]:
        """Other forms of a TOSEC title that are the same game for an install: the edition's mark off ("Sensible World of Soccer
        '95-'96", "Gloom Deluxe", "Project-X - Special Edition 93"), a "1" before the subtitle ("Leisure Suit Larry 1 - In the Land
        of ..."), the title after an ellipsis ("Pierre le Chef is... Out to Lunch"), the alias of a renamed release."""
        out = []
        head, sep, sub = t.partition(" - ")
        for _ in range(2):
            new = _EDITION_RE.sub("", head) if not sep else head + sep + _EDITION_RE.sub("", sub)
            if new == (head + sep + sub) and sep:
                new = _EDITION_RE.sub("", head) + sep + sub
            if new and new != head + sep + sub:
                out.append((new, "edition"))
                head, sep, sub = new.partition(" - ")
            else:
                break
        if sep and re.search(r"\s1$", head):
            out.append((head[:-2] + sep + sub, "first of a series"))
            out.append((head[:-2], "first of a series"))
        if "... " in t:
            out.append((t.split("... ", 1)[1], "subtitle"))
        a = _ALIASES.get(self._node(t))
        if a:
            out.append((a, "other title"))
        return out

    @staticmethod
    def _last_variants(t: str) -> list[str]:
        """"Ivan 'Ironman' Stewart's Super Off Road": "Ivan Stewart's Super Off Road"; "Archer Maclean's Pool": "Archer Maclean Pool"."""
        nick = _NICKNAME_RE.sub("", t)
        out = [nick] if nick != t else []
        own = _POSSESSIVE_RE.sub("", nick)
        if own != nick:
            out.append(own)
        return out

    def _same_game(self, title: str, installs: list[dict[str, Any]]) -> bool:
        """An install of ``installs`` has the Hall of Light page OpenRetro gives the game of ``title`` (no OpenRetro data: False)."""
        hols = self._hols().get(self._node(title), ())
        return any(str(i.get("hol") or "") in hols for i in installs)

    def _hols(self) -> dict[str, set[str]]:
        """OpenRetro's games by title (``node``): their Hall of Light page numbers - fetched with the other databases."""
        if getattr(self, "_hol_index", None) is None:
            index: dict[str, set[str]] = {}
            try:
                from . import openretro
                odb = openretro.database()
            except Exception:  # noqa: BLE001 - no OpenRetro data: no tie-break
                odb = None
            if odb is not None:
                for name, gid in odb.titles():
                    m = re.search(r"(\d+)\s*$", str((odb.games.get(gid) or {}).get("hol") or ""))
                    if m:
                        index.setdefault(self._node(name), set()).add(m.group(1))
            self._hol_index = index
        return self._hol_index

    @staticmethod
    def _best(cands: list[dict[str, Any]], title: str) -> dict[str, Any]:
        """Of several installs of a title, the plain one (not the CD32 / AGA / Arcadia variant) and the closest title."""
        low = title.casefold()
        return min(cands, key=lambda i: (_plain(i["title"]) != i["title"], i["title"].casefold() != low, len(i["title"]), i["archive"]))

    def _found(self, title: str, strict: bool, others: Optional[set[str]]) -> Optional[tuple[list[dict[str, Any]], str, str]]:
        """:meth:`_find`, first by the game's Hall of Light page (OpenRetro's, for this title: one game's page, an install that has
        it - no demo install for a game; "Indiana Jones and the Last Crusade - The Action Game" is not the adventure's install),
        never an install that is another game's (``_NOT_ITS_INSTALL``), nor an Arcadia board's for a title that is not, nor a demo's or
        a preview's for a game ("Zap (Preview)" is no install of Zap) - whichever way it is found."""
        t = _plain(title or "")
        k = self._node(t)
        arcadia, demo = "arcadia" in (title or "").casefold(), bool(_DEMO_RE.search(title or ""))

        def its(i: dict[str, Any]) -> bool:                # (an Arcadia install wants the arcade board's ROMs, not the floppies)
            return (i["archive"] not in _NOT_ITS_INSTALL.get(k, ()) and (arcadia or "arcadia" not in i["title"].casefold())
                    and (demo or not _DEMO_RE.search(i["title"])))
        hols = self._hols().get(k) or set()
        cands = [i for h in sorted(hols) for i in self.byhol.get(h, ()) if its(i)]
        if cands and len({str(i["hol"]) for i in cands}) == 1:
            return cands, "Hall of Light", t
        hit = self._find(title, strict, others)
        if hit is not None:
            keep = [i for i in hit[0] if its(i)]
            hit = (keep, hit[1], hit[2]) if keep else None
        return hit

    def _find(self, title: str, strict: bool, others: Optional[set[str]] = None) -> Optional[tuple[list[dict[str, Any]], str, str]]:
        """``(every install of it, how, the title it was found by)``. ``strict``: the main title of a longer one only when that main
        title is no game of its own (``others``: the nodes of every game's title): "Alien Breed II - The Horror Continues" is the
        game "Alien Breed II", "Kick Off 2 - KOA Competition" is an edition of the game "Kick Off 2"."""
        t = _plain(title or "")
        k = self._node(t)
        if not k:
            return None
        if k in self.by:
            return self.by[k], "title", t
        if k.replace(" ", "") in self.squashed:
            return self.squashed[k.replace(" ", "")], "spelling", t
        g = self.table.get(k)
        if g and g in self.groups:
            return self.groups[g], "other title", t
        if self._loose(k) in self.loose:                    # ("Batman - The Movie": "Batman the Movie"; "Matthaeus": "Matthäus")
            return self.loose[self._loose(k)], "spelling", t
        for v, how in self._variants(t):
            hit = self._find(v, strict, others)
            if hit is not None:
                return hit[0], how, hit[2]
        compilation = " & " in t                            # (two games on one disk: no main title is either of them)
        hit = None if compilation else self._in_parts(k)
        if hit:
            return hit, "main title", t
        if " - " in t and not compilation:
            head, sub = t.split(" - ", 1)
            if _ADDON_RE.search(sub) or _ADDON_HEAD_RE.search(head):
                return None                               # (a data / course / scenario disk: not the game's install)
            ks, kh = self._node(sub), self._node(head)
            by_sub = self.by.get(ks) if len(ks) > 3 else None
            by_head = None
            own = not strict or (others is not None and kh not in others) or bool(_EDITION_SUB_RE.search(sub))
            if len(kh) > 3 and own:                          # (an "... Edition" is the game's own: its install is the game's)
                for key, table in ((kh, self.by), (self._loose(kh), self.loose),
                                   (self._loose(self._node(head.translate(_UMLAUTS))), self.loose)):
                    if key in table:
                        by_head = table[key]
                        break
                else:
                    by_head = self._in_parts(kh, sub)
            if by_sub and _abbreviates(head, sub):
                return by_sub, "subtitle", sub
            if by_sub and by_head:                           # (both a game: "Star Wars - Return of the Jedi" is the install Return of
                if self._same_game(t, by_sub) and not self._same_game(t, by_head):   # the Jedi, "Xenon 2 - Megablast" is Xenon 2 -
                    return by_sub, "subtitle", sub           #  OpenRetro's Hall of Light page of the game decides; none: the main title)
                return by_head, "main title", head
            if by_sub and not _NUMBERED_RE.search(head) and len([w for w in ks.split(" ") if w not in _ARTICLES]) >= 2:
                return by_sub, "subtitle", sub               # (another title of the game: "Legend of Robin Hood - Conquests of the
            if by_head:                                      #  Longbow"; not "Sir Fred - The Legend", nor "Zork III - The Dungeon Master")
                return by_head, "main title", head
        for v in self._last_variants(t):                     # (a nickname or a possessive off: only the very title, spelled
            kv = self._node(v)                                #  otherwise - "Hero's Quest" is not the install "HeroQuest")
            for key, table in ((kv, self.by), (kv.replace(" ", ""), self.squashed), (self._loose(kv), self.loose)):
                if key in table and (v != _POSSESSIVE_RE.sub("", t) or table is self.by):
                    return table[key], "spelling", v
        for v in [t] + self._last_variants(t):
            kv = self._node(v)
            bag = self.bag.get(" ".join(sorted(kv.split(" ")))) if len(kv.split(" ")) >= 3 else None
            if bag and len({i["archive"] for i in bag}) == 1:     # (the same words in another order, one install has them:
                return bag, "spelling", v                          #  "Aventura Espacial, La", "Lemmings - Oh! No More")
        words = [] if " & " in t else k.split(" ")             # ("Captain Fizz Meets the Blaster-Trons": the install "Captain Fizz";
        for n in range(len(words) - 1, 1, -1):              #  not "Classic Arcadia & Baby Arcadia", two games on one disk)
            if words[n] in _JOIN_WORDS:                     # (a short title of two words at least)
                head = " ".join(words[:n])
                for key, table in ((head, self.by), (head.replace(" ", ""), self.squashed)):
                    if len(key) > 3 and key in table:
                        return table[key], "short title", t
        return None

    def match(self, title: str, strict: bool = False, others: Optional[set[str]] = None) -> Optional[tuple[dict[str, Any], str]]:
        """``(install, how)`` (``Hall of Light`` | ``title`` | ``spelling`` | ``other title`` | ``subtitle`` | ``short title`` |
        ``main title``), or None.
        ``strict``: "Kick Off 2 - KOA Competition" is not the game "Kick Off 2" (what a result recorded for a game must go by; a
        suggestion to download may be looser): the main title counts only when it is no game of its own (``others``)."""
        hit = self._found(title, strict, others)
        return None if hit is None else (self._best(hit[0], hit[2]), hit[1])

    def exact(self, title: str, others: Optional[set[str]] = None) -> list[dict[str, Any]]:
        """Every install of exactly this game (its variants too: the CD32 / AGA install of the same title), never of a longer title
        that starts with it - unless that is only the game's full title (``others``, see :meth:`match`)."""
        hit = self._found(title, True, others)
        return [] if hit is None else list(hit[0])


# --------------------------------------------------------------------------- download

_BAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def folder_name(install: dict[str, Any]) -> str:
    return (_BAD_NAME_RE.sub("", install["title"]).strip(" .") or install["archive"][:-4]) + FOLDER_SUFFIX


def _safe_path(root: Path, name: str) -> Path:
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or name.startswith("/"):
        raise WhdInstallError(f"unsafe path in the install: {name}")
    return root.joinpath(*parts)


def download_install(install: dict[str, Any], dest_dir: Path, game: str = "", opener: Optional[Callable[[Any], Any]] = None,
                     cancel: Any = None) -> Path:
    """Download ``install`` and unpack it into ``dest_dir / folder_name(install)`` (replacing an older download of it). The archive is
    checked first (an LhA archive, every file's CRC) - a page of the site instead of the file is never unpacked. Returns the folder."""
    from .lha import LhaError, LhaFile
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise InterruptedError("cancelled")
    url = BASE_URL + install["archive"]
    data = _fetch(url, opener=opener)
    try:
        arc = LhaFile(data)
        files = [(e.name, arc.read(e)) for e in arc.entries if not e.is_dir]
    except LhaError as exc:
        raise WhdInstallError(f"{install['archive']} is not a readable install archive ({exc})") from None
    if not files:
        raise WhdInstallError(f"{install['archive']} is empty")
    dest_dir = Path(dest_dir)
    final = dest_dir / folder_name(install)
    work = Path(tempfile.mkdtemp(prefix=".romorg-whd-", dir=dest_dir))
    try:
        for name, content in files:
            p = _safe_path(work, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        (work / MARKER).write_text(json.dumps({"archive": install["archive"], "title": install["title"], "url": url, "game": game,
                                               "date": install.get("date", ""), "downloaded_at": time.time()}, indent=1), encoding="utf-8")
        if final.exists():
            import shutil
            shutil.rmtree(final)
        os.replace(work, final)
    except BaseException:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        raise
    return final


def package(install: dict[str, Any], opener: Optional[Callable[[Any], Any]] = None) -> tuple[bytes, bool]:
    """``(the install's .lha, downloaded now)``: from the cache (``whdinstalls/packages/``) or whdload.de (checked: an LhA archive)."""
    from .lha import LhaError, LhaFile
    cache = db_path().parent / "packages" / install["archive"]
    try:
        data = cache.read_bytes()
        LhaFile(data)
        return data, False
    except (OSError, LhaError):
        pass
    data = _fetch(BASE_URL + install["archive"], opener=opener)
    try:
        LhaFile(data)
    except LhaError as exc:
        raise WhdInstallError(f"{install['archive']} is not a readable install archive ({exc})") from None
    cache.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pkg-", dir=cache.parent)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, cache)
    return data, True


XFD_URL = "https://aminet.net/util/pack/xfdmaster.lha"


def xfd_package(opener: Optional[Callable[[Any], Any]] = None) -> bytes:
    """The XFD package (xfdmaster.library and its decrunchers, for installs that run xfdDecrunch): from the cache
    (``whdinstalls/xfdmaster.lha``) or Aminet, once. It is "freely distributable for noncommercial use", kept whole and as it is."""
    from .lha import LhaError, LhaFile
    cache = db_path().parent / "xfdmaster.lha"
    def good(data: bytes) -> bool:
        try:
            return any(e.name.replace("\\", "/").lower().endswith("libs/xfdmaster.library") for e in LhaFile(data).entries)
        except LhaError:
            return False
    try:
        data = cache.read_bytes()
        if good(data):
            return data
    except OSError:
        pass
    data = _fetch(XFD_URL, opener=opener)
    if not good(data):
        raise WhdInstallError("the XFD package from Aminet is not what it should be (no xfdmaster.library in it)")
    cache.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".xfd-", dir=cache.parent)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, cache)
    return data


def installed_in(folder: Path) -> dict[str, dict[str, Any]]:
    """``{archive: marker data + "folder"}`` of the installs downloaded into ``folder`` (its direct subfolders)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        subs = [p for p in Path(folder).iterdir() if p.is_dir()]
    except OSError:
        return out
    for d in subs:
        try:
            data = json.loads((d / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("archive"):
            out[data["archive"]] = {**data, "folder": str(d)}
    return out
