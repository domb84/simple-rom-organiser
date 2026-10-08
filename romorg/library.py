"""The "Build library" selection: which local files to keep, exclude, supersede or set aside.

Pure logic, no I/O. ``select`` takes the local files that survived duplicate removal (as
:class:`Item`) and a :class:`LibraryProfile` and decides, per file, ``keep`` / ``excluded`` /
``superseded`` / ``incomplete``; kept multi-disk sets come back as :class:`ChosenSet` (one
playlist each). The result depends only on the ROM names (never on where a file lives), so
running it on its own kept output changes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Optional, Sequence

from . import tags
from .tags import STYLE_NOINTRO, STYLE_REDUMP, STYLE_TOSEC, STYLE_WHDLOAD

if TYPE_CHECKING:
    from .datfile import Rom
    from .platforms import Platform

RULES = tags.RULES

KEEP, EXCLUDED, SUPERSEDED, INCOMPLETE = "keep", "excluded", "superseded", "incomplete"

# Reason codes besides the exclusion rules (``RULES``): ``language`` and ``flag_<x>`` for every
# ``tags.KEEP_FLAGS`` entry. ``Decision.codes`` of an excluded item lists every code that applies.
LANGUAGE_CODE = "language"
FLAG_CODES = tuple(f"flag_{f}" for f in tags.KEEP_FLAGS)
# Rating filter codes (Amendment 18): applied after every other rule, at GAME level.
RATING_LOW, RATING_NOT_TOP, RATING_UNRATED = "rating_low", "rating_not_top", "rating_unrated"
RATING_CODES = (RATING_LOW, RATING_NOT_TOP, RATING_UNRATED)
# Per-game overrides (the user's own "always keep" / "always exclude" of one game), applied after every rule.
OVERRIDE_KEEP, OVERRIDE_EXCLUDE = "override_keep", "override_exclude"
OVERRIDE_CODES = (OVERRIDE_KEEP, OVERRIDE_EXCLUDE)
OVERRIDE_ACTIONS = ("keep", "exclude")
ALL_CODES = RULES + FLAG_CODES + (LANGUAGE_CODE,) + RATING_CODES + OVERRIDE_CODES
BORROWED_CODE = "borrowed"      # Decision.codes of a kept disk that completes a set of another edition
# "Games you have saves for" (Amendment 30): what the build does with a game the rules would set aside while RetroArch
# saves or save states exist for it. ``keep`` = it stays (SAVED_KEEP), ``archive`` = it goes and its saves go with it,
# ``leave`` = it goes and the saves stay where they are.
SAVED_GAMES = ("keep", "archive", "leave")
SAVED_KEEP = "saved_keep"       # Decision.codes of a game kept because you have saves or save states for it
SAVED_KEEP_TEXT = "kept: you have saves or save states for it"

_RULE_SHORT = {
    "bad_dump": "bad dump", "virus": "virus-infected", "bad_size": "over/under dump",
    "pre_release": "pre-release", "prototype": "prototype", "demo": "demo", "faked": "faked",
    "unreleased": "unreleased", "modified": "modified",
    "flag_cr": "crack flag", "flag_h": "hack flag", "flag_t": "trainer flag",
    "flag_a": "alternate flag", "flag_f": "fix flag", "flag_tr": "translation flag",
    "language": "not in the selected languages",
    "rating_low": "rated below the minimum", "rating_not_top": "not among the top rated games",
    "rating_unrated": "no usable rating",
    "override_keep": "always kept by you", "override_exclude": "always excluded by you",
    "saved_keep": "kept because you have saves for it",
}

RANK_SCOPES = ("dat", "owned")
DEFAULT_MIN_VOTES = 5


# --------------------------------------------------------------------------- profile

def _norm_languages(raw: Any) -> tuple[str, ...]:
    """Ordered, de-duplicated, known language codes. A set / frozenset has no order: English first, then A-Z."""
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (set, frozenset)):
        raw = sorted(raw, key=lambda c: (c != "En", str(c)))
    out: list[str] = []
    for c in raw or ():
        if isinstance(c, str) and c in tags.LANGUAGES and c not in out:
            out.append(c)
    return tuple(out)


def _norm_regions(raw: Any) -> tuple[str, ...]:
    out: list[str] = []
    for r in raw or ():
        r = tags.canon_region(r) if isinstance(r, str) else r
        if isinstance(r, str) and r in tags.REGIONS and r not in out:
            out.append(r)
    return tuple(out)


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def _norm_min_rating(v: Any) -> Optional[float]:
    n = _num(v)
    return None if n is None or n <= 0 or n > 10 else round(n, 1)


def _norm_top_n(v: Any) -> Optional[int]:
    n = _num(v)
    return None if n is None or n < 1 else int(n)


def _valid_votes(v: Any) -> bool:
    n = _num(v)
    return n is not None and n >= 1


def _norm_overrides(raw: Any) -> tuple[tuple[str, str, str], ...]:
    """``((dat, game, "keep" | "exclude"), ...)``: valid entries only, one per game (the last wins), sorted."""
    found: dict[tuple[str, str], str] = {}
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(entry, (list, tuple)) and len(entry) == 3 and all(isinstance(x, str) for x in entry) \
                and entry[0] and entry[1] and entry[2] in OVERRIDE_ACTIONS:
            found[(entry[0], entry[1])] = entry[2]
    return tuple(sorted((d, n, a) for (d, n), a in found.items()))


def _norm_votes(v: Any) -> int:
    n = _num(v)
    return DEFAULT_MIN_VOTES if n is None or n < 1 else int(n)


@dataclass(frozen=True)
class LibraryProfile:
    exclude: frozenset[str] = frozenset(RULES)   # enabled exclusion rules
    latest_only: bool = True
    best_variant: bool = True                    # Amiga Games only (implies latest for that DAT)
    complete_only: bool = True                   # multi-disk sets
    # Language filter (Amiga Games + No-Intro): ORDERED codes, preferred first; empty = no filter.
    languages: tuple[str, ...] = ("En",)
    # Flag types that may stay (Amiga Games): a type that is missing here excludes its variants.
    keep_flags: frozenset[str] = frozenset(tags.KEEP_FLAGS)
    rescue_only_dump: bool = False               # keep-every-version DATs: keep the sole [m]/[o]/[u] dump
    region_priority: tuple[str, ...] = tags.DEFAULT_REGION_PRIORITY   # No-Intro: best region first
    one_per_game: bool = True                    # No-Intro: exactly one variant per game
    # Amiga Games: a disk slot nobody has in the same edition may be filled by a disk of another edition
    # (country / language / edition flags / version / year) of the same title, publisher and disk count.
    borrow_other_editions: bool = True
    # No-Intro / Redump: a game with NO version in the selected languages anywhere in its DAT (e.g. a Japan-only
    # release) is kept anyway instead of being left out. A game that has a version in a selected language is unchanged.
    keep_other_language: bool = True
    # Ratings (Amendment 18; LaunchBox community ratings, 0-10). Inactive (nothing changes, no data needed) while
    # both ``min_rating`` and ``top_n`` are None.
    min_rating: Optional[float] = None           # games rated below this are excluded (None = off)
    top_n: Optional[int] = None                  # keep only the N best-rated games (None = off)
    min_votes: int = DEFAULT_MIN_VOTES           # a game with fewer votes counts as UNRATED
    keep_unrated: bool = False                   # with a rating filter: keep games that have no usable rating
    rank_scope: str = "dat"                      # top_n ranks against the whole DAT target set ("dat") or only your games ("owned")
    # "Always keep" / "always exclude" of single games: (DAT name, game = set name or rom name, action).
    overrides: tuple[tuple[str, str, str], ...] = ()
    # RetroArch saves / save states (Amendment 30): "keep" (a game you have saves for is never set aside by the rules),
    # "archive" (it is set aside and its saves go to the archive with it), "leave" (it is set aside, the saves stay).
    saved_games: str = "keep"

    def __post_init__(self) -> None:
        # frozen dataclass: normalise whatever the caller passed (lists, sets, unknown codes)
        object.__setattr__(self, "exclude", frozenset(r for r in self.exclude if r in RULES))
        object.__setattr__(self, "languages", _norm_languages(self.languages))
        object.__setattr__(self, "keep_flags", frozenset(f for f in self.keep_flags if f in tags.KEEP_FLAGS))
        object.__setattr__(self, "region_priority", _norm_regions(self.region_priority))
        object.__setattr__(self, "min_rating", _norm_min_rating(self.min_rating))
        object.__setattr__(self, "top_n", _norm_top_n(self.top_n))
        object.__setattr__(self, "min_votes", _norm_votes(self.min_votes))
        object.__setattr__(self, "rank_scope", self.rank_scope if self.rank_scope in RANK_SCOPES else "dat")
        object.__setattr__(self, "overrides", _norm_overrides(self.overrides))
        object.__setattr__(self, "saved_games", self.saved_games if self.saved_games in SAVED_GAMES else "keep")

    @property
    def rating_active(self) -> bool:
        """True when a rating filter (minimum rating and / or top N) is set."""
        return self.min_rating is not None or self.top_n is not None

    def to_dict(self) -> dict:
        return {"exclude": sorted(self.exclude), "latest_only": self.latest_only,
                "best_variant": self.best_variant, "complete_only": self.complete_only,
                "languages": list(self.languages), "keep_flags": sorted(self.keep_flags),
                "rescue_only_dump": self.rescue_only_dump,
                "region_priority": list(self.region_priority), "one_per_game": self.one_per_game,
                "borrow_other_editions": self.borrow_other_editions,
                "keep_other_language": self.keep_other_language,
                "min_rating": self.min_rating, "top_n": self.top_n, "min_votes": self.min_votes,
                "keep_unrated": self.keep_unrated, "rank_scope": self.rank_scope,
                "overrides": [list(o) for o in self.overrides], "saved_games": self.saved_games}

    @classmethod
    def from_dict(cls, d: Any, defaults: Optional["LibraryProfile"] = None) -> "LibraryProfile":
        """Unknown keys / rules are ignored, missing keys take ``defaults`` (old saved profiles load)."""
        base = defaults if defaults is not None else cls()
        if not isinstance(d, Mapping):
            return base
        exclude = base.exclude
        raw = d.get("exclude")
        if isinstance(raw, (list, tuple, set, frozenset)):
            exclude = frozenset(r for r in raw if r in RULES)

        def flag(key: str, default: bool) -> bool:
            v = d.get(key)
            return v if isinstance(v, bool) else default

        langs = base.languages
        if isinstance(d.get("languages"), (list, tuple, set, frozenset)):
            langs = _norm_languages(d["languages"])
        keep = base.keep_flags
        if isinstance(d.get("keep_flags"), (list, tuple, set, frozenset)):
            keep = frozenset(f for f in d["keep_flags"] if f in tags.KEEP_FLAGS)
        regions = base.region_priority
        if isinstance(d.get("region_priority"), (list, tuple)):
            regions = _norm_regions(d["region_priority"])
        rating = {}
        if "min_rating" in d:
            rating["min_rating"] = d["min_rating"]
        if "top_n" in d:
            rating["top_n"] = d["top_n"]
        if "min_votes" in d and _valid_votes(d["min_votes"]):
            rating["min_votes"] = d["min_votes"]
        if isinstance(d.get("rank_scope"), str) and d["rank_scope"] in RANK_SCOPES:
            rating["rank_scope"] = d["rank_scope"]
        return cls(exclude=exclude, latest_only=flag("latest_only", base.latest_only),
                   best_variant=flag("best_variant", base.best_variant),
                   complete_only=flag("complete_only", base.complete_only),
                   languages=langs, keep_flags=keep,
                   rescue_only_dump=flag("rescue_only_dump", base.rescue_only_dump),
                   region_priority=regions, one_per_game=flag("one_per_game", base.one_per_game),
                   borrow_other_editions=flag("borrow_other_editions", base.borrow_other_editions),
                   keep_other_language=flag("keep_other_language", base.keep_other_language),
                   **{**{"min_rating": base.min_rating, "top_n": base.top_n, "min_votes": base.min_votes,
                         "rank_scope": base.rank_scope}, **rating},
                   keep_unrated=flag("keep_unrated", base.keep_unrated),
                   overrides=_norm_overrides(d["overrides"]) if "overrides" in d else base.overrides,
                   saved_games=d["saved_games"] if isinstance(d.get("saved_games"), str) and d["saved_games"] in SAVED_GAMES
                   else base.saved_games)

    @classmethod
    def latest_only_profile(cls) -> "LibraryProfile":
        """The legacy ``latest_only`` flag: no exclusions, no filters, no best variant / completeness."""
        return cls(exclude=frozenset(), latest_only=True, best_variant=False, complete_only=False,
                   languages=(), one_per_game=False, borrow_other_editions=False)


def _dats(platform: Any, attr: str) -> tuple[str, ...]:
    return tuple(getattr(platform, attr, ()) or ())


def _borrow_scope(platform: Any) -> bool:
    """Borrowing disks of other editions applies to the TOSEC "Games"-style DATs (``best_variant_dats``) only."""
    return bool(_dats(platform, "best_variant_dats")) and _style_of_platform(platform) == STYLE_TOSEC


def default_profile(platform: "Platform") -> LibraryProfile:
    """All rules on, minus the options that do not apply to the platform."""
    return LibraryProfile(
        exclude=frozenset(RULES),
        latest_only=bool(_dats(platform, "latest_dats")),
        best_variant=bool(_dats(platform, "best_variant_dats")),
        complete_only=bool(_dats(platform, "m3u_dats")),
        languages=("En",) if _dats(platform, "language_dats") else (),
        one_per_game=bool(_dats(platform, "region_dats")),
        borrow_other_editions=_borrow_scope(platform))


def load_profile(cfg: dict, platform: "Platform") -> LibraryProfile:
    """The platform's profile from ``cfg["library"][name]`` (legacy ``cfg["latest_only"][name]`` honoured)."""
    defaults = default_profile(platform)
    lib = cfg.get("library") if isinstance(cfg, dict) else None
    entry = lib.get(platform.name) if isinstance(lib, dict) else None
    if entry is not None:
        return LibraryProfile.from_dict(entry, defaults)
    legacy = cfg.get("latest_only") if isinstance(cfg, dict) else None
    if isinstance(legacy, dict) and isinstance(legacy.get(platform.name), bool):
        return replace(defaults, latest_only=legacy[platform.name] and defaults.latest_only)
    return defaults


