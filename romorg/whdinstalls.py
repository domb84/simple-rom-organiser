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


class Matcher:
    """Which install a game title has (``platform``: whose alternate titles count, e.g. ``"Commodore Amiga"``)."""

    def __init__(self, installs: list[dict[str, Any]], platform: Any = None) -> None:
        from . import alttitles
        self._node = alttitles.node
        self.by: dict[str, list[dict[str, Any]]] = {}
        self.squashed: dict[str, list[dict[str, Any]]] = {}
        for i in installs:
            for name in _plain(i["title"]).split(" / "):     # ("Prince of Persia / Prince de Perse": either title)
                k = self._node(name)
                if k:
                    self.by.setdefault(k, []).append(i)
                    self.squashed.setdefault(k.replace(" ", ""), []).append(i)
        self.groups: dict[str, list[dict[str, Any]]] = {}
        self.table = alttitles.table(platform) if platform is not None else {}
        for k, v in self.by.items():
            g = self.table.get(k)
            if g:
                self.groups.setdefault(g, []).extend(v)

    @staticmethod
    def _best(cands: list[dict[str, Any]], title: str) -> dict[str, Any]:
        """Of several installs of a title, the plain one (not the CD32 / AGA / Arcadia variant) and the closest title."""
        low = title.casefold()
        return min(cands, key=lambda i: (_plain(i["title"]) != i["title"], i["title"].casefold() != low, len(i["title"]), i["archive"]))

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
        if " - " in t:
            head, sub = t.split(" - ", 1)
            if _ADDON_RE.search(sub):
                return None                               # (a data / course / scenario disk: not the game's install)
            ks = self._node(sub)
            if len(ks) > 3 and ks in self.by:
                return self.by[ks], "subtitle", sub
            kh = self._node(head)
            if len(kh) > 3 and kh in self.by and (not strict or (others is not None and kh not in others)):
                return self.by[kh], "main title", head
        words = [] if " & " in t else k.split(" ")             # ("Captain Fizz Meets the Blaster-Trons": the install "Captain Fizz";
        for n in range(len(words) - 1, 1, -1):              #  not "Classic Arcadia & Baby Arcadia", two games on one disk)
            if words[n] in _JOIN_WORDS:                     # (a short title of two words at least)
                head = " ".join(words[:n])
                for key, table in ((head, self.by), (head.replace(" ", ""), self.squashed)):
                    if len(key) > 3 and key in table:
                        return table[key], "short title", t
        return None

    def match(self, title: str, strict: bool = False, others: Optional[set[str]] = None) -> Optional[tuple[dict[str, Any], str]]:
        """``(install, how)`` (``title`` | ``spelling`` | ``other title`` | ``subtitle`` | ``short title`` | ``main title``), or None.
        ``strict``: "Kick Off 2 - KOA Competition" is not the game "Kick Off 2" (what a result recorded for a game must go by; a
        suggestion to download may be looser): the main title counts only when it is no game of its own (``others``)."""
        hit = self._find(title, strict, others)
        return None if hit is None else (self._best(hit[0], hit[2]), hit[1])

    def exact(self, title: str, others: Optional[set[str]] = None) -> list[dict[str, Any]]:
        """Every install of exactly this game (its variants too: the CD32 / AGA install of the same title), never of a longer title
        that starts with it - unless that is only the game's full title (``others``, see :meth:`match`)."""
        hit = self._find(title, True, others)
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
