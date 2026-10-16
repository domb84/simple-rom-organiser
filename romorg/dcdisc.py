"""The product number of a Dreamcast disc, read from its header.

Every Dreamcast disc starts its data with a boot header (``IP.BIN``): ``SEGA SEGAKATANA`` at byte 0, the product number at
0x40 (10 characters: ``HDR-0128``, ``T36802N``, ``MK-51000``) and the game's name at 0x80. Flycast names a game's own memory card
files after that number (``HDR-0128.A1.bin``) when it keeps one card per game, so the number is what tells which game such a card
belongs to. Read from a ``.chd`` (its first data track) or a ``.gdi`` (the data tracks it lists); "" when it cannot be read.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["read_product", "codes_of"]

_MAGIC = b"SEGA SEGAKATANA"


def _from_sector(raw: bytes) -> str:
    for lead in (16, 0, 24):                              # (a raw 2352-byte sector: the data after the sync and header; a cooked one)
        if raw[lead:lead + len(_MAGIC)] == _MAGIC:
            return raw[lead + 0x40:lead + 0x4A].decode("ascii", errors="replace").strip()
    return ""


def read_product(path: Path) -> str:
    """The product number of a Dreamcast disc image ("" when it cannot be read)."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".chd":
            from . import chd as chdlib
            info = chdlib.Chd(path, load_map=False)
            try:
                for track in info.tracks:
                    if not track.is_audio:
                        code = _from_sector(info.read_track_range(track, 0, 1))
                        if code:
                            return code
            finally:
                info.close()
        elif path.suffix.lower() == ".gdi":
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 5 and parts[2] == "4":                       # (a data track: its file is the 5th field)
                    name = " ".join(parts[4:-1]).strip('"') if len(parts) > 5 else parts[4]
                    f = path.parent / name
                    if f.is_file():
                        with open(f, "rb") as fh:
                            code = _from_sector(fh.read(2352))
                        if code:
                            return code
    except Exception:  # noqa: BLE001 - an unreadable disc is a disc without a number
        return ""
    return ""


def codes_of(product: str) -> set[str]:
    """The forms a product number is written in on a save's name: as it is, without spaces, and its first word (``T40509D 50``)."""
    p = product.strip().upper()
    return {c for c in (p, p.replace(" ", ""), p.split(" ")[0] if p else "") if c}
