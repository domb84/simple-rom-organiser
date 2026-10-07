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
