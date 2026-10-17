"""Library totals (Amendment 17): "with your library rules you would have N of M games".

*M* (the target) is the number of games the library rules keep when EVERY rom of the system's DATs is
present: the system's current :class:`library.LibraryProfile` is applied to the whole DAT with
:func:`library.select`. *N* is the number of those games the user owns in at least one version that passes
the rules: the same ``select`` over the user's matched files (already in memory after a scan, no file is
touched). The unit is a GAME as the library defines it (:func:`library.game_key`), never a file.

Selecting a whole DAT is heavy (Amiga Games ~10 s), so :class:`TotalsManager` runs it on ONE background
worker thread, caches the results in memory, never runs the same job twice and drops results whose key was
superseded while they were computed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import library, tags
from .library import EXCLUDED, INCOMPLETE, KEEP, Item

HAVE, INCOMPLETE_OWNED, EXCLUDED_OWNED = "have", "incomplete", "excluded"


# --------------------------------------------------------------------------- keys

def profile_signature(profile: library.LibraryProfile) -> str:
    """Short stable digest of every rule of a profile (changes whenever a rule / option / language changes)."""
    rules = profile.to_dict()
    rules.pop("saved_games", None)          # (what happens to games you have saves for does not change the totals)
    rules.pop("saved_overrides", None)
    rules.pop("keep_bios", None)           # (nor do BIOS / firmware files: they are no games)
    rules.pop("keep_extras", None)         # (nor the files that sit with a disc)
    rules.pop("convert_first", None)       # (nor what a build converts first)
    rules.pop("delete_converted", None)
    blob = json.dumps(rules, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def signature_text(parts: Iterable[Any]) -> str:
    """Digest of any hashable description (the DAT names / paths / mtimes)."""
    return hashlib.sha1(repr(tuple(parts)).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- items

def target_items(platform: Any, dats: Iterable[Any]) -> list[Item]:
    """One :class:`library.Item` per game-unit of the platform's DATs: a rom (TOSEC / WHDLoad), a set
    (No-Intro: the first of its alternate roms) or a disc (Redump), as if every file was present."""
    from . import discsys

    wanted = set(getattr(platform, "dats", ()) or ())
    items: list[Item] = []
    for dat in dats:
        name = getattr(dat, "name", "")
        if wanted and name not in wanted:
            continue
        if name in tags.REDUMP_DAT_NAMES:
            index = discsys.get_index(dat)
            totals = index.disc_totals()
            for g in index.games.values():
                items.append(Item(key=len(items), dat=name, rom=g.rep, style=tags.STYLE_REDUMP, path=Path(g.name),
                                  member=None, disc_total=totals.get(g.name, 0)))
            continue
        seen: set[str] = set()
        for rom in dat.roms:
            style = tags.style_of_rom(rom)
            if rom.set_name:                  # No-Intro / WHDLoad: alternates share one set
                if rom.set_name in seen:
                    continue
                seen.add(rom.set_name)
            items.append(Item(key=len(items), dat=name, rom=rom, style=style, path=Path(rom.name), member=None))
    return items


def user_items(result: Any) -> list[Item]:
    """The user's matched files as library items (what ``plan_library`` selects over, minus the file moves).

    Several copies of one rom are one item (their decisions are identical); a disc system uses one item
    per game."""
    from . import discsys, organiser

    items: list[Item] = []
    if hasattr(result, "units") and hasattr(result, "index"):       # Redump + CHD (discsys.plan_units)
        index = result.index
        totals = index.disc_totals() if index else {}
        seen_games: set[str] = set()
        for u in result.units:
            if u.kind != "chd" or u.game is None or u.game.name in seen_games:
                continue
            seen_games.add(u.game.name)
            items.append(Item(key=len(items), dat=u.game.rep.dat, rom=u.game.rep, style=tags.STYLE_REDUMP,
                              path=Path(u.path), member=None, disc_total=totals.get(u.game.name, 0)))
        return items
    seen: set[tuple[str, str, str]] = set()
    for u in organiser.matched_units(result):
        sig = (u.dat, u.rom.name, u.form)
        if sig in seen:
            continue
        seen.add(sig)
        items.append(Item(key=len(items), dat=u.dat, rom=u.rom, style=u.style, path=u.path, member=u.member,
                          form=u.form, link=u.link))
    return items


# --------------------------------------------------------------------------- selection results

@dataclass
class Target:
    """The games the rules keep for the WHOLE DAT."""
    games: dict[tuple, frozenset]               # game key -> the rom names kept for it
    by_dat: dict[str, int]
    computed_at: float
    seconds: float = 0.0
    items: int = 0
    incomplete: int = 0                         # games the DAT itself only has in part (no complete set can exist)
    cutoff: Optional[tuple] = None              # rank key of the N-th best game (rating filter, rank_scope "dat")
    rating: Optional[dict] = None               # library.Selection.rating of the target (None = no rating filter)
    coverage: Optional[dict] = None             # library.rating_coverage of the games the other rules keep (None = no ratings installed)
    rated_all: Optional[dict] = None            # {games, rated} of EVERY title of the DAT, whatever the rules say (None = no ratings / not rated)
    rated_target: Optional[dict] = None         # {games, rated} of the titles that stay after ALL the rules, the rating filter too

    @property
    def count(self) -> int:
        return len(self.games)


@dataclass
class UserPart:
    """What the same rules do to the files the user has."""
    games: dict[tuple, tuple[str, frozenset]]   # game key -> (HAVE | INCOMPLETE_OWNED | EXCLUDED_OWNED, kept names)
    computed_at: float
    seconds: float = 0.0
    items: int = 0
    problems: dict[tuple, tuple[str, list[str], str]] = field(default_factory=dict)   # game key -> (state, the names you own, why), of the games not kept


def _kept(items: list[Item], sel: library.Selection) -> dict[tuple, set[str]]:
    out: dict[tuple, set[str]] = {}
    # a disk BORROWED from another edition completes a set of ANOTHER game: it is no ownership of its own game
    borrowed_by = {b["key"]: cs.id for cs in sel.sets for b in cs.borrowed.values()}
    for it in items:
        d = sel.decisions.get(it.key)
        if d is None or d.action != KEEP or d.missing:
            continue
        if library.BORROWED_CODE in d.codes and borrowed_by.get(it.key) == d.set_id:
            continue
        out.setdefault(library.game_key(it), set()).add(library._rom_name(it))
    return out


def _rated(items: list[Item], sel: library.Selection, platform: Any, profile: library.LibraryProfile,
           lookup: Callable[[str], Optional[tuple[float, int]]]) -> Optional[dict[str, int]]:
    """``{games, rated}``: the titles ``sel`` keeps and how many of them have a usable rating (None: this system has no ratings)."""
    cov = library.rating_coverage(items, sel, platform, profile, lookup)
    return {"games": cov["games"], "rated": cov["rated"]} if cov["games"] else None


def compute_target(platform: Any, dats: Iterable[Any], profile: library.LibraryProfile,
                   lookup: Optional[Callable[[str], Optional[tuple[float, int]]]] = None) -> Target:
    """The whole-DAT target. With a rating filter ``lookup(title) -> (rating, votes) | None`` is required; M then counts
    the games kept by ALL rules including the rating filter (``rank_scope`` "dat"; the cutoff of the N-th best game is
    kept for the user part)."""
    t0 = time.time()
    items = target_items(platform, dats)
    cutoff = None
    if profile.rank_scope == "owned" and profile.top_n is not None:
        # "rank only among my games": the cap depends on the user's files, so the target is every game that passes the
        # other rules and the minimum rating (a superset of any owned top N)
        profile = dataclasses.replace(profile, top_n=None, min_rating=profile.min_rating or 0.1)   # (any rated game passes)
    coverage = None
    if profile.rating_active:
        if lookup is None:
            raise library.RatingsUnavailable("ratings are not installed")
        sel = library._select_base(items, profile, platform)
        coverage = library.rating_coverage(items, sel, platform, profile, lookup)
        cutoff = library.target_cutoff(items, profile, platform, lookup, sel)
        library.apply_ratings(sel, items, profile, platform, library.RatingContext(lookup, cutoff))
    else:
        sel = library.select(items, profile, platform)
        if lookup is not None:
            coverage = library.rating_coverage(items, sel, platform, profile, lookup)
    rated_all = rated_target = None
    if lookup is not None:
        everything = dataclasses.replace(
            library.LibraryProfile(exclude=frozenset(), latest_only=False, best_variant=False, complete_only=False, languages=(),
                                   keep_flags=frozenset(tags.KEEP_FLAGS), one_per_game=False, borrow_other_editions=False,
                                   keep_other_language=True), min_votes=profile.min_votes)
        rated_all = _rated(items, library.select(items, everything, platform), platform, profile, lookup)
        rated_target = _rated(items, sel, platform, profile, lookup)
    kept = _kept(items, sel)
    by_dat: dict[str, int] = {}
    for key in kept:
        by_dat[key[0]] = by_dat.get(key[0], 0) + 1
    broken = {library.game_key(it) for it in items
              if (d := sel.decisions.get(it.key)) is not None
              and (d.action == INCOMPLETE or (d.action == KEEP and d.missing))} - set(kept)
    return Target({k: frozenset(v) for k, v in kept.items()}, by_dat, time.time(), time.time() - t0, len(items),
                  len(broken), cutoff, dict(sel.rating) or None, coverage, rated_all, rated_target)


def compute_user(result: Any, platform: Any, profile: library.LibraryProfile,
                 ratings: Optional[library.RatingContext] = None, lang_games: Optional[frozenset] = None) -> UserPart:
    return compute_user_items(user_items(result), platform, profile, ratings, lang_games)


def compute_user_items(items: list[Item], platform: Any, profile: library.LibraryProfile,
                       ratings: Optional[library.RatingContext] = None, lang_games: Optional[frozenset] = None) -> UserPart:
    t0 = time.time()
    sel = library.select(items, profile, platform, ratings=ratings, lang_games=lang_games)
    kept = _kept(items, sel)
    games: dict[tuple, tuple[str, frozenset]] = {}
    problems: dict[tuple, tuple[str, list[str], str]] = {}
    for it in items:
        key = library.game_key(it)
        if key in kept:
            continue
        d = sel.decisions.get(it.key)
        state = INCOMPLETE_OWNED if d is not None and (d.action == INCOMPLETE or (d.action == KEEP and d.missing)) \
            else EXCLUDED_OWNED
        why = ""
        if d is not None:
            why = d.reason or ", ".join(library._RULE_SHORT.get(c, c) for c in d.codes)
        old = problems.get(key)
        if old is None or (state == INCOMPLETE_OWNED and old[0] != INCOMPLETE_OWNED):
            problems[key] = (state, [library._rom_name(it)], why)
        elif old[0] == state:
            problems[key] = (state, sorted({*old[1], library._rom_name(it)}), old[2] or why)
        if games.get(key, ("", frozenset()))[0] != INCOMPLETE_OWNED:
            games[key] = (state, frozenset())
    for key, names in kept.items():
        games[key] = (HAVE, frozenset(names))
    return UserPart(games, time.time(), time.time() - t0, len(items), problems)


def combine(target: Target, user: Optional[UserPart]) -> dict[str, Any]:
    """The numbers of the Overview block from the whole-DAT target and (when scanned) the user's part."""
    out: dict[str, Any] = {"target_games": target.count, "target_incomplete": target.incomplete, "by_dat": {d: {"target": n} for d, n in target.by_dat.items()},
                           "rating": target.rating, "rating_coverage": target.coverage,
                           "rated_all": target.rated_all, "rated_target": target.rated_target}
    if user is None:
        return out
    have = outside = incomplete = excluded = not_preferred = 0
    for key, (state, names) in user.games.items():
        if state == HAVE:
            if key not in target.games:
                outside += 1           # kept by the rules but not a game of the target (a changed DAT, a borrowed disk)
                continue
            have += 1
            row = out["by_dat"].setdefault(key[0], {"target": 0})
            row["have"] = row.get("have", 0) + 1
            if not (names & target.games[key]):
                not_preferred += 1
        elif state == INCOMPLETE_OWNED:
            incomplete += 1
        else:
            excluded += 1
    for row in out["by_dat"].values():
        row.setdefault("have", 0)
    total = target.count
    out.update(have_games=have, missing_games=total - have, percent=round(100.0 * have / total, 1) if total else 0.0,
               not_preferred=not_preferred, owned_but_excluded=excluded, owned_incomplete=incomplete,
               owned_outside_target=outside, owned_games=len(user.games))
    return out


