# Handover 2: Windows CHD parity, paused after phase 2 (2026-10-06)

Follows `docs/HANDOVER_WINDOWS.md`. Goal: the built-in CHD reader/writer on Windows with feature parity, no chdman
dependency, at chdman's speed or better. User decisions (final): ship libFLAC DLL in zip and exe; Windows builds on
Python 3.14 (compression.zstd, no libzstd); chdman 0.289 downloaded only as a test oracle.

Nothing is on `main` or pushed. All work is on local branches.

## State of the branches

| Branch | Where | State |
|---|---|---|
| `windows-parity` | main checkout | Phase 1 merged (win-fixes + win-packaging). 1004 tests OK, 42 skipped; selfcheck passes with libFLAC. **The base to merge into.** |
| `p2-ui` | `.claude/worktrees/wf_c0845ae7-cea-3` | Done and reviewed (ok-with-nits); fixup commit 25199c9 landed. Liveness check without `os.kill`, chdman.exe next to zip found, per-platform hints, FLAC-encoder status in Convert step, winpthreads/MinGW notices, pinned PyInstaller, ARCHITECTURE Amendment 23. Fixup's own test run was cut off: rerun the suite. |
| `p2-writer` | `...-cea-2` | 6 commits, complete per task list but **not reviewed**: faster `_build_map`, broken pool frees files, writer honours `chd_workers`, release timeout restarts workers, workers start 2x faster, `tools/bench_chdwrite.py`, `WINDOWS_MAX_AUTO=12` kept (measured). |
| `p2-reader` | `...-cea-1` | Huffman last-bit fix (a1b02d4) + a server.py POST-body fix (fc3d6d3). **Not reviewed**; real-data check (chdman's thps4_dvd.chd + originals) may be incomplete. Its test logs: `.claude/worktrees/p2r_run*.txt`. |
| `p2-parse` | `...-cea-4` | 38fb8cb (cue/gdi parsed like chdman 0.289, safe generated GDI) + **76658b4 WIP** (real-data test env vars, discsys edits) committed by me when stopping: **unreviewed, tests not run.** |

Expected merge conflict: `romorg/server.py` — p2-reader fc3d6d3 and p2-ui 3d99786 both "read the body of a rejected
POST". Keep one. `romorg/discsys.py` is touched by p2-writer (worker plumbing) and p2-parse (GDI helpers).

## To continue (in order)

1. Finish p2-parse: in its worktree, check the WIP commit, run `ROMORG_LIBFLAC=<lib> py -3.14 -m unittest discover -s tests`, finish task items 3-4 (real-data tests via env vars pointed at the Windows data; long paths through a full Convert).
2. Adversarially review p2-writer, p2-reader, p2-parse (`git diff windows-parity...<branch>`); fix findings. The workflow script that does impl→review→fixup per track: `~/.claude/projects/C--Users-DominicBird-Repos-simple-rom-organiser/5cdc4aaa-.../workflows/scripts/windows-chd-phase2b.js` (task texts per track are in it). Drop the `ui` track and edit the "RESUMING" text if re-run.
3. Merge p2-ui, p2-writer, p2-reader, p2-parse into `windows-parity`; full suite must be 0 failures.
4. Phase 3 (quiet machine, no other agents): `tools/bench_chdwrite.py` on all 5 inputs, default and zstd presets, vs chdman; `chdman verify` every output; header SHA-1 equal to chdman's; build `packaging\build_windows.ps1` and `build_windows_exe.ps1` and run the self-check with `--require-native` inside each; final review of the whole diff; then ask the user before merging to `main`/pushing.
5. Delete `win-fixes`, `win-packaging`, the p2 worktrees and the `worktree-wf_*` branches once merged.

## Facts you need

- Interpreter `py -3.14` (`python` on PATH is 3.7). libFLAC 1.5.0 Win64 for tests: `packaging\.cache\native\libFLAC.dll` (set `ROMORG_LIBFLAC`). Fetched by `packaging\fetch_flac.ps1` (pinned SHA-256s).
- Scratch (session temp, may be cleaned): `%TEMP%\claude\c--Users-DominicBird-Repos-simple-rom-organiser\5cdc4aaa-8bb7-40db-8b12-6466135451dd\scratchpad\` holds `mame\chdman.exe` (0.289, from `mame0289b_x64.exe` on the mamedev GitHub release, extract with 7z), `sets\` (extracted test discs), `out_chdman\`, `profiler_report.md`, `audit_report.md`, `profile\`. If gone, re-extract with `chdman extractcd` from the originals below (DVD: use `thps4.iso` output of the CD extract, it is MODE1/2048).
- chdman fails (rc=1) with absolute paths (it joins `<sheet folder> + name`); run it with cwd set and relative paths.
- Originals (read-only): `F:\Emulation\roms\ps2\Tony Hawk's Pro Skater 4 (USA).chd`, `F:\Emulation\roms\psx\Spider - The Video Game (USA)\...chd`, `F:\Emulation\roms\dreamcast\Alienfront Online (USA)\...chd`, `F:\Emulation\roms\dreamcast\Dead or Alive 2 (USA)\...chd`.
- chdman 0.289 on this machine (16 threads, idle): spider 8.76 s, doa2 22.73, alienfront 21.34, thps4 as CD 85.41, thps4 as DVD 110.17. SHA-1s: spider e332ae9c..., doa2 16ba675c..., alienfront 24a58e6a..., thps4 CD 381bc185..., thps4 DVD 20ba764e....
- Phase-1 profiler (built-in, with taskkill + readline fixes, loaded machine): spider ~6.5 s, doa2 ~14, alienfront ~13.8, thps4 CD ~35, thps4 DVD ~28-36; zstd faster still; sizes within 0.1-0.8% of chdman. Reader via scheduler 4-8x faster than `chdman verify`.
- Open items not yet done anywhere: emulator load test of written CHDs; clicking the Convert dropdowns in a browser.

## Update 2026-10-06 (evening): phases 2 and 3 finished

All four p2 branches were reviewed, fixed and merged into `windows-parity` (p2-reader's `server.py` POST-body fix was
dropped in favour of p2-ui's equivalent). Merged suite: 1060 tests, OK, 46 skipped (real DATs / Linux-only / env-gated).

Measured on this machine (Windows 11, 16 threads, idle, libFLAC 1.5.0, 12 workers, median of 2; `tools/bench_chdwrite.py`).
chdman 0.289 numbers are from the earlier idle run. Every output has chdman's header SHA-1.

| Disc | chdman 0.289 | built-in default | built-in Zstandard | size vs chdman (default) | scheduler verify |
|---|---|---|---|---|---|
| Spider (PS1 CD) | 8.76 s | 5.0 s | 3.5 s | 1.0013 | 2.2 s |
| Dead or Alive 2 (GD) | 22.73 s | 13.7 s | 7.7 s | 1.0032 | 4.5 s |
| Alienfront (GD) | 21.34 s | 11.8 s | 7.6 s | 1.0063 | 3.6 s |
| THPS4 as CD | 85.41 s | 31.5 s | 23.5 s | 1.0059 | 7.9 s |
| THPS4 as DVD | 110.17 s | 27.9 s | 22.1 s | 1.0084 | 8.1 s |

`chdman verify` (0.289) reports raw and overall SHA-1 verified for all 8 built-in outputs (4 discs x default and zstd).
Both packages built (`dist\*-win64.zip` 14.3 MB, `.exe` 12.3 MB); `--require-native` self-check passes inside each
(libFLAC from the package, compression.zstd, worker processes, cdfl hunks).

Still open: not pushed, not merged to `main`; no emulator load test of written CHDs; Convert dropdowns not clicked in a
browser; RealDatTest (Sony DATs) and the other real-DAT tests never ran here (DATs absent); long paths from the frozen
exe untested (no longPathAware manifest); non-ASCII library CHD paths still reach chdman in `hash_all_chdman` /
`chdtool` extract+verify when chdman is the engine.