def store_profile(cfg: dict, platform: "Platform", profile: LibraryProfile) -> None:
    cfg.setdefault("library", {})[platform.name] = profile.to_dict()


# --------------------------------------------------------------------------- data

@dataclass
class Item:
    """One local FILE that survived duplicate removal (built by ``organiser.plan_renames``)."""
    key: int
    dat: str
    rom: "Rom"                    # the rom chosen by organiser.choose_rom
    style: str                    # "tosec" | "nointro"
    path: Path
    member: Optional[str]
    form: str = ""                # organiser._header_form
    link: bool = False            # a symlink: takes part in the selection but is never moved
    disc_total: int = 0           # Redump multi-disc games: discs of this release in the DAT (0 = unknown)


@dataclass
class Decision:
    key: int
    action: str
    codes: tuple[str, ...] = ()   # excluded: rule keys
    detail: str = ""              # excluded: exact flag texts ("[b corrupt file]")
    superseded_by: str = ""
    set_id: Optional[int] = None
    missing: tuple[int, ...] = ()
    reason: str = ""              # human text
    elsewhere: tuple[int, ...] = ()  # incomplete: missing disks the user has under another identity
    # keep: ``codes == ("borrowed",)`` marks a disk of another edition used to complete a set (``BORROWED_CODE``)


@dataclass
class ChosenSet:
    """A kept multi-disk set -> one playlist."""
    id: int
    dat: str
    name: str
    total: int
    slots: dict[int, int]         # disk number -> Item.key
    labels: dict[int, str]
    flags: tuple[str, ...]
    cracked: bool
    # slot -> {"key": Item.key, "name": rom name, "edition": "(DE)", "differences": ["country", ...], "text": note}
    borrowed: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass
class IncompleteSet:
    dat: str
    name: str
    total: int
    present: dict[int, int]       # disk number -> Item.key
    missing: tuple[int, ...]
    kept: bool = False            # Redump: the discs stay where they are (no ``_incomplete`` move), only reported


@dataclass
class Vanished:
    """A title (one game as the user knows it) none of whose local variants is kept."""
    dat: str
    title: str
    name: str                     # one of its file names (the closest to being kept)
    reason: str                   # primary reason code ("language", "flag_cr", "bad_dump", ... "incomplete")
    codes: tuple[str, ...]        # every code that blocks a variant on its own (incl. ``reason``)
    languages: tuple[str, ...] = ()   # reason == "language": the languages its variants do have
    variants: int = 0
    detail: str = ""              # rating reasons: "rated 6.2 (12 votes)" of the closest variant


@dataclass
class Selection:
    decisions: dict[int, Decision] = field(default_factory=dict)
    # Rating filter summary (empty while no filter is set): see :func:`apply_ratings`
    rating: dict[str, Any] = field(default_factory=dict)
    sets: list[ChosenSet] = field(default_factory=list)
    incomplete: list[IncompleteSet] = field(default_factory=list)
    vanished: list[Vanished] = field(default_factory=list)

    def exclusion_counts(self) -> dict[str, dict[str, int]]:
        """``{"exclusive": {code: files}, "any": {code: files}}``: excluded files per reason code.

        ``exclusive`` counts each file once, under its first code (rules in ``RULES`` order, then
        ``flag_*``, then ``language``); ``any`` counts every code a file hits."""
        excl: dict[str, int] = {}
        anyc: dict[str, int] = {}
        for d in self.decisions.values():
            if d.action != EXCLUDED or not d.codes:
                continue
            excl[d.codes[0]] = excl.get(d.codes[0], 0) + 1
            for c in d.codes:
                anyc[c] = anyc.get(c, 0) + 1
        return {"exclusive": excl, "any": anyc}

    def saved_kept(self) -> int:
        """Files kept only because you have saves for them (``saved_games == "keep"``)."""
        return sum(1 for d in self.decisions.values() if d.action == KEEP and SAVED_KEEP in d.codes)

    def borrow_summary(self) -> dict[str, Any]:
        """``{"sets": playlists completed with borrowed disks, "disks": borrowed disks, "by_difference": {kind: disks}}``."""
        by: dict[str, int] = {}
        disks = 0
        sets = 0
        for cs in self.sets:
            if not cs.borrowed:
                continue
            sets += 1
            for b in cs.borrowed.values():
                disks += 1
                for d in b.get("differences", ()):
                    by[d] = by.get(d, 0) + 1
        return {"sets": sets, "disks": disks, "by_difference": by}

    def vanish_summary(self) -> dict[str, Any]:
        """``{"titles": n, "by_reason": {code: n}, "by_code": {code: n}}`` over :attr:`vanished`."""
        by_reason: dict[str, int] = {}
        by_code: dict[str, int] = {}
        for v in self.vanished:
            by_reason[v.reason] = by_reason.get(v.reason, 0) + 1
            for c in v.codes:
                by_code[c] = by_code.get(c, 0) + 1
        return {"titles": len(self.vanished), "by_reason": by_reason, "by_code": by_code}


# --------------------------------------------------------------------------- helpers

def _rom_name(item: Item) -> str:
    """Name that carries the tags: the set name for No-Intro, else the rom name."""
    return getattr(item.rom, "set_name", "") or item.rom.name


def _style(item: Item) -> str:
    return item.style if item.style in (STYLE_TOSEC, STYLE_NOINTRO, STYLE_WHDLOAD, STYLE_REDUMP) else STYLE_TOSEC


def _tags_of(item: Item) -> tags.Tags:
    if _style(item) == STYLE_REDUMP:     # name + the DAT <category> (Demos, Coverdiscs, Preproduction)
        return tags.redump_tags(_rom_name(item), getattr(item.rom, "category", "") or "")
    if _style(item) == STYLE_WHDLOAD:    # game name + archive stem (the stem alone lacks the status)
        return tags.parse_whdload(_rom_name(item), getattr(item.rom, "game", "") or "")
    return tags.parse_name(_rom_name(item), _style(item))


def game_key(item: Item) -> tuple:
    """The identity of the GAME an item belongs to, exactly as :func:`select` groups variants: TOSEC / WHDLoad
    ``tags.identity_key`` (title, country, publisher, edition and status tags), No-Intro / Redump ``tags.game_key``
    (title, status, product tags; regions, languages, versions and disc numbers are not part of it)."""
    return (item.dat, tags.game_key(_tags_of(item)))      # tags.game_key = identity_key for TOSEC / WHDLoad


def rom_title(rom: "Rom", dat: str = "") -> str:
    """The game title of a DAT rom as the library (and the ratings) know it: name without year, publisher, regions,
    languages, versions, disk / disc numbers and dump flags."""
    item = Item(key=0, dat=dat or getattr(rom, "dat", ""), rom=rom, style=tags.style_of_rom(rom), path=Path(rom.name),
                member=None)
    return _tags_of(item).title


def _describe(info: Sequence[tuple[str, str]]) -> str:
    by_rule: dict[str, list[str]] = {}
    for rule, text in info:
        by_rule.setdefault(rule, []).append(text)
    return ", ".join(f"{_RULE_SHORT.get(r, r)} {' '.join(t)}" for r, t in by_rule.items())


def exclusion_of(rom_name: str, style: str, profile: LibraryProfile) -> tuple[tuple[str, ...], str]:
    """``(rule keys, exact flag texts)`` that exclude ``rom_name`` under ``profile`` (empty when kept).

    Only the exclusion rules; the language / keep-flag filters depend on the DAT (see :func:`eligibility`)."""
    info = [(r, x) for r, x in tags.exclusion_info(tags.parse_name(rom_name, style)) if r in profile.exclude]
    rules = tuple(dict.fromkeys(r for r, _x in info))
    return rules, " ".join(x for _r, x in info)


def eligibility(rom_name: str, style: str, profile: LibraryProfile, languages: bool = True,
                flags: bool = True, parsed: Optional[tags.Tags] = None) -> list[tuple[str, str]]:
    """Every ``(code, exact text)`` that makes ``rom_name`` ineligible under ``profile``.

    Codes: the exclusion rules (``RULES``), ``flag_<x>`` (a flag type missing from
    ``profile.keep_flags``; text = the exact ``[cr FLT]``) and ``language`` (no selected language;
    text = the languages it has, e.g. ``(De)``). ``languages`` / ``flags`` switch those two filters off
    (they only apply to some DATs). ``parsed``: the already parsed tags (WHDLoad needs the game name too)."""
    t = parsed if parsed is not None else tags.parse_name(rom_name, style)
    info = [(r, x) for r, x in tags.exclusion_info(t) if r in profile.exclude]
    if flags and style == STYLE_TOSEC:
        for code in tags.KEEP_FLAGS:
            if code not in profile.keep_flags:
                rx = tags._FLAG_RES[code]
                for f in t.dump_flags:
                    if rx.match(f):
                        info.append((f"flag_{code}", f"[{f}]"))
    if languages and profile.languages:
        have = tags.variant_languages(t)
        if not have & set(profile.languages):
            info.append((LANGUAGE_CODE, "(" + ",".join(sorted(have, key=lambda c: (c != "En", c))) + ")"))
    return info


