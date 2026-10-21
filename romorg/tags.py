"""Parse the tags in No-Intro and TOSEC names (regions, languages, version, flags...).

Reusable by the scanner/organiser (latest-version-only grouping) and the UI
(region/language chips and filters). Pure functions, no I/O.

No-Intro:  ``Title (Region) (Languages) (Rev 1) (Beta) (Unl) [b]``
TOSEC:     ``Title v1.2 (demo) (1990)(Publisher)(DE)(de-en)(Disk 1 of 2)[cr X][a]``
"""

from __future__ import annotations

import dataclasses
import functools
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

STYLE_NOINTRO = "nointro"
STYLE_TOSEC = "tosec"
STYLE_REDUMP = "redump"        # Redump (Dreamcast, PlayStation, PlayStation 2): No-Intro style names + <category> + (Disc N)
# Header name of the Redump DATs that use that style (== redump.DAT_NAME): only the DAT name selects it, so
# nothing of it can leak into the No-Intro consoles.
REDUMP_DAT_NAMES = frozenset({"Sega - Dreamcast", "Sony - PlayStation", "Sony - PlayStation 2"})

# region -> "PAL" | "NTSC" | "" (unknown / both)
REGIONS: dict[str, str] = {}
for _r in ("Europe", "Australia", "New Zealand", "Germany", "France", "Spain", "Italy", "Netherlands",
           "Sweden", "Denmark", "Norway", "Finland", "Scandinavia", "United Kingdom", "Ireland",
           "Portugal", "Austria", "Switzerland", "Belgium", "Greece", "Poland", "Russia", "Croatia",
           "Czech", "Hungary", "Turkey", "South Africa", "China", "Hong Kong", "India", "Argentina",
           "United Arab Emirates", "Slovakia", "Slovenia", "Yugoslavia", "Serbia", "Romania", "Bulgaria", "Lithuania", "Latvia",
           "Estonia", "Ukraine", "Israel", "Iceland", "Luxembourg", "Singapore", "Thailand", "Malaysia", "Indonesia", "Egypt"):
    REGIONS[_r] = "PAL"
for _r in ("USA", "Canada", "Japan", "Korea", "Taiwan", "Brazil", "Mexico", "Peru", "Latin America", "Chile", "Colombia",
           "Philippines", "Vietnam"):
    REGIONS[_r] = "NTSC"
for _r in ("World", "Asia", "Unknown"):
    REGIONS[_r] = ""
del _r

# One region, several spellings: No-Intro writes (UK), TOSEC's GB is the United Kingdom. Everything is stored and shown under
# the canonical name, so there is ONE entry in the region priority and one value to filter by.
REGION_ALIASES = {"UK": "United Kingdom", "Great Britain": "United Kingdom"}


def canon_region(name: str) -> str:
    return REGION_ALIASES.get(name, name)


def is_region(name: str) -> bool:
    return name in REGIONS or name in REGION_ALIASES


TOSEC_COUNTRIES: dict[str, str] = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria", "AU": "Australia",
    "BE": "Belgium", "BR": "Brazil", "CA": "Canada", "CH": "Switzerland", "CN": "China",
    "CZ": "Czech", "DE": "Germany", "DK": "Denmark", "ES": "Spain", "EU": "Europe",
    "FI": "Finland", "FR": "France", "GB": "United Kingdom", "GR": "Greece", "HK": "Hong Kong",
    "HR": "Croatia", "HU": "Hungary", "IE": "Ireland", "IN": "India", "IT": "Italy",
    "JP": "Japan", "KR": "Korea", "MX": "Mexico", "NL": "Netherlands", "NO": "Norway",
    "NZ": "New Zealand", "PL": "Poland", "PT": "Portugal", "RU": "Russia", "SE": "Sweden",
    "TR": "Turkey", "TW": "Taiwan", "US": "USA", "ZA": "South Africa",
    "BG": "Bulgaria", "CL": "Chile", "CS": "Serbia", "EE": "Estonia", "EG": "Egypt", "ID": "Indonesia", "IL": "Israel",
    "IS": "Iceland", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "MY": "Malaysia", "PE": "Peru", "PH": "Philippines",
    "RO": "Romania", "SG": "Singapore", "SI": "Slovenia", "SK": "Slovakia", "TH": "Thailand", "VN": "Vietnam", "YU": "Yugoslavia",
}

LANGUAGES: dict[str, str] = {
    "En": "English", "Ja": "Japanese", "Fr": "French", "De": "German", "Es": "Spanish",
    "It": "Italian", "Nl": "Dutch", "Pt": "Portuguese", "Sv": "Swedish", "No": "Norwegian",
    "Da": "Danish", "Fi": "Finnish", "Zh": "Chinese", "Ko": "Korean", "Pl": "Polish",
    "Ru": "Russian", "El": "Greek", "Ca": "Catalan", "Hu": "Hungarian", "Cs": "Czech",
    "Sk": "Slovak", "Tr": "Turkish", "Ar": "Arabic", "He": "Hebrew", "Hr": "Croatian",
    "Is": "Icelandic", "Et": "Estonian", "Lv": "Latvian", "Lt": "Lithuanian", "Sr": "Serbian",
    "Sl": "Slovenian", "Ro": "Romanian", "Bg": "Bulgarian", "Uk": "Ukrainian", "Id": "Indonesian",
    "Th": "Thai", "Vi": "Vietnamese", "Ms": "Malay", "Hi": "Hindi", "Ga": "Irish", "Eu": "Basque",
    "Gd": "Scottish Gaelic", "Cy": "Welsh",
}

REGION_LANGUAGE: dict[str, str] = {}
for _codes, _lang in (
        (("USA", "World", "Europe", "Australia", "New Zealand", "United Kingdom", "Canada",
          "Ireland", "South Africa"), "En"),
        (("Japan",), "Ja"), (("Germany", "Austria"), "De"), (("France",), "Fr"),
        (("Spain", "Mexico", "Argentina", "Latin America", "Peru"), "Es"), (("Italy",), "It"),
        (("Netherlands",), "Nl"), (("Sweden",), "Sv"), (("Norway",), "No"), (("Denmark",), "Da"),
        (("Finland",), "Fi"), (("Brazil", "Portugal"), "Pt"), (("Russia",), "Ru"),
        (("Korea",), "Ko"), (("China", "Taiwan", "Hong Kong"), "Zh"), (("Poland",), "Pl"),
        (("Greece",), "El"), (("Czech",), "Cs"), (("Slovakia",), "Sk"), (("Slovenia",), "Sl"), (("Hungary",), "Hu"),
        (("Croatia",), "Hr"), (("Serbia", "Yugoslavia"), "Sr"), (("Romania",), "Ro"), (("Bulgaria",), "Bg"), (("Lithuania",), "Lt"),
        (("Latvia",), "Lv"), (("Estonia",), "Et"), (("Ukraine",), "Uk"), (("Israel",), "He"), (("Iceland",), "Is"),
        (("Turkey",), "Tr"), (("Thailand",), "Th"), (("Vietnam",), "Vi"), (("Indonesia",), "Id"), (("Chile", "Colombia"), "Es")):
    for _c in _codes:
        REGION_LANGUAGE[_c] = _lang
del _codes, _lang, _c

_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_GROUP_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
# Contract forms plus the variants seen in real DATs: "V1.1", "Rev 1.2", "v2.0-beta3".
_VERSION_PART_RE = re.compile(
    r"^(?:Rev [0-9A-Z]+(?:\.\d+)*|REV-[0-9A-Z]+|[vV]\d+(?:\.\d+)*[a-z]?(?:-(?:alpha|beta|rc)\d*)?)$")
_STATUS_RE = re.compile(
    r"^(?:Beta|(?:Possible )?Proto|Demo|Sample|Kiosk|Promo|Debug(?: Version)?|Tech Demo|Auto Demo)"
    r"(?:\s+\d+)?$")
# "Pt-BR", "Zh-Hans": language code plus a script/country variant
_LANG_VARIANT_RE = re.compile(r"^([A-Z][a-z])-([A-Za-z]{2,4})$")
_TAIL_RE = re.compile(r"(?:\s*(?:\([^()]*\)|\[[^\[\]]*\]))+\s*")
_PRERELEASE = {"alpha": 1, "beta": 2, "rc": 3}
# " v1.2", " v1.3c", " v1.3 rev1", " Rev 2"; an upper-case locale suffix ("v1.0E", "v1.44GE")
# is split off into the flags (group 2) so it stays part of the supersede key.
_TOSEC_VERSION_RE = re.compile(
    r"\s(v\d+(?:\.\d+)*[a-z]?|[Rr]ev ?\d+(?:\.\d+)*|r\d+(?:\.\d+)*)([A-Z]{1,3})?((?:\s?(?:[Rr]ev|r) ?\d+(?:\.\d+)*)?)"
    r"([A-Z]{1,3})?$")
