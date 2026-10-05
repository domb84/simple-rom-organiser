"""Supported platforms: which DATs (TOSEC or No-Intro) make up a platform folder.

This is the single place to extend when another system is added.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:
    from .datfile import DatFile
    from .tosec import DatInfo


class DatSource(str, Enum):
    TOSEC = "tosec"
    NOINTRO = "nointro"
    WHDLOAD = "whdload"      # MrV2K's WHDLoad database (its own source, folder and DAT)
    REDUMP = "redump"        # redump.org Logiqx DATs (Dreamcast, PlayStation, PlayStation 2), HTTP only, own folder


LAYOUT_PER_DAT = "per_dat"   # one folder per DAT under the platform root
LAYOUT_FLAT = "flat"         # everything directly in the platform root
LAYOUT_GAME_FOLDER = "game_folder"   # one folder per game: <root>/<Redump name>/<Redump name>.chd + sidecars

ALT_SNES_HEADER = "snes_header"
ALT_N64_BYTEORDER = "n64_byteorder"
ALT_NES_HEADER = "nes_header"


@dataclass(frozen=True)
class Platform:
    name: str                      # "Commodore Amiga"
    dats: tuple[str, ...]          # DAT names (part before " (TOSEC-v"), in PRIORITY order
    m3u_dats: tuple[str, ...]      # DATs whose multi-disk sets get M3Us
    kickstart_dat: Optional[str]   # DAT whose files can be installed as PUAE kickstarts
    source: str = DatSource.TOSEC.value   # where its DATs come from ("tosec" | "nointro")
    layout: str = LAYOUT_PER_DAT          # "per_dat" | "flat"
    extensions: tuple[str, ...] = ()      # hints (UI + 7z member eligibility), lower case with dot
    alt_hashes: tuple[str, ...] = ()      # alternate-hash strategies for scanner.scan
    convertible: bool = False             # offers "Convert to No-Intro format"
    folder_hint: str = ""                 # usual folder name (EmuDeck/RetroDECK)
    latest_dats: tuple[str, ...] = ()        # DATs where "latest versions only" applies
    best_variant_dats: tuple[str, ...] = ()  # DATs where "one best variant per game" applies
    language_dats: tuple[str, ...] = ()      # DATs where the language filter applies (Games, No-Intro)
    region_dats: tuple[str, ...] = ()        # DATs where region priority / "one per game" applies
    # Own Kickstart step WITHOUT a DAT: the ROMs are taken from this sub-folder of the platform folder
    # (matched by md5 against kickstart.PUAE_BIOS). Empty = none ("kickstart_dat" may still be set).
    kickstart_folder: str = ""
    # Top-level folders of the platform folder the app never scans, moves or sets aside.
    protected_dirs: tuple[str, ...] = ()


def _nointro(name: str, dat: str, extensions: tuple[str, ...], alt_hashes: tuple[str, ...],
             convertible: bool, folder_hint: str) -> Platform:
    return Platform(name=name, dats=(dat,), m3u_dats=(), kickstart_dat=None,
                    source=DatSource.NOINTRO.value, layout=LAYOUT_FLAT, extensions=extensions,
                    alt_hashes=alt_hashes, convertible=convertible, folder_hint=folder_hint,
                    latest_dats=(dat,), language_dats=(dat,), region_dats=(dat,))


WHDLOAD_DAT = "Commodore - Amiga - WHDLoad"   # == whdload.DAT_NAME == tags.WHDLOAD_DAT_NAME
WHDLOAD_PLATFORM = "Commodore Amiga - WHDLoad"

PLATFORMS: dict[str, Platform] = {
    "Commodore Amiga": Platform(
        name="Commodore Amiga",
        dats=(
            "Commodore Amiga - Games - [ADF]",
            "Commodore Amiga - Operating Systems - Workbench",
            "Commodore Amiga - Kickstart-Disks",
            "Commodore Amiga - Firmware",
        ),
        m3u_dats=(
            "Commodore Amiga - Games - [ADF]",
            "Commodore Amiga - Operating Systems - Workbench",
            "Commodore Amiga - Kickstart-Disks",
        ),
        kickstart_dat="Commodore Amiga - Firmware",
        extensions=(".adf", ".zip", ".7z"),
        folder_hint="amiga",
        latest_dats=("Commodore Amiga - Games - [ADF]",),
        best_variant_dats=("Commodore Amiga - Games - [ADF]",),
        language_dats=("Commodore Amiga - Games - [ADF]",),
    ),
    # A SEPARATE system from "Commodore Amiga" (TOSEC ADF): own folder, own DAT source, own config keys,
    # own library profile and Kickstart step; nothing is shared or cross-matched with it.
    WHDLOAD_PLATFORM: Platform(
        name=WHDLOAD_PLATFORM,
        dats=(WHDLOAD_DAT,),
        m3u_dats=(),
        kickstart_dat=None,
        source=DatSource.WHDLOAD.value,
        layout=LAYOUT_FLAT,
        extensions=(".lha", ".lzx"),
        folder_hint="whdload",
        latest_dats=(WHDLOAD_DAT,),
        best_variant_dats=(WHDLOAD_DAT,),
        language_dats=(WHDLOAD_DAT,),
        kickstart_folder="Kickstarts",
        protected_dirs=("Kickstarts",),
    ),
    "Nintendo Game Boy Advance": _nointro(
        "Nintendo Game Boy Advance", "Nintendo - Game Boy Advance",
        (".gba", ".zip", ".7z"), (), False, "gba"),
    "Nintendo 64": _nointro(
        "Nintendo 64", "Nintendo - Nintendo 64",
        (".z64", ".v64", ".n64", ".zip", ".7z"), (ALT_N64_BYTEORDER,), True, "n64"),
    "Nintendo Entertainment System": _nointro(
        "Nintendo Entertainment System", "Nintendo - Nintendo Entertainment System",
        (".nes", ".unh", ".zip", ".7z"), (ALT_NES_HEADER,), False, "nes"),
    "Super Nintendo Entertainment System": _nointro(
        "Super Nintendo Entertainment System", "Nintendo - Super Nintendo Entertainment System",
        (".sfc", ".smc", ".swc", ".fig", ".zip", ".7z"), (ALT_SNES_HEADER,), True, "snes"),
}

REDUMP_DC_DAT = "Sega - Dreamcast"           # == redump.DAT_NAME
DREAMCAST_PLATFORM = "Sega Dreamcast"

PLATFORMS[DREAMCAST_PLATFORM] = Platform(
    name=DREAMCAST_PLATFORM,
    dats=(REDUMP_DC_DAT,),
    m3u_dats=(),                              # (the stand-alone M3U step is for TOSEC; Build library writes DC playlists)
    kickstart_dat=None,
    source=DatSource.REDUMP.value,
    layout=LAYOUT_GAME_FOLDER,
    extensions=(".chd", ".gdi", ".cue", ".bin", ".raw"),
    convertible=True,                         # raw Redump sets -> CHD (the built-in writer; chdman optional)
    folder_hint="dreamcast",
    latest_dats=(REDUMP_DC_DAT,),
    language_dats=(REDUMP_DC_DAT,),
    region_dats=(REDUMP_DC_DAT,),
)

REDUMP_PSX_DAT = "Sony - PlayStation"
PSX_PLATFORM = "Sony PlayStation"
REDUMP_PS2_DAT = "Sony - PlayStation 2"
PS2_PLATFORM = "Sony PlayStation 2"

# The Sony systems are built exactly like the Dreamcast (own folder, own Redump DAT, own config keys and library
# profile, folder per game); nothing is shared or cross-matched between them (romorg.discsys / romorg.playstation).
PLATFORMS[PSX_PLATFORM] = Platform(
    name=PSX_PLATFORM,
    dats=(REDUMP_PSX_DAT,),
    m3u_dats=(),
    kickstart_dat=None,
    source=DatSource.REDUMP.value,
    layout=LAYOUT_GAME_FOLDER,
    extensions=(".chd", ".cue", ".bin"),
    convertible=True,
    folder_hint="psx",
    latest_dats=(REDUMP_PSX_DAT,),
    language_dats=(REDUMP_PSX_DAT,),
    region_dats=(REDUMP_PSX_DAT,),
)
PLATFORMS[PS2_PLATFORM] = Platform(
    name=PS2_PLATFORM,
    dats=(REDUMP_PS2_DAT,),
    m3u_dats=(),
    kickstart_dat=None,
    source=DatSource.REDUMP.value,
    layout=LAYOUT_GAME_FOLDER,
    extensions=(".chd", ".iso", ".cue", ".bin"),
    convertible=True,
    folder_hint="ps2",
    latest_dats=(REDUMP_PS2_DAT,),
    language_dats=(REDUMP_PS2_DAT,),
    region_dats=(REDUMP_PS2_DAT,),
)

DEFAULT_PLATFORM = "Commodore Amiga"


def get_platform(name: str) -> Platform:
    """Platform by exact name; raises ``KeyError`` for unknown names."""
    try:
        return PLATFORMS[name]
    except KeyError:
        raise KeyError(f"unknown platform: {name!r}") from None


def list_platforms() -> list[Platform]:
    return sorted(PLATFORMS.values(), key=lambda p: p.name.lower())


def all_dat_names() -> set[str]:
    """Every DAT name used by any platform (used to protect DAT folders)."""
    return {d for p in PLATFORMS.values() for d in p.dats}


def has_kickstart(platform: Any) -> bool:
    """True when the platform has a Kickstart step (TOSEC firmware DAT or its own Kickstart folder)."""
    return bool(getattr(platform, "kickstart_dat", None) or getattr(platform, "kickstart_folder", ""))


def source_of(platform: Platform) -> str:
    """The DAT source of a platform as a plain string ("tosec" | "nointro" | "whdload")."""
    src = platform.source
    return src.value if isinstance(src, DatSource) else str(src)


_source = source_of


def locate_dats(platform: Platform, directory: Optional[Union[str, Path]] = None,
                nointro_directory: Optional[Union[str, Path]] = None,
                whdload_directory: Optional[Union[str, Path]] = None,
                redump_directory: Optional[Union[str, Path]] = None) -> dict[str, "DatInfo"]:
    """``{dat name: DatInfo}`` for the platform's DATs present locally (exact names)."""
    if _source(platform) == DatSource.REDUMP.value:
        from . import redump
        local = {d.name: d for d in redump.list_dats(redump_directory)}
    elif _source(platform) == DatSource.NOINTRO.value:
        from . import nointro
        local = {d.name: d for d in nointro.list_dats(nointro_directory)}
    elif _source(platform) == DatSource.WHDLOAD.value:
        from . import whdload
        local = {d.name: d for d in whdload.list_dats(whdload_directory)}
    else:
        from . import tosec
        local = {d.name: d for d in tosec.list_dats(directory)}
    return {name: local[name] for name in platform.dats if name in local}


