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

## Update 2026-10-07: full bug check, libzstd bundled, AppImage re-verified in a Deck-like container

**Read this first: the Deck is still the final check.** Everything below ran on Windows, in WSL (Ubuntu 24.04, Python 3.12) and
in Docker Arch Linux containers on a Windows host. None of it ran on SteamOS or on Deck hardware.

### What changed since the sections above

- **The AppImage now bundles libzstd** (pinned `zstd-1.5.7-3` from the Arch Linux Archive, SHA-256 checked before use,
  BSD-3-Clause text in `licenses/zstd/`). Before, Zstandard CHDs, the Zstandard writer preset and GameCube RVZ discs used the
  system's libzstd or, without one, a pure-Python decoder at about 1 MB/s. Inside an AppImage `python -m romorg.selfcheck` (the
  `--self-check` of the AppImage) now FAILS unless libFLAC and libzstd were loaded from `<AppDir>/tools/lib`; normal start-up
  is unaffected. The AppImage is about 19.4 MiB (was about 18).
- **Bug check (seven independent lenses, every finding re-verified by a skeptic on Windows and in real Linux, every fix
  reviewed for Linux neutrality).** Fixed, each with a regression test: a valid FLAC frame that is not 2-channel 16-bit crashed
  the process (SIGSEGV) in the libFLAC binding on Linux; damaged CHDs and crafted RVZs could cost 20+ s and gigabytes before an
  error, and non-`ChdError` exceptions from a damaged CHD aborted a whole scan; a `.rvz` hashed as a plain file poisoned the cache
  of the later GameCube scan; a failed move of a later original during Convert left the new CHD behind; `chd_workers = Infinity`
  in `config.json` failed every job; several workers dying at once broke the pool early; the Windows generated-GDI scratch
  folder could leak; `ratings.Store` did not reload a replaced index within one clock tick (flaky test on tmpfs); the shipped
  `.pyc` files embedded the builder's path; `tools/fetch_real_dats.py --help` created a folder and downloaded 120 MB; stale
  README / CHD_WRITER_PLAN text; the tag parser's `Rumble` hardware pattern also matched a TOSEC author. See `git log` for the
  eight `bc-*` / `deck-appimage` branches merged on 2026-10-07.
- `.gitattributes` keeps `*.sh`, `AppRun` and the `.desktop` file LF on every checkout (a Windows checkout otherwise turns
  `build_appimage.sh` into CRLF and `set -o pipefail` fails).

### What was proven, and where

| Check | Where | Result |
|---|---|---|
| Full suite, 1131 tests | Windows, Python 3.14 | OK (48 skipped) |
| Full suite, 1131 tests | WSL Ubuntu 24.04, Python 3.12, libFLAC + libsndfile + libzstd installed | OK (60 skipped) |
| Full suite, 1131 tests, AppImage's own Python 3.13.16, bundled libs only | Arch container, uid 1000, `--read-only` root, 8 CPUs, no network, no system libzstd / libFLAC / libsndfile / libogg | OK (59 skipped, all environment-bound) |
| `--self-check`, `smoke_test.sh`, server end to end (scan, convert of two ISOs via the HTTP API, quit) | the same container, AppImage mounted through real FUSE | passed; libFLAC and libzstd both loaded from the bundle |
| Four real discs written (default and Zstandard preset) | the same container | header SHA-1 equal to chdman 0.289's; built-in reader verifies raw + overall |
| Super Mario Sunshine RVZ | the same container | SHA-1 `8d094f2c5c112aba9660f0478b16c7f2caf63cbf`; 2.6 s with workers (bundled libzstd), 6.1 s with one worker |
| The old SIGSEGV | the same container | the pre-fix code dies with rc -11, the current code raises `FlacError`/`ChdError` for mono, 8/12/20/24-bit, 3- and 8-channel frames |
| Negative controls | extracted AppDir with the bundled libzstd removed or unloadable | the self-check FAILS (so the "from the bundle" check is real) |

AppImage built from merge commit 3eb8874 + `.gitattributes`: 20,388,344 bytes, sha256
`4aa61d1cc2ed33740ca3301874ab2e3db2ad57835383477b07cf8018a6043595` (clean cache, every pinned SHA-256 verified).

### What was NOT proven (do these on the Deck)

1. Run the AppImage on the Deck: `--self-check`, then use the app (scan, Convert a CD and a DVD, a GameCube folder).
2. Real SteamOS glibc (about 2.41; the container had 2.44), the SteamOS kernel, its FUSE setup, Desktop Mode / Steam launch.
   The bundled libFLAC and libzstd need at most GLIBC_2.34 by a symbol check; the bundled Python is a python-build-standalone
   build that was not run against glibc 2.41.
3. Real hardware timings (Zen 2, 8 threads, SD-card exFAT scratch). The container numbers are not comparable.
4. Network paths (DAT downloads, HTTPS with the system CA bundle through AppRun's `SSL_CERT_FILE` logic): the containers had no network.
5. The `PY_SERIES=3.12` / `3.14` AppImage variants.
6. Re-run `tools/bench_chd.py` / `tools/bench_chdwrite.py` on the Deck against the numbers in `HANDOVER_WINDOWS.md` section 4.

### Method, in case it must be repeated

- WSL Ubuntu is a convenience, not the target. For Deck fidelity use `docker run archlinux:latest` with `--read-only`,
  `--tmpfs /tmp`, `--cpus 8`, `--network none`, user uid 1000, `/dev/fuse` + `SYS_ADMIN` for a real FUSE mount, and delete
  libzstd / libFLAC / libsndfile / libogg from the image's `/usr/lib` so only the bundle can satisfy them.
- Build the AppImage inside an Arch container from a `git archive` (or a checkout with LF endings: see `.gitattributes`).
- Run the whole unit suite with the AppImage's own Python: `PYTHONPATH=<source> ROMORG_BUNDLE_DIR=<extracted AppDir>
  PYTHON=<AppImage python> <AppDir python> -m unittest discover -s tests` (the pyz test needs a python on PATH, hence `PYTHON`).