# Versions the pattern above does not know, tried after it: "v855e.01" (MicroProse), "v2.0f i2.202" (Sierra's interpreter),
# "vP1.00-D1.00" / "v0.90-1.00" (program and data), "v1.0D3", "v3.1 29701" (a build), "v3.0+", "v1.2-G" (the locale after a dash);
# and a version before a subtitle ("Whale's Voyage v1.2 - Super Enhanced", "Falcon v1.1 - Mission Disk Volume 1 - ...") or before
# "CD32" ("Gunship 2000 v3.32 CD32"): the title is the rest, so the release is a version of its title like the others.
_TOSEC_VER = r"v[A-Z]?\d+[a-z]?(?:\.\d+[a-z]?)*(?:-[A-Z]?\d+(?:\.\d+)*)?(?:[A-Z]\d+)?\+?(?:\s[ir]\d+(?:\.\d+)*)?(?:\s\d{4,})?"
_TOSEC_VERSION_END_RE = re.compile(rf"\s({_TOSEC_VER})(?:-([A-Z]{{1,2}}))?$")
_TOSEC_VERSION_MID_RE = re.compile(rf"\s({_TOSEC_VER})(?:-([A-Z]{{1,2}}))?\s(?:-\s(\S.*)|(CD32|CDTV))$")
_TOSEC_DATE_RE = re.compile(r"^[\dx?]{4}(?:-[\dx?]{2}(?:-[\dx?]{2})?)?$", re.IGNORECASE)
_TOSEC_STATUS_RE = re.compile(
    r"^(?:alpha|beta|preview|pre-release|proto|demo(?:-[a-z]+)?)(?: \d+)?$", re.IGNORECASE)
_TOSEC_COUNTRY_RE = re.compile(r"^[A-Z]{2}(?:-[A-Z]{2})*$")
_TOSEC_LANG_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})*$")
_BAD_RE = re.compile(r"^b(?:\d+)?(?:\s.*)?$")
_DISK_RE = re.compile(r"^Dis[kc] (\d+|[A-Z]) of (\d+|[A-Z])$")
# MPAL = Brazilian PAL-M (N64): a 60 Hz system, so it counts as NTSC for video filters.
_VIDEO_TAGS = {"PAL": ("PAL",), "NTSC": ("NTSC",), "PAL-NTSC": ("NTSC", "PAL"),
               "NTSC-PAL": ("NTSC", "PAL"), "MPAL": ("NTSC",)}
# No-Intro puts some dump flags in front of the title: "[BIOS] Nintendo Game Boy Advance Boot ROM (World)"
_LEADING_FLAGS_RE = re.compile(r"^(?:\[([^\[\]]*)\]\s*)+")
# Per-disk labels directly following "(Disk N of M)" in TOSEC names; not part of a set's identity.
_DISK_LABELS = {"program", "data", "save", "boot", "game", "intro", "data disk", "save disk",
                "program disk", "game disk", "boot disk", "a", "b", "c", "d"}


@dataclass(frozen=True)
class Tags:
    title: str                    # name w/o extension, tags and version token
    regions: tuple[str, ...]      # region names in name order (TOSEC codes mapped)
    languages: tuple[str, ...]    # 2-letter No-Intro style codes ("En", "Fr")
    languages_implied: bool       # True when no language tag (languages derived from the regions)
    version: str                  # "Rev 1" | "Rev A" | "REV-B" | "v1.1" | ""
    version_key: tuple            # see version_key()
    status: str                   # "" (release) or "Beta 2", "Proto", "Demo", "Sample", ...
    flags: tuple[str, ...]        # every other "(...)" tag verbatim, in order
    dump_flags: tuple[str, ...]   # every "[...]" tag verbatim
    bad: bool                     # [b] / [b...] present
    bios: bool                    # [BIOS] present
    video: tuple[str, ...]        # sorted subset of ("NTSC", "PAL")
    style: str                    # "nointro" | "tosec"
    date: str = ""                # TOSEC date paren ("1990", "1990-05-01"); not part of supersede_key
    publisher: str = ""           # TOSEC paren right after the date (also stays in ``flags``)
    category: str = ""            # Redump: the DAT <category> ("Games", "Demos", "Coverdiscs", ...)


# --------------------------------------------------------------------------- versions

def _letters(s: str) -> int:
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n


def version_key(version: str) -> tuple:
    """Sortable key: ``""`` < Rev 1 < Rev 2, Rev A < Rev B, v1.1 < v1.2 < v1.10."""
    return _version_key(version)


def _version_key(version: str, widths: Optional[dict[int, int]] = None) -> tuple:
    """:func:`version_key`; ``widths`` (dotted position -> digits) compares those ``vX.Y``
    parts as decimal fractions instead (``v1.02`` < ``v1.1`` with ``{1: 2}``)."""
    v = (version or "").strip()
    if not v:
        return (0,)
    bare = re.match(r"^(?:rev ?|r)(\d+(?:\.\d+)*)$", v, re.IGNORECASE)   # "r219", "rev1", "Rev 2.1"
    if bare:
        return (1, *(int(x) for x in bare.group(1).split(".")))
    m = re.match(r"^(?:Rev |REV-)([0-9A-Z]+)$", v, re.IGNORECASE)
    if m is None:
        m2 = re.match(r"^Rev (\d+(?:\.\d+)+)$", v, re.IGNORECASE)
        if m2:
            return (1, *(int(x) for x in m2.group(1).split(".")))
    if m:
        part = m.group(1).upper()
        if part.isdigit():
            return (1, int(part))
        if part.isalpha():
            return (1, _letters(part))
        d = re.match(r"^(\d+)([A-Z]+)$", part)
        if d:
            return (1, int(d.group(1)), _letters(d.group(2)))
        return (1, 0)
    m = re.match(r"^v(\d+(?:\.\d+)*)([a-z]?)(?:-(alpha|beta|rc)(\d*))?(?:\s?(?:rev|r) ?(\d+(?:\.\d+)*))?$",
                 v, re.IGNORECASE)
    if m:
        nums = [int(x.ljust(widths[i], "0")) if widths and i in widths else int(x)
                for i, x in enumerate(m.group(1).split("."))]
        letter = [ord(m.group(2).lower()) - 96] if m.group(2) else []
        if m.group(5):  # TOSEC "v1.3 rev2": a revision of v1.3
            return (1, *nums, *letter, 0, *(int(x) for x in m.group(5).split(".")))
        if m.group(3):
            # pre-release of X.Y sorts after everything below X.Y and before X.Y itself:
            # lower the last component and append a large marker (v2.0-rc2 -> 2, -1, 1e6+3, 2)
            parts = nums + letter
            parts[-1] -= 1
            stage = _PRERELEASE[m.group(3).lower()]
            return (1, *parts, 1_000_000 + stage, int(m.group(4) or 0))
        return (1, *nums, *letter)
    # the other forms ("v855e.01", "v2.0f i2.202", "v1.1+", "vP1.00-D1.00"): a leading "vX.Y<letter>" read as above (its widths and
    # letter), then the rest's numbers in order
    lead = re.match(r"^v(\d+(?:\.\d+)*)([a-z]?)(?![0-9])", v, re.IGNORECASE)
    head: list[int] = []
    if lead:
        head = [int(x.ljust(widths[i], "0")) if widths and i in widths else int(x) for i, x in enumerate(lead.group(1).split("."))]
        head += [ord(lead.group(2).lower()) - 96] if lead.group(2) else []
        v = v[lead.end():]
    nums = head + [int(x) for x in re.findall(r"\d+", v)]
    return (1, *nums) if nums else (1, 0)


def _max_version(parts: list[str]) -> str:
    return max(parts, key=lambda p: (version_key(p), p))


# --------------------------------------------------------------------------- parsing

def _split_groups(rest: str) -> list[tuple[str, str]]:
    """``(kind, text)`` for every ``(..)`` / ``[..]`` group; kind is ``"("`` or ``"["``."""
    out: list[tuple[str, str]] = []
    for m in _GROUP_RE.finditer(rest):
        if m.group(1) is not None:
            out.append(("(", m.group(1)))
        else:
            out.append(("[", m.group(2)))
    return out


def _video_from(regions: tuple[str, ...], explicit: tuple[str, ...]) -> tuple[str, ...]:
    if explicit:
        return tuple(sorted(set(explicit)))
    out: set[str] = set()
    for r in regions:
        if r == "World":
            out.update(("NTSC", "PAL"))
        else:
            v = REGIONS.get(r, "")
            if v:
                out.add(v)
    return tuple(sorted(out))


