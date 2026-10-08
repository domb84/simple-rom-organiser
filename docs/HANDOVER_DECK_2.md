# Handover to the Steam Deck, round 2 (v0.2 after the Windows pass 4 and the saves redesign)

Written 2026-10-08 on Windows for a Claude session (or a person) on the Steam Deck, any account. Nothing here needs the earlier
conversation. Read this file, then `docs/DECK_RUNBOOK.md` (the step-by-step Deck checks; its "Saves" step is the important one)
and the last sections of `docs/ARCHITECTURE.md` ("Windows pass 4", Amendment 31, "Folder picker and sidebar progress").

## Get the code

The work is on the branch **`v0.2`**, not on `main` (v0.2 has not been merged to `main` yet; do not merge it without asking the user).

```
git clone https://github.com/domb84/simple-rom-organiser.git ~/simple-rom-organiser     # or: git fetch && git checkout v0.2 && git pull
cd ~/simple-rom-organiser && git checkout v0.2 && git log --oneline -3     # expect b97cc97 or newer + a "Handover to the Deck, round 2" commit
```

The Deck has no GitHub login for pushing: commit on the Deck and either push with whatever credentials you have or hand the
user a patch (`git format-patch origin/v0.2`).

## What changed since the Deck last saw v0.2 (commit d5056eb, 28 commits, about 7,000 lines)

1. **Windows pass 4** (everything in `docs/HANDOVER_WINDOWS_4.md` was run on Windows): case-insensitive folders, read-only files,
   locked files (a move is a rename; a copy only to another drive and never left in two places), a journal written before each
   move, undo fixes, RetroArch detection and `retroarch.cfg` handling (byte-exact, BOM, line endings, backups), a Windows smoke
   test. Several of those bugs existed on the Deck too and are fixed for both.
2. **Saves redesign (Amendment 31, the user's specification)**: with no RetroArch config the app does not look at saves at all.
   With one, a scan builds a *save index* (`romorg/saveindex.py`): saves and states per game, matched to the ROM you have, a DAT
   title without a ROM, or nothing. Overview, Library and Browse show the counts; builds rename saves with their ROM (Flycast
   `.A1.bin` memory cards, `.1.srm`, `.eep` ... too). When a rule would replace/archive a game you have saves for, the default
   policy is *keep both ROMs*; alternatives *archive the saves with the ROM* and *leave the saves*, plus a per-game choice in the
   Library tab. Saves beside ROMs are ordinary files again (no special handling in the sort).
3. **The native "Browse..." dialog is gone** on both platforms (it opened behind the browser on Windows). Every folder field has
   the one in-app **Folders...** browser. `POST /api/fs/pick` and `dialog_available` no longer exist.
4. **Sidebar scan bar**: it never drew (its pieces were `<span>`s, which have no width). Fixed; it has no text of its own now.
5. The AppImage and the Linux smoke test check the v0.2 pages and the new self-check line `the app is complete`.

## What to do on the Deck, in order

1. `python3 -m unittest discover -s tests` (expect `OK`, about 1431 tests, 100+ skipped: browser tests skip without a Chromium,
   real-data tests skip without the variables). Then `python3 -m romorg.selfcheck`. Any failure is a regression of this work.
2. Build the AppImage (`packaging/build_appimage.sh`), run `packaging/smoke_test.sh` on it and `<AppImage> --self-check`.
   Last verified in a Deck-like Arch container (before the saves redesign): suite OK on the bundled Python 3.13, libFLAC and
   libzstd loaded from the bundle. Re-run it; the Deck itself has never run this code.
3. **The real saves check** (`docs/DECK_RUNBOOK.md`, "Saves"): the user's real `retroarch.cfg` lives on the Deck and points at
   their saves folder (`.../assets/saves/<core>/<game>.<suffix>`, sorted per core). On Windows only a *copy* of a config pointed at
   that folder could be used (read-only). On the Deck: open the RetroArch page, make sure the Deck's RetroArch is the selected
   install, scan SNES, and compare with the numbers measured from Windows on the user's real SNES folder with default rules:
   - 27 save files in 9 sets (9 saves, 18 states, 2 state screenshots not counted): 3 sets belong to ROMs the rules keep,
     4 to ROMs the rules would archive (Super Mario World (USA), Zelda ALttP (Switch Online), Star Fox (Japan), Yoshi's Island
     (Europe) (En,Fr,De)), 2 have no ROM and no DAT entry (Super Mario Collection (Japan) (Rev 1), Yoshi's Island (USA, Asia) (Rev 1)).
   - Policy *keep both ROMs*: 4 games kept for their saves (Kept/Excluded/Superseded 2,266 / 853 / 924); *leave* and *archive*:
     2,262 / 854 / 927, with 12 files of 4 games staying or going to `_saves`.
   Check Overview (Saves card), Library (Saves column, "Games with saves" filter, per-row choice) and Browse (Saves column and tab).
   Then do a real build on a COPY of a small ROM folder, and Undo it.
4. Folder fields: only **Folders...** exists now; try it on every page (Overview, Library "build elsewhere" / archive, Collection,
   RetroArch). The sidebar bar should visibly fill during a scan of a big folder.
5. Speed numbers (runbook step 5) against the old Deck numbers; no SD-card handling is wanted, assume NVMe.

## Decisions already made (do not reopen without asking)

- Copy or Move only, never links. Saves are never moved next to a ROM. No copy of a save to the edition that replaces a game
  (saves are not reliably region-compatible). Existing folders are never renamed (see `HANDOVER_WINDOWS_4.md`).
- The user's rule for non-ROM files: anything that matches no ROM database goes to the archive (`_unmatched` / `_other`).
- One consistent UI on both platforms: no per-platform dialogs. Linux must not regress; the Deck is the main target and the
  AppImage must stay self-contained (bundled Python 3.13, libFLAC, libzstd; see `steamos-priority` in the user's notes).
- Long paths (>260) and chdman with non-ASCII paths on Windows are not to be fixed. SD cards never matter.

## Open and parked

- Parked by the user: *Convert first* converts a headered file again when it lies in `_converted_originals` (question 5);
  an archive-only library build now writes a marker undo log (covers question 3).
- Not built: per-game saves choices inside a Collection's shared rules (only the default policy), a home-page saves badge,
  saves of multi-disc `.m3u` games, saves of ROMs already sitting in `_excluded/` from older builds; saves that appear after a
  scan are seen at the next scan. A save set with no ROM and no DAT entry is reported as "unmatched".
- A `ResourceWarning: subprocess N is still running` may print at the end of a full test run on Windows (a Popen of a test is
  not waited for); no process is left behind.
- Why Windows dropped the topmost flag of the old native dialog was never found; the dialog is removed, so it no longer matters.

## Windows packages (for reference)

Built from this branch on Windows: `dist\Simple_ROM_Organiser-0.2.0-win64.zip` (14.6 MB) and `.exe` (12.5 MB), both passing their
self-check and smoke test. `dist\` is git-ignored; rebuild with `packaging\build_windows.ps1` / `build_windows_exe.ps1` (they need
the `py` launcher with Python 3.14).
