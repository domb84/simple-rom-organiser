"""The "Build library" selection: which local files to keep, exclude, supersede or set aside.

Pure logic, no I/O. ``select`` takes the local files that survived duplicate removal (as
:class:`Item`) and a :class:`LibraryProfile` and decides, per file, ``keep`` / ``excluded`` /
``superseded`` / ``incomplete``; kept multi-disk sets come back as :class:`ChosenSet` (one
playlist each). The result depends only on the ROM names (never on where a file lives), so
running it on its own kept output changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence

from . import tags
from .tags import STYLE_NOINTRO, STYLE_TOSEC

if TYPE_CHECKING:
    from .datfile import Rom
    from .platforms import Platform

RULES = tags.RULES

KEEP, EXCLUDED, SUPERSEDED, INCOMPLETE = "keep", "excluded", "superseded", "incomplete"

# Reason codes besides the exclusion rules (``RULES``): ``language`` and ``flag_<x>`` for every
# ``tags.KEEP_FLAGS`` entry. ``Decision.codes`` of an excluded item lists every code that applies.
LANGUAGE_CODE = "language"
FLAG_CODES = tuple(f"flag_{f}" for f in tags.KEEP_FLAGS)
ALL_CODES = RULES + FLAG_CODES + (LANGUAGE_CODE,)

_RULE_SHORT = {
    "bad_dump": "bad dump", "virus": "virus-infected", "bad_size": "over/under dump",
    "pre_release": "pre-release", "prototype": "prototype", "demo": "demo", "faked": "faked",
    "unreleased": "unreleased", "modified": "modified",
    "flag_cr": "crack flag", "flag_h": "hack flag", "flag_t": "trainer flag",
    "flag_a": "alternate flag", "flag_f": "fix flag", "flag_tr": "translation flag",
    "language": "not in the selected languages",
}


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
        if isinstance(r, str) and r in tags.REGIONS and r not in out:
            out.append(r)
    return tuple(out)


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

    def __post_init__(self) -> None:
        # frozen dataclass: normalise whatever the caller passed (lists, sets, unknown codes)
        object.__setattr__(self, "exclude", frozenset(r for r in self.exclude if r in RULES))
        object.__setattr__(self, "languages", _norm_languages(self.languages))
        object.__setattr__(self, "keep_flags", frozenset(f for f in self.keep_flags if f in tags.KEEP_FLAGS))
        object.__setattr__(self, "region_priority", _norm_regions(self.region_priority))

    def to_dict(self) -> dict:
        return {"exclude": sorted(self.exclude), "latest_only": self.latest_only,
                "best_variant": self.best_variant, "complete_only": self.complete_only,
                "languages": list(self.languages), "keep_flags": sorted(self.keep_flags),
                "rescue_only_dump": self.rescue_only_dump,
                "region_priority": list(self.region_priority), "one_per_game": self.one_per_game}

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
        return cls(exclude=exclude, latest_only=flag("latest_only", base.latest_only),
                   best_variant=flag("best_variant", base.best_variant),
                   complete_only=flag("complete_only", base.complete_only),
                   languages=langs, keep_flags=keep,
                   rescue_only_dump=flag("rescue_only_dump", base.rescue_only_dump),
                   region_priority=regions, one_per_game=flag("one_per_game", base.one_per_game))

    @classmethod
    def latest_only_profile(cls) -> "LibraryProfile":
        """The legacy ``latest_only`` flag: no exclusions, no filters, no best variant / completeness."""
        return cls(exclude=frozenset(), latest_only=True, best_variant=False, complete_only=False,
                   languages=(), one_per_game=False)


def _dats(platform: Any, attr: str) -> tuple[str, ...]:
    return tuple(getattr(platform, attr, ()) or ())


def default_profile(platform: "Platform") -> LibraryProfile:
    """All rules on, minus the options that do not apply to the platform."""
    return LibraryProfile(
        exclude=frozenset(RULES),
        latest_only=bool(_dats(platform, "latest_dats")),
        best_variant=bool(_dats(platform, "best_variant_dats")),
        complete_only=bool(_dats(platform, "m3u_dats")),
        languages=("En",) if _dats(platform, "language_dats") else (),
        one_per_game=bool(_dats(platform, "region_dats")))


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


@dataclass
class IncompleteSet:
    dat: str
    name: str
    total: int
    present: dict[int, int]       # disk number -> Item.key
    missing: tuple[int, ...]


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


@dataclass
class Selection:
    decisions: dict[int, Decision] = field(default_factory=dict)
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
    return item.style if item.style in (STYLE_TOSEC, STYLE_NOINTRO) else STYLE_TOSEC


def _tags_of(item: Item) -> tags.Tags:
    return tags.parse_name(_rom_name(item), _style(item))


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
                flags: bool = True) -> list[tuple[str, str]]:
    """Every ``(code, exact text)`` that makes ``rom_name`` ineligible under ``profile``.

    Codes: the exclusion rules (``RULES``), ``flag_<x>`` (a flag type missing from
    ``profile.keep_flags``; text = the exact ``[cr FLT]``) and ``language`` (no selected language;
    text = the languages it has, e.g. ``(De)``). ``languages`` / ``flags`` switch those two filters off
    (they only apply to some DATs)."""
    t = tags.parse_name(rom_name, style)
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
    t = tags.parse_name(_rom_name(item), _style(item))
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

    @property
    def refs(self) -> tuple:
        return tuple(sorted((i, it.key) for i, it in self.slots.items()))


def _disk_info(t: tags.Tags) -> Optional[tuple[int, int]]:
    d = tags._disk(t)
    if d is None or d[2] < 2:
        return None
    return d[1], d[2]


def _build_sets(group: list[Item], vkeys: Mapping[str, tuple],
                profile: Optional[LibraryProfile] = None) -> tuple[list[_Set], list[_Set]]:
    """``(complete sets, incomplete sets)`` of one identity group."""
    from . import m3u

    parsed = {it.key: tags.parse_name(it.rom.name, STYLE_TOSEC) for it in group}
    by_key = {it.key: it for it in group}
    complete: list[_Set] = []
    partial: list[_Set] = []

    def make(anchor: Item, slots: dict[int, Item], total: int, labels: dict[int, str],
             flags: tuple[str, ...], name: str, missing: tuple[int, ...]) -> _Set:
        ts = [parsed[it.key] for it in slots.values()]
        at = parsed[anchor.key]
        langs = frozenset().union(*(tags.variant_languages(t) for t in ts))
        return _Set(
            total=total, slots=slots, labels=labels, flags=flags, name=name, anchor=anchor,
            vkey=vkeys.get(anchor.rom.name, (0,)), date=tags.date_key(at.date),
            cracked=any(tags.is_cracked(t) for t in ts),
            mods=sum(tags.modification_count(t) for t in ts),
            complete=not missing, missing=missing,
            lang_rank=_lang_rank(langs, profile) if profile is not None else 0,
            platform_rank=tags.platform_rank(at), part=tags.partition_key(at))

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
        for ss in m3u.resolve_slots(cands, vkeys):
            slots = {i: by_key[c.ref] for i, c in ss.slots.items()}
            anchor = by_key[ss.anchor.ref] if ss.anchor is not None else slots[min(slots)]
            flags = tuple(f for f in ss.flags if not m3u.is_neutral_flag(f))
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

def select(items: Sequence[Item], profile: LibraryProfile, platform: "Platform") -> Selection:
    """Decide keep / excluded / superseded / incomplete per item (see the module docstring)."""
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
    for it in items:
        info = eligibility(_rom_name(it), _style(it), profile, languages=it.dat in scope.language,
                           flags=scope.flags(it.dat))
        if info:
            excluded.append((it, info))
        else:
            alive.append(it)
    # keep-every-version DATs (opt-in): the only dump of a version is not excluded for [m] / [u]
    rescued: dict[int, str] = {}
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
        if dat in latest_dats | best_dats | m3u_dats and all(_style(i) == STYLE_TOSEC for i in dat_items):
            _select_tosec(dat, dat_items, profile, dat in best_dats and profile.best_variant,
                          dat in latest_dats and profile.latest_only, dat in m3u_dats and profile.complete_only,
                          sel, decide, next_id, keep_all=dat not in latest_dats | best_dats)
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
                      if any(sel.decisions[k].action == INCOMPLETE for k in x.present.values())]
    sel.vanished = _vanished(items, sel)
    return sel


def _vanished(items: Sequence[Item], sel: Selection) -> list[Vanished]:
    """Titles of which no local variant is kept, with the reason (language first, then flags, rules)."""
    groups: dict[tuple, list[Item]] = {}
    for it in items:
        groups.setdefault((it.dat, tags.title_key(_tags_of(it))), []).append(it)
    out: list[Vanished] = []
    priority = {c: i for i, c in enumerate((LANGUAGE_CODE,) + FLAG_CODES + RULES + (INCOMPLETE,))}
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
        out.append(Vanished(dat=dat, title=t.title, name=members[best].rom.name, reason=codes[0],
                            codes=tuple(codes), languages=langs, variants=len(members)))
    out.sort(key=lambda v: (v.dat, v.title.casefold(), v.name))
    return out


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
                  keep_all: bool = False) -> None:
    """``keep_all``: a keep-every-version DAT (Kickstart-Disks, Workbench, Firmware)."""
    groups: dict[tuple, list[Item]] = {}
    for it in dat_items:
        groups.setdefault(tags.identity_key(tags.parse_name(it.rom.name, STYLE_TOSEC)), []).append(it)
    # disks of one title + publisher + disk total, whatever their country / language / edition
    # (only used to tell the user that a missing disk exists under another identity)
    pool: dict[tuple, dict[int, set[tuple]]] = {}
    for gkey, members in groups.items():
        for it in members:
            t = tags.parse_name(it.rom.name, STYLE_TOSEC)
            d = _disk_info(t)
            if d is not None:
                pool.setdefault((t.title.casefold(), t.publisher.casefold(), d[1]), {}) \
                    .setdefault(d[0], set()).add(gkey)
    for gkey in sorted(groups, key=lambda k: sorted(i.rom.name for i in groups[k])[0]):
        group = groups[gkey]
        vkeys = tags.group_version_keys([i.rom.name for i in group], STYLE_TOSEC)
        complete, partial = _build_sets(group, vkeys, profile)
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
                    flags=s.flags, cracked=s.cracked))
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
                if elsewhere:
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
    return STYLE_NOINTRO if src == "nointro" else STYLE_TOSEC


_BOTH = [STYLE_TOSEC, STYLE_NOINTRO]
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
    per_style = {st: tags.rule_tokens(st) for st in _BOTH}
    for rule in RULES:
        applies = [st for st in _BOTH if per_style[st][rule]]
        # list tokens of the requested style (the TOSEC list is the superset)
        toks = per_style.get(platform_style, per_style[STYLE_TOSEC])[rule]
        out.append({"id": rule, "field": "exclude", "label": tags.RULE_LABELS[rule], "kind": "exclude",
                    "default": True, "tokens": list(toks), "description": _RULE_DESCRIPTIONS[rule],
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
        "Keep only the newest version of a release (older ones go to _superseded).", _BOTH)
    opt("best_variant", "best_variant", "One best variant per game", True,
        "Keep ONE variant per game: preferred language, then cracked, then best platform "
        "(CD32 > AGA > OCS), then newest, then fewest modifications.", [STYLE_TOSEC])
    opt("one_per_game", "one_per_game", "One version per game", True,
        "Keep ONE version per game: preferred language, then the best region (see region priority), "
        "then the newest revision, then the fewest extra tags. Off: the latest version of every region.",
        [STYLE_NOINTRO])
    opt("complete_only", "complete_only", "Complete multi-disk sets only", True,
        "Multi-disk games need every disk; incomplete ones go to _incomplete.", [STYLE_TOSEC])
    opt("rescue", "rescue_only_dump", "Keep the only dump of an OS version", False,
        "Workbench / Kickstart disks: keep a disk that is excluded only because of [m], [o] or [u] "
        "when it is the sole dump of its version.", [STYLE_TOSEC])
    opt("languages", "languages", "Languages", True,
        "Keep only releases playable in the selected languages (first = preferred). A release with no "
        "language and no country tag counts as English; a country implies its language; "
        "(de-en) counts as both.", _BOTH, default_value=["En"])
    opt("region_priority", "region_priority", "Region priority", True,
        "Best region first. When one version per game is kept, the first region listed here wins "
        "(unlisted regions follow in alphabetical order).", [STYLE_NOINTRO],
        default_value=list(tags.DEFAULT_REGION_PRIORITY))
    return [e for e in out if platform_style in e["applies_to"]]


def available_languages(source: Any, platform: Any = None) -> list[dict[str, Any]]:
    """Languages present in ``source`` for the language checkboxes.

    ``source``: a ``DatFile``, an iterable of DatFiles, of :class:`Item`, or of ``Rom``. Returns
    ``[{"code": "En", "name": "English", "count": n, "games": m}]``, English first, then by ``count``
    (releases = No-Intro sets / TOSEC roms, bad dumps and other excluded rules NOT filtered out)
    descending, then code. ``games`` = distinct titles (``tags.title_key``). Counting follows
    ``tags.variant_languages`` (the rules the filter itself uses)."""
    roms: list[tuple[str, str]] = []     # (name, style)
    seen_units: set[tuple[str, str]] = set()

    def add_rom(rom: Any, dat_name: str = "") -> None:
        set_name = getattr(rom, "set_name", "")
        nm = set_name or rom.name
        dat = dat_name or getattr(rom, "dat", "")
        if (dat, nm) in seen_units:
            return
        seen_units.add((dat, nm))
        roms.append((nm, STYLE_NOINTRO if set_name else STYLE_TOSEC))

    def walk(obj: Any) -> None:
        if isinstance(obj, Item):
            nm = _rom_name(obj)
            if (obj.dat, nm) not in seen_units:
                seen_units.add((obj.dat, nm))
                roms.append((nm, _style(obj)))
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
    for name, style in roms:
        t = tags.parse_name(name, style)
        for code in tags.variant_languages(t):
            count[code] = count.get(code, 0) + 1
            games.setdefault(code, set()).add(tags.title_key(t))
    rows = [{"code": c, "name": tags.LANGUAGES.get(c, c), "count": n, "games": len(games[c])}
            for c, n in count.items()]
    rows.sort(key=lambda r: (r["code"] != "En", -r["count"], r["code"]))
    return rows


def profile_info(platform: Any, profile: Optional[LibraryProfile] = None) -> dict[str, Any]:
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
            "keep_flags": bool(_dats(platform, "best_variant_dats")),
            "rescue": bool(_dats(platform, "m3u_dats")) and style == STYLE_TOSEC,
        },
        "scopes": {
            "latest_dats": list(_dats(platform, "latest_dats")),
            "best_variant_dats": list(_dats(platform, "best_variant_dats")),
            "complete_dats": list(_dats(platform, "m3u_dats")),
            "language_dats": list(_dats(platform, "language_dats")),
            "region_dats": list(_dats(platform, "region_dats")),
            "exclude_dats": list(getattr(platform, "dats", ()) or ()),
        },
        "regions": tags.region_order(prof.region_priority),
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
        if reason and v.reason != reason:
            continue
        if v.reason == LANGUAGE_CODE:
            text = "no version in selected languages" + (f" (has {', '.join(v.languages)})" if v.languages else "")
        elif v.reason == INCOMPLETE:
            text = "only incomplete sets"
        else:
            text = "every version excluded: " + _RULE_SHORT.get(v.reason, v.reason)
        items.append({"dat": v.dat, "title": v.title, "name": v.name, "reason": v.reason,
                      "codes": list(v.codes), "languages": list(v.languages), "variants": v.variants,
                      "text": text})
        if limit is not None and len(items) >= limit:
            break
    out["items"] = items
    return out
