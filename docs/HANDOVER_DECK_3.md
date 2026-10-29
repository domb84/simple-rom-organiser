# Handover, round 3 (v0.5 on the Steam Deck)

Written 2026-10-26 on the Steam Deck for a Claude session (or a person) on any account. Nothing here needs the earlier
conversation. Read this file, then `docs/PLAN-v0.5.md` and the last amendments of `docs/ARCHITECTURE.md` (70-87).

## Where the work is

- Repository: `/home/deck/Dev/simple-rom-organiser`, branch **`v0.5`** (not merged to `main`, not pushed; do neither without asking
  the user). Last commits: `5328f14 System disks counted apart from games`, `4459de9 Amiga playlists around WHDLoad ...`,
  `2df270f Version 0.5.0: big-screen UI ...`.
- **Uncommitted work in the tree** (all tests pass; built into `dist/` as 0.5.0): Amendments 84-90 below. Commit only when the user
  asks (they say "commit" when they want it).
- Packages: `dist/Simple_ROM_Organiser-0.5.0-x86_64.AppImage` and `dist/simple-rom-organiser.pyz`.
- Claude's notes from earlier sessions, if your account sees them: `~/.claude/projects/-home-deck-Dev-simple-rom-organiser/memory/`
  (`MEMORY.md` is the index: the user's library rules, wording, testing set-up, RetroArch paths). They are on this machine only.

## What v0.5 is (one paragraph)

The default UI (`/`) is a new big-screen UI for 1280x720, driven by controller, keyboard or mouse: `romorg/static/next.html/.css/.js`
(own vanilla kit, no framework; the CSP forbids style attributes, so styles go through the CSSOM). The old UI is at `/classic`. The
server (`romorg/server.py`, stdlib only) is unchanged in shape: a JSON API the UI calls. Background tasks carry on when their screen
is left (a toast when done, "What's running" in the menu).

## Done since the last commit (uncommitted)

1. **Amendment 84, speed.** Opening a system took ~10 s (the saved scan was summed up twice). The summary is saved with the scan
   (`SUMMARY_VERSION`), the last-used system's scan is read at startup (`App.preload_last_scan`, checked against the folder once when
   opened: `_still_current`), and the page shows at once with loading tiles. The whole-database count went 46 s -> ~18 s (caches in
   `tags`, `library.combined_key`, `whdready.load`; no second selection for "rated titles"). User's Amiga: click 10.5 s -> 1.2 s,
   totals 50 s -> 24 s, build preview 56 s -> 34 s.
2. **Amendment 85, games not disks.** "Games you have" on the Amiga counted DAT entries (one per disk). `App._game_counts`: releases
   grouped into games by `library.game_key`; yours when every disk of one release is there; system disks are no games.
   User's data: 4,484 of 4,780 games.
3. **Amendment 86, Needs a look = 2, not 8.** Every incomplete disk of a game the rating drops is excluded for its rating
   (`library._rate_incomplete`); a better version is a release, not a disk (`totals._release_of`); one row a game on the page.
4. **Amendment 87, translations.** `[tr xx]` counts as language xx only (it was also the original's language).
5. **Amendment 88, Files list.** Made in the background after the system page (after the totals), and 27 s -> 20 s; Needs a look
   no longer flashes its placeholder.
6. **Amendment 89, Missing tile.** Counts what the rules keep that you lack (was the whole database's 296).
7. **Amendment 90, every system.** "Games you have" counts games on every system (No-Intro regions / revisions, Redump discs were
   counted one each); the Switch's updates and DLC are no games and never missing. Checked on all 16 of the user's systems.
8. **Amendment 91, calculation times.** Version choice remembered (`library.base_selection`), the database's item list shared
   (`totals.target_items`), plain-text paths in the big loops. A top-N / rating change: ~39 s -> ~23 s of calculations on the Amiga.
   Left: totals before the preview (UX choice), remembered `rated_all`.
9. **Amendment 92, one release per Amiga game.** `library._one_release` (CD32 disc, else the WHDLoad-boot version, else best original /
   floppy set), `organiser._into_game_folders` (`Commodore Amiga - Games/<title>/`).

## Open, only if the user asks

- Share the user-files selection between the build preview and the totals (the version choice is shared now; they differ slightly: the preview counts
  saves - `prefer_saved`).
- Remove the classic UI; Game Mode launcher (Steam shortcut); running several tasks at once.
- WHDLoad saves (`saves/PUAE/WHDSaves/`) are not shown on the Saves page.
- Check PUAE's floppy write-redirect option: saves written into an ADF would stop it matching TOSEC.

## How to work here (the user's standing rules)

- UI wording: "Exclude", never "Leave out". Plain, short words. What the rules exclude on purpose is never a "problem" to report.
- Ask before decisions that change library behaviour for everyone (as with the translation rule); the user decides quickly.
- Never touch the user's real data for experiments: `~/.local/share/simple-rom-organiser` (holds `s3-secret`, do not copy it) and
  `~/MEGA/Emulation/...`. To measure, copy the data folder minus `s3-secret` to a scratch folder and run the app with
  `ROMORG_DATA_DIR=<copy> ROMORG_OFFLINE=1 ROMORG_RETROARCH_DETECT=0 python3 -m romorg --port 38271 --no-browser --new-instance
  --no-update`, then scan the Amiga folder in it (read-only: the scan writes nothing to the ROM folder when there are no
  `.romorg-undo-*` files in it - check first).
- Stop processes by PID only (`flatpak-spawn --host ss -ltnp | grep :<port>`, then `flatpak-spawn --host kill <pid>`): a `pkill`
  pattern in the same command kills its own shell.
- The sandbox has no Node and no browser; the host has the Brave flatpak. `python3 -m unittest tests.test_browser` starts it
  itself (headless, via `flatpak-spawn`). In `cdp.wait` a script that throws counts as true: use `?.` in test lookups.

## Checks before handing back

```
python3 -m unittest discover -s tests          # expect OK, about 1,960 tests, ~53 skipped (8 min; browser tests included)
bash packaging/build_appimage.sh && bash packaging/build_pyz.sh
EXTRACT_AND_RUN=1 bash packaging/smoke_test.sh dist/Simple_ROM_Organiser-0.5.0-x86_64.AppImage    # expect SMOKE TEST PASSED
rm -rf /tmp/appimage_extracted_*
```

Do not rebuild the AppImage while the full suite runs (a packaging test reads it).
