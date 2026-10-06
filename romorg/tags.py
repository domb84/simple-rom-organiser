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
STYLE_WHDLOAD = "whdload"      # MrV2K's WHDLoad database: Retroplay .lha names (see parse_whdload)
STYLE_REDUMP = "redump"        # Redump (Dreamcast, PlayStation, PlayStation 2): No-Intro style names + <category> + (Disc N)
# Header name of the Redump DATs that use that style (== redump.DAT_NAME): only the DAT name selects it, so
# nothing of it can leak into the No-Intro consoles.
REDUMP_DAT_NAMES = frozenset({"Sega - Dreamcast", "Sony - PlayStation", "Sony - PlayStation 2"})
# Header name of that DAT (== whdload.DAT_NAME): the only thing that selects the WHDLoad style, so
# nothing of it can leak into the TOSEC Amiga system.
WHDLOAD_DAT_NAME = "Commodore - Amiga - WHDLoad"

# region -> "PAL" | "NTSC" | "" (unknown / both)
REGIONS: dict[str, str] = {}
for _r in ("Europe", "Australia", "New Zealand", "Germany", "France", "Spain", "Italy", "Netherlands",
           "Sweden", "Denmark", "Norway", "Finland", "Scandinavia", "UK", "United Kingdom", "Ireland",
           "Portugal", "Austria", "Switzerland", "Belgium", "Greece", "Poland", "Russia", "Croatia",
           "Czech", "Hungary", "Turkey", "South Africa", "China", "Hong Kong", "India", "Argentina",
           "United Arab Emirates"):
    REGIONS[_r] = "PAL"
for _r in ("USA", "Canada", "Japan", "Korea", "Taiwan", "Brazil", "Mexico", "Peru", "Latin America"):
    REGIONS[_r] = "NTSC"
for _r in ("World", "Asia", "Unknown"):
    REGIONS[_r] = ""
del _r

TOSEC_COUNTRIES: dict[str, str] = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria", "AU": "Australia",
    "BE": "Belgium", "BR": "Brazil", "CA": "Canada", "CH": "Switzerland", "CN": "China",
    "CZ": "Czech", "DE": "Germany", "DK": "Denmark", "ES": "Spain", "EU": "Europe",
    "FI": "Finland", "FR": "France", "GB": "United Kingdom", "GR": "Greece", "HK": "Hong Kong",
    "HR": "Croatia", "HU": "Hungary", "IE": "Ireland", "IN": "India", "IT": "Italy",
    "JP": "Japan", "KR": "Korea", "MX": "Mexico", "NL": "Netherlands", "NO": "Norway",
    "NZ": "New Zealand", "PL": "Poland", "PT": "Portugal", "RU": "Russia", "SE": "Sweden",
    "TR": "Turkey", "TW": "Taiwan", "US": "USA", "ZA": "South Africa",
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
        (("USA", "World", "Europe", "Australia", "New Zealand", "UK", "United Kingdom", "Canada",
          "Ireland", "South Africa"), "En"),
        (("Japan",), "Ja"), (("Germany", "Austria"), "De"), (("France",), "Fr"),
        (("Spain", "Mexico", "Argentina", "Latin America", "Peru"), "Es"), (("Italy",), "It"),
        (("Netherlands",), "Nl"), (("Sweden",), "Sv"), (("Norway",), "No"), (("Denmark",), "Da"),
        (("Finland",), "Fi"), (("Brazil", "Portugal"), "Pt"), (("Russia",), "Ru"),
        (("Korea",), "Ko"), (("China", "Taiwan", "Hong Kong"), "Zh"), (("Poland",), "Pl"),
        (("Greece",), "El")):
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
    build: int = 0                # WHDLoad: the 4-digit build number of the archive name (0 = none)
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
    return (1, 0)


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


def _parse_language_group(text: str) -> tuple[Optional[tuple[str, ...]], list[str]]:
    """``(codes, variants)``: "En,Pt-BR" -> (("En", "Pt"), ["Pt-BR"]); not a language group -> (None, [])."""
    out: list[str] = []
    variants: list[str] = []
    for raw in re.split(r"[,+]", text):
        p = raw.strip()
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
                         if k == "(" and all(p in REGIONS for p in t.split(", "))), 0)
    if first_region and all(k == "(" for k, _t in groups[:first_region]):
        title = " ".join([title] + [f"({t})" for _k, t in groups[:first_region]])
        groups = groups[first_region:]
    for kind, text in groups:
        if kind == "[":
            dump_flags.append(text)
            continue
        if not regions:
            parts = text.split(", ")
            if all(p in REGIONS for p in parts):
                regions = tuple(parts)
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
    if style == STYLE_WHDLOAD:
        return parse_whdload(base, "")
    if style == STYLE_REDUMP:
        return dataclasses.replace(_parse_nointro(base), style=STYLE_REDUMP)
    return _parse_nointro(base)


