# Handover 4: bring Windows to parity with v0.2 (Collection, library elsewhere, RetroArch, one move per file)

Written 2026-10-07 on the Steam Deck (SteamOS, Python 3.13) for a Claude session on a Windows machine. Read
`docs/HANDOVER_WINDOWS_3.md` (the nine extra systems), `docs/V0_2_PLAN.md` (the design of v0.2 as built) and
`docs/ARCHITECTURE.md` (the last section, "v0.2: removed and changed") first. Nothing here needs the earlier
conversation. **Nothing below was run on Windows.** The code is stdlib-only Python, so most of it should just work; this
file lists what v0.2 added, what must be checked on Windows, and where Windows is most likely to differ.

Branch `v0.2` (not merged to `main`). The Deck has no working GitHub login: if `git log` on your checkout lacks the
newest commit ("v0.2: one move per file ..."), ask the user to run `git push origin v0.2` on the Deck.

## What v0.2 added (all platform independent unless noted)

1. **Sidebar and detail UI.** Home page, a sidebar of systems, per-system tabs *Overview / Library / Browse* (the Tools tab is gone),
   pages for **Collection**, **RetroArch** and **Disc images (CHD)** (`view-collection`, `view-retroarch`, `view-chd`).
2. **Library, in place or elsewhere.** *Build library* has a destination: the folder itself, or another folder (copy or move, never
   a link; optional *keep in sync*). `romorg/libexport.py`. An archive folder for what the rules set aside (`<ROM folder>-archive`
   by default, outside the ROM folder).
3. **Collection** (`romorg/collection.py`, `romorg/sortroot.py`, `romorg/meter.py`, server `_collection_*`). One *Scan* reads
   a whole ROM folder once (every file, every DAT of every system) and finds each file's system by checksum, whatever its folder.
   Preview, build and undo then work from that scan. In place: every file goes straight to `<root>/<standard short name>/`
   (EmulationStation-DE names: `snes`, `megadrive`, `psx`, `atarilynx` ...). Existing folders are never renamed, emptied ones
   are removed. Files matching nothing go to `<root>-archive/_unmatched/...`, non-ROM files to `_other/...`, what the rules
   exclude to `<root>-archive/<system>/_excluded|_superseded|_incomplete|_duplicates/...`. Elsewhere: copy or move into a clean library.
4. **One move per file.** `server._collection_single_pass` merges the sort and the library plan so a file moves once, from where it
   is to its final name and place (or the archive). Archive moves lie outside the ROM folder, so they are journalled by
   `sortroot.apply_moves` (JSON in `<data dir>/collection-undo/`); the rest by `organiser.apply_renames` with the **ROM root** as
   journal folder (`.romorg-undo-*.json` files in the root, hidden by the leading dot and skipped by the scanner).
   *Convert first* (`_collection_convert_first`) converts from where the raw files lie, then reads the folder again.
5. **Convert first** option in Library and Collection (raw discs to CHD for disc systems; SNES copier-header / N64 byte-order clean-up).
   The per-system Convert / Verify tools are gone; CHD settings are on the *Disc images (CHD)* page; *Check every track while
   scanning* replaces Verify.
6. **RetroArch page** (`romorg/retroarch.py`): finds installs (Steam, Flatpak, native, Snap, Windows, portable, custom `retroarch.cfg`),
   reads and writes `retroarch.cfg` (with a backup), moves saves / states into the same folder structure as the ROMs (per core),
   renames saves with the ROM (*saves follow ROM renames*), zip snapshot backup, and a **BIOS & firmware check** across every
   installed core and every system that has a ROM folder. The Amiga Kickstart tool is gone (this replaces it); the
   `/api/kickstart/*` endpoints and `romorg/kickstart.py` still exist but no UI calls them.
7. **Regions** (`tags.REGION_ALIASES`, `canon_region`, `library.available_regions`): one name per region, the list built from the
   data, a language-aware default (*keep other-language titles* when that is the only version).
8. **Removed API:** `POST /api/organise/plan|apply` (use `/api/library/plan|apply`). `/api/organise/undo` and `/undo-logs` stay.

## Steps, in order

1. `py -3.14 -m unittest discover -s tests` from the repo root. Deck result: **1238 tests, 42 skipped, all OK** (about 200 s).
   `tests/test_browser.py` drives a real headless Chromium-family browser through `tests/cdp.py`; on the Deck it is Brave (flatpak).
   On Windows it needs a Chrome / Edge / Brave path: see how `tests/uifixture.py` finds one and set the variable it reads if it
   skips. Expect a handful more skips than the Deck; anything that **fails** is a Windows regression of this pass: fix it, do not
   skip it. Pay most attention to `test_sortroot.py`, `test_collection.py`, `test_libexport.py`, `test_retroarch.py` and the
   collection tests in `test_server.py`, `test_ps_server.py`, `test_dc_server.py` (names contain `collection`, `archive`, `single`).
2. `py -3.14 -m romorg.selfcheck --require-native`: everything `OK`.
3. Build both Windows packages (`packaging\build_windows.ps1`, `packaging\build_windows_exe.ps1`), run the self-check inside each,
   and start the exe with no config: the home page, the sidebar, *Collection*, *RetroArch* and *Disc images (CHD)* pages must
   open. The Deck builds are `packaging/build_appimage.sh` and `build_pyz.sh`; `packaging/smoke_test.sh` has the checks the
   Windows scripts should mirror (`/`, `/api/status`, `/api/updates`, `/api/library/profile`, `/api/chdman`, `POST /api/quit`).
