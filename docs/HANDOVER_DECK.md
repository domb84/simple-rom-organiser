# Handover: picking the Windows work up on the Steam Deck (or any Linux machine)

Written 2026-10-06 on Windows. Everything below is on `origin/main` (`git pull`). Read this file first, then
`docs/HANDOVER_WINDOWS.md` (what the Linux side built) and `docs/HANDOVER_WINDOWS_2.md` (what the Windows pass did and
measured). Nothing here needs the earlier conversation.

## What happened

The built-in CHD reader/writer built on the Deck was brought to Windows: 40 commits since `ad26484`. Windows now
reads and writes CHDs without chdman, faster than chdman 0.289 on every disc tried, with chdman's exact SHA-1s,
`chdman verify` clean, and the output loads in PCSX2 (standard + Zstandard DVD), Flycast and PCSX ReARMed. Windows
packages ship Python 3.14 and libFLAC 1.5.0. Windows suite: 1068 tests OK, 46 skipped.

**None of it was run on Linux.** Changes were made to code both platforms share, kept POSIX paths intact by reading, and
the reviewers reasoned about Linux too, but nobody executed it. That is the job on the Deck.

## Decisions already made (do not reopen)

- Long paths (>260) on Windows and chdman with non-ASCII paths on Windows: not our problem; left as they are.
- chdman stays optional and unbundled; the built-in engine is the default everywhere.
- Zstandard preset off by default, with its warning. libFLAC DLL ships on Windows; Python 3.14 for the Windows builds
  (the AppImage stays on 3.13, which has no `compression.zstd`: the system/bundled libzstd route applies there).
- Only createcd (cue/gdi/iso) and createdvd (iso) are written; no hard disks, parents or laserdiscs.

## What to do on the Deck, in order

1. `git pull`, then `python -m unittest discover -s tests` from the repo root. Expect everything to pass; skips are
   real-data and Windows-only tests. Anything that fails is a Linux regression from this pass: fix it, do not skip it.
2. `python -m romorg.selfcheck` (and `--require-native` inside the AppImage, as `packaging/build_appimage.sh` does).
   The success line for the writer still starts "OK    the writer " (the AppImage build greps for it).
3. Rebuild the AppImage (`packaging/build_appimage.sh`) and the pyz (`packaging/build_pyz.sh`) and run their self-checks.
   `build_pyz.sh` now insists on Python >= 3.11 when it picks an interpreter automatically (set `PYTHON` to override).
4. Re-run the Deck benchmarks to confirm nothing got slower: `tools/bench_chd.py` (reading) and the new
   `tools/bench_chdwrite.py` (writing; see its `--help`; `--chdman PATH` times chdman on the same inputs). Previous Deck
   numbers (8 threads) are in `HANDOVER_WINDOWS.md` section 4: PS1 CD 15.8 s, Dreamcast 44.4 s, PS2 DVD 39.7 s (standard).
5. Optional real-data run: `python tools/fetch_real_dats.py <folder>` downloads the DATs with the app's own downloaders and
   lays them out for the real-data tests; set `ROMORG_REAL_SCRATCH=<folder>` (other `ROMORG_REAL_*` variables are
   documented in comments above the tests in `tests/test_chd.py`, `test_chdwrite.py`, `test_playstation.py`, `test_datfile.py`
   etc.). With chdman at hand, `ROMORG_CHDMAN_ORACLE=<path>` enables the tests that compare against it.

## Code both platforms share that changed: review these on Linux

