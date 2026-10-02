# Simple ROM Organiser

A small local web app that checks ROM folders against DAT files and tidies them up.
Supported systems:

| System | DATs | Source |
| --- | --- | --- |
| Commodore Amiga | 4 [TOSEC](https://www.tosecdev.org/) DATs (Games ADF, Workbench, Kickstart-Disks, Firmware) | TOSEC pack from tosecdev.org |
| Nintendo Game Boy Advance | *Nintendo - Game Boy Advance* | No-Intro, via libretro-database |
| Nintendo 64 | *Nintendo - Nintendo 64* | No-Intro, via libretro-database |
| Nintendo Entertainment System | *Nintendo - Nintendo Entertainment System* | No-Intro, via libretro-database |
| Super Nintendo Entertainment System | *Nintendo - Super Nintendo Entertainment System* | No-Intro, via libretro-database |

Every system has its own folder (e.g. `.../roms/amiga`, `.../roms/snes`). The app matches
every file against the system's DATs, shows what you have and what is missing, renames
and sorts the files, and - for the Amiga - writes M3U playlists for multi-disk games and
installs Kickstart ROMs for RetroArch.

What it does:

1. Keeps the DATs current **automatically**. At every start it checks, in the background,
   the newest TOSEC release (<https://www.tosecdev.org/downloads>, ~100 MB, Amiga; downloaded
   only when its release date differs from the installed one) and the four No-Intro DATs in the
   [libretro-database](https://github.com/libretro/libretro-database/tree/master/metadat/no-intro)
   mirror on GitHub (a few MB, consoles; only changed files are fetched). Nothing to download by hand.
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
  Untick **Move unmatched files into `_unmatched/`** to leave them where they are.
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

## Using it

1. **Systems & folders** - at the top, the **updates line** shows the installed DATs
   (`TOSEC 2025-03-13 · No-Intro 2026.08.01 · checked 14:02`); a progress bar appears while
   something is being updated. The small **Check for updates** button repeats the check on
   demand (it becomes **Cancel update** while running). Offline you get a quiet note and the
   installed DATs are used; with no DATs installed and no network a clear error with **Retry**
   appears. Below, one row per system: its DAT source (TOSEC / No-Intro), the local DAT
   version, and its folder. Type the path or use **Browse...** / **Folders...**, then **Save**,
   or press **Scan** on the row (scanning remembers the folder too). Scanning a system whose
   DATs are not installed yet simply waits for the automatic update (its progress is shown in
   the scan's progress line). After an update the page says *DATs updated - rescan*.
2. **Scan** - shows overall cards (have / missing / % complete, matched, unmatched,
   duplicates; for consoles counted in **games**) and, for the Amiga, one card per DAT.
   Tabs: **Games** (consoles: every game of the DAT, green = have, red = missing, with
   *Have* / *Missing* chips), **Matched files**, **Missing**, **Unmatched**. Rows show
   region / language / status chips; **bad dumps show the exact flag from the DAT name**
   (`Bad dump [b corrupt file]`), and other names a library rule would set aside show e.g.
   `Modified [m baddump]` or `Pre-release (beta)`. The chip rows above the table filter by
   region, language, video standard (PAL / NTSC), tag (`Beta`, `Proto`, `Unl`, `Bad dump`, ...)
   and *Library rules*. Filters only change what is shown. **Duplicates** counts the extra
   copies Build library would set aside.
3. **Build library** - the **Library rules** panel (per system, remembered) and then
   **Preview library**, **Build library**, **Undo last**. The preview shows cards by reason
   (kept, renamed / moved, excluded, superseded, incomplete, duplicates, playlists to
   write / remove, conflicts, games that vanish), reason and status filter chips, a count of
   excluded files per reason (bad dump, language, cracks off, ...; click one to list just
   those), a table with the reason and the exact flags, the list of incomplete sets with
   their missing disk numbers and the **Games that vanish** list (titles none of whose
   versions stay, searchable, with one-click fixes such as "+ German").
   **Build library** asks for confirmation, moves the files, writes the playlists, saves one
   undo log and re-scans; building again on a built library does nothing (empty preview).
   *Advanced: tidy only / playlists only* keeps the plain Organise (rename and sort, with
   duplicates handled but no library rules) and M3U steps.
4. **Convert to No-Intro format** (SNES, N64) - optional, see below.
5. **Kickstarts** (Amiga) - optional, see below.

Steps a system does not use are hidden.

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
A title with no version in a ticked language is left out; the **Games that vanish** list
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
Japan, then every other region A-Z; reorder with the up / down buttons; a multi-region release
ranks by its best region), then the newest revision, then the fewest extra tags. Different
products stay separate (`(Unl)`, `(Aftermarket)`, `(Pirate)`, `(Beta)`). Switch it off to keep
the latest version of every region instead. These two settings are hidden for Amiga.

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

**Undo.** One undo log covers the moves and the playlists: **Undo last** moves the files
back, removes the playlists the build created (only if unchanged) and restores the outdated
playlists it deleted.

With the rules off (or in *Advanced: tidy only*) the older behaviour remains: **Latest
version only** moves older versions to `_superseded/` using the same ranking as
above for consoles.

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

## M3U playlists (multi-disk games)

Written following the
[libretro PUAE documentation](https://docs.libretro.com/library/puae/#m3u-and-disk-control):

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
checked) and `config.json` (last system, folder per system, library rules per system, last
Kickstart destination). Set `ROMORG_DATA_DIR` to use a different location. Undo logs and playlists
are written into your platform folder.

## Adding a platform

Platforms are defined in one place, `romorg/platforms.py`: a name, its DAT names in
priority order, where they come from (`tosec` or `nointro`), the layout (`per_dat` folders
or `flat`), which DATs get M3U playlists, the optional Kickstart / BIOS DAT, and for
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
- **Updates and scans.** The update runs on its own background thread and never blocks the UI.
  It never interrupts a running scan: a scan works on the DATs it loaded at its start, and if
  new DATs are installed afterwards the page says *DATs updated - rescan*.

## Security

The server only listens on `127.0.0.1`, rejects requests with an unexpected `Host`
header, and requires a per-run secret token for every action that changes anything,
so other websites open in your browser cannot drive it.
