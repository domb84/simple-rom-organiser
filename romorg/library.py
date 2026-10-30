"""The "Build library" selection: which local files to keep, exclude, supersede or set aside.

Pure logic, no I/O. ``select`` takes the local files that survived duplicate removal (as
:class:`Item`) and a :class:`LibraryProfile` and decides, per file, ``keep`` / ``excluded`` /
``superseded`` / ``incomplete``; kept multi-disk sets come back as :class:`ChosenSet` (one
playlist each). The result depends only on the ROM names (never on where a file lives), so
running it on its own kept output changes nothing.
"""

from __future__ import annotations

import re
import dataclasses
import functools
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Optional, Sequence

from . import alttitles, tags
from .tags import STYLE_NOINTRO, STYLE_REDUMP, STYLE_TOSEC

if TYPE_CHECKING:
    from .datfile import Rom
    from .platforms import Platform

RULES = tags.RULES
FLAWED_RULES = frozenset({"bad_dump", "modified"})      # allowed by default: ranked below every clean version, kept as a last resort

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
# What the build does with a game the rules would replace or archive while RetroArch saves or save states exist for it
# (Amendment 31; only when a RetroArch config is known): ``keep`` = both ROMs stay, the one the rules would archive too
# (SAVED_KEEP), ``archive`` = it goes and its saves go with it, ``leave`` = it goes and the saves stay where they are.
# ``carry`` = the game the rules replace goes and its save games (not its states) are copied to the version that replaces it, when
# they work there (:mod:`savecarry`); where they would not, both ROMs stay, as with ``keep``. ``convert`` = the same, and a PlayStation save
# is rewritten for the other region's disc when the two discs' product codes differ (both discs must be readable).
# ``saved_games`` is the default for every game; ``saved_overrides`` says it per game.
SAVED_GAMES = ("keep", "archive", "leave", "carry", "convert")
SAVED_KEEP = "saved_keep"       # Decision.codes of a game kept because you have saves or save states for it
SAVED_KEEP_TEXT = "kept: you have saves or save states for it"
WHDLOAD_LANGUAGE = "whdload_language"   # Decision.codes of a version in another language kept: the only one WHDLoad is known to run
SAVED_PREFER = "saved_prefer"   # Decision.codes of the release kept instead of the one the rules prefer, because you have saves for it

_RULE_SHORT = {
    "bad_dump": "bad dump", "virus": "virus-infected", "bad_size": "over/under dump",
    "pre_release": "pre-release", "prototype": "prototype", "demo": "demo", "faked": "faked",
    "unreleased": "unreleased", "modified": "modified", "budget": "budget re-release", "online": "online only",
    "cd_conversion": "a floppy conversion of a CD32 / CDTV game",
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


def _norm_overrides(raw: Any, choices: tuple = OVERRIDE_ACTIONS) -> tuple[tuple[str, str, str], ...]:
    """``((dat, game, choice), ...)`` with a choice of ``choices``: valid entries only, one per game (the last wins), sorted."""
    found: dict[tuple[str, str], str] = {}
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(entry, (list, tuple)) and len(entry) == 3 and all(isinstance(x, str) for x in entry) \
                and entry[0] and entry[1] and entry[2] in choices:
            found[(entry[0], entry[1])] = entry[2]
    return tuple(sorted((d, n, a) for (d, n), a in found.items()))


def _norm_saved_overrides(raw: Any) -> tuple[tuple[str, str, str], ...]:
    """``((dat, game, "keep" | "archive" | "leave"), ...)``: see :func:`_norm_overrides`."""
    return _norm_overrides(raw, SAVED_GAMES)


def _norm_votes(v: Any) -> int:
    n = _num(v)
    return DEFAULT_MIN_VOTES if n is None or n < 1 else int(n)


@dataclass(frozen=True)
class LibraryProfile:
    exclude: frozenset[str] = frozenset(RULES) - FLAWED_RULES   # enabled exclusion rules
    latest_only: bool = True
    best_variant: bool = True                    # Amiga Games only (implies latest for that DAT)
    complete_only: bool = True                   # multi-disk sets
    # Language filter (Amiga Games + No-Intro): ORDERED codes, preferred first; empty = no filter.
    languages: tuple[str, ...] = ("En",)
    # Flag types that may stay (Amiga Games): a type that is missing here excludes its variants.
    keep_flags: frozenset[str] = frozenset(tags.KEEP_FLAGS)
    rescue_only_dump: bool = False               # keep-every-version DATs: keep the sole [m]/[o]/[u] dump
    region_priority: tuple[str, ...] = tags.DEFAULT_REGION_PRIORITY   # No-Intro: best region first
    region_ties: tuple[str, ...] = ()            # regions that rank the same as the region above them in the list (equally best)
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
    saved_games: str = "convert"
    # The same per game: (DAT name, game = set name or rom name, "keep" | "archive" | "leave"), see ``override_game``.
    saved_overrides: tuple[tuple[str, str, str], ...] = ()
    # BIOS / firmware / key files (told by checksum or name) stay with the ROMs; off: they are files like any other that matches no game.
    keep_bios: bool = True
    keep_extras: bool = False                    # disc systems: files that sit with a game but are no disc image (saves, states, checksums) stay there
    convert_first: bool = True                   # Build library first converts what the system can convert (raw discs, zips of them -> CHD)
    delete_converted: bool = True                # ... and deletes the originals once checked (off: they are kept in _converted_originals)
    compress_7z: bool = True                     # cartridge games are kept as .7z (LZMA: smaller than zip, RetroArch reads it), repacked on Build
    prefer_saved: bool = True                    # of the releases / versions of a game, the one you have saves for is kept (not a newer one, not another region)
    prefer_editions: bool = True                 # of a game's releases a special / limited / premium edition beats the plain release (off: after it)
    combine_alt_titles: bool = True              # the titles a game has in other regions (Biohazard / Resident Evil) are one game (``alttitles``)
    prefer_whdload: bool = True                  # Amiga: of a game's versions, one a WHDLoad install can be made from first (``whdready``)
    prefer_unprotected: bool = True              # Amiga: an original (SPS) that asks for the manual gives way to a crack WHDLoad installs
    build_whdload: bool = True                   # Amiga: Build library also makes each kept game a WHDLoad archive beside its disks (``whdbuild``)

    def __post_init__(self) -> None:
        # frozen dataclass: normalise whatever the caller passed (lists, sets, unknown codes)
        object.__setattr__(self, "exclude", frozenset(r for r in self.exclude if r in RULES))
        object.__setattr__(self, "languages", _norm_languages(self.languages))
        object.__setattr__(self, "keep_flags", frozenset(f for f in self.keep_flags if f in tags.KEEP_FLAGS))
        object.__setattr__(self, "region_priority", _norm_regions(self.region_priority))
        object.__setattr__(self, "region_ties", _norm_regions(self.region_ties))
        object.__setattr__(self, "min_rating", _norm_min_rating(self.min_rating))
        object.__setattr__(self, "top_n", _norm_top_n(self.top_n))
        object.__setattr__(self, "min_votes", _norm_votes(self.min_votes))
        object.__setattr__(self, "rank_scope", self.rank_scope if self.rank_scope in RANK_SCOPES else "dat")
        object.__setattr__(self, "overrides", _norm_overrides(self.overrides))
        object.__setattr__(self, "saved_games", self.saved_games if self.saved_games in SAVED_GAMES else "convert")
        object.__setattr__(self, "saved_overrides", _norm_saved_overrides(self.saved_overrides))

    @property
    def rating_active(self) -> bool:
        """True when a rating filter (minimum rating and / or top N) is set."""
        return self.min_rating is not None or self.top_n is not None

    def to_dict(self) -> dict:
        return {"exclude": sorted(self.exclude), "latest_only": self.latest_only,
                "best_variant": self.best_variant, "complete_only": self.complete_only,
                "languages": list(self.languages), "keep_flags": sorted(self.keep_flags),
                "rescue_only_dump": self.rescue_only_dump,
                "region_priority": list(self.region_priority), "region_ties": list(self.region_ties), "one_per_game": self.one_per_game,
                "borrow_other_editions": self.borrow_other_editions,
                "keep_other_language": self.keep_other_language,
                "min_rating": self.min_rating, "top_n": self.top_n, "min_votes": self.min_votes,
                "keep_unrated": self.keep_unrated, "rank_scope": self.rank_scope,
                "overrides": [list(o) for o in self.overrides], "saved_games": self.saved_games,
                "saved_overrides": [list(o) for o in self.saved_overrides], "keep_bios": self.keep_bios,
                "keep_extras": self.keep_extras, "convert_first": self.convert_first, "delete_converted": self.delete_converted,
                "compress_7z": self.compress_7z,
                "prefer_saved": self.prefer_saved, "prefer_editions": self.prefer_editions,
                "combine_alt_titles": self.combine_alt_titles, "prefer_whdload": self.prefer_whdload,
                "prefer_unprotected": self.prefer_unprotected, "build_whdload": self.build_whdload}

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
        ties = base.region_ties
        if isinstance(d.get("region_ties"), (list, tuple)):
            ties = _norm_regions(d["region_ties"])
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
                   region_priority=regions, region_ties=ties, one_per_game=flag("one_per_game", base.one_per_game),
                   borrow_other_editions=flag("borrow_other_editions", base.borrow_other_editions),
                   keep_other_language=flag("keep_other_language", base.keep_other_language),
                   **{**{"min_rating": base.min_rating, "top_n": base.top_n, "min_votes": base.min_votes,
                         "rank_scope": base.rank_scope}, **rating},
                   keep_unrated=flag("keep_unrated", base.keep_unrated),
                   overrides=_norm_overrides(d["overrides"]) if "overrides" in d else base.overrides,
                   saved_games=d["saved_games"] if isinstance(d.get("saved_games"), str) and d["saved_games"] in SAVED_GAMES
                   else base.saved_games,
                   saved_overrides=_norm_saved_overrides(d["saved_overrides"]) if "saved_overrides" in d else base.saved_overrides,
                   keep_bios=flag("keep_bios", base.keep_bios), keep_extras=flag("keep_extras", base.keep_extras),
                   convert_first=flag("convert_first", base.convert_first), delete_converted=flag("delete_converted", base.delete_converted),
                   compress_7z=flag("compress_7z", base.compress_7z),
                   prefer_saved=flag("prefer_saved", base.prefer_saved),
                   prefer_editions=flag("prefer_editions", base.prefer_editions),
                   combine_alt_titles=flag("combine_alt_titles", base.combine_alt_titles),
                   prefer_whdload=flag("prefer_whdload", base.prefer_whdload),
                   prefer_unprotected=flag("prefer_unprotected", base.prefer_unprotected),
                   build_whdload=flag("build_whdload", base.build_whdload))

    @classmethod
    def latest_only_profile(cls) -> "LibraryProfile":
        """The legacy ``latest_only`` flag: no exclusions, no filters, no best variant / completeness."""
        return cls(exclude=frozenset(), latest_only=True, best_variant=False, complete_only=False,
                   languages=(), one_per_game=False, borrow_other_editions=False)


def _dats(platform: Any, attr: str) -> tuple[str, ...]:
    return tuple(getattr(platform, attr, ()) or ())


def complete_dats(platform: Any) -> tuple[str, ...]:
    """The DATs "Complete multi-disk sets only" decides: the multi-disk ones (``m3u_dats``), or the discs of a system with a folder
    per game (Dreamcast, PlayStation, PlayStation 2: their Redump DATs)."""
    return _dats(platform, "m3u_dats") or (_dats(platform, "latest_dats") if getattr(platform, "layout", "") == "game_folder" else ())


def _borrow_scope(platform: Any) -> bool:
    """Borrowing disks of other editions applies to the TOSEC "Games"-style DATs (``best_variant_dats``) only."""
    return bool(_dats(platform, "best_variant_dats")) and _style_of_platform(platform) == STYLE_TOSEC


def _has_one_per_game(platform: "Platform") -> bool:
    """"One version per game" means something where the databases carry regions (No-Intro, Redump) and on the Amiga, whose SPS originals,
    floppy sets and CD32 discs of a game are choices between releases (:func:`_one_release`)."""
    return bool(_dats(platform, "region_dats")) or bool(_dats(platform, "image_set_dats"))


def default_profile(platform: "Platform") -> LibraryProfile:
    """All rules on, minus the options that do not apply to the platform, and minus bad dumps and modified copies: allowed, they are
    ranked below every clean version (:func:`_pick_best`), so they are kept only for a game that has nothing else (34 Amiga games are
    WHDLoad-ready only from one)."""
    return LibraryProfile(
        exclude=frozenset(RULES) - FLAWED_RULES,
        latest_only=bool(_dats(platform, "latest_dats")),
        best_variant=bool(_dats(platform, "best_variant_dats")),
        complete_only=bool(complete_dats(platform)),
        languages=("En",) if _dats(platform, "language_dats") else (),
        one_per_game=_has_one_per_game(platform),
        borrow_other_editions=_borrow_scope(platform),
        convert_first=bool(getattr(platform, "convertible", False)),               # (where a system converts nothing, or never deletes
        delete_converted="delete_converted" in applicable_fields(platform),       #  an original - SNES / N64 - the rule is off, not on)
        compress_7z=bool(getattr(platform, "compress_7z", False)))


# --------------------------------------------------------------------------- defaults and overrides
# The rules a library is built by are set once, for every system (``cfg["library_defaults"]``: only what differs from the app's own
# defaults). A system uses them unless it overrides a rule (``cfg["library_overrides"][system]``: only the rules it overrides, plus the
# choices made for single games, which are always the system's own). The profile a system builds with is, from the bottom:
#   the system's capability defaults (``default_profile``)  <  your defaults  <  the system's overrides
RULE_FIELDS = ("exclude", "latest_only", "best_variant", "complete_only", "languages", "keep_flags", "rescue_only_dump",
               "region_priority", "region_ties", "one_per_game", "borrow_other_editions", "keep_other_language", "min_rating", "top_n",
               "min_votes", "keep_unrated", "rank_scope", "saved_games", "keep_bios", "keep_extras", "convert_first", "delete_converted", "compress_7z", "prefer_saved", "prefer_editions",
               "combine_alt_titles", "prefer_whdload", "prefer_unprotected", "build_whdload")