# --------------------------------------------------------------------------- WHDLoad (Retroplay .lha)
# Entries of "Commodore - Amiga - WHDLoad": game name ``Title (German) (AGA) (Beta)`` + archive name
# ``Title_v1.2_De_AGA_0417.lha``. The game name is the identity (clean title, product tags, status); the
# archive stem adds the version, the 4-digit build number and variant codes the game name may lack
# (``De``/``EnFrDe``, ``AGA``, ``NTSC``, ``512k``, ``Hack_by_<author>``...). Measured on the real DAT
# (4121 entries): see docs/ARCHITECTURE.md, Amendment 10.

_WHD_LANG_WORDS: dict[str, str] = {
    "german": "De", "french": "Fr", "italian": "It", "spanish": "Es", "polish": "Pl", "danish": "Da",
    "czech": "Cs", "swedish": "Sv", "greek": "El", "finnish": "Fi", "dutch": "Nl", "croatian": "Hr",
    "norwegian": "No", "portuguese": "Pt", "russian": "Ru", "hungarian": "Hu", "turkish": "Tr",
    "english": "En",
}
# language codes of the archive stem (Retroplay uses Cz / Dk / Se / Gr; "No" is left out: ambiguous
# with "No_jump" / "No_Music" style words)
_WHD_STEM_LANGS: dict[str, str] = {
    "En": "En", "De": "De", "Fr": "Fr", "It": "It", "Es": "Es", "Pl": "Pl", "Cz": "Cs", "Dk": "Da",
    "Nl": "Nl", "Se": "Sv", "Gr": "El", "Fi": "Fi", "Hr": "Hr", "Pt": "Pt", "Ru": "Ru", "Hu": "Hu",
    "Tr": "Tr",
}
_WHD_MULTILANG_RE = re.compile(r"^(?:(?:%s)){2,}$" % "|".join(_WHD_STEM_LANGS))
_WHD_VERSION_TOKEN_RE = re.compile(r"^[vV]\d+(?:\.\d+)*[a-z]?(?:-[A-Za-z])?$")   # v1.2, v1.4a, v2.1-B
_WHD_BUILD_RE = re.compile(r"^\d{4}(?:&\d{4})*$")
_WHD_MEMORY_RE = re.compile(r"^(\d+(?:\.\d+)?)\s?(?:KB|k|MB|Mb)(?: Chip)?$", re.IGNORECASE)
_WHD_MEMORY_WORDS = {"low mem", "fast mem", "slow mem", "chip mem", "fast"}
_WHD_LOW_MEMORY = {"512kb", "512k", "low mem"}
_WHD_STATUS_RE = re.compile(r"^(?:Beta|Pre Release|Preview|Unreleased|Game Demo|Demo)(?:\s+\d+)?$")
# canonical display form of the flags a stem token stands for (also folds "2 Disk" -> "Two Disk")
_WHD_STEM_FLAGS: dict[str, str] = {
    "image": "Image", "files": "Files", "1disk": "One Disk", "2disk": "Two Disk", "3disk": "Three Disk",
    "4disk": "Four Disk", "cd": "CD-ROM", "nointro": "No Intro", "lores": "Low Res", "hires": "Hi Res",
    "atarist": "ST Port", "altversion": "Alt Version", "publicdomain": "PD", "enhanced": "Enhanced",
    "arcadia": "Arcadia", "mt32": "MT32", "cdtv": "CDTV", "censored": "Censored", "crunched": "Crunched",
    "nomusic": "No Music", "nospeech": "No Speech", "novoice": "No Voice", "easyplay": "Easy Play",
    "ecs": "ECS", "ocs": "OCS", "aga": "AGA", "cd32": "CD32", "ntsc": "NTSC", "68020": "68020", "68030": "68030",
    "68040": "68040", "68060": "68060", "hack": "Hack",
}
_WHD_FLAG_ALIASES = {"2 disk": "two disk", "1 disk": "one disk", "3 disk": "three disk",
                     "4 disk": "four disk", "cd rom": "cd-rom"}