class _Scope:
    """Which DATs of the platform each rule family covers (``Platform.*_dats``)."""

    def __init__(self, platform: Any) -> None:
        self.latest = set(_dats(platform, "latest_dats"))
        self.best = set(_dats(platform, "best_variant_dats"))
        self.m3u = set(_dats(platform, "m3u_dats"))
        self.language = set(_dats(platform, "language_dats"))
        self.region = set(_dats(platform, "region_dats"))
        self.keep_all_dats_excluded = self.latest | self.best      # DATs with one-variant rules

    def flags(self, dat: str) -> bool:
        return dat in self.best


def _lang_rank(langs: Iterable[str], profile: LibraryProfile) -> int:
    """Position of the best selected language among ``langs`` (0 = most preferred; 0 without a filter)."""
    if not profile.languages:
        return 0
    order = {c: i for i, c in enumerate(profile.languages)}
    return min((order[c] for c in langs if c in order), default=len(order))


def _release_key(item: Item) -> tuple:
    """One released version of a product: the name without its dump flags."""
    t = _tags_of(item)
    return (t.style, t.title.casefold(), t.version, t.regions,
            () if t.languages_implied else t.languages, t.flags)


# Rules whose exclusion is rescued on the "keep every version" DATs (Kickstart-Disks, Workbench,
# Firmware) when the excluded file is the only dump of its version AND ``profile.rescue_only_dump``
# is on (default off): an official release must not vanish because its sole listed dump carries [m] / [u].
RESCUE_RULES = frozenset({"modified", "bad_size"})


def _stem(name: str) -> str:
    return tags._EXT_RE.sub("", name.strip())


@dataclass
class _Set:
    """A candidate complete set (a single disk counts as a set of one)."""
    total: int
    slots: dict[int, Item]
    labels: dict[int, str]
    flags: tuple[str, ...]        # non-neutral dump flags of the anchor (bracketed)
    name: str                     # playlist name (multi) / rom stem (single)
    anchor: Item
    vkey: tuple
    date: str
    cracked: bool
    mods: int
    complete: bool = True
    missing: tuple[int, ...] = ()
    chosen: bool = False
    lang_rank: int = 0            # best selected language the set offers (0 = most preferred)
    platform_rank: int = 0        # tags.platform_rank of the anchor (0 = CD32 / best)
    part: tuple = ()              # tags.partition_key of the anchor (language + chipset)
    borrowed: dict = field(default_factory=dict)   # slot -> note dict of a disk of another edition (see ChosenSet)

    @property
    def refs(self) -> tuple:
        return tuple(sorted((i, it.key) for i, it in self.slots.items()))


def _disk_info(t: tags.Tags) -> Optional[tuple[int, int]]:
    d = tags._disk(t)
    if d is None or d[2] < 2:
        return None
    return d[1], d[2]


_PAREN_RE = re.compile(r"\(([^()]*)\)")
_DATE_TOKEN_RE = re.compile(r"^(?:[0-9x?]{4})(?:-[0-9x?]{2}(?:-[0-9x?]{2})?)?$", re.IGNORECASE)


def _edition_text(name: str, publisher: str) -> str:
    """The country / language / edition tokens of a TOSEC name as written, e.g. ``(DE)`` or ``(de)(M3)``
    (the parentheses between the publisher and the disk token); ``""`` when there are none."""
    from . import m3u

    stem = tags._EXT_RE.sub("", name.strip())
    m = m3u._DISK_TOKEN.search(stem)
    head = stem[:m.start()] if m else stem
    groups = [g for g in _PAREN_RE.findall(head) if not _DATE_TOKEN_RE.match(g.strip())]
    if groups and publisher and groups[0] == publisher:
        groups = groups[1:]
    return "".join(f"({g})" for g in groups)


class _Borrow:
    """Disks of other editions that may complete a set (``LibraryProfile.borrow_other_editions``).

    One instance per DAT: ``pool`` indexes every eligible multi-disk file - plus the files excluded ONLY by the
    language filter (the user accepts a disk in an unselected language) - by ``(title, publisher, disk total)``
    and disk number. Quality exclusions (bad dump, virus, pre-release, demo, ...) and keep-flag exclusions never
    enter it. A candidate must also have the anchor's status tokens (a demo never completes a game) and a
    compatible chipset (``tags.chipset_compatible``)."""

    def __init__(self, profile: LibraryProfile, items: Iterable[Item]) -> None:
        self.profile = profile
        self.by_key: dict[int, Item] = {}
        self.tags: dict[int, tags.Tags] = {}
        self.pool: dict[tuple, dict[int, list[Item]]] = {}
        for it in sorted(items, key=lambda i: (i.rom.name, i.key)):
            t = tags.parse_name(it.rom.name, STYLE_TOSEC)
            d = _disk_info(t)
            if d is None:
                continue
            self.by_key[it.key] = it
            self.tags[it.key] = t
            self.pool.setdefault((t.title.casefold(), t.publisher.casefold(), d[1]), {}) \
                .setdefault(d[0], []).append(it)

    def _tier(self, anchor: tags.Tags, t: tags.Tags) -> int:
        if tags.identity_key(t) == tags.identity_key(anchor) and tags.partition_key(t) == tags.partition_key(anchor):
            return 0              # the same edition (a different version / date)
        langs = self.profile.languages
        if not langs or tags.variant_languages(t) & set(langs):
            return 1              # another edition, but in a selected (or neutral) language
        return 2

    @staticmethod
    def differences(anchor: tags.Tags, t: tags.Tags) -> list[str]:
        out: list[str] = []
        if anchor.regions != t.regions:
            out.append("country")
        if tags.variant_languages(anchor) != tags.variant_languages(t) \
                or (() if anchor.languages_implied else anchor.languages) != (() if t.languages_implied else t.languages):
            out.append("language")
        if tags.identity_key(anchor)[4] != tags.identity_key(t)[4]:
            out.append("edition")
        if anchor.version != t.version:
            out.append("version")
        if tags.date_key(anchor.date) != tags.date_key(t.date):
            out.append("year")
        return out

    def fill(self, ss: Any, anchor: Item, anchor_tags: tags.Tags, group_names: Sequence[str]
             ) -> Optional[dict[int, tuple[Item, dict[str, Any]]]]:
        """Disks of other editions for EVERY missing slot of ``ss`` (an anchored, incomplete ``m3u.SlotSet``),
        or None when any slot cannot be filled (no best-effort sets)."""
        from . import m3u

        a = anchor_tags
        a_status = tags.identity_key(a)[5]
        entry = self.pool.get((a.title.casefold(), a.publisher.casefold(), ss.total), {})
        taken = {c.ref for c in ss.slots.values()}
        out: dict[int, tuple[Item, dict[str, Any]]] = {}
        for slot in ss.missing:
            opts: list[Item] = []
            seen: set[str] = set()
            for it in entry.get(slot, ()):
                if it.key in taken or it.rom.name in seen:
                    continue
                t = self.tags[it.key]
                if tags.identity_key(t)[5] != a_status or not tags.chipset_compatible(a, t):
                    continue
                seen.add(it.rom.name)
                opts.append(it)
            if not opts:
                return None
            cands = [m3u.DiskCand(ref=it.key, name=it.rom.name) for it in opts]
            vkeys = tags.group_version_keys(sorted({*group_names, *(c.name for c in cands)}), STYLE_TOSEC)
            best = m3u.pick_borrowed(cands, ss.flags, vkeys, lambda c: self._tier(a, self.tags[c.ref]))
            if best is None:
                return None
            it = self.by_key[best.ref]
            t = self.tags[it.key]
            diffs = self.differences(a, t)
            edition = _edition_text(it.rom.name, t.publisher)
            if {"country", "language", "edition"} & set(diffs):
                text = f"disk {slot} borrowed from the {edition or 'untagged'} edition ({_stem(it.rom.name)})"
            else:
                text = f"disk {slot} borrowed from another version of the same edition ({_stem(it.rom.name)})"
            out[slot] = (it, {"key": it.key, "name": it.rom.name, "edition": edition,
                              "differences": diffs, "text": text})
            taken.add(it.key)
        return out


def _build_sets(group: list[Item], vkeys: Mapping[str, tuple],
                profile: Optional[LibraryProfile] = None,
                borrow: Optional[_Borrow] = None) -> tuple[list[_Set], list[_Set]]:
    """``(complete sets, incomplete sets)`` of one identity group.

    With ``borrow`` an anchored incomplete set whose disk count has no complete set in the group is completed
    by disks of other editions (every missing slot must be fillable)."""
    from . import m3u

    parsed = {it.key: tags.parse_name(it.rom.name, STYLE_TOSEC) for it in group}
    by_key = {it.key: it for it in group}
    complete: list[_Set] = []
    partial: list[_Set] = []

    def tags_of(it: Item) -> tags.Tags:
        t = parsed.get(it.key)
        if t is None:
            t = parsed[it.key] = tags.parse_name(it.rom.name, STYLE_TOSEC)
        return t

    def make(anchor: Item, slots: dict[int, Item], total: int, labels: dict[int, str],
             flags: tuple[str, ...], name: str, missing: tuple[int, ...],
             borrowed: Optional[dict[int, dict[str, Any]]] = None) -> _Set:
        ts = [tags_of(it) for it in slots.values()]
        at = parsed[anchor.key]
        langs = frozenset().union(*(tags.variant_languages(t) for t in ts))
        return _Set(
            total=total, slots=slots, labels=labels, flags=flags, name=name, anchor=anchor,
            vkey=vkeys.get(anchor.rom.name, (0,)), date=tags.date_key(at.date),
            cracked=any(tags.is_cracked(t) for t in ts),
            mods=sum(tags.modification_count(t) for t in ts),
            complete=not missing, missing=missing,
            lang_rank=_lang_rank(langs, profile) if profile is not None else 0,
            platform_rank=tags.platform_rank(at), part=tags.partition_key(at),
            borrowed=dict(borrowed or {}))

    multi: list[Item] = []
    for it in sorted(group, key=lambda i: (i.rom.name, i.key)):
        if _disk_info(parsed[it.key]) is None:
            sflags = tuple(f for f in (f"[{d}]" for d in parsed[it.key].dump_flags)
                           if not m3u.is_neutral_flag(f))
            complete.append(make(it, {1: it}, 1, {}, sflags, _stem(it.rom.name), ()))
        else:
            multi.append(it)
    if multi:
        # several local files may carry one rom name: the first stands for it
        first: dict[str, Item] = {}
        for it in multi:
            first.setdefault(it.rom.name, it)
        cands = [m3u.DiskCand(ref=it.key, name=name) for name, it in first.items()]
        found = m3u.resolve_slots(cands, vkeys)
        have_complete = {ss.total for ss in found if ss.complete}
        for ss in found:
            slots = {i: by_key[c.ref] for i, c in ss.slots.items()}
            anchor = by_key[ss.anchor.ref] if ss.anchor is not None else slots[min(slots)]
            flags = tuple(f for f in ss.flags if not m3u.is_neutral_flag(f))
            if borrow is not None and ss.anchor is not None and not ss.complete \
                    and ss.total not in have_complete:
                filled = borrow.fill(ss, anchor, parsed[anchor.key], [it.rom.name for it in group])
                if filled is not None:
                    allslots = dict(slots)
                    notes: dict[int, dict[str, Any]] = {}
                    for slot, (bit, note) in filled.items():
                        allslots[slot] = bit
                        notes[slot] = note
                    allslots = dict(sorted(allslots.items()))
                    names = [m3u.parse_disk_name(it.rom.name) for it in allslots.values()]
                    labels = {i: (d.label if d else "") for i, d in zip(allslots, names)}
                    name = m3u._slot_name(ss.title, ss.flags, [d for d in names if d])
                    s = make(anchor, allslots, ss.total, labels, flags, name, (), notes)
                    complete.append(s)
                    continue
            s = make(anchor, slots, ss.total, dict(ss.labels), flags, ss.name, tuple(ss.missing))
            (complete if s.complete else partial).append(s)
    return complete, partial