# A system's override of the exclusion rules is two: the rules every system has (``exclude``) and the ones only the Amiga systems have
# (``exclude_amiga``: virus-infected, over/under dump, faked, modified, unreleased). Both are stored as a full list of rules; each only
# decides its own rules. Your defaults keep the one list ``exclude``.
AMIGA_STYLES = frozenset({STYLE_TOSEC})
OVERRIDE_FIELDS = RULE_FIELDS + ("exclude_amiga",)
GAME_FIELDS = ("overrides", "saved_overrides")          # per game: never global
REGION_FIELDS = ("region_priority", "region_ties")      # the region order and which of its regions are equally good: one rule


@functools.lru_cache(maxsize=1)
def amiga_exclusions() -> frozenset:
    """The exclusion rules only the Amiga (TOSEC) has."""
    return frozenset(e["id"] for e in rule_catalog("tosec") if e["kind"] == "exclude" and e["applies_to"] and set(e["applies_to"]) <= AMIGA_STYLES)


def _split_exclude(ov: Mapping[str, Any], base: Iterable[str], split: bool) -> Optional[list]:
    """The effective exclusion rules of a system: its override of ``exclude`` decides the rules every system has, its override of
    ``exclude_amiga`` the Amiga-only ones (``split``: the system has those); what is not overridden comes from ``base``."""
    if "exclude" not in ov and "exclude_amiga" not in ov:
        return None
    if not split:
        return list(ov["exclude"]) if "exclude" in ov else None
    amiga = amiga_exclusions()
    shared = set(ov["exclude"] if "exclude" in ov else base) - amiga
    mine = set(ov["exclude_amiga"] if "exclude_amiga" in ov else base) & amiga
    return sorted(shared | mine)


def applicable_fields(platform: "Platform") -> frozenset:
    """The rules of the profile that mean something for this system (its databases carry the tags they read)."""
    style = _style_of_platform(platform)
    has_langs = bool(_dats(platform, "language_dats"))
    has_regions = bool(_dats(platform, "region_dats"))
    tosec = bool(_dats(platform, "best_variant_dats")) and style == STYLE_TOSEC
    out = {"exclude", "saved_games", "keep_bios", "prefer_saved"}
    for field, ok in (("latest_only", bool(_dats(platform, "latest_dats"))), ("best_variant", bool(_dats(platform, "best_variant_dats"))),
                      ("complete_only", bool(_dats(platform, "m3u_dats"))), ("languages", has_langs), ("keep_other_language", has_langs),
                      ("keep_flags", tosec), ("rescue_only_dump", bool(_dats(platform, "m3u_dats")) and style == STYLE_TOSEC),
                      ("region_priority", has_regions), ("region_ties", has_regions), ("one_per_game", _has_one_per_game(platform)), ("borrow_other_editions", _borrow_scope(platform))):
        if ok:
            out.add(field)
    if getattr(platform, "layout", "") == "game_folder":            # (the disc systems with a folder per game: Dreamcast, PlayStation, PlayStation 2)
        out.update(("keep_extras", "complete_only"))                # (complete_only: a raw disc of a set with a disc missing is not converted yet)
    if getattr(platform, "convertible", False):                    # (raw discs to CHD; SNES / N64: clean up the dumps)
        out.add("convert_first")
        if getattr(platform, "layout", "") == "game_folder" or getattr(platform, "disc_dats", ()):   # (and the Amiga's CD32 / CDTV discs)
            out.add("delete_converted")
    if getattr(platform, "compress_7z", False):                   # (the cartridge systems RetroArch reads .7z games of)
        out.add("compress_7z")
    if _ratings_supported(platform):
        out.update(("min_rating", "top_n", "min_votes", "keep_unrated", "rank_scope"))
    if _style_of_platform(platform) in AMIGA_STYLES:
        out.add("exclude_amiga")
        if _dats(platform, "best_variant_dats"):
            out.add("prefer_whdload")
            out.add("build_whdload")
        if _dats(platform, "image_set_dats"):
            out.add("prefer_unprotected")
    else:
        out.add("prefer_editions")                                # (the release tags of No-Intro / Redump names: editions, budget lines)
    out.add("combine_alt_titles")
    return frozenset(out)


def app_defaults() -> LibraryProfile:
    """The app's own defaults for every rule (what Settings shows before you change anything)."""
    return LibraryProfile()


def global_profile(cfg: dict) -> LibraryProfile:
    """Your defaults: the app's own with what is stored in ``cfg["library_defaults"]`` on top."""
    stored = cfg.get("library_defaults") if isinstance(cfg, dict) else None
    stored = {k: v for k, v in stored.items() if k in RULE_FIELDS} if isinstance(stored, dict) else {}
    return LibraryProfile.from_dict(stored, app_defaults())


def store_global(cfg: dict, profile: LibraryProfile) -> None:
    """Remember your defaults: only the rules that differ from the app's own."""
    base, mine = app_defaults().to_dict(), profile.to_dict()
    changed = {f: mine[f] for f in RULE_FIELDS if mine[f] != base[f]}
    if changed:
        cfg["library_defaults"] = changed
    else:
        cfg.pop("library_defaults", None)


def effective_profile(platform: "Platform", defaults: Optional[Mapping[str, Any]] = None,
                      overrides: Optional[Mapping[str, Any]] = None) -> LibraryProfile:
    """The system's capability defaults, your defaults on top (only the rules the system has), the system's overrides on top of those."""
    base = default_profile(platform)
    ok = applicable_fields(platform)
    mine = {k: v for k, v in (defaults or {}).items() if k in RULE_FIELDS and k in ok}
    prof = LibraryProfile.from_dict(mine, base)
    ov = {k: v for k, v in (overrides or {}).items() if k in GAME_FIELDS or (k in OVERRIDE_FIELDS and k in ok)}
    if "region_priority" in ov and "region_ties" not in ov:      # (ties name the regions of an order: they do not carry over to another one)
        ov["region_ties"] = []
    own = ov.pop("exclude_amiga", None)
    merged = _split_exclude({**({"exclude": ov["exclude"]} if "exclude" in ov else {}), **({"exclude_amiga": own} if own is not None else {})},
                            prof.exclude, "exclude_amiga" in ok)
    if merged is not None:
        ov["exclude"] = merged
    return LibraryProfile.from_dict({**prof.to_dict(), **ov}, base)


def _legacy_overrides(cfg: dict, platform: "Platform") -> Optional[dict]:
    """What an older config kept for the system (a full profile in ``cfg["library"]``, or the ``latest_only`` flag), as overrides: the
    rules in which it differs from what the system would use without any."""
    lib = cfg.get("library") if isinstance(cfg, dict) else None
    entry = lib.get(platform.name) if isinstance(lib, dict) else None
    plain = effective_profile(platform, cfg.get("library_defaults") or {}, {})
    if entry is not None:
        saved = LibraryProfile.from_dict(entry, default_profile(platform))
        ok = applicable_fields(platform)
        mine, base = saved.to_dict(), plain.to_dict()
        out = {f: mine[f] for f in RULE_FIELDS if f in ok and mine[f] != base[f]}
        for f in GAME_FIELDS:
            if mine[f]:
                out[f] = mine[f]
        return out
    legacy = cfg.get("latest_only") if isinstance(cfg, dict) else None
    if isinstance(legacy, dict) and isinstance(legacy.get(platform.name), bool):
        value = legacy[platform.name] and plain.latest_only
        return {"latest_only": value} if value != plain.latest_only else {}
    return None


def overrides_of(cfg: dict, platform: "Platform") -> dict:
    """The system's overrides (a copy): only the rules it overrides, and its per-game choices."""
    table = cfg.get("library_overrides") if isinstance(cfg, dict) else None
    entry = table.get(platform.name) if isinstance(table, dict) else None
    out = dict(entry) if isinstance(entry, dict) else (_legacy_overrides(cfg, platform) or {})
    if "exclude" in out and "exclude_amiga" in applicable_fields(platform) and not out.get("exclude_split"):
        out["exclude_amiga"] = out["exclude"]           # (an override made when there was one list of exclusions decided all of them)
        out["exclude_split"] = True
    return out


def overridden_fields(cfg: dict, platform: "Platform") -> list:
    """The rules this system overrides (in the order of ``RULE_FIELDS``)."""
    ov, ok = overrides_of(cfg, platform), applicable_fields(platform)
    return [f for f in OVERRIDE_FIELDS if f in ov and f in ok]


def load_profile(cfg: dict, platform: "Platform") -> LibraryProfile:
    """The profile the system builds with (see the top of this section)."""
    defaults = cfg.get("library_defaults") if isinstance(cfg, dict) else None
    return effective_profile(platform, defaults if isinstance(defaults, dict) else {}, overrides_of(cfg, platform))


def store_profile(cfg: dict, platform: "Platform", profile: LibraryProfile) -> None:
    """Remember what ``profile`` changes: a rule that differs from what the system uses now becomes an override of the system's;
    one it already overrides stays so (also when it is set back to the default). The per-game choices are the system's own."""
    ov = overrides_of(cfg, platform)
    current = load_profile(cfg, platform).to_dict()
    mine = profile.to_dict()
    ok = applicable_fields(platform)
    split = "exclude_amiga" in ok
    for f in RULE_FIELDS:
        if f in ok and mine[f] != current[f] and not (split and f == "exclude"):
            ov[f] = mine[f]
    if split and mine["exclude"] != current["exclude"]:         # (which of the two exclusion overrides: by the rules that changed)
        changed = set(mine["exclude"]) ^ set(current["exclude"])
        if changed - amiga_exclusions():
            ov["exclude"] = mine["exclude"]
        if changed & amiga_exclusions():
            ov["exclude_amiga"] = mine["exclude"]
        ov["exclude_split"] = True
    if any(f in ov for f in REGION_FIELDS) and all(f in ok for f in REGION_FIELDS):
        for f in REGION_FIELDS:                # the order and its ties are one rule: a system that has its own has both
            ov[f] = mine[f]
    for f in GAME_FIELDS:
        if mine[f]:
            ov[f] = mine[f]
        else:
            ov.pop(f, None)
    _put_overrides(cfg, platform, ov)


def _put_overrides(cfg: dict, platform: "Platform", ov: dict) -> None:
    table = cfg.setdefault("library_overrides", {})
    if ov:
        table[platform.name] = ov
    else:
        table.pop(platform.name, None)
    if not table:
        cfg.pop("library_overrides", None)
    legacy = cfg.get("library")
    if isinstance(legacy, dict):
        legacy.pop(platform.name, None)
        if not legacy:
            cfg.pop("library", None)


def set_override(cfg: dict, platform: "Platform", fields: Iterable[str], on: bool) -> None:
    """Override ``fields`` for this system (they start at what the system uses now) or go back to your defaults for them."""
    ov = overrides_of(cfg, platform)
    current = load_profile(cfg, platform).to_dict()
    ok = applicable_fields(platform)
    wanted = list(fields)
    if any(f in REGION_FIELDS for f in wanted):
        wanted += [f for f in REGION_FIELDS if f not in wanted]      # (one rule: overridden, or not, together)
    for f in wanted:
        if f not in OVERRIDE_FIELDS or f not in ok:
            continue
        if on:
            ov.setdefault(f, current["exclude" if f == "exclude_amiga" else f])
            if f in ("exclude", "exclude_amiga") and "exclude_amiga" in ok:
                ov["exclude_split"] = True
        else:
            ov.pop(f, None)
    if "exclude" not in ov and "exclude_amiga" not in ov:
        ov.pop("exclude_split", None)
    _put_overrides(cfg, platform, ov)


def reset_overrides(cfg: dict, platform: "Platform") -> None:
    """Back to your defaults for every rule (the choices made for single games stay)."""
    ov = {k: v for k, v in overrides_of(cfg, platform).items() if k in GAME_FIELDS}
    _put_overrides(cfg, platform, ov)


RULES_VERSION = 3          # 2: the exclusion rules "budget" and "online"; 3: "cd_conversion" - each on by default
_RULES_ADDED = {2: ("budget", "online"), 3: ("cd_conversion",)}      # (version -> the rules it added: tags.NEW_RULES)


def migrate_config(cfg: dict) -> bool:
    """Bring an older config to the layout above, in place; True when it changed anything. A Collection's shared rules (that page is
    gone) become your defaults, each system's saved profile becomes its list of overrides (only where it differs from the defaults).
    A saved list of exclusion rules from before a rule existed gets that rule (new rules are on by default)."""
    changed = False
    since = cfg.get("rules_version", 1)
    coll = cfg.get("collection")
    if isinstance(coll, dict):
        mine = coll.get("global") if isinstance(coll.get("global"), dict) else {}
        if mine and "library_defaults" not in cfg:
            prof = LibraryProfile.from_dict({k: v for k, v in mine.items() if k in RULE_FIELDS}, app_defaults())
            store_global(cfg, prof)
        cfg.pop("collection", None)
        changed = True
    table = cfg.get("convert_on_build")                           # (the old per-system "convert first" box: now a rule, overridden where it was on)
    if isinstance(table, dict):
        from .platforms import list_platforms
        for platform in list_platforms():
            if table.get(platform.name) and "convert_first" in applicable_fields(platform):
                ov = dict(overrides_of(cfg, platform))
                ov.setdefault("convert_first", True)
                _put_overrides(cfg, platform, ov)
        cfg.pop("convert_on_build", None)
        changed = True
    legacy = cfg.get("library")
    if isinstance(legacy, dict):
        from .platforms import list_platforms
        for platform in list_platforms():
            if platform.name in legacy or (isinstance(cfg.get("latest_only"), dict) and platform.name in cfg["latest_only"]):
                found = _legacy_overrides(cfg, platform)
                if found is not None:
                    _put_overrides(cfg, platform, found)
                    changed = True
        cfg.pop("library", None)
        changed = True
    if since < RULES_VERSION:                                     # (after the conversions above: their lists are as old as the config)
        added = [r for v, rs in sorted(_RULES_ADDED.items()) if v > since for r in rs]   # (only the rules newer than the config:
        lists = [cfg.get("library_defaults")] + list((cfg.get("library_overrides") or {}).values())   #  one turned off stays off)
        for entry in lists:
            if not isinstance(entry, dict):
                continue
            for key in ("exclude", "exclude_amiga"):
                if isinstance(entry.get(key), list):
                    entry[key] = list(dict.fromkeys([*entry[key], *added]))
        cfg["rules_version"] = RULES_VERSION
        changed = True
    return changed


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
    holds: frozenset = frozenset()  # the DAT rom names the file holds (an archive of a whole CD32 disc: its .bin and .cue)


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
    keeper: str = ""              # duplicate: the copy that stays (its path)
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
    covered_by: str = ""          # a complete version of the same game is kept (its name): nothing to report (:func:`_mark_covered`)


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
    combine_alt: bool = False                  # the profile's "Combine alternate titles" (what :func:`game_key` of its items needs)
    before_borrow: dict[int, Optional[Decision]] = field(default_factory=dict)   # a borrowed disk's own decision (its set may be dropped)

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
    return item.style if item.style in (STYLE_TOSEC, STYLE_NOINTRO, STYLE_REDUMP) else STYLE_TOSEC


