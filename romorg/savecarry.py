"""Which saves still work in another release of the same game.

A cartridge's own memory (battery RAM, EEPROM, flash) holds the same data whatever the region or revision, so the save of one release
works in all of them. A memory card holds files named after the disc's product code, which carries the region: a game sees only the
saves of its own region. The same holds for the other disc consoles here. Only save games are looked at: a save state is made by one
emulator for one exact ROM and never carries over.

A console that is not listed (computers, the Switch and Wii U, whose saves are told by a game ID rather than a ROM's name) carries
nothing: its saves are not named after the ROM, so there is nothing to copy.
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple

__all__ = ["ANY_RELEASE", "SAME_REGION", "CONVERTIBLE", "CONVERTIBLE_DATS", "works", "explain", "region_letter", "convert_ps1_card"]

ANY_RELEASE = frozenset({
    "Nintendo Game Boy", "Nintendo Game Boy Color", "Nintendo Game Boy Advance", "Nintendo DS", "Nintendo Entertainment System",
    "Super Nintendo Entertainment System", "Nintendo 64", "Sega Master System", "Sega Game Gear", "Sega Mega Drive - Genesis",
    "Sega 32X", "Atari Lynx"})
SAME_REGION = frozenset({"Sony PlayStation", "Sony PlayStation 2", "Sega Dreamcast", "Nintendo GameCube", "Nintendo Wii"})


def works(platform: str, old_regions: Iterable[str], new_regions: Iterable[str]) -> bool:
    """True when a save of a release of ``old_regions`` works in a release of ``new_regions`` (same game) on this system."""
    if platform in ANY_RELEASE:
        return True
    if platform in SAME_REGION:
        return bool(set(old_regions) & set(new_regions))
    return False


def explain(platform: str) -> str:
    """One line for the screen: what this system's saves do across releases."""
    if platform in ANY_RELEASE:
        return "its saves work in every release of a game"
    if platform in SAME_REGION:
        return "its saves work in releases of the same region only"
    return "its saves cannot be copied to another release"


# --------------------------------------------------------------------------- converting a PlayStation card to another region
# A game finds its saves on a card by the name in the card's directory: ``B`` + a region letter (A America, E Europe, I Japan) + the disc's
# product code (``SLUS-00520``) + a tail the game chooses. The other region's disc has another code, so its game does not see the save.
# Rewriting that one name (and the directory frame's checksum) lets the other release find it. Only the PlayStation is done: the
# saves of the other systems are not named after the disc in this way.
CONVERTIBLE = frozenset({"Sony PlayStation", "Sony PlayStation 2"})
SERIAL_SAVES = frozenset({"Sony PlayStation 2"})         # (PCSX2 names a save by the game's code, so every release with the code finds it; a PlayStation
                                                          #  card file is named after the game's file, so a new release needs the card copied)
CONVERTIBLE_DATS = frozenset({"Sony - PlayStation", "Sony - PlayStation 2"})        # (the same systems, by the name of their database)
_CARD = 131072
_HEADERS = (0, 3904, 128)               # bytes before the card in a DexDrive (.gme) or PSP (.vmp) file
_REGION = {"U": "A", "E": "E", "P": "I"}


def region_letter(serial: str) -> str:
    """``A`` / ``E`` / ``I`` for a PlayStation product code (``SLUS-00520``, ``SCES-00967``, ``SLPS-00700``); "" for one that says nothing."""
    s = serial.strip().upper()
    return _REGION.get(s[2], "") if len(s) >= 10 and s[4] == "-" else ""


def convert_ps1_card(data: bytes, old: str, new: str) -> Optional[Tuple[bytes, int]]:
    """``(the card with its saves of the game ``old`` renamed for the game ``new``, how many saves)``; None when ``data`` is not a PlayStation
    card, ``new`` says no region, or the card has no save of ``old``. Nothing else on the card changes."""
    extra = len(data) - _CARD
    if extra not in _HEADERS or data[extra:extra + 2] != b"MC":
        return None
    letter = region_letter(new)
    old, new = old.strip().upper(), new.strip().upper()
    if not letter or len(new) != 10 or len(old) != 10:
        return None
    out = bytearray(data)
    hits = 0
    for i in range(1, 16):
        at = extra + i * 128
        frame = out[at:at + 128]
        if frame[0] != 0x51:                                     # (the first block of a save; a link block carries no name)
            continue
        name = bytes(frame[10:30])
        if name[2:12].decode("ascii", errors="replace").upper() != old:
            continue
        out[at + 10:at + 30] = (b"B" + letter.encode() + new.encode() + name[12:]).ljust(20, b"\0")[:20]
        check = 0
        for b in out[at:at + 127]:
            check ^= b
        out[at + 127] = check
        hits += 1
    return (bytes(out), hits) if hits else None