def _newer(s: _Set) -> tuple:
    return (s.vkey, s.date)


def _pick_best(sets: list[_Set]) -> _Set:
    """Best language, then cracked, then best platform (CD32 > AGA > OCS), then newest, then fewest
    modification flags ([t] [h] [tr] [f] [a]), then name."""
    pool = sets
    for key in (lambda s: s.lang_rank, lambda s: not s.cracked, lambda s: s.platform_rank):
        best = min(key(s) for s in pool)
        pool = [s for s in pool if key(s) == best]
    newest = max(_newer(s) for s in pool)
    pool = [s for s in pool if _newer(s) == newest]
    return min(pool, key=lambda s: (s.mods, s.anchor.rom.name.casefold(), s.anchor.rom.name, s.refs))


# --------------------------------------------------------------------------- select

class RatingsUnavailable(Exception):
    """A rating filter is set but no rating data was supplied (never filter silently with missing data)."""


@dataclass
class RatingContext:
    """What the rating filter needs: a title lookup and, for ``rank_scope == "dat"``, the rank of the cutoff game.

    ``lookup(title) -> (rating out of 10, votes) | None`` (already bound to the platform); ``cutoff`` = the rank key of
    the N-th best game of the whole target set (see :func:`target_cutoff`; ``INF_KEY`` = fewer than N rated games, no cap)."""
    lookup: Callable[[str], Optional[tuple[float, int]]]
    cutoff: Optional[tuple] = None


INF_KEY: tuple = (float("inf"),)


OTHER_LANGUAGE_STYLES = (STYLE_NOINTRO, STYLE_REDUMP)      # whose game identity does not include the region


def language_games(items: Iterable[Item], profile: LibraryProfile, platform: "Platform") -> frozenset:
    """The games (``game_key``) that have at least one variant which passes every rule INCLUDING the language filter.
    Pass the whole DAT's items: a game that is not in the set has no version in the selected languages, so
    ``keep_other_language`` keeps what exists of it."""
    scope = _Scope(platform)
    out = set()
    for it in items:
        if _style(it) not in OTHER_LANGUAGE_STYLES or it.dat not in scope.language:
            continue
        if not eligibility(_rom_name(it), _style(it), profile, languages=True, flags=scope.flags(it.dat),
                           parsed=_tags_of(it) if _style(it) in (STYLE_WHDLOAD, STYLE_REDUMP) else None):
            out.add(game_key(it))
    return frozenset(out)


def select(items: Sequence[Item], profile: LibraryProfile, platform: "Platform",
           ratings: Optional[RatingContext] = None, lang_games: Optional[frozenset] = None,
           saved: Optional[frozenset] = None) -> Selection:
    """Decide keep / excluded / superseded / incomplete per item (see the module docstring).

    With a rating filter in ``profile`` (``profile.rating_active``) ``ratings`` is required (:class:`RatingsUnavailable`
    otherwise) and the filter is applied last, at game level (:func:`apply_ratings`). ``saved``: the content names (as
    ``retroarch.save_name_keys`` writes them) RetroArch has saves or states for; with ``profile.saved_games == "keep"`` a
    game among them is never set aside (:func:`apply_saved`); None / empty = nothing changes."""
    if profile.rating_active and ratings is None:
        raise RatingsUnavailable("a rating filter is set but no rating data was supplied")
    sel = _select_base(items, profile, platform, lang_games)
    if profile.rating_active:
        apply_ratings(sel, items, profile, platform, ratings)  # type: ignore[arg-type]
    if saved and profile.saved_games == "keep":
        apply_saved(sel, items, saved)
    apply_overrides(sel, items, profile)
    sel.vanished = _vanished(items, sel)
    return sel


def content_names(item: Item) -> tuple[str, ...]:
    """The names RetroArch may use for the item's saves: the file's name without extension (the CHD of a disc game, the
    archive of a zipped ROM) and, for a member of an archive, the member's."""
    names = [item.path.stem]
    if item.member:
        names.append(Path(item.member).stem)
    return tuple(names)


def apply_saved(sel: Selection, items: Sequence[Item], saved: frozenset) -> None:
    """Keep the games the rules set aside (excluded or superseded) when RetroArch has saves or states for them. A user's
    own "always exclude" still wins (it is applied after this). Files that are part of a multi-disk set, symbolic links
    and incomplete games are left to their rules."""
    from .retroarch import fold_name

    for it in items:
        d = sel.decisions.get(it.key)
        if d is None or d.action not in (EXCLUDED, SUPERSEDED) or d.set_id is not None or it.link:
            continue
        if any(fold_name(n) in saved for n in content_names(it)):
            sel.decisions[it.key] = Decision(key=it.key, action=KEEP, codes=(SAVED_KEEP,), reason=SAVED_KEEP_TEXT)


def override_game(dat: str, rom: Any) -> tuple[str, str]:
    """The (DAT, game) an override is stored under: the set name (No-Intro / Redump) or the rom name (TOSEC)."""
    return (dat, getattr(rom, "set_name", "") or rom.name)


def apply_overrides(sel: Selection, items: Sequence[Item], profile: LibraryProfile) -> None:
    """The user's per-game choices beat every rule: ``keep`` keeps the file (whatever excluded / superseded / left it
    out), ``exclude`` sets it aside. A playlist is dropped when one of its disks is excluded this way."""
    if not profile.overrides:
        return
    wanted = {(d, n): a for d, n, a in profile.overrides}
    excluded_keys: set[int] = set()
    for it in items:
        action = wanted.get(override_game(it.dat, it.rom))
        if action == "keep":
            d = sel.decisions.get(it.key)
            if d is None or d.action != KEEP or BORROWED_CODE in d.codes:
                sel.decisions[it.key] = Decision(key=it.key, action=KEEP, codes=(OVERRIDE_KEEP,),
                                                 reason="kept because you chose to always keep it")
        elif action == "exclude":
            sel.decisions[it.key] = Decision(key=it.key, action=EXCLUDED, codes=(OVERRIDE_EXCLUDE,),
                                             reason="excluded because you chose to always exclude it")
            excluded_keys.add(it.key)
    if excluded_keys:
        sel.sets = [cs for cs in sel.sets if not excluded_keys & set(cs.slots.values())]


def _select_base(items: Sequence[Item], profile: LibraryProfile, platform: "Platform",
                 lang_games: Optional[frozenset] = None) -> Selection:
    """Every rule except the rating filter. ``lang_games``: see :func:`language_games` (None = the items themselves)."""
    sel = Selection()
    next_id = [1]

    def decide(item: Item, action: str, **kw: Any) -> None:
        sel.decisions[item.key] = Decision(key=item.key, action=action, **kw)

    scope = _Scope(platform)
    latest_dats, best_dats, m3u_dats = scope.latest, scope.best, scope.m3u

    # 1. eligibility (every DAT): exclusion rules; on the Games DAT also the keep-flags; on Games and
    #    No-Intro DATs also the language filter
    alive: list[Item] = []
    excluded: list[tuple[Item, list[tuple[str, str]]]] = []
    lang_only: dict[str, list[Item]] = {}    # excluded ONLY for language: may still be borrowed (see _Borrow)
    for it in items:
        info = eligibility(_rom_name(it), _style(it), profile, languages=it.dat in scope.language,
                           flags=scope.flags(it.dat),
                           parsed=_tags_of(it) if _style(it) in (STYLE_WHDLOAD, STYLE_REDUMP) else None)
        if info:
            excluded.append((it, info))
            if (profile.borrow_other_editions and it.dat in scope.best and _style(it) == STYLE_TOSEC
                    and all(code == LANGUAGE_CODE for code, _x in info)):
                lang_only.setdefault(it.dat, []).append(it)
        else:
            alive.append(it)
    # a game that has no version in the selected languages at all: keep what exists of it (No-Intro / Redump)
    rescued: dict[int, str] = {}
    if profile.keep_other_language and profile.languages:
        only_lang = [(it, info) for it, info in excluded
                     if _style(it) in OTHER_LANGUAGE_STYLES and all(code == LANGUAGE_CODE for code, _x in info)]
        if only_lang:
            known = lang_games if lang_games is not None else language_games(items, profile, platform)
            back = {it.key: info for it, info in only_lang if game_key(it) not in known}
            if back:
                excluded = [(it, info) for it, info in excluded if it.key not in back]
                for it, info in only_lang:
                    if it.key in back:
                        alive.append(it)
                        rescued[it.key] = (f"kept although it only exists in {info[0][1]}: no version in your languages exists")
    # keep-every-version DATs (opt-in): the only dump of a version is not excluded for [m] / [u]
    if profile.rescue_only_dump:
        kept_versions = {(it.dat, _release_key(it)) for it in alive
                         if it.dat not in latest_dats | best_dats and _style(it) == STYLE_TOSEC}
        still: list[tuple[Item, list[tuple[str, str]]]] = []
        for it, info in excluded:
            rules = {r for r, _x in info}
            if (it.dat not in latest_dats | best_dats and _style(it) == STYLE_TOSEC
                    and rules <= RESCUE_RULES and (it.dat, _release_key(it)) not in kept_versions):
                rescued[it.key] = "kept although " + _describe(info) + ": the only dump of this version"
                alive.append(it)
            else:
                still.append((it, info))
        excluded = still
    for it, info in excluded:
        codes = tuple(dict.fromkeys(r for r, _x in info))
        decide(it, EXCLUDED, codes=codes, detail=" ".join(x for _r, x in info),
               reason="excluded: " + _describe(info))

    by_dat: dict[str, list[Item]] = {}
    for it in sorted(alive, key=lambda i: (i.rom.name, i.key)):
        by_dat.setdefault(it.dat, []).append(it)

    for dat in sorted(by_dat):
        dat_items = by_dat[dat]
        if all(_style(i) == STYLE_REDUMP for i in dat_items):
            if dat in latest_dats and (profile.latest_only or (dat in scope.region and profile.one_per_game)):
                _select_redump(dat, dat_items, profile, dat in scope.region and profile.one_per_game, sel,
                               decide, next_id)
            else:
                for it in dat_items:
                    decide(it, KEEP)
        elif all(_style(i) == STYLE_WHDLOAD for i in dat_items):
            best = dat in best_dats and profile.best_variant
            latest = dat in latest_dats and profile.latest_only
            if best or latest:
                _select_whdload(dat_items, profile, best, sel, decide)
            else:
                for it in dat_items:
                    decide(it, KEEP)
        elif dat in latest_dats | best_dats | m3u_dats and all(_style(i) == STYLE_TOSEC for i in dat_items):
            _select_tosec(dat, dat_items, profile, dat in best_dats and profile.best_variant,
                          dat in latest_dats and profile.latest_only, dat in m3u_dats and profile.complete_only,
                          sel, decide, next_id, keep_all=dat not in latest_dats | best_dats,
                          borrow=(_Borrow(profile, [*dat_items, *lang_only.get(dat, ())])
                                  if profile.borrow_other_editions and dat in best_dats else None))
        elif dat in latest_dats and (profile.latest_only or (dat in scope.region and profile.one_per_game)):
            _select_nointro(dat, dat_items, profile, dat in scope.region and profile.one_per_game, sel, decide)
        else:
            for it in dat_items:
                decide(it, KEEP)
    for key, why in rescued.items():
        d = sel.decisions.get(key)
        if d is None:
            continue
        if d.action == INCOMPLETE:   # the only dump of a version: a playlist-less keep beats losing the version
            d.action, d.missing, d.elsewhere = KEEP, (), ()
        if d.action == KEEP:
            d.reason = why
    sel.incomplete = [x for x in sel.incomplete
                      if x.kept or any(sel.decisions[k].action == INCOMPLETE for k in x.present.values())]
    return sel



# --------------------------------------------------------------------------- the rating filter