def _tags_of(item: Item) -> tags.Tags:
    if _style(item) == STYLE_REDUMP:     # name + the DAT <category> (Demos, Coverdiscs, Preproduction)
        return tags.redump_tags(_rom_name(item), getattr(item.rom, "category", "") or "")
    if item.dat in _image_set_dats() and item.rom.game:  # (an SPS image is "<release> - <file>.ipf": the release's name has the tags)
        return tags.parse_name(item.rom.game + ".ipf", _style(item))
    if item.dat in _disc_dats() and item.rom.game:       # (a CD32 / CDTV disc's files - "(Track 01 of 02)" - are one game: the disc's name)
        return tags.parse_name(item.rom.game + ".cue", _style(item))
    return tags.parse_name(_rom_name(item), _style(item))


def game_key(item: Item, combine: bool = False) -> tuple:
    """The identity of the GAME an item belongs to, exactly as :func:`select` groups variants: TOSEC
    ``tags.identity_key`` (title, country, publisher, edition and status tags), No-Intro / Redump ``tags.game_key``
    (title, status, product tags; regions, languages, versions and disc numbers are not part of it). ``combine``: the profile's
    "Combine alternate titles" (the titles of one game in other regions are one title)."""
    return (game_dat(item.dat), _gkey(item, combine))      # tags.game_key = identity_key for TOSEC; a disc: its games DAT's


def _gkey(item: Item, combine: bool) -> tuple:
    """:func:`game_key`'s key, remembered on the item (asked of the same item a dozen times in a run) while the alternate titles stay."""
    state = alttitles.state() if combine else None
    memo = item.__dict__.setdefault("_gkeys", {})
    hit = memo.get(combine)
    if hit is not None and hit[0] == state:
        return hit[1]
    value = combined_key(game_dat(item.dat), tags.game_key(_tags_of(item)), combine)
    memo[combine] = (state, value)
    return value


_GAME_DATS: Optional[dict[str, str]] = None
_IMAGE_SETS: Optional[frozenset] = None


def game_dat(dat: str) -> str:
    """The DAT whose games ``dat``'s are: a CD DAT's discs (``Platform.disc_dats``) and the SPS / IPF releases
    (``Platform.image_set_dats``) are games of the system's games DAT - a game's disc, its original disks and its floppies are one game
    (counted once); any other DAT is its own."""
    global _GAME_DATS
    if _GAME_DATS is None:
        from . import platforms
        _GAME_DATS = {d: p.game_dat for p in platforms.PLATFORMS.values() if p.game_dat
                      for d in (*p.disc_dats, *p.image_set_dats)}
    return _GAME_DATS.get(dat, dat)


def _image_set_dats() -> frozenset:
    global _IMAGE_SETS
    if _IMAGE_SETS is None:
        from . import platforms
        _IMAGE_SETS = frozenset(d for p in platforms.PLATFORMS.values() for d in p.image_set_dats)
    return _IMAGE_SETS


_DISC_DATS: Optional[frozenset] = None


def _disc_dats() -> frozenset:
    global _DISC_DATS
    if _DISC_DATS is None:
        from . import platforms
        _DISC_DATS = frozenset(d for p in platforms.PLATFORMS.values() for d in p.disc_dats)
    return _DISC_DATS


_DISC_FILES: dict[str, dict[str, frozenset]] = {}


def register_disc_files(dat: str, roms: Iterable[Any]) -> None:
    """The files of each game of a CD DAT (``platforms.load_platform_dats``): a disc is complete with all of them."""
    games: dict[str, set] = {}
    for r in roms:
        games.setdefault(r.game, set()).add(r.name)
    _DISC_FILES[dat] = {g: frozenset(v) for g, v in games.items()}


# Different games that share a TOSEC title (checked by hand: the DAT gives no other way to tell them apart, and LaunchBox knows only a
# few of them). "Combine alternate titles" makes the releases of a title by other publishers one game; a title here is split by its
# publishers instead: {title (casefold): [publishers of one game, publishers of another, ...]}. A publisher in no list is the first game.
KEEP_APART: dict[str, list[frozenset]] = {
    "overlord": [frozenset({"virgin mastertronic", "melbourne house"}), frozenset({"virgin"})],      # Probe's strategy game (Supremacy) / Rowan's flight sim
    "arena": [frozenset({"psygnosis"}), frozenset({"defcom"})],                                    # athletics / puzzle game
    "war zone": [frozenset({"prism leisure", "eidersoft"}), frozenset({"core"})],                   # Eidersoft's tank game / Core's shooter
    "legend": [frozenset({"crl"}), frozenset({"mindscape"})],                                      # the wargame (The Black Orchid) / the RPG
    "protector": [frozenset({"prism leisure", "paradox"}), frozenset({"virgin mastertronic"})],     # the arcade shooter / the helicopter game
    "turbo trax": [frozenset({"microdeal"}), frozenset({"arcane"})],                               # the slot-car racer / Arcane's racer
    "enforcer, the": [frozenset({"methodic solutions"}), frozenset({"trojan"})],                   # (Police Academy II) / the light-gun game
    "monopoly": [frozenset({"leisure genius"}), frozenset({"supervision"})],                       # two different programs
    "locomotion": [frozenset({"byte back"}), frozenset({"kingsoft"})],                             # Byte Back's / Prestige's switching puzzle
}
# New candidates: tools/keep_apart_candidates.py lists the titles LaunchBox knows as two Amiga games (a lead to check, never a rule).


def _apart(title: str, publisher: str) -> str:
    """The publisher part of a combined TOSEC key: "" (any publisher: one game), or which game of a :data:`KEEP_APART` title it is."""
    games = KEEP_APART.get(title.strip())
    if not games:
        return ""
    parts = {publisher.strip(), *(x.strip() for x in publisher.split(" - "))}      # ("Mindscape - Future Publishing": a cover disk of Mindscape's)
    for n, pubs in enumerate(games):
        if parts & pubs:
            return f"#{n}"
    return "#0"


_COMBINED: dict[tuple, tuple] = {}
_COMBINED_STATE: list = [None]


def combined_key(dat: str, key: tuple, combine: bool) -> tuple:
    """A game key of ``dat`` (``tags.game_key`` / ``identity_key``) as "Combine alternate titles" makes it: the title of a group of
    alternate titles is the group's (``alttitles``) and, of a TOSEC name, the country and the publisher are no part of the game (the
    German release of another publisher is the same game: the best variant is chosen by your languages). Remembered while the
    alternate titles stay the same (``alttitles.state``): a library is asked it hundreds of thousands of times."""
    if not combine or len(key) < 2:
        return key
    now = alttitles.state()
    if _COMBINED_STATE[0] != now:
        _COMBINED.clear()
        _COMBINED_STATE[0] = now
    hit = _COMBINED.get((dat, key))
    if hit is None:
        hit = _COMBINED[(dat, key)] = _combined_key(dat, key)
    return hit


