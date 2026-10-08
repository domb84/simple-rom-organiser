# Version 0.2: a clean library built from the source, which stays untouched

## Goal

Point the app at a ROM folder (later: at the *root* of all ROM folders), choose a destination, and get a clean
library there: only what the rules keep, named and laid out properly. The source stays where it is, as an archive.

## Phases

| Phase | What | State |
|---|---|---|
| 1 | One system: *Build library in another folder* (`romorg/libexport.py`, Library tab "Where to build") | done |
| 2 | A ROM **root**: detect the system folders, scan them all, one global profile (with per-system overrides), one preview and one build for everything | done |
| 3 | Sync: re-run to add new games, remove what the rules no longer keep (only files the manifest says are ours), report drift | done |
| 4 | Extras: sidecars policy per system, playlist/gamelist export for frontends, verifying a built library against the DATs | later |

## Phase 1 design (as built)

* The selection engine is unchanged: `organiser.plan_library` / `discsys.plan_library` give a `LibraryPlan`. A
  kept op is `status in (move, ok)`, no reason `code`, not `unmatched`, no reserved `folder`. `libexport.kept_files`
  yields `(source file, path the in-place build would give it)`; the destination path is that path relative to the root.
* Transfer: `auto` / `hardlink` hard link on one file system else copy; `copy`; `symlink` (copy where links fail).
  Copies go to `<name>.romorg.part`, then `os.replace`; size is verified and the mtime is kept.
* Manifest: SQLite at `<dest>/.romorg-library/library.sqlite` (`runs`, `files`, `dirs`). The hidden folder is skipped by
  the scanner, so a destination can itself be scanned later. Undo removes a file only while it is unchanged.
* API: `/api/library/plan` and `/api/library/apply` take `export_to`, `export_mode`, `export_sidecars` (the plan answer
  gains `export`: counts, bytes, free space, conflicts, notes); `/api/library/export/runs`, `/export/undo`,
  `/export/settings`; settings come back in `/api/status` as `library_export`.
* With Copy the source is never changed, so an export does not re-scan. Move (copy / move only; no links) takes the files out of the source; it cannot be combined with sync.

## Phase 2 design (as built)

1. *Root scan*: the user picks `~/Emulation/roms`; `folder_hint` / `OTHER_SYSTEM_DIRS` map sub-folder names to systems
   (`gba`, `snes`, `psx` ...); the user confirms or corrects the mapping once and it is remembered per root.
2. *Global settings*: one profile (region priority, one-per-game, languages, flag rules, rating cut-off) is the
   default for every system; a system may override single fields. Stored beside the per-system profiles
   (`profiles["*"]`); `library.effective_profile(platform)` merges.
3. *Collection plan*: for each system run the existing scan + plan (jobs run one after another; DAT matching is
   per system already), then `libexport.plan_export` per system into `<dest>/<system folder>/`. One summary: per
   system kept / skipped / unmatched, total size, free space.
4. *Result*: `<dest>` holds one folder per system, ready for a frontend; the manifest is one per destination.

## Decisions taken in Phase 1 (change if wrong)

* Default mode is `auto` (a link costs nothing on one drive; exFAT and other drives get copies).
* The destination mirrors the in-place layout, including folder names.
* Save files, memory cards and notes are not copied unless asked; unmatched files are never copied.
* Global versus per-system settings: global is the default, per-system fields override (Phase 2).

## Phase 2 as built

* `romorg/collection.py`: `detect_systems` (folder hint + aliases), `effective_profile` (system defaults + the global rules;
  a rule a system lacks is not switched on), `clean_global`, `check_folders`.
* Settings in `config.json["collection"]`: `root, dest, mode, sidecars, systems{name:{path, enabled, own_rules}},
  global{rule fields}, last{dest, runs}`. "Own rules" uses the system's Library-tab profile.
