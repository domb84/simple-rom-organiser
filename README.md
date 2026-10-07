# Simple ROM Organiser

A small local web app that checks ROM folders against DAT files and tidies them up.
Supported systems:

| System | DATs | Source |
| --- | --- | --- |
| Commodore Amiga | 4 [TOSEC](https://www.tosecdev.org/) DATs (Games ADF, Workbench, Kickstart-Disks, Firmware) | TOSEC pack from tosecdev.org |
| Commodore Amiga - WHDLoad | *Commodore - Amiga - WHDLoad* (4121 Retroplay `.lha` archives) | MrV2K's [WHDLoad-Database](https://github.com/MrV2K/WHDLoad-Database) on GitHub |
| Nintendo Game Boy Advance | *Nintendo - Game Boy Advance* | No-Intro, via libretro-database |
| Nintendo 64 | *Nintendo - Nintendo 64* | No-Intro, via libretro-database |
| Nintendo Entertainment System | *Nintendo - Nintendo Entertainment System* | No-Intro, via libretro-database |
| Super Nintendo Entertainment System | *Nintendo - Super Nintendo Entertainment System* | No-Intro, via libretro-database |
| Nintendo Game Boy, Game Boy Color, Nintendo DS | *Nintendo - Game Boy*, *Nintendo - Game Boy Color*, *Nintendo - Nintendo DS* | No-Intro, via libretro-database |
| Sega Mega Drive - Genesis, Master System, Game Gear, 32X | *Sega - Mega Drive - Genesis*, *Sega - Master System - Mark III*, *Sega - Game Gear*, *Sega - 32X* | No-Intro, via libretro-database |
| Atari Lynx | *Atari - Lynx* (the DAT lists the headered `.lnx` and the raw `.lyx` / `.bll` dumps as separate roms of one game: both match) | No-Intro, via libretro-database |
| Sega Dreamcast | *Sega - Dreamcast* (1516 discs, Redump) | [redump.org](http://redump.org/) (plain HTTP only) |
| Sony PlayStation | *Sony - PlayStation* (10,914 discs, Redump) | [redump.org](http://redump.org/) (plain HTTP only) |
| Sony PlayStation 2 | *Sony - PlayStation 2* (11,774 discs, Redump) | [redump.org](http://redump.org/) (plain HTTP only) |
| Nintendo GameCube | *Nintendo - GameCube* (2,019 discs, Redump); files are `.iso`, `.gcm` or Dolphin `.rvz` | [redump.org](http://redump.org/) (plain HTTP only) |

Every system has its own folder (e.g. `.../roms/amiga`, `.../roms/snes`). The app matches
every file against the system's DATs, shows what you have and what is missing, renames
and sorts the files, and - for the Amiga - writes M3U playlists for multi-disk games and
installs Kickstart ROMs for RetroArch.

**Commodore Amiga - WHDLoad is a separate system from Commodore Amiga (TOSEC).** It has its own
row, folder, DAT, library rules, Kickstart step and config keys; nothing is shared or
cross-matched with the TOSEC ADF system. Each DAT entry is one pre-installed Retroplay `.lha`
archive; files are matched by the hash (sha1, crc + size) of the **whole `.lha` file** - the
archives are never opened. Organising renames them to the database name
(`1000ccTurbo_v1.0.lha`), flat in the system folder; files that match nothing go to `_unmatched/`.
Its library rules: exclude *Beta / Pre Release / Preview*, *Game Demo / Demo* and *Unreleased*
(the exact tokens are listed in the panel), keep your languages (no tag = English, `(German)` /
`_De` = German, `EnFrDe` = three languages) and keep **one best archive per game**: your language
order, then CD32 > AGA > OCS, then standard memory over `512KB` / `Low Mem` builds, then PAL /
untagged over NTSC, then the newest version (highest build number last). Different products
(`Two Disk` / `One Disk` installs, `Image` / `Files`, demos, cover disks, hacks, CDTV, CD-ROM) are
never merged. No dump-flag, multi-disk or playlist options exist for it.

**Sega Dreamcast, Sony PlayStation and Sony PlayStation 2 (Redump)** are the *folder per game* systems (one shared engine): `<system folder>/<Redump name>/<Redump name>.chd`
plus the sidecar files next to it (see [Sega Dreamcast: CHD + Redump](#sega-dreamcast-chd--redump) and
[Sony PlayStation / PlayStation 2](#sony-playstation--playstation-2-chd--redump)). CHD files are
matched per track against the Redump hashes by a built-in pure-Python CHD reader (or by `chdman` when you
have it), raw Redump sets (`.gdi` / `.cue` + track files) are recognised too and can be converted to CHD.

What it does:

1. Keeps the DATs current **automatically**. At every start it checks, in the background,
   the newest TOSEC release (<https://www.tosecdev.org/downloads>, ~100 MB, Amiga; downloaded
   only when its release date differs from the installed one), the twelve No-Intro DATs in the
   [libretro-database](https://github.com/libretro/libretro-database/tree/master/metadat/no-intro)
   mirror on GitHub (a few MB each, consoles; only changed files are fetched) and the Redump
   Dreamcast / PlayStation / PlayStation 2 / GameCube DATs (one HEAD request per DAT; the zips only when their date is newer). Nothing to download by hand.
2. **Systems & folders**: one row per system with its DAT status and folder (native dialog,
   or the built-in folder browser with shortcuts for Home and SD cards / USB drives under
   `/run/media`). Each folder is remembered.
3. Scans a folder (always including subfolders) and shows how many games / DAT entries you
   have, which are missing and which files matched nothing - with region, language and tag
   chips and filters.
4. **Build library** (one preview, one apply, one undo): files are renamed and sorted - Amiga
   files into one folder per DAT under their exact TOSEC name, console files under their exact
   No-Intro name, flat in the console folder. Following the **library rules** (each can be
   switched off; the panel shows the exact TOSEC / No-Intro tags behind every rule and how
   many files it moves): bad dumps, betas, demos and other unwanted variants
   are set aside, only the languages you tick are kept (English by default), only the latest
   version (and for Amiga games the one best variant, for consoles one version per game in
   your region order) is kept,
   incomplete multi-disk games and duplicate copies are set aside, and playlists are written
   for the complete multi-disk sets. Everything set aside goes to its own folder (`_excluded/`, `_superseded/`, `_incomplete/`,
   `_duplicates/`, next to the games) - nothing is ever deleted. The old "Organise" and "M3U" steps live on under *Advanced*.
5. Optionally converts copier-headered SNES dumps and byte-swapped N64 dumps to the clean
   No-Intro format (originals kept).
6. Amiga: copies your Kickstart ROMs into RetroArch's system / BIOS folder for the PUAE core.

It is pure Python (standard library only, Python 3.11+) with a plain HTML/JS
interface - no pip, no Node, no internet access needed except for updating the DATs (offline, the installed DATs keep working).
That makes it suitable for SteamOS, whose root filesystem is read-only.

## Folder layout

Choose the folder that holds *everything* for one system, e.g.
`/run/media/deck/SD/roms/amiga` or `/run/media/deck/SD/roms/snes`.

### Consoles (No-Intro): flat

```text
snes/
  Super Mario World (USA).sfc                        loose file: exact No-Intro rom name
  Donkey Kong Country (USA) (Rev 2).zip              archive: named after the game
  Zelda no Densetsu (Japan).smc                      matched with its copier header: stays .smc
  media/                                             frontend media folders are left alone
  _unmatched/                                        only files that matched nothing
  _excluded/                                         bad dumps, betas, demos, language/flag exclusions
  _superseded/                                       older versions / worse variants
  _duplicates/                                       extra copies of the same ROM
  _converted_originals/                              originals kept by "Convert"
  .romorg-undo-<timestamp>.json                      undo logs (hidden)
```

- Matched files anywhere under the console folder (subfolders, `_unmatched/`, old folders)
  are moved up into the console folder itself under their exact No-Intro name - except files
  that still qualify for a reason folder (`_excluded`, `_superseded`, `_incomplete`,
  `_duplicates`), which stay there.
- Organise **never changes file contents**, so the extension stays honest: a SNES file
  matched with its 512-byte copier header skipped is named `<game>.smc`, a byte-swapped
  N64 file `<game>.v64` / `<game>.n64`, a NES file whose iNES header differs `<game>.nes`.
  Use **Convert** to get the exact DAT files.
- `.zip` / `.7z` archives with one matching game are named `<game>.zip` / `<game>.7z`
  (the file inside is unchanged).
- Unmatched files keep their relative path under `_unmatched/`. Folders named `media`,
  `images`, `videos`, `manuals`, `downloaded_images`, `snap` or `boxart` are left in place.

### Reserved folders

The app owns exactly these folder names, all directly under the platform folder (compared
case-insensitively, since SD cards are often exFAT): `_unmatched`, `_excluded`, `_superseded`,
`_incomplete`, `_duplicates`, `_converted_originals`. Inside each, a file keeps its path
relative to the platform folder. **Do not use these names for your own folders**: a file
inside one is treated as already set aside for that reason - if it is a ROM that matches a DAT
and no rule excludes it, the next organise / Build library moves it back to its canonical place,
and if it is excluded it simply stays.

Libraries built by an older version kept these folders *inside*
`_unmatched/` (`_unmatched/_excluded/` ...). The next Build library recognises them and moves
the files to the new top-level folders (or to their canonical place if they now qualify) as
ordinary previewed moves; **Undo last** reverts it, and old undo logs still work.

### Amiga (TOSEC): one folder per DAT

After organising it looks like this:

```text
amiga/
  Commodore Amiga - Firmware/                        Kickstart ROM images (.rom)
  Commodore Amiga - Games - [ADF]/                   games, plus their .m3u playlists
  Commodore Amiga - Kickstart-Disks/
  Commodore Amiga - Operating Systems - Workbench/
  _unmatched/                                        only files that matched nothing
  _excluded/                                         bad dumps, betas, demos, language/flag exclusions
  _superseded/                                       older versions / worse variants
  _incomplete/                                       multi-disk games with missing disks
  _duplicates/                                       extra copies of the same ROM
  .romorg-undo-<timestamp>.json                      undo logs (hidden)
```

- DAT folder names are the DAT name without its version (characters that are illegal on
  FAT/exFAT are replaced with `_`). Folders are created only when something goes in them.
- Inside a DAT folder files are flat: a loose file is named `<TOSEC rom name>`, a zip with
  a single matching member is named `<TOSEC game name>.zip` (the file inside is unchanged).
- If a file's content is listed in several DATs, the DAT listed first for the platform wins.
- The DAT folders are canonical: a matched file anywhere under the platform folder (any
  depth, including `_unmatched/`) that is not at its canonical place is moved there, except files
  that still qualify for a reason folder (`_excluded`, `_superseded`, `_incomplete`,
  `_duplicates`). So you can simply drop new files anywhere in the folder and organise again.
- Unmatched files keep their relative path under `_unmatched/`
  (`incoming/foo.adf` -> `_unmatched/incoming/foo.adf`). Files already in `_unmatched/` stay.
  Unmatched files are always moved (there is no switch for it): the preview shows how many are going
  to `_unmatched/`, the confirmation repeats the number, and *Undo last* puts them back.
- Left in place even when unmatched: files in the folder of a DAT that is not installed
  (so a missing DAT never empties an organised folder), frontend / emulator files
  (`gamelist.xml`, `systeminfo.txt`, `metadata.txt`, `rom.key`, `*.uae`, save files) and
  symbolic links.
- The preview warns (and Apply asks twice) when most files match nothing or other systems'
  folders (`snes/`, `psx/` ...) would be moved - i.e. when the folder is too high up. The
  home folder, `/` and the root of a drive / SD card cannot be scanned at all.
- Folders that became empty because of the moves are removed (never the platform folder
  or DAT folders, and never folders that were already empty).
- Hidden files and the undo logs are never moved. Playlists created by this app whose
  disks move are removed (they are kept in the undo log; write the M3Us again afterwards).
  Playlists you made yourself are left where they are; the preview tells you when their
  disks move.

## Running from source

```sh
python3 -m romorg                 # opens your browser at http://127.0.0.1:<random port>/
python3 -m romorg --port 8765     # fixed port
python3 -m romorg --no-browser    # just print the URL
python3 -m romorg --no-update     # do not check for / download newer DATs at startup
```

`ROMORG_OFFLINE=1` switches the automatic update off as well (tests and smoke runs use it
with a temporary `ROMORG_DATA_DIR`).

Stop it with **Ctrl+C**, or with the **Quit** button in the page.

Tests: `python3 -m unittest discover -s tests -v`

On Windows use the `py` launcher with Python 3.14 (the version the Windows builds ship) instead of
`python3` / `python`: `python3` is often only the Microsoft Store stub and plain `python` can be an old
install. So `py -3.14 -m romorg` and `py -3.14 -m unittest discover -s tests -v`, the same from PowerShell
and Git Bash. The shell-script tests use Git for Windows' bash (they skip when none is found; WSL's
`bash.exe` is only a last resort). For the native FLAC tests set `ROMORG_LIBFLAC` to a libFLAC.dll (the
Windows build scripts fetch one into `packaging\.cache\native`).

## Using it

The app has a **global header** on every screen: the title (click it to go home), the **updates
line** (`TOSEC 2025-03-13 · No-Intro 2026.08.01 · checked 14:02`) with **Check for updates** (under **More**;
it becomes **Cancel update** while running; offline you get a quiet note, with no DATs and no
network a clear error with **Retry**), **Quit**, and - while something runs - the **job bar**
(scan, verify, build, convert ... with **Cancel**). The address uses `#` routes, so the browser's
Back / Forward buttons and a reload keep you where you were.

**The system list** is on the left of every screen (v0.2). **Collection** is at the top, then the systems grouped into
*Computers*, *Cartridge consoles* and *Disc systems* (the groups come from the app, nothing is hard-coded in the page).
Each system has a dot (green: scanned, amber: its DAT is missing, hollow: not scanned) and the % you own; hover for the
folder, DAT and last scan. A running scan shows its progress under the system. **Find a system** filters the list.
Clicking a system opens it on the right with its **Overview / Library / Browse / Tools** tabs, and the tab you are on stays
selected when you switch system. **Hide list** collapses the list (remembered); in a narrow window it becomes a **Systems**
button above the page. **More** in the header holds Databases, Check for updates, Compact rows and Quit. The last scan
summary of every system is a tiny record in `config.json` (counts, time, folder - never file lists), so the list survives a
restart; the full results (Browse, Library) exist only for the system scanned last and need a new scan after a restart.
Opening `#/` goes to the system you used last.

**A system page (`#/system/<slug>/<tab>`)** has a *< Systems* link and four tabs (arrow keys
move between them):

* **Overview** - the folder field with **Browse... / Folders... / Clear** (a folder is **saved the
  moment you choose it**, when you leave the field or press Enter / Scan; the chip says *Saved* or
  *Not saved* with the reason), the DAT status (the per-DAT table is in a collapsible *DAT files*),
  the **Scan folder** button, two **totals blocks** side by side - *All DAT entries* (every ROM /
  game of the DAT) and *With your library rules* (see below) - and the summary cards (have / missing / % complete, matched,
  unmatched, duplicates, header / byte-swapped, CHDs verified / identified ...; consoles are
  counted in **games**; one card per DAT for the Amiga). **Click a summary card** to open Browse on
  the matching list. First run: no folder -> the folder box is highlighted; no DAT -> a clear
  note (it is downloaded automatically on the first scan).
* **Library** - the **Library rules** panel collapsed to ONE line
  (`4 of 4 exclusions · English · Europe > USA first · latest versions only`) with an **Edit**
  control; open it for the catalog-driven rules, languages, keep-flags and region priority. Then
  **Preview library**, **Build library**, **Undo last**, the preview cards, the incomplete-sets
  and *Games that vanish* lists, and under *Advanced: organise only / playlists only* the plain
  Organise and M3U steps. Without a scan the tab says *Scan first* with a button.
  **Every Preview button stays useful.** After the first press it reads **Recalculate preview**
  (refresh icon), with *Calculated 14:32 · 1,234 files* and *Recalculates with your current rules and
  folder contents* under it, and a spinner while it works (an error shows *Try again*). As soon as you
  change a rule, language, region, keep-flag or option - or a scan / build / undo / convert changes the
  folder - the preview on screen is marked **out of date**: a banner (*Rules changed since this
  preview - Recalculate*), dimmed cards, **Recalculate** becomes the highlighted button and
  **Build library / Apply / Convert ...** are disabled until you recalculated. Build library also sends the
  identity of the plan you looked at and the app refuses (409) a plan that no longer matches the saved
  rules. The Advanced organise / playlist previews and the Convert and Kickstart previews behave the same.
* **Browse** - the heavy part, built only when you open it: tabs **Games** (consoles: green = have,
  red = missing), **Matched files**, **Missing**, **Unmatched**, search, a collapsible *Filters*
  box (region, language, video, tag, library rules), paging, and the checksums (below).
  Bad dumps show the exact DAT flag (`Bad dump [b corrupt file]`).
* **Tools** - only what applies to the system: **Convert** (SNES, N64 No-Intro format; disc systems
  raw Redump sets to CHD incl. the chdman status), **Verify CHDs** (disc systems) and
  **Kickstarts** (Amiga, WHDLoad). A system with no tool has no Tools tab.

### Library totals (Overview)

*With your library rules: have N of M games.* **M** is what your rules would keep if you owned
**every** ROM of the system's DAT (the same exclusions, keep-flags, languages, latest-only, best variant,
one-per-game / region priority, borrowed editions and complete-sets-only that Build library uses);
**N** is how many of those games you own in at least one version that passes the rules (a multi-disk game
needs a complete set). The unit is a **game** as the library defines it, never a file. Under it:
*K of your N are not the preferred version (an upgrade is available)*, *P games you own are excluded by your
rules* (you only have a beta / bad dump / other-language version) and *multi-disk games you own are
incomplete*, so the numbers reconcile with *All DAT entries*. Without a scan only the target size M is shown
(*Scan to see how many you have*). M is computed in the background (a few seconds for the Amiga Games DAT,
under two seconds for the others; *calculating...* with a spinner meanwhile), cached per rules + DAT
version, recomputed when a rule or the DAT changes, and the small result is kept in `config.json`
(`library_totals`: counts only) so M shows at once after a restart. `GET /api/library/totals?platform=`
returns it (docs/ARCHITECTURE.md, Amendment 17).

### Checksums in Browse

Every row of Games / Matched / Missing / Unmatched has a **Show / Hide** button in its
*Checksums* column, and the toolbar has **Show checksums** which opens it for every visible row.
It lists **DAT (No-Intro / TOSEC / Redump / WHDLoad)** rows and **Your file** rows in monospace
(CRC32, MD5, SHA-1); a hash is a button - click it to copy. A green check means both sides are
equal. Nothing is re-hashed: the values are what the scan already knows, and `-` means *not
known* (hover for why) - the app never invents a hash:

* normal match: DAT and your file, with checks;
* SNES copier header / NES iNES header / N64 byte-swapped: **Your file (as stored)** (its own
  hashes, different from the DAT by design) and **Your file (normalised)** (what was compared,
  equal to the DAT) plus the reason;
* inside a zip / 7z: only the CRC32 (and size) is stored for the member, MD5 / SHA-1 show `-`;
* MD5 of your own loose files is not computed while scanning (`-`);
* discs (Dreamcast, PlayStation, PlayStation 2): a block per track (number, type, size) with the
  Redump hashes and the decoded hashes of your CHD / raw set; an audio track that was only
  identified by length says *length only* until **Verify fully** hashed it;
* multi-disk Amiga sets: the disks of the set, each with DAT and local hashes, missing disks marked;
* missing games: DAT checksums only; unmatched files: *Your file - no match* with their own hashes.

## How matching works

- Files are matched by **content**, never by name. Loose files are hashed (CRC32 + SHA-1)
  and matched by SHA-1, falling back to CRC32 + size.
- `.zip` archives are matched by the CRC32 + size of their members (read from the zip
  directory, no extraction). `.7z` / `.rar` work if the `7z` command is installed;
  otherwise they are listed as *unsupported* (and treated as unmatched when organising).
- Hashes are cached (keyed by path, size and modification time), so re-scans are fast.
- Hidden files, `.m3u` files and the app's undo logs are ignored. Leftovers of an
  interrupted move (`*.romorg-tmp-*`), partial output of an interrupted Convert
  (`*.romorg-convert-*`, removed by Undo) and dangling symbolic links are listed as errors.
- **Have / Missing** count distinct DAT entries (Amiga) or **games** (consoles);
  **Duplicates** are extra local copies of something you already have. TOSEC sometimes
  lists identical content under several names - such files match all of them.
- No-Intro DATs list some games in two forms with the same name, e.g. NES `.nes`
  (with iNES header) and `.unh` (headerless), or N64 `.z64` and `.v64`. Either one counts
  as having the game.
- If a file matches nothing as it is, the console's alternative forms are tried (files
  are never modified by scanning):
  - SNES: files whose size is 512 bytes over a multiple of 1 KiB (or 512 bytes over the
    size of a DAT entry, for the few odd-sized ones) are also hashed without that copier
    header (`.smc`, `.swc`, `.fig`);
  - NES: files with an iNES header are also hashed without it (matches the `.unh` entry);
  - N64: byte-swapped (`.v64`) and little-endian (`.n64`) dumps are hashed in the
    big-endian `.z64` byte order.
  These show a *copier header* / *iNES header skipped* / *byte order* chip in the results.

## Name tags (regions, languages, versions)

The names of DAT entries are parsed into tags, for No-Intro (`Game (USA, Europe) (En,Fr)
(Rev 1) (Beta)`) and TOSEC (`Game v1.2 (1990)(Publisher)(DE)(de-en)[cr Group]`):
regions (with the PAL / NTSC standard derived from them; *World* counts as both),
languages (taken from the region when there is no language tag, e.g. USA -> En), version
(`Rev 1`, `Rev A`, `v1.1`), status (`Beta`, `Proto`, `Demo`, `Sample`, ...), other flags
(`Unl`, `Aftermarket`, `Virtual Console`, ...) and dump flags (`[b]` bad dump, `[BIOS]`,
TOSEC `[cr]`, `[a]`, `[h]` ...; No-Intro's leading `[BIOS]` too). They are shown as chips
and can be used to filter the result tabs (the *Tag* filter offers status, license,
distribution, hardware and dump flags - publisher names, disk labels and dates are left out). (Organising by filter, e.g. "PAL + English only", is planned.)

## Build library: the library rules

The *Library rules* panel is grouped into **Exclude** (on by default), **Keep these dump
types**, **Options**, **Languages** and, for consoles, **Region priority**. Each rule shows the
exact tags it acts on as small chips (`(pre-release)`, `[b ...]`, `[m ...]`; ` ...` stands for
free text) and, after a scan, the number of files it moves. Choices are remembered per system;
*Reset to defaults* restores them. A file moved because of a
rule keeps its file name and goes to `_<reason>/<its path relative to the folder>` (a folder
next to the games, e.g. `_excluded/`).
A file under `_<reason>/` that no longer qualifies (you switched the rule off, or
the DAT changed) moves back to its canonical place on the next build. If the target name in a
reason folder is already taken, the file gets a free name (`name (2).ext`); nothing is overwritten.
A symlinked file takes part in the selection (it can fill a disk slot) but is never moved.
On the keep-every-version DATs (Kickstart-Disks, Workbench, Firmware) the option *Keep the
only dump of a version* (below, off by default) rescues a sole `[m]` / `[o]` / `[u]` dump.

**Duplicates** (always, not a rule). Several local files with the same content matching the
same DAT entry (a loose file and the same ROM inside a zip count too): exactly one is kept -
the one already at its canonical path and name, else exact DAT content, else a loose file over
an archive, else the shortest path, then alphabetical. The rest go to `_duplicates/`. The scan's
*Duplicates* number equals the moves the build would make.

**Excluded** (`_excluded/`): bad dumps `[b]`, `[b1]`, `[b reason]`; `(pre-release)`, `(beta)`,
`(alpha)`, `(preview)` (No-Intro also `(Debug)`, `(Test Program)`); `(proto)` (No-Intro
`(Proto)`, `(Possible Proto)`); all demos `(demo-playable)`, `(demo-rolling)`, `(demo-slideshow)`,
`(demo)` (No-Intro `(Demo)`, `(Sample)`, `(Kiosk)`); `[faked ...]`; `[unreleased]` /
`(unreleased)`; modified `[m ...]`; virus-infected `[v ...]`; over- and under-dumps `[o]` / `[u]`.
**Kept:** cracks `[cr]`, hacks `[h]`, trainers `[t]`, alternates `[a]`, fixes `[f]`,
translations `[tr]`, and for consoles `(Unl)`, `(Aftermarket)`, `(Pirate)`, Virtual Console etc.
Each rule can be switched off on its own. `[bootable]` is a note, not a bad dump. A
`[m baddump]` file is a *modified* dump, not a bad one.
The exclusions apply to all Amiga DATs, so Workbench loses its `[m ...]` modified disks too -
untick *Modified* or tick *Keep the only dump of a version* if you want those.

**Latest versions only** (`_superseded/`; Amiga *Games - [ADF]* and the No-Intro consoles
only - never Firmware, Kickstart-Disks or Workbench, where every version is kept). Consoles:
the newest `Rev` / version **per region** (regions are never compared with each other; betas
and the like are separate anyway). Amiga: see the next rule.

**One best variant per game** (Amiga *Games - [ADF]* only). A game is identified by its title
without version tokens plus country, publisher and edition flags; the year, the language and
bare chipset tags (`AGA`, `OCS`, `ECS`, `CD32`) are not part of it, so an OCS and an AGA release
of the same game compete. Among the *complete* sets of a game exactly one is kept, ranked:
1. your preferred language (the order you ticked / arranged in *Languages*),
2. cracked (`[cr ...]` on any disk) over uncracked,
3. platform: CD32 over AGA over OCS (untagged / OCS / ECS count as OCS),
4. the newer version,
5. fewer extra modification flags (`[t] [h] [tr] [f] [a]`), then name.

So the latest cracked AGA version wins, a game that only exists as a crack is kept (unless you
switch cracks off), and everything else of that game goes to `_superseded/`. Disks of different
languages or chipsets never share a playlist.

**Languages** (Amiga *Games - [ADF]* and every No-Intro console). The checkboxes come from the
installed DAT: English first, then by number of releases, each with its count. **Only English is
ticked by default**; tick more to keep them, and the order of the ticked ones (the *Preferred
first* list, with up / down buttons) decides which release wins. Nothing ticked means no
language filter. What counts as a language of a release:
- an explicit language tag lists them all: `(de-en)` is English *and* German, `(En,Fr,De)`;
- no language tag: the language of the country tag (`(DE)` -> German; Switzerland -> German,
  Belgium -> Dutch ...);
- no language and no country tag, or only a neutral region (World, Asia, Unknown): English.
  This is the TOSEC convention, quoted from the naming convention: *English is the default
  when there is no language / country flag*;
- TOSEC `(M3)` (multi-language) includes English;
- `[tr en]` marks an English translation and counts as English (`[tr de]` adds German).
**No-Intro and Redump (consoles and discs):** a game with no version in your languages *anywhere in its DAT* (a
Japan-only release, for example) is **kept** by default (option *Keep games that exist only in other languages*, in the
rules and in Collection). The whole DAT decides, not the files you own: if an English version of the game exists in the DAT,
your Japanese copy is still left out, and the English one is "missing". Among several versions of such a game the usual
region and revision order picks one. Turn the option off to have every title without a version in your languages left out.
TOSEC and WHDLoad keep the rule below.

A TOSEC / WHDLoad title with no version in a ticked language is left out; the **Games that vanish** list
shows them with the languages they do have, and offers a button to tick one of those languages.

**Keep these dump types** (Amiga Games). Cracks `[cr]`, hacks `[h]`, trainers `[t]`,
alternates `[a]`, fixes `[f]` and translations `[tr]` are all kept by default. Unticking a type
excludes every variant that carries it (reason `flag_<x>`). **Be careful with cracks**: a large part of
the Amiga library (about a third of the games in the TOSEC *Games - [ADF]* DAT) exists only as
cracked dumps, so unticking `[cr]` makes those titles vanish; the panel shows a warning with the
number as soon as it is off, and the preview lists the titles.

**Consoles (No-Intro): region priority and one version per game.** *One version per game*
(on by default) keeps exactly one release of each game (per header form): your preferred
language, then the best region from your **Region priority** list (default Europe, USA, World,
Japan, then every other region A-Z; a multi-region release ranks by its best region), then the newest revision, then the fewest extra tags. Different
products stay separate (`(Unl)`, `(Aftermarket)`, `(Pirate)`, `(Beta)`). Switch it off to keep
the latest version of every region instead. These two settings are hidden for Amiga.

**Reordering the region list.** The list sits in a scroll box (about 360 px high) with a divider under
the four *prioritised* regions. Drag a row by its **⋮⋮** handle with the mouse or a finger (the list
scrolls by itself when you hold the row near its top or bottom edge; swiping anywhere else on a row
still scrolls the list). With the keyboard or a controller: focus a row (arrow keys), **Space / Enter**
picks it up, **Up / Down** (or Home / End) moves it, **Space / Enter** drops it, **Escape** cancels.
Each row also has **Top** (move to the first place), **▲** and **▼** buttons, and the *Find a region...*
box narrows the list to the matches (dragging while filtered keeps every hidden region where it was).
Every finished move is saved once; if saving fails the list jumps back and a message says so.

**Keep the only dump of a version** (off by default; Workbench, Kickstart-Disks and Firmware).
A disk excluded only by `[m]`, `[o]` or `[u]` is kept anyway when it is the sole dump of its
version (for example a modified Workbench disk). Bad dumps, viruses, pre-releases and demos
are never rescued.

**PUAE and TOSEC tags.** Keep tags like `(AGA)`, `(CD32)`, `(A1200)`, `(NTSC)`, `(PAL)` in
the file names. The PUAE core's *Automatic* model reads them from the whole file path, so an
AGA game boots on an A1200 without any per-game configuration.

**Complete multi-disk sets only** (`_incomplete/`; Games, Workbench and Kickstart-Disks).
If no complete set of a multi-disk game can be built from the files you have, its disks go to
`_incomplete/` and the preview lists the missing disk numbers. The best set is the newest
*complete* one, so if v1.1 cannot be completed but 1.0 can, 1.0 wins.

**Playlists.** For every kept multi-disk set one `.m3u` is written next to the disks (inside
the DAT folder). Sets are built **slot by slot**: TOSEC only lists the disks that changed
between versions, so for each disk number the newest compatible disk you have is used. For
example `ABC Monday Night Football v1.1 (1991)(Data East)(US)(Disk 1 of 3)[cr SR]` plus
`ABC Monday Night Football (1990)(Data East)(US)(Disk 2 of 3)` and `(Disk 3 of 3)` make one
complete set, named after its newest disk 1:
`ABC Monday Night Football v1.1 (1991)(Data East)(US)[cr SR].m3u`. Compatible means the same
title, publisher, country and language and compatible dump flags (an unflagged disk fits a
`[cr X]` disk 1); excluded variants (pre-release, `[b]` ...) are never borrowed from.

**Complete sets with disks from other editions** (rules panel, *Complete sets with disks from other
editions*, on by default, Amiga Games only - never Workbench / Kickstart-Disks). When your edition of a
multi-disk game lacks a disk but you own that disk from another edition (matched by checksum), the
set is completed with it. Only the **country, language, edition flags, version and year** may differ:
the title, publisher and disk count (`Disk N of M`) must be identical, the chipset must fit (an OCS set never
takes an AGA-only disk and the other way round; an `OCS-AGA` disk fits both; ECS counts as OCS), the dump
flags must fit disk 1 (an unflagged disk fits a `[cr X]` disk 1, a different crack never does) and the disk must
pass every exclusion rule (bad dumps, viruses, pre-releases, prototypes, demos, faked, unreleased, modified, size
problems are never borrowed). The disk may be in a language you did not select: the language filter does not
exclude it and it stays in the library as part of the set (the other edition's remaining disks are classified
as usual, e.g. `_excluded/` for language). A slot is filled from the same edition first, then from another
edition in a selected (or neutral) language, then from any compatible one; the newest wins. Disk 1 still names the
playlist. The preview shows the *Sets completed with borrowed disks* count and a note per borrowed disk
(`disk 2 borrowed from the (DE) edition (Foo (1991)(Pub)(DE)(Disk 2 of 3))`); the file's reason says
`borrowed as disk 2 of ...`. Borrowing only happens for a game that has no complete set of its own with that
disk count. Switch the option off to get the old behaviour (such sets go to `_incomplete/`). On the real TOSEC
Games DAT with every ROM present and the default English-only rules this completes 14 sets (41 files that
would have been `_incomplete`); with no language filter 26 sets (102 files).

**Sorting, details and your own choices.** Every column header that sorts (name, rating, year, size) cycles ascending, descending, off; games without a rating
(or year, or size) always stay at the bottom. The **Library** list shows each file's rating, year and size, and its **Details** button explains why the file is kept,
excluded or superseded and shows the DAT and file checksums. Tick rows and use **Always keep** / **Always exclude** to override the rules for single games (**Back to the
rules** undoes it; press Recalculate to see the result). The **Columns** menu hides columns, **Compact** shrinks the rows, and sorts, filters and hidden columns are
remembered per system. `tools/bench_server.py` times the scan, lists and planner on a real folder; `python3 -m unittest tests.test_browser` clicks through the UI in a real browser.

**Ratings (optional filter).** The rules panel has a **Ratings** group for every system with a LaunchBox
platform (Amiga, WHDLoad, GBA, GB, GBC, DS, N64, NES, SNES, Mega Drive, Master System, Game Gear, 32X, Lynx, Dreamcast, PlayStation, PlayStation 2). It is **off by default - nothing changes and no
rating data is needed**. Set a **Minimum rating** (0-10, shown as LaunchBox stars x 2 with the vote count, e.g.
`8.4 · 123 votes`), a **Top N games** limit, or both; **Minimum votes** (default 5: a game with fewer votes counts as
*unrated*); **Keep unrated games** (off: while a rating filter is set, games with no usable rating are left out - tick it to keep
them in addition to the rated games that pass); **Rank against** *the whole DAT* (default: the top N is taken from every game the
other rules keep for the whole DAT, so adding files never pushes others out and the totals read "have K of N") or *only my games*
(rank among the games you own; the totals then show every game that passes the other rules, and a note says so). The filter is
applied **last**, per **game** (all versions, disks and discs of a game share one rating): games it removes go to `_excluded/` with
the reasons *Rated below the minimum*, *Not among the top rated games* or *No usable rating*; the preview shows them as cards and
chips, the **Games that vanish** list says why (with hints such as "lower the minimum rating" or "tick Keep unrated games"), and the
Overview / Library totals count the target AFTER the rating filter (rating-excluded files you own count under "excluded by your
rules"). Games with the same title as the N-th game stay or go together, so "top 300" can keep a few more than 300. The rules summary
line shows e.g. `rated ≥ 7 · min 5 votes`. In **Browse → Games** a **Rating** column (click the header to sort best first) and
**Rated / Unrated** chips are shown whenever the ratings data is installed.

The data is the **LaunchBox Games Database** community ratings (`https://gamesdb.launchbox-app.com/Metadata.zip`, about 108 MB,
rebuilt daily, free, no key). It is fetched **only when a rating filter is enabled** in some system's rules (or when you press
**Download ratings**), and then kept fresh at startup only when the local copy is older than 7 days *and* the server's file is newer.
The zip is stream-parsed (no 512 MB temporary file, about 65 MB of memory, about 30 s) into a small local index
`ratings/ratings.sqlite` (about 3 MB) and then deleted; offline, the installed index keeps working. Until the data is installed, Preview
and Build wait for it with a clear message and progress (they never apply a filter with missing data). A DAT title is matched to the
LaunchBox title of the same platform conservatively: normalised title (case, punctuation, `&` = and, articles, subtitle separators),
LaunchBox's alternate names, II..IX read as digits (only when unique), then a high-threshold *unique* fuzzy match (same numbers, no
added words); when in doubt there is **no** rating rather than a wrong one. Several LaunchBox games with the same title: the one with
the most votes. Ratings: LaunchBox Games Database community ratings (see `docs/THIRD_PARTY.md`).

**Undo.** One undo log covers the moves and the playlists: **Undo last** moves the files
back, removes the playlists the build created (only if unchanged) and restores the outdated
playlists it deleted.

With the rules off (or in *Advanced: tidy only*) the older behaviour remains: **Latest
version only** moves older versions to `_superseded/` using the same ranking as
above for consoles.

## Nintendo GameCube: ISO and Dolphin RVZ

A flat folder with one file per disc (`.iso`, `.gcm` or `.rvz`), matched against the Redump DAT *Nintendo - GameCube*
(2,019 games). Redump hashes the ISO, so a `.rvz` (Dolphin's lossless compressed format) is **rebuilt in memory while it
is read** and hashed: the same CRC32 / SHA-1 as the ISO it was made from, which the app compares with Redump (checked on
real Dolphin files: the rebuilt images equal the Redump entries). Nothing is written, and a matched `.rvz` keeps its
extension when it is renamed to the Redump name (the Matched list shows an **rvz** chip). Decoding runs on all cores
(about 4 s for a 1.4 GB disc on a Steam Deck; the result is cached, so a rescan takes a fraction of a second).

Not read: **Wii** discs (the same container, but the partitions are encrypted; they are listed as unsupported), the
older `.wia` format, NKit images (they drop data the hash needs) and compressed ISOs such as `.gcz` / `.ciso`.
There is nothing to convert on this system.

## Sega Dreamcast: CHD + Redump

**Layout.** One folder per game, named exactly like the Redump entry, holding the CHD and its sidecars:

```text
dreamcast/
  Sonic Adventure (USA) (En,Ja,Fr,De,Es) (Rev A)/
    Sonic Adventure (USA) (En,Ja,Fr,De,Es) (Rev A).chd
    Sonic Adventure (USA) (En,Ja,Fr,De,Es) (Rev A).zip      sidecars: every file that starts with the CHD's old name
    Sonic Adventure (USA) (En,Ja,Fr,De,Es) (Rev A).state    (.zip .md5 .gdi .cue .state .srm .sav ...) is renamed with it
  Shenmue (Europe) (En,Fr,De,Es) (Disc 1)/...               multi-disc: one folder per disc, the playlist is next to disc 1
  _unmatched/  _excluded/  _superseded/  _duplicates/  _converted_originals/    the usual reserved folders
```

*Organise* / *Build library* rename the game **folder**, the CHD and every sidecar to the Redump name (so save
states stay linked to the game; they are never deleted or rewritten). Everything else in the folder keeps its
name and moves along with the folder. A loose `.chd` in the system folder gets a new `<Redump name>/` folder with
its sidecars (files that start with its name). A CHD that matches no Redump entry moves **with its whole folder**
into `_unmatched/<folder>`; other unrelated files are handled by the usual unmatched rules (frontend files such as
`gamelist.xml` / `media/` stay). Two copies of one game: the one with more files (your saves) stays, the other
moves whole to `_duplicates/`. Every step is previewed first, never overwrites, and is undoable (one undo log).

**Where Redump comes from.** `http://redump.org/datfile/dc/` - **plain HTTP only** (redump.org refuses
HTTPS connections). At every start the app sends one `HEAD` request (user agent `simple-rom-organiser/<version>`)
and reads the version from the `Content-Disposition` file name (`Sega - Dreamcast - Datfile (1516) (2026-06-14 18-25-41).zip`);
only when that date is newer than the installed DAT's `<version>` is the zip downloaded, its single `.dat`
extracted, validated by parsing and swapped in atomically. It lives in its own folder (`redump/` in the data folder)
that no other updater touches; offline, the installed DAT keeps working. The DAT lists one `.cue` and one
`(Track N).bin` per track for every disc; the `.cue` entries are ignored and a game is *one disc*.

**How a CHD is matched** (the pure-Python reader needs nothing installed): the CHD's header and metadata give its
tracks (data / audio, sizes) without decoding anything. The Redump games with the same track sizes are the
candidates; every **data** track is decoded (the GD-ROM pad frames and the subcode are dropped, exactly like
`chdman extractcd`) and hashed (crc32 + md5 + sha1) and must equal the Redump track. Audio tracks are compared by
**length only**. That is the level **identified**. When every track - audio included - was decoded and equals
Redump (by chdman, or by the Verify fully button) the level is **verified**. A CHD matches a game only if every
`.bin` track matches. Results are cached per file (path, size, modification time and the CHD's own header SHA-1),
so a rescan of an unchanged CHD never decodes it again; the cache is in the data folder, scanning never writes
into your game folder.

| Level | What was compared | How |
| --- | --- | --- |
| **identified** | track sizes + crc32 / md5 / sha1 of every data track; audio by length | built-in reader (default) |
| **verified** | every track, audio included | **Verify fully** (built-in reader, parallel), or chdman when the reader cannot decode the CHD |
| **raw (convertible)** | an unpacked Redump set, every track file hashed | files, no CHD involved |

**Engines and speed (Amendment 14).** The **built-in reader** decodes first (`auto` = the default): a parallel scheduler
splits every track of every CHD into ~4 MB chunks, decodes them in one worker process per CPU thread (`chd_workers` in
`config.json` or `ROMORG_CHD_WORKERS`; `1` = the old sequential in-process path, handy for debugging) and hashes them in
order (crc32 + md5 + sha1 in parallel threads). Memory stays bounded (at most 256 MB of chunks in flight; fewer
workers when the machine is short of RAM). FLAC audio (`cdfl`) is decoded by the system's / the bundled **libFLAC through
ctypes** (about 80-100 MB/s per core instead of 1.2 MB/s in pure Python; the pure-Python decoder remains as the fallback,
same results). The Dreamcast / PlayStation bar shows which engine ran and its MB/s. See the table in
`docs/ARCHITECTURE.md` (Amendment 14) for measured times on a Steam Deck. **chdman is not needed**: the app reads and writes CHDs itself. An installed chdman is used only for a CHD in a
format newer than the built-in reader knows, or when you choose it in the Convert step ("always chdman" for
reading, "chdman, when it is installed" for writing). The built-in reader reads everything chdman 0.289 reads: CHD versions 1 to
5, every compression (`zlib`, `lzma`, `zstd`, `huff`, `flac`, the CD codecs `cdlz` / `cdzl` / `cdfl` / `cdzs`, the
laserdisc codec `avhu`) and CHDs that need a parent file (the parent is found by its SHA-1 in the same folder).
Zstandard uses the system's libzstd (SteamOS has it) and falls back to a slow built-in decoder without one; the engine setting `chd_engine` is `auto`
| `python` (never chdman) | `chdman`.

**chdman (optional).** The AppImage no longer ships chdman. It ships libFLAC / libogg under `tools/lib` (not in git: the
build downloads checksum-pinned Arch Linux packages, see `docs/THIRD_PARTY.md` for versions, licences as the packages
declare them, and where to get the source; the text is also inside the AppImage as `licenses/THIRD_PARTY.md`). A chdman
you have installed is still found - `$ROMORG_CHDMAN` / the saved chdman path, `chdman` on `PATH`, the Flatpak
`org.mamedev.MAME`, `~/.local/bin`, `~/Emulation/tools`, ... - and can be chosen in the Convert step.
The Windows packages carry no chdman either and do not need one; to use it anyway, take `chdman.exe` from the MAME
download at mamedev.org and put it on `PATH`, next to the app (the exe's folder, or the top folder of the unzipped
package), or save its path in the Convert step (see `docs/PACKAGING.md`).
`Simple_ROM_Organiser.AppImage --self-check` tests the CHD engine (libFLAC decodes, the scheduler runs, the writer writes
a CHD that reads back).
When chdman does extract, the disc goes to **scratch space - never your game folder**: (1) **RAM** (`/dev/shm`, `$XDG_RUNTIME_DIR`, or
`/tmp` when it is a tmpfs) when it has the room AND the machine has that much memory available PLUS a 2 GiB reserve
(`ROMORG_TEMP_RESERVE_MB` / config `temp_ram_reserve_mb`); (2) otherwise `<data dir>/cache/tmp` (`$ROMORG_TEMP_DIR` /
config `temp_dir`); (3) otherwise the built-in reader is used and the job message says why. Every job uses its own marked folder,
swept after crashes; a cancel deletes it at once. A Flatpak chdman can only see folders the Flatpak is allowed to
(`flatpak override --user --filesystem=/run/media/deck org.mamedev.MAME`).

**Convert raw sets to CHD.** The app writes the CHD itself (`romorg/chdwrite.py`): for the same set it produces the
same disc image as `chdman createcd` / `createdvd` - identical data SHA-1, metadata and header SHA-1, accepted by
`chdman verify` - within 0.5 % of chdman's size and in the same time or less (PlayStation 2 images about twice as
fast; measurements in `docs/ARCHITECTURE.md`, Amendment 22). *Compression of new CHDs* offers **standard** (LZMA /
FLAC, read by every emulator) and **Zstandard** (about 3x faster to convert and faster to load, but only emulators
from 2024 on read it - convert one game and try it before converting a collection). A folder with a `.gdi` (or a `.cue` with one file per track) and its
track files that matches a Redump game is flagged *raw (convertible)*. A Dreamcast set that has only a `.cue` is converted only when
the cue carries Redump's `REM SINGLE-DENSITY AREA` / `REM HIGH-DENSITY AREA` markers: the app then generates the `.gdi` (track 1 at LBA 0,
the following single-density tracks back to back, the first high-density track at LBA 45000) in memory (chdman, if you chose it, gets it in scratch space with symlinks to your files); otherwise
the set is marked *needs a .gdi* and not converted. *Convert* writes the new CHD
as `<Redump name>.chd.romorg.part` next to its final place (that is the intended output, not scratch), **checks the kind of disc
(a Dreamcast game must come out as a GD-ROM CHD `CHGD`, not a CD `CHT2`), decodes it again with the built-in reader
and compares every track with Redump**, and
only if all tracks match renames it into `<Redump name>/<Redump name>.chd` and moves the raw files to
`_converted_originals/<their path>` (nothing is deleted). Any failure, mismatch, missing space or Cancel leaves the raw files
untouched and no `.part` / temp files behind. About the disc size must be free next to the games during a conversion. **Undo last**
removes the new CHD (after checking its SHA-1) and puts the raw files back.

**Library rules for Redump names** (`Title (Region) (Languages) (Rev A) (Disc 1)`), in the same panel as the
consoles: *Demos / samples* (`(Demo)`, `(Sample)`, the Japanese `(Taikenban)` / `(Tentou Taikenban)` /
`(Tentou-you Demo)` / `(Trial Disk)` discs **and** every disc Redump files under the categories *Demos* and
*Coverdiscs*), *Pre-release* (`(Beta)`), *Prototypes* (`(Proto)` and the category *Preproduction*); the other
categories (Games, Applications, Multimedia, Bonus Discs, Video, Add-Ons) are kept. Languages (English by
default; no language tag falls back to the region: Japan = Japanese), region priority (Europe, USA, World, Japan)
and **one release per game**. **Multi-disc games stay together**: the best *release* (language, region, newest revision)
is chosen as a whole and ALL its discs are kept (the newest revision of every disc); the others are
`_superseded`. Each kept multi-disc game gets an `.m3u` playlist (relative paths) written next to disc 1, e.g.
`Shenmue (Europe) (En,Fr,De,Es)/Shenmue (Europe) (En,Fr,De,Es).m3u` -> `Shenmue ... (Disc 1).chd`,
`../Shenmue ... (Disc 2)/...`; a game with a disc missing keeps its discs where they are (no playlist) and is listed.
Disc labels (`|Disc 2`) are off by default (not every frontend reads them).

## Sony PlayStation / PlayStation 2: CHD + Redump

Both systems are built exactly like the Dreamcast (same engine, same screens, same guarantees): own folder per
system, own Redump DAT (`Sony - PlayStation`: 10,914 discs; `Sony - PlayStation 2`: 11,774 discs; no cross-matching),
**one folder per game** named like the Redump entry: `<system folder>/<Redump name>/<Redump name>.chd` plus sidecars.
A loose CHD (the usual PS2 library: `Gran Turismo 4 (USA).chd` next to its `.state` / `.srm` / `.mcr` files) gets its
own folder; the folder, the CHD and every file that starts with the CHD's old name are renamed to the Redump name
(so `Tony Hawk's Pro Skater 4 (USA).chd` becomes `Tony Hawk's Pro Skater 4 (USA) (v1.02)/...` and its save states
follow); other files in a game folder move along; a game folder that matches nothing goes whole to `_unmatched/`; raw
sets are left alone except via *Convert*. Preview first, undo log, nothing overwritten.

**What is matched.** PlayStation: every game is a `.cue` + `(Track N).bin` per track (a single-track disc: one
`.bin`). PlayStation 2: 8,606 DVD games are ONE `.iso`, 3,168 CD games are `.cue` + `.bin`. A CHD made with
`chdman createcd` stores a DVD ISO as a `MODE1` track of 2048 bytes per 2448-byte frame (no padding, no subcode in the
extracted file), a PlayStation disc as `MODE2_RAW`; the built-in reader decodes both (MODE 2 sectors get their sync and
error-correction bytes rebuilt, with the header counted as zeros as the CD standard says) and the result must equal
the Redump size and crc32 / md5 / sha1 - a PS2 CHD matches only if the whole ISO (or every bin track) is identical. A DVD
CHD made by `chdman createdvd` (metadata `DVD `, 2048-byte units) carries the ISO's SHA-1 in its header, so it is
*identified* instantly without decoding; **Verify fully** decodes it (*verified*). Every compression chdman 0.289
writes is decoded by the built-in reader; a CHD in a format newer than that is reported as *needs chdman* and **left
in place** (never moved to `_unmatched/`); with such a chdman installed it is read by chdman. PS2 DVD games have no audio tracks, so for them
*identified* from a decode is already fully verified.

**Big-file scan times (built-in reader, Steam Deck, 8 decode processes).** Measured on the real files: God of War II (8.1 GB of ISO data)
40 s (was 185 s on one core), Bully (4.4 GB) 24 s (was 127 s), the whole seven-file PS2 folder (30 GB decoded) 2.6-3.2 min at ~190 MB/s
(was 5.2 min with the old four file-level processes), a PlayStation disc about 5 s. Every result is cached by (path, size, modification time,
CHD header SHA-1): the first scan is the slow one, every rescan takes a fraction of a second. The scan shows *file i/n, MB/s and an ETA*
and can be cancelled at any time.

**Library rules** (Build library; same rules and defaults as the Dreamcast): region priority Europe > USA > World >
Japan > others, English by default, ONE release per game, discs of a multi-disc game always stay together, newest
revision wins within a region; Redump categories *Demos* and *Coverdiscs* are demos, *Preproduction* is
prototype / pre-release (plus the `(Beta)`, `(Proto)`, `(Sample)` and Japanese `(Taikenban)` tokens), while
*Applications*, *Educational*, *Bonus Discs*, *Multimedia*, *Add-Ons* are kept on purpose. Budget re-releases
(`(PlayStation the Best)`, `(Greatest Hits)`, `(Platinum)` ...), `(Unl)` and special editions are different products and are never merged;
`(Rev N)` / `(vX.Y)` are revisions (newest wins); `(Disc A)` / `(Disc B)` count as discs 1 / 2.
**Playlists:** PlayStation multi-disc games get an `.m3u` with relative paths next to disc 1 (RetroArch and DuckStation read it);
**PlayStation 2 gets none** - PCSX2 does not use `.m3u` playlists (the policy is a per-system setting).

**Convert.** A PlayStation `.cue` + `.bin` set (or a PS2 CD set) becomes a CD-image CHD (what `chdman createcd` writes);
a single PS2 `.iso` a DVD-image CHD (`createdvd`; config key `ps2_iso_chd` = `dvd` (default) | `cd` makes it a CD image, which
is the same kind of CHD as the one in your library). The new CHD is decoded again and compared with Redump before
the originals move to `_converted_originals/`. *Assumption, not verified here:* PCSX2 loads both `createcd` and
`createdvd` CHDs (the createcd ones are what the test library uses).

## Convert to No-Intro format (SNES, N64)

Organise only renames. **Convert** writes a clean copy of each copier-headered SNES dump
(header removed -> `.sfc`) and each byte-swapped N64 dump (-> big-endian `.z64`; also a
`.v64` that matches one of the DAT's own `.v64` entries, when the set has a `.z64`) under its
exact DAT name, checks it against the DAT's SHA-1, and moves the original to
`_converted_originals/` (keeping its relative path). Nothing is deleted;
**Undo last** removes the converted copies (only if unchanged) and puts the originals back.
Single-file `.zip` archives are converted into a new `.zip`; `.7z` / `.rar` and zips with
several files are skipped ("extract the archive first"). NES dumps are not converted,
because emulators need the iNES header.

## Organising safety and undo

- **Nothing is ever overwritten.** If the target already exists the file is skipped
  (`conflict`). Extra copies of the same ROM are not conflicts: one is kept and the others
  are set aside in `_duplicates/`. Targets are re-checked at the moment of moving.
- Case-only renames work on case-insensitive file systems (exFAT SD cards).
- Characters that are illegal on Windows / FAT / exFAT (`: ? * " < > | \`) are replaced
  with `_`, since Steam Deck SD cards are often exFAT.
- Moves use a hard link + unlink where the file system supports it, so a file that
  appears at the target during the apply is never replaced.
- Every apply writes an undo log `.romorg-undo-<timestamp>.json` into the platform folder,
  *before* each move, so even an apply interrupted by closing the app, a shutdown or a
  crash can be undone. Paths in the log are relative to the platform folder, so undo still
  works after the SD card is mounted somewhere else.
  **Undo last** restores the original paths (recreating removed folders and removed
  playlists), skipping files that have since been moved or replaced. If some files could
  not be restored the log is kept with just those, so you can fix the cause and undo again.
  Undo never touches anything outside the platform folder. The folder is re-scanned after
  apply and undo.
- Closing the app (Quit, SIGTERM from Steam, Ctrl+C) during an apply stops it between two
  moves.

## Build the library in another folder (v0.2)

On the Library tab, **Where to build** chooses between *In this folder* (the classic build: files are moved) and
*In another folder*: the files your rules keep are **copied** (or **moved**, if you choose) into a destination
folder, laid out exactly as an in-place build would lay them out. Excluded, superseded and unmatched files stay in
the source and are not copied. The choice is remembered.

- **How files get there:** **Copy** (the default) leaves the source with all its files; it works across drives and on exFAT SD
  cards. **Move** takes the files out of the source folder: a rename on one drive (instant, nothing written again), else a
  copy that is checked and then removed. Undo moves them back. There are no links. Move cannot be combined with *keep in
  sync* (the originals are gone, so there is nothing to compare with).
- **Safe:** the destination may not be inside the source or contain it; a different file already at a target is never
  overwritten (shown as a conflict); copies are written under a temporary name and renamed when complete; the free
  space is checked first. Running it again only adds what is missing.
- **Undo last build** removes only what the build added (recorded in `<destination>/.romorg-library/library.sqlite`),
  and leaves any file you changed since. Your own files in the destination are never touched.
- **Keep the destination in sync** (off by default; for one system and for a collection): the build then also brings the
  destination in line with the source.
  - A file this app built earlier that your rules no longer keep, or whose source is gone, is **removed**.
  - A file whose source changed is copied again.
  - A file you edited in the destination is left alone and reported. Files this app did not build are never touched.
  - The preview lists the counts (*To remove*, *To replace*, *Edited by you*) before anything is done.
  - Safety: a sync that would keep nothing (source empty or not mounted) removes nothing. A sync that would remove most of
    the library (over 20 files and over half) is refused until you confirm it separately.
  - **Undo last build** also puts removed files back from the source where it still has them. A file replaced by a newer
    version is not turned back into the old one.
- Save files and other files lying next to discs stay in the source unless "Also copy save files ..." is ticked.
- The plan for the next steps is in `docs/V0_2_PLAN.md`.

### A whole ROM root: Collection

In the list on the left, **Collection** (`#/collection`) builds the clean library of *every* system at once.

1. **Where:** the ROM root (the folder that holds one folder per system, e.g. `~/Emulation/roms`) and the destination.
   **Find the systems** matches the sub-folders to the supported systems by the usual frontend names (`gba`, `snes`,
   `genesis` / `megadrive`, `psx` / `ps1` ...); correct any folder in the table or untick a system. The destination may not
   be inside the root or contain it.
2. **Rules for every system:** one set of rules (exclusions, one version per game, latest versions, languages, region
   priority, kept variants) applies to all systems; a rule a system does not have (no regions, no language tags) is
   simply not used there. Tick **Own rules** for a system to use the rules of its own Library tab instead. Nothing set
   means every system uses its own defaults.
3. **Preview collection** scans and checks each system against its DATs (one after the other, hashes are cached) and
   shows per system what would be kept, copied or linked, what is already in the destination and any conflicts, plus the
   free space. **Build collection** does it: `<destination>/<system folder>/...`, using the transfer mode above.
   A system that fails (folder missing, no DATs) is reported and the others go on.
   **Undo last build** removes what the last build added in every system.

**Sort a mixed folder, keep the ROM folders clean.** The *Sort and tidy* panel works on whatever is under the ROM root:

- **Sort into systems:** reads every file once (hashes are remembered, so a second run is fast), finds its system by checksum
  against the DATs of every ticked system, and moves it into that system's folder (`snes`, `gba`, `psx` ... - the folder names
  found by *Find the systems*, or the usual short names such as `snes` where a folder does not exist yet). A file in the wrong
  system's folder moves to the right one; one already at home stays. Discs (CHD, cue/bin sets) move as a whole game folder. A
  save or note named like a ROM goes with it. Preview shows every move first, **Undo sort** moves it all back.
- **Set-aside folder** (default: next to the ROM root, named like it with `-aside`): files that match no system go to
  `_unmatched/` and files that are not ROMs (pictures, text, playlists ...) to `_other/` there, keeping their folders. Loose
  unmatched files only: one that sits inside a system's folder is dealt with by that system's library build.
- **After a reorganise** (next), what the rules set aside (`_excluded`, `_superseded`, `_incomplete`, `_duplicates`,
  `_unmatched`) moves out of the system folders to `<set-aside>/<system folder>/<same folder>/`, so the ROM folders hold only what
  you keep (tick or untick *After a reorganise, move what the rules set aside ...*). The converted-originals folder is never swept.
  **Undo last build** brings it all back first. **Bring set-aside files back** returns everything to the systems' folders (a later
  reorganise decides again).
- Nothing is deleted or overwritten; a name that is taken gets ` (2)`.

**Just reorganise what is there:** instead of building in another folder, choose *Just reorganise the ROM folders where they
are*. No destination is needed. Every ticked system gets the classic Build library with the shared rules (or its own): files
are renamed and sorted inside the system's own folder, what the rules leave out is set aside in `_excluded/`, `_superseded/`
and the like, files that match nothing go to `_unmatched/`, and nothing is deleted. Preview shows the counts per system,
Reorganise collection does it, and **Undo last build** uses each system's own undo log. Saves follow the renames if RetroArch is
set up (see below).

With Copy the ROM folders keep their files; with Move they give them up. Building again later adds only what is missing.

## RetroArch: saves, states and the config (v0.2)

**RetroArch** in the list on the left (`#/retroarch`). Optional: with no RetroArch found, and none chosen, nothing here matters.

- **Finding it:** the app looks for the Steam build (also in other Steam libraries), Flatpak, a normal Linux install and Snap,
  on Windows `%APPDATA%\RetroArch`, Steam and the usual portable folders, and on macOS the Application Support folder. You
  can also point at any `retroarch.cfg` (or its folder) yourself.
- **Right now:** where RetroArch keeps save files and save states, whether it makes one folder per core, whether saves sit next
  to the games, the BIOS folder and the games folder, read from `retroarch.cfg`. Core and game override files that set their
  own save folders are listed, because the global setting does not apply to those.
- **Saves and states:** choose the folder for save files, the folder for states (the same or separate) and whether each gets one
  folder per core. **Preview** lists every file that moves. **Move saves and update RetroArch** then
  1. writes a zip backup of those files (optional; the folder is yours to choose),
  2. moves the files, never overwriting one (a name already taken is left alone and reported) and removing folders it emptied,
  3. backs up `retroarch.cfg` and changes only its save settings.
  Each core keeps its own folder (`bsnes/`, `Flycast/` ...): nothing is merged. States and their `.png` thumbnails go to the
  state folder, everything else to the save folder.
- **One folder per core:** turning it off flattens the core folders. Turning it on cannot tell which core a flat file belongs to:
  those files are moved as they are and listed, since RetroArch will not find them until they are in their core's folder.
- **Safety:** the app will not change anything while RetroArch is running (it rewrites its config when it closes).
  **Undo last change** moves the files back and restores the config from its backup.
- **Shared folders:** assets you keep outside RetroArch (menu assets, content database, cheats, playlists, thumbnails, downloaded
  core assets, controller remaps, the config browser folder). Name the folder that holds them (the app suggests the one that
  holds your playlists or saves) and each setting shows whether RetroArch already uses the folder of that name there, uses a
  different one, or is on its own default. Ticked folders that exist but are unused (for example cheats that RetroArch still
  looks for in its own empty folder) are set in `retroarch.cfg`; the favourites / history lists follow the playlist folder.
  Nothing is moved, the config is backed up, and **Undo last change** restores it.
- **Saves follow the games:** RetroArch finds a save by the game's file name (`<name>.srm`, `<name>.state`, `.state1` ...,
  `<name>.state1.png`). When a library build renames a game, its saves and states are renamed to match, in the same core
  folder (cores stay separate). A build into another folder or a collection **copies** them to the new names and leaves the
  old ones. Nothing is overwritten (a file already at the new name is left alone). Undoing the build undoes the saves too.
  It waits if RetroArch is running (RetroArch writes its saves back when it closes). The switch is on the RetroArch page.
- **BIOS and firmware:** press **Check** and the app reads the `.info` file of every installed core, lists the BIOS / firmware
  each wants (required or optional) and compares them with RetroArch's system folder, verified by MD5 where the core gives a
  checksum. By default it covers the cores of **every system that has a ROM folder** (or choose *Every installed core*, or one
  system). Missing files are looked for in all those ROM folders, the Collection root and one more folder you can name: by name,
  and, when the checksum is known, under any other BIOS-looking name. It is one pass with progress (a minute at most; it says
  when it stopped early). **Place found files** moves (or copies) them to the path the core expects inside the system folder.
  Nothing is overwritten, nothing is written outside the system folder, and **Undo last change** puts them back.
- Only for the platform's disk DATs (for Amiga: Games, Workbench, Kickstart-Disks); a set
  never mixes disks from different DATs.
- Disks are grouped by their TOSEC name with the `(Disk X of Y)` part removed, so each
  version / crack / alternate of a game is its own set, e.g.
  `Game (1990)(Publisher)(Disk 1 of 3).adf` ... `(Disk 3 of 3).adf` -> `Game (1990)(Publisher).m3u`.
- A playlist is only written for a **complete** set (all disks present, built slot by slot -
  see *Build library*); incomplete sets are listed with the missing disk numbers.
- The `.m3u` is written next to disk 1 (after organising: inside the DAT folder) and lists the
  disks in order by their **current** file names (relative paths). With *Disk labels* on,
  entries are `file|Disk 1` etc. so the labels show in RetroArch's Disk Control menu.
  *Add save disk* appends a `#SAVEDISK:` line.
- Load the `.m3u` in RetroArch (PUAE core) instead of disk 1, then swap disks from
  Quick Menu > Disk Control.
- Existing playlists are only replaced if this app created them (first line
  `# Generated by simple-rom-organiser`); others are reported as `conflict`.
- Playlists reference current file names, so **organise first, then write playlists**.
  Organise and undo remove this app's playlists that would point at moved files; write the
  playlists again afterwards. Outdated playlists of this app (disks gone, or replaced by a
  playlist with a new name) are shown as `stale` and removed by **Write playlists**.
- Sets are matched flexibly: an untouched disk with a bare or shorter crack flag
  (`[cr]`, `[cr Galahad]`) joins `[cr CSL]` / `[cr Galahad v1]` disks. Disks with
  incompatible dump flags are never mixed (such families are reported incomplete), and
  excluded variants (`[b]`, pre-release ...) are never borrowed from.

## Kickstarts for RetroArch (PUAE)

PUAE needs Kickstart ROMs in RetroArch's system folder under fixed names such as
`kick34005.A500` (1.3), `kick40068.A1200` (3.1) or `kick40060.CD32` - see the BIOS table in
the [PUAE documentation](https://docs.libretro.com/library/puae/).

- Your scanned files are matched to that table by MD5 (taken from the TOSEC DAT entry each
  file matched, mainly *Commodore Amiga - Firmware*). Kickstarts inside zip / 7z archives work too.
- The destination list shows the usual SteamOS locations that exist on your machine:
  EmuDeck `~/Emulation/bios` (also on SD cards), RetroDECK `~/retrodeck/bios`, Flatpak
  RetroArch `~/.var/app/org.libretro.RetroArch/config/retroarch/system`, Steam RetroArch
  `~/.local/share/Steam/steamapps/common/RetroArch/system` and `~/.config/retroarch/system`.
  You can also type or browse to any folder.
- **Preview** shows each PUAE file as `copy`, `ok` (already there with the right content),
  `conflict` (a different file has that name - never overwritten) or `missing` (you don't
  have it). **Copy Kickstarts** copies (never moves) the files; the content is verified
  against the expected MD5 before writing.

### WHDLoad: its own Kickstart step

PUAE needs Kickstarts for WHDLoad too, but the WHDLoad system has **no DAT for them**: put your
Kickstart ROMs into a `Kickstarts/` folder inside the WHDLoad system folder (any sub-folders).
That folder is **protected** - it is never scanned, moved, set aside or counted as unmatched by
scan / organise / Build library. The step matches those files by MD5 against the PUAE table,
previews `copy` / `ok` / `conflict` / `missing` and lists every other file as `unmatched` (it is
reported, never copied). Copy never moves and never overwrites. The destination is remembered
**per system** (`kickstart_dests` in `config.json`; the single `kickstart_dest` of older versions is
migrated to Commodore Amiga only) and saved as soon as you pick it. The Commodore Amiga (TOSEC)
Kickstart step above is unchanged. Systems without a Kickstart source do not show the step.

## Building and installing

The main download is a single self-contained **AppImage**, about 18 MB:
`dist/Simple_ROM_Organiser-<version>-x86_64.AppImage`.

- It bundles its own CPython 3.13 (python-build-standalone) and the `romorg` package.
- It does not use the host's Python. At run time it needs only `fusermount`, which SteamOS has.
- A `.pyz` zipapp is a secondary option. It needs the host's `python3` (3.11+).

```sh
packaging/build_appimage.sh       # -> dist/Simple_ROM_Organiser-<version>-x86_64.AppImage
packaging/smoke_test.sh           # start it, check / and /api/status, then POST /api/quit
packaging/build_pyz.sh            # secondary: dist/simple-rom-organiser.pyz
```

**Build requirements:** `bash`, `curl`, `tar` and `sha256sum`. Python and FUSE are not needed to build.

**Download cache:** downloads (Python tarball, appimagetool, runtime) are cached in `packaging/.cache/`, so later builds work offline.

**Options:**

| Variable | Effect |
|---|---|
| `PY_SERIES=3.12` | Bundle Python 3.12 instead of 3.13. |
| `PBS_TARBALL=/path.tar.gz` | Use a local Python tarball. |
| `PRECOMPILE=0` | Ship `.py` sources only: about 15 MB, but startup takes about 0.6 s instead of 0.1 s. |
| `REFRESH_TOOLS=1` | Download appimagetool and the runtime again. |
| `ROMORG_CACHE=dir` | Use a different cache directory. |

**Rebuild after changing `romorg/`.** The AppImage holds a copy of the package from build time.

### Install on the Steam Deck (Desktop Mode)

```sh
packaging/install.sh              # newest AppImage in dist/ (builds one if there is none)
packaging/install.sh --rebuild    # rebuild first
packaging/install.sh --pyz        # install the .pyz to ~/.local/bin instead
packaging/install.sh --uninstall  # remove the app (your DATs and settings are kept)
```

The installer writes only under `$HOME`, so SteamOS's read-only root is not a problem. It installs:

- `~/Applications/Simple_ROM_Organiser.AppImage`, under the same name for every version
- a `.desktop` menu entry
- an icon

You can also just `chmod +x` the AppImage and double-click it.

### Running and quitting

- **Starting:** the app starts a local server on `127.0.0.1` with a random port and opens your default browser.
- **Quitting:** there is no terminal window, so use the **Quit** button in the top bar (`POST /api/quit`). Closing the browser tab does *not* stop the server.
  - Killing the process (`pkill -f romorg`, or stopping it in Steam) also stops it cleanly, and the FUSE mount goes away.
- **From a terminal:** `./Simple_ROM_Organiser-*.AppImage [--port N] [--no-browser] [--verbose]`.
- **Without FUSE** (containers, sandboxes): add `--appimage-extract-and-run`.
- **Log:** without a terminal, output goes to `~/.local/state/simple-rom-organiser/app.log`, which also shows the URL.

### Steam / Game Mode (best effort)

**Adding the app to Steam:**

1. In Desktop Mode, open Steam and choose **Games > Add a Non-Steam Game to My Library...**
2. Tick *Simple ROM Organiser*, or browse to `~/Applications/Simple_ROM_Organiser.AppImage`.

**Desktop Mode is the main target.** In Game Mode the browser window may not appear. If that happens, open a browser yourself at the URL in `app.log`. Setting the launch option `--port 8765` keeps that URL fixed.

When you are done, use **Quit** so that Steam sees the "game" exit.

More detail is in [docs/PACKAGING.md](docs/PACKAGING.md).

The UI is sized for the Deck's 1280x800 screen with large touch targets. The native
**Browse...** button uses `kdialog` (or `zenity`) and is only shown when available
(Desktop Mode); the **Folders...** browser works everywhere.

## Where data is stored

| Platform | Location |
| --- | --- |
| Linux / SteamOS | `$XDG_DATA_HOME/simple-rom-organiser` (default `~/.local/share/simple-rom-organiser`) |
| Windows | `%LOCALAPPDATA%\simple-rom-organiser` |
| macOS | `~/Library/Application Support/simple-rom-organiser` |

Inside it: `dats/` (extracted TOSEC DATs and `release.json`), `nointro/` (the No-Intro
DATs and their `manifest.json` - kept separate, so updating the TOSEC pack never touches
them), `cache/` (downloaded pack zip, hash cache), `updates.json` (when the DATs were last
checked), `whdload/` (the WHDLoad DAT and its `manifest.json`, again separate from the other two), `ratings/` (the LaunchBox ratings index, only when a rating filter is used), `redump/` (the Redump DAT) and `config.json` (last system, folder per system, library rules per system, Kickstart destination per system, and for the Dreamcast `chdman_path`, `chd_engine` = `auto` | `python` and `chd_workers` = hash processes, 0 = auto, `temp_dir` = scratch folder for chdman extractions, `temp_ram_reserve_mb` = memory kept free before RAM is used for them). All writes to `config.json` are serialised and applied to the latest file content, so concurrent actions (a scan finishing while you change a rule) never overwrite each other. Set `ROMORG_DATA_DIR` to use a different location. Undo logs and playlists
are written into your platform folder.

## Adding a platform

Platforms are defined in one place, `romorg/platforms.py`: a name, its DAT names in
priority order, where they come from (`tosec`, `nointro`, `whdload` or `redump`), the layout (`per_dat` folders,
`flat` or `game_folder`), which DATs get M3U playlists, the optional Kickstart / BIOS DAT, and for
consoles the alternative hashes to try (`snes_header`, `nes_header`, `n64_byteorder`) and
whether Convert is offered. The server and UI pick new entries up automatically. No-Intro
DATs are fetched from
`https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/no-intro/<name>.dat`.

## Where the DATs come from

- **TOSEC**: the latest pack zip from tosecdev.org; the version shown is the release date. The
  ~100 MB pack is downloaded only when the newest release date differs from the installed one.
  The new DATs and `release.json` are swapped in together, so an interrupted update never
  leaves a half-installed set (a download interrupted by a network error or by closing the app is
  resumed; cancelling it discards the partial file).
- **No-Intro**: the copies in the libretro-database repository on GitHub (not DAT-o-MATIC
  directly). The version shown is the DAT header's `version` (e.g. `2026.08.01`). Updates
  use the GitHub ETag, so unchanged files are not downloaded again; no GitHub API calls are
  made. Each download is validated (parsed) before it replaces the old file. Offline, the
  DATs already downloaded keep working.
- **WHDLoad**: only the one DAT, `Commodore - Amiga - WHDLoad.dat`, from
  `https://raw.githubusercontent.com/MrV2K/WHDLoad-Database/main/Commodore%20-%20Amiga%20-%20WHDLoad.dat`
  (the version shown is the header `date`, e.g. `2026-07-05`; checked with the ETag like the
  No-Intro DATs, validated by parsing before it replaces the old file, cached offline). It is
  stored in its own folder and never touched by TOSEC / No-Intro updates. **That repository
  states no licence: this app downloads only the DAT (a list of names and hashes), never any
  game file.** The DAT is Windows-1252 encoded; it is read as such.
- **Redump (Sega Dreamcast, Sony PlayStation, Sony PlayStation 2, Nintendo GameCube)**: `http://redump.org/datfile/dc/`, `.../psx/`, `.../ps2/` and `.../gc/` over **plain HTTP** (HTTPS is refused by
  the site). One HEAD request per start reads the version from the `Content-Disposition` file name; the zip
  (about 0.7 MB, one `.dat`) is downloaded only when that date is newer, validated by parsing, and replaces the
  old DAT atomically; it is stored in its own `redump/` folder. Offline, the installed DAT keeps working.
- **LaunchBox ratings** (only with a rating filter): `https://gamesdb.launchbox-app.com/Metadata.zip`; one HEAD request
  (`Last-Modified` / `ETag`) at most per start, and only when the installed index is older than 7 days; the zip is
  downloaded only when newer, stream-parsed into `ratings/ratings.sqlite`, validated and swapped in atomically, then
  deleted. **Credit: "Ratings: LaunchBox Games Database community ratings"** (shown in the rules panel and in
  `docs/THIRD_PARTY.md`).
- **Updates and scans.** The update runs on its own background thread and never blocks the UI.
  It never interrupts a running scan: a scan works on the DATs it loaded at its start, and if
  new DATs are installed afterwards the page says *DATs updated - rescan*.

## Security

The server only listens on `127.0.0.1`, rejects requests with an unexpected `Host`
header, and requires a per-run secret token for every action that changes anything,
so other websites open in your browser cannot drive it.
