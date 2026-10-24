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
| Nintendo Game Boy, Game Boy Color, Nintendo DS | *Nintendo - Game Boy*, *Nintendo - Game Boy Color*, *Nintendo - Nintendo DS* | No-Intro, via libretro-database |
| Sega Mega Drive - Genesis, Master System, Game Gear, 32X | *Sega - Mega Drive - Genesis*, *Sega - Master System - Mark III*, *Sega - Game Gear*, *Sega - 32X* | No-Intro, via libretro-database |
| Atari Lynx | *Atari - Lynx* (the DAT lists the headered `.lnx` and the raw `.lyx` / `.bll` dumps as separate roms of one game: both match) | No-Intro, via libretro-database |
| Sega Dreamcast | *Sega - Dreamcast* (1516 discs, Redump) | [redump.org](http://redump.org/) (plain HTTP only) |
| Sony PlayStation | *Sony - PlayStation* (10,914 discs, Redump) | [redump.org](http://redump.org/) (plain HTTP only) |
| Sony PlayStation 2 | *Sony - PlayStation 2* (11,774 discs, Redump) | [redump.org](http://redump.org/) (plain HTTP only) |
| Nintendo GameCube | *Nintendo - GameCube* (Redump's list, as libretro's mirror has it: no betas, prototypes or demos); files are `.iso`, `.gcm` or Dolphin `.rvz` | libretro-database, `metadat/redump` |
| Nintendo Wii | *Nintendo - Wii* (Redump's list, as libretro's mirror has it, with each disc's serial); a plain `.iso` is hashed, Dolphin's `.rvz` `.wia` `.wbfs` `.ciso` are identified by the game code in their header | libretro-database, `metadat/redump` |
| Nintendo Wii U | *Nintendo - Wii U*, made from GameTDB's list (Redump has none): a catalogue with no checksums; `.wux` / `.wud` are told by the product code | [gametdb.com](https://www.gametdb.com/) |

Every system has its own folder (e.g. `.../roms/amiga`, `.../roms/snes`). The app matches
every file against the system's DATs, shows what you have and what is missing, renames
and sorts the files, and - for the Amiga - writes M3U playlists for multi-disk games.

**WHDLoad installs (Amiga).** The Amiga's Files page lists, under **WHDLoad installs**, which of your games has a WHDLoad
install on [whdload.de](https://www.whdload.de/) - 2,843 game installs; about two thirds of a typical TOSEC collection has one. An
install is a game's WHDLoad slave, installer script, icons and ReadMe (some hundred KB, no game data); it installs the game from its disks
to a hard drive. **Download** one, or **Download all**: each is unpacked next to the game's disks, in `<game> (WHDLoad install)/` (with a
small `.romorg-whdload.json` note), checked first (a real LhA archive, every file's CRC). Scans, builds and the counts leave those folders
alone; the Files tree shows their files as *WHDLoad install*. A game is matched by its title, the same letters (`Push-Over` /
`Pushover`), another title of the game (`Die Siedler` / `The Settlers`) or its subtitle / main title; a data, course or scenario disk gets
none. The list of installs is fetched with the other databases and kept for a week. Most installs need the game's **original** disks
(WHDLoad's RawDIC reads their own disk format): the ADFs of a TOSEC collection are mostly cracked copies, which such an installer may not
accept. (The separate *Commodore Amiga - WHDLoad* system of v0.2, which matched Retroplay's pre-installed packs, is gone.)

**Prefer versions WHDLoad can install** (Amiga rule, on by default). Of a game's versions, the one its WHDLoad install can be made from
is kept - after your language order, before cracked / chipset / newest. **Check which versions WHDLoad can install** (WHDLoad installs,
on the Files page) finds them: for every game with an install and more than one version it fetches the install (kept in
`whdinstalls/packages/`) and runs the install's own script here, on each version's disks (`romorg/amigainstall.py`: the Amiga
Installer language, the floppies it asks for are your ADFs by disk label or by a file on them). A version whose disks are not AmigaDOS
disks (a crack with its own loader), or whose install reads the original disks' own format (RawDIC), or needs an Amiga program
(xfdDecrunch, Patcher), is no WHDLoad version; the reason is kept (`whdinstalls/ready.json`). The check takes a while (one install
per game, a pause between downloads).

**Sega Dreamcast, Sony PlayStation and Sony PlayStation 2 (Redump)** are the *folder per game* systems (one shared engine): `<system folder>/<Redump name>/<Redump name>.chd`
plus the sidecar files next to it (see [Sega Dreamcast: CHD + Redump](#sega-dreamcast-chd--redump) and
[Sony PlayStation / PlayStation 2](#sony-playstation--playstation-2-chd--redump)). CHD files are
matched per track against the Redump hashes by a built-in pure-Python CHD reader (or by `chdman` when you
have it), raw Redump sets (`.gdi` / `.cue` + track files) are recognised too and can be converted to CHD.

What it does:

1. Downloads the databases itself - **once**. On the **first start** it fetches, in the background, every database that is missing:
   the newest TOSEC release (<https://www.tosecdev.org/downloads>, ~100 MB, Amiga), the No-Intro DATs of the
   [libretro-database](https://github.com/libretro/libretro-database/tree/master/metadat/no-intro) mirror on GitHub (a few MB each,
   consoles), the Redump DATs (Dreamcast, PlayStation, PlayStation 2 from redump.org; GameCube and Wii from libretro's mirror of them) and the lists the Wii U and Switch catalogues are
   made from (GameTDB; the title database `blawar/titledb`, about 100 MB). **Later starts ask the network for nothing**; press
   **Check for updates** (More menu) to look for newer ones and fetch them. A system whose DAT is missing fetches it when you scan it.
2. **Systems & folders**: one row per system with its DAT status and folder (type it, or use the built-in folder
   browser, **Folders...**, with shortcuts for Home and drives). Each folder is remembered.
3. Scans a folder (always including subfolders) and shows how many games / DAT entries you
   have, which are missing and which files matched nothing - with region, language and tag
   chips and filters.
4. **Build library** (one preview, one apply): files are renamed and sorted - Amiga
   files into one folder per DAT under their exact TOSEC name, console files under their exact
   No-Intro name, flat in the console folder. Following the **library rules** (each can be
   switched off; the panel shows the exact TOSEC / No-Intro tags behind every rule and how
   many files it moves): bad dumps, betas, demos and other unwanted variants
   are archived, only the languages you tick are kept (English by default), only the latest
   version (and for Amiga games the one best variant, for consoles one version per game in
   your region order) is kept,
   incomplete multi-disk games and duplicate copies are archived, and playlists are written
   for the complete multi-disk sets. Everything archived goes to its own folder (`_excluded/`, `_superseded/`, `_incomplete/`,
   `_duplicates/`, next to the games) - nothing is ever deleted.
5. Optionally converts copier-headered SNES dumps and byte-swapped N64 dumps to the clean
   No-Intro format (originals kept).
6. BIOS and firmware: that page checks the BIOS / firmware (Kickstarts included) every installed core expects and copies what you have.

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
```

- Matched files anywhere under the console folder (subfolders, `_unmatched/`, old folders)
  are moved up into the console folder itself under their exact No-Intro name - except files
  that still qualify for a reason folder (`_excluded`, `_superseded`, `_incomplete`,
  `_duplicates`), which stay there.
- Organise **never changes file contents**, so the extension stays honest: a SNES file
  matched with its 512-byte copier header skipped is named `<game>.smc`, a byte-swapped
  N64 file `<game>.v64` / `<game>.n64`, a NES file whose iNES header differs `<game>.nes`.
  Turn on the rule **Convert when building** (Settings) to get the exact DAT files.
- `.zip` / `.7z` archives with one matching game are named `<game>.zip` / `<game>.7z`
  (the file inside is unchanged).
- Unmatched files keep their relative path under `_unmatched/`. Folders named `media`,
  `images`, `videos`, `manuals`, `downloaded_images`, `snap` or `boxart` are left in place.

### Reserved folders

The app owns exactly these folder names, all directly under the platform folder (compared
case-insensitively, since SD cards are often exFAT): `_unmatched`, `_excluded`, `_superseded`,
`_incomplete`, `_duplicates`, `_converted_originals`. Inside each, a file keeps its path
relative to the platform folder. **Do not use these names for your own folders**: a file
inside one is treated as already archived for that reason - if it is a ROM that matches a DAT
and no rule excludes it, the next organise / Build library moves it back to its canonical place,
and if it is excluded it simply stays.

Libraries built by an older version kept these folders *inside*
`_unmatched/` (`_unmatched/_excluded/` ...). The next Build library recognises them and moves
the files to the new top-level folders (or to their canonical place if they now qualify) as
ordinary previewed moves.

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
  to `_unmatched/`, the confirmation repeats the number.
- Left in place even when unmatched: files in the folder of a DAT that is not installed
  (so a missing DAT never empties an organised folder), frontend / emulator files
  (`gamelist.xml`, `systeminfo.txt`, `metadata.txt`, `rom.key`, other `.key` files) and
  symbolic links. A hidden file is one whose name starts with a single dot (`.nomedia`): it is never looked at. A name that starts with
  several (`...GAMELIST.txt`, a ROM pack's list) is an ordinary file. **Anything else that is not a checksummed ROM is set aside** (saves, save states, emulator configs, notes), keeping its path under
  `_unmatched/` (or the archive folder).
- The preview warns (and Apply asks twice) when most files match nothing or other systems'
  folders (`snes/`, `psx/` ...) would be moved - i.e. when the folder is too high up. The
  home folder, `/` and the root of a drive / SD card cannot be scanned at all.
- No empty folder is left behind: every empty folder below the platform folder is removed after a build, also one that was
  already empty (never the platform folder itself, nor the system's own folders such as the Kickstarts).
- Hidden files are never moved. Playlists created by this app whose
  disks move are removed (the build writes them again).
  Playlists you made yourself are left where they are; the preview tells you when their
  disks move.

## Running from source

```sh
python3 -m romorg                 # opens your browser at http://127.0.0.1:<random port>/
python3 -m romorg --port 8765     # fixed port
python3 -m romorg --no-browser    # just print the URL
python3 -m romorg --no-update     # do not download anything at startup, not even what is missing
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

**The system list** is on the left of every screen (v0.2): the systems grouped into
*Computers*, *Cartridge consoles* and *Disc systems* (the groups come from the app, nothing is hard-coded in the page).
Each system has a dot (green: scanned, amber: its DAT is missing, hollow: not scanned) and the % you own; hover for the
folder, DAT and last scan. A blue **Custom rules** tag (like the saves count) marks a system with library rules of its own (hover for which). A running scan shows its progress under the system. **Find a system** filters the list.
Clicking a system opens its one page on the right, with its **Files / Settings** tabs, and the tab you are on stays
selected when you switch system. **Hide list** collapses the list (remembered); in a narrow window it becomes a **Systems**
button above the page. **More** in the header holds Check for updates, Compact rows and Quit; **Databases** (in the list on the left, with Settings) lists every database the app uses: its address, the systems that use it, the installed and newest version. The last scan
summary of every system is a tiny record in `config.json` (counts, time, folder - never file lists), so the list survives a
restart. The full result of each scan is also saved (`<data folder>/scans/<system>.scan`), so Browse and Library are there when
you open a system after a restart, with no scan: while the folder (files, sizes, newest change), the databases and the scan options
are what they were. Change any of them and the saved scan is not used (a rescan reads only what changed, so it is quick).
Opening `#/` goes to the system you used last.

**A system page (`#/system/<slug>/files`)** is one page with two tabs, **Files** and **Settings**. (There used to be Overview, Library,
Browse and File tree; their addresses still open the page. Where the text further down says *Overview*, *Library tab* or *Browse*: what
you look at is on **Files**, what you set is on **Settings**.)

**Files** is the system as a build would leave it, top to bottom:

* the **tiles** (below): the totals you chose in Settings, and the lists behind them (upgrades, excluded, incomplete);
* the **rules bar**: *Your default rules* or *Custom: ...*, a one-line summary of what the rules do
  (`4 of 4 exclusions · English · Europe > USA first · latest versions only`) with a **Change** link to the Settings tab, a status that
  says whether the tree below is up to date, and **Build library**;
* **the file tree**, with two tabs above it: **ROM folder** and **Archive** (each with its real path, file count and size). It is what
  the folder and the system's part of the archive would hold after a build with the current rules, opened folder by folder; a folder says
  how many files and bytes are below it, how many the build changes and how many games are missing. A file is marked *renamed*, *moved*,
  *new* (a playlist), *set aside* (goes to the archive with this build), *copy*, *saves* or *not looked at* (a hidden file, an emulator's own
  folder: it stays). A game you do not have is listed greyed where it would be;
* **the filters** above the tree, the ones Browse had: a search box and chips for **Show** (everything, games you have, missing games,
  BIOS and firmware, unmatched files, saves), **What the build does**, **Region**, **Language** and **Rated**. The folders keep their counts
  for what passes the filters, and the address keeps the filters (`#/system/<slug>/files?show=missing&region=Europe`);
* **press a file** and its details open under it: the ROM's name and checksums (CRC32, MD5, SHA-1, each with a copy button), its
  rating, why the rules keep or leave it, its saves, and the per-game choices (always keep / always exclude / use this version);
* **Saves** (collapsed, at the foot): the emulator's saves of this system, by game.

The tree **updates by itself**: a scan, or any change of a rule, option or folder, recalculates it about half a second later (the old tree
stays, dimmed, and **Build library** waits for the new one). Build library sends the identity of the plan you looked at, so the app refuses
(409) a plan that no longer matches the saved rules. Without a scan the page says *Scan first* with a button to the Settings tab.

**Settings** holds everything you can set for the system: the **folder** (**Folders... / Clear**, saved the moment you choose it) and
**Scan folder**, the **library rules** (every exclusion, language, keep-flag, region priority and option, with an Override box each), the
**build options** (layout, labels, where to build, the system's own archive folder), the **DAT files**, and the scan's summary
cards and checks.

Gone with the old tabs: the sortable table and its **Columns** menu (the tree is by folder and name; the filters and the search do the finding).
Converting is the library rule **Convert when building** (Settings, for every system that can convert; a system can override it), and the CHD settings are on the **Disc images (CHD)** page in the list on the
left. Kickstarts and other BIOS files are handled by the **BIOS and firmware** page.

### Library totals and the tiles

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
It lists **DAT (No-Intro / TOSEC / Redump)** rows and **Your file** rows in monospace
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
  identified by length says *length only* until the *Check every track while scanning* switch (Disc images page) hashed it;
* multi-disk Amiga sets: the disks of the set, each with DAT and local hashes, missing disks marked;
* missing games: DAT checksums only; unmatched files: *Your file - no match* with their own hashes.

## How matching works

- Files are matched by **content**, never by name. Loose files are hashed (CRC32 + SHA-1)
  and matched by SHA-1, falling back to CRC32 + size.
- `.zip` archives are matched by the CRC32 + size of their members (read from the zip
  directory, no extraction). `.7z` / `.rar` work if the `7z` command is installed;
  otherwise they are listed as *unsupported* (and treated as unmatched when organising).
- Hashes are cached (keyed by path, size and modification time; a file that was only moved or renamed is recognised by its
  inode and is not read again), so re-scans are fast. **Rescan folder** reads only new and changed files; **Recalculate
  checksums** (next to it) reads everything again. A scan you stop keeps what it had read:
  scanning again carries on from there. The server keeps the scan of the last six systems you scanned, so going to another system
  and back needs no new scan, and a restart does not need one either while nothing changed (see above). Otherwise the scan after
  a restart is quick, because the hashes are cached.
- Hidden files and `.m3u` files are ignored (the undo logs of versions before 0.4 are deleted when a folder is scanned). Leftovers of an
  interrupted move (`*.romorg-tmp-*`), partial output of an interrupted Convert
  (`*.romorg-convert-*`) and dangling symbolic links are listed as errors. The next **Build library** cleans up after a run that
  was killed by itself: partial files are deleted, a file left under a temporary name is renamed back, and an original whose
  conversion was cut short is put back and converted again.
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
without version tokens plus its edition flags; the year, the language and bare chipset tags (`AGA`,
`OCS`, `ECS`, `CD32`) are not part of it, so an OCS and an AGA release of the same game compete. With
**Combine alternate titles** (on by default) the country, the publisher and the game's other titles
(`Another World` / `Out of This World`, `Anstoss` / `On the Ball`) are not part of it either: the German
release of another publisher competes too, and the language filter then sets aside a game that has no
version in your languages (off: every country and publisher is a game of its own). `PAL` / `NTSC` and `(M3)` releases are the same game
too. A few titles are two different games by different publishers
(Overlord, Arena, Legend ...): `library.KEEP_APART` keeps them apart, and `tools/keep_apart_candidates.py` lists the titles LaunchBox knows
as two Amiga games to check new ones by hand. The disks of a set still come from one release. Among the *complete* sets of a game exactly one is kept, ranked:
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
rules). The whole DAT decides, not the files you own: if an English version of the game exists in the DAT,
your Japanese copy is still left out, and the English one is "missing". Among several versions of such a game the usual
region and revision order picks one. Turn the option off to have every title without a version in your languages left out.
TOSEC keeps the rule below.

A TOSEC title with no version in a ticked language is left out; the **Games that vanish** list
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

**Equally good regions.** Press **=** on a region to rank it the same as the region above it. With Europe and USA tied
for first (both shown as 1), a version from either is a best match, and the newest revision of the two is kept. It is saved
as `region_ties` beside `region_priority`, in your defaults (Settings) or in a system's override.

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
platform (Amiga, GBA, GB, GBC, DS, N64, NES, SNES, Mega Drive, Master System, Game Gear, 32X, Lynx, Dreamcast, PlayStation, PlayStation 2). It is **off by default - nothing changes and no
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

**Ratings are kept up to date at startup**, with or without a rating filter (a check is only a HEAD request; the download happens
when the local copy is over 7 days old and LaunchBox's is newer, or when a system is missing from the installed index). LaunchBox
covers the Nintendo systems too (NES to Switch, GameCube, Wii, Wii U); its Switch coverage is thin.

**Tiles.** The row at the top of every system's Library page is the same for every system and is chosen in **Settings > Tiles on the
system pages**: tick up to ten of sixteen and drag them into order. Have (all entries / with your rules), Rated (both), Needs a
look (opens the upgrades, left-out and incomplete lists), Files to change, Unmatched files, BIOS and firmware, Saves, Database, Last
scan, and four that need a calculation of their own (`GET /api/library/insights`): Space (database sizes), Best you are missing,
Average rating, Regions, and Convert (disc systems). A tile with nothing to say for a system (no saves, nothing unmatched, no ratings)
is left out of that page. The choice is `page_tiles` in `config.json`.

**Files next to a disc (disc systems).** On Dreamcast, PlayStation and PlayStation 2 the files in a game's folder that are not its disc
image (a save or save state, a `.zip`, a checksum file) are **not part of the game**: by default Build library sets them aside like any file that
matches nothing, but a save, save state or memory card goes under its game: `<archive folder>/<system folder>/_saves/<Title>/<file>` (or `_saves/` in the
ROM folder while no archive folder is set); the rest goes to `_unmatched`. The rule **Keep other files that
sit with a game** (Settings, overridable per system; off by default) keeps them with the game and renames them with it, as earlier versions
always did. Playlists, `gamelist.xml` and media folders always stay. Saves in RetroArch's own save folders are a different thing: see
*What happens to the saves*. Likewise loose saves and states in any system's folder are set aside when no game claims them.

**Copy a name or a checksum.** In the upgrades list and in every checksum panel (Browse, the Library list's details) a file name,
CRC32, MD5 or SHA-1 is a button: press it and it is on the clipboard, ready to search for. The app itself never downloads games.

**Saves in a ROM folder go under their game.** A save file, save state or memory card lying in a system's ROM folder (not in RetroArch's own save
folders) is not a ROM, so the build sets it aside - under the game it belongs to: `<archive>/<system>/_saves/<Title>/<file>` (the title without
region, version or disc, so every edition shares a folder). A file is pinned to a title when its name is a game's (`Game (USA).srm`,
`.state1`, Flycast's `.A1.bin`), when it sat in that game's own folder, or when it is a memory card that names its games (a GameCube card by its
game codes, a Dreamcast card by a title only one game has). A card that holds several games is **moved under the first and copied under the others**.
A save no title claims goes to `_unmatched`. These rows are the **Saves** reason of the Library list.

**Saves of games you have no file for** are listed in the Files tree too (Show: Saves). Each sits on the release your library rules
would choose among those the save works in: the same region; any region for a cartridge system; for the PlayStation 2, another region
it can be converted for (below). It never moves to another disc or mode of a game (Disc 1 is not Disc 2, Arcade Mode is not
Simulation Mode). Its details say which release the save is named for ("The save is named for ...") and list the other releases it
fits. A save whose title is a game you *do* have goes with that ROM (the one release your rules prefer, when you have several).
Titles are compared without articles, accents, ™ and punctuation ("The Legend of Zelda™: Breath of the Wild" is "Legend of Zelda,
The - Breath of the Wild"), then as another spelling would have them ("Okami", "Ookami"). Saves the emulators name by a game code
(PCSX2, Dolphin, Cemu, Eden / Ryujinx) get the title the game database or the emulator gives that code.

**Dreamcast cards named by the disc's number.** Flycast can keep one memory card per game, named after the disc's product number
(`HDR-0128.A1.bin`, `T36802N.A1.bin`). The Dreamcast database has no product numbers, so the app reads the number from the boot
header of each disc you have (`.chd` or `.gdi`, remembered per file): a card with that name is that disc's save, shown with it.

**Convert a save for another region** (PlayStation and PlayStation 2, experimental): a game you have with no save of its own, whose
title has a save from another region, shows **Convert the save of ... for this disc** in its details (the disc's code is read from the
`.chd` / `.iso`). A PlayStation 2 save of a game you lack, shown on another region's release, offers **Convert SLUS-xxxxx →
SLES-xxxxx** (the code from the game database). The copy is made next to the original (a PlayStation card copy named after the disc;
a PCSX2 folder card's save folder copied under the new code; a card image rewritten in place, its old content kept with the save backups). Not while the emulator runs.

**The file tree** is the Files tab of every system (above): the ROM folder and the archive as a build would leave them, made from the
system's scan and following the rules. It is a preview: looking at it moves nothing.

**Saves are counted for the system they belong to.** A RetroArch core that plays several systems keeps their saves in one folder (Genesis Plus GX:
Mega Drive, Master System, Game Gear). A save that matches nothing in the system you are looking at but is a game (or, read loosely, the one title)
of another system the core plays is that system's: it is not counted or listed as *unmatched* here.

**Saves whose name is not quite a game's.** A save that matches no ROM and no database name is read again, loosely: without a `- Copy` at the
end, square-bracket flags and the region / language / version / revision tags of any naming scheme, and then without a scene release number in front
(`2375 - Sonic Advance (J) (M2) (v1.1)`, `Yoshi's Island (USA, Asia) (Rev 1)`, `Tony Hawk's Pro Skater 2 (USA) - Copy`). When what is left is **exactly
one** title of the system's database, the save is **told by its title** (Browse > Saves says *title only*; in a ROM folder it goes to `_saves/<Title>/`).
A title that begins with a number keeps it (`007 - The World Is Not Enough`), and two titles that differ only in punctuation are told apart by the
exact spelling or not at all. Such a save is known by title, not by edition: it is never renamed with a ROM. The other files of a save set (the empty
memory cards B, C, D of a game) follow the one that names the game.

**Memory cards: the games inside a save.** A card file holds the saves of the games on it, whatever it is called, so the app reads the card's
directory (`romorg/memcards.py`, stdlib only): **PlayStation** cards (RetroArch's `.srm`, `.mcr`, `.mcd`, DexDrive `.gme`, PSP `.vmp`: the
product code `SLUS-01234` and the game's title), **Dreamcast VMUs** (Flycast's `.A1.bin`: what each file on the card is), **GameCube** cards
(`.raw`, `.gcp`: the game codes) and **PlayStation 2** card images (the serials). Browse > Saves lists them under each save set, and a set that
matches no ROM but whose card names a title of the database says **looks like** that title (on your Dreamcast, `MK-51000` is Sonic Adventure and
`T36802N` is Soul Reaver). Cartridge saves and save states carry no game name, so only their file name tells. Nothing is written or moved.

**Lists behind the hints.** The Overview's three hint lines each have a **Show** button: the games you own that are not the preferred
version (with its checksums), the games your rules leave out (and why), and the multi-disk games with disks missing
(`GET /api/library/upgrades?kind=upgrades|excluded|incomplete`). Above the totals, **Library rules:** says whether the system follows
your defaults or has rules of its own, and which.

**Upgrades.** When the Overview says some of the games you own are not the preferred version, **Show the upgrades** lists them: the
game, the file you have, and the name, CRC32, MD5 and SHA-1 of the version your rules prefer (`GET /api/library/upgrades`).

**Rated tiles.** Overview and Library show two more tiles for the platform, under the totals: the share of titles with a rating,
before the filters (all DAT entries) and after them (with your library rules), like the other counts.

**Your own IGDB key (optional, Settings > Ratings).** [IGDB](https://www.igdb.com) rates many titles LaunchBox lacks (most of the
Switch). Make a free application at dev.twitch.tv/console, paste its Client ID and Client secret, press **Test**, then **Save**. The
ratings are read into `ratings/igdb.sqlite` (refreshed weekly) and, per title, the rating with more votes wins. The secret is stored
**in plain text** in `config.json` and is never sent back to the browser. Nothing is shipped with the app; no key, no IGDB traffic.
Not yet tried against the live service.

**There is no Undo** (removed in 0.4). A build renames and moves, never over another file: what the rules set aside is in the
reason folders or the archive, and changing a rule and building again moves it back. The only files ever deleted are the originals of a
conversion or a 7z repack, once the new file has been checked against the database (the rules "Delete the originals after converting"
and "Compress games as 7z").

With the library rules switched off the older behaviour remains: **Latest
version only** moves older versions to `_superseded/` using the same ranking as
above for consoles.

## Nintendo GameCube: ISO and Dolphin RVZ

A flat folder with one file per disc (`.iso`, `.gcm` or `.rvz`), matched against *Nintendo - GameCube* (Redump's list as libretro's
mirror has it, `metadat/redump`; about 2,000 games). Redump hashes the ISO, so a `.rvz` (Dolphin's lossless compressed format) is **rebuilt in memory while it
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
  Shenmue (Europe) (En,Fr,De,Es)/                           multi-disc: one folder per game, a folder per disc inside it,
    Shenmue (Europe) (En,Fr,De,Es) (Disc 1)/...             the playlist (.m3u) in the game's folder
  _unmatched/  _excluded/  _superseded/  _duplicates/  _converted_originals/    the usual reserved folders
```

*Organise* / *Build library* rename the game **folder**, the CHD and every sidecar to the Redump name (so save
states stay linked to the game; they are never deleted or rewritten). Everything else in the folder keeps its
name and moves along with the folder. A loose `.chd` in the system folder gets a new `<Redump name>/` folder with
its sidecars (files that start with its name). A CHD that matches no Redump entry moves **with its whole folder**
into `_unmatched/<folder>`; other unrelated files are handled by the usual unmatched rules (frontend files such as
`gamelist.xml` / `media/` stay). Two copies of one game: the one with more files (your saves) stays, the other
moves whole to `_duplicates/`. Every step is previewed first and never overwrites.

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
Redump (by chdman, or with *Check every track while scanning*) the level is **verified**. A CHD matches a game only if every
`.bin` track matches. Results are cached per file (path, size, modification time and the CHD's own header SHA-1),
so a rescan of an unchanged CHD never decodes it again; the cache is in the data folder, scanning never writes
into your game folder.

| Level | What was compared | How |
| --- | --- | --- |
| **identified** | track sizes + crc32 / md5 / sha1 of every data track; audio by length | built-in reader (default) |
| **verified** | every track, audio included | *Check every track while scanning* (built-in reader, parallel), or chdman when the reader cannot decode the CHD |
| **raw (convertible)** | an unpacked Redump set, every track file hashed | files, no CHD involved |

**Engines and speed (Amendment 14).** The **built-in reader** decodes first (`auto` = the default): a parallel scheduler
splits every track of every CHD into ~4 MB chunks, decodes them in one worker process per CPU thread (`chd_workers` in
`config.json` or `ROMORG_CHD_WORKERS`; `1` = the old sequential in-process path, handy for debugging) and hashes them in
order (crc32 + md5 + sha1 in parallel threads). Memory stays bounded (at most 256 MB of chunks in flight; fewer
workers when the machine is short of RAM). FLAC audio (`cdfl`) is decoded by the system's / the bundled **libFLAC through
ctypes** (about 80-100 MB/s per core instead of 1.2 MB/s in pure Python; the pure-Python decoder remains as the fallback,
same results). The Dreamcast / PlayStation bar shows which engine ran and its MB/s. See the table in
`docs/ARCHITECTURE.md` (Amendment 14) for measured times on a Steam Deck. **chdman is not needed**: the app reads and writes CHDs itself. An installed chdman is used only for a CHD in a
format newer than the built-in reader knows, or when you choose it on the Disc images page ("always chdman" for
reading, "chdman, when it is installed" for writing). The built-in reader reads everything chdman 0.289 reads: CHD versions 1 to
5, every compression (`zlib`, `lzma`, `zstd`, `huff`, `flac`, the CD codecs `cdlz` / `cdzl` / `cdfl` / `cdzs`, the
laserdisc codec `avhu`) and CHDs that need a parent file (the parent is found by its SHA-1 in the same folder).
Zstandard uses the system's libzstd (SteamOS has it) and falls back to a slow built-in decoder without one; the engine setting `chd_engine` is `auto`
| `python` (never chdman) | `chdman`.

**chdman (optional).** The AppImage no longer ships chdman. It ships libFLAC / libogg under `tools/lib` (not in git: the
build downloads checksum-pinned Arch Linux packages, see `docs/THIRD_PARTY.md` for versions, licences as the packages
declare them, and where to get the source; the text is also inside the AppImage as `licenses/THIRD_PARTY.md`). A chdman
you have installed is still found - `$ROMORG_CHDMAN` / the saved chdman path, `chdman` on `PATH`, the Flatpak
`org.mamedev.MAME`, `~/.local/bin`, `~/Emulation/tools`, ... - and can be chosen on the Disc images page.
The Windows packages carry no chdman either and do not need one; to use it anyway, take `chdman.exe` from the MAME
download at mamedev.org and put it on `PATH`, next to the app (the exe's folder, or the top folder of the unzipped
package), or save its path on the Disc images page (see `docs/PACKAGING.md`).
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
untouched and no `.part` / temp files behind. About the disc size must be free next to the games during a conversion.

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
sets are left alone except via *Convert*. Preview first, nothing overwritten.

**What is matched.** PlayStation: every game is a `.cue` + `(Track N).bin` per track (a single-track disc: one
`.bin`). PlayStation 2: 8,606 DVD games are ONE `.iso`, 3,168 CD games are `.cue` + `.bin`. A CHD made with
`chdman createcd` stores a DVD ISO as a `MODE1` track of 2048 bytes per 2448-byte frame (no padding, no subcode in the
extracted file), a PlayStation disc as `MODE2_RAW`; the built-in reader decodes both (MODE 2 sectors get their sync and
error-correction bytes rebuilt, with the header counted as zeros as the CD standard says) and the result must equal
the Redump size and crc32 / md5 / sha1 - a PS2 CHD matches only if the whole ISO (or every bin track) is identical. A DVD
CHD made by `chdman createdvd` (metadata `DVD `, 2048-byte units) carries the ISO's SHA-1 in its header, so it is
*identified* instantly without decoding; *Check every track while scanning* decodes it (*verified*). Every compression chdman 0.289
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
*Applications*, *Educational*, *Bonus Discs*, *Multimedia*, *Add-Ons* are kept on purpose. Of a game's releases ONE is kept:
**Prefer special and limited editions** (on by default) puts `(Special Edition)`, `(Genteiban)`, `(Premium Box)`, first prints and other builds
first (they can hold more than the original), then the plain release, then store editions and bundles (`(Toys R Us)`, `(Controller Set)`,
`(EDC)` ...); **Budget re-releases** (`(PlayStation the Best)`, `(Greatest Hits)`, `(Platinum)`, `@barai` ...; on by default) are set aside
when another release of the game is there, and **Online-only titles** (`(Shenmue Passport)`, browsers, network start-up discs, FFXI ...; on by
default) are excluded. **Combine alternate titles** (on by default) makes the titles a game has in other regions one game (`Biohazard` /
`Resident Evil`, `Eternal Arcadia` / `Skies of Arcadia`, `Probotector` / `Contra`): the ratings data (LaunchBox's alternate names) and
GameTDB's Wii U IDs say which titles are one game, and so are titles spelt alike (`Rock 'n' Roll Racing` / `Rock n' Roll Racing`); two
titles released for the very same regions stay two games. The same game also takes in its re-releases (`(Sega Channel)`, `(Mega Drive Mini)`,
`(Virtual Console)`, the `... Collection` extracts: after the retail release), its console-feature releases (`(SGB Enhanced)`, `(Rumble
Version)`), the label of a disc (`(Disc 1) (Leon-hen)`), the other discs of a GameCube / Wii game, and its pirate copies and the unlicensed
copies of Taiwan, China, Korea, Asia, Russia and Brazil (after the licensed release); an unlicensed game of another region (Tengen's
`Tetris (USA) (Unl)`) and homebrew (`(Aftermarket)`, whatever competition, publisher or board) stay games of their own. Bonus and extras
discs stay too, but are not rated as the game. Store samples, previews, trials, kiosk downloads and trailers (`(Jitsuen-you Sample)`,
`(Preview Disc)`, `(Otameshiban)`, `(Trade Demo)`, `(DS Download Station ...)`, `(Video)` ...) are demos;
`(Rev N)` / `(vX.Y)` are revisions (newest wins); `(Disc A)` / `(Disc B)` count as discs 1 / 2.
**Playlists:** PlayStation multi-disc games get an `.m3u` with relative paths in the game's folder, above the folders of the discs (RetroArch and DuckStation read it);
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
`_converted_originals/` (keeping its relative path). Nothing is deleted.
Single-file `.zip` archives are converted into a new `.zip`; `.7z` / `.rar` and zips with
several files are skipped ("extract the archive first"). NES dumps are not converted,
because emulators need the iNES header.

## Organising safety

- **Nothing is ever overwritten.** If the target already exists the file is skipped
  (`conflict`). Extra copies of the same ROM are not conflicts: one is kept and the others
  are archived in `_duplicates/`. Targets are re-checked at the moment of moving.
- Case-only renames work on case-insensitive file systems (exFAT SD cards).
- Characters that are illegal on Windows / FAT / exFAT (`: ? * " < > | \`) are replaced
  with `_`, since Steam Deck SD cards are often exFAT.
- Moves use a hard link + unlink where the file system supports it, so a file that
  appears at the target during the apply is never replaced.
- Each move is one rename that never overwrites, so an apply interrupted by closing the app, a shutdown or a crash leaves
  every file whole, in its old place or its new one; the next build finishes the job. Nothing is written into the
  platform folder but your games (no logs, no journals). The folder is re-scanned after an apply.
- Closing the app (Quit, SIGTERM from Steam, Ctrl+C) during an apply stops it between two
  moves.

## Regions and the Preview (v0.2)

- **One region, one entry.** `UK` (No-Intro) and `United Kingdom` (TOSEC's GB) are the same region: it is listed once as
  *United Kingdom*, saved settings that say `UK` are read as that, and names are parsed to it.
- **The region list comes from your data.** Region priority lists the regions that exist in the installed DATs of the system
  (not a fixed list); your own priorities stay at the top.
- **Preview and Build share the scan.** Preview reads and matches each system's folder; the Build that follows (and a second
  Preview) reuses that scan while the folder (file count, size, newest change) and the DATs are unchanged, and says so in the
  job bar. A build, a change in the folder or a scan of a single system clears it.

## Progress, time and data (v0.2)

The job bar at the top shows what is running with its percentage, how long it has been running, an estimate of the time left
(from the second it can tell), how much data has been dealt with so far (files hashed, discs read, files copied or moved, in
whatever unit fits: MB, GB, TB) and the rate. 
## Build the library in another folder (v0.2)

On the Library tab, **Where to build** chooses between *In this folder* (the classic build: files are moved) and
*In another folder*: the files your rules keep are **copied** (or **moved**, if you choose) into a destination
folder, laid out exactly as an in-place build would lay them out. Excluded, superseded and unmatched files stay in
the source and are not copied. The choice is remembered.

- **How files get there:** **Copy** (the default) leaves the source with all its files; it works across drives and on exFAT SD
  cards. **Move** takes the files out of the source folder: a rename on one drive (instant, nothing written again), else a
  copy that is checked and then removed. There are no links. Move cannot be combined with *keep in
  sync* (the originals are gone, so there is nothing to compare with).
- **Safe:** the destination may not be inside the source or contain it; a different file already at a target is never
  overwritten (shown as a conflict); copies are written under a temporary name and renamed when complete; the free
  space is checked first. Running it again only adds what is missing.
- What the build added is recorded in `<destination>/.romorg-library/library.sqlite`, so a later build knows which files are
  its own. Your own files in the destination are never touched.
- **Keep the destination in sync** (off by default): the build then also brings the
  destination in line with the source.
  - A file this app built earlier that your rules no longer keep, or whose source is gone, is **removed**.
  - A file whose source changed is copied again.
  - A file you edited in the destination is left alone and reported. Files this app did not build are never touched.
  - The preview lists the counts (*To remove*, *To replace*, *Edited by you*) before anything is done.
  - Safety: a sync that would keep nothing (source empty or not mounted) removes nothing. A sync that would remove most of
    the library (over 20 files and over half) is refused until you confirm it separately.
- Save files and other files lying next to discs stay in the source unless "Also copy save files ..." is ticked.
- The plan for the next steps is in `docs/V0_2_PLAN.md`.

### Library rules: your defaults and a system's own

The library rules (exclusions, one version per game, latest versions, languages, region priority, kept variants, ratings, what
happens to saves) are **one set for the whole app**: **Library defaults** on the **Settings** page. Every system follows it.

On a system's **Library** tab each rule has an **Override** box. Until it is ticked the rule shows your default, greyed and
unchangeable; tick it to give *this system only* a different value (the rule then says **this system only**, with a link,
**use the default again**). The line under the rules says how many rules are overridden, and **Reset all to defaults** puts every
rule of the system back on your defaults (the choices you made for single games stay). A system with overrides has a small blue
**Custom rules** tag in the system list. A change to your defaults reaches every system that has not overridden that rule. A rule a system
cannot use (a DAT without languages, a system with no save source ...) is not shown there.

A system with a different mix of files than the rest (a hack set, a translation set) is best made a system of its own.

**Archive a single system's leftovers too.** On a system's Library tab, with *In this folder*, **Archive unmatched files (excluded,
superseded, incomplete, duplicates) to another folder** is on by default - but only exists once an archive folder is set. There is
**no default archive folder**: choose one for every system on the **Settings** page (the sidebar). Until you do, the Library tab says so
and what the rules leave out stays in the system's folder in `_excluded`, `_superseded` ... as before. A system can have its own folder: type another one on its Library tab (a button there goes back to the
common one; Settings lists the systems that have their own). After the build, what the rules leave out is moved to the archive,
**straight from where it was**, into a folder named like this system's own:
`<archive>/<system folder name>/_excluded`, `_superseded`, `_incomplete`, `_duplicates` and `_unmatched`. The Library tab lists
the folders for the system you have open. The system folder then holds only what you keep, the preview counts the files.

## The archive in an S3 bucket (v0.4)

The archive can be an S3 bucket instead of a folder: MEGA S4 or any S3-compatible service. Type `s3://<bucket>/<folder>` where you
would choose the archive folder (in Settings for every system, or on a system's Library tab for it alone; the folder part is optional).
Set up the connection once in **Settings > S3 connection**: the endpoint (MEGA S4's global one is `s3.g.megas4.com`; a region can be
left empty), the access key ID and the secret key. The secret key goes into a file of its own in the app's data folder (`s3-secret`,
readable only by you) and is never shown again; **Test connection** lists the bucket, puts a small object with metadata, reads it back
and deletes it, and says whether checksums can be checked without downloading.

A build then does what it does with an archive folder, with one difference: each file the rules set aside is **uploaded** to
`<bucket>/<folder>/<system folder name>/_excluded/...` (`_superseded`, `_duplicates`, `_unmatched`, `_saves` ...), checked in the
bucket (its size, its MD5 and its SHA-1, kept with the object as metadata together with its CRC32 and the path it came from), and
only then **deleted from the disk**. No local copy is kept. A name another object already has gets a number, as in a folder; an
object is never replaced. A build that cannot reach the bucket stops before it moves anything. Uploads a killed build left unfinished
are cleared at the next build.

The file tree's **Archive** tab shows the bucket, marked **S3**, with its address: what is in the bucket already (*In the bucket*,
read from its listing: nothing is downloaded) and what the build uploads (*Uploaded*). Scanning the system reads the listing again.

## Restoring from the archive (v0.4)

The file tree's **Archive** tab (a folder or an S3 bucket) has a tick box on every file and folder that is in the archive; tick
what you want back and press **Restore** (a folder: everything in it). The archive **keeps its copy**; nothing there is changed.

* A file whose game is recognised goes where a build puts a kept game, **under its database name** - and in the form the library
  keeps it: a raw cartridge dump becomes a `.7z` where the system's games are kept as 7z, a raw disc (`.iso`, `.cue` / `.gdi` and
  tracks) becomes a CHD in its game's folder (the rules *Compress games as 7z* and *Convert when building*, as a build would). A file
  already in that form comes back as it is.
* A file that is not recognised goes back to the path it came from (an S3 object remembers it; otherwise its path in the archive
  without the reason folder).
* Saves in `_saves` are not restored yet.
* A restored game the rules still leave out is archived again by the next build; change the rules to keep it. It is not uploaded or
  copied a second time: the archive already holds the same file, so only the local copy goes.

**Missing games the archive holds.** On the ROM folder side, a game the rules want that your folder lacks but the archive holds is
marked **In the archive**; its details have **Restore from the archive**, and the tree offers **Restore N missing games from the
archive** for all of them. The archive is matched without downloading anything: an archive folder is scanned like the ROM folder
(when the system is scanned); an S3 object by what was recorded when it was uploaded (the game the build identified, its SHA-1, a
zip's / 7z's member CRCs). The app assumes it manages the bucket: objects other tools put there are not looked into.

## Disc images (CHD) and converting first (v0.2)

- **Disc images (CHD)** in the list on the left holds the settings every disc system shares (Dreamcast, PlayStation, PlayStation 2):
  where chdman is (optional), how CHDs are read while scanning, how new CHDs are written, the compression, and **Check every
  track while scanning** (slower: audio tracks are decoded too, so every disc ends up *verified*; a scan alone *identifies* a
  CD by its data tracks and checks audio by length). The old per-system *Verify CHDs* tool is gone: that switch does it
  in the scan.
- **Convert when building** (a library rule: **Settings > Library defaults** for every system that can convert, and a system can override it
  on its own Settings tab; off by default): *Convert raw discs to CHD* for the disc systems, *Clean up dumps* for the SNES (copier headers)
  and N64 (byte order). It is a step of **Build library**, not a separate job: the Files tree shows what it will do (the new CHD in its game's
  folder, the original in `_converted_originals`, or marked deleted), and the build converts first (each CHD is checked against Redump before
  the original is touched), re-reads the folder and then builds the library. What counts as a raw disc: an `.iso`,
  a `.cue` / `.gdi` with its tracks, and a **`.zip` or `.7z` that holds one** (read without unpacking: the tracks are streamed through
  their checksums, the CRC-32 the archive lists rules most games out first, the answer is remembered per archive; a 7z needs no 7-Zip when it
  is LZMA / LZMA2). A zipped disc is unpacked only to convert it, into a work folder next to where the CHD goes, and that folder is removed.
  A GD-ROM in an archive needs a `.gdi` inside. An archive with no disc image in it, or a damaged one, is left as it was.
- **Delete the originals after converting** (a rule too, disc systems only, off by default): off, the originals (the `.iso`, the zip, the
  cue and its tracks) are kept in `_converted_originals`. On, they are deleted once the CHD has been checked against Redump, and the build's
  confirmation says so.
- **A raw disc is named after its game even before it is converted:** with the rule off, an `.iso` or a zip / 7z of one is renamed to
  `<Redump name>/<Redump name>.iso` (or `.zip`, `.7z`) like a CHD game is. A `.cue` with tracks is converted, not renamed.

## RetroArch: saves, states and the config (v0.2)

**Emulators** in the list on the left (`#/retroarch`): one card per emulator, all the same shape. A green dot is an emulator
that was found and is in use, a hollow one an emulator that was not found or is switched off. Each card says which systems it
serves and how many saves and states its systems' last scans found, shows its folder, and has **Change...** (its folder,
**Folders...**, and the switch that makes the app look at its saves or not). The ones in use come first. RetroArch is optional:
with no RetroArch found, and none chosen, nothing on its card matters.

- **Finding RetroArch:** the app looks for the Steam build (also in other Steam libraries), Flatpak, a normal Linux install and
  Snap, on Windows `%APPDATA%\RetroArch`, Steam and the usual portable folders, and on macOS the Application Support folder.
  **Change...** on its card picks another one found, or any `retroarch.cfg` (or its folder) you point at.
- **What RetroArch's card shows:** where it keeps save files and save states, whether each gets one folder per core, and its config
  file, read from `retroarch.cfg`. Core and game override files that set their own save folders are listed, because the global
  setting does not apply to those.
- **Move saves and states to another folder...** (folded away): the folder for save files, the folder for states (the same or
  separate) and whether each gets one folder per core. **Preview** lists every file that moves. **Move saves and update RetroArch**
  then
  1. writes a zip backup of those files (optional; the folder is yours to choose),
  2. moves the files, never overwriting one (a name already taken is left alone and reported) and removing folders it emptied,
  3. backs up `retroarch.cfg` and changes only its save settings.
  Each core keeps its own folder (`bsnes/`, `Flycast/` ...): nothing is merged. States and their `.png` thumbnails go to the
  state folder, everything else to the save folder. Turning one folder per core off flattens the core folders; turning it on
  cannot tell which core a flat file belongs to: those files are moved as they are and listed. Nothing changes while RetroArch
  is running (it rewrites its config when it closes); the old `retroarch.cfg` is kept as a backup next to it.
- **Asset folders** (folded away): RetroArch's menu assets, content database, cheats, playlists, thumbnails, downloaded core
  assets, controller remaps and config browser folder, each with the folder RetroArch uses now ("RetroArch's own" for its default).
  **Change...** on a row points that one setting at a folder you choose, or back at RetroArch's own. Nothing is guessed and no
  file is moved: only that setting in `retroarch.cfg` changes (the favourites / history lists follow the playlist folder), the
  config is backed up first.
- **PCSX2** is also looked for in RetroArch's system folder (`<system>/pcsx2`, where RetroArch's PlayStation 2 core keeps its
  memory cards and states, and where many set a standalone PCSX2's folders too).
- **Saves follow the games:** RetroArch finds a save by the game's file name (`<name>.srm`, `<name>.state`, `.state1` ...,
  `<name>.state1.png`, Flycast's `<name>.A1.bin` memory cards ...). When a library build renames a game, its saves and states
  are renamed to match, in the same core folder (cores stay separate). A build into another folder **copies**
  them to the new names and leaves the old ones. Nothing is overwritten (a file already at the new name is left alone).
  It waits if RetroArch is running (RetroArch writes its saves back when it closes).
  The switch is **Rename saves with their ROMs** in the Library rules.
- **Saves in the scan, the library and the builds:** all of this exists only when the app knows a RetroArch config (it found an
  install, or you chose a `retroarch.cfg` on this page). Without one the app does not look at any save folder, shows no saves
  column and no saves setting, and sorts and builds exactly as if saves did not exist.
  - *Scan:* every scan of a system reads the save folders once and reports **how many saves
    there are and which title each belongs to**: save files and states (a state's screenshot is kept apart and not counted), the
    games they belong to, and how many of those games have a ROM in the folder, are a title in the database you have no ROM for,
    or match nothing. A save belongs to a ROM when its name is that file's name (for a zip also the name inside); a Flycast memory
    card `<name>.A1.bin`, `<name>.state1`, `<name>.srm` ... all belong to `<name>`. Where RetroArch makes one folder per core only
    the cores that play the system count, so a Genesis save never belongs to a Super Nintendo game of the same name.
  - *Overview* of a system: "N save files for M games (K with a ROM here, L without, U unmatched)", and once the library is
    previewed, what a build would do with them.
  - *Browse:* a **Saves** column on the Games, Matched and Missing lists (the total of saves and states, `1 save · 6 states`
    under it, and the total of all editions of the title), also for titles you have no ROM for but have saves for; sortable; a
    **Games with saves** filter; and a **Saves** tab that lists every save set with the title it matched.
  - *Library:* a **Saves** column (sortable), the filters **Games with saves** and **Saves affected by this build**, and on each row
    the choice for that game (below).
  - *Rename:* when a build gives a ROM its database name its saves and states are renamed to match, in the same core folder
    (a build into another folder copies them). Nothing is overwritten.
  - *When a rule replaces or archives a game you have saves for* you choose, in your Library defaults (and a system's own rules)
    for every game, and on the Library tab for a single game: **Copy or convert the save to the latest version, leave the existing save**
    (the default: when the version you have is not the one a save belongs to - an older revision, another region, another name, or a ROM the
    rules replace - the save games are copied to the version you have, under its name (a multi-disc game's: its playlist's); a PlayStation or
    PlayStation 2 save is converted for the other disc's game code; the existing save stays), **Keep both ROMs** (the ROM the rules would
    replace stays, so the saves keep their game), **Archive the saves with the ROM** (they move to
    `<archive folder>/<system folder>/_saves/<core folder>/...`, or to `_saves/` in the ROM folder while no archive folder is
    used) **Leave the saves where they are**, or **Copy the save to the better version**. The last one is for the day you add a better match
    (the version your rules prefer): the old ROM is replaced, and its **save games** (memory cards, battery saves, `.srm`; never
    the save states, which belong to one exact ROM) are copied to the better ROM's name, in the same core folder. A better version that
    already has a save of its own is never overwritten (the preview says so); the old save stays where it is.
    It is done only where the save works in the other release: cartridge systems (Game Boy, GBA, DS, NES, SNES, N64, Master System,
    Game Gear, Mega Drive, 32X, Lynx) in every region; PlayStation, PlayStation 2, GameCube, Wii and Dreamcast only when both releases share a
    region (their memory cards name the files after the disc's region code). Where it would not work, both ROMs stay, as with Keep.
    The Files tab marks such a game with a *save copied to the better version* tag.
  - **Converting a PlayStation save's region** (*Copy the save, converting its region (PlayStation, PlayStation 2)*, experimental): a PlayStation game finds
    its saves on a card by a name made of the disc's region (`BA` USA, `BE` Europe, `BI` Japan) and product code (`SLUS-00520`), so a USA
    save is invisible to the European disc, whose code differs. With this choice the copy is rewritten for the better version: the app reads
    both discs' product codes (from the `.chd` or `.iso`; a game whose disc it cannot read keeps both ROMs), changes that one save's name on
    the card copy and its checksum byte, and leaves everything else on the card (other games' saves, the save's data) byte for byte. A card
    without a save of that game (an empty card) is not copied. The old card is never touched. It is
    written from the card format and checked against your own cards, but not against a real console: a game that keeps the region inside
    its save data may still refuse the save.
    **PlayStation 2** (PCSX2): a save there is a folder named after the disc's code (`BASLUS-20946GTA50000`), so the same rewrite is done on
    that name and on the file inside that carries it. On a *folder card* the save's folder is copied under the new name (PCSX2's own
    `_pcsx2_*` files are left out). In a *card image* (`.ps2`, with or without the spare bytes after each page) the names are rewritten in
    place, so the save is **moved**, not copied; the changed pages' error-correcting codes are recomputed, and the old image is kept in the
    save backups folder (`save-backups/replaced` in the app's data folder, or the backup folder you chose). A card that already has a save under the new
    name is left alone (the preview says so), and nothing is done while PCSX2 is running.
 
    Nothing is moved while RetroArch is running (the ROMs are still archived); a build into another folder never archives
    anything from your folders.
  - *Multi-disc games:* RetroArch names the saves of a game started from a playlist after the `.m3u` (`Game.srm`), not after a
    disc. A playlist you have saves under counts as that game's name: its saves are listed with the game, follow the game when
    its rules say archive or leave, and are renamed when the build replaces your playlist with the one named after the
    database (`game.m3u` becomes `Game (1990)(Pub).m3u`, `game.srm` becomes `Game (1990)(Pub).srm`). Only a playlist that
    already has saves is followed: a save made from disc 1 on its own keeps disc 1's name.
  - *The system list:* a system with saves shows a small badge with their number (set at its last scan).
- **BIOS and firmware:** press **Check** and the app reads the `.info` file of every installed core, lists the BIOS / firmware
  each wants (required or optional) and compares them with RetroArch's system folder, verified by MD5 where the core gives a
  checksum. By default it covers the cores of **every system that has a ROM folder** (or choose *Every installed core*, or one
  system). Missing files are looked for in all those ROM folders, one more folder you can name: by name,
  and, when the checksum is known, under any other BIOS-looking name. It is one pass with progress (a minute at most; it says
  when it stopped early). **Place found files** moves (or copies) them to the path the core expects inside the system folder.
  Nothing is overwritten and nothing is written outside the system folder.
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
  Organise removes this app's playlists that would point at moved files; write the
  playlists again afterwards. Outdated playlists of this app (disks gone, or replaced by a
  playlist with a new name) are shown as `stale` and removed by **Write playlists**.
- Sets are matched flexibly: an untouched disk with a bare or shorter crack flag
  (`[cr]`, `[cr Galahad]`) joins `[cr CSL]` / `[cr Galahad v1]` disks. Disks with
  incompatible dump flags are never mixed (such families are reported incomplete), and
  excluded variants (`[b]`, pre-release ...) are never borrowed from.

## Nintendo Switch (v0.2)

The **Nintendo Switch** is a system like the others (Cartridge consoles, in the sidebar) with Overview, Library and Browse. Games, updates
and add-ons (`.nsp` `.nsz` `.xci` `.xcz`) live in the one folder; the folders the emulators keep in there (named by a 16-digit title ID,
with shader caches) are ignored.

* **The database.** There is no checksum DAT for the Switch (No-Intro and Redump have none, and a card dump is far too big to hash for
  a lookup). The "DATs" are three catalogues made from one source, the community title database `blawar/titledb` (names, languages and
  demo flags from its US, GB and JP eShop lists - the store a title is sold in is its region -, the NCA content IDs, every update
  version in `versions.json`): ***Nintendo - Switch*** (the games, `Game (USA, Europe) (En,Fr,De)`), ***Nintendo - Switch (Updates)*** (one entry per update version, `Game - Update (USA, Europe) (v3)`) and
  ***Nintendo - Switch (DLC)***. They are made when the database is fetched, on the first start (about 100 MB over the wire; "Update the title database now" on
  the Emulators page does it again). Each has its own completion figure.
* **How a file is matched.** By the **title ID** inside it - the `.cnmt.xml`, an NCA the database knows (this recognises a renamed eShop
  file and gives its version), the ticket, the `[title ID]` in the name, and for a card dump the header of its small CNMT NCA (the one
  place your own `prod.keys` is used: 512 bytes of one file; nothing else is decrypted) - and for an update by its **version** as well.
  An update whose version the database does not list is unmatched.
* **Library.** The ordinary rules: *latest update only* applies to the updates catalogue (older updates go to the archive's
  `_superseded`), duplicates, region on the games, unmatched files to `_unmatched`, and files are renamed to the catalogue names.
* **Checksums.** A Switch file names each NCA by the first 16 bytes of its SHA-256, so it carries its own checksums. The option **Also
  check every file's checksums when scanning** (on the Switch system's Overview) hashes every NCA of every matched `.nsp` / `.xci` and
  compares it with its name (`.nsz` / `.xcz` are not checked). Browse shows each file as correct or damaged, and Overview counts the
  damaged ones. The first scan reads every file; the verdict of a file that has not changed is remembered.
* **Saves** (Eden and Ryujinx, by title ID, shown on the system's Overview and Browse): see below. Copying saves between the emulators is
  planned, see `docs/SWITCH_PLAN.md`.

## Nintendo Wii and Wii U, and the saves of the emulators (v0.2)

**Nintendo Wii** and **Nintendo Wii U** are systems like the others (disc systems, in the sidebar), with the same Overview, Library
and Browse tabs.

* **Wii:** *Nintendo - Wii*, Redump's list as libretro's mirror has it (https, newer, with each disc's serial such as `RVL-SB4E-USA-B0`; it
  leaves out the betas, prototypes and demos that the default rules exclude anyway, 143 of 3,780 discs). A plain `.iso` is hashed. Dolphin's `.rvz` / `.wia` / `.wbfs` / `.ciso` are not the original
  image (the encrypted partitions are stored decrypted), so Redump's checksum cannot be had without rebuilding the whole disc; such a
  file is **identified, not verified**: the 4-character game code in its header (`SB4E`) is in the serial of its Redump disc; the
  revision and disc number in the header pick between the discs that share it. A copy of an `.iso` that matches no checksum
  (scrubbed, trimmed) is identified the same way.
* **Wii U:** Redump has no Wii U DAT. The app makes one from GameTDB's `wiiutdb.xml` (the 457 disc games, no checksums; eShop and
  Virtual Console titles are left out), with No-Intro's names where it knows the title (they carry all the languages) when it fetches the list (on the first start, then when you press Check for updates). A `.wux` / `.wud` is told by the product code in its first sector (`WUP-P-AFXE-00-551USA-0`);
  nothing is decrypted.
* **GameCube** and **Wii** are separate systems with their own folders, even though Dolphin plays both.

### Saves of the emulators

The **Emulators** page (sidebar, below Settings) lists the emulators the app looks at - **Dolphin** (GameCube, Wii), **Cemu** (Wii U),
**PCSX2** (PlayStation 2), **Eden** and **Ryujinx** (Switch) - each with its folder (found on this machine and selected; type or browse
to override), and a switch to ignore it. RetroArch, below them, serves every other system. A system whose emulator is not set up (or
switched off) shows no saves anywhere: no card, no column, no choice in the Library rules.

* **RetroArch's** saves are named after the ROM: they follow a renamed ROM, and the Library rules' "rename saves" switch applies.
* **The others** are told by the game's ID, so they never need a new name: Dolphin by the 4-character game code (GameCube `.gci` files
  and Wii `data` folders), Cemu by the title ID (Cemu's own `title_list_cache.xml` says which disc is which), PCSX2 by the disc's serial
  (read from `SYSTEM.CNF`; memory-card folders and save states). A game that has saves is **kept** when the rules would archive it (or you
  choose to archive its saves with it: they go to `<archive>/<system>/_saves/...`). The saves of a memory card
  *image* (`.ps2`, GameCube `.raw`) cannot be split by game: they are counted but never moved.
* On the platform page the **Overview** shows "Saves (Dolphin)" and the **Browse** tab has the Saves column and list, with the source.

## BIOS, firmware and key files

The **BIOS and firmware** page (sidebar, `#/bios`) is about what a console or an emulator needs besides the games.

**Kept with the ROMs.** A scan of any system also looks, among the files no game matched, for BIOS and firmware files and for the
emulators' key files. A BIOS is told by its **checksum**, whatever the file is called, against two lists:
- the **TOSEC "- Firmware" DATs** (about 140 systems: the Sega 32X, the Mega Drive, the NES and SNES, the Game Boy, the 3DO, the Saturn,
  PlayStation, MSX ...), read from the TOSEC pack that is downloaded with the other databases;
- libretro's (`System.dat`: 514 files of 60 systems, bundled; names and checksums only, no file), which says the name the emulators expect
  (`scph1001.bin`, `dc_boot.bin` ...).

A Library build names a BIOS file as the **database** names it (the TOSEC entry's name, like a ROM gets its DAT's name), in the folder it is in;
a file only libretro's list knows gets libretro's name. The name an emulator expects is for the copy in its folder: the check below finds the
file by its checksum under any name and *Place found files* puts it into RetroArch's system folder as the core wants it (`scph5501.bin`).
The same BIOS twice (any folders) is a ROM twice: one copy stays and the spare goes to `_duplicates`, with the copy it duplicates named.

A **zip** is kept the same way when every file in it is on a list (told by the checksums in its directory: nothing is unpacked), so
`[BIOS] Nintendo Game Boy Advance Boot ROM (World).zip` stays. `prod.keys`, `title.keys` and `keys.txt` (Switch and Wii U emulators)
are told by name. The checksum alone decides, never the name: a file that only has a BIOS's name is an ordinary file. The scan counts
them (a *BIOS / firmware files* card on the Overview and on the Library page) and the Browse tab marks them in the Unmatched list. On the Library
page they are a reason of their own, **BIOS / firmware**: the chip above the table shows just those rows, and the *Kept* card counts only games.

The Amiga's **Kickstarts** are games of its own TOSEC firmware DAT (`Commodore Amiga - Firmware`), so they are matched like any game; they are counted
as BIOS / firmware all the same (the card, the Library chip), and follow the Amiga's rules for that DAT (every version is kept).

A BIOS told by a TOSEC firmware list is judged **like a ROM of that list**: its entry's name (`... BIOS v3.0 (1997-01-06)(Sony)[b]`) goes through the
same tag parsing and the same exclusion rules as any game's, so a bad dump, an over / under dump or a modified one is set aside to `_excluded`
(and the archive) with the rules that are on, and stays when its rule is off. The dump types you switched off apply too. The rules that pick
between games (languages, regions, versions) do not: a Japanese BIOS is still a BIOS.

They stay in the system's folder instead of going to `_unmatched` or the archive while the library rule **Keep BIOS, firmware and key
files** is on. It is on by default: set your default in **Settings > Library defaults**, and a system can override it on its Library tab
like any other rule. Files that only a No-Intro `[BIOS]` set has (the SNES CD boot ROM, the NES `Pro Action Replay` ...) are on neither
list, so they are unmatched like any other file.

The page also shows where the lists come from and what the last scan of each system found. The check further down is the former
*BIOS, firmware and key files* panel of the Emulators page: it reads the `.info` file of every installed core, compares what each needs with
RetroArch's system folder (verified by checksum where the core gives one) and copies what you have into place. It also looks for the key
files the emulators need: `prod.keys` and `title.keys` for Eden / yuzu (`<user folder>/keys`) and Ryujinx (`system`), `keys.txt` for Cemu
(its data folder). For each emulator that is set up it shows whether the file is there and usable (a `prod.keys` needs the header key, a
`keys.txt` a 32-digit key), and when it is missing it looks in the other Switch emulator's folder, the ROM folders and the downloads.
**Place found files** *copies* a found key file into place - never moves it, never over a file that is there.

## Kickstarts for RetroArch (PUAE)

There is no Kickstart tool on the Amiga systems any more: the **RetroArch** page's BIOS & firmware check
covers every installed core, PUAE included, and copies the Kickstart ROMs you have (matched by MD5) into
RetroArch's system folder.

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

The UI is sized for the Deck's 1280x800 screen with large touch targets. Every folder field has one **Folders...**
button: the app's own folder browser, the same on Windows and Linux. (There is no native "Browse..." dialog: the one
Windows offered opened behind the browser, and the Linux one only worked in Desktop Mode.)

**Windows:** two packages, a single `.exe` and a `.zip` (`packaging\build_windows_exe.ps1`,
`packaging\build_windows.ps1`; see [docs/PACKAGING.md](docs/PACKAGING.md)). Both builds end with
`packaging\smoke_test.ps1`, which makes the checks of `packaging/smoke_test.sh` against the finished package.

## Where data is stored

| Platform | Location |
| --- | --- |
| Linux / SteamOS | `$XDG_DATA_HOME/simple-rom-organiser` (default `~/.local/share/simple-rom-organiser`) |
| Windows | `%LOCALAPPDATA%\simple-rom-organiser` |
| macOS | `~/Library/Application Support/simple-rom-organiser` |

Inside it: `dats/` (extracted TOSEC DATs and `release.json`), `nointro/` (the No-Intro
DATs and their `manifest.json` - kept separate, so updating the TOSEC pack never touches
them), `cache/` (downloaded pack zip, hash cache), `updates.json` (when the DATs were last
checked), `whdinstalls/` (the list of WHDLoad installs), `ratings/` (the LaunchBox ratings index, only when a rating filter is used), `redump/` (the Redump DAT) and `config.json` (last system, folder per system, library rules per system, Kickstart destination per system, and for the Dreamcast `chdman_path`, `chd_engine` = `auto` | `python` and `chd_workers` = hash processes, 0 = auto, `temp_dir` = scratch folder for chdman extractions, `temp_ram_reserve_mb` = memory kept free before RAM is used for them). All writes to `config.json` are serialised and applied to the latest file content, so concurrent actions (a scan finishing while you change a rule) never overwrite each other. Set `ROMORG_DATA_DIR` to use a different location. Only playlists
are written into your platform folder.

## Adding a platform

Platforms are defined in one place, `romorg/platforms.py`: a name, its DAT names in
priority order, where they come from (`tosec`, `nointro` or `redump`), the layout (`per_dat` folders,
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
- **WHDLoad installs**: the list of game installs, `https://www.whdload.de/games/all.html` (titles, archive names, sizes), fetched with
  the other databases and kept for a week. An install (`https://www.whdload.de/games/<name>.lha`) is downloaded only when you ask for it;
  only install packages are ever downloaded, never pre-installed games.
- **Redump (Sega Dreamcast, Sony PlayStation, Sony PlayStation 2)**: `http://redump.org/datfile/dc/`, `.../psx/` and `.../ps2/` over **plain HTTP** (HTTPS is refused by
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
