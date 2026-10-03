"""Sony PlayStation and PlayStation 2 (Redump DATs): configuration of the shared disc-system engine.

Both systems are built exactly like the Dreamcast (:mod:`romorg.discsys`): Redump DAT + CHD files, one folder per
game. What differs:

* **PlayStation** (``Sony - PlayStation``): CD images, every game is a ``.cue`` + one ``(Track N).bin`` per track
  (a single-track disc: ``<name>.bin``); multi-disc games get an ``.m3u`` playlist (RetroArch, DuckStation).
* **PlayStation 2** (``Sony - PlayStation 2``): DVD games are ONE ``.iso`` (CD games: cue + bin). The CHDs of
  ``chdman createcd`` keep an ISO as a ``MODE1`` track of 2048 bytes per frame; ``chdman createdvd`` CHDs carry
  the ISO's SHA-1 in their header. No playlists (PCSX2 does not use ``.m3u``). A single ``.iso`` is converted with
  ``createdvd`` (config key ``ps2_iso_chd`` = ``dvd`` | ``cd`` overrides); cue/bin sets with ``createcd``.
"""

from __future__ import annotations

from .discsys import DiscSystem, register

PSX_DAT = "Sony - PlayStation"
PSX_PLATFORM = "Sony PlayStation"
PS2_DAT = "Sony - PlayStation 2"
PS2_PLATFORM = "Sony PlayStation 2"

PSX = register(DiscSystem(key="psx", platform=PSX_PLATFORM, dat_name=PSX_DAT, label="PlayStation",
                          gd=False, iso=False, playlists=True))
PS2 = register(DiscSystem(key="ps2", platform=PS2_PLATFORM, dat_name=PS2_DAT, label="PlayStation 2",
                          gd=False, iso=True, playlists=False, iso_convert="createdvd",
                          iso_convert_key="ps2_iso_chd"))