def _combined_key(dat: str, key: tuple) -> tuple:
    if key[0] == STYLE_TOSEC and len(key) == 6:            # (style, title, countries, publisher, edition, statuses)
        edition = tuple(f for f in key[4] if tags.flag_kind(f) not in ("video", "language")    # (PAL / NTSC, M3: a release, not a game;
                        and not re.fullmatch(r"[A-Z]{1,3}", f))      #  nor a version's letter: "v1.14C", "v1.00R", "v1.44GE" - your languages choose)
        g = alttitles.group(dat, key[1])
        # which game of a KEEP_APART title: by its own title, else by its group's (Mindscape's "Four Crystals of Trazere" is its Legend)
        apart = _apart(key[1], key[3]) if key[1].strip() in KEEP_APART or not g else _apart(g[1:], key[3])
        key = (key[0], key[1], (), apart, edition, key[5])
    g = alttitles.group(dat, key[1])                        # (key[1]: the title, casefold)
    if g:
        key = (key[0], g) + key[2:]
    return key


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
    (they only apply to some DATs). ``parsed``: the already parsed tags (Redump needs the DAT category too)."""
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
        self.disc = set(_dats(platform, "disc_dats"))
        self.keep_all_dats_excluded = self.latest | self.best      # DATs with one-variant rules

    def flags(self, dat: str) -> bool:
        return dat in self.best


def _region_pos(t: Any, profile: LibraryProfile) -> int:
    """Where the best of a release's regions stands in the region list itself: regions you ranked as equal (``region_ties``) are the same tier, but
    when nothing else tells two releases apart the one higher in the list is preferred, not the one that comes first alphabetically."""
    pos = _region_positions(tuple(profile.region_priority))
    return min((pos.get(r, len(pos)) for r in t.regions), default=len(pos))


@functools.lru_cache(maxsize=16)
def _region_positions(priority: tuple) -> dict:
    return {r: i for i, r in enumerate(tags.region_order(priority))}


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
    return tags.strip_ext(name.strip())


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

    stem = tags.strip_ext(name.strip())
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
                borrow: Optional[_Borrow] = None, complete_totals: Iterable[int] = ()) -> tuple[list[_Set], list[_Set]]:
    """``(complete sets, incomplete sets)`` of one identity group.

    With ``borrow`` an anchored incomplete set whose disk count has no complete set in the group - nor in
    ``complete_totals``: the disk counts other editions of a combined game have complete - is completed by disks of
    other editions (every missing slot must be fillable)."""
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
        have_complete = {ss.total for ss in found if ss.complete} | set(complete_totals)
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


def _pick_best(sets: list[_Set], prefer_whdload: bool = False) -> _Set:
    """Best language, then (``prefer_whdload``) the version WHDLoad works best from (``whdready.level``), then clean (no disk a bad dump
    or modified: allowed, those are kept only when nothing clean is), then cracked, then best platform (AGA > ECS > OCS: the floppies'
    chipset - discs are ranked across DATs, :func:`_rank_across`), then newest, then fewest modification flags ([t] [h] [tr] [f] [a]),
    then name."""
    from . import whdready
    pool = sets

    def whd(s: _Set) -> int:                                # (boots > installs the files > installs disk images > not known)
        return -(whdready.level_of(it.rom for it in s.slots.values()) or 0) if prefer_whdload else 0

    def flawed(s: _Set) -> bool:
        return any(tags.exclusion_rules(_tags_of(it)) & FLAWED_RULES for it in s.slots.values())

    for key in (lambda s: s.lang_rank, whd, flawed, lambda s: not s.cracked, lambda s: s.platform_rank):
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
        info = eligibility(_rom_name(it), _style(it), profile, languages=True, flags=scope.flags(it.dat),
                           parsed=_tags_of(it) if _style(it) == STYLE_REDUMP or it.dat in _image_set_dats() else None)
        if all(code == "budget" for code, _x in info):         # (a budget re-release in your language is one: the budget rule decides it)
            out.add(game_key(it, profile.combine_alt_titles))
    return frozenset(out)


def select(items: Sequence[Item], profile: LibraryProfile, platform: "Platform",
           ratings: Optional[RatingContext] = None, lang_games: Optional[frozenset] = None,
           saved: Optional[frozenset] = None, report: bool = True) -> Selection:
    """Decide keep / excluded / superseded / incomplete per item (see the module docstring).

    With a rating filter in ``profile`` (``profile.rating_active``) ``ratings`` is required (:class:`RatingsUnavailable`
    otherwise) and the filter is applied last, at game level (:func:`apply_ratings`). ``saved``: the content names (as
    ``retroarch.save_name_keys`` writes them) RetroArch has saves or states for; a game among them whose choice is ``keep`` (the
    profile's, or its own in ``saved_overrides``) is never set aside (:func:`apply_saved`); None / empty = nothing changes."""
    if profile.rating_active and ratings is None:
        raise RatingsUnavailable("a rating filter is set but no rating data was supplied")
    sel = base_selection(items, profile, platform, lang_games)
    if profile.rating_active:
        apply_ratings(sel, items, profile, platform, ratings)  # type: ignore[arg-type]
    if saved and profile.prefer_saved:
        apply_prefer_saved(sel, items, saved)
    if saved:
        apply_saved(sel, items, saved, profile, platform.name)
    apply_overrides(sel, items, profile)
    if report:                                   # (the lists a page shows: a count of the games does not read them)
        sel.vanished = _vanished(items, sel)
        _mark_covered(items, sel)
    return sel


# --------------------------------------------------------------------------- the base selection, remembered

# The rules ``_select_base`` (and everything it calls) reads. The rating filter, your own per-game choices, saves and the build's own
# options are applied after it, so changing one of those does not change it. (tests/test_base_selection.py checks both lists.)
BASE_FIELDS = ("exclude", "latest_only", "best_variant", "complete_only", "languages", "keep_flags", "rescue_only_dump", "region_priority",
               "region_ties", "one_per_game", "borrow_other_editions", "keep_other_language", "prefer_editions", "combine_alt_titles",
               "prefer_whdload", "prefer_unprotected")
_BASES: list[tuple[tuple, Any, Selection]] = []       # (key, what keeps the key's ids alive, the selection), oldest first
_BASES_LOCK = threading.Lock()
_BASES_KEPT = 4                                        # the whole database, your files and the build preview of a system, and one more


def _clone_selection(sel: Selection) -> Selection:
    """A copy of ``sel`` that the later rules may change (decisions, sets and incomplete sets are changed in place by them). A decision
    that sits in two places (``before_borrow`` and ``decisions``) stays one decision in the copy."""
    seen: dict[int, Decision] = {}

    def dec(d: Optional[Decision]) -> Optional[Decision]:
        if d is None:
            return None
        c = seen.get(id(d))
        if c is None:
            c = seen[id(d)] = dataclasses.replace(d)
        return c

    out = Selection(rating=dict(sel.rating), combine_alt=sel.combine_alt)
    out.decisions = {k: dec(d) for k, d in sel.decisions.items()}
    out.before_borrow = {k: dec(d) for k, d in sel.before_borrow.items()}
    out.sets = [replace(cs, slots=dict(cs.slots), labels=dict(cs.labels), borrowed={n: dict(b) for n, b in cs.borrowed.items()})
                for cs in sel.sets]
    out.incomplete = [replace(x, present=dict(x.present)) for x in sel.incomplete]
    out.vanished = list(sel.vanished)
    return out


def _base_key(items: Sequence[Item], profile: LibraryProfile, platform: "Platform", lang_games: Optional[frozenset]) -> tuple[tuple, tuple]:
    """``(key, refs)``: everything the base selection depends on, and the objects whose ids the key holds (kept alive with the entry, so
    an id is never given to another object while the entry is there)."""
    from . import openretro, whdready
    ready, known = whdready.load(), whdready.known()
    disc = tuple(_DISC_FILES.get(d) for d in sorted(_disc_dats()))
    checks = openretro.database()                       # (the protection checks an original is judged by)
    reads = tuple(getattr(whdready, n) for n in ("level_of", "has_install", "leaves_manual", "needs_whdload", "release_of", "sets_with"))
    refs = (ready, known, disc, checks, reads)
    key = (repr(platform), tuple(getattr(profile, f) for f in BASE_FIELDS), lang_games, alttitles.state(),
           id(ready) if ready else 0, id(known) if known else 0, tuple(id(d) if d else 0 for d in disc), id(checks), tuple(map(id, reads)),
           tuple((it.key, it.dat, id(it.rom), it.style, it.path, it.member, it.form, it.link, it.holds, it.disc_total) for it in items))
    return key, refs


def base_selection(items: Sequence[Item], profile: LibraryProfile, platform: "Platform",
                   lang_games: Optional[frozenset] = None) -> Selection:
    """Every rule except the rating filter, with a bad / modified copy beside a clean one set aside (:func:`_select_base` and
    :func:`_flawed_beside_clean`), as a copy the caller may change. The same files under rules that differ only in what comes after
    (the rating filter, your game choices, saves, build options) are decided once: the totals of the whole database and of your files and
    the build preview all ask for it."""
    key, refs = _base_key(items, profile, platform, lang_games)
    with _BASES_LOCK:
        for entry in _BASES:
            if entry[0] == key:
                _BASES.remove(entry)
                _BASES.append(entry)
                hit = entry[2]
                break
        else:
            hit = None
    if hit is None:
        hit = _select_base(items, profile, platform, lang_games)
        _flawed_beside_clean(items, hit)
        with _BASES_LOCK:
            _BASES.append((key, (refs, list(items)), hit))
            del _BASES[:-_BASES_KEPT]
        return _clone_selection(hit)
    return _clone_selection(hit)


def _flawed_beside_clean(items: Sequence[Item], sel: Selection) -> None:
    """A bad dump or modified copy (allowed: ``FLAWED_RULES`` not excluded) is kept only when no clean copy of the same version is: one
    beside a kept clean dump of that version (a Kickstart [b] next to its [!], the [m] of a Workbench disk you also have clean, a No-Intro
    [b]) is superseded by it. A kept multi-disk set counts as one copy, flawed when one of its disks is."""
    by_key = {it.key: it for it in items}
    in_set: dict[int, ChosenSet] = {k: cs for cs in sel.sets for k in cs.slots.values()}

    def version(it: Item, drop_disk: bool) -> tuple:
        t = _tags_of(it)
        flags = list(t.flags)
        d = tags._disk(t) if drop_disk else None
        if d is not None:
            del flags[d[0]]
            if d[0] < len(flags) and flags[d[0]].casefold() in tags._DISK_LABELS:
                del flags[d[0]]
        return (it.dat, t.style, t.title.casefold(), t.regions, () if t.languages_implied else t.languages, t.status, tuple(flags),
                t.version, tags.date_key(t.date))

    groups: dict[tuple, list[tuple[str, list[Item], Optional[ChosenSet]]]] = {}
    seen: set[int] = set()
    for it in items:
        d = sel.decisions.get(it.key)
        if d is None or d.action != KEEP or it.key in seen:
            continue
        cs = in_set.get(it.key)
        if cs is not None:
            members = [by_key[k] for k in cs.slots.values() if k in by_key]
            seen.update(m.key for m in members)
            anchor = by_key.get(cs.slots.get(min(cs.slots)), it)
            groups.setdefault(version(anchor, True) + (cs.total,), []).append((cs.name, members, cs))
        else:
            seen.add(it.key)
            groups.setdefault(version(it, False) + (0,), []).append((_rom_name(it), [it], None))
    gone: set[int] = set()
    for copies in groups.values():
        if len(copies) < 2:
            continue
        flawed = [c for c in copies if any(tags.exclusion_rules(_tags_of(m)) & FLAWED_RULES for m in c[1])]
        clean = [c for c in copies if c not in flawed]
        if not flawed or not clean:
            continue
        winner = min(c[0] for c in clean)
        used = {m.key for c in clean for m in c[1]}             # (a disk the clean set shares with the flawed one stays)
        for _name, members, cs in flawed:
            for m in members:
                if m.key in used or sel.decisions[m.key].codes == (BORROWED_CODE,):
                    continue                                   # (a disk borrowed from another edition stays with its own set)
                sel.decisions[m.key] = Decision(key=m.key, action=SUPERSEDED, superseded_by=winner,
                                                reason=f"superseded by {winner} (a clean dump of the same version)")
            if cs is not None:
                gone.add(cs.id)
    if gone:
        sel.sets = [cs for cs in sel.sets if cs.id not in gone]
    # a set with a disk that is not kept is no set: two sets of a version share their disks and differ in a few (a flawed disk 1 and 6 beside
    # clean ones), so the comparison above may have put a disk of one aside without ever calling the set flawed - its playlist would point at it
    sel.sets = [cs for cs in sel.sets
                if all((d := sel.decisions.get(k)) is None or d.action == KEEP for k in cs.slots.values())]


def title_key(it: Item) -> str:
    """The game an item is of, as a person would say it: its title in lower case, letters and digits only (``Push-Over`` = ``pushover``)
    - the same for every release, edition, publisher, region and DAT of a system (floppies, an SPS release, another region's discs).
    An edition named after the title is the same game (``Wing Commander - Platinum Edition`` = ``Wing Commander``); any other subtitle
    is not (``Lemmings - Oh No! More Lemmings`` is a game of its own)."""
    title = _EDITION_RE.sub("", _tags_of(it).title.casefold())
    return re.sub(r"[^0-9a-z]+", "", title)


_EDITION_RE = re.compile(r"\s+-\s+[^-]*\bedition\s*$")


def kept_titles(items: Sequence[Item], sel: Selection) -> dict[str, str]:
    """``{title_key: name}`` of the games ``sel`` keeps complete (a kept item with no disk missing)."""
    out: dict[str, str] = {}
    for it in items:
        d = sel.decisions.get(it.key)
        if d is not None and d.action == KEEP and not d.missing:
            out.setdefault(title_key(it), it.rom.game or _rom_name(it))
    return out


def _mark_covered(items: Sequence[Item], sel: Selection) -> None:
    """An incomplete set of a game that is kept complete in another version (the original SPS disks next to cracked floppies with
    a disk missing, another region's discs) is no problem to report: ``covered_by`` names the version that is kept. The set is
    still set aside as before."""
    if not sel.incomplete:
        return
    have = kept_titles(items, sel)
    by_key = {it.key: it for it in items}
    for x in sel.incomplete:
        mine = [by_key[k] for k in x.present.values() if k in by_key]
        if mine:
            x.covered_by = have.get(title_key(mine[0]), "")


def content_names(item: Item) -> tuple[str, ...]:
    """The names RetroArch may use for the item's saves: the file's name without extension (the CHD of a disc game, the
    archive of a zipped ROM) and, for a member of an archive, the member's."""
    names = [item.path.stem]
    if item.member:
        names.append(Path(item.member).stem)
    return tuple(names)


def _release_of_parts(it: Item) -> Optional[tuple]:
    """The release ``it`` is one disk / disc / image of, when it is one of several (a TOSEC "Disk N of M", a Redump "(Disc N)", an
    image of an SPS release): what is kept or set aside as a whole. None for a game of one file."""
    if it.dat in _image_set_dats() and it.rom.game:
        return ("sps", it.dat, _release_name(it))
    t = _tags_of(it)
    if _style(it) == STYLE_TOSEC:
        d = tags._disk(t)
        return (("disk", it.dat, tags._set_key(t, d[0]), t.version, tags.date_key(t.date))    # (a release: its version and date too)
                if d is not None and d[2] >= 2 else None)
    if tags.disc_number(t):
        return ("disc", it.dat, t.title.casefold(), t.regions, () if t.languages_implied else t.languages, t.status,
                tuple(f for f in t.flags if tags.flag_kind(f) != "disk"), tuple(f for f in t.dump_flags if f != "!"))
    return None


def apply_prefer_saved(sel: Selection, items: Sequence[Item], saved: frozenset) -> None:
    """Of the releases / versions of a game, keep the one you have saves for: a release the rules set aside for another one (a newer
    version, a release of a region higher in your list) that has saves, while the one they keep has none, is kept instead, and the other
    is set aside for it. Discs of a multi-disc set are left to their rules (a set is kept whole)."""
    from .retroarch import fold_name

    def has(it: Item) -> bool:
        return any(fold_name(n) in saved for n in content_names(it))

    by_name: dict[str, list[Item]] = {}
    for it in items:
        by_name.setdefault(_rom_name(it), []).append(it)
        if _style(it) == STYLE_TOSEC and _stem(_rom_name(it)) != _rom_name(it):
            by_name.setdefault(_stem(_rom_name(it)), []).append(it)     # (a TOSEC winner is named by its stem: _winner_name)
    losers: dict[str, list[Item]] = {}
    for it in items:
        d = sel.decisions.get(it.key)
        if d is not None and d.action == SUPERSEDED and d.superseded_by and d.set_id is None and not it.link and has(it) \
                and _release_of_parts(it) is None:
            losers.setdefault(d.superseded_by, []).append(it)
    for win_name, cands in losers.items():
        winners = [w for w in by_name.get(win_name, ()) if (wd := sel.decisions.get(w.key)) is not None and wd.action == KEEP and wd.set_id is None
                   and _release_of_parts(w) is None]
        if not winners or any(has(w) for w in winners):
            continue
        pick = max(cands, key=lambda i: (_rom_name(i).casefold(), i.key))   # (of several with saves: the last by name, the newest revision)
        pname = _rom_name(pick)
        sel.decisions[pick.key] = Decision(key=pick.key, action=KEEP, codes=(SAVED_PREFER,),
                                           reason=f"kept instead of {win_name}: you have saves for this one")
        for w in winners:
            sel.decisions[w.key] = Decision(key=w.key, action=SUPERSEDED, superseded_by=pname,
                                            reason=f"superseded by {pname}, which you have saves for")
        for other in cands:
            if other is not pick:
                d = sel.decisions[other.key]
                sel.decisions[other.key] = dataclasses.replace(d, superseded_by=pname)


def saved_choice(profile: LibraryProfile, dat: str, game: str) -> str:
    """What happens to a game the rules would set aside when it has saves: its own choice, else the profile's."""
    for d, n, choice in profile.saved_overrides:
        if d == dat and n == game:
            return choice
    return profile.saved_games


def apply_saved(sel: Selection, items: Sequence[Item], saved: frozenset, profile: Optional[LibraryProfile] = None,
                platform_name: str = "") -> None:
    """Keep the games the rules set aside (excluded or superseded) when RetroArch has saves or states for them and their
    choice is ``keep`` (``profile.saved_games``, or the game's own in ``profile.saved_overrides``; without a profile: all).
    A user's own "always exclude" still wins (it is applied after this). Files of a kept multi-disk set, symbolic links and incomplete
    games are left to their rules; a save named after one disk of a release of several (disk 1, launched without a playlist) keeps
    every disk of it (:func:`_release_of_parts`), never half a game."""
    from .retroarch import fold_name

    parts: dict[tuple, list[Item]] = {}
    for it in items:
        rel = _release_of_parts(it)
        if rel is not None:
            parts.setdefault(rel, []).append(it)

    def set_aside(it: Item) -> bool:
        d = sel.decisions.get(it.key)
        return d is not None and d.action in (EXCLUDED, SUPERSEDED) and d.set_id is None and not it.link

    for it in items:
        d = sel.decisions.get(it.key)
        if not set_aside(it):
            continue
        choice = saved_choice(profile, *override_game(it.dat, it.rom)) if profile is not None else "keep"
        if choice not in ("keep", "carry", "convert"):
            continue
        if not any(fold_name(n) in saved for n in content_names(it)):
            continue
        if choice in ("carry", "convert") and d.action == SUPERSEDED and _carries(sel, items, it, d, platform_name, choice == "convert"):
            continue                                                  # it goes: its saves are copied to the version that replaces it
        if d.action == SUPERSEDED and _same_code(sel, items, it, d, platform_name):
            continue                                                  # it goes: the version that replaces it has the same game code, the saves work there as they are
        rel = _release_of_parts(it)
        for m in (parts[rel] if rel is not None else [it]):
            if m is it or set_aside(m):
                sel.decisions[m.key] = Decision(key=m.key, action=KEEP, codes=(SAVED_KEEP,), reason=SAVED_KEEP_TEXT)


def _same_code(sel: Selection, items: Sequence[Item], it: Item, d: Decision, platform_name: str) -> bool:
    """True when the game that replaces ``it`` is kept and has the same game code (PlayStation 2: PCSX2 names a save by the code, so it
    works in every release with it)."""
    from . import pcsx2, savecarry

    if not d.superseded_by or platform_name not in savecarry.SERIAL_SAVES:
        return False
    for other in items:
        od = sel.decisions.get(other.key)
        if od is not None and od.action == KEEP and _rom_name(other) == d.superseded_by:
            mine = pcsx2.read_serial(it.path, psx=True)
            return bool(mine) and mine == pcsx2.read_serial(other.path, psx=True)
    return False


def _carries(sel: Selection, items: Sequence[Item], it: Item, d: Decision, platform_name: str, convert: bool = False) -> bool:
    """True when the game that replaces ``it`` is kept and the saves of ``it`` work in it (``convert``: or can be made to, which needs the
    product codes of both discs)."""
    from . import pcsx2, savecarry

    if not d.superseded_by:
        return False
    for other in items:
        od = sel.decisions.get(other.key)
        if od is not None and od.action == KEEP and _rom_name(other) == d.superseded_by:
            if convert and platform_name in savecarry.CONVERTIBLE:
                return bool(pcsx2.read_serial(it.path, psx=True) and pcsx2.read_serial(other.path, psx=True))
            return savecarry.works(platform_name, _tags_of(it).regions, _tags_of(other).regions)
    return False


def override_game(dat: str, rom: Any) -> tuple[str, str]:
    """The (DAT, game) an override is stored under: the set name (No-Intro / Redump) or the rom name (TOSEC)."""
    return (dat, getattr(rom, "set_name", "") or rom.name)


def apply_overrides(sel: Selection, items: Sequence[Item], profile: LibraryProfile) -> None:
    """The user's per-game choices beat every rule: ``keep`` keeps the file (whatever excluded / superseded / left it
    out), ``exclude`` sets it aside. A playlist is dropped when one of its disks is excluded this way, and a disk it borrowed from
    another edition goes back to its own decision (the language filter's, say): it was only kept for that set."""
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
        dropped = [cs for cs in sel.sets if excluded_keys & set(cs.slots.values())]
        sel.sets = [cs for cs in sel.sets if cs not in dropped]
        for cs in dropped:
            for note in cs.borrowed.values():
                key = note["key"]
                d = sel.decisions.get(key)
                if key in excluded_keys or d is None or BORROWED_CODE not in d.codes or d.set_id != cs.id:
                    continue
                still = next((o for o in sel.sets if key in o.slots.values()), None)
                if still is not None:                         # (another kept set borrows it too: it stays, part of that one)
                    d.set_id = still.id
                elif sel.before_borrow.get(key) is not None:
                    sel.decisions[key] = sel.before_borrow[key]
            # its other disks are a set that lacks one now, as when that disk is not there (reported; "Complete multi-disk sets only":
            # set aside) - never a whole game of yours
            miss = tuple(sorted(slot for slot, k in cs.slots.items() if k in excluded_keys))
            present = {slot: k for slot, k in sorted(cs.slots.items()) if k not in excluded_keys
                       and (dd := sel.decisions.get(k)) is not None and dd.action == KEEP and dd.set_id == cs.id}
            if not present:
                continue
            text = f"incomplete set: disk {', '.join(str(m) for m in miss)} excluded because you chose to always exclude it"
            for k in present.values():
                dd = sel.decisions[k]
                dd.set_id, dd.missing = None, miss
                if profile.complete_only:
                    dd.action, dd.codes, dd.reason = INCOMPLETE, (), text
                else:
                    dd.reason = text + " - kept, no playlist"
            sel.incomplete.append(IncompleteSet(dat=cs.dat, name=cs.name, total=cs.total, present=present, missing=miss,
                                                kept=not profile.complete_only))


def _select_base(items: Sequence[Item], profile: LibraryProfile, platform: "Platform",
                 lang_games: Optional[frozenset] = None) -> Selection:
    """Every rule except the rating filter. ``lang_games``: see :func:`language_games` (None = the items themselves)."""
    sel = Selection(combine_alt=profile.combine_alt_titles)
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
                           parsed=_tags_of(it) if _style(it) == STYLE_REDUMP or it.dat in _image_set_dats() else None)
        if info and it.dat in scope.disc:              # (a CD32 / CDTV disc is the game itself: never "a conversion")
            info = [(r, x) for r, x in info if r != "cd_conversion"]
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
        only_lang = [(it, info) for it, info in excluded       # (a budget re-release in another language: the budget rule below decides it)
                     if _style(it) in OTHER_LANGUAGE_STYLES and {code for code, _x in info} & {LANGUAGE_CODE}
                     and {code for code, _x in info} <= {LANGUAGE_CODE, "budget"}]
        if only_lang:
            known = lang_games if lang_games is not None else language_games(items, profile, platform)
            back = {it.key: info for it, info in only_lang if game_key(it, profile.combine_alt_titles) not in known}
            if back:
                excluded = [(it, info) if it.key not in back else (it, [x for x in info if x[0] != LANGUAGE_CODE])
                            for it, info in excluded]
                excluded = [(it, info) for it, info in excluded if info]
                for it, info in only_lang:
                    if it.key in back and all(code == LANGUAGE_CODE for code, _x in info):
                        alive.append(it)
                        rescued[it.key] = (f"kept although it only exists in {info[0][1]}: no version in your languages exists")
    # a budget re-release is set aside only when another release of its game is there: the only release of a game stays
    only_budget = [(it, info) for it, info in excluded if {code for code, _x in info} == {"budget"}]
    if only_budget:                                   # (a release that lacks a disc is no other release: the complete budget set stays)
        parts: dict[tuple, list[Item]] = {}
        for it in alive:
            if (rel := _release_of_parts(it)) is not None and rel[0] == "disc":
                parts.setdefault(rel, []).append(it)
        others = {game_key(it, profile.combine_alt_titles) for it in alive
                  if (rel := _release_of_parts(it)) is None or rel[0] != "disc" or not _discs_missing(parts[rel])}
        back = {it.key for it, _info in only_budget if game_key(it, profile.combine_alt_titles) not in others}
        if back:
            excluded = [(it, info) for it, info in excluded if it.key not in back]
            for it, info in only_budget:
                if it.key in back:
                    alive.append(it)
                    rescued[it.key] = f"kept although a budget re-release {info[0][1]}: no other release of the game is here"
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

    # a disc's files are decided together: one excluded file excludes its disc
    games_out: dict[tuple, list[tuple[str, str]]] = {}
    for it, info in excluded:
        if it.dat in scope.disc:
            games_out.setdefault((it.dat, it.rom.game), info)
    if games_out:
        for dat in list(by_dat):
            for it in [i for i in by_dat[dat] if (i.dat, i.rom.game) in games_out]:
                info = games_out[(it.dat, it.rom.game)]
                by_dat[dat].remove(it)
                decide(it, EXCLUDED, codes=tuple(dict.fromkeys(r for r, _x in info)), detail=" ".join(x for _r, x in info),
                       reason="excluded with its disc: " + _describe(info))
            if not by_dat[dat]:
                del by_dat[dat]
    for dat in sorted(by_dat):
        dat_items = by_dat[dat]
        if dat in scope.disc:
            _select_discs(dat, dat_items, profile, decide)
            continue
        if all(_style(i) == STYLE_REDUMP for i in dat_items):
            if dat in latest_dats:                     # (discs of a game stay together and become a playlist, whatever keeps the versions)
                one = dat in scope.region and profile.one_per_game
                _select_redump(dat, dat_items, profile, one, sel, decide, next_id,
                               complete_only=profile.complete_only and getattr(platform, "layout", "") == "game_folder",
                               every_revision=not (profile.latest_only or one))
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
    if scope.disc and profile.best_variant:
        _rank_across(items, sel, profile, platform, images=profile.one_per_game)
    if profile.prefer_unprotected and profile.best_variant and _dats(platform, "image_set_dats"):
        _prefer_unprotected(items, sel, profile, platform)
    if profile.prefer_whdload and profile.languages and _dats(platform, "best_variant_dats"):
        _whdload_language(items, sel, profile, platform)
    if profile.one_per_game and profile.best_variant and _dats(platform, "image_set_dats"):
        _one_release(items, sel, profile, platform)
    return sel