def _stable(obj: Any) -> str:
    """Deterministic text of a game key (no set / hash ordering)."""
    if isinstance(obj, (set, frozenset)):
        return "{" + ",".join(sorted(_stable(x) for x in obj)) + "}"
    if isinstance(obj, (tuple, list)):
        return "(" + ",".join(_stable(x) for x in obj) + ")"
    return repr(obj)


def _rank_key(rating: float, votes: int, title: str, unit: tuple) -> tuple:
    """Order of the rated games: rating high to low, then votes, then title, then the game key (total order)."""
    return (-rating, -votes, title.casefold(), _stable(unit))


@dataclass
class _Unit:
    key: tuple
    owner: Item
    items: list[Item]
    title: str
    rated: Optional[tuple[float, int]]      # None = unrated (no match / fewer votes than ``min_votes``)
    complete: bool                          # no member is a partial Redump set (``Decision.missing``)

    def rank(self) -> tuple:
        assert self.rated is not None
        return _rank_key(self.rated[0], self.rated[1], self.title, self.key)


def _units(items: Sequence[Item], sel: Selection, platform: Any, profile: LibraryProfile,
           lookup: Callable[[str], Optional[tuple[float, int]]]) -> list[_Unit]:
    """The kept GAMES of the rated DATs (a game = :func:`game_key`; a disk borrowed from another edition belongs to the
    game of the set it completes) with their rating."""
    from . import ratings as _ratings

    rdats = set(_ratings.rated_dats(platform))
    by_key = {it.key: it for it in items}
    owner_of: dict[int, Item] = {}
    for cs in sel.sets:
        borrowed = {b["key"] for b in cs.borrowed.values()}
        own = [by_key[k] for _slot, k in sorted(cs.slots.items()) if k not in borrowed and k in by_key]
        if own:
            for k in cs.slots.values():
                owner_of[k] = own[0]
    units: dict[tuple, _Unit] = {}
    for it in items:
        d = sel.decisions.get(it.key)
        if d is None or d.action != KEEP or it.dat not in rdats:
            continue
        owner = owner_of.get(it.key, it)
        key = game_key(owner)
        u = units.get(key)
        if u is None:
            title = _tags_of(owner).title
            r = lookup(title)
            if r is not None and r[1] < profile.min_votes:
                r = None
            u = units[key] = _Unit(key, owner, [], title, r, True)
        u.items.append(it)
        if d.missing:
            u.complete = False
    return sorted(units.values(), key=lambda u: _stable(u.key))


def _cutoff(units: Sequence[_Unit], profile: LibraryProfile) -> tuple:
    """Rank key of the ``top_n``-th best complete, rated game at or above ``min_rating`` (``INF_KEY``: fewer)."""
    if profile.top_n is None:
        return INF_KEY
    pool = sorted(u.rank() for u in units
                  if u.rated is not None and u.complete
                  and (profile.min_rating is None or u.rated[0] >= profile.min_rating))
    # (cut at the rating / votes / title of the N-th game: every game of that title stays or goes together)
    return pool[profile.top_n - 1][:3] if len(pool) >= profile.top_n else INF_KEY


def target_cutoff(target_items: Sequence[Item], profile: LibraryProfile, platform: Any,
                  lookup: Callable[[str], Optional[tuple[float, int]]],
                  base: Optional[Selection] = None) -> tuple:
    """The cutoff of ``rank_scope == "dat"``: the rank key of the N-th best game of the whole DAT target set (the games
    every OTHER rule keeps when every rom is present). ``base``: that selection when the caller already has it."""
    if profile.top_n is None:
        return INF_KEY
    if base is None:
        base = _select_base(target_items, profile, platform)
    return _cutoff(_units(target_items, base, platform, profile, lookup), profile)


def rating_coverage(items: Sequence[Item], sel: Selection, platform: Any, profile: LibraryProfile,
                    lookup: Callable[[str], Optional[tuple[float, int]]]) -> dict[str, Any]:
    """How many of the games ``sel`` keeps (before any rating filter) have a usable rating:
    ``{"games", "rated", "ge": [n for rating >= 0.5, 1.0 ... 10.0]}`` (``ge`` drives the live "N games >= x" hint)."""
    units = _units(items, sel, platform, profile, lookup)
    ratings = [u.rated[0] for u in units if u.rated is not None]
    return {"games": len(units), "rated": len(ratings),
            "ge": [sum(1 for r in ratings if r >= k / 2.0) for k in range(1, 21)]}


def apply_ratings(sel: Selection, items: Sequence[Item], profile: LibraryProfile, platform: Any,
                  ctx: RatingContext) -> None:
    """The rating filter, AFTER every other rule, per GAME (all variants / disks / discs of a game share its rating).

    * rated = the title matches LaunchBox and has at least ``min_votes`` votes; everything else is unrated.
    * ``min_rating``: a rated game below it gets ``rating_low``.
    * ``top_n``: a rated game ranked after the N-th best (rating, votes, title) gets ``rating_not_top``; games with the
      same title as the N-th stay or go together, so "top 300" can keep a few more than 300. The N-th best
      is searched among the games at or above ``min_rating`` of the whole DAT target set (``rank_scope == "dat"``,
      ``ctx.cutoff``) or of the games kept here (``"owned"``).
    * a game without a usable rating gets ``rating_unrated`` unless ``keep_unrated`` (then it is kept IN ADDITION).
    Excluded games turn every kept file into ``excluded`` and their playlists / incomplete reports vanish.
    Summary in ``sel.rating``."""
    units = _units(items, sel, platform, profile, ctx.lookup)
    if profile.top_n is None:
        cutoff = INF_KEY
    elif profile.rank_scope == "dat":
        if ctx.cutoff is None:
            raise RatingsUnavailable("top N against the whole DAT needs the target cutoff (see target_cutoff)")
        cutoff = ctx.cutoff
    else:
        cutoff = _cutoff(units, profile)
    dropped: dict[str, int] = {}
    kept_games = rated_games = 0
    gone: set[int] = set()
    for u in units:
        codes: list[str] = []
        if u.rated is None:
            if not profile.keep_unrated:
                codes.append(RATING_UNRATED)
            detail = "no usable rating"
        else:
            rated_games += 1
            r, v = u.rated
            detail = f"rated {r:.1f} ({v} vote{'' if v == 1 else 's'})"
            if profile.min_rating is not None and r < profile.min_rating:
                codes.append(RATING_LOW)
            if profile.top_n is not None and u.rank()[:3] > cutoff:
                codes.append(RATING_NOT_TOP)
        if not codes:
            kept_games += 1
            continue
        dropped[codes[0]] = dropped.get(codes[0], 0) + 1
        for it in u.items:
            sel.decisions[it.key] = Decision(key=it.key, action=EXCLUDED, codes=tuple(codes), detail=detail,
                                             reason=f"excluded: {_describe([(c, '') for c in codes]).strip()} ({detail})")
            gone.add(it.key)
    if gone:
        sel.sets = [cs for cs in sel.sets if not (set(cs.slots.values()) & gone)]
        sel.incomplete = [x for x in sel.incomplete if not (x.kept and set(x.present.values()) <= gone)]
    sel.rating = {"min_rating": profile.min_rating, "top_n": profile.top_n, "min_votes": profile.min_votes,
                  "keep_unrated": profile.keep_unrated, "rank_scope": profile.rank_scope,
                  "games": len(units), "rated": rated_games, "unrated": len(units) - rated_games,
                  "kept": kept_games, "excluded": len(units) - kept_games, "excluded_by": dropped}

def _vanished(items: Sequence[Item], sel: Selection) -> list[Vanished]:
    """Titles of which no local variant is kept, with the reason (language first, then flags, rules)."""
    groups: dict[tuple, list[Item]] = {}
    for it in items:
        groups.setdefault((it.dat, tags.title_key(_tags_of(it))), []).append(it)
    out: list[Vanished] = []
    # (a rating code only ever hits a variant that passed every other rule: it is the real blocker, so it ranks first)
    priority = {c: i for i, c in enumerate(RATING_CODES + (LANGUAGE_CODE,) + FLAG_CODES + RULES + (INCOMPLETE,))}
    for (dat, _k), members in groups.items():
        decs = [sel.decisions.get(m.key) for m in members]
        if any(d is None or d.action == KEEP for d in decs):
            continue
        blockers: list[tuple[str, ...]] = []
        for d in decs:
            if d is None:
                continue
            blockers.append(tuple(d.codes) if d.action == EXCLUDED and d.codes else (d.action,))
        fewest = min(len(b) for b in blockers)
        codes = sorted({c for b in blockers if len(b) == fewest for c in b},
                       key=lambda c: priority.get(c, 99))
        # the file closest to being kept names the title
        best = min(range(len(members)), key=lambda i: (len(blockers[i]), members[i].rom.name.casefold()))
        t = _tags_of(members[best])
        langs: tuple[str, ...] = ()
        if codes[0] == LANGUAGE_CODE:
            seen: set[str] = set()
            for m in members:
                seen |= tags.variant_languages(_tags_of(m))
            langs = tuple(sorted(seen, key=lambda c: (c != "En", c)))
        bd = sel.decisions.get(members[best].key)
        out.append(Vanished(dat=dat, title=t.title, name=members[best].rom.name, reason=codes[0],
                            codes=tuple(codes), languages=langs, variants=len(members),
                            detail=bd.detail if bd is not None and codes[0] in RATING_CODES else ""))
    out.sort(key=lambda v: (v.dat, v.title.casefold(), v.name))
    return out


def _whd_vkeys(group: list[Item]) -> dict[int, tuple]:
    """Version key per item, comparable inside one game (zero-padded ``v1.02`` read as a fraction)."""
    versions = {it.key: _tags_of(it).version for it in group}
    widths = tags._fraction_widths(versions.values())
    if not widths:
        return {it.key: _tags_of(it).version_key for it in group}
    return {k: tags._version_key(v, widths) for k, v in versions.items()}


def _whd_variant(t: tags.Tags) -> tuple:
    """The variant signature of a WHDLoad archive: language set, chipset tags, memory tags, video mode."""
    return (tuple(sorted(t.languages)), tuple(sorted(tags.chipset(t))),
            tuple(sorted({tags._whd_memory(f) for f in t.flags if tags._whd_memory(f)})),
            "NTSC" in t.video)


def _select_whdload(dat_items: list[Item], profile: LibraryProfile, best: bool, sel: Selection,
                    decide: Any) -> None:
    """WHDLoad: games = ``tags.identity_key`` (title + status + product tags). ``best``: keep ONE variant per
    game - best selected language, then platform (CD32 > AGA > OCS), then standard memory over low-memory
    builds, then PAL / untagged over NTSC, then the newest version, then the highest build number, then the
    name. Otherwise (latest only): keep the newest version of every variant (same language set, chipset,
    memory tags and video mode); older ones are superseded. Nothing of the TOSEC Amiga rules is involved."""
    groups: dict[tuple, list[Item]] = {}
    for it in dat_items:
        groups.setdefault(tags.identity_key(_tags_of(it)), []).append(it)
    for group in groups.values():
        vkeys = _whd_vkeys(group)
        info = {}
        for it in group:
            t = _tags_of(it)
            info[it.key] = (_lang_rank(tags.variant_languages(t), profile), tags.platform_rank(t),
                            tags.whd_memory_rank(t), 1 if "NTSC" in t.video else 0)
        if best:
            pool = group
            for idx in range(4):
                low = min(info[i.key][idx] for i in pool)
                pool = [i for i in pool if info[i.key][idx] == low]
            newest = max(vkeys[i.key] for i in pool)
            pool = [i for i in pool if vkeys[i.key] == newest]
            top = max(_tags_of(i).build for i in pool)
            pool = [i for i in pool if _tags_of(i).build == top]
            winner = min(pool, key=lambda i: (_rom_name(i).casefold(), _rom_name(i), i.key))
            wname = _rom_name(winner)
            for it in group:
                if it.key == winner.key or _rom_name(it) == wname:
                    decide(it, KEEP)
                else:
                    decide(it, SUPERSEDED, superseded_by=wname,
                           reason=f"one best variant per game: kept {wname} ({_whd_why(it, winner, info, vkeys)})")
            continue
        by_variant: dict[tuple, list[Item]] = {}
        for it in group:
            by_variant.setdefault(_whd_variant(_tags_of(it)), []).append(it)
        for members in by_variant.values():
            top = max((vkeys[i.key], _tags_of(i).build) for i in members)
            cand = [i for i in members if (vkeys[i.key], _tags_of(i).build) == top]
            winner = min(cand, key=lambda i: (_rom_name(i).casefold(), _rom_name(i), i.key))
            wname = _rom_name(winner)
            for it in members:
                if it.key == winner.key or _rom_name(it) == wname:
                    decide(it, KEEP)
                else:
                    decide(it, SUPERSEDED, superseded_by=wname,
                           reason=f"superseded by {wname} (newer version of the same variant)")