_WHD_CANON_MEMORY = {"15mb": "1.5MB", "1mbchip": "1MB Chip", "1mb": "1MB", "2mb": "2MB", "8mb": "8MB",
                     "12mb": "12MB", "512k": "512KB", "512kb": "512KB", "lowmem": "Low Mem",
                     "fast": "Fast Mem", "slow": "Slow Mem", "chip": "Chip Mem"}
# Variant-only rules: (rule, paren words casefolded without trailing number). Beta / Pre Release / Preview
# are pre-release builds; every kind of demo is a demo; nothing in this DAT is marked prototype / bad dump.
_WHD_PAREN_RULE_OF: dict[str, str] = {
    "beta": "pre_release", "pre release": "pre_release", "preview": "pre_release",
    "game demo": "demo", "demo": "demo", "playable demo": "demo",
    "unreleased": "unreleased",
}
_WHD_PAREN_WORDS: dict[str, tuple[str, ...]] = {
    "pre_release": ("beta", "pre release", "preview"),
    "demo": ("game demo", "demo", "playable demo"),
    "unreleased": ("unreleased",),
}


def _whd_canon(flag: str) -> str:
    """Case/spelling-folded form of a game-name flag (identity + de-duplication)."""
    f = re.sub(r"\s+", " ", flag.strip().casefold())
    return _WHD_FLAG_ALIASES.get(f, f)


def _whd_memory(flag: str) -> str:
    """Canonical memory tag (``"512KB"``, ``"1MB"``, ``"Fast Mem"``...) a flag stands for, else ``""``."""
    f = flag.strip()
    low = f.casefold()
    if low in _WHD_MEMORY_WORDS:
        return _WHD_CANON_MEMORY.get(low.replace(" mem", "").replace(" ", ""), f.title())
    m = _WHD_MEMORY_RE.match(f)
    if not m:
        return ""
    compact = low.replace(" ", "")
    if compact in _WHD_CANON_MEMORY:
        return _WHD_CANON_MEMORY[compact]
    return f"{m.group(1)}{'KB' if low.endswith(('kb', 'k')) else 'MB'}"


def is_whd_memory(flag: str) -> bool:
    return bool(_whd_memory(flag))


def whd_memory_rank(t: Tags) -> int:
    """0 = no memory tag (standard), 1 = needs more memory (1MB, 2MB, Fast / Slow / Chip Mem ...),
    2 = low-memory build (512KB / 512k / Low Mem)."""
    mems = [_whd_memory(f) for f in t.flags]
    mems = [m for m in mems if m]
    if not mems:
        return 0
    return 2 if any(m.casefold() in _WHD_LOW_MEMORY for m in mems) else 1


def _whd_is_variant_flag(flag: str) -> bool:
    """Flags that describe a variant of the SAME game (chipset, NTSC, memory); everything else is part of
    the game's identity (disk layout, Image/Files, Hack, Demo kinds, publishers, CDTV, CD-ROM ...)."""
    return is_chipset(flag) or flag.strip().casefold() == "ntsc" or is_whd_memory(flag)


def _parse_whd_game(game: str) -> tuple[str, list[str], list[str], str]:
    """``(title, language codes, flags, status)`` of a game name (flags exclude languages / status)."""
    cut = _title_cut(game)
    title = game[:cut].strip()
    langs: list[str] = []
    flags: list[str] = []
    status = ""
    for kind, text in _split_groups(game[cut:]):
        if kind != "(":
            flags.append(f"[{text}]")
            continue
        code = _WHD_LANG_WORDS.get(text.casefold())
        if code:
            if code not in langs:
                langs.append(code)
            continue
        if not status and _WHD_STATUS_RE.match(text):
            status = text
            continue
        flags.append(text)
    return title, langs, flags, status


