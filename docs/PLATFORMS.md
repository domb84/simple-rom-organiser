# Systems supported: what each one reads, how it is matched, what it can do

*v0.2, this branch. Every system has Overview, Library and Browse. "Library" = rules, preview, build, undo.*

**Matched** says how a file is told. *Checksum* = the file's hash is looked up in the DAT. *ID* = no checksum can be had, so the game's ID
in the file is looked up in a catalogue; such a match is **identified**, not **verified**.

| System | Files | Database | Matched | Rules that apply (from the tags in the names) | Saves from | Library build |
|---|---|---|---|---|---|---|
| Atari Lynx | `.lnx .lyx .bll` (+ zip, 7z) | No-Intro | Checksum (CRC32, SHA-1; the zip's members) | region, language, latest | RetroArch | rename to the DAT name, rules, archive |
| Nintendo Entertainment System | `.nes .unh` (+ zip, 7z) | No-Intro | Checksum, also without the 16-byte header | region, language, latest | RetroArch | same |
| Super Nintendo | `.sfc .smc .swc .fig` (+ zip, 7z) | No-Intro | Checksum, also without the copier header | region, language, latest | RetroArch | same; convert to the No-Intro form |
| Nintendo 64 | `.z64 .v64 .n64` (+ zip, 7z) | No-Intro | Checksum, byte-swapped images too | region, language, latest | RetroArch | same; convert byte order |
| Game Boy, Color, Advance; DS | `.gb .gbc .gba .nds .dsi` (+ zip, 7z) | No-Intro | Checksum | region, language, latest | RetroArch | same |
| Master System, Game Gear, Mega Drive, 32X | `.sms .gg .md .gen .bin .32x` (+ zip, 7z) | No-Intro | Checksum | region, language, latest | RetroArch | same |
| Commodore Amiga | `.adf` (+ zip, 7z) | TOSEC (4 DATs) | Checksum | language, latest, best variant (no region) | RetroArch | same + `.m3u` playlists for multi-disk games |
| Amiga - WHDLoad | `.lha .lzx` | WHDLoad | Checksum of the archive | language, latest, best variant | RetroArch | same |
| Sega Dreamcast | `.chd .gdi .cue .bin .raw` | Redump (1,516 discs) | Checksum of every track (CHD read in place) | region, language, latest | RetroArch | folder per game, `.m3u`, convert to CHD |
| Sony PlayStation | `.chd .cue .bin` | Redump (10,914) | Checksum of every track | region, language, latest | RetroArch | folder per game, `.m3u`, convert to CHD |
| Sony PlayStation 2 | `.chd .iso .cue .bin` | Redump (11,774) | Checksum of the ISO / every track | region, language, latest | **PCSX2** (serial) | folder per game, no playlists, convert to CHD |
| Nintendo GameCube | `.iso .gcm .rvz` | Redump's list via libretro (1,981 games) | Checksum; a `.rvz` is rebuilt in memory and hashed as the ISO it stands for | region, language, latest | **Dolphin** (game code) | rename to the DAT name, rules, archive |
| Nintendo Wii | `.iso .rvz .wia .wbfs .ciso` | Redump's list via libretro (3,637 games, with serials) | Plain `.iso`: checksum. Compressed formats: **ID** (the game code in the header is in the disc's serial; then revision and disc) | region, language, latest | **Dolphin** (game code) | same as GameCube |
| Nintendo Wii U | `.wux .wud` | Made from GameTDB's `wiiutdb.xml` (Redump has none): its 457 disc games, named as No-Intro names them where it knows the title (`Legend of Zelda, The - Twilight Princess HD (USA) (En,Fr,Es)`), else by GameTDB; eShop and Virtual Console titles are not discs and are left out | **ID** (product code in the first sector) | region, language | **Cemu** (title ID, via Cemu's own game list) | same as GameCube |
| Nintendo Switch | `.nsp .nsz .xci .xcz` (games, updates, add-ons in one folder) | Three catalogues made from titledb: **Nintendo - Switch** (24k games: region, languages, demo flag), **(Updates)** (38k, one per version), **(DLC)** (18k). No checksums exist | **ID** (title ID inside the file, plus the version for an update). Optional check of every NCA against its own name | region + language (games, updates); demo; **latest update only** (updates) | **Eden / Ryujinx** (title ID) | same as the others: rename to the catalogue name, rules, archive |

## Where things come from

* **Rules** (region, language, latest, variant) read the tags in the names: `(USA)`, `(En,Fr)`, `(Rev 2)`, `(v1.1)`, TOSEC flags. The Wii U
  catalogue has region and language tags (GameTDB); the Switch catalogues have region, languages, demo and version tags (titledb).
* **Exclusion flags** (demo, beta, prototype, bad dump, pirate ...) apply wherever the names or DAT categories carry them.
* **Completion figures** count the DAT's entries. The Switch has one figure per catalogue (games / updates / DLC).
* **Saves**: only **RetroArch's** are named after the ROM, so only they are renamed with it. The other emulators tell a game by an ID; a
  game that has saves is kept when the rules would archive it, or its saves go to the archive with it (`_saves`). Memory-card images and
  Ryujinx's save folders are counted but never moved. A system with no emulator set up (Emulators page) shows no saves at all.
* **Verification** (is the file intact?): cartridge systems, Amiga, Dreamcast, PlayStation, PS2, GameCube and plain Wii `.iso` verify by
  the checksum match itself; the Switch has the NCA check; Wii compressed images and the Wii U cannot be verified.

## Redump, and libretro's copy of it

Dreamcast, PlayStation and PS2 use Redump's own downloads: they are verified track by track, and libretro's copy keeps only a few tracks
per disc. The **GameCube** and the **Wii** use libretro's mirror of Redump (`metadat/redump`): it has every checksum Redump has for the
games you keep, the discs' serials (which find a compressed Wii disc by its game code), is newer and comes over https. It leaves out
betas, prototypes and demos (1,742 discs over the five systems, one retail title among them), which the default rules exclude anyway.

## BIOS, firmware and key files

A file that matches no game may be something an emulator needs. It is told by its **checksum** against the list libretro's cores use
(`System.dat`, 514 files of 60 systems, bundled with the app), whatever it is called and whichever system's folder it is in. Such a file
is treated like a ROM: it stays with the ROMs, a Library build gives it the name the emulators expect (`scph1001.bin`), and it never goes
to `_unmatched` or the archive (Collection leaves it where it is). The key files of the Switch and Wii U emulators (`prod.keys`,
`title.keys`, `keys.txt`) have no common checksum; they are told by name and left as they are. A file that only *has* a BIOS's name is
an ordinary unmatched file.

## Gaps in parity (what is not the same, and why)

1. **Wii (compressed images), Wii U and Switch** are matched by ID, not checksum: no checksum DAT exists, and a Wii image in RVZ/WIA/WBFS
   or a `.wux` cannot be turned back into the original without rebuilding the whole disc.
2. **Convert**: SNES, N64 and the disc systems that use CHD; none for the others.
3. **Playlists** (`.m3u`): Amiga, Dreamcast and PlayStation. GameCube, Wii and PS2 have multi-disc games too, but their emulators here
   (Dolphin, PCSX2) do not read `.m3u`.
4. A **Switch update** is matched by title ID *and version*: one whose version titledb does not list is left unmatched, and so is a title
   none of titledb's stores (US, GB, JP) lists, such as a Korea-only game. Nothing of your own files was affected (all 8 matched).