| File | Change | Linux risk to check |
|---|---|---|
| `romorg/chdsched.py` | Worker pool: buffered reader on worker pipes, `write_all` loop for short writes, `Scheduler.release(path)` closes a file in every worker, a broken pool kills its workers, `restart_workers`, `_requeue`, worker start-up trimmed, `taskkill` removed (Windows uses `TerminateProcess`; Linux still `killpg`). `WINDOWS_MAX_AUTO = 12` is Windows-only. | The `fcntl F_SETPIPE_SZ` call must still run before the pipe is wrapped in a `BufferedReader`; `killpg` path must still reap workers; no busy-spin when the pool is broken. |
| `romorg/chdwrite.py` | `_build_map` rewritten with C loops (about 1.7x faster, byte-identical in 1548 random cases); pool reads replies through a buffer; `info["engine"]` says `threads` and `info["fallbacks"]` counts if a slice fell back to the parent; writer honours the `chd_workers` setting; closes the image reader explicitly. | Output must stay byte-identical to chdman: `tests/test_chdwrite.py` has the reference SHA-1s. |
| `romorg/chd.py`, `chdhuff.py`, `flacdec.py`, `zstddec.py` | Huffman decoder reads codes that end exactly on the last bit (bits past the end are zeros; over-read is still an error, as in MAME); one shared 32-thread decode pool fixes a shutdown race. | `tests/test_chd_formats.py` has the two real THPS4 hunks as fixtures. |
| `romorg/cdimage.py`, `discsys.py` | cue/gdi parsing now follows chdman 0.289 exactly (case-sensitive keywords, quoting, apostrophes, double spaces, CRLF, BOM); `_gdi_track_ref` for chdman-as-writer tries symlink, then hard link, then a relative path, then copy; the built-in writer takes sets chdman could not open (Windows-only checks gated on `os.name == "nt"`). | Symlink-first must still be the first choice on Linux; generated GDI names with spaces/apostrophes; exFAT scratch (SD card) now falls to the relative-path branch. |
| `romorg/chdtool.py`, `winproc.py`, `tempspace.py`, `server.py` | `bundled_chdman()` returns None off Linux; `winproc.pid_alive` replaces `os.kill(pid, 0)` on Windows (POSIX unchanged); `chdman_input` short-path logic is Windows-only; `_send` swallows `ConnectionError`; refused POST bodies are read before replying. | AppImage's bundled-chdman lookup (now unused since chdman is no longer shipped there). |
| `romorg/selfcheck.py` | `--require-native` (libFLAC loaded, library zstd, worker processes, `cdfl` hunks); writer check now fails if the engine is not `processes`. | AppImage self-check output format. |
| `romorg/static/app.js`, `index.html`, `server.py` `/api/chdman` | Platform-neutral wording, `flac_encoder` status shown in the Convert step, per-platform chdman hint. | Deck wording must still appear on Linux. |
| `tests/` | The "POSIX only" gate on the worker pool tests is gone; `find_bash` prefers Git for Windows' bash on Windows; real-data tests read `ROMORG_REAL_*` variables with the Deck defaults kept. | Deck default paths (`/tmp/claude-1000/...`, `/home/deck/MEGA/...`) must still be used when the variables are unset. |

Known Linux-only fast path untouched: `nativeflac.decode_frames_fd` (needs `os.memfd_create`). Windows decodes FLAC through libFLAC callbacks in worker processes (the fastest option there; measured).

## Reference numbers (Windows, 16 threads, idle, libFLAC, 12 workers; chdman 0.289 for comparison)

| Disc | chdman | built-in | built-in Zstandard | size vs chdman |
|---|---|---|---|---|
| Spider (PS1 CD) | 8.76 s | 5.0 s | 3.5 s | +0.13% |
| Dead or Alive 2 (GD) | 22.73 s | 13.7 s | 7.7 s | +0.32% |
| Alienfront (GD) | 21.34 s | 11.8 s | 7.6 s | +0.63% |
| THPS4 as CD | 85.41 s | 31.5 s | 23.5 s | +0.59% |
| THPS4 as DVD | 110.17 s | 27.9 s | 22.1 s | +0.84% |

Reading through the scheduler beat `chdman verify` by 4-8x (e.g. THPS4 DVD 8 s vs 56 s). A built-in CHD without libFLAC
stores audio with LZMA (valid, larger); Windows now ships libFLAC so this only applies to a Linux box without libFLAC.

## Windows-only things, in case a Windows machine comes back

- Build: `packaging\build_windows.ps1` (zip) and `packaging\build_windows_exe.ps1` (exe) need the `py` launcher with
  Python 3.14 (plain `python` is often 3.7 or the Store stub). Both build scripts run the self-check inside the package
  and fail unless libFLAC loads from the package. The packages (14.3 MB zip, 12.3 MB exe) are built into `dist\`
  (git-ignored). libFLAC comes from Xiph's flac-1.5.0-win.zip, SHA-256 pinned in `packaging\fetch_flac.ps1`; the DLL
  statically links libogg and mingw-w64 winpthreads, whose licences ship in `licenses\`.
- Run the suite from Git Bash or PowerShell; both pass. The WSL `bash.exe` in System32 breaks shell-script tests, which
  `tests/chdtestlib.find_bash` now avoids.
- chdman 0.289 oracle: `mame0289b_x64.exe` from https://github.com/mamedev/mame/releases/tag/mame0289 (a 7-Zip
  self-extractor; `7z e ... chdman.exe`). It rejects absolute `-i` paths when the `.gdi`/`.cue` sits elsewhere (it joins
  the sheet's folder and the track name): run it with the working directory set and relative paths.
- Emulator checks done by the user (2026-10-06): THPS4 (standard and Zstandard DVD) in PCSX2, Dreamcast discs in Flycast,
  Spider in PCSX ReARMed. All load and play.

## Not done anywhere

- Linux/Deck run of any of this (step 1-4 above).
- DAT-o-MATIC GBA test (`test_dat_o_matic_matches_libretro`) stays skipped: the app has no downloader for that DAT.
- Convert for hard disks, parents and laserdiscs (out of scope by decision).
