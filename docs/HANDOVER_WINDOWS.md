# Handover: the Windows pass over the CHD engine

Written 2026-10-05 on a Steam Deck (SteamOS, Python 3.13) for whoever picks this up on a Windows machine. Everything
below was built and measured on Linux only. **Nothing in this list has been run on Windows.** Your job is to run it
there, fix what breaks, and decide the open questions at the end.

Before anything else: check that `git log` on your side contains the commit "Built-in CHD writer" and this file. The
Linux machine had no working GitHub login, so the commits may still need pushing from there (`git push origin main`).
If they are missing, stop and ask the user to push.

## 1. What changed since the last Windows work

The last commit made on Windows is `4d939a7` (libsndfile in the zip). It was merged into the Linux line in `1848bcf`.
Since then, three things were added:

| Commit | What | Read |
|---|---|---|
| `a8c5366` | **Reader parity.** The built-in CHD reader reads everything chdman 0.289 reads: Zstandard, Huffman, FLAC-data and laserdisc hunks, parent files, CHD versions 1 to 4. | `docs/ARCHITECTURE.md` Amendments 20 and 21 |
| "Built-in CHD writer" | **Writer.** The app converts cue / gdi / iso sets to CHD itself; chdman is optional and no longer shipped in the AppImage. | `docs/ARCHITECTURE.md` Amendment 22, `docs/CHD_WRITER_PLAN.md` |
| (same) | **Settings and UI.** `chd_writer` (`auto` / `chdman`), `chd_preset` (`default` / `zstd`), two new dropdowns in the Convert step. | `romorg/server.py` (`chdman_save`, `_writer_facts`), `romorg/static/app.js` (`renderChdman`) |

New modules: `romorg/chdhuff.py`, `romorg/zstdnative.py`, `romorg/zstddec.py`, `romorg/cdimage.py`,
`romorg/chdwrite.py`, `romorg/flacenc.py`. Changed for this work: `chd.py`, `chdworker.py`, `chdsched.py`,
`cdecc.py`, `flacnative.py`, `nativeflac.py`, `discsys.py`, `selfcheck.py`.

The merge kept the Linux CHD engine (worker processes in `chdsched.py`) and dropped the Windows line's `chdpool.py`
and its decode threads inside `chd.py`. The Windows adaptations of the scheduler (`winproc`, `--chd-worker`,
`GlobalMemoryStatusEx`, `WINDOWS_MAX_AUTO`) were ported by reading the code, not by running it.

## 2. First steps

1. `python -m unittest discover -s tests` from the repository root. On Linux: 983 tests, 30 skipped, all pass.
   Expect failures on Windows; list them before fixing anything. The fake chdman used by the tests is a Python
   script (`tests/chdtestlib.py`, `install_script`), made portable during the merge but never run on Windows.
2. `python -m romorg.selfcheck`. It reports which FLAC and Zstandard libraries load, runs the scheduler with two
   worker processes, and has the writer make a small CHD that the reader reads back.
3. Build both packages (`packaging\build_windows.ps1`, `packaging\build_windows_exe.ps1`) and run the self-check
   from inside each. The frozen exe starts workers as `<exe> --chd-worker` (`packaging/windows_entry.py`).

## 3. What is most likely to break, in order

1. **Worker processes for writing.** `chdwrite._Pool` starts workers with `chdsched.spawn_worker()` and talks to
   them over unbuffered pipes: a JSON line, then raw bytes, in both directions. On Windows check binary mode on both
   ends (`windows_entry.py` sets it for the frozen exe; `python -m romorg.chdworker` relies on `sys.stdin.buffer`),
   that `proc.stdout.readline()` on an unbuffered pipe behaves, and that no console window flashes. If a worker
   fails, the pool silently compresses in the parent (`info["engine"]` says `threads`), so a broken pool shows up
   as slowness, not as an error: assert on `engine == "processes"` when you test.