def _implied_languages(regions: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for r in regions:
        lang = REGION_LANGUAGE.get(r)
        if lang and lang not in out:
            out.append(lang)
    return tuple(out)


def is_bad(flag: str) -> bool:
    """True for a bad-dump flag text (without brackets): ``b``, ``b1``, ``b corrupt file``.

    ``[bootable]``, ``[bf]`` and ``[m baddump]`` do not match."""
    return bool(_BAD_RE.match(flag))


_is_bad = is_bad


_LANG_ALIASES = {"Dk": "Da", "Se": "Sv"}        # (GameTDB's Wii U names spell Danish and Swedish so)


def _parse_language_group(text: str) -> tuple[Optional[tuple[str, ...]], list[str]]:
    """``(codes, variants)``: "En,Pt-BR" -> (("En", "Pt"), ["Pt-BR"]); not a language group -> (None, [])."""
    out: list[str] = []
    variants: list[str] = []
    for raw in re.split(r"[,+]", text):
        p = _LANG_ALIASES.get(raw.strip(), raw.strip())
        if p not in LANGUAGES:
            m = _LANG_VARIANT_RE.match(p)
            if not m or m.group(1) not in LANGUAGES:
                return None, []
            variants.append(p)
            p = m.group(1)
        if p not in out:
            out.append(p)
    return (tuple(out), variants) if out else (None, [])


def _title_cut(name: str) -> int:
    """Index where the trailing run of ``(..)``/``[..]`` groups starts (``len(name)`` if none).

    Brackets inside the title ("Lelele no Le (^^;", "X (Gedou Ban) - Y") stay in the title.
    """
    i = 0
    while True:
        i = min((j for j in (name.find("(", i), name.find("[", i)) if j >= 0), default=-1)
        if i < 0:
            return len(name)
        if _TAIL_RE.fullmatch(name, i):
            return i
        i += 1


def _parse_nointro(name: str) -> Tags:
    dump_flags: list[str] = []
    lead = _LEADING_FLAGS_RE.match(name)
    if lead and lead.end() < len(name):  # "[BIOS] Title (World)": prefix flags
        dump_flags.extend(t for _k, t in _split_groups(lead.group(0)))
        name = name[lead.end():]
    cut = _title_cut(name)
    title = name[:cut].strip()
    regions: tuple[str, ...] = ()
    languages: Optional[tuple[str, ...]] = None
    version = status = ""
    flags: list[str] = []
    explicit_video: tuple[str, ...] = ()
    groups = _split_groups(name[cut:])
    # No-Intro puts the region first; plain "(..)" groups before it are part of the title
    # ("Sansu 5 Nen (Jou) (Japan)").
    first_region = next((i for i, (k, t) in enumerate(groups)
                         if k == "(" and all(is_region(p) for p in t.split(", "))), 0)
    if first_region and all(k == "(" for k, _t in groups[:first_region]):
        title = " ".join([title] + [f"({t})" for _k, t in groups[:first_region]])
        groups = groups[first_region:]
    for kind, text in groups:
        if kind == "[":
            dump_flags.append(text)
            continue
        if not regions:
            parts = text.split(", ")
            if all(is_region(p) for p in parts):
                regions = tuple(dict.fromkeys(canon_region(p) for p in parts))
                continue
        if languages is None:
            langs, variants = _parse_language_group(text)
            if langs is not None:
                languages = langs
                flags.extend(variants)
                continue
        if not version:
            parts = text.split(", ")
            if all(_VERSION_PART_RE.match(p) for p in parts):
                version = _max_version(parts)
                continue
        if not status and _STATUS_RE.match(text):
            status = text
            continue
        if text in _VIDEO_TAGS and not explicit_video:
            explicit_video = _VIDEO_TAGS[text]
        flags.append(text)
    implied = languages is None
    langs_out = _implied_languages(regions) if implied else languages  # type: ignore[assignment]
    return Tags(
        title=title, regions=regions, languages=langs_out, languages_implied=implied,
        version=version, version_key=version_key(version), status=status, flags=tuple(flags),
        dump_flags=tuple(dump_flags), bad=any(_is_bad(f) for f in dump_flags),
        bios=any(f.upper() == "BIOS" for f in dump_flags),
        video=_video_from(regions, explicit_video), style=STYLE_NOINTRO)


def _parse_tosec(name: str) -> Tags:
    cut = _title_cut(name)
    title = name[:cut].strip()
    version = ""
    suffix = ""
    m = _TOSEC_VERSION_RE.search(title)
    if m:
        version = m.group(1) + m.group(3)
        suffix = (m.group(2) or "") + (m.group(4) or "")
        title = title[:m.start()].rstrip()
    elif (e := _TOSEC_VERSION_END_RE.search(title)) is not None:
        version, suffix, title = e.group(1), e.group(2) or "", title[:e.start()].rstrip()
    elif (mid := _TOSEC_VERSION_MID_RE.search(title)) is not None:
        version, suffix = mid.group(1), mid.group(2) or ""
        title = title[:mid.start()].rstrip() + (f" - {mid.group(3)}" if mid.group(3) else f" {mid.group(4)}")
    regions: tuple[str, ...] = ()
    languages: Optional[tuple[str, ...]] = None
    status = date = publisher = ""
    seen_date = False
    publisher_next = False
    flags: list[str] = []
    dump_flags: list[str] = []
    explicit_video: tuple[str, ...] = ()
    if suffix:
        flags.append(suffix)
    for kind, text in _split_groups(name[cut:]):
        if kind == "[":
            dump_flags.append(text)
            continue
        if publisher_next:  # the paren right after the date is the publisher
            publisher_next = False
            publisher = text
            flags.append(text)
            continue
        if not seen_date and _TOSEC_DATE_RE.match(text):
            seen_date, date, publisher_next = True, text, True
            continue
        if not status and _TOSEC_STATUS_RE.match(text):
            status = text
            continue
        if not regions and _TOSEC_COUNTRY_RE.match(text):
            codes = text.split("-")
            if all(c in TOSEC_COUNTRIES for c in codes):
                regions = tuple(TOSEC_COUNTRIES[c] for c in codes)
                continue
        if languages is None and _TOSEC_LANG_RE.match(text):
            codes = [c.capitalize() for c in text.split("-")]
            if all(c in LANGUAGES for c in codes):
                languages = tuple(dict.fromkeys(codes))
                continue
        if text in _VIDEO_TAGS and not explicit_video:
            explicit_video = _VIDEO_TAGS[text]
        flags.append(text)
    implied = languages is None
    langs_out = _implied_languages(regions) if implied else languages  # type: ignore[assignment]
    return Tags(
        title=title, regions=regions, languages=langs_out, languages_implied=implied,
        version=version, version_key=version_key(version), status=status, flags=tuple(flags),
        dump_flags=tuple(dump_flags), bad=any(_is_bad(f) for f in dump_flags),
        bios=any(f.upper() == "BIOS" for f in dump_flags),
        video=_video_from(regions, explicit_video), style=STYLE_TOSEC, date=date,
        publisher=publisher)


_TRAILING_DOT_RE = re.compile(r"(?<=[)\]])\.+$")


@functools.lru_cache(maxsize=65536)
def parse_name(name: str, style: str = STYLE_NOINTRO) -> Tags:
    """Parse a rom/set name (a trailing short extension is ignored)."""
    base = _EXT_RE.sub("", name.strip())
    base = _TRAILING_DOT_RE.sub("", base)     # a DAT entry such as "Rex Run (World) (Aftermarket) (Unl)." (No-Intro Game Boy)
    if style == STYLE_TOSEC:
        return _parse_tosec(base)
    if style == STYLE_REDUMP:
        return dataclasses.replace(_parse_nointro(base), style=STYLE_REDUMP)
    return _parse_nointro(base)


def style_of_rom(rom: Any) -> str:
    """The tag style of a DAT rom: Redump for those DATs, else No-Intro (set names) or TOSEC."""
    if getattr(rom, "dat", "") in REDUMP_DAT_NAMES:
        return STYLE_REDUMP
    return STYLE_NOINTRO if getattr(rom, "set_name", "") else STYLE_TOSEC


def of_rom(rom: Any) -> Tags:
    """Parsed tags of a DAT rom (``datfile.Rom.tags``)."""
    style = style_of_rom(rom)
    if style == STYLE_REDUMP:
        return redump_tags(rom.set_name or rom.game, getattr(rom, "category", ""))
    if style == STYLE_NOINTRO:
        return parse_name(rom.set_name, STYLE_NOINTRO)
    return parse_name(rom.name, STYLE_TOSEC)


# --------------------------------------------------------------------------- latest version

def _clean_dump_flags(t: Tags) -> tuple[str, ...]:
    return tuple(f for f in t.dump_flags if f != "!")


def supersede_key(t: Tags) -> tuple:
    """Group key for "latest version only": everything except the version (and TOSEC date)."""
    return (t.style, t.title.casefold(), t.regions, () if t.languages_implied else t.languages,
            t.status, tuple(f for f in t.flags if not is_edition(f)), _clean_dump_flags(t))     # (an edition competes with its game)


def _disk(t: Tags) -> Optional[tuple[int, int, int]]:
    """``(flag index, disk number, total)`` for a ``Disk N of M`` flag, else None."""
    for i, f in enumerate(t.flags):
        m = _DISK_RE.match(f)
        if m:
            def num(s: str) -> int:
                return int(s) if s.isdigit() else ord(s) - 64
            return i, num(m.group(1)), num(m.group(2))
    return None


def _set_key(t: Tags, disk_index: int) -> tuple:
    """supersede_key without the disk flag (and a per-disk label right after it)."""
    flags = list(t.flags)
    del flags[disk_index]
    if disk_index < len(flags) and flags[disk_index].casefold() in _DISK_LABELS:
        del flags[disk_index]
    return (t.style, t.title.casefold(), t.regions, () if t.languages_implied else t.languages,
            t.status, tuple(flags), _clean_dump_flags(t))


_VNUM_RE = re.compile(r"^[vV](\d+(?:\.\d+)*)")


def _fraction_widths(versions: Iterable[str]) -> dict[int, int]:
    """Dotted positions (after the first) where a group writes a part with a leading zero
    ("v1.02"): there, every member's part is read as a decimal fraction, so a mix like
    v1.02 / v1.1 or v1.01 / v1.21 / v1.3 orders as 1.02 < 1.1 and 1.21 < 1.3. Groups
    without zero-padding keep the integer order (v1.9 < v1.10)."""
    parts = [m.group(1).split(".") for m in map(_VNUM_RE.match, versions) if m]
    widths: dict[int, int] = {}
    for i in {i for p in parts for i, x in enumerate(p) if i and len(x) > 1 and x.startswith("0")}:
        widths[i] = max(len(p[i]) for p in parts if len(p) > i)
    return widths


def group_version_keys(names: Iterable[str], style: str = STYLE_NOINTRO) -> dict[str, tuple]:
    """Version key per name, comparable within one group (zero-padded ``v1.02`` read as fractions)."""
    return _group_keys(list(dict.fromkeys(names)), style)


def date_key(date: str) -> str:
    """Sortable form of a TOSEC date: unknown digits (``x`` / ``?``) count as ``0``."""
    return re.sub(r"[x?]", "0", date or "", flags=re.IGNORECASE)


def _group_keys(names: list[str], style: str) -> dict[str, tuple]:
    """Version key per name, comparable within one supersede group."""
    versions = {n: parse_name(n, style).version for n in names}
    widths = _fraction_widths(versions.values())
    if not widths:
        return {n: parse_name(n, style).version_key for n in names}
    return {n: _version_key(v, widths) for n, v in versions.items()}


def superseded(names: Iterable[str], style: str = STYLE_NOINTRO, prefer_premium: bool = True) -> dict[str, str]:
    """``{older name: newest name superseding it}`` among ``names``.

    Names are grouped by :func:`supersede_key`; in a group the highest
    :func:`version_key` wins and only names with a *strictly* lower key are
    superseded. Multi-disk names are superseded only when the winning version
    has all of its disks among ``names``.
    """
    uniq = list(dict.fromkeys(names))
    groups: dict[tuple, list[tuple[tuple, str]]] = {}
    disk_sets: dict[tuple, list[tuple[tuple, str, int, int]]] = {}
    for n in uniq:
        t = parse_name(n, style)
        d = _disk(t)
        if d is None:
            groups.setdefault(supersede_key(t), []).append((t.version_key, n))
        else:
            disk_sets.setdefault(_set_key(t, d[0]), []).append((t.version_key, n, d[1], d[2]))
    out: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        keys = _group_keys([n for _vk, n in members], style)
        # (the newest version; of the same version the plain release before an edition, a budget re-release or a bundle)
        members = [((keys[n], -edition_rank(parse_name(n, style), prefer_premium)), n) for _vk, n in members]
        best_key, best_name = max(members)
        for vk, n in members:
            if vk < best_key:
                out[n] = best_name
    for members in disk_sets.values():
        if len(members) < 2:
            continue
        keys = _group_keys([m[1] for m in members], style)
        members = [(keys[n], n, disk, tot) for _vk, n, disk, tot in members]
        best_key = max(m[0] for m in members)
        winners = [m for m in members if m[0] == best_key]
        total = max(m[3] for m in winners)
        by_disk: dict[int, str] = {}
        for _vk, n, disk, _tot in sorted(winners, key=lambda m: m[1]):
            by_disk.setdefault(disk, n)
        if not all(i in by_disk for i in range(1, total + 1)):
            continue  # newest version incomplete locally: keep everything
        for vk, n, disk, _tot in members:
            if vk < best_key:
                out[n] = by_disk.get(disk, by_disk[1])
    return out


# --------------------------------------------------------------------------- flag kinds

_LICENSE_FLAGS = {"unl", "aftermarket", "pirate", "hack", "homebrew", "pd", "fw", "sw", "sw-r",
                  "cw", "cw-r", "gw", "gw-r", "lw", "pw", "freeware", "shareware"}
_FLAG_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("alt", re.compile(r"^Alt(?: \d+)?$")),
    ("date", re.compile(r"^(?:19|20)[\dx?]{2}(?:-[\dx?]{2}(?:-[\dx?]{2})?)?$", re.IGNORECASE)),
    ("video", re.compile(r"^(?:PAL|NTSC|PAL-NTSC|NTSC-PAL|PAL60|MPAL|SECAM)$")),
    ("disk", re.compile(r"^(?:Dis[kc] (?:\d+|[A-Z])(?: of (?:\d+|[A-Z]))?|Side [A-Z])$")),
    ("language", re.compile(r"^(?:[A-Z][a-z]-[A-Za-z]{2,4}|M\d+)$")),
    ("serial", re.compile(r"^\d{5,}$|^[A-Z]{4}-\d{5}$")),
    # re-releases / compilations / digital stores the dump was taken from
    ("distribution", re.compile(
        r"Virtual Console|Switch Online|Classic Mini|Collection|Museum|Classics|Anniversary|"
        r"Legacy|Generations|Evercade|Steam|GOG|Digital|LodgeNet|Retro-Bit|Limited Run|"
        r"Flashback|^NP$|e-Reader|^Switch$|GameCube|^3DS|^DSI$|^Wii|Arcade|Capcom Town|"
        r"Animal Crossing|Rockman 123|Columbus Circle|FamicomBox|Competition Cart|Kiosk|"
        r"^Extract$|^Manual$|Netcard|Enhancement Chip|Emulator Optimized|Earlier|Later|"
        r"Hackers? Edition|Piko Interactive|QUByte|Rev [\d.]+|Program|Music|Super Game|Datach|"
        r"Famicom 3D|Family BASIC|Keyboard|Conversion|Toaplan|NESWorld|NintendoAge|Fixed|"
        r"Possible|Unknown", re.IGNORECASE)),
    # homebrew competitions
    ("event", re.compile(r"Jam|Compo|Byte-Off|NESDev|SNESDEV|Contest|Competition", re.IGNORECASE)),
    # the console features a release of a game has ("SGB Enhanced", "GB Compatible", "NDSi Enhanced", "Rumble Version", "60 Hz"):
    # the same game ("Foo" and "Foo (SGB Enhanced)" are one), no part of a game key
    ("feature", re.compile(r"^(?:CGB\+)?(?:SGB|GB|GBA|NDSi|DSi)(?:, (?:SGB|GB))?.*(?:Enhanced|Compatible)|^CGB\+SGB|^Rumble Version$|"
                           r"^60 Hz$|^dmaSafe$|^Joypad$|^Light Phaser$", re.IGNORECASE)),
    # cartridge boards / mappers / hardware
    ("hardware", re.compile(r"ROM$|^72 pin|^DS Broadcast$|^Sega Channel$|^Mega Drive (?:Mini|4)|Genesis Mini|"
                            r"ModRetro|^NINA|^Rainbow$|^AGA$|^OCS$|^ECS$|^A\d{3,4}|"
                            r"Mapper|pin cart|^Dev$|^CD32$|^CDTV$|^MT32$|"
                            r"^\d+(?:\.\d+)?\s?(?:KB|MB|k)(?: Chip)?$|^(?:Low|Fast|Slow|Chip) Mem$",
                            re.IGNORECASE)),
)