4. **Manual run on a real folder** (the Deck runbook is `docs/DECK_RUNBOOK.md`). Make a mixed folder with a few ROMs of two or
   three systems in oddly named sub-folders, a duplicate, a ROM no DAT knows and a picture. Then:
   - *Collection > Scan*: the *Systems found* table shows the folder each system will get; one *Unmatched files* row; no per-system
     unmatched column; nothing reads the files again when you press *Preview*.
   - *Preview* then *Build library*: files land in `<root>\snes`, `<root>\gba` ..., the old folders are gone, the archive folder
     `<root>-archive` holds `_unmatched`, `_other` and `<system>\_duplicates`. **Check with Process Monitor or a log that no file is
     moved twice** (the undo logs in the root, plus the `sort` journal in the data folder, must have no file as both a
     destination and a source: the server tests assert exactly this).
   - *Undo last* puts everything back, including emptied folders.
   - With *Convert first* ticked and a headered `.smc`: the clean `.sfc` appears in `snes\`, the original in
     `snes\_converted_originals\<its old folder>\`; undo returns it.
   - *Build elsewhere* with Copy and with Move into a folder on **another drive**; with *keep in sync*.
5. RetroArch page on Windows: it must find `%APPDATA%\RetroArch\retroarch.cfg` or a portable install next to `retroarch.exe`, read
   the save / state / system directories, and refuse to write while `retroarch.exe` runs (check how `retroarch.py` detects a
   running instance: it is process-list based and was only tested on Linux). *Move saves* across drives, *saves follow renames*,
   *BIOS & firmware check* against a real `system` folder, backup zip and *Undo*.

## What is most likely to break on Windows

1. **Case-insensitive paths.** A folder called `SNES` is the same folder as the standard `snes`. `sortroot.inside` uses
   `Path.relative_to` (case-insensitive on Windows) and `Names.free` compares casefolded names, but `collection.Sys.current` /
   `std` and `Mapper._inside` compare `Path` objects: check that a root which already has `SNES\` and `GBA\` is left alone (no
   moves, no `snes (2)`), and that `Gameboy\` becomes `gb\`.
2. **Moving across drives.** The archive folder is next to the ROM folder by default (same drive), but a user may put it elsewhere:
   `sortroot.apply_moves` falls back to `shutil.move` (copy then delete, not atomic) and journals after the move. Test an
   archive on another drive including a cancel in the middle, and undo.
3. **Sharing violations.** Antivirus and the Windows indexer hold files briefly. `organiser.apply_renames` already retries blocked
   targets and breaks swap cycles with a temporary name; `sortroot.apply_moves` and `libexport` have no retry: a failed move is
   reported in `failed` and nothing is lost. If you see many failures on a real library, add a short retry (3 tries, 100 ms)
   in `sortroot.apply_moves`, not in the callers.
4. **Path length.** The archive prefix and `_converted_originals\<old path>` make paths longer than the sources. The known
   Windows limit (260 characters, decision of the Windows pass 2) applies; an over-long move fails with the usual hint
   (`winproc.long_path_hint`). Check that a failure is reported per file and does not stop the run.
5. **Removing empty folders** (`sortroot.remove_empty_tree`, `organiser._remove_empty_dirs`): `os.rmdir` on a folder that
   Explorer has open fails; it must be ignored silently (it is).
6. **Folder pickers.** The *Browse...* buttons use the native dialog when `/api/status.dialog_available` is true. On Windows that
   is the one from the earlier passes; check it for the new fields (*Collection root*, *destination*, *archive folder*, the
   RetroArch folders).
7. **File names the OS refuses.** `organiser.safe_filename` handles reserved characters; the new standard folder names are plain
   ASCII. A DAT name ending in a dot or a space, or `CON`/`NUL`, is an existing concern, not a new one.
8. **Threads and the progress meter.** `romorg/meter.py` is a lock-protected counter used by the scan, the sort and the exports;
   `chdsched` worker processes do not touch it. Nothing OS specific, but the *data scanned* figure and the ETA in the Collection
   page are new: check they move while a scan runs.
9. **The browser tests.** They assume a Chromium-family browser with remote debugging; if none is found they skip, which hides
   regressions in the sidebar, the Collection page and the CHD page: run them by hand once (open each page, press each button).

## Decisions taken (do not undo without asking the user)

* Copy or Move only; **no links** (symlink / hard link) anywhere.
* Collection has a single *Scan* button; everything else works from the scan's metadata. All systems with matching ROMs are built;
  there is no per-system on / off. *Own rules* is the only per-system control.
* Existing folders are never renamed; files go to the standard folder and emptied folders are removed.
* The archive folder is outside the ROM folders, default `<ROM folder>-archive`. Files that match no checksum are not guessed to
  belong to a system: `_unmatched` / `_other` at the top of the archive.
* Incomplete multi-disk Amiga sets are expected (the DATs themselves lack disks); no special `[use ...]` handling.
* A rule-set aside file goes straight to the archive (never via `_excluded` in the ROM folder).
* The Amiga Kickstart tool is not coming back: the RetroArch BIOS & firmware check is the way.

## When you are done

Add a short "Windows pass 4" section to `docs/ARCHITECTURE.md` with the numbers (tests run / skipped, the manual checks above,
anything you changed) and commit on `v0.2`.