def _parse_whd_stem(stem: str) -> dict[str, Any]:
    """Version, build number, language codes and variant words of an archive stem."""
    tokens = [t for t in stem.split("_") if t]
    vi = next((i for i, t in enumerate(tokens) if i >= 1 and _WHD_VERSION_TOKEN_RE.match(t)), -1)
    out: dict[str, Any] = {"title": "", "version": "", "build": 0, "langs": [], "flags": [],
                           "status": "", "author": ""}
    if vi < 0:
        out["title"] = "_".join(tokens)
        rest = tokens[1:]
    else:
        out["title"] = "_".join(tokens[:vi])
        out["version"] = ("v" + tokens[vi][1:]).replace("-", "").lower()   # v2.1-B -> v2.1b
        rest = tokens[vi + 1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        low = tok.casefold()
        if low == "by" and i + 1 < len(rest):
            who = []
            j = i + 1
            while j < len(rest) and not _WHD_BUILD_RE.match(rest[j]) and not _WHD_VERSION_TOKEN_RE.match(rest[j]):
                who.append(rest[j])
                j += 1
            out["author"] = "_".join(who)
            out["flags"].append("Hack")      # "<something>_by_<author>" is a third-party modification
            i = j
            continue
        if _WHD_BUILD_RE.match(tok):
            out["build"] = max(out["build"], *(int(x) for x in tok.split("&")))
        elif tok in _WHD_STEM_LANGS and tok != "En":
            out["langs"].append(_WHD_STEM_LANGS[tok])
        elif tok == "En":
            out["langs"].append("En")
        elif _WHD_MULTILANG_RE.match(tok):
            out["langs"].extend(_WHD_STEM_LANGS[tok[k:k + 2]] for k in range(0, len(tok), 2))
        elif low in _WHD_LANG_WORDS:
            out["langs"].append(_WHD_LANG_WORDS[low])
        elif low in ("beta", "beta2", "beta3") or (low.startswith("beta") and low[4:].isdigit()):
            out["status"] = "Beta"
        elif low == "prerelease":
            out["status"] = out["status"] or "Pre Release"
        elif low in _WHD_CANON_MEMORY:
            out["flags"].append(_WHD_CANON_MEMORY[low])
        elif low in _WHD_STEM_FLAGS:
            out["flags"].append(_WHD_STEM_FLAGS[low])
        elif low in ("fix",):
            out["flags"].append("Fix")
        i += 1
    return out


@functools.lru_cache(maxsize=65536)
def parse_whdload(stem: str, game: str = "") -> Tags:
    """Tags of one WHDLoad database entry.

    ``stem`` = archive name without extension (``Title_v1.2_De_AGA_0417``), ``game`` = the DAT's game name
    (``1869 - Erlebte Geschichte (Teil 1) (German) (AGA)``; empty -> only the stem is parsed).
    Title / status / product flags come from the game name; version, build number and variant words
    (language codes, chipset, NTSC, memory, Hack_by_<author>) are merged in from the stem. No tag =
    English / PAL / standard memory / OCS.
    """
    stem = _EXT_RE.sub("", stem.strip()) if re.search(r"\.(?:lha|lzx)$", stem, re.IGNORECASE) else stem.strip()
    st = _parse_whd_stem(stem)
    if game:
        title, langs, flags, status = _parse_whd_game(game)
    else:
        title, langs, flags, status = st["title"], [], [], ""
    for code in st["langs"]:
        if code not in langs:
            langs.append(code)
    have = {_whd_canon(f) for f in flags}
    for f in st["flags"]:
        if _whd_canon(f) not in have and not (is_whd_memory(f) and any(is_whd_memory(x) and
                                              _whd_memory(x) == _whd_memory(f) for x in flags)):
            flags.append(f)
            have.add(_whd_canon(f))
    if st["author"]:
        flags.append("by " + st["author"])
    if not status:
        status = st["status"]
    ntsc = any(f.strip().casefold() == "ntsc" for f in flags)
    version = st["version"]
    return Tags(
        title=title, regions=(), languages=tuple(langs), languages_implied=not langs,
        version=version, version_key=version_key(version), status=status, flags=tuple(flags),
        dump_flags=(), bad=False, bios=False, video=("NTSC",) if ntsc else (), style=STYLE_WHDLOAD,
        build=st["build"])


def style_of_rom(rom: Any) -> str:
    """The tag style of a DAT rom: WHDLoad for that DAT, else No-Intro (set names) or TOSEC."""
    if getattr(rom, "dat", "") == WHDLOAD_DAT_NAME:
        return STYLE_WHDLOAD
    if getattr(rom, "dat", "") in REDUMP_DAT_NAMES:
        return STYLE_REDUMP
    return STYLE_NOINTRO if getattr(rom, "set_name", "") else STYLE_TOSEC


def of_rom(rom: Any) -> Tags:
    """Parsed tags of a DAT rom (``datfile.Rom.tags``)."""
    style = style_of_rom(rom)
    if style == STYLE_WHDLOAD:
        return parse_whdload(getattr(rom, "set_name", "") or rom.name, getattr(rom, "game", "") or "")
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
            t.status, t.flags, _clean_dump_flags(t))


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