def platform_dat_status(platform: Platform,
                        directory: Optional[Union[str, Path]] = None,
                        nointro_directory: Optional[Union[str, Path]] = None) -> list[dict[str, Any]]:
    """``[{name, version|None, present, file|None, source}]`` per DAT of ``platform`` (priority order)."""
    local = locate_dats(platform, directory, nointro_directory)
    source = _source(platform)
    out: list[dict[str, Any]] = []
    for name in platform.dats:
        info = local.get(name)
        out.append({
            "name": name,
            "version": info.version if info else None,
            "present": info is not None,
            "file": info.path.name if info else None,
            "source": source,
        })
    return out


def load_platform_dats(platform: Platform,
                       directory: Optional[Union[str, Path]] = None,
                       nointro_directory: Optional[Union[str, Path]] = None,
                       ) -> tuple[list["DatFile"], list[str]]:
    """Parse the newest local copy of each of the platform's DATs.

    Returns ``(loaded DatFiles in priority order, names of DATs not found locally)``.
    Names are matched exactly, so e.g. "... - Operating Systems - AMIX" is never picked up.
    No-Intro DATs get ``Rom.set_name`` (alternates such as .nes/.unh count as one game).
    """
    from .datfile import parse_dat, parse_redump

    local = locate_dats(platform, directory, nointro_directory)
    nointro_src = _source(platform) in (DatSource.NOINTRO.value, DatSource.WHDLOAD.value)   # set names
    loaded: list["DatFile"] = []
    missing: list[str] = []
    for name in platform.dats:
        info = local.get(name)
        if info is None:
            missing.append(name)
            continue
        if _source(platform) == DatSource.REDUMP.value:
            dat = parse_redump(info.path)       # a game = a disc: every rom of it shares set_name = game
        else:
            dat = parse_dat(info.path, set_names=True) if nointro_src else parse_dat(info.path)
        if dat.name != name:
            # Header names normally equal the filename part; keep the platform's name canonical.
            dat.name = name
            dat.roms = [replace(r, dat=name) if r.dat != name else r for r in dat.roms]
        loaded.append(dat)
    return loaded, missing