def flag_kind(flag: str) -> str:
    """Rough category of a ``(...)`` flag for facets/diagnostics.

    ``license | alt | date | video | disk | language | serial | distribution | event |
    hardware | other`` (``other`` = free text such as a publisher or developer name).
    """
    if flag.casefold() in _LICENSE_FLAGS:
        return "license"
    for kind, pattern in _FLAG_PATTERNS:
        if pattern.search(flag):
            return kind
    return "other"


# --------------------------------------------------------------------------- library rules
# Exclusion rule keys (see library.LibraryProfile.exclude), in report priority order.
RULES = ("bad_dump", "virus", "bad_size", "pre_release", "prototype", "demo", "faked",
         "unreleased", "modified", "budget", "online", "cd_conversion")
NEW_RULES = ("budget", "online", "cd_conversion")      # rules added after configs were first written: on for an older saved list too (library.migrate_config)
RULE_LABELS: dict[str, str] = {
    "bad_dump": "Bad dumps [b]",
    "virus": "Virus-infected [v]",
    "bad_size": "Over/under dumps [o] [u]",
    "pre_release": "Pre-release / beta / alpha / preview / debug",
    "prototype": "Prototypes",
    "demo": "Demos, samples, kiosk",
    "faked": "Faked [faked]",
    "unreleased": "Unreleased",
    "modified": "Modified [m]",
    "budget": "Budget re-releases",
    "online": "Online-only titles",
    "cd_conversion": "CD32 / CDTV conversions (floppy rips)",
}

