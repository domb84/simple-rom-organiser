# Handover 3: check the nine new systems on Windows (feature parity with the Steam Deck build)

Written 2026-10-06 on the Steam Deck (SteamOS, Python 3.13) for a Claude session on a Windows machine. Read
`docs/HANDOVER_DECK.md` (what the Windows pass left for Linux) and `docs/HANDOVER_WINDOWS_2.md` (what the Windows pass
did) first if you have not; this file only covers what was added afterwards. Nothing here needs the earlier
conversation. **Nothing below was run on Windows.**

Check first that your checkout has commit `2881515` ("Nine more systems: eight No-Intro cartridge systems and
Nintendo GameCube") and this file. The Deck has no working GitHub login, so those commits may still be unpushed: if
`git log` lacks them, stop and ask the user to run `git push origin main` on the Deck.

## What was added (one commit, `2881515`)

1. **Eight No-Intro cartridge systems**: Game Boy, Game Boy Color, Nintendo DS, Sega Mega Drive - Genesis, Master System,
   Game Gear, 32X, Atari Lynx. Each is one `_nointro(...)` entry in `romorg/platforms.py`, its DAT name in
   `nointro.NOINTRO_DATS` (now 12 DATs, about 15 MB the first time), and a LaunchBox name in `ratings.LB_PLATFORMS`.
   No new code paths: the same scanner, organiser and Library as GBA / NES.
2. **Nintendo GameCube** (`Nintendo GameCube`, flat folder, `.iso` / `.gcm` / `.rvz`, Redump DAT "Nintendo - GameCube",
   2,019 games). This one has new code:
   - `romorg/rvz.py`: reads Dolphin's RVZ and rebuilds the ISO it stands for while hashing it (Zstandard, bzip2, LZMA,
     LZMA2 or stored groups; regenerated padding from a Lagged Fibonacci generator). Wii and WIA are refused.
   - `scanner.scan(..., containers=("rvz",))` and `Platform.containers`: an `.rvz` gets the hashes of the ISO, with
     `Match.matched_via == "container"`; `organiser._target_name` keeps its `.rvz` extension when renaming.
   - `chdworker.py` has a new request `op: "rvz"`; `rvz.hash_image` starts workers with `chdsched.spawn_worker` and hashes in
     order in the caller; a worker that fails hands its slice to the caller.
   - `redump.py`: `GC_DAT_NAME`, `SYSTEMS["Nintendo - GameCube"] = "gc"`, in `REDUMP_DATS`.
   - `selfcheck.check_rvz` (a 1.4 KB embedded RVZ) is a **required** self-check line; `romorg.rvz` is in the AppImage import list.
   - UI: an "rvz" chip (`VIA_LABEL` in `app.js`); Convert is hidden (not convertible).
3. Docs: README (systems table, a GameCube section), `docs/ARCHITECTURE.md` Amendments 23 (the eight systems) and 24
   (GameCube), `docs/PACKAGING.md`.

## Steps, in order

1. `py -3.14 -m unittest discover -s tests` from the repo root (Git Bash or PowerShell). On the Deck: **1097 tests, 41
   skipped, all OK**; the Windows pass had 46 skips at the previous commit, so expect a similar difference. Anything that
   fails is a Windows regression of this pass: fix it, do not skip it. Pay attention to `tests/test_rvz.py` (27 tests; the
   worker-process ones, `test_a_worker_that_cannot_start_or_dies_is_replaced_by_this_process`, and the scan / rename / undo
   ones are the ones most likely to differ on Windows).
2. `py -3.14 -m romorg.selfcheck --require-native`: look for `OK    the RVZ reader rebuilt a test GameCube image exactly`.
   On Python 3.14 it should name `compression.zstd` as the Zstandard library (on the Deck it names `libzstd.so.1`); it must
   be an `OK`, and `ROMORG_NO_ZSTD=1` must still give an `OK` through the built-in Python decoder.
3. Build both Windows packages (`packaging\build_windows.ps1`, `packaging\build_windows_exe.ps1`) and run the self-check
   inside each: `romorg.rvz` is picked up by `--collect-submodules romorg` and by the build's module list, but the exe's
   `--selftest` must import it and the frozen worker (`<exe> --chd-worker`) must answer an `op: "rvz"` request.