def _whdload_language(items: Sequence[Item], sel: Selection, profile: LibraryProfile, platform: Any) -> None:
    """A game none of whose kept versions WHDLoad is known to run (the shipped list: boots in PUAE) keeps, as well, a version set aside
    only for its language that it is known to run - an ADF set whose every disk is here, or an SPS release (the user: "if there's no
    English version then another language is OK if it's known working")."""
    from . import whdready
    gdat = getattr(platform, "game_dat", "")
    image_sets = set(_dats(platform, "image_set_dats"))
    by_key = {it.key: it for it in items}
    in_set = {k: cs for cs in sel.sets for k in cs.slots.values()}
    releases: dict[tuple[str, str], list[Item]] = {}            # an SPS release's images (once: looking them up per image was n²)
    for it in items:
        if it.dat in image_sets:
            releases.setdefault((it.dat, _release_name(it)), []).append(it)
    verdicts: dict[tuple, bool] = {}

    def boots(members: list[Item]) -> bool:
        key = tuple(sorted(m.key for m in members))
        if key not in verdicts:
            if members[0].dat in image_sets:
                verdicts[key] = (whdready.release_of(members[0].rom) or [None])[0] == whdready.BOOTS
            else:
                verdicts[key] = whdready.level_of(m.rom for m in members) == whdready.BOOTS
        return verdicts[key]

    running: set[tuple] = set()
    for it in items:
        d = sel.decisions.get(it.key)
        if d is None or d.action != KEEP or (it.dat != gdat and it.dat not in image_sets):
            continue
        cs = in_set.get(it.key)
        members = [by_key[k] for k in cs.slots.values() if k in by_key] if cs is not None else \
            releases[(it.dat, _release_name(it))] if it.dat in image_sets else [it]
        if boots(members):
            running.add(game_key(it, profile.combine_alt_titles))
    lang_out = [it for it in items if (it.dat == gdat or it.dat in image_sets) and (d := sel.decisions.get(it.key)) is not None
                and d.action == EXCLUDED and d.codes == (LANGUAGE_CODE,) and not it.link]
    by_name = {whdready.shipped_name(it.rom.name, it.rom.sha1 or ""): it for it in lang_out if it.dat == gdat}
    lang_releases: dict[tuple[str, str], list[Item]] = {}
    for it in lang_out:
        if it.dat in image_sets:
            lang_releases.setdefault((it.dat, _release_name(it)), []).append(it)
    done: set[tuple] = set()
    for it in sorted(lang_out, key=lambda i: (i.rom.name.casefold(), i.key)):
        g = game_key(it, profile.combine_alt_titles)
        if g in running or g in done:
            continue
        if it.dat in image_sets:
            members = lang_releases[(it.dat, _release_name(it))]
            ok = boots(members)
        else:
            members, ok = [], False
            for k in whdready.sets_with(whdready.shipped_name(it.rom.name, it.rom.sha1 or "")):
                if (whdready.known().get(k) or [None])[0] == whdready.BOOTS and all(n in by_name for n in k.split("\n")):
                    members, ok = [by_name[n] for n in k.split("\n")], True
                    break
        if not ok:
            continue
        langs = "/".join(sorted(tags.variant_languages(_tags_of(it)))) or "another language"
        sid = None
        disks = {(_disk_info(_tags_of(m)) or (0, 0))[0]: m for m in members}
        if len(members) > 1 and it.dat == gdat and sorted(disks) == list(range(1, len(members) + 1)):   # (its disks a playlist)
            sid = max([cs.id for cs in sel.sets] + [0]) + 1
            name = _DISC_RE.sub("", _stem(disks[1].rom.name))
            sel.sets.append(ChosenSet(id=sid, dat=gdat, name=name, total=len(members), slots={n: m.key for n, m in sorted(disks.items())},
                                      labels={}, flags=(), cracked=any(tags.is_cracked(_tags_of(m)) for m in members)))
        for m in members:
            sel.decisions[m.key] = Decision(key=m.key, action=KEEP, codes=(WHDLOAD_LANGUAGE,), set_id=sid,
                                            reason=f"kept although in {langs}: no version in your languages is known to run with "
                                                   "WHDLoad, this one is")
        done.add(g)


_DISC_RE = re.compile(r"\s*\(Dis[ck] (\d+) of (\d+)\)")
_IMAGE_DISK_RE = re.compile(r"\s*\(Dis[kc] (?:\d+|[A-Z]) of (?:\d+|[A-Z])\)")


def _release_name(it: Item) -> str:
    """The release an image of an original (``Platform.image_set_dats``) belongs to: its game name before the disk's place in the set. An
    SPS release is one entry and names itself; the Unofficial IPF DAT lists each disk of a game on its own (``(Disk 1 of 2)``,
    ``(Disk 2 of 2)(Utility)[cp manual code]``): they are one release, not one release a disk."""
    m = _IMAGE_DISK_RE.search(it.rom.game)
    return it.rom.game[:m.start()] if m else it.rom.game
_SHEETS = (".cue", ".ccd", ".mds")


def _select_discs(dat: str, dat_items: list[Item], profile: LibraryProfile, decide: Callable[..., None]) -> None:
    """A CD DAT's games: each a disc of several files (a cue sheet, its ISO and audio tracks; a CloneCD set) decided as one; a game
    of several discs ("(Disc 1 of 2)") as one release. Incomplete: a file of a disc or a disc of the release is not there. One
    release per game kept (``best_variant``): the best of your languages first, then a good dump ([!]), the newest, the fewest flags."""
    files = _DISC_FILES.get(dat, {})
    discs: dict[str, list[Item]] = {}
    for it in dat_items:
        discs.setdefault(it.rom.game, []).append(it)
    releases: dict[str, dict[int, str]] = {}
    totals: dict[str, int] = {}
    for game in discs:
        m = _DISC_RE.search(game)
        rkey = _DISC_RE.sub("", game) if m else game
        releases.setdefault(rkey, {})[int(m.group(1)) if m else 1] = game
        totals[rkey] = max(totals.get(rkey, 1), int(m.group(2)) if m else 1)

    # discs of one release whose names differ after the disc token ("(Disc 1 of 2)[!]" / "(Disc 2 of 2)", a per-disc label
    # "(Disc 1 of 2)(Game Disc)"): one release when together they are every disc, once each
    bases: dict[tuple, list[str]] = {}
    for rkey, by_no in releases.items():
        first = by_no[min(by_no)]
        m = _DISC_RE.search(first)
        if m and len(by_no) < totals[rkey]:
            bases.setdefault((first[:m.start()], totals[rkey]), []).append(rkey)
    for (_base, total), rkeys in bases.items():
        nums = [n for r in rkeys for n in releases[r]]
        if len(rkeys) > 1 and sorted(nums) == list(range(1, total + 1)):
            keep = min(rkeys, key=lambda r: min(releases[r]))
            for r in rkeys:
                if r != keep:
                    releases[keep].update(releases.pop(r))
                    totals.pop(r)

    def held(game: str) -> set[str]:                       # (the files of a disc that are here: one by one, or in an archive of them)
        return {it.rom.name for it in discs[game]} | {n for it in discs[game] for n in it.holds}

    def whole(game: str) -> bool:
        want = files.get(game)
        have = held(game)
        return want <= have if want else any(n.lower().endswith(_SHEETS) for n in have)

    complete, partial = [], []
    for rkey, by_no in releases.items():
        ok = len(by_no) == totals[rkey] and all(whole(g) for g in by_no.values())
        (complete if ok else partial).append(rkey)
    for rkey in partial:
        why = []
        if len(releases[rkey]) < totals[rkey]:
            why.append(f"{totals[rkey] - len(releases[rkey])} of its {totals[rkey]} discs are not here")
        for g in releases[rkey].values():
            want, have = files.get(g, frozenset()), held(g)
            if want and not want <= have:
                why.append(f"{len(want - have)} of the {len(want)} files of {g} are not here")
            elif not want and not any(n.lower().endswith(_SHEETS) for n in have):
                why.append(f"{g} has no cue sheet")
        for g in releases[rkey].values():
            for it in discs[g]:
                if profile.complete_only:
                    decide(it, INCOMPLETE, reason="incomplete disc: " + "; ".join(why))
                else:
                    decide(it, KEEP, reason="kept although incomplete: " + "; ".join(why))
    by_game: dict[tuple, list[str]] = {}
    for rkey in complete:
        anchor = discs[releases[rkey][min(releases[rkey])]][0]
        by_game.setdefault(game_key(anchor, profile.combine_alt_titles), []).append(rkey)

    def parsed(rkey: str) -> tags.Tags:
        return tags.parse_name(rkey + ".cue", STYLE_TOSEC)

    def verified(rkey: str) -> bool:
        return any(f == "!" for g in releases[rkey].values() for it in discs[g]
                   for f in tags.parse_name(it.rom.name, STYLE_TOSEC).dump_flags)

    for _k, rkeys in by_game.items():
        rkeys.sort(key=lambda r: (r.casefold(), r))                         # (then: the fewest flags, the newest, a good dump)
        rkeys.sort(key=lambda r: len(parsed(r).dump_flags))
        rkeys.sort(key=lambda r: tags.date_key(parsed(r).date), reverse=True)
        rkeys.sort(key=lambda r: not verified(r))
        rkeys.sort(key=lambda r: _lang_rank(tags.variant_languages(parsed(r)), profile))   # (first: your language order)
        win = rkeys[0]
        for rkey in rkeys:
            for g in releases[rkey].values():
                for it in discs[g]:
                    if rkey == win or not profile.best_variant:
                        decide(it, KEEP)
                    else:
                        decide(it, SUPERSEDED, superseded_by=win, reason=f"superseded by {win}")