# The classification tables below are the single source of truth: the rule catalog shown in the UI
# (``rule_tokens`` / ``library.rule_catalog``) is generated from them.
# Paren words (casefolded, a trailing number is ignored), in display order per rule.
_PAREN_WORDS: dict[str, tuple[str, ...]] = {
    "pre_release": ("beta", "alpha", "preview", "pre-release", "prerelease", "debug", "debug version",
                    "test program"),
    "prototype": ("proto", "possible proto"),
    "demo": ("demo", "tech demo", "auto demo", "sample", "kiosk"),
    "unreleased": ("unreleased",),
}
_PAREN_DEMO_PREFIX = "demo-"                       # every "(demo-xxx)" is a demo
_PAREN_DEMO_KINDS = ("playable", "rolling", "slideshow")   # the ones TOSEC uses (display only)
# the words No-Intro itself uses (the rest is TOSEC-only vocabulary)
_NOINTRO_PAREN_WORDS = frozenset({"beta", "proto", "possible proto", "demo", "tech demo", "auto demo",
                                  "sample", "kiosk", "debug", "debug version", "test program"})
_PAREN_RULE_OF: dict[str, str] = {w: r for r, ws in _PAREN_WORDS.items() for w in ws}
_TRAILING_NUM_RE = re.compile(r"\s+\d+$")
# bracket codes are case-sensitive: [b] bad dump but [B..] is something else.
# (rule, pattern, display tokens: " ..." stands for any free-text descriptor)
_BRACKET_RULES: tuple[tuple[str, "re.Pattern[str]", tuple[str, ...]], ...] = (
    ("bad_dump", re.compile(r"^b\d*(?:\s.*)?$|(?i:^a\d*\s+bad\s?dump\b)"), ("[b]", "[b ...]", "[a baddump]")),
    ("virus", re.compile(r"^v\d*(?:\s.*)?$|(?i:^inc\.?\s+virus\b)"), ("[v]", "[v ...]", "[inc. Virus]")),
    ("bad_size", re.compile(r"^[ou]\d*(?:\s.*)?$"), ("[o]", "[o ...]", "[u]", "[u ...]")),
    ("modified", re.compile(r"^m\d*(?:\s.*)?$|^modified\b"), ("[m]", "[m ...]", "[modified ...]")),
    ("faked", re.compile(r"^fake(?:d)?\b"), ("[faked]", "[faked ...]", "[fake release]")),
    ("unreleased", re.compile(r"^unreleased\b"), ("[unreleased]",)),
    ("pre_release", re.compile(r"^(?:beta|alpha|preview|pre-release)\b", re.IGNORECASE),
     ("[beta]", "[alpha]", "[preview]", "[pre-release]")),
    ("prototype", re.compile(r"^proto(?:type)?\b", re.IGNORECASE), ("[proto]", "[prototype]")),
    ("demo", re.compile(r"^(?:technical )?demo\b", re.IGNORECASE), ("[demo]", "[technical demo]")),
    # (TOSEC writes it with other notes in one bracket: "[HD, CD32 rip]", "[CD32 rip, Quarterback 6.0 archive]")
    ("cd_conversion", re.compile(r"^(?:.*,\s*)?(?:CD32|CDTV|CD) rip\b"), ("[CD32 rip]", "[CDTV rip]", "[HD, CD32 rip]")),
)
# a floppy conversion of a CD32 / CDTV game by its title ("Fears CD32 (1995)(Manyk)(AGA)(Disk 1 of 3)...")
_CD_TITLE_RE = re.compile(r"\s(CD32|CDTV)$")
_CR_RE = re.compile(r"^cr\d*(?:\s.*)?$")
_MOD_RE = re.compile(r"^(?:t|h|tr|f|a)\d*(?:\s.*)?$")


# Redump (Sega Dreamcast): the No-Intro words plus the Japanese trial / store-demo discs. Everything the
# DAT calls a demo is also covered by its <category> (Demos, Coverdiscs); the name tokens catch a disc whose
# category is missing.
_REDUMP_DEMO_RE = re.compile(
    r"^(?:(?:Tentou(?:-you)?|Tokubetsu)\s+)?Taikenban(?:\s+(?:\d+|Disc))?$"
    r"|^Tentou-you Demo(?:nstration)?(?:\s+Movie)?$|^Trial\b"
    r"|^Trade Demo$|^Jitsuen-you Sample$|^Demo Eizou$|^(?:Limited Edition\s+)?Preview Disc$|^Otameshiban$"
    r"|^Special Taiken Disc$|^Movie Demo Disc$|^Cheheompan$|^Taikai Sen'?you Disc$", re.IGNORECASE)
# The DS kiosk downloads, trailers and download-play clients (DS Download Station, DS Broadcast, "(Video)", "(Multiplayer)"): no game
_KIOSK_RE = re.compile(r"^(?:Video|Multiplayer|DS Broadcast|(?:DS )?Download Station\b.*)$", re.IGNORECASE)
_DEMO_TITLE_RE = re.compile(r"(?:^|[\s-])(?:Tentou\s+)?(?:Taikenban|Otameshiban)$", re.IGNORECASE)
_REDUMP_DEMO_TOKENS = ("(Taikenban)", "(Tentou Taikenban)", "(Tentou-you Taikenban)",
                       "(Tokubetsu Taikenban)", "(Tentou-you Demo)", "(Tentou-you Demo Movie)", "(Trial Disk)",
                       "(Trade Demo)", "(Jitsuen-you Sample)", "(Demo Eizou)", "(Preview Disc)", "(Otameshiban)", "(Cheheompan)")
# <category> -> rule (Games, Applications, Multimedia, Bonus Discs, Video and Add-Ons are kept)
CATEGORY_RULES: dict[str, str] = {"Demos": "demo", "Coverdiscs": "demo", "Preproduction": "prototype"}


def _paren_rule(part: str, style: str = STYLE_TOSEC) -> Optional[str]:
    p = _TRAILING_NUM_RE.sub("", part.strip().casefold())
    if style in (STYLE_REDUMP, STYLE_NOINTRO) and (_REDUMP_DEMO_RE.match(part.strip()) or _KIOSK_RE.match(part.strip())):
        return "demo"
    if p in _PAREN_RULE_OF:
        return _PAREN_RULE_OF[p]
    if p.startswith(_PAREN_DEMO_PREFIX):
        return "demo"
    return None


def _paren_rules(text: str, style: str = STYLE_TOSEC) -> list[str]:
    out: list[str] = []
    for part in text.split(", "):
        r = _paren_rule(part, style)
        if r and r not in out:
            out.append(r)
    return out


def classify_token(kind: str, text: str, style: str = STYLE_TOSEC) -> Optional[str]:
    """Exclusion rule key for one tag (``kind`` ``"("`` or ``"["``, ``text`` without delimiters), else None.

    A ``(a, b)`` paren group returns the first matching part's rule. ``style`` selects the vocabulary."""
    if kind == "(":
        rules = _paren_rules(text, style)
        return rules[0] if rules else None
    for rule, pattern, _tokens in _BRACKET_RULES:
        if pattern.match(text):
            return rule
    return None


def exclusion_info(t: Tags) -> list[tuple[str, str]]:
    """``[(rule, exact text incl. delimiters)]`` of every excluding tag, in name order, de-duplicated.

    Looks at the status and the ``(..)`` flags (the TOSEC publisher paren is skipped) and every ``[..]`` flag."""
    out: list[tuple[str, str]] = []

    def add(rule: str, text: str) -> None:
        if (rule, text) not in out:
            out.append((rule, text))

    parens = ([t.status] if t.status else [])
    flags = list(t.flags)
    if t.publisher and t.publisher in flags:
        flags.remove(t.publisher)
    for text in parens + flags:
        for rule in _paren_rules(text, t.style):
            add(rule, f"({text})")
    for text in t.dump_flags:
        rule = classify_token("[", text, t.style)
        if rule:
            add(rule, f"[{text}]")
    if t.style == STYLE_TOSEC:
        m = _CD_TITLE_RE.search(t.title or "")
        if m:
            add("cd_conversion", f"{m.group(1)} in the title")
    if t.category and CATEGORY_RULES.get(t.category):
        add(CATEGORY_RULES[t.category], f"(category: {t.category})")
    if t.style in (STYLE_REDUMP, STYLE_NOINTRO):
        m = _DEMO_TITLE_RE.search(t.title or "")
        if m:                                   # ("Super Bomberman 2 - Caravan You Taikenban": the trial in the title)
            add("demo", m.group(0).strip(" -"))
        for f in t.flags:
            if edition_kind(f) == "budget":
                add("budget", f"({f})")
        online = online_only(t)
        if online:
            add("online", online)
    return out