def _whd_why(it: Item, winner: Item, info: dict[int, tuple], vkeys: dict[int, tuple]) -> str:
    """The first ranking criterion on which ``winner`` beats ``it`` (for the preview text)."""
    a, b = info[it.key], info[winner.key]
    for idx, text in enumerate(("preferred language", "better platform (CD32 > AGA > OCS)",
                                "standard memory", "PAL instead of NTSC")):
        if a[idx] != b[idx]:
            return text
    if vkeys[it.key] != vkeys[winner.key]:
        return "newer version"
    if _tags_of(it).build != _tags_of(winner).build:
        return "higher build number"
    return "name order"


def _select_nointro(dat: str, dat_items: list[Item], profile: LibraryProfile, one_per_game: bool,
                    sel: Selection, decide: Any) -> None:
    by_form: dict[str, list[Item]] = {}
    for it in dat_items:
        by_form.setdefault(it.form, []).append(it)
    order = tags.region_order(profile.region_priority)
    for form in sorted(by_form):
        members = by_form[form]
        if one_per_game:
            _one_per_game(members, profile, order, decide)
            continue
        names = list(dict.fromkeys(_rom_name(i) for i in members))
        older = tags.superseded(names, STYLE_NOINTRO)
        for it in members:
            newer = older.get(_rom_name(it))
            if newer:
                decide(it, SUPERSEDED, superseded_by=newer,
                       reason=f"superseded by {newer} (newer version of the same region)")
            else:
                decide(it, KEEP)


def _select_redump(dat: str, dat_items: list[Item], profile: LibraryProfile, one_per_game: bool,
                   sel: Selection, decide: Any, next_id: list[int]) -> None:
    """Redump (Sega Dreamcast): ONE release per game, discs of a game always stay together.

    A *game* = ``tags.redump_game_key`` (title + status + product tags; the disc number is not part of
    it). Its *editions* are the (regions, explicit languages) groups; every disc of an edition belongs to
    that edition. Per game the best edition is kept (selected language order, region priority, newest
    revision, fewest extra tags, name) with ALL its discs - for every disc number the newest revision
    of that edition; everything else is ``superseded``. With ``one_per_game`` off every edition is kept
    (still only the newest revision of each disc). A kept multi-disc edition becomes a :class:`ChosenSet`
    (a playlist) when all of its discs (``Item.disc_total`` or the highest disc number) are present,
    else an :class:`IncompleteSet` that is only reported (the discs stay).
    """
    order = tags.region_order(profile.region_priority)
    games: dict[tuple, list[Item]] = {}
    for it in dat_items:
        games.setdefault(tags.game_key(_tags_of(it)), []).append(it)
    for group in games.values():
        names = list(dict.fromkeys(_rom_name(i) for i in group))
        vkeys = tags.group_version_keys(names, STYLE_REDUMP)
        editions: dict[tuple, list[Item]] = {}
        for it in group:
            t = _tags_of(it)
            editions.setdefault((t.regions, () if t.languages_implied else t.languages), []).append(it)

        def first_of(members: list[Item]) -> Item:
            return min(members, key=lambda i: (tags.disc_number(_tags_of(i)), _rom_name(i)))

        def newest_of(members: list[Item]) -> tuple:
            return max(vkeys[_rom_name(i)] for i in members)

        if one_per_game:
            # best edition: selected language, region priority, then the newest revision (over its
            # discs), fewest extra tags, name
            def lr(members: list[Item]) -> tuple:
                t = _tags_of(first_of(members))
                return (_lang_rank(tags.variant_languages(t), profile), tags.region_rank(t.regions, order))

            best = min(lr(m) for m in editions.values())
            pool = {k: m for k, m in editions.items() if lr(m) == best}
            newest = max(newest_of(m) for m in pool.values())
            pool = {k: m for k, m in pool.items() if newest_of(m) == newest}
            winner_key = min(pool, key=lambda k: (tags.extra_tag_count(_tags_of(first_of(pool[k]))),
                                                  _rom_name(first_of(pool[k])).casefold(),
                                                  _rom_name(first_of(pool[k])), str(k)))
            kept_editions = {winner_key: editions[winner_key]}
        else:
            kept_editions = editions
        winner_name = ""
        for key, members in kept_editions.items():
            # newest revision of every disc number
            by_disc: dict[int, list[Item]] = {}
            for it in members:
                by_disc.setdefault(tags.disc_number(_tags_of(it)), []).append(it)
            chosen: dict[int, Item] = {}
            for disc, cands in by_disc.items():
                chosen[disc] = max(cands, key=lambda i: (vkeys[_rom_name(i)],
                                                         tuple(-ord(c) for c in _rom_name(i).casefold())))
            for disc, cands in by_disc.items():
                win = chosen[disc]
                for it in cands:
                    if it.key == win.key or _rom_name(it) == _rom_name(win):
                        decide(it, KEEP)
                    else:
                        decide(it, SUPERSEDED, superseded_by=_rom_name(win),
                               reason=f"older revision: kept {_rom_name(win)}")
            winner_name = winner_name or _rom_name(next(iter(chosen.values())))
            discs = sorted(d for d in chosen if d > 0)
            if discs:
                total = max([it.disc_total for it in chosen.values()] + [discs[-1]])
                first = chosen[discs[0]]
                stem = _disc_free_name(_rom_name(first))
                if discs == list(range(1, total + 1)) and total >= 2:
                    sel.sets.append(ChosenSet(
                        id=next_id[0], dat=dat, name=stem, total=total,
                        slots={d: chosen[d].key for d in discs}, labels={d: f"Disc {d}" for d in discs},
                        flags=(), cracked=False))
                    for d in discs:
                        sel.decisions[chosen[d].key].set_id = next_id[0]
                    next_id[0] += 1
                elif total >= 2:
                    miss = tuple(d for d in range(1, total + 1) if d not in chosen)
                    sel.incomplete.append(IncompleteSet(
                        dat=dat, name=stem, total=total, present={d: chosen[d].key for d in discs},
                        missing=miss, kept=True))
                    for d in discs:
                        dec = sel.decisions[chosen[d].key]
                        dec.missing = miss
                        dec.reason = ("incomplete set (missing disc " + ", ".join(str(m) for m in miss)
                                      + ") - kept, no playlist")
        if one_per_game:
            for key, members in editions.items():
                if key in kept_editions:
                    continue
                for it in members:
                    decide(it, SUPERSEDED, superseded_by=winner_name,
                           reason=f"one version per game: kept {winner_name}")


_DISC_TOKEN_RE = re.compile(r"\s*\(Dis[ck] (?:\d+|[A-Z])\)")


def _disc_free_name(name: str) -> str:
    """``"Skies of Arcadia (USA) (Disc 1)"`` -> ``"Skies of Arcadia (USA)"`` (the playlist's name)."""
    return _DISC_TOKEN_RE.sub("", name).strip()


def _one_per_game(members: list[Item], profile: LibraryProfile, order: list[str], decide: Any) -> None:
    """Keep ONE variant per game: best selected language, best region (``order``), newest version of
    that region, fewest extra tags, then name."""
    games: dict[tuple, list[Item]] = {}
    for it in members:
        games.setdefault(tags.game_key(_tags_of(it)), []).append(it)
    for group in games.values():
        names = list(dict.fromkeys(_rom_name(i) for i in group))
        vkeys = tags.group_version_keys(names, STYLE_NOINTRO)
        info = {}
        for it in group:
            t = _tags_of(it)
            info[it.key] = (_lang_rank(tags.variant_languages(t), profile),
                            tags.region_rank(t.regions, order))
        pool = group
        for idx in (0, 1):
            best = min(info[i.key][idx] for i in pool)
            pool = [i for i in pool if info[i.key][idx] == best]
        newest = max(vkeys[_rom_name(i)] for i in pool)
        pool = [i for i in pool if vkeys[_rom_name(i)] == newest]
        winner = min(pool, key=lambda i: (tags.extra_tag_count(_tags_of(i)), _rom_name(i).casefold(),
                                          _rom_name(i), i.key))
        wname = _rom_name(winner)
        for it in group:
            if it.key == winner.key or _rom_name(it) == wname:
                decide(it, KEEP)
            else:
                decide(it, SUPERSEDED, superseded_by=wname,
                       reason=f"one version per game: kept {wname}")