def _rank_across(items: Sequence[Item], sel: Selection, profile: LibraryProfile, platform: Any, images: bool = False) -> None:
    """One release per game across the system's DATs: a game kept both as a disc (a CD DAT's) and on floppies (its games DAT) keeps
    the best of your languages, then the best by :data:`tags.PLATFORM_ORDER` - a CD32 disc, an AGA floppy, a CDTV disc, an ECS floppy, the rest; the others are
    superseded. Two discs of a game in two CD DATs ([ISO] and [IMG]): the one of the DAT listed first."""
    from .platforms import disc_class
    disc_dats = set(_dats(platform, "disc_dats"))
    gdat = getattr(platform, "game_dat", "")
    order = {d: i for i, d in enumerate(_dats(platform, "dats"))}
    image_sets = set(_dats(platform, "image_set_dats")) if images else set()      # (an original - SPS - is a release like a floppy set)
    releases: dict[tuple, dict[tuple, list[Item]]] = {}       # game -> release -> its kept files
    for it in items:
        d = sel.decisions.get(it.key)
        if d is None or d.action != KEEP or (it.dat not in disc_dats and it.dat != gdat and it.dat not in image_sets):
            continue
        if it.dat in disc_dats:                               # (the discs of the one release _select_discs kept: the name before the disc token)
            m = _DISC_RE.search(it.rom.game)
            rel = (it.dat, it.rom.game[:m.start()] if m else it.rom.game)
        elif it.dat in image_sets:
            rel = (it.dat, _release_name(it))
        else:
            rel = (it.dat, d.set_id if d.set_id is not None else it.rom.name)
        releases.setdefault(game_key(it, profile.combine_alt_titles), {}).setdefault(rel, []).append(it)

    def cls(rel: tuple, members: list[Item]) -> str:
        return disc_class(rel[0]) or tags.platform_class(_tags_of(members[0]))

    for _g, rels in releases.items():
        if len(rels) < 2 or not any(r[0] in disc_dats for r in rels):
            continue
        ranked = sorted(rels.items(), key=lambda kv: (_lang_rank(tags.variant_languages(_tags_of(kv[1][0])), profile),   # (your language order first)
                                                      tags.PLATFORM_ORDER.index(cls(*kv)), order.get(kv[0][0], 99)))
        best_rel, best_members = ranked[0]
        best_cls = cls(best_rel, best_members)
        winner = best_rel[1] if best_rel[0] in disc_dats or best_rel[0] in image_sets else _stem(best_members[0].rom.name)
        gone: set[int] = set()
        for rel, members in ranked[1:]:
            c = cls(rel, members)
            if c not in ("CD32", "CDTV") and best_cls not in ("CD32", "CDTV"):
                continue                                  # (floppies of a game: their own DAT's choice stands)
            for it in members:
                sel.decisions[it.key] = Decision(
                    key=it.key, action=SUPERSEDED, superseded_by=winner,
                    reason=f"superseded by {winner} (the {best_cls} release: CD32 > AGA > CDTV > ECS > OCS)")
                gone.add(it.key)
        if gone:
            sel.sets = [cs for cs in sel.sets if not gone & set(cs.slots.values())]


def _whole(members: Sequence[Item]) -> bool:
    """True unless ``members`` - the kept images of one release - are some of the disks of a set (``Disk 2 of 3`` and no disk 3 among them)."""
    nums: set[int] = set()
    total = 0
    for it in members:
        d = _disk_info(_tags_of(it))
        if d is not None:
            nums.add(d[0])
            total = max(total, d[1])
    return not total or nums >= set(range(1, total + 1))


def _one_release(items: Sequence[Item], sel: Selection, profile: LibraryProfile, platform: Any) -> None:
    """One release per game among the originals (SPS / IPF, ``Platform.image_set_dats``) and the floppy sets kept of it (the games DAT): the
    CD32 and CDTV discs were ranked against them by :func:`_rank_across`. In order: a version a WHDLoad install boots from (when WHDLoad is
    preferred), your languages, how well an install runs from it (``whdready.level``), a clean dump, the best chipset (AGA > ECS > OCS), the
    newest version (an original or a floppy set alike), the original before a floppy set, the fewest extra tags (a compilation, a budget
    re-release, an alternate dump), then the name. The others are superseded."""
    from . import whdready
    image_sets = set(_dats(platform, "image_set_dats"))
    image_order = {d: i for i, d in enumerate(_dats(platform, "image_set_dats"))}
    gdat = getattr(platform, "game_dat", "")
    disc_dats = set(_dats(platform, "disc_dats"))
    releases: dict[tuple, dict[tuple, list[Item]]] = {}       # game -> release -> its kept files
    discs: dict[tuple, str] = {}                              # game -> its kept CD32 / CDTV disc (a disc needs no WHDLoad: it wins outright)
    for it in items:
        d = sel.decisions.get(it.key)
        if d is None or d.action != KEEP:
            continue
        if it.dat in disc_dats:
            m = _DISC_RE.search(it.rom.game)
            discs.setdefault(game_key(it, profile.combine_alt_titles), it.rom.game[:m.start()] if m else it.rom.game)
            continue
        if it.dat not in image_sets and it.dat != gdat:
            continue
        if it.dat == gdat and d.set_id is None and _disk_info(_tags_of(it)) is not None:
            continue                                          # (a disk of a set that is not complete: kept as it is, no release of its own)
        rel = (it.dat, _release_name(it)) if it.dat in image_sets else (it.dat, d.set_id if d.set_id is not None else it.rom.name)
        releases.setdefault(game_key(it, profile.combine_alt_titles), {}).setdefault(rel, []).append(it)
    gone: set[int] = set()
    for g, rels in releases.items():
        if g in discs:
            for members in rels.values():
                for it in members:
                    sel.decisions[it.key] = Decision(key=it.key, action=SUPERSEDED, superseded_by=discs[g],
                                                     reason=f"superseded by {discs[g]} (the game's disc needs no WHDLoad: one release of the game)")
                    gone.add(it.key)
            continue
        if len(rels) < 2:
            continue
        info: dict[tuple, tuple] = {}                          # release -> (tags of it, its name, its WHDLoad level, flawed)
        for rel, members in rels.items():
            anchor = members[0] if rel[0] in image_sets else min(members, key=lambda m: (m.rom.name, m.key))
            t = _tags_of(anchor)
            level = (whdready.release_of(anchor.rom) or [None])[0] if rel[0] in image_sets else whdready.level_of(m.rom for m in members)
            flawed = any(tags.exclusion_rules(_tags_of(m)) & FLAWED_RULES for m in members)
            info[rel] = (t, rel[1] if rel[0] in image_sets else _stem(anchor.rom.name), level, flawed, _whole(members))
        vkeys = tags.group_version_keys([v[1] + ".ipf" if r[0] in image_sets else v[1] for r, v in info.items()], STYLE_TOSEC)
        pool = list(rels)
        for key in (lambda r: not info[r][4],                    # (a release with a disk missing - the others are in another language - never beats a whole one)
                    lambda r: profile.prefer_whdload and info[r][2] != whdready.BOOTS,      # (a version that boots with WHDLoad: also in another language, as _whdload_language kept it)
                    lambda r: _lang_rank(tags.variant_languages(info[r][0]), profile),
                    lambda r: -(info[r][2] or 0) if profile.prefer_whdload else 0,
                    lambda r: info[r][3],
                    lambda r: tags.PLATFORM_ORDER.index(tags.platform_class(info[r][0]))):
            best = min(key(r) for r in pool)
            pool = [r for r in pool if key(r) == best]
        vk = lambda r: vkeys[info[r][1] + ".ipf" if r[0] in image_sets else info[r][1]]       # noqa: E731
        newest = max(vk(r) for r in pool)                      # (the newest version: whatever its form - an original or a floppy set)
        pool = [r for r in pool if vk(r) == newest]
        win = min(pool, key=lambda r: (r[0] not in image_sets, sum(1 for f in info[r][0].dump_flags if not f.isdigit()),
                                       image_order.get(r[0], 99), info[r][1].casefold(), info[r][1], r))     # (SPS before the Unofficial IPF: then the name)
        for rel, members in rels.items():
            if rel == win:
                continue
            for it in members:
                sel.decisions[it.key] = Decision(key=it.key, action=SUPERSEDED, superseded_by=info[win][1],
                                                 reason=f"superseded by {info[win][1]} (one release of the game)")
                gone.add(it.key)
    if gone:
        sel.sets = [cs for cs in sel.sets if not gone & set(cs.slots.values())]


# The protections OpenRetro gives a release that make it ask for the manual (or a code wheel / sheet that came in the box).
MANUAL_CHECKS = frozenset({"Manual", "Code Sheet", "Code Wheel", "Codewheel", "Word Lookup In Manual", "Registration Code"})


def _prefer_unprotected(items: Sequence[Item], sel: Selection, profile: LibraryProfile, platform: Any) -> None:
    """An original (an SPS / IPF release, ``Platform.image_set_dats``) that asks for the manual - OpenRetro's protection of the release:
    a manual, a code wheel, a code sheet - and that its WHDLoad install does not take the check out of (the install is not known to
    boot from it, or its ReadMe does not say it removes the check: ``whdready.leaves_manual``) gives way to a kept crack of the game
    (TOSEC's ``[cr]``) the install boots from, or to any kept crack when no install of the game is known: it is superseded by it."""
    from . import whdready
    try:
        from . import openretro
        odb = openretro.database()
    except Exception:  # noqa: BLE001 - no OpenRetro data: nothing is known of protections, nothing changes
        odb = None
    if odb is None:
        return
    image_sets = set(_dats(platform, "image_set_dats"))
    gdat = getattr(platform, "game_dat", "")
    originals: dict[tuple, dict[str, list[Item]]] = {}     # game -> release -> its kept images (one zip: the library has one of them)
    cracks: dict[tuple, dict[Any, list[Item]]] = {}        # game -> kept set of the games DAT with a cracked disk -> its disks
    titles: dict[tuple, set[str]] = {}                     # game -> the titles of every version of it (is an install known?)
    kept: dict[tuple, dict[Any, list[Item]]] = {}
    for it in items:
        if it.dat not in image_sets and it.dat != gdat:
            continue
        g = game_key(it, profile.combine_alt_titles)
        titles.setdefault(g, set()).add(it.rom.name)
        d = sel.decisions.get(it.key)
        if d is None or d.action != KEEP:
            continue
        if it.dat in image_sets:
            originals.setdefault(g, {}).setdefault(_release_name(it), []).append(it)
        else:
            kept.setdefault(g, {}).setdefault(d.set_id if d.set_id is not None else it.rom.name, []).append(it)
    for g, sets in kept.items():                           # (a set is a crack when any disk is: TOSEC often tags disk 1 only)
        for sid, members in sets.items():
            if any(tags.is_cracked(_tags_of(m)) for m in members):
                cracks.setdefault(g, {})[sid] = members
    gone: set[int] = set()
    for g, releases in originals.items():
        kept_cracks = list((cracks.get(g) or {}).values())
        if not kept_cracks:
            continue
        booting = [m for m in kept_cracks if whdready.level_of(x.rom for x in m) == whdready.BOOTS]
        installs = any(whdready.has_install(t) for t in titles.get(g, ()))
        crack = (booting or ([] if installs else kept_cracks))[:1]
        if not crack:
            continue
        winner = _stem(sorted(x.rom.name for x in crack[0])[0])
        for name, members in releases.items():
            release = odb.release([m.rom.sha1 for m in members if m.rom.sha1])
            check = (release or {}).get("protection", "")
            if check not in MANUAL_CHECKS:
                continue
            lv, arc = (whdready.release_of(members[0].rom) or [None, None])[:2]
            if lv == whdready.BOOTS and not whdready.leaves_manual(arc):
                continue                                   # (its install boots and takes the check out)
            how = "its WHDLoad install boots from the crack" if booting else "the game has no WHDLoad install"
            for it in members:
                sel.decisions[it.key] = Decision(
                    key=it.key, action=SUPERSEDED, superseded_by=winner,
                    reason=f"superseded by {winner}: this original asks for the manual ({check.lower()}) and {how}")
                gone.add(it.key)
    if gone:
        sel.sets = [cs for cs in sel.sets if not gone & set(cs.slots.values())]


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
        if d is None or d.action != KEEP or (it.dat not in rdats and game_dat(it.dat) not in rdats):   # (a CD32 disc, an SPS release: the games DAT's)
            continue
        if _tags_of(it).bios:                              # (a [BIOS] set is no game: never rated, never counted as one)
            continue
        owner = owner_of.get(it.key, it)
        key = game_key(owner, profile.combine_alt_titles)
        u = units.get(key)
        if u is None:
            title = _tags_of(owner).title
            r = None if tags.bonus_disc(_tags_of(owner)) else lookup(title)        # (a bonus disc is no game of its own: not rated as it)
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
    _rate_incomplete(sel, items, profile, platform, ctx, cutoff)
    sel.rating = {"min_rating": profile.min_rating, "top_n": profile.top_n, "min_votes": profile.min_votes,
                  "keep_unrated": profile.keep_unrated, "rank_scope": profile.rank_scope,
                  "games": len(units), "rated": rated_games, "unrated": len(units) - rated_games,
                  "kept": kept_games, "excluded": len(units) - kept_games, "excluded_by": dropped}