def exclusion_rules(t: Tags) -> frozenset[str]:
    return frozenset(r for r, _x in exclusion_info(t))


def bad_flags(t: Tags) -> list[str]:
    """Exact bracketed texts of the bad-dump flags, e.g. ``["[b corrupt file]"]``."""
    return [f"[{f}]" for f in t.dump_flags if is_bad(f)]


def is_cracked(t: Tags) -> bool:
    return any(_CR_RE.match(f) for f in t.dump_flags)


def modification_count(t: Tags) -> int:
    """Number of ``[t] [h] [tr] [f] [a]`` flags (trainers, hacks, translations, fixes, alternates)."""
    return sum(1 for f in t.dump_flags if _MOD_RE.match(f))


# --- platform (chipset) versions of one Amiga title: a ranking attribute, NOT part of the identity
_CHIPSET_RE = re.compile(r"^(?:OCS|ECS|AGA|CD32|CDTV)(?:-(?:OCS|ECS|AGA|CD32|CDTV))*$")
# Platform classes best-first: a CD32 disc, an AGA floppy, a CDTV disc, an ECS floppy, the rest. CD32 / CDTV are the discs of the
# CD DATs (library: Platform.disc_dats), never a tag on a floppy - a floppy with "(CD32)" is a conversion, ranked by its chipset.
PLATFORM_ORDER: tuple[str, ...] = ("CD32", "AGA", "CDTV", "ECS", "OCS")
_DISC_CLASSES = frozenset({"CD32", "CDTV"})
PLATFORM_BASE = "OCS"                  # untagged / OCS / ECS-only / anything without a better chipset


def is_chipset(flag: str) -> bool:
    """True for a bare chipset tag: ``AGA``, ``OCS-AGA``, ``ECS-AGA``, ``OCS-ECS-AGA``, ``OCS``, ``CD32``.

    ``AGA Data`` / ``Car Disk 1 AGA`` (a label of one disk) are not chipset tags."""
    return bool(_CHIPSET_RE.match(flag))


def _edition_flags(t: Tags) -> list[str]:
    """``t.flags`` before the disk token (what follows it is a per-disk label)."""
    d = _disk(t)
    return list(t.flags[:d[0]] if d else t.flags)


def chipset(t: Tags) -> tuple[str, ...]:
    """The chipset / platform tags of a TOSEC name (before the disk token), e.g. ``("OCS-AGA",)``."""
    if t.style != STYLE_TOSEC:
        return ()
    return tuple(f for f in _edition_flags(t) if is_chipset(f))


def platform_class(t: Tags) -> str:
    """One of :data:`PLATFORM_ORDER`: the best platform a floppy's chipset tags allow (untagged -> ``OCS``; "CD32" / "CDTV" tags do
    not count - those classes are the CD DATs' discs)."""
    found = {part for c in chipset(t) for part in c.split("-")} - _DISC_CLASSES
    for cls in PLATFORM_ORDER:
        if cls in found:
            return cls
    return PLATFORM_BASE


def platform_rank(t: Tags) -> int:
    """0 = best platform (index in :data:`PLATFORM_ORDER`)."""
    return PLATFORM_ORDER.index(platform_class(t))


def chipset_classes(t: Tags) -> frozenset[str]:
    """The platform classes a TOSEC disk claims: parts of its chipset tags with ``ECS`` counted as ``OCS``;
    a disk without a chipset tag is plain ``OCS`` (the TOSEC default). ``OCS-AGA`` = ``{OCS, AGA}``."""
    parts = {("OCS" if part == "ECS" else part) for c in chipset(t) for part in c.split("-")}
    return frozenset(parts) or frozenset({PLATFORM_BASE})


def chipset_compatible(anchor: Tags, other: Tags) -> bool:
    """Whether disk ``other`` may stand in a set built around disk 1 ``anchor`` (borrowing across editions).

    Conservative: ``other`` must run on every platform class the anchor claims (``anchor`` classes are a
    subset of ``other``'s). So an OCS set never takes an AGA-only disk and an AGA set never takes an untagged
    (OCS) disk; a ``OCS-AGA`` disk fits both an OCS and an AGA set."""
    return chipset_classes(anchor) <= chipset_classes(other)


def identity_key(t: Tags) -> tuple:
    """What makes two names the *same game* across versions, years, disks, dump variants,
    languages and chipsets.

    TOSEC: style, title, countries, publisher, edition flags (``M3``, ``PAL``, ``AGA Data`` ...: the
    ``(..)`` flags before the disk token minus publisher, dates and bare chipset tags such as
    ``AGA`` / ``OCS-AGA``) and status tokens. Languages and chipsets are ranking attributes, not part
    of it. No-Intro: ``(style, title, regions)`` (latest-per-region is handled by :func:`superseded`;
    one-per-game uses :func:`game_key`)."""
    if t.style != STYLE_TOSEC:
        return (t.style, t.title.casefold(), t.regions)
    flags = _edition_flags(t)
    if t.publisher and t.publisher in flags:
        flags.remove(t.publisher)
    edition = tuple(f for f in flags
                    if flag_kind(f) != "date" and not _paren_rules(f) and not is_chipset(f))
    statuses = tuple(sorted({text for _r, text in exclusion_info(t)
                             if _r in ("pre_release", "prototype", "demo", "unreleased")}))
    return (t.style, t.title.casefold(), t.regions, t.publisher.casefold(), edition, statuses)


def partition_key(t: Tags) -> tuple:
    """What must be equal for two disks to share one playlist: explicit languages + chipset tags."""
    return (() if t.languages_implied else t.languages, chipset(t))


_GAME_KEY_SKIP_KINDS = frozenset({"date", "video", "serial", "alt", "distribution", "language", "feature"})


def game_key(t: Tags) -> tuple:
    """Identity of a *game* across regions, languages, revisions and re-releases.

    No-Intro: title + status (Beta/Proto ...) + the tags that make a distinct product (licence
    ``Unl`` / ``Aftermarket`` / ``Pirate``, publishers, hardware, events, dump flags); regions, languages,
    versions, dates, ``Alt``, ``PAL``/``NTSC`` and re-release tags (``Virtual Console`` ...) are NOT in it.
    TOSEC: :func:`identity_key`."""
    if t.style == STYLE_TOSEC:
        return identity_key(t)
    if t.style == STYLE_REDUMP:
        return redump_game_key(t)
    flags = [f for f in t.flags if flag_kind(f) not in _GAME_KEY_SKIP_KINDS and flag_kind(f) != "disk" and not is_edition(f)]
    return (t.style, t.title.casefold(), t.status.casefold(), _no_bootleg(flags, t),
            _key_dump_flags(t))


# Where unlicensed copies of other games were made (the Taiwanese Master System carts, the Chinese and Korean ones): an "(Unl)" release
# of these regions alone is a copy of the game of its title. Elsewhere an unlicensed release is a game of its own ("Tetris (USA) (Unl)"
# is Tengen's Tetris, not Nintendo's).
_BOOTLEG_REGIONS = frozenset({"Taiwan", "China", "Hong Kong", "Korea", "Asia", "Russia", "Brazil"})


def _no_bootleg(flags: list, t: "Tags") -> tuple:
    """The product tags of a game key (casefold, sorted). Of a copy of a game they are none: a pirate copy (the maker, the board), an
    unlicensed one of the bootleg regions or of a re-release (``(Atari Lynx Collection 1) (Unl)``) is the game of its title
    (:func:`unlicensed` ranks them last). Homebrew (``Aftermarket``) is a game of its own, whatever competition, publisher, board or store
    it came from (``(NESDev 2018)``, ``(Sly Dog Studios)``, ``(UxROM)``, ``(itch.io)``: versions of it)."""
    low = [f.casefold() for f in flags]
    if "pirate" in low:
        return ()
    if "aftermarket" in low:
        return tuple(sorted(f for f, raw in zip(low, flags) if flag_kind(raw) == "license"))
    if "unl" in low and ((t.regions and set(t.regions) <= _BOOTLEG_REGIONS) or any(is_edition(f) for f in t.flags)):
        return tuple(sorted(f for f, raw in zip(low, flags) if flag_kind(raw) not in ("license", "other", "hardware")))
    return tuple(sorted(f for f in low if flag_kind(f) != "event"))