def _select_tosec(dat: str, dat_items: list[Item], profile: LibraryProfile, best: bool, latest: bool,
                  complete_only: bool, sel: Selection, decide: Any, next_id: list[int],
                  keep_all: bool = False, borrow: Optional[_Borrow] = None) -> None:
    """``keep_all``: a keep-every-version DAT (Kickstart-Disks, Workbench, Firmware). ``borrow``: complete
    sets with disks of other editions (the borrowed files are made ``keep`` after every group is decided)."""
    borrowed_uses: list[tuple[int, int, str, int, dict[str, Any]]] = []
    groups: dict[tuple, list[Item]] = {}
    for it in dat_items:
        groups.setdefault(tags.identity_key(tags.parse_name(it.rom.name, STYLE_TOSEC)), []).append(it)
    # disks of one title + publisher + disk total, whatever their country / language / edition
    # (only used to tell the user that a missing disk exists under another identity)
    pool: dict[tuple, dict[int, set[tuple]]] = {}
    seen_keys: set[int] = set()
    for gkey, members in groups.items():
        for it in members:
            seen_keys.add(it.key)
            t = tags.parse_name(it.rom.name, STYLE_TOSEC)
            d = _disk_info(t)
            if d is not None:
                pool.setdefault((t.title.casefold(), t.publisher.casefold(), d[1]), {}) \
                    .setdefault(d[0], set()).add(gkey)
    if borrow is not None:        # files excluded only by the language filter are borrowable too
        for key, t in borrow.tags.items():
            if key not in seen_keys:
                d = _disk_info(t)
                pool.setdefault((t.title.casefold(), t.publisher.casefold(), d[1]), {}) \
                    .setdefault(d[0], set()).add(tags.identity_key(t))
    for gkey in sorted(groups, key=lambda k: sorted(i.rom.name for i in groups[k])[0]):
        group = groups[gkey]
        vkeys = tags.group_version_keys([i.rom.name for i in group], STYLE_TOSEC)
        complete, partial = _build_sets(group, vkeys, profile, borrow)
        kept_sets: list[_Set]
        if not complete:
            kept_sets = []
        elif best:
            kept_sets = [_pick_best(complete)]
        elif latest:
            by_sig: dict[tuple, list[_Set]] = {}
            for s in complete:
                by_sig.setdefault((s.flags, s.part), []).append(s)
            kept_sets = [_pick_best_latest(v) for _k, v in sorted(by_sig.items())]
        else:
            kept_sets = list(complete)
        collapse = (best or latest) and bool(complete)       # leftovers of a group with a kept set go
        in_complete = {it.key for s in complete for it in s.slots.values()}
        kept_keys: dict[int, Optional[int]] = {}
        for s in sorted(kept_sets, key=lambda s: (s.name.casefold(), s.name, s.refs)):
            sid: Optional[int] = None
            if s.total >= 2:
                sid = next_id[0]
                next_id[0] += 1
                sel.sets.append(ChosenSet(
                    id=sid, dat=dat, name=s.name, total=s.total,
                    slots={i: it.key for i, it in sorted(s.slots.items())}, labels=dict(s.labels),
                    flags=s.flags, cracked=s.cracked, borrowed=dict(s.borrowed)))
                for slot, note in sorted(s.borrowed.items()):
                    borrowed_uses.append((note["key"], sid, s.name, slot, note))
            for it in s.slots.values():
                kept_keys.setdefault(it.key, sid)
        if not complete and not complete_only:
            kept_all = True       # nothing complete and no completeness rule: leave the group alone
        else:
            kept_all = False
        incomplete_keys: dict[int, tuple[int, ...]] = {}
        for it in sorted(group, key=lambda i: (i.rom.name, i.key)):
            if it.key in kept_keys:
                decide(it, KEEP, set_id=kept_keys[it.key])
            elif kept_all:
                decide(it, KEEP)
            elif it.key in in_complete:
                if collapse:
                    winner = _winner_name(kept_sets)
                    decide(it, SUPERSEDED, superseded_by=winner,
                           reason=f"superseded by {winner}")
                else:
                    decide(it, KEEP)
            elif collapse:       # a disk of no complete set, in a game that has one: not part of the best set
                winner = _winner_name(kept_sets)
                decide(it, SUPERSEDED, superseded_by=winner, reason=f"superseded by {winner}")
            elif complete_only and _disk_info(tags.parse_name(it.rom.name, STYLE_TOSEC)) is not None:
                missing = _missing_for(it, partial, group)
                if keep_all and complete:   # every version stays: a spare disk of a game with a complete set
                    decide(it, KEEP)
                    continue
                if not missing:
                    if complete:      # a spare disk next to kept complete sets (keep-every-version DATs)
                        decide(it, KEEP)
                        continue
                    own = _disk_info(tags.parse_name(it.rom.name, STYLE_TOSEC)) or (0, 0)
                    missing = tuple(i for i in range(1, own[1] + 1) if i != own[0])  # no set could be formed
                incomplete_keys[it.key] = missing
                t = tags.parse_name(it.rom.name, STYLE_TOSEC)
                total = (_disk_info(t) or (0, 0))[1]
                other = pool.get((t.title.casefold(), t.publisher.casefold(), total), {})
                elsewhere = tuple(m for m in missing if other.get(m, set()) - {gkey})
                text = "incomplete set: missing disk " + ", ".join(str(m) for m in missing)
                if elsewhere and borrow is not None:
                    text += (f" (disk {', '.join(str(m) for m in elsewhere)} exists under another edition but "
                             "cannot be borrowed: chipset, dump flags, status or the other edition's own "
                             "disks do not fit)")
                elif elsewhere:
                    text += (f" (disk {', '.join(str(m) for m in elsewhere)} exists under a different "
                             "country / language / edition - not mixed in)")
                decide(it, INCOMPLETE, missing=missing, elsewhere=elsewhere, reason=text)
            else:
                decide(it, KEEP)
        if incomplete_keys:
            for s in partial:
                if any(it.key in incomplete_keys for it in s.slots.values()):
                    sel.incomplete.append(IncompleteSet(
                        dat=dat, name=s.name, total=s.total,
                        present={i: it.key for i, it in sorted(s.slots.items())}, missing=s.missing))
    # A borrowed disk belongs to ANOTHER group (its own decision - language-excluded, superseded, incomplete -
    # was made independently): it stays, as part of the set it completes. Never superseded / excluded / moved.
    for key, sid, set_name, slot, note in borrowed_uses:
        text = f"borrowed as disk {slot} of {set_name} ({note['text']})"
        d = sel.decisions.get(key)
        if d is None or d.action != KEEP:
            sel.decisions[key] = Decision(key=key, action=KEEP, codes=(BORROWED_CODE,), set_id=sid, reason=text)
        else:
            if BORROWED_CODE not in d.codes:
                d.codes = tuple(d.codes) + (BORROWED_CODE,)
            if d.set_id is None:
                d.set_id = sid
            d.reason = (d.reason + "; " if d.reason else "") + text


def _pick_best_latest(sets: list[_Set]) -> _Set:
    newest = max(_newer(s) for s in sets)
    pool = [s for s in sets if _newer(s) == newest]
    return min(pool, key=lambda s: (s.anchor.rom.name.casefold(), s.anchor.rom.name, s.refs))


def _winner_name(kept: list[_Set]) -> str:
    if not kept:
        return ""
    return min(kept, key=lambda s: (s.name.casefold(), s.name)).name


def _missing_for(it: Item, partial: list[_Set], group: list[Item]) -> tuple[int, ...]:
    """Missing disk numbers of the least incomplete partial set containing ``it``."""
    mine = [s for s in partial if any(i.key == it.key for i in s.slots.values())]
    if mine:
        return min(mine, key=lambda s: (len(s.missing), s.missing)).missing
    from . import m3u

    own = m3u.parse_disk_name(it.rom.name)
    if own is None:
        return ()
    have = {own.index}
    variant = frozenset(own.flags)
    for g in group:
        other = m3u.parse_disk_name(g.rom.name)
        if g.key != it.key and other is not None and other.total == own.total \
                and m3u._compat(other.flags, variant) is not None:
            have.add(other.index)     # a disk that fits this disk's dump variant
    return tuple(i for i in range(1, own.total + 1) if i not in have)


# --------------------------------------------------------------------------- UI metadata

def _style_of_platform(platform: Any) -> str:
    src = getattr(platform, "source", "")
    src = getattr(src, "value", src)
    if src == "whdload":
        return STYLE_WHDLOAD
    if src == "redump":
        return STYLE_REDUMP
    return STYLE_NOINTRO if src == "nointro" else STYLE_TOSEC


_BOTH = [STYLE_TOSEC, STYLE_NOINTRO]
_ALL = [STYLE_TOSEC, STYLE_NOINTRO, STYLE_WHDLOAD, STYLE_REDUMP]
# Descriptions that differ for the WHDLoad system (shown instead of the generic ones).
_WHD_DESCRIPTIONS = {
    "pre_release": "Beta, Pre Release and Preview builds (the database also marks many unfinished hacks and "
                   "ports as (Beta)).",
    "demo": "Game demos and demo-only releases: (Game Demo), (Demo), (Playable Demo).",
    "unreleased": "Games that were never released: (Unreleased).",
    "latest_only": "Keep only the newest version of every variant (same game, language, chipset, memory "
                   "build and video mode); older versions go to _superseded.",
    "best_variant": "Keep ONE archive per game: your language order first, then the platform "
                    "(CD32 > AGA > OCS), then standard memory over 512KB / Low Mem builds, then PAL / untagged "
                    "over NTSC, then the newest version (highest build number last). Different products "
                    "(Two Disk / One Disk installs, Image / Files, Demos, Cover Disks, Hacks, CDTV) are never merged.",
    "languages": "Keep only archives playable in the selected languages (first = preferred). An archive "
                 "with no language tag counts as English; (German) / _De = German; a multi-language archive "
                 "(En,Fr,De) counts for each of its languages.",
}
_REDUMP_DESCRIPTIONS = {
    "pre_release": "Betas ((Beta)). Redump files most of them under the Preproduction category.",
    "prototype": "Prototypes ((Proto)) and every disc Redump files under the Preproduction category.",
    "demo": "Demos, samples, Japanese trial discs ((Taikenban), (Tentou Taikenban), (Tentou-you Demo ...)) and "
            "every disc Redump files under the Demos or Coverdiscs categories.",
}
_RULE_DESCRIPTIONS = {
    "bad_dump": "Dumps marked as bad or corrupt. Never useful.",
    "virus": "Disks that carry a virus.",
    "bad_size": "Over- or under-sized dumps.",
    "pre_release": "Betas, alphas, previews and debug builds.",
    "prototype": "Prototypes, never officially released.",
    "demo": "Demos, samples, kiosk and slideshow disks.",
    "faked": "Fake releases (a release pretending to be something else).",
    "unreleased": "Games that were never released.",
    "modified": "Disks modified from the original dump (a common reason a Workbench disk is listed).",
}
_FLAG_DESCRIPTIONS = {
    "cr": "Cracked copies (trainer-free loaders, no copy protection). Cracked variants rank first. "
          "Switching this off drops roughly a third of the games, because they exist only as cracks.",
    "h": "Hacked copies (modified by a scene group). Ranked below clean variants.",
    "t": "Trainers (cheats such as infinite lives). Ranked below clean variants.",
    "a": "Alternate dumps of the same release. Ranked below the main dump.",
    "f": "Fixed copies (bug fixes by a third party).",
    "tr": "Translations. A [tr de] flag adds German, [tr en] adds English.",
}