* Job `collection` (`/api/collection/plan|apply`): per enabled system `_run_scan(keep=False)` (the UI's current scan is not
  replaced), `_make_library_plan(profile)`, `libexport.plan_export` / `apply_export` into `<dest>/<folder name>/`.
  One manifest per system folder; the last build's run ids are kept for `/api/collection/undo`.
* The preview and the build each scan (the hash cache makes the second pass cheap); nothing is held between them.
* Not done (Phase 3): removing files the rules no longer keep, drift reports.

## Phase 3 as built (sync)

* `plan_export(..., sync=True)`: files recorded in the manifest that the plan no longer wants become `Removal`s (only while
  unchanged; an edited one is reported as `kept_edited`). A manifest file whose source changed is a `replace` (written
  to `.part`, then `os.replace`; hard links and symlinks are re-made the same way). A manifest file the user edited is
  a conflict ("edited in the destination since it was built"). This is the drift report.
* Guards: nothing kept while the manifest holds files -> `ExportError`; removals over `MASS_MIN` (20) and over half of the
  library -> refused by `apply_export` unless `allow_mass=True` (UI: a second, red confirmation; API `allow_mass_removal`).
* Removals are re-checked at apply time, recorded in the `removed` table and undone by `undo_run` (re-linked or re-copied
  from the recorded source if it still exists; otherwise reported). Empty folders left behind are removed.
* Settings: `library_export.sync` (one system), `collection.sync`. Both default to off.
* Not done: a system that is switched off in a collection keeps what was built before (nothing processes it); the
  older version of a replaced file is not kept for undo.

## RetroArch saves (v0.2, in stages)

1. **Done:** `romorg/retroarch.py` and the RetroArch page: find installs (Steam, Flatpak, native, Snap, Windows, portable,
   custom), read the config, relocate save files and states (zip backup, journal, undo), write `retroarch.cfg` with a backup;
   refuses while RetroArch runs; reports core/game overrides.
2. **Done:** saves follow ROM renames (`<content name>.srm` / `.state*` / `.state1.png`, per core folder, disc games share the
   playlist name; copy for collection builds, move for in-place builds).
3. **Done:** BIOS / firmware check per core from the cores' `.info` files (`firmwareN_path`, `_opt`), found by checksum in the
   platform's ROM folder and placed in RetroArch's system folder.
4. **Done:** shared folders: `shared_folders` / `apply_shared` read the asset settings (menu assets, rdb, cheats, playlists,
   thumbnails, downloads, remaps, rgui config) against the folders of those names in a base folder and set the unused ones.

5. **Done (Amendment 30):** saves are part of the build. Profile rule `saved_games` = `keep` (default) | `archive` | `leave`:
   `library.select(..., saved=...)` keeps a game the rules would archive when RetroArch has saves for it (reason
   `saved_keep`); in `archive` mode the saves go with the archived game to `<archive>/<system folder>/_saves/<path below the
   save folder>` (journalled as a `libsweep`, undone with the build). The preview counts renamed / kept / archived save files
   and conflicts from the plan. The Collection sort no longer moves a loose save into the system folder with its ROM (it goes to
   `_other` like every non-ROM file). Not done: copying a save to the edition that replaces the game (rejected: unsafe), saves of
   multi-disc `.m3u` playlists and of ROMs already in `_excluded/` from an earlier build.

## Collection in place (v0.2)

`collection.place` = `elsewhere` (default) | `inplace`. In place: `_collection_run_inplace` scans each ticked system without
replacing the current scan, plans with the effective rules and applies the classic library build (`organiser.apply_renames` /
`discsys.apply_plan`); `last.runs[system] = {log, root}` for undo; saves follow like a single-system build.

## Sort a mixed folder and the archive area (v0.2)

