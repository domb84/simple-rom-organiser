"""Sega Dreamcast (Redump "Sega - Dreamcast"): configuration of the shared disc-system engine.

All behaviour lives in :mod:`romorg.discsys` (identification of CHDs, raw sets, tidy, Build library, Convert,
Verify fully); this module only declares the Dreamcast (GD-ROM images, ``.gdi`` raw sets, playlists for
multi-disc games) and re-exports the engine's names, so ``dreamcast.scan(...)`` and friends keep working.
"""

from __future__ import annotations

from . import chd as chdlib  # noqa: F401 - re-exported (tests patch ``dreamcast.chdlib``)
from . import chdpool, chdtool, scanner, tempspace  # noqa: F401
from .discsys import *  # noqa: F401,F403
from .discsys import DiscSystem, register

DAT_NAME = "Sega - Dreamcast"
PLATFORM_NAME = "Sega Dreamcast"

SYSTEM = register(DiscSystem(key="dreamcast", platform=PLATFORM_NAME, dat_name=DAT_NAME, label="Dreamcast",
                             gd=True, iso=False, playlists=True))