def rule_catalog(platform_style: str = STYLE_TOSEC) -> list[dict[str, Any]]:
    """Every rule / option of the Build library panel for one DAT style (``"tosec"`` | ``"nointro"``).

    Ordered list of ``{id, field, label, kind, default, tokens, description, applies_to}``:

    * ``kind == "exclude"``: an exclusion rule (``id`` is a ``tags.RULES`` key; ``field`` is
      ``"exclude"``; ``default`` True = excluded by default). ``tokens`` are the exact TOSEC /
      No-Intro tokens (``"(pre-release)"``, ``"[b ...]"``: ``" ..."`` = free text) generated from the
      tables ``tags.classify_token`` uses.
    * ``kind == "keep_flag"``: a flag type that may stay (``id`` is ``cr h t a f tr``; ``field`` is
      ``"keep_flags"``; ``default`` True = kept). Unticked = variants with that flag are excluded
      (reason code ``flag_<id>``).
    * ``kind == "option"``: ``latest_only``, ``best_variant``, ``complete_only``, ``rescue``
      (field ``rescue_only_dump``), ``one_per_game``, ``languages`` and ``region_priority`` (these two
      carry ``default_value`` lists; their ``default`` says whether the filter is on).

    Only entries that apply to ``platform_style`` are returned (``applies_to`` lists the styles)."""
    out: list[dict[str, Any]] = []
    per_style = {st: tags.rule_tokens(st) for st in _ALL}
    whd = platform_style == STYLE_WHDLOAD
    redump = platform_style == STYLE_REDUMP
    for rule in RULES:
        applies = [st for st in _ALL if per_style[st][rule]]
        # list tokens of the requested style (the TOSEC list is the superset)
        toks = per_style.get(platform_style, per_style[STYLE_TOSEC])[rule]
        out.append({"id": rule, "field": "exclude", "label": tags.RULE_LABELS[rule], "kind": "exclude",
                    "default": True, "tokens": list(toks),
                    "description": ((_WHD_DESCRIPTIONS.get(rule) if whd else None)
                                    or (_REDUMP_DESCRIPTIONS.get(rule) if redump else None)
                                    or _RULE_DESCRIPTIONS[rule]),
                    "applies_to": applies})
    for code in tags.KEEP_FLAGS:
        out.append({"id": code, "field": "keep_flags", "label": tags.KEEP_FLAG_LABELS[code],
                    "kind": "keep_flag", "default": True, "tokens": [f"[{code}]", f"[{code} ...]"],
                    "description": _FLAG_DESCRIPTIONS[code], "applies_to": [STYLE_TOSEC]})

    def opt(id_: str, field_: str, label: str, default: bool, desc: str, applies: list[str],
            default_value: Any = None) -> None:
        d: dict[str, Any] = {"id": id_, "field": field_, "label": label, "kind": "option",
                             "default": default, "tokens": [], "description": desc, "applies_to": applies}
        if default_value is not None:
            d["default_value"] = default_value
        out.append(d)

    opt("latest_only", "latest_only", "Latest versions only", True,
        _WHD_DESCRIPTIONS["latest_only"] if whd else
        "Keep only the newest version of a release (older ones go to _superseded).", _ALL)
    opt("best_variant", "best_variant", "One best variant per game", True,
        _WHD_DESCRIPTIONS["best_variant"] if whd else
        "Keep ONE variant per game: preferred language, then cracked, then best platform "
        "(CD32 > AGA > OCS), then newest, then fewest modifications.", [STYLE_TOSEC, STYLE_WHDLOAD])
    opt("one_per_game", "one_per_game", "One version per game", True,
        "Keep ONE version per game: preferred language, then the best region (see region priority), "
        "then the newest revision, then the fewest extra tags. Off: the latest version of every region. "
        "Multi-disc games keep ALL discs of the chosen release.",
        [STYLE_NOINTRO, STYLE_REDUMP])
    opt("complete_only", "complete_only", "Complete multi-disk sets only", True,
        "Multi-disk games need every disk; incomplete ones go to _incomplete.", [STYLE_TOSEC])
    opt("borrow_editions", "borrow_other_editions", "Complete sets with disks from other editions", True,
        "A multi-disk game that lacks a disk in your edition is completed with that disk from another edition "
        "you own (matched by checksum): only the country, language, edition flags, version and year may "
        "differ. The title, publisher and disk count ('Disk N of M') must be the same, the chipset (OCS / AGA / "
        "CD32) must fit, and the disk must pass every rule above (bad dumps, viruses, pre-releases, prototypes, "
        "demos, faked, unreleased, modified, size problems are never borrowed). The disk may be in a language "
        "you did not select; it is kept as part of the set and the playlist says where it came from. "
        "Off: only disks of one edition form a set.", [STYLE_TOSEC])
    opt("rescue", "rescue_only_dump", "Keep the only dump of an OS version", False,
        "Workbench / Kickstart disks: keep a disk that is excluded only because of [m], [o] or [u] "
        "when it is the sole dump of its version.", [STYLE_TOSEC])
    opt("languages", "languages", "Languages", True,
        _WHD_DESCRIPTIONS["languages"] if whd else
        "Keep only releases playable in the selected languages (first = preferred). A release with no "
        "language and no country tag counts as English; a country implies its language; "
        "(de-en) counts as both.", _ALL, default_value=["En"])
    opt("other_language", "keep_other_language", "Keep games that exist only in other languages", True,
        "A game with no version in your languages anywhere in the database (for example a Japan-only release) is kept "
        "instead of left out. As soon as a version in one of your languages exists, only that one is kept.",
        [STYLE_NOINTRO, STYLE_REDUMP])
    opt("region_priority", "region_priority", "Region priority", True,
        "Best region first. When one version per game is kept, the first region listed here wins "
        "(unlisted regions follow in alphabetical order).", [STYLE_NOINTRO, STYLE_REDUMP],
        default_value=list(tags.DEFAULT_REGION_PRIORITY))
    # ---- Ratings group (Amendment 18). Numbers / a choice, rendered by the UI from these entries only.
    def rating(id_: str, label: str, kind: str, desc: str, **extra: Any) -> None:
        out.append({"id": id_, "field": extra.pop("field", id_), "label": label, "kind": kind, "group": "ratings",
                    "tokens": [], "description": desc, "applies_to": list(_ALL), **extra})

    rating("min_rating", "Minimum rating", "number",
           "Leave out games rated below this (0-10, LaunchBox community rating). Empty = off. Games with no usable "
           "rating are left out too unless 'Keep unrated games' is ticked.",
           default=None, min=0.5, max=10, step=0.5, unit="/10", filter=True, summary="rated \u2265 {v}",
           hint="coverage_ge")
    rating("top_n", "Top N games", "number",
           "Keep only the N best-rated games (rating, then votes, then title). Empty = off.",
           default=None, min=1, max=100000, step=1, unit="games", integer=True, filter=True, summary="top {v}")
    rating("min_votes", "Minimum votes", "number",
           f"A game with fewer votes than this counts as unrated (default {DEFAULT_MIN_VOTES}).",
           default=DEFAULT_MIN_VOTES, min=1, max=100000, step=1, unit="votes", integer=True,
           summary="min {v} votes", summary_when_filter=True)
    rating("keep_unrated", "Keep unrated games", "option",
           "While a rating filter is set, games without a usable rating are left out. Tick to keep them in addition "
           "to the rated games that pass.", default=False, summary="keep unrated", summary_when_filter=True)
    rating("rank_scope", "Rank against", "choice",
           "Whole DAT: the top N is taken from every game the other rules keep for the whole DAT, so adding files "
           "never pushes others out and totals read 'have K of N'. Only my games: rank among the games you own.",
           default="dat", choices=[{"value": "dat", "label": "The whole DAT"}, {"value": "owned", "label": "Only my games"}],
           summary_when_filter=True)
    return [e for e in out if platform_style in e["applies_to"]]


def _with_dat(rom: Any, dat_name: str) -> Any:
    """``rom`` with its ``dat`` filled in (roms of a DatFile whose own ``dat`` is empty)."""
    try:
        return replace(rom, dat=dat_name)
    except TypeError:
        return rom


def available_regions(dats: Iterable[Any]) -> dict[str, int]:
    """``{region: games}`` over the DATs (one count per game / set, whatever its dump flags): the regions that exist in the data,
    each under its canonical name."""
    counts: dict[str, int] = {}
    seen: set = set()
    for dat in dats:
        for rom in getattr(dat, "roms", ()):
            key = (getattr(dat, "name", ""), getattr(rom, "set_name", "") or rom.name)
            if key in seen:
                continue
            seen.add(key)
            for r in tags.of_rom(rom).regions:
                counts[r] = counts.get(r, 0) + 1
    return counts


def available_languages(source: Any, platform: Any = None) -> list[dict[str, Any]]:
    """Languages present in ``source`` for the language checkboxes.

    ``source``: a ``DatFile``, an iterable of DatFiles, of :class:`Item`, or of ``Rom``. Returns
    ``[{"code": "En", "name": "English", "count": n, "games": m}]``, English first, then by ``count``
    (releases = No-Intro sets / TOSEC roms, bad dumps and other excluded rules NOT filtered out)
    descending, then code. ``games`` = distinct titles (``tags.title_key``). Counting follows
    ``tags.variant_languages`` (the rules the filter itself uses)."""
    roms: list[tags.Tags] = []
    seen_units: set[tuple[str, str]] = set()

    def add_rom(rom: Any, dat_name: str = "") -> None:
        set_name = getattr(rom, "set_name", "")
        nm = set_name or rom.name
        dat = dat_name or getattr(rom, "dat", "")
        if (dat, nm) in seen_units:
            return
        seen_units.add((dat, nm))
        roms.append(tags.of_rom(rom if getattr(rom, "dat", "") or not dat_name else
                                _with_dat(rom, dat_name)))

    def walk(obj: Any) -> None:
        if isinstance(obj, Item):
            nm = _rom_name(obj)
            if (obj.dat, nm) not in seen_units:
                seen_units.add((obj.dat, nm))
                roms.append(_tags_of(obj))
        elif hasattr(obj, "roms") and not hasattr(obj, "set_name"):
            for r in obj.roms:
                add_rom(r, getattr(obj, "name", ""))
        elif hasattr(obj, "name") and hasattr(obj, "game"):
            add_rom(obj)
        else:
            for x in obj:
                walk(x)

    walk(source)
    count: dict[str, int] = {}
    games: dict[str, set] = {}
    for t in roms:
        for code in tags.variant_languages(t):
            count[code] = count.get(code, 0) + 1
            games.setdefault(code, set()).add(tags.title_key(t))
    rows = [{"code": c, "name": tags.LANGUAGES.get(c, c), "count": n, "games": len(games[c])}
            for c, n in count.items()]
    rows.sort(key=lambda r: (r["code"] != "En", -r["count"], r["code"]))
    return rows


def _ratings_supported(platform: Any) -> bool:
    from . import ratings

    return ratings.supported(platform)


def profile_info(platform: Any, profile: Optional[LibraryProfile] = None,
                 regions_present: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """Everything the Build library panel needs for one platform (see ARCHITECTURE.md, Amendment 8)."""
    prof = profile if profile is not None else default_profile(platform)
    style = _style_of_platform(platform)
    has_langs = bool(_dats(platform, "language_dats"))
    return {
        "platform": getattr(platform, "name", ""),
        "style": style,
        "profile": prof.to_dict(),
        "defaults": default_profile(platform).to_dict(),
        "catalog": rule_catalog(style),
        "available": {
            "latest_only": bool(_dats(platform, "latest_dats")),
            "best_variant": bool(_dats(platform, "best_variant_dats")),
            "complete_only": bool(_dats(platform, "m3u_dats")),
            "one_per_game": bool(_dats(platform, "region_dats")),
            "region_priority": bool(_dats(platform, "region_dats")),
            "languages": has_langs,
            "keep_flags": bool(_dats(platform, "best_variant_dats")) and style == STYLE_TOSEC,
            "borrow_editions": _borrow_scope(platform),
            "rescue": bool(_dats(platform, "m3u_dats")) and style == STYLE_TOSEC,
            "ratings": _ratings_supported(platform),
        },
        "rating_codes": list(RATING_CODES),
        "reason_labels": {c: _RULE_SHORT[c] for c in RATING_CODES + OVERRIDE_CODES},
        "override_codes": list(OVERRIDE_CODES),
        "scopes": {
            "latest_dats": list(_dats(platform, "latest_dats")),
            "best_variant_dats": list(_dats(platform, "best_variant_dats")),
            "complete_dats": list(_dats(platform, "m3u_dats")),
            "language_dats": list(_dats(platform, "language_dats")),
            "region_dats": list(_dats(platform, "region_dats")),
            "exclude_dats": list(getattr(platform, "dats", ()) or ()),
        },
        "regions": tags.region_order(prof.region_priority, regions_present),
        "language_names": dict(tags.LANGUAGES),
    }


def vanish_report(sel: Selection, limit: Optional[int] = 200, reason: str = "") -> dict[str, Any]:
    """JSON for the preview's "no version in selected languages" / vanish list.

    ``{"titles", "by_reason", "by_code", "items": [{"dat", "title", "name", "reason", "codes",
    "languages", "variants", "text"}]}``; ``reason`` filters the items (``"language"``, ``"flag_cr"`` ...);
    ``limit`` caps ``items`` (None = all). ``text`` is the human line (``"no version in selected
    languages (has De)"``)."""
    out = sel.vanish_summary()
    items = []
    for v in sel.vanished:
        hint = ""
        if reason and v.reason != reason:
            continue
        if v.reason == LANGUAGE_CODE:
            text = "no version in selected languages" + (f" (has {', '.join(v.languages)})" if v.languages else "")
        elif v.reason == INCOMPLETE:
            text = "only incomplete sets"
        elif v.reason in RATING_CODES:
            text = {RATING_LOW: "below the minimum rating", RATING_NOT_TOP: "not among the top rated games",
                    RATING_UNRATED: "no usable rating (not in the LaunchBox data, or too few votes)"}[v.reason]
            if v.detail and v.reason != RATING_UNRATED:
                text += f" ({v.detail})"
            hint = {RATING_LOW: "lower the minimum rating", RATING_NOT_TOP: "raise Top N",
                    RATING_UNRATED: "tick Keep unrated games (or lower Minimum votes)"}[v.reason]
        else:
            text = "every version excluded: " + _RULE_SHORT.get(v.reason, v.reason)
        items.append({"dat": v.dat, "title": v.title, "name": v.name, "reason": v.reason,
                      "codes": list(v.codes), "languages": list(v.languages), "variants": v.variants,
                      "text": text, "hint": hint, "detail": v.detail})
        if limit is not None and len(items) >= limit:
            break
    out["items"] = items
    return out