# --------------------------------------------------------------------------- the background worker

@dataclass
class _Want:
    platform: Any
    profile: library.LibraryProfile
    tkey: tuple
    state: Any = None              # the ScanState (``.result``, ``.serial``) or None
    ukey: Optional[tuple] = None


class TotalsManager:
    """Caches targets / user parts per platform and computes them one at a time on a daemon thread."""

    def __init__(self, load_dats: Callable[[Any], list[Any]],
                 persist: Optional[Callable[[str, dict[str, Any]], None]] = None,
                 rating_lookup: Optional[Callable[[Any], Optional[Callable[[str], Any]]]] = None) -> None:
        self._load_dats = load_dats
        self._rating_lookup = rating_lookup      # platform -> title lookup of the installed ratings (None = not installed)
        self._persist = persist
        self._cv = threading.Condition()
        self._want: dict[str, _Want] = {}
        self._targets: dict[str, tuple[tuple, Any]] = {}      # platform -> (key, Target | Exception)
        self._users: dict[str, tuple[tuple, Any]] = {}
        self._insights: dict[str, tuple[tuple, dict[str, Any]]] = {}      # platform -> (key, {tile: numbers})
        self._busy: Optional[tuple] = None
        self._thread: Optional[threading.Thread] = None
        self.discarded = 0           # results dropped because their key was superseded while they ran
        self.target_runs = 0
        self.user_runs = 0

    # ---- public API

    def request(self, platform: Any, profile: library.LibraryProfile, dats_sig: str, state: Any = None,
                persisted: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Register what is wanted, wake the worker if needed and return the answer so far (never blocks)."""
        sig = profile_signature(profile)
        name = platform.name
        tkey = (sig, dats_sig)
        ukey = (getattr(state, "serial", id(state)), sig, dats_sig) if state is not None else None
        with self._cv:
            cached = self._targets.get(name)
            have_target = cached is not None and cached[0] == tkey
            if (state is None and not have_target and persisted
                    and persisted.get("profile_signature") == sig and persisted.get("dats_signature") == dats_sig):
                return self._answer(platform, sig, None, persisted=persisted)       # instant M after a restart
            want = _Want(platform, profile, tkey, state, ukey)
            self._want[name] = want
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="romorg-totals", daemon=True)
                self._thread.start()
            self._cv.notify_all()
            return self._answer(platform, sig, want)

    def upgrades(self, name: str) -> Optional[list[tuple[tuple, list[str], list[str]]]]:
        """``(game key, the names you own, the names the rules prefer)`` of each owned game that is not the preferred version
        (the ``not_preferred`` of the answer); None while the numbers it needs are not ready."""
        with self._cv:
            want, t, u = self._want.get(name), self._targets.get(name), self._users.get(name)
            if want is None or want.ukey is None or t is None or u is None or t[0] != want.tkey or u[0] != want.ukey \
                    or isinstance(t[1], Exception) or isinstance(u[1], Exception):
                return None
            target, user = t[1], u[1]
            rows = []
            for key, (state, names) in user.games.items():
                if state == HAVE and key in target.games and not (names & target.games[key]):
                    rows.append((key, sorted(names), sorted(target.games[key])))
            return rows

    def insights(self, platform: Any, wants: list[str], lookup: Optional[Callable[[str], Any]]) -> Optional[dict[str, Any]]:
        """The numbers of the page tiles that need a calculation of their own (``space``, ``best_missing``, ``avg_rating``, ``regions``),
        from the cached target and, once scanned, your games. None while those are not ready. Each is cached until the rules, the
        databases, the ratings or the scan change."""
        name = platform.name
        with self._cv:
            want, t, u = self._want.get(name), self._targets.get(name), self._users.get(name)
            if want is None or t is None or t[0] != want.tkey or isinstance(t[1], Exception):
                return None
            if want.ukey is not None and (u is None or u[0] != want.ukey or isinstance(u[1], Exception)):
                return None
            target, user = t[1], (u[1] if want.ukey is not None else None)
            key = (want.tkey, want.ukey, lookup is not None)
            cached = self._insights.get(name)
            if cached is None or cached[0] != key:
                cached = self._insights[name] = (key, {})
            done = cached[1]
        todo = [w for w in wants if w not in done]
        if todo:
            done.update(self._compute_insights(platform, todo, target, user, lookup))
        return {w: done[w] for w in wants if w in done}

    def _compute_insights(self, platform: Any, wants: list[str], target: Target, user: Optional[UserPart],
                          lookup: Optional[Callable[[str], Any]]) -> dict[str, Any]:
        roms: dict[tuple[str, str], Any] = {}
        for it in target_items(platform, self._load_dats(platform)):
            roms.setdefault((it.dat, library._rom_name(it)), it.rom)
        owned: dict[tuple, frozenset] = {}
        if user is not None:
            owned = {k: names for k, (st, names) in user.games.items() if st == HAVE and k in target.games}

        def rom_of(key: tuple, names: Any) -> Any:
            for n in sorted(names):
                r = roms.get((key[0], n))
                if r is not None:
                    return r
            return None

        def size(key: tuple, names: Any) -> int:
            r = rom_of(key, names)
            return int(r.size or 0) if r is not None else 0

        def rating(key: tuple, names: Any) -> Optional[tuple[float, int, str]]:
            r = rom_of(key, names)
            if r is None or lookup is None:
                return None
            title = library.rom_title(r, key[0])
            hit = lookup(title)
            return (hit[0], hit[1], title) if hit else None

        out: dict[str, Any] = {}
        if "space" in wants:
            out["space"] = {"target": sum(size(k, n) for k, n in target.games.items()),
                            "have": sum(size(k, n) for k, n in owned.items()) if user is not None else None}
        if "best_missing" in wants:
            # the best EVIDENCED rating: a 10 from five voters does not beat a 9.2 from four hundred (the rating is pulled towards the
            # library's average by a weight of 25 votes, the way film sites rank their lists)
            best = None
            if lookup is not None:
                hits = [(k, h) for k, n in target.games.items() if (h := rating(k, n))]
                mean = sum(h[0] for _, h in hits) / len(hits) if hits else 0.0
                for k, h in hits:
                    if k in owned:
                        continue
                    score = (h[0] * h[1] + mean * 25.0) / (h[1] + 25.0)
                    if best is None or (score, h[1]) > best[0]:
                        best = ((score, h[1]), h)
            out["best_missing"] = {"title": best[1][2], "rating": best[1][0], "votes": best[1][1]} if best else None
        if "avg_rating" in wants:
            mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None      # noqa: E731
            every = [h[0] for k, n in target.games.items() if (h := rating(k, n))] if lookup is not None else []
            mine = [h[0] for k, n in owned.items() if (h := rating(k, n))] if lookup is not None and user is not None else []
            out["avg_rating"] = {"target": mean(every), "owned": mean(mine), "rated_owned": len(mine)}
        if "regions" in wants:
            counts: dict[str, int] = {}
            for k, names in owned.items():
                r = rom_of(k, names)
                regs = tags.of_rom(r).regions if r is not None else ()
                first = regs[0] if regs else "Unknown"
                counts[first] = counts.get(first, 0) + 1
            total = sum(counts.values())
            out["regions"] = [{"region": r, "games": n, "pct": round(100.0 * n / total, 1)}
                              for r, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]] if total else []
        return out

    def target_names(self, name: str) -> Optional[frozenset]:
        """The rom names the rules keep for the whole database (the games a library of this system is meant to have); None while the
        target for the rules now in force is not calculated."""
        with self._cv:
            want, t = self._want.get(name), self._targets.get(name)
            if want is None or t is None or t[0] != want.tkey or isinstance(t[1], Exception):
                return None
            return frozenset(n for names in t[1].games.values() for n in names)

    def name_keys(self, name: str) -> dict:
        """``{rom name: game key}`` of every rom the rules keep (the key the tiles count games by); empty while the target is not ready."""
        with self._cv:
            want, t = self._want.get(name), self._targets.get(name)
            if want is None or t is None or t[0] != want.tkey or isinstance(t[1], Exception):
                return {}
            return {n: key for key, names in t[1].games.items() for n in names}

    def missing_names(self, name: str) -> Optional[frozenset]:
        """The rom names the rules keep for games you have no version of (what "missing" counts); None while the target, or your scan
        of the system, is not calculated for the rules now in force."""
        with self._cv:
            want, t, u = self._want.get(name), self._targets.get(name), self._users.get(name)
            if want is None or t is None or t[0] != want.tkey or isinstance(t[1], Exception):
                return None
            if want.ukey is None or u is None or u[0] != want.ukey or isinstance(u[1], Exception):
                return None
            owned = u[1].games
            return frozenset(n for key, names in t[1].games.items() if (owned.get(key) or (None,))[0] != HAVE for n in names)

    def problems(self, name: str, state: str) -> Optional[list[dict[str, Any]]]:
        """The owned games that are not kept (``state``: EXCLUDED_OWNED or INCOMPLETE_OWNED): title, the names you own, and why."""
        with self._cv:
            want, u = self._want.get(name), self._users.get(name)
            if want is None or want.ukey is None or u is None or u[0] != want.ukey or isinstance(u[1], Exception):
                return None
            rows = [{"title": names[0], "dat": key[0], "have": names, "why": why}
                    for key, (st, names, why) in u[1].problems.items() if st == state]
        rows.sort(key=lambda r: (r["title"].lower(), r["dat"]))
        return rows

    def describe_upgrades(self, platform: Any, rows: list[tuple[tuple, list[str], list[str]]]) -> list[dict[str, Any]]:
        """The rows of :meth:`upgrades` with the DAT's name and, for every preferred version, its files with size and checksums
        (a disc: one file per track; a catalogue without checksums, like Switch and Wii U: none)."""
        from . import discsys

        wanted = {n for _, _, better in rows for n in better}
        files: dict[tuple[str, str], list[Any]] = {}
        reps: dict[tuple[str, str], Any] = {}
        dats = list(self._load_dats(platform))
        for it in target_items(platform, dats):
            n = library._rom_name(it)
            if n in wanted:
                reps.setdefault((it.dat, n), it.rom)
        for dat in dats:
            if getattr(dat, "name", "") in tags.REDUMP_DAT_NAMES:
                for g in discsys.get_index(dat).games.values():
                    if g.name in wanted:
                        files[(dat.name, g.name)] = list(g.tracks)
        out = []
        for key, have, better in rows:
            found = [(n, files.get((key[0], n)) or [reps[(key[0], n)]]) for n in better if (key[0], n) in reps]
            title = library.rom_title(reps[(key[0], found[0][0])], key[0]) if found else have[0]
            out.append({"title": title, "dat": key[0], "have": have,
                        "better": [{"name": n, "files": [{"name": r.name, "size": r.size, "crc": r.crc, "md5": r.md5, "sha1": r.sha1}
                                                         for r in roms]} for n, roms in found]})
        out.sort(key=lambda r: (r["title"].lower(), r["dat"]))
        return out

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until nothing is queued or running (tests)."""
        end = time.time() + timeout
        with self._cv:
            while self._busy is not None or self._next_job() is not None:
                left = end - time.time()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.2))
        return True

    def forget(self, name: str = "") -> None:
        with self._cv:
            for table in (self._want, self._targets, self._users, self._insights):
                if name:
                    table.pop(name, None)
                else:
                    table.clear()

    # ---- answers

    def _answer(self, platform: Any, sig: str, want: Optional[_Want], persisted: Optional[dict[str, Any]] = None
                ) -> dict[str, Any]:
        name = platform.name
        out: dict[str, Any] = {"platform": name, "profile_signature": sig, "calculating": False, "stale": False,
                               "error": "", "computed_at": None, "scanned": bool(want is not None and want.state is not None)}
        if want is None:                                   # persisted counts only
            out.update(target_games=int(persisted.get("target_games", 0)),
                       target_incomplete=int(persisted.get("target_incomplete", 0)), computed_at=persisted.get("computed_at"),
                       by_dat={d: {"target": n} for d, n in (persisted.get("by_dat") or {}).items()},
                       rating=None, rating_coverage=persisted.get("rating_coverage"),
                       rated_all=persisted.get("rated_all"), rated_target=persisted.get("rated_target"),
                       have_games=None, missing_games=None, percent=None, not_preferred=None,
                       owned_but_excluded=None, owned_incomplete=None, owned_games=None)
            return out
        t = self._targets.get(name)
        u = self._users.get(name)
        t_ok = t is not None and t[0] == want.tkey
        u_ok = want.ukey is None or (u is not None and u[0] == want.ukey)
        for entry in (t if t_ok else None, u if (u_ok and want.ukey is not None) else None):
            if entry is not None and isinstance(entry[1], Exception):
                out.update(error=str(entry[1]) or type(entry[1]).__name__)
        out.update(target_games=None, have_games=None, missing_games=None, percent=None, not_preferred=None,
                   owned_but_excluded=None, owned_incomplete=None, owned_games=None, by_dat={})
        if t_ok and not isinstance(t[1], Exception) and u_ok and (want.ukey is None or not isinstance(u[1], Exception)):
            out.update(combine(t[1], u[1] if want.ukey is not None else None))
            out["computed_at"] = t[1].computed_at if want.ukey is None else max(t[1].computed_at, u[1].computed_at)
            return out
        if out["error"]:
            return out
        out["calculating"] = True
        # the previous numbers (other rules / folder) stay visible, flagged stale, until the new ones are ready
        if t is not None and not isinstance(t[1], Exception) and (want.ukey is None or (u is not None and not isinstance(u[1], Exception))):
            out.update(combine(t[1], u[1] if want.ukey is not None and u is not None else None))
            out["stale"] = True
            out["computed_at"] = t[1].computed_at
        return out

    # ---- worker

    def _needs(self, name: str, want: _Want) -> Optional[tuple]:
        t = self._targets.get(name)
        if t is None or t[0] != want.tkey:
            return ("target", name, want.tkey)
        if want.ukey is not None:
            u = self._users.get(name)
            if u is None or u[0] != want.ukey:
                return ("user", name, want.ukey)
        return None

    def _lang_games(self, want: _Want) -> Optional[frozenset]:
        """Games of the whole DAT that have a version in the selected languages (``keep_other_language``); None when the
        rule is off."""
        p = want.profile
        if not (p.keep_other_language and p.languages):
            return None
        return library.language_games(target_items(want.platform, self._load_dats(want.platform)), p, want.platform)

    def _next_job(self) -> Optional[tuple]:
        for name, want in list(self._want.items()):
            job = self._needs(name, want)
            if job is not None:
                return job
        return None

    def _loop(self) -> None:
        while True:
            with self._cv:
                job = self._next_job()
                while job is None:
                    self._cv.wait()
                    job = self._next_job()
                self._busy = job
                kind, name, key = job
                want = self._want[name]
            value: Any
            try:
                lookup = None
                if self._rating_lookup is not None:
                    lookup = self._rating_lookup(want.platform)
                if kind == "target":
                    args = (want.platform, self._load_dats(want.platform), want.profile)
                    value = compute_target(*args, lookup) if lookup is not None else compute_target(*args)
                else:
                    ctx = None
                    if want.profile.rating_active:
                        if lookup is None:
                            raise library.RatingsUnavailable("ratings are not installed")
                        with self._cv:
                            t = self._targets.get(name)
                        cut = t[1].cutoff if t is not None and t[0] == want.tkey and not isinstance(t[1], Exception) else None
                        if want.profile.rank_scope == "dat" and want.profile.top_n is not None and cut is None:
                            raise library.RatingsUnavailable("the target is not ready")
                        ctx = library.RatingContext(lookup, cut)
                    args = (want.state.result, want.platform, want.profile)
                    lg = self._lang_games(want)
                    value = compute_user(*args, ctx, lg)
            except Exception as exc:  # noqa: BLE001 - reported in the answer, cached so it is not retried in a loop
                traceback.print_exc()
                value = exc
            record = None
            with self._cv:
                cur = self._want.get(name)
                table = self._targets if kind == "target" else self._users
                if kind == "target":
                    self.target_runs += 1
                else:
                    self.user_runs += 1
                if cur is not None and (cur.tkey if kind == "target" else cur.ukey) == key:
                    table[name] = (key, value)
                    if kind == "target" and not isinstance(value, Exception):
                        record = {"target_games": value.count, "target_incomplete": value.incomplete, "by_dat": dict(value.by_dat),
                                  "profile_signature": key[0], "dats_signature": key[1],
                                  "computed_at": value.computed_at}
                        if value.coverage is not None:
                            record["rating_coverage"] = value.coverage
                        for field in ("rated_all", "rated_target"):
                            if getattr(value, field) is not None:
                                record[field] = getattr(value, field)
                else:
                    self.discarded += 1
                self._busy = None
                self._cv.notify_all()
            if record is not None and self._persist is not None:
                try:
                    self._persist(name, record)
                except Exception:  # noqa: BLE001 - a convenience only
                    traceback.print_exc()