2. **No FLAC encoder.** `flacenc.py` needs a real libFLAC (`FLAC__stream_encoder_*`). The Windows packages ship only
   libsndfile, which cannot do it. Without libFLAC the writer stores audio tracks with LZMA: valid, and the CHD's
   SHA-1 is still chdman's, but the file is larger than chdman's. There is deliberately no other encoder: MAME's
   decoder rejects FLAC frames whose block size is not exactly what its own encoder writes (proven with chdman
   0.289). Decide with the user whether to ship a libFLAC DLL (BSD-3-Clause) next to libsndfile.
   `flacnative._candidates()` already looks for `libFLAC.dll`, `FLAC.dll`, `libFLAC-14.dll` next to the app and in
   `native\`.
3. **No Zstandard library.** The builds use Python 3.13 and bundle no libzstd. Reading zstd CHDs then uses the
   pure-Python decoder (`zstddec.py`, about 1 MB/s per worker; correct, slow). Writing with the Zstandard preset
   needs a library; the UI disables the option without one (`zstd_writer` in `/api/chdman`). Two ways out: build
   with Python 3.14 (`compression.zstd` is tried first), or ship `libzstd.dll`.
4. **FLAC decoding speed.** The fast path that scales over threads (`nativeflac.decode_frames_fd`) needs Linux
   memfds. On Windows decoding goes through libFLAC callbacks or libsndfile's virtual I/O, as before the merge.
   Worker processes still give the parallelism for scans; only single-process reads are affected.
5. **Decode threads in the reader.** `chd.Chd.threads` (default up to 4) is new. Workers set it to 1. Harmless if it
   works; if anything hangs on Windows, set `ROMORG_CHD_THREADS=1` to rule it out.
6. **Paths.** `cdimage.py` resolves track files next to the sheet with `pathlib`; long paths (over 260 characters)
   need the Windows setting, as elsewhere in the app.

## 4. How to check the writer on Windows

The oracle is chdman, exactly as on Linux:

- `tests/test_chdwrite.py` compares the writer with what chdman 0.289 wrote for 16 generated layouts
  (`tests/fixtures/chdwrite/chdman_reference.json`). It needs no chdman and must pass unchanged on Windows: the
  SHA-1s do not depend on the platform.
- With a `chdman.exe` at hand, convert one real set with the app and run `chdman verify -i new.chd`; it must report
  both the raw and the overall SHA-1 as verified. Then `chdman createcd` the same set and compare the header SHA-1
  (`chdman info`): they must be equal.
- Speed reference from the Deck (8 threads), to compare with a Windows machine and with chdman there:

| Input | chdman 0.289 | built-in, standard | built-in, Zstandard |
|---|---|---|---|
| PlayStation CD, 429 MB | 15.8 s | 15.8 s | 4.2 s |
| Dreamcast GD-ROM, 1.2 GB | 50.7 s | 44.4 s | 13.3 s |
| PlayStation 2 ISO, 1.5 GB, as DVD | 91.2 s | 39.7 s | 24.2 s |

  There is no benchmark script in the repository for the writer yet; `tools/bench_chd.py` covers reading only. A
  `tools/bench_chdwrite.py` would be a useful first addition.

## 5. Things that are true on every platform and still unchecked

- No written CHD was loaded in an emulator. The evidence is SHA-1 equality with chdman, `chdman verify`, and the
  reader. If you have DuckStation, PCSX2 or Flycast on Windows, load one standard and one Zstandard CHD in each.
- The two new dropdowns in the Convert step were not clicked through in a browser; the API behind them is tested
  (`tests/test_dc_server.py`).
- CHD versions 1 to 4 are read from the format description and tested on synthetic files only: no current chdman
  writes them.

## 6. Decisions the user already made (do not reopen without asking)

- Only what the app converts is in scope for the writer: `createcd` from cue / gdi / iso, `createdvd` from iso. No
  hard disks, parents, laserdiscs.
- chdman is not packaged. An installed one stays usable as an option, and the read fallback for unknown codecs
  stays.
- The Zstandard preset is offered, off by default, with a warning that it needs an emulator from 2024 or later.

## 7. Open questions for the user

1. Ship a libFLAC DLL in the Windows zip (and exe)? Without it, converted discs with audio tracks are larger than
   chdman's.
2. Move the Windows build to Python 3.14, or ship `libzstd.dll`, so that zstd CHDs are fast to read and the
   Zstandard preset is available?
3. Is the worker count on Windows (`WINDOWS_MAX_AUTO = 12`, CPU threads minus one) right for writing too? It was
   set for reading and never measured.

## 8. Working notes

- The project is standard-library Python only; C libraries are loaded with ctypes and are always optional. Keep it
  that way.
- Commit on `main`; the user asks for commits explicitly. Report test results as they are.
- When a change touches both platforms, say which one it was run on. This file exists because that was not
  possible from here.