def _key_dump_flags(t: "Tags") -> tuple:
    """The ``[..]`` flags that are part of a game key: not ``[!]``, not a disc id (``[ACPP52]``, GameTDB's Wii U names)."""
    return tuple(sorted(f for f in t.dump_flags if f != "!" and not _DISC_ID_RE.match(f)))


_DISC_ID_RE = re.compile(r"^[A-Z0-9]{6}$|^[0-9A-F]{16}$")          # (a GameTDB disc id, a Switch title id)


_BONUS_RE = re.compile(r"^(?:.*\bbonus disc\b.*|the making of|un cd bonus|making of)$", re.IGNORECASE)
_BONUS_TITLE_RE = re.compile(r"\bbonus\b.*\bdisc\b", re.IGNORECASE)


def bonus_disc(t: "Tags") -> bool:
    """A bonus or making-of disc of a game (Redump category "Bonus Discs"): kept, but not rated as the game."""
    return (t.category == "Bonus Discs" or any(_BONUS_RE.match(f.strip()) for f in t.flags)
            or bool(_BONUS_TITLE_RE.search(t.title or "")))


def unlicensed(t: "Tags") -> bool:
    """A pirate or unlicensed release (not homebrew): of a game's releases the licensed one is kept first."""
    fl = {f.casefold() for f in t.flags}
    return bool(fl & {"pirate", "unl"}) and "aftermarket" not in fl


_REDUMP_KEY_SKIP_KINDS = _GAME_KEY_SKIP_KINDS | {"disk"}
_DISC_RE = re.compile(r"^Dis[ck] (\d+|[A-Z])$")
# The releases of a game that are no game of their own (Redump and No-Intro names). Of a game's releases one is kept, by rank:
#   premium  special, limited, collector's, first-print editions, boxes and packs, other builds (can hold more than the original):
#            the best when "Prefer special and limited editions" is on, else just after the plain release;
#   plain    the standard release;
#   store    store editions, bundles, re-releases of the same content (Toys R Us, Controller Set, EDC, Reprint ...);
#   budget   budget lines ("the Best", Greatest Hits, Platinum, @barai ...): last, and set aside by the rule "Budget re-releases"
#            when another release of the game is there.
_PREMIUM_RE = re.compile(
    r"^(?:special edition|limited (?:edition|box)|collector'?s edition|kickstarter edition|director'?s cut|(?:super )?premium (?:box|pack|package)|"
    r"deluxe pack|dx pack|super value set|(?:shokai |tokubetsu |club nintendo )?genteiban|shokai seisan-?ban|tokubetsu-?ban|"
    r"disney classic games|strictly limited games|final cut|premium edition|extra edition|gentei box|special limited pack|"
    r"platinum limited|.+ box(?: saishuu-ban)?|ebkore plus|append-ban|premium-ban|tsutaya original version|kabunushi goyuutai-ban|"
    r".+ hakkou kinen|shindou edition)$", re.IGNORECASE)
_BUDGET_RE = re.compile(
    r"^(?:dorikore|satakore|sega the best|playstation(?: 2)? the best(?: for family)?|psone books|greatest hits|platinum|player'?s choice|"
    r"major wave|the best|best|artdink best choice|gust best price|honkakuha de 1300|konami collector'?s series|@barai|fukyuu-?ban|renkaban|"
    r"[\w'.-]+(?: [\w'.-]+)? the best|[\w'.-]+ best|eternal hits|best collection|superlite 1500|mega hits!?)$",
    re.IGNORECASE)
_STORE_RE = re.compile(
    r"^(?:rerelease|reprint(?: [a-z])?|ratalaika games|edc|keihin ban|toys ?'?r'? ?us(?: exclusive)?|walmart(?: edition)?|"
    r"best buy(?: edition)?|target(?: limited edition)?|controller set|with guncon2?|(?:eyetoy camera )?doukonban|tatacon doukon set|"
    r"shotenban|launch cart|"
    # re-releases of the same game: TV / download services, mini consoles, compilations and collections, re-issues
    r"sega channel|mega drive (?:mini(?: 2)?|4)(?:, genesis mini(?: 2)?)?|genesis mini(?: 2)?|sega smash pack(?: \d)?|sega ages|lock-on|"
    r"mega drive playtv|iam8bit|master system evolution|sega 3d fukkoku akaibusu|piepacker|heartbeat catalyst|switch, ps vita|"
    r"modretro chromatic|atari lynx 30th birthday|pokemon box|nhl 0\d|retro-bit|limited run games|evercade|(?:.+ )?virtual console|"
    r"(?:nintendo )?switch online|.*\bcollection\b.*|.*\banniversary\b.*|capcom town|steam|gog|modem pack|recalled|"
    r".*\+ (?:guncon|jogcon|.*controller).*|front mission history|sega classic|blockbuster flash rom|itch\.io|amiga format)$", re.IGNORECASE)


def edition_kind(flag: str) -> str:
    """``premium`` | ``budget`` | ``store`` for a release tag of the same game, else ""."""
    f = flag.strip()
    if _BUDGET_RE.match(f):
        return "budget"
    if _STORE_RE.match(f):
        return "store"
    if _PREMIUM_RE.match(f):
        return "premium"
    return ""


def is_edition(flag: str) -> bool:
    """True for a tag that marks a release of the same game (an edition, a budget line, a store edition, a bundle)."""
    return bool(edition_kind(flag))


def edition_rank(t: "Tags", prefer_premium: bool = True) -> int:
    """How a release of a game ranks among its others (lower = kept first): a premium edition (0 when preferred, else 2), the plain
    release 1, a store edition or bundle 3, a budget release 4."""
    kinds = {edition_kind(f) for f in t.flags} - {""}
    if "budget" in kinds:
        return 4
    if "store" in kinds:
        return 3
    if "premium" in kinds:
        return 0 if prefer_premium else 2
    return 1


# Online-only titles: the network discs (browsers, start-up and access discs, the Passport discs) and games that only ran on servers that
# are gone. Excluded by the rule "Online-only titles".
_ONLINE_FLAG_RE = re.compile(r"^(?:Shenmue |Dream |Broadband )?Passport$", re.IGNORECASE)
_ONLINE_TITLE_RE = re.compile(
    r"^(?:Dream Passport|Broadband Passport|(?:Internet |Web )Browser|EGBrowser|Network Access Disc|Online Start-Up Disc|"
    r"Network Adaptor Start-Up Disc|PlayOnline Viewer|Final Fantasy XI|EverQuest - Online Adventures|Sakura Taisen Online|"
    r"Twisted Metal - Black Online|Hardware - Online Arena|Speed Devils - Online Racing|Nobunaga no Yabou Online|Gundam Battle Online|"
    r"Gangcheol Gigap Sadan - Online Battlefield|Chou! Tanoshii Internet|Yume Baken '99 - Internet)\b", re.IGNORECASE)
_ONLINE_TOKENS = ("(Shenmue Passport)", "Dream Passport", "Web Browser", "Online Start-Up Disc", "Network Access Disc",
                  "Final Fantasy XI", "EverQuest - Online Adventures", "Sakura Taisen Online")
_BUDGET_TOKENS = ("(Greatest Hits)", "(Platinum)", "(Player's Choice)", "(PlayStation the Best)", "(Sega the Best)", "(Dorikore)",
                  "(Major Wave)", "(@barai)", "(Best)")


def online_only(t: "Tags") -> str:
    """The text that makes a name an online-only title, else ""."""
    for f in t.flags:
        if _ONLINE_FLAG_RE.match(f.strip()):
            return f"({f})"
    if t.style in (STYLE_REDUMP, STYLE_NOINTRO) and _ONLINE_TITLE_RE.match(t.title or ""):
        return "(online only)"
    return ""



def disc_number(t: Tags) -> int:
    """The ``(Disc N)`` number of a Redump name (0 = a single disc game)."""
    for f in t.flags:
        m = _DISC_RE.match(f)
        if m:
            g = m.group(1)
            return int(g) if g.isdigit() else ord(g) - 64      # "(Disc A)" / "(Disc B)" = disc 1 / 2
    return 0


def redump_game_key(t: Tags) -> tuple:
    """Identity of a Redump *game* across regions, languages, revisions, re-releases and DISCS: title + status
    + the tags that make a distinct product (``Unl``, editions, publishers, trial discs...). ``(Disc N)``,
    dates, video tags, ``Alt`` and ``Rerelease`` are not part of it."""
    keep: list[str] = []
    after_disc = False
    for f in t.flags:
        kind = flag_kind(f)
        if kind == "disk":
            after_disc = True               # (what follows "(Disc N)" names that disc: "(Leon-hen)", "(Ichi)", "(English Voice Over)")
            continue
        if kind in _REDUMP_KEY_SKIP_KINDS or is_edition(f) or (after_disc and kind == "other"):
            continue
        keep.append(f)
    return (t.style, t.title.casefold(), t.status.casefold(), _no_bootleg(keep, t),
            _key_dump_flags(t))