def _rate_incomplete(sel: Selection, items: Sequence[Item], profile: LibraryProfile, platform: Any,
                     ctx: RatingContext, cutoff: tuple) -> None:
    """A game the rating filter would drop anyway is no problem to report when you have it only in part: every disk of it set aside as
    incomplete - of a set that is reported or of none - is excluded for the game's rating like a kept game's (they were set aside before
    the filter ran, so they are no unit of :func:`apply_ratings`); the reported sets left with no such disk go."""
    from . import ratings as _ratings

    rdats = set(_ratings.rated_dats(platform))
    games: dict[tuple, list[Item]] = {}
    for it in items:
        d = sel.decisions.get(it.key)
        if d is not None and d.action == INCOMPLETE and (it.dat in rdats or game_dat(it.dat) in rdats):
            games.setdefault(game_key(it, profile.combine_alt_titles), []).append(it)
    for key, mine in games.items():
        owner = min(mine, key=lambda it: (_rom_name(it), it.key))
        title = _tags_of(owner).title
        r = None if tags.bonus_disc(_tags_of(owner)) else ctx.lookup(title)
        if r is not None and r[1] < profile.min_votes:
            r = None
        codes: list[str] = []
        if r is None:
            if not profile.keep_unrated:
                codes.append(RATING_UNRATED)
            detail = "no usable rating"
        else:
            detail = f"rated {r[0]:.1f} ({r[1]} vote{'' if r[1] == 1 else 's'})"
            if profile.min_rating is not None and r[0] < profile.min_rating:
                codes.append(RATING_LOW)
            if profile.top_n is not None and _rank_key(r[0], r[1], title, key)[:3] > cutoff:
                codes.append(RATING_NOT_TOP)
        if not codes:
            continue
        for it in mine:
            d = sel.decisions[it.key]
            missing = ", ".join(str(m) for m in d.missing)
            sel.decisions[it.key] = Decision(
                key=it.key, action=EXCLUDED, codes=tuple(codes), detail=detail,
                reason=f"excluded: {_describe([(c, '') for c in codes]).strip()} ({detail}"
                       + (f"; incomplete: missing disk {missing})" if missing else "; incomplete)"))
    sel.incomplete = [x for x in sel.incomplete
                      if x.kept or any(sel.decisions.get(k) is not None and sel.decisions[k].action == INCOMPLETE for k in x.present.values())]


def _vanished(items: Sequence[Item], sel: Selection) -> list[Vanished]:
    """Titles of which no local variant is kept, with the reason (language first, then flags, rules). A title whose game is kept in
    another form (another publisher's release under "Combine alternate titles", its CD32 disc, its floppies next to an SPS release:
    ``game_key``) has not vanished."""
    groups: dict[tuple, list[Item]] = {}
    kept_games: set[tuple] = set()
    for it in items:
        groups.setdefault((it.dat, tags.title_key(_tags_of(it))), []).append(it)
        d = sel.decisions.get(it.key)
        if d is not None and d.action == KEEP:
            kept_games.add(game_key(it, sel.combine_alt))
    out: list[Vanished] = []
    # (a rating code only ever hits a variant that passed every other rule: it is the real blocker, so it ranks first)
    priority = {c: i for i, c in enumerate(RATING_CODES + (LANGUAGE_CODE,) + FLAG_CODES + RULES + (INCOMPLETE,))}
    for (dat, _k), members in groups.items():
        decs = [sel.decisions.get(m.key) for m in members]
        if any(d is None or d.action == KEEP for d in decs) or any(game_key(m, sel.combine_alt) in kept_games for m in members):
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


def _select_nointro(dat: str, dat_items: list[Item], profile: LibraryProfile, one_per_game: bool,
                    sel: Selection, decide: Any) -> None:
    by_form: dict[str, list[Item]] = {}
    for it in dat_items:
        by_form.setdefault(it.form, []).append(it)
    order = tags.region_tiers(tags.region_order(profile.region_priority), profile.region_ties)
    for form in sorted(by_form):
        members = by_form[form]
        if one_per_game:
            _one_per_game(members, profile, order, decide)
            continue
        names = list(dict.fromkeys(_rom_name(i) for i in members))
        older = tags.superseded(names, STYLE_NOINTRO, profile.prefer_editions)
        for it in members:
            newer = older.get(_rom_name(it))
            if newer:
                decide(it, SUPERSEDED, superseded_by=newer,
                       reason=f"superseded by {newer} (newer version of the same region)")
            else:
                decide(it, KEEP)


def _select_redump(dat: str, dat_items: list[Item], profile: LibraryProfile, one_per_game: bool,
                   sel: Selection, decide: Any, next_id: list[int], complete_only: bool = False, every_revision: bool = False) -> None:
    """Redump (Sega Dreamcast): ONE release per game, discs of a game always stay together.

    A *game* = ``tags.redump_game_key`` (title + status + product tags; the disc number is not part of
    it). Its *editions* are the (regions, explicit languages) groups; every disc of an edition belongs to
    that edition. Per game the best edition is kept (selected language order, region priority, newest
    revision, fewest extra tags, name) with ALL its discs - for every disc number the newest revision
    of that edition; everything else is ``superseded``. With ``one_per_game`` off every edition is kept
    (still only the newest revision of each disc; ``every_revision``: every revision too - "Latest versions only" off). A kept multi-disc edition becomes a :class:`ChosenSet`
    (a playlist) when all of its discs (``Item.disc_total`` or the highest disc number) are present,
    else an :class:`IncompleteSet` that is only reported (the discs stay), or, with ``complete_only``, set aside as ``incomplete``.
    """
    order = tags.region_tiers(tags.region_order(profile.region_priority), profile.region_ties)
    games: dict[tuple, list[Item]] = {}
    for it in dat_items:
        games.setdefault(_gkey(it, profile.combine_alt_titles), []).append(it)
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

        most = max(tags.disc_number(_tags_of(i)) for i in group)
        if one_per_game:
            # best edition: every disc there, selected language, region priority, then the newest revision (over its
            # discs), fewest extra tags, name
            def lr(members: list[Item]) -> tuple:                # (an edition with every disc before one that lacks a disc)
                t = _tags_of(first_of(members))
                return (_discs_missing(members, most) > 0, _lang_rank(tags.variant_languages(t), profile), tags.unlicensed(t),
                        tags.region_rank(t.regions, order))

            best = min(lr(m) for m in editions.values())
            pool = {k: m for k, m in editions.items() if lr(m) == best}
            newest = max(newest_of(m) for m in pool.values())
            pool = {k: m for k, m in pool.items() if newest_of(m) == newest}
            winner_key = min(pool, key=lambda k: (tags.extra_tag_count(_tags_of(first_of(pool[k]))),
                                                  _region_pos(_tags_of(first_of(pool[k])), profile),
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
                chosen[disc] = max(cands, key=lambda i: (-tags.edition_rank(_tags_of(i), profile.prefer_editions),     # (premium / plain / store / budget)
                                                         vkeys[_rom_name(i)], -tags.extra_tag_count(_tags_of(i)),
                                                         tuple(-ord(c) for c in _rom_name(i).casefold())))
            for disc, cands in by_disc.items():
                win = chosen[disc]
                for it in cands:
                    if it.key == win.key or _rom_name(it) == _rom_name(win) or every_revision:
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
                        missing=miss, kept=not complete_only))
                    text = "incomplete set (missing disc " + ", ".join(str(m) for m in miss) + ")"
                    for dec in (sel.decisions[it.key] for d in discs for it in by_disc[d]):   # (every revision kept: "Latest versions only" off)
                        if dec.action != KEEP:
                            continue
                        dec.missing = miss
                        if complete_only:                      # (the rule Complete multi-disk sets only: set aside until the set is complete)
                            dec.action, dec.reason = INCOMPLETE, text
                        else:
                            dec.reason = text + " - kept, no playlist"
        if one_per_game:
            for key, members in editions.items():
                if key in kept_editions:
                    continue
                for it in members:
                    decide(it, SUPERSEDED, superseded_by=winner_name,
                           reason=f"one version per game: kept {winner_name}")


def _discs_missing(members: Iterable[Item], least: int = 0) -> int:
    """How many discs a Redump edition / release lacks (0: a game of one disc, or every disc of ``Item.disc_total`` - the DAT's count for
    the release - else of the highest number, or ``least``: the highest disc number of any release of the game - a lone "Disc 1" next to
    another release's "Disc 2" lacks one, where the DAT says nothing: No-Intro)."""
    nums = {tags.disc_number(_tags_of(i)): i for i in members}
    discs = {n for n in nums if n > 0}
    if not discs:
        return 0
    known = max(nums[n].disc_total for n in discs)
    total = max(known, max(discs)) if known else max(max(discs), least)   # (a release of the DAT has its own count: a bonus disc elsewhere is not its)
    return sum(1 for d in range(1, total + 1) if d not in discs)


_DISC_TOKEN_RE = re.compile(r"\s*\(Dis[ck] (?:\d+|[A-Z])\)")


def _disc_free_name(name: str) -> str:
    """``"Skies of Arcadia (USA) (Disc 1)"`` -> ``"Skies of Arcadia (USA)"`` (the playlist's name)."""
    return _DISC_TOKEN_RE.sub("", name).strip()


def _other_discs(winner: Item, group: list[Item], vkeys: Mapping[str, tuple]) -> set[int]:
    """The keys of the other discs of ``winner``'s release (same regions, languages and tags but the disc and the version): the newest
    of each disc number. Empty for a single-disc game."""
    def release(t: tags.Tags) -> tuple:
        return (t.regions, () if t.languages_implied else t.languages, t.status,
                tuple(f for f in t.flags if tags.flag_kind(f) != "disk"), tuple(f for f in t.dump_flags if f != "!"))

    wt = _tags_of(winner)
    own = tags.disc_number(wt)
    if not own:
        return set()
    best: dict[int, Item] = {}
    for it in group:
        t = _tags_of(it)
        n = tags.disc_number(t)
        if n and n != own and release(t) == release(wt):
            if n not in best or vkeys[_rom_name(it)] > vkeys[_rom_name(best[n])]:
                best[n] = it
    return {it.key for it in best.values()}


def _one_per_game(members: list[Item], profile: LibraryProfile, order: list[str], decide: Any) -> None:
    """Keep ONE variant per game: a release with every disc, best selected language, best region (``order``), newest version of
    that region, fewest extra tags, then name."""
    games: dict[tuple, list[Item]] = {}
    for it in members:
        games.setdefault(_gkey(it, profile.combine_alt_titles), []).append(it)
    for group in games.values():
        names = list(dict.fromkeys(_rom_name(i) for i in group))
        vkeys = tags.group_version_keys(names, STYLE_NOINTRO)
        releases: dict[tuple, list[Item]] = {}
        for it in group:
            rel = _release_of_parts(it)
            if rel is not None:
                releases.setdefault(rel, []).append(it)
        most = max(tags.disc_number(_tags_of(i)) for i in group)
        info = {}
        for it in group:
            t = _tags_of(it)
            rel = _release_of_parts(it)
            info[it.key] = (rel is not None and _discs_missing(releases[rel], most) > 0,             # (a release with every disc first)
                            _lang_rank(tags.variant_languages(t), profile), tags.unlicensed(t),     # (a pirate copy after the licensed game)
                            tags.region_rank(t.regions, order))
        pool = group
        for idx in (0, 1, 2, 3):
            best = min(info[i.key][idx] for i in pool)
            pool = [i for i in pool if info[i.key][idx] == best]
        rank = min(tags.edition_rank(_tags_of(i), profile.prefer_editions) for i in pool)      # (a premium edition, the plain release, a store edition, a budget one)
        pool = [i for i in pool if tags.edition_rank(_tags_of(i), profile.prefer_editions) == rank]
        newest = max(vkeys[_rom_name(i)] for i in pool)
        pool = [i for i in pool if vkeys[_rom_name(i)] == newest]
        winner = min(pool, key=lambda i: (tags.extra_tag_count(_tags_of(i)), _region_pos(_tags_of(i), profile), _rom_name(i).casefold(),
                                          _rom_name(i), i.key))
        wname = _rom_name(winner)
        discs = _other_discs(winner, group, vkeys)          # (a game on several discs - GameCube, Wii: the chosen release's other discs stay)
        for it in group:
            if it.key == winner.key or _rom_name(it) == wname or it.key in discs:
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
        groups.setdefault(combined_key(dat, tags.identity_key(tags.parse_name(it.rom.name, STYLE_TOSEC)), profile.combine_alt_titles), []).append(it)
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
                    .setdefault(d[0], set()).add(tags.identity_key(t))
    if borrow is not None:        # files excluded only by the language filter are borrowable too
        for key, t in borrow.tags.items():
            if key not in seen_keys:
                d = _disk_info(t)
                pool.setdefault((t.title.casefold(), t.publisher.casefold(), d[1]), {}) \
                    .setdefault(d[0], set()).add(tags.identity_key(t))
    for gkey in sorted(groups, key=lambda k: sorted(i.rom.name for i in groups[k])[0]):
        group = groups[gkey]
        vkeys = tags.group_version_keys([i.rom.name for i in group], STYLE_TOSEC)
        # the disks of a set are of one edition (country, publisher ...: ``identity_key``), also when "Combine alternate titles" makes
        # several editions one game: the best variant is then chosen among the complete sets of all of them
        editions: dict[tuple, list[Item]] = {}
        for it in group:
            editions.setdefault(tags.identity_key(tags.parse_name(it.rom.name, STYLE_TOSEC)), []).append(it)
        complete, partial = [], []
        totals = _complete_totals(editions, vkeys) if borrow is not None and len(editions) > 1 else frozenset()
        for members in editions.values():
            c, p = _build_sets(members, vkeys, profile, borrow, totals)
            complete += c
            partial += p
        kept_sets: list[_Set]
        if not complete:
            kept_sets = []
        elif best:
            kept_sets = [_pick_best(complete, profile.prefer_whdload)]
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
                elsewhere = tuple(m for m in missing if other.get(m, set()) - {tags.identity_key(t)})
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
        sel.before_borrow.setdefault(key, dataclasses.replace(d) if d is not None else None)
        if d is None or d.action != KEEP:
            sel.decisions[key] = Decision(key=key, action=KEEP, codes=(BORROWED_CODE,), set_id=sid, reason=text)
        else:
            if BORROWED_CODE not in d.codes:
                d.codes = tuple(d.codes) + (BORROWED_CODE,)
            if d.set_id is None:
                d.set_id = sid
            d.reason = (d.reason + "; " if d.reason else "") + text