def superseded(names: Iterable[str], style: str = STYLE_NOINTRO) -> dict[str, str]:
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
        members = [(keys[n], n) for _vk, n in members]
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
    ("serial", re.compile(r"^\d{5,}$")),
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
    # cartridge boards / mappers / hardware. Also the console-feature flags of the Game Boy, DS and Mega Drive DATs
    # ("SGB Enhanced", "GB Compatible", "NDSi Enhanced", "Rumble Version", "DS Broadcast", "Sega Channel", Mega Drive
    # Mini / 4 compilations). They stay "hardware", not "distribution": the latter is skipped when variants are grouped,
    # and "Foo (SGB Enhanced)" must remain a different game key from "Foo".
    ("hardware", re.compile(r"ROM$|^72 pin|^(?:CGB\+)?(?:SGB|GB|NDSi|DSi)(?:, (?:SGB|GB))?.*(?:Enhanced|Compatible)|"
                            r"^CGB\+SGB|Rumble|^DS Broadcast$|^Sega Channel$|^Mega Drive (?:Mini|4)|Genesis Mini|"
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
         "unreleased", "modified")
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
)
_CR_RE = re.compile(r"^cr\d*(?:\s.*)?$")
_MOD_RE = re.compile(r"^(?:t|h|tr|f|a)\d*(?:\s.*)?$")


# Redump (Sega Dreamcast): the No-Intro words plus the Japanese trial / store-demo discs. Everything the
# DAT calls a demo is also covered by its <category> (Demos, Coverdiscs); the name tokens catch a disc whose
# category is missing.
_REDUMP_DEMO_RE = re.compile(
    r"^(?:(?:Tentou(?:-you)?|Tokubetsu)\s+)?Taikenban(?:\s+(?:\d+|Disc))?$"
    r"|^Tentou-you Demo(?:nstration)?(?:\s+Movie)?$|^Trial\b", re.IGNORECASE)
_REDUMP_DEMO_TOKENS = ("(Taikenban)", "(Tentou Taikenban)", "(Tentou-you Taikenban)",
                       "(Tokubetsu Taikenban)", "(Tentou-you Demo)", "(Tentou-you Demo Movie)", "(Trial Disk)")
# <category> -> rule (Games, Applications, Multimedia, Bonus Discs, Video and Add-Ons are kept)
CATEGORY_RULES: dict[str, str] = {"Demos": "demo", "Coverdiscs": "demo", "Preproduction": "prototype"}