def redump_tags(name: str, category: str = "") -> Tags:
    """Tags of a Redump game name, with its DAT ``<category>`` (cached)."""
    return _redump_tags(name, category)


@functools.lru_cache(maxsize=16384)
def _redump_tags(name: str, category: str) -> Tags:
    t = parse_name(name, STYLE_REDUMP)
    return dataclasses.replace(t, category=category) if category else t


def title_key(t: Tags) -> tuple:
    """A *title* as the user knows it (used by the vanish report): TOSEC = title + publisher (any
    country, language, edition, status); No-Intro = :func:`game_key` without the status."""
    if t.style == STYLE_TOSEC:
        return (t.style, t.title.casefold(), t.publisher.casefold())
    k = game_key(t)
    return k[:2] + k[3:]


def extra_tag_count(t: Tags) -> int:
    """Tags beyond the plain release (fewer = closer to the standard release): non-date/serial/video flags + dump flags except ``[!]``."""
    return (sum(1 for f in t.flags if flag_kind(f) not in ("date", "serial", "video"))
            + sum(1 for f in t.dump_flags if f != "!"))


# --------------------------------------------------------------------------- languages

# Regions that tags.REGION_LANGUAGE leaves without a language (used by the language filter only).
EXTRA_REGION_LANGUAGE: dict[str, str] = {
    "Switzerland": "De", "Belgium": "Nl", "Czech": "Cs", "Hungary": "Hu", "Croatia": "Hr",
    "Turkey": "Tr", "India": "En", "United Arab Emirates": "Ar", "Scandinavia": "Sv",
}
_M_RE = re.compile(r"^M\d+$")
_TR_RE = re.compile(r"^tr\d*\s+([A-Za-z]{2}(?:-[A-Za-z]{2,4})*)\b")


def tr_languages(t: Tags) -> tuple[str, ...]:
    """Languages of the ``[tr <code> ...]`` translation flags (``[tr de]`` -> ``("De",)``)."""
    out: list[str] = []
    for f in t.dump_flags:
        m = _TR_RE.match(f)
        if not m:
            continue
        for part in m.group(1).split("-"):
            code = part.capitalize()
            if code in LANGUAGES and code not in out:
                out.append(code)
    return tuple(out)


def variant_languages(t: Tags) -> frozenset[str]:
    """Every language a release can be played in.

    A language tag lists them (``(de-en)``, ``(En,Fr,De)`` = all of them); without one the country's
    official language counts (``(DE)`` -> De); no language and no country (TOSEC convention) or
    only a language-neutral region (World / Asia / Unknown) = English; a TOSEC ``(M3)`` multi-language
    tag adds English; ``[tr <code> ...]`` adds that language."""
    langs: set[str] = set(t.languages)
    if t.languages_implied:
        for r in t.regions:
            extra = EXTRA_REGION_LANGUAGE.get(r)
            if extra:
                langs.add(extra)
        if not langs:
            langs.add("En")
    if t.style == STYLE_TOSEC and any(_M_RE.match(f) for f in t.flags):
        langs.add("En")
    langs.update(tr_languages(t))
    return frozenset(langs)


# --------------------------------------------------------------------------- keep-flags / regions

KEEP_FLAGS: tuple[str, ...] = ("cr", "h", "t", "a", "f", "tr")
KEEP_FLAG_LABELS: dict[str, str] = {
    "cr": "Cracked [cr]", "h": "Hacks [h]", "t": "Trainers [t]", "a": "Alternates [a]",
    "f": "Fixes [f]", "tr": "Translations [tr]",
}
_FLAG_RES = {code: re.compile(rf"^{code}\d*(?:\s.*)?$") for code in KEEP_FLAGS}


def flag_types(t: Tags) -> frozenset[str]:
    """Which of :data:`KEEP_FLAGS` the name carries (``[cr FLT][h Triangle]`` -> ``{cr, h}``)."""
    return frozenset(code for code, rx in _FLAG_RES.items() if any(rx.match(f) for f in t.dump_flags))


DEFAULT_REGION_PRIORITY: tuple[str, ...] = ("Europe", "USA", "World", "Japan")


def region_order(priority: Iterable[str] = DEFAULT_REGION_PRIORITY, present: Optional[Iterable[str]] = None) -> list[str]:
    """``priority`` (canonical names) followed by the other regions in alphabetical order: those found in the data
    (``present``) or, when that is not known, every region the app knows."""
    first = list(dict.fromkeys(canon_region(p) for p in priority if p))
    pool = set(present) if present is not None else set(REGIONS)
    return first + sorted(r for r in pool if r not in first)


def region_tiers(order: list[str], ties: Iterable[str] = ()) -> dict[str, int]:
    """``{region: tier}`` of an ordered region list: each region is one tier after the one above it, unless it is in ``ties``
    ("as good as the region above"), which gives it the same tier (so Europe and USA tied are both tier 0)."""
    tied = {canon_region(t) for t in ties}
    out: dict[str, int] = {}
    tier = -1
    for i, r in enumerate(order):
        if i == 0 or r not in tied:
            tier += 1
        out[r] = tier
    return out


def region_rank(regions: Iterable[str], order: Any, ties: Iterable[str] = ()) -> int:
    """Best (lowest) tier of any of ``regions``; unknown regions rank after all. ``order`` is an ordered list (with ``ties``,
    see :func:`region_tiers`) or the ``{region: tier}`` that :func:`region_tiers` returns."""
    pos = order if isinstance(order, dict) else region_tiers(order, ties)
    worst = max(pos.values(), default=-1) + 1
    ranks = [pos.get(r, worst) for r in regions]
    return min(ranks) if ranks else worst


# --------------------------------------------------------------------------- rule tokens (UI catalog)

def _display_paren(word: str, style: str) -> str:
    if style in (STYLE_NOINTRO, STYLE_REDUMP):
        return "(" + " ".join(w.capitalize() for w in word.split(" ")) + ")"
    return f"({word})"


def rule_tokens(style: str) -> dict[str, list[str]]:
    """``{rule: [exact tokens as they appear in names]}`` for ``style``, generated from the tables
    :func:`classify_token` uses. No-Intro lists only the words No-Intro uses (and the bracket flags)."""
    out: dict[str, list[str]] = {r: [] for r in RULES}
    if style == STYLE_REDUMP:
        for rule, words in _PAREN_WORDS.items():
            for w in words:
                if w in _NOINTRO_PAREN_WORDS and w not in ("debug", "debug version", "test program"):
                    out[rule].append(_display_paren(w, style))
        out["demo"].extend(_REDUMP_DEMO_TOKENS)
        for cat, rule in CATEGORY_RULES.items():
            out[rule].append(f"category {cat}")
        out["budget"].extend(_BUDGET_TOKENS)
        out["online"].extend(_ONLINE_TOKENS)
        return out
    for rule, words in _PAREN_WORDS.items():
        for w in words:
            if style == STYLE_NOINTRO and w not in _NOINTRO_PAREN_WORDS:
                continue
            out[rule].append(_display_paren(w, style))
        if rule == "demo" and style == STYLE_TOSEC:
            out[rule].extend(f"({_PAREN_DEMO_PREFIX}{k})" for k in _PAREN_DEMO_KINDS)
            out[rule].append(f"({_PAREN_DEMO_PREFIX}*)")
    for rule, _rx, tokens in _BRACKET_RULES:
        if style == STYLE_NOINTRO and rule != "bad_dump":
            continue
        out[rule].extend(tokens)
    if style == STYLE_NOINTRO:
        out["budget"].extend(_BUDGET_TOKENS)
    return out


def token_rule(token: str, style: str = STYLE_TOSEC) -> Optional[str]:
    """Classify a catalog token (``"(beta)"``, ``"[b ...]"``, ``"(demo-*)"``) with :func:`classify_token`."""
    if token in _ONLINE_TOKENS:
        return "online"
    if token in _BUDGET_TOKENS:
        return "budget"
    if token.startswith("category "):
        return CATEGORY_RULES.get(token[len("category "):])
    kind, body = token[0], token[1:-1].replace(" ...", " x").replace("*", "x")
    return classify_token(kind, body, style)


# --------------------------------------------------------------------------- JSON / diagnostics

def to_json(t: Tags) -> dict[str, Any]:
    out = {
        "regions": list(t.regions),
        "languages": list(t.languages),
        "languages_implied": t.languages_implied,
        "variant_languages": sorted(variant_languages(t)),     # (what the language rules read: TOSEC's untagged release is English)
        "version": t.version,
        "status": t.status,
        "flags": list(t.flags),
        "dump_flags": list(t.dump_flags),
        "bad": t.bad,
        "bios": t.bios,
        "video": list(t.video),
        "bad_flags": bad_flags(t),
        "excluded_by": [{"rule": r, "text": x} for r, x in exclusion_info(t)],
    }
    if t.category:      # Redump only
        out["category"] = t.category
    return out