def _complete_totals(editions: Mapping[tuple, list[Item]], vkeys: Mapping[str, tuple]) -> frozenset:
    """The disk counts of the multi-disk sets the editions of a combined game have complete of their own: an edition's natural
    complete set always wins, so no disk is borrowed for such a count (Amendment 12 B)."""
    from . import m3u
    out: set[int] = set()
    for members in editions.values():
        first: dict[str, Item] = {}
        for it in sorted(members, key=lambda i: (i.rom.name, i.key)):
            if m3u.parse_disk_name(it.rom.name) is not None:
                first.setdefault(it.rom.name, it)
        if first:
            out |= {ss.total for ss in m3u.resolve_slots([m3u.DiskCand(ref=it.key, name=n) for n, it in first.items()], vkeys)
                    if ss.complete}
    return frozenset(out)


def variant_sets(items: Sequence[Item], profile: LibraryProfile, platform: "Platform") -> list[tuple[str, list[list[Item]]]]:
    """``[(title, [the disks of each complete version])]`` of the games of the best-variant DATs (Amiga Games): the versions the rule
    "one best variant per game" chooses among, after the exclusions (what "Prefer versions WHDLoad can install" looks at)."""
    sel = select(items, replace(profile, prefer_whdload=False), platform)
    best = _Scope(platform).best
    alive = [it for it in items if it.dat in best and _style(it) == STYLE_TOSEC
             and (d := sel.decisions.get(it.key)) is not None and d.action in (KEEP, SUPERSEDED)]
    groups: dict[tuple, list[Item]] = {}
    for it in sorted(alive, key=lambda i: (i.rom.name, i.key)):
        groups.setdefault((it.dat, combined_key(it.dat, tags.identity_key(tags.parse_name(it.rom.name, STYLE_TOSEC)),
                                                profile.combine_alt_titles)), []).append(it)
    borrows: dict[str, _Borrow] = {}
    if profile.borrow_other_editions:                     # (the sets the library may complete with another edition's disks: as select does)
        lang_only = [it for it in items if it.dat in best and _style(it) == STYLE_TOSEC and (d := sel.decisions.get(it.key)) is not None
                     and d.action == EXCLUDED and d.codes and all(c == LANGUAGE_CODE for c in d.codes)]
        for dat in {it.dat for it in alive}:
            borrows[dat] = _Borrow(profile, [it for it in (*alive, *lang_only) if it.dat == dat])
    out: list[tuple[str, list[list[Item]]]] = []
    for (dat, _gk), group in groups.items():
        vkeys = tags.group_version_keys([i.rom.name for i in group], STYLE_TOSEC)
        editions: dict[tuple, list[Item]] = {}
        for it in group:
            editions.setdefault(tags.identity_key(tags.parse_name(it.rom.name, STYLE_TOSEC)), []).append(it)
        borrow = borrows.get(dat)
        totals = _complete_totals(editions, vkeys) if borrow is not None and len(editions) > 1 else frozenset()
        sets: list[list[Item]] = []
        for members in editions.values():
            complete, _partial = _build_sets(members, vkeys, profile, borrow, totals)
            sets += [[s.slots[n] for n in sorted(s.slots)] for s in complete]
        if sets:
            out.append((tags.parse_name(group[0].rom.name, STYLE_TOSEC).title, sets))
    return out


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
    if src == "redump":
        return STYLE_REDUMP
    return STYLE_NOINTRO if src == "nointro" else STYLE_TOSEC


_BOTH = [STYLE_TOSEC, STYLE_NOINTRO]
_ALL = [STYLE_TOSEC, STYLE_NOINTRO, STYLE_REDUMP]
_REDUMP_DESCRIPTIONS = {
    "pre_release": "Betas ((Beta)). Redump files most of them under the Preproduction category.",
    "prototype": "Prototypes ((Proto)) and every disc Redump files under the Preproduction category.",
    "demo": "Demos, samples, Japanese trial discs ((Taikenban), (Tentou Taikenban), (Tentou-you Demo ...)) and "
            "every disc Redump files under the Demos or Coverdiscs categories.",
}
_RULE_DESCRIPTIONS = {
    "bad_dump": "Dumps marked as bad or corrupt. Allowed by default: a bad dump is kept only when the game has no clean version "
                "(some still install with WHDLoad: it reads only the files it needs).",
    "virus": "Disks that carry a virus.",
    "bad_size": "Over- or under-sized dumps.",
    "pre_release": "Betas, alphas, previews and debug builds.",
    "prototype": "Prototypes, never officially released.",
    "demo": "Demos, samples, kiosk and slideshow disks.",
    "faked": "Fake releases (a release pretending to be something else).",
    "unreleased": "Games that were never released.",
    "modified": "Disks modified from the original dump (a copy tool's mark, a saved high-score table; a common reason a Workbench disk is "
                "listed). Allowed by default: kept only when the game has no clean version.",
    "budget": "Budget re-releases (the Best, Greatest Hits, Platinum, @barai ...): set aside when another release of the game is there; "
              "the only release of a game is kept.",
    "online": "Titles that only ran online on servers that are gone: browsers, start-up and network discs, the Passport discs, "
              "online-only games (Final Fantasy XI, EverQuest Online Adventures ...).",
    "cd_conversion": "Floppy conversions of CD32 and CDTV games (TOSEC's [CD32 rip], \"... CD32\" titles): a CD32 or CDTV game is its "
                     "disc image, which PUAE runs as it is.",
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
    redump = platform_style == STYLE_REDUMP
    for rule in RULES:
        applies = [st for st in _ALL if per_style[st][rule]]
        # list tokens of the requested style (the TOSEC list is the superset)
        toks = per_style.get(platform_style, per_style[STYLE_TOSEC])[rule]
        out.append({"id": rule, "field": "exclude", "label": tags.RULE_LABELS[rule], "kind": "exclude",
                    "default": True, "tokens": list(toks),
                    "description": ((_REDUMP_DESCRIPTIONS.get(rule) if redump else None)
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
        "Keep only the newest version of a release (older ones go to _superseded).", _ALL)
    opt("prefer_editions", "prefer_editions", "Prefer special and limited editions", True,
        "Of a game's releases, a special, limited, collector's or first-print edition, a premium box or another build of the game is kept "
        "instead of the plain release (it can hold more: extra modes, characters, fixes). Off: the plain release first. Store editions and "
        "bundles always come after the plain release, budget re-releases last.", [STYLE_NOINTRO, STYLE_REDUMP])
    opt("combine_alt_titles", "combine_alt_titles", "Combine alternate titles", True,
        "A game released under another title, by another publisher or in another country (Biohazard and Resident Evil, Eternal Arcadia "
        "and Skies of Arcadia, Anstoss and On the Ball, the German release of an Amiga game) is one game: one release of it is kept, by your "
        "languages and regions, and it counts once; the language filter then sets aside the games with no version in your languages. The "
        "titles come from the ratings data (LaunchBox's alternate names) and GameTDB. Off: every title (on the Amiga every country and "
        "publisher) is a game of its own.", _ALL)
    opt("prefer_whdload", "prefer_whdload", "Prefer versions WHDLoad can install", True,
        "Of a game's versions, keep the one its WHDLoad install works best from: one known to boot, then one the install copies the "
        "game's files from, then one it makes disk images of - after your language order, before cracked / chipset / newest. The app "
        "knows this for many TOSEC versions already (installs run on them and booted in PUAE); \"Check which versions WHDLoad can "
        "install\" (WHDLoad installs, on the Files page) finds it for yours.", [STYLE_TOSEC])
    opt("build_whdload", "build_whdload", "Build WHDLoad games", True,
        "Build library also makes every game it keeps that a WHDLoad install works from a WHDLoad archive (<release>.lha) beside "
        "its disks, for PUAE to run as from a hard disk: the install from whdload.de, run here on your own disks. One that is "
        "already there and the same is left as it is; the first build can take an hour or more.", [STYLE_TOSEC])
    opt("prefer_unprotected", "prefer_unprotected", "Prefer cracks to originals that ask for the manual", True,
        "An original disk set (SPS / IPF) whose game asks for something from the manual or a code wheel - OpenRetro's protection of "
        "the release - and whose WHDLoad install does not take that check out gives way to a cracked version you have that the "
        "install boots from (or to any cracked version when the game has no WHDLoad install): the original is set aside as "
        "superseded.", [STYLE_TOSEC])
    opt("best_variant", "best_variant", "One best variant per game", True,
        "Keep ONE variant per game: preferred language, then cracked, then best platform "
        "(CD32 > AGA > CDTV > ECS > OCS: a CD32 or CDTV disc image, when you have one, over the game's floppies), then newest, "
        "then fewest modifications.", [STYLE_TOSEC])
    opt("one_per_game", "one_per_game", "One version per game", True,
        "Keep ONE version per game: preferred language, then the best region (see region priority), "
        "then the newest revision, then the fewest extra tags. Off: the latest version of every region. "
        "Multi-disc games keep ALL discs of the chosen release. Amiga: one release of a game whatever form it comes in - its CD32 disc, "
        "else the version a WHDLoad install runs from, else the best original (SPS) or floppy set; the others are superseded.",
        [STYLE_NOINTRO, STYLE_REDUMP, STYLE_TOSEC])
    opt("complete_only", "complete_only", "Complete multi-disk sets only", True,
        "Multi-disk games need every disk; incomplete ones go to _incomplete (disc systems: PlayStation ...; a raw disc of such a game is "
        "not converted to a CHD either). Off: they stay where they are, with no playlist.", [STYLE_TOSEC, STYLE_REDUMP])
    opt("borrow_editions", "borrow_other_editions", "Complete sets with disks from other editions", True,
        "A multi-disk game that lacks a disk in your edition is completed with that disk from another edition "
        "you own (matched by checksum): only the country, language, edition flags, version and year may "
        "differ. The title, publisher and disk count ('Disk N of M') must be the same, the chipset (OCS / AGA / "
        "CD32) must fit, and the disk must pass every rule above (bad dumps, viruses, pre-releases, prototypes, "
        "demos, faked, unreleased, modified, size problems are never borrowed). The disk may be in a language "
        "you did not select; it is kept as part of the set and the playlist says where it came from. "
        "Off: only disks of one edition form a set.", [STYLE_TOSEC])
    opt("keep_bios", "keep_bios", "Keep BIOS, firmware and key files", True,
        "A BIOS or firmware file in the system's folder (told by its checksum: libretro's list and the TOSEC firmware lists, also "
        "in a zip) and the key files of the emulators (told by name) stay with the ROMs instead of going to _unmatched or the "
        "archive. Off: they are files like any other that match no game.", _ALL)
    opt("keep_extras", "keep_extras", "Keep other files that sit with a game", False,
        "A disc game lives in a folder of its own, and the files next to its disc image (saves and save states, a .zip, checksum files "
        "and the like) are not part of the game. Off: they are set aside like any file that matches no game (into _unmatched or the "
        "archive folder, with their path kept, and Undo brings them back); only the disc image stays. On: they stay with the game and "
        "are renamed with it.", [STYLE_REDUMP])
    opt("convert_first", "convert_first", "Convert when building", True,
        "Build library first converts what this system can convert: raw disc images (an .iso, a .cue or .gdi with its tracks, also inside a "
        "zip or 7z) become a CHD named after the game; for the SNES and N64 the dumps are cleaned up (copier headers, byte order) so the "
        "files equal the database's; the Amiga's CD32 and CDTV discs become CHDs too. Each result is checked against the database before "
        "the original is touched. It takes a while; "
        "the preview says what will be converted. Settings for the CHDs are on the Disc images page.", _ALL)
    opt("compress_7z", "compress_7z", "Compress games as 7z", True,
        "Build library repacks each game into a .7z of its own (LZMA): a quarter to a sixth smaller than a zip, and RetroArch loads it "
        "like a zip. Each new .7z is checked against the database before the zip or loose file it replaces is deleted (nothing of the game "
        "changes, only the packing). Off: games stay as they are. For the systems whose RetroArch cores load .7z games.", [STYLE_NOINTRO])
    opt("delete_converted", "delete_converted", "Delete the originals after converting", True,
        "Off: the originals (the .iso, the zip, the cue and its tracks) go to _converted_originals, nothing is deleted. On: they are deleted "
        "once the CHD has been checked against the database.", [STYLE_REDUMP, STYLE_TOSEC])
    opt("rescue", "rescue_only_dump", "Keep the only dump of an OS version", False,
        "Workbench / Kickstart disks: keep a disk that is excluded only because of [m], [o] or [u] "
        "when it is the sole dump of its version.", [STYLE_TOSEC])
    opt("languages", "languages", "Languages", True,
        "Keep only releases playable in the selected languages (first = preferred). A release with no "
        "language and no country tag counts as English; a country implies its language; "
        "(de-en) counts as both.", _ALL, default_value=["En"])
    opt("other_language", "keep_other_language", "Keep games that exist only in other languages", True,
        "A game with no version in your languages anywhere in the database (for example a Japan-only release) is kept "
        "instead of left out. As soon as a version in one of your languages exists, only that one is kept.",
        [STYLE_NOINTRO, STYLE_REDUMP])
    opt("region_priority", "region_priority", "Region priority", True,
        "Best region first. When one version per game is kept, the first region listed here wins "
        "(unlisted regions follow in alphabetical order). Regions marked \"same as above\" are equally good: a version from any of them is a best match.", [STYLE_NOINTRO, STYLE_REDUMP],
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
    try:
        from . import ratings
    except ImportError:           # (the rating data is an extra: a rule set must still load without it)
        return False
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
            "complete_only": bool(complete_dats(platform)),
            "one_per_game": _has_one_per_game(platform),
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
            "complete_dats": list(complete_dats(platform)),
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