def _paren_rule(part: str, style: str = STYLE_TOSEC) -> Optional[str]:
    p = _TRAILING_NUM_RE.sub("", part.strip().casefold())
    if style == STYLE_WHDLOAD:       # its own vocabulary; nothing of the TOSEC / No-Intro table applies
        return _WHD_PAREN_RULE_OF.get(p)
    if style == STYLE_REDUMP and _REDUMP_DEMO_RE.match(part.strip()):
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

    A ``(a, b)`` paren group returns the first matching part's rule. ``style`` selects the vocabulary
    (WHDLoad has its own and no bracket flags)."""
    if kind == "(":
        rules = _paren_rules(text, style)
        return rules[0] if rules else None
    if style == STYLE_WHDLOAD:
        return None
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
    if t.category and CATEGORY_RULES.get(t.category):
        add(CATEGORY_RULES[t.category], f"(category: {t.category})")
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
_CHIPSET_RE = re.compile(r"^(?:OCS|ECS|AGA|CD32)(?:-(?:OCS|ECS|AGA|CD32))*$")
# Platform classes best-first. Extend here (e.g. a future CD32 phase): CD32 > AGA > OCS.
PLATFORM_ORDER: tuple[str, ...] = ("CD32", "AGA", "OCS")
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
    if t.style == STYLE_WHDLOAD:
        return tuple(f for f in t.flags if is_chipset(f))
    if t.style != STYLE_TOSEC:
        return ()
    return tuple(f for f in _edition_flags(t) if is_chipset(f))


def platform_class(t: Tags) -> str:
    """One of :data:`PLATFORM_ORDER`: the best platform the chipset tags allow (untagged -> ``OCS``)."""
    found = {part for c in chipset(t) for part in c.split("-")}
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
    if t.style == STYLE_WHDLOAD:
        return whd_identity_key(t)
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


def whd_identity_key(t: Tags) -> tuple:
    """What makes two WHDLoad entries the *same game*: the clean title, the status and every product tag
    (disk layout, Image/Files, Hack + author, Demo kinds, CDTV, CD-ROM, publishers, cover disks ...).
    Language, chipset (AGA / CD32 / OCS), NTSC, memory variant, version and build are ranking
    attributes, not identity. Titles are compared case-insensitively; different spellings stay different."""
    edition = tuple(sorted({_whd_canon(f) for f in t.flags if not _whd_is_variant_flag(f)}))
    return (t.style, t.title.casefold(), t.status.casefold(), edition)


def partition_key(t: Tags) -> tuple:
    """What must be equal for two disks to share one playlist: explicit languages + chipset tags."""
    return (() if t.languages_implied else t.languages, chipset(t))


_GAME_KEY_SKIP_KINDS = frozenset({"date", "video", "serial", "alt", "distribution", "language"})


def game_key(t: Tags) -> tuple:
    """Identity of a *game* across regions, languages, revisions and re-releases.

    No-Intro: title + status (Beta/Proto ...) + the tags that make a distinct product (licence
    ``Unl`` / ``Aftermarket`` / ``Pirate``, publishers, hardware, events, dump flags); regions, languages,
    versions, dates, ``Alt``, ``PAL``/``NTSC`` and re-release tags (``Virtual Console`` ...) are NOT in it.
    TOSEC: :func:`identity_key`."""
    if t.style in (STYLE_TOSEC, STYLE_WHDLOAD):
        return identity_key(t)
    if t.style == STYLE_REDUMP:
        return redump_game_key(t)
    flags = tuple(sorted(f.casefold() for f in t.flags if flag_kind(f) not in _GAME_KEY_SKIP_KINDS))
    return (t.style, t.title.casefold(), t.status.casefold(), flags,
            tuple(sorted(f for f in t.dump_flags if f != "!")))


_REDUMP_KEY_SKIP_KINDS = _GAME_KEY_SKIP_KINDS | {"disk"}
_DISC_RE = re.compile(r"^Dis[ck] (\d+|[A-Z])$")


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
    flags = tuple(sorted(f.casefold() for f in t.flags
                         if flag_kind(f) not in _REDUMP_KEY_SKIP_KINDS and f.casefold() != "rerelease"))
    return (t.style, t.title.casefold(), t.status.casefold(), flags,
            tuple(sorted(f for f in t.dump_flags if f != "!")))


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
    if t.style == STYLE_WHDLOAD:
        return (t.style, t.title.casefold())
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


def region_order(priority: Iterable[str] = DEFAULT_REGION_PRIORITY) -> list[str]:
    """``priority`` followed by every other known region in alphabetical order."""
    first = list(dict.fromkeys(p for p in priority if p))
    return first + sorted(r for r in REGIONS if r not in first)


def region_rank(regions: Iterable[str], order: list[str]) -> int:
    """Best (lowest) position of any of ``regions`` in ``order``; unknown regions rank after all."""
    pos = {r: i for i, r in enumerate(order)}
    ranks = [pos.get(r, len(order)) for r in regions]
    return min(ranks) if ranks else len(order)


# --------------------------------------------------------------------------- rule tokens (UI catalog)

def _display_paren(word: str, style: str) -> str:
    if style in (STYLE_NOINTRO, STYLE_REDUMP):
        return "(" + " ".join(w.capitalize() for w in word.split(" ")) + ")"
    return f"({word})"


def rule_tokens(style: str) -> dict[str, list[str]]:
    """``{rule: [exact tokens as they appear in names]}`` for ``style``, generated from the tables
    :func:`classify_token` uses. No-Intro lists only the words No-Intro uses (and the bracket flags);
    WHDLoad lists its own few paren words (``(Beta)``, ``(Game Demo)`` ...) and no bracket flags."""
    out: dict[str, list[str]] = {r: [] for r in RULES}
    if style == STYLE_REDUMP:
        for rule, words in _PAREN_WORDS.items():
            for w in words:
                if w in _NOINTRO_PAREN_WORDS and w not in ("debug", "debug version", "test program"):
                    out[rule].append(_display_paren(w, style))
        out["demo"].extend(_REDUMP_DEMO_TOKENS)
        for cat, rule in CATEGORY_RULES.items():
            out[rule].append(f"category {cat}")
        return out
    if style == STYLE_WHDLOAD:
        for rule, words in _WHD_PAREN_WORDS.items():
            out[rule].extend("(" + " ".join(w.capitalize() for w in word.split(" ")) + ")" for word in words)
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
    return out


def token_rule(token: str, style: str = STYLE_TOSEC) -> Optional[str]:
    """Classify a catalog token (``"(beta)"``, ``"[b ...]"``, ``"(demo-*)"``) with :func:`classify_token`."""
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