4. Real-data runs (need the user's files; the Deck results are the oracle):
   - GameCube: a Dolphin `.rvz` whose rebuilt ISO must equal Redump. Deck numbers: *Super Mario Sunshine (USA, Canada)*,
     CRC32 `771ad977`, SHA-1 `8d094f2c5c112aba9660f0478b16c7f2caf63cbf`, size 1,459,978,240. `tests/test_rvz.py::RealFileTest`
     runs it when `ROMORG_REAL_GC_DIR` points at the folder (default is the Deck's `/home/deck/MEGA/...` path, so on Windows
     set the variable). Also scan the whole folder through the app's own Redump download
     (`redump.update_dats(["Nintendo - GameCube"])`, then `scanner.scan` with the platform's `containers`), as the Deck did:
     3 of 3 matched, `correctly_named` 3, a cached rescan 0.03 s.
   - `python tools/fetch_real_dats.py <folder>` now also downloads the GameCube DAT (it uses the app's downloaders, so
     `REDUMP_DATS` and `NOINTRO_DATS` are covered); set `ROMORG_REAL_SCRATCH` and run `tests/test_datfile.py` for the real
     counts of the eight No-Intro DATs (Game Boy 2254 / 2238 / 2254 / 0, Color 2566, DS 7701, Mega Drive 3365, Master
     System 1163, Game Gear 915, 32X 207, Lynx 681 rom blocks, 364 sets, 235 with alternates).
5. Speed: nothing measured on Windows yet. Deck reference (8 threads): the 1.4 GB Sunshine image hashes in **3.4 s with 4
   worker processes**, 3.8 s with 8, 10 s in one process; the first version of the padding generator took 106 s, so a Windows
   time in the tens of seconds means the workers did not start (the caller then decodes alone: look for slow wall time and
   one busy core). A scan of three discs cold took 14 s on the Deck. Record the Windows numbers in
   `docs/ARCHITECTURE.md` Amendment 24 next to the Deck's.

## What is most likely to break on Windows

1. **Worker processes for decoding RVZ.** `rvz._parallel` talks to `chdsched.spawn_worker()` over pipes: a JSON line with
   `op: "rvz"`, the reply `{"id", "n"}` followed by `n` raw bytes, read through `io.BufferedReader`. The frozen exe must
   handle the request (the worker loop is the shared `chdworker.serve`; the `rvz` branch imports `romorg.rvz` lazily).
   Failures are silent by design (the caller decodes the slice itself), so a broken pool shows up as slowness, not an
   error: time it, and check the process count while it runs.
2. **Open files and renames.** The scan opens the `.rvz` in the caller and in the workers. The workers are killed when
   `hash_image` returns (`chdsched.kill_worker`), so a rename right after a scan must not hit a Windows sharing violation:
   `tests/test_rvz.py::PlatformTest::test_rename_keeps_the_extension_and_undo_restores_everything` exercises exactly that;
   it must pass. The worker's `release` request also closes cached images (`Scheduler.release`).
3. **Zstandard.** The Windows builds use Python 3.14, so `compression.zstd` decodes RVZ groups; `zstdnative.compress` (used
   only by the test-side RVZ writer) needs it too. Tests that need to *compress* skip when no library can.
4. **Paths.** `rvz.Rvz(path)` opens the path as given; long paths (over 260 characters) are the user's known Windows
   limit and not our problem (decision of the Windows pass). Non-ASCII names in a GameCube folder are untested here.
5. **Cancel.** `hash_image` raises `InterruptedError`; `scanner.scan` turns it into `ScanCancelled`. Check that cancelling a
   scan of a big `.rvz` stops promptly and leaves no worker processes behind (Task Manager).
6. **Line endings / tests.** `tests/rvztestlib.py` and the new tests write binary files only; `random.randbytes` needs
   Python 3.9+.

## Expected, not bugs (seen on the Deck with the user's real folders)

Scans of the user's Mega Drive / Master System / 32X / Lynx folders matched 646 / 249 / 31 / 68 files; the unmatched ones are
hacks, translations, homebrew and BIOS files (the libretro No-Intro DATs contain no BIOS) plus 13 Mega Drive dumps whose size
differs from the DAT's. The Organise plan would move those into `_unmatched/` (undoable). The user was asked whether they want a
"leave these folders alone" setting (like the Amiga Kickstart folder): **no answer yet; do not build it unasked.**

## Decisions already made (do not reopen without asking)

- GameCube only reads `.iso`, `.gcm` and `.rvz`. Not read: Wii discs (encrypted partitions: rebuilding them would need the
  H0-H3 hash tree and AES through OpenSSL's libcrypto), WIA, GCZ / CISO, NKit. Wii RVZs found in a GameCube folder are
  listed as unsupported on purpose.
- Sega Model 3 (the user has a Supermodel folder) is **not** in the app: it needs a matcher for the files inside zips, with
  Supermodel's `Games.xml` (63 games, CRC32 only) as the source. The user said "just add" the console work; leave it.
- Everything decided in `HANDOVER_DECK.md` still stands (chdman optional and unbundled, Zstandard preset off by default, only
  createcd / createdvd are written).

## Not done anywhere

- Windows run of any of this (steps 1-5).
- No emulator was started on anything new; GameCube matching is verified by SHA-1 against Redump only.
- A mixed Game Boy / Game Boy Color folder is scanned once per system (two separate platforms).