`romorg/sortroot.py` (pure planning + journalled moves) and the server's `_collection_identify` / `_collection_sort`:
one `scanner.scan` over the root with the DATs of all ticked cartridge / flat systems (alt-hash strategies and containers
united) plus one `discsys.scan` per ticked disc system; a file that matches several systems is left alone. Moves go to
`<system folder>/`, loose unmatched files to `<aside>/_unmatched/<rel>`, non-ROM files to `<aside>/_other/<rel>`. In place
reorganises can sweep the reserved folders to `<aside>/<system folder>/<reserved>/`. Journals in `<data dir>/collection-undo/`.
Settings: `collection.aside`, `collection.sweep`, `collection.last_sort`. Concurrent settings edits merge under the config lock.

## Progress meter, tidy single systems, standard names (v0.2)

* `romorg/meter.py`: a process-wide byte counter the scanner (file sizes as they are covered), the disc reader, the exporter and the
  mover add to; reset per job and reported as `bytes` in the job's status. The UI adds elapsed, ETA (percent based) and rate.
  `_staged(job, i, n, label)` maps a step's progress onto its share of a Collection job (fractional `done`).
* `library apply` with `aside_to`: `sortroot.plan_sweep` + `apply_moves(kind="libsweep", library_log=...)` after the build; the
  library undo undoes that journal first.
* `/api/collection/rename/plan|apply|undo`: system folders to `Platform.folder_hint` (the ES-DE set); `detect_systems` also matches
  a folder named like the full system name.

## One pass over a mixed root (v0.2)

Identification reads each file once: `collect_files` walks the root once; `discsys.scan_many` finds the CHDs / cue / gdi sets once and
tries each disc against the Redump DATs of all disc systems (track-size prefilter first, track hashes cached and shared, so a
disc is decoded at most once); the other systems' ROMs are one `scanner.scan(files=...)` over the remaining files with all DATs;
leftover loose `.iso` files get one more look as PlayStation 2 DVD games (`discsys.identify_isos`). Disc files are never hashed
by the flat scan.

## Regions, automatic folder names, shared scans (v0.2)

* `tags.REGION_ALIASES` (`UK` -> `United Kingdom`); `canon_region`; `library.available_regions(dats)` and the server's cached
  `_available_regions` feed `profile_info` / the Collection page.
* Standard folder names are applied by the in-place reorganise and the sort (journal `rename`, undone with them); a build into another
  folder names the destination folders. The preview lists the renames (`renames` in the result).
* `_collection_scan` keeps a Preview's scans (`_coll_scans`, keyed by folder signature + DAT signature) for the Build.

## Collection: one scan, then metadata (v0.2)

`collection.RootScan` / `Sys` hold what one scan of the root found (`_collection_scan_work`: one `collect_files`, one
`discsys.scan_many`, one `scanner.scan` with all flat DATs, `identify_isos`). The preview and the builds derive everything from it:
`_collection_layout` (folder renames, `sortroot.plan_sort`, a `Mapper` from where a file is to where it will be),
`collection.virtual_flat` / `virtual_disc` (the system's scan result under those new paths), then the ordinary
`_make_library_plan`; in place the apply runs renames, sort, each system's `apply_renames` / `apply_plan`, the sweep and a rescan;
elsewhere `plan_export(src_map=...)` exports from the real files. No per-system on/off, no "find systems" step.

## CHD settings page, Convert first (v0.2)

The per-system Tools tab is gone (the Kickstart tool went too: the RetroArch BIOS & firmware check covers it; the `/api/kickstart/*` endpoints remain). Convert (raw discs to CHD; SNES / N64 clean-up) is `convert_on_build[platform]`
(`/api/platforms/options {convert}`): `library_apply` converts first (`_apply_conversions`, rescan) and links the two undo logs
(`libconv-*.json`); the library preview adds `convert {count, kind}`. Collection `convert` converts each system's
convertible units (`_collection_convertible`) after the sort, before planning from a fresh scan of that folder. CHD settings are the
`#/chd` view (same ids as the old chdman box); `chd_verify_scan` passes `full=True` to `discsys.scan` / `scan_many` (every
track hashed, so `match_chd` reports `verified`). `/api/convert/*` and `/api/dc/verify` remain as API.
