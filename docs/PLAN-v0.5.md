# v0.5 plan: one big-screen UI for controller, mouse and keyboard

Goal (the user, 2026-10-24): "a native Steam Deck UI". First step built: a controller layer on the existing UI (Amendment 69). After
seeing it at 1280x720 the user found the existing UI "too wordy and not controller friendly" and, on 2026-10-25, chose a new design
as **the** UI of the app, replacing the old one ("the old one is too complicated"):

* the mock-ups: Design canvas "Deck big-screen UI" (<https://claude.ai/artifact/XtrHAcpjdPEazRNq8dqdct>) - Home, a system, Build,
  Building, Rules, list picker, the file tree (folders, inside a folder, archive), Archive settings (where, folder, S3);
* our own small CSS / JS kit, **no framework** (the user asked; none of the big-screen web frameworks fits a stdlib, no-build app);
* it works with a **controller, a mouse or touch, and a keyboard** - one UI on the Deck, in Desktop Mode and on Windows;
* 1280x720 is the base size; bigger windows get more columns and wider panels, never smaller text.

## The kit

Every screen is built from the same few parts (`static/` - the names below are the CSS classes to come):

| Part | What it is |
| --- | --- |
| header | where you are (`Amiga ›` + title) and one short fact on the right |
| tile | a big button: icon, title, at most one short line; the main action is blue |
| row | one line, the setting's name on the left and its value, a switch or `›` on the right |
| switch | the on / off control of a row; A, Enter, a click or a tap flips it |
| list picker | a full-height panel of choices from the right; replaces every `<select>` |
| side panel | the facts about the focused row (file tree, folder picker) |
| views | LB / RB chips across the top (file tree) |
| folder browser | one folder at a time, drives on the left |
| progress | one bar, a count, what it is doing now, Cancel |
| strip | the buttons that apply, at the bottom; key names with a keyboard, hidden while the mouse is used |

Rules: text 20 px or larger at 1280x720; at most one short line under a heading, longer explanations behind X (Details); nothing needs
hover; the focused control grows and gets the blue ring; menus say "Exclude" / "Excluded" (never "Leave out").

## Input

| Controller | Keyboard | Mouse / touch | Does |
| --- | --- | --- | --- |
| D-pad, left stick | arrows, Tab | - | move the focus |
| A | Enter, Space | click / tap | activate |
| B | Esc, Backspace | the header's back arrow | back / up a folder / close / cancel |
| X | X | the panel's Details button | details of the focused thing, or the screen's second action |
| Y | `/` | the search button | search |
| LB / RB | `[` / `]` | click a view chip | previous / next view or system |
| LT / RT | Page Up / Down | wheel | scroll |
| Menu (Start) | Ctrl+Enter | the button | the screen's main action (Restore ticked, Save and test); with none, the app menu |
| - | F10 | the `⋯` button | app menu: what's running, Quit |

Steam Input may present the controller as a gamepad or as keys; both work. Text is typed with Steam+X
on the Deck; the UI avoids typing wherever a list can do it.

## Old UI to new screens

Everything the old UI does keeps a place, unless it is listed under "Dropped". "New" marks a screen still to mock up.

| Old (page / section) | New |
| --- | --- |
| Home: list of systems, Find a system | Home: Build library, Archive, Settings tiles; system tiles; Y searches systems |
| Top bar: database status line, automatic updates | Settings › Databases; a warning tile on Home only when something needs doing |
| More menu: Stop the app, updates | Menu: Quit, Check for updates |
| System › Files: tiles (have, with rules, needs a look, files to change) | System: Games you have, Needs a look, Build library tiles |
| System: Rescan folder, Recalculate checksums | System: X Rescan; Recalculate in the system's Settings (rare) |
| System: "Not checked (DAT not installed)" box | System: one warning tile → Databases (New) |
| System › Files: the tree, sides (library / archive), where | Files: views Library, Changes, Excluded, Missing, Archive; one folder at a time |
| Files: search | Files: Y search (New) |
| Files: Show (everything, have, missing, BIOS, unmatched, saves), the build state, region, language, rating | Files: views + a Filter button opening list pickers (New) |
| Files: totals ("the numbers count games") | Files: a count in the header |
| Files: saves-kept rows (dimmed), "show more" in big folders | Files: dimmed rows; the list loads more as the focus reaches the end |
| Files detail: game, DAT, game code, could be, also fits, year, rating | side panel, behind "More details" (X) |
| Files detail: the rules (why), the build | side panel: Now, After the build, Why |
| Files detail: a better match (with checksums) | side panel line + More details |
| Files detail: checksums with copy | More details (copy buttons for mouse users) |
| Files detail: Always keep / Always exclude / Back to the rules | A on a game: a small menu of the three (New) |
| Files detail: convert, save for another release | side panel lines |
| Files: Restore (multi-select bar, single restore) | Files › Archive: Y ticks, Start restores; Restore in the side panel for one |
| System › Library: Incomplete sets, Games that vanish (quick fixes), The build in numbers | System: a "Needs a look" screen listing both with their fixes (New) |
| System: WHDLoad installs | System: a WHDLoad tile on Amiga (New) |
| System: Saves in the emulator's folders | System: a Saves tile (New) |
| System › Settings: Folder | System: Folder tile → folder browser |
| System › Settings: Check all files (full read) | System › Settings: a switch |
| System › Settings: Library rules with Override per rule | System › Rules: the same rows; each says "Every system" or "This system" and Override is a row action (New) |
| System › Settings: Building (layout preview, disk labels, save disk, here / other folder, archive folder, sidecars, sync) | System › Settings: rows; Folder layout behind Details (New) |
| System › Settings: DAT, Per DAT | System › Settings: Database row → picker; per-DAT rows under Advanced |
| Settings: Archive folder, per-system archive folders, S3 connection | Settings › Archive (mocked): Folder or S3, then Connection / Bucket / Test; per-system folders as rows |
| Settings: Library defaults | Settings › Rules (mocked: switches and pickers; the platform order as a reorder list, New) |
| Settings: Ratings, IGDB key | Settings › Databases › Ratings |
| Databases page (sources, installed, newest, status, last checked) | Settings › Databases: one row per database, A updates it (New) |
| BIOS page (kept with the ROMs, where checksums come from, found in folders, check the emulators') | Settings › BIOS and firmware: one row per emulator / file with a found / missing tag (New) |
| Emulators page (RetroArch found, another RetroArch, move saves and states per core, backup zip, asset folders) | Settings › Emulators (New) |
| Saves page (version change behaviour, per-system behaviour) | Settings › Saves: rows and pickers (New) |
| Disc images page: compression (standard / Zstandard), check every track, chdman (path, reader and writer choice) | Settings › Disc images: rows; chdman under its own row |
| Folder dialog (hidden folders) | folder browser (mocked); "Show hidden" as a row action |
| Confirm dialog | a centred panel: the question, two big buttons (New) |

### Dropped

* Compact rows (the kit has one density).

### Kept on purpose

* **chdman support stays as it is** (the user, 2026-10-25): the built-in reader and writer are the default; an installed chdman is
  still found and can be chosen.
* **Two CHD checks instead of one** (the user, 2026-10-25): "Fully check DVD images" (read a DVD CHD instead of trusting its header
  SHA-1) and "Check audio tracks on CDs" (data tracks are always hashed); the older single switch stands for both until either is set.
* **"Tiles on the system pages"** stays configurable (the user, 2026-10-25): Settings › System page tiles; the system screen draws
  the tiles of that setting, the main action ends its first row.

## Phases

1. **The kit and the shell - built 2026-10-25 (Amendment 70)**: `static/next.html`, `next.css`, `next.js`, served at **`/next`**
   while it grows; the old UI stays at `/` until phase 6, then they swap. Input (controller, keys, mouse / touch), the strip, list
   picker, confirm dialog, app menu, hash routes.
2. **Home, System, Build - built 2026-10-25**: Home (jobs, systems, find), a system (the tiles of the setting, Build with the size of
   the next build, Files / Rules / Folder / Missing, Rescan), Build (preview counts, progress, result, the WHDLoad build after it),
   the folder browser, Settings (tiles; the pages not rebuilt open the classic UI), Archive (folder or S3, test, stop, per-system
   folders), the S3 connection, System page tiles.
3. **Files - built 2026-10-25 (Amendment 71)**: views Library / Changes / Excluded / Missing / Archive (`/api/tree` `show=changed` and
   `show=excluded` added), one folder at a time, Show more, search across folders (Y), filters (X), side panel, the keep / exclude
   menu, More details with checksums, archive ticks (Y) and Restore (Start).
4. **Settings - built 2026-10-25**: Rules (every rule of Library defaults; languages as a tick list, the region order as a list you put
   in order), Databases (check for updates, download ratings), BIOS and firmware (check the cores, copy found files into place),
   Emulators (RetroArch, another RetroArch, rename saves, other emulators' folders), Saves, Disc images (the two CHD checks, chdman
   kept), Advanced (IGDB key, hidden folders).
5. **The rest of a system - built 2026-10-25**: Needs a look (vanish with fixes, incomplete, not preferred), WHDLoad, Saves, the
   system's Settings (where to build, archive, playlists, recalculate, folders after building) and Rules (This system / Every system).
   RetroArch (Amendment 72, mocked as screens 32-37): which RetroArch, move saves and states (folders, one folder per core, the backup
   zip and its folder, a preview with the retroarch.cfg changes, the result), asset folders one by one or one shared folder for all.
   A system's databases (versions, folders, counts per database): the classic "DAT" section, which only ever showed facts.
   Nothing is left on the classic page.
6. **Swap - done 2026-10-25, old UI removed 2026-10-31 (Amendment 115)**: the big-screen UI is the only page (`/`, also `/index.html`); `/classic` and
   `/next` are gone, and so are `index.html`, `app.js`, `style.css`, `deck.js`, `deck.css`. The browser tests drive the new UI.
7. **Game Mode launch and packaging** (needs the Deck): `--deck` opens a fullscreen app window of an installed browser and quits the
   server when it closes; Steam launch options documented (`shortcuts.vdf` only if the user asks); self-check and smoke test cover
   the new files.

Before phases 3-5, mock up the screens marked New, a few at a time, on the same canvas.

## Testing

* Here: the headless Brave harness (`tests/cdp.py`) at 1280x720 and 1920x1080, driving every screen three ways (mocked gamepad, keys,
  clicks); screenshots compared with the mock-ups by eye.
* On the Deck (the user): Game Mode launch, which Steam Input layout the page sees, Steam+X typing, suspend and resume, feel.

## Decisions

* The new design replaces the old UI; no second UI is kept after phase 7.
* stdlib only, no framework, no build step, no network.
* Wording: "Exclude" / "Excluded" in menus.
