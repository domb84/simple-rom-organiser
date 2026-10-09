# Nintendo Switch: games and saves of Eden and Ryujinx

What is built (2026-10-09) and the plan for moving saves between the two emulators. Facts below were measured on the user's Steam
Deck (Eden with a custom NAND under `roms/switch/yuzu`, Ryujinx portable in `roms/switch/portable`, one shared games folder).

## Built: tidy, checksums and save counts (2026-10-09)

* `switchverify`: every `.nca` of an NSP / XCI is hashed and compared with its own name (content ID = first 16 bytes of its SHA-256); a verdict per file is
  remembered in `<data>/switch/verified.sqlite`. Compressed files are not checked.
* `switchtidy`: the rules and the plan (`TidyRules`, `plan_tidy`); applied through `sortroot.apply_moves` (journal kind `switch`) and undone with
  `undo_moves`. Archive: `<archive>/switch/{_superseded,_duplicates,_unmatched}/<path below the games folder>`.
* Saves: every non-empty save folder is one *save game*: Eden `<profile>/<title>` per profile plus device data, Ryujinx account and device saves.

## Built: recognising the games

A Switch file cannot be matched by checksum the way other systems are: a card dump is many gigabytes, an eShop file is encrypted, and
the No-Intro style DATs hash the whole file. The unit that both emulators and every tool use is the **title ID**, and the container
headers give it without any key:

| file | how the title ID (and version) is read | key needed |
| --- | --- | --- |
| `.nsp` / `.nsz` (PFS0) | `<rights id>.tik` file name (first 16 hex digits); `*.cnmt.xml` (id, type, version); the NCA names (content IDs) looked up in the title database | no |
| `.xci` / `.xcz` (HFS0) | `[title ID]` in the file name; or the header of the small CNMT NCA (512 bytes, AES-128-XTS with `header_key` from the user's `prod.keys`) | only for the second way |

Why the NCA names of a card dump do not work: they are a different build from the eShop one, and the community database
(`blawar/titledb`) lists the eShop ones. A wrong `[title ID]` in a name is caught when the keys can read the header.

Names come from `titledb` (`US.en.json` for the names, `cnmts.json` for NCA ids and which application an update / add-on belongs to),
fetched on demand (32 MB gzip) into a 25 MB SQLite file in the data folder. Without it everything still works; games just have the
name of their file.

Modules: `switchfmt` (containers, ids), `switchkeys` (prod.keys, pure-Python AES-XTS), `switchdb` (title database), `switchscan`
(a folder), `switchsaves` (save folders), `switchapp` (the page's backend). Page: sidebar *Nintendo Switch*.

## Built: finding and matching saves

Both emulators keep a game's save as a plain folder of the files the game wrote (the same names and sizes in both: Super Mario
Odyssey's `Common.bin` 1036 bytes and `File1.bin` 2,097,164 bytes are identical in the two), keyed by title ID:

* Eden / yuzu: `<nand>/user/save/0000000000000000/<profile UUID, 32 hex>/<title ID>/...` (all-zero UUID = device data).
* Ryujinx: `bis/user/save/<save id>/{0,1}/...`. The save id is a counter, not the title. The title is in `ExtraData0`: the first 8
  bytes (little endian) are the title ID, the next 16 the user ID, the byte at `0x20` the type (LibHac: 0 system, 1 account, 2 bcat,
  3 device, 4 temporary, 5 cache). `0` is the committed copy, `1` the working copy.

The Switch page lists every title that has a save in either emulator with: the files and size in each, the newest file's date, and a
verdict: *same* (same files at the same sizes), *Eden newer*, *Ryujinx newer*, *Eden only*, *Ryujinx only*; and whether a game file
is in the games folder.

## Plan: moving saves (not built, nothing is written yet)

Both formats are plain file trees, so a transfer is a copy of the files, never a conversion.

1. **Match by title ID, then by profile.** The profile IDs differ between the emulators (Eden `A188...3D1D`, Ryujinx
   `00000000000000010000000000000000`, "Dom" in `system/Profiles.json`). The saves do not say whose they are by name. Default: when each
   emulator has exactly one profile (the user's case) they are paired; otherwise the page asks once and remembers. Device data and
   cache/bcat saves are left out.
2. **Eden -> Ryujinx** only into a save that Ryujinx already has (start the game once in Ryujinx): replace the files in
   `bis/user/save/<id>/0` **and** `1` (both, so the working copy does not undo it), leave `ExtraData*` and `saveMeta` alone. Creating
   a save from nothing needs Ryujinx's own bookkeeping (a new save id, `ExtraData0/1`, `saveMeta/<id>/...` and an entry in
   `system/save/8000000000000000/{0,1}/imkvdb.arc`, an "IMKV" key/value file): possible but easy to get wrong, so not done.
3. **Ryujinx -> Eden**: Eden needs no index, so a missing `<profile>/<title>` folder is simply created; an existing one is replaced.
4. **Safety, as with the RetroArch saves:** a preview that lists every file; a zip backup of what is replaced (kept in the data
   folder, with an Undo); copy, never move; nothing while the emulator runs (process check, like `retroarch.is_running`); the
   default direction is "newer wins" and a game where the destination is newer asks first; no overwrite of a different file by a
   same-named folder.
5. **Games:** Ryujinx and Eden can share the games folder (they already do); nothing to move. The app only has to leave
   `games/<title id>/{gui,cache}` (Ryujinx's per-game data) alone.
6. **To check before building 2:** that Ryujinx accepts a replaced `0`/`1` without touching `ExtraData` (test on a copy of the user's
   portable folder with the three games whose saves are already the same in both).
