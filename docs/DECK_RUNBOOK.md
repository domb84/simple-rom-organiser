# Runbook: verify the Windows-pass work on a real Steam Deck

For whoever (any account, any Claude session or a person) sits at the Deck next. Everything in this file can be done from a
fresh login. It is the "do this on the Deck" half of `docs/HANDOVER_DECK.md`; read that file's last section ("Update
2026-10-07") if you want the background. **Why this matters:** every check so far ran on Windows, in WSL Ubuntu and in Arch
Linux containers on a Windows host. None ran on SteamOS or on Deck hardware, and the Deck is the platform that matters most.

Rule for the whole job: the Deck build must not regress. If something that worked before stops working, that is a bug in the
Windows-pass changes. Fix it or report it; do not skip it.

## 0. Get the code (about 2 minutes)

The repository is public, so no login is needed to read it:

```
git clone https://github.com/domb84/simple-rom-organiser.git ~/simple-rom-organiser     # or: cd ~/simple-rom-organiser && git pull
cd ~/simple-rom-organiser
git log --oneline -3          # expect e840c23 (or newer) "Handover for the Deck: bug-check results..."
```

The Deck has had no working GitHub login for pushing. If you fix something, commit it, and either push with whatever
credentials you have or leave the commit in place and tell the user (`git format-patch -1` makes a file to carry over).

Python on the Deck: SteamOS ships a system `python3`; the dev checks below use it. The AppImage never uses it.

## 1. Quick checks with the system Python (about 5 minutes)

```
python3 --version                                    # 3.11 or newer is enough (write down what it is)
python3 -m unittest discover -s tests 2>&1 | tail   # expect: OK, with skips (real DATs, browser, Windows-only)
python3 -m romorg.selfcheck                          # expect: SELF-CHECK PASSED
```

Expected: about 1131 tests, `OK`, 50-70 skipped. Zero failures and zero errors is the bar. The one line
`OSError: disk full` printed mid-run is a test's expected log output, not a failure. Anything failing here is a regression
from this pass: note the test name and the traceback, and fix at the root cause.

The self-check prints OK lines for: Zstandard (`libzstd.so.1` when the system has it, else the pure-Python decoder), libFLAC
(the system's), the scheduler with 2 worker processes, the writer (`processes, cdlz/cdzl/cdfl`) and the RVZ reader.
`WARN libFLAC not available` is fine on a Deck without libFLAC: the AppImage bundles its own.

## 2. Build the AppImage on the Deck (about 5 minutes, needs network)

```
packaging/build_appimage.sh          # needs bash curl tar sha256sum and tar with zstd support (all present on SteamOS)
ls -la dist/Simple_ROM_Organiser-*-x86_64.AppImage     # about 19-20 MB
```

The build downloads a relocatable CPython 3.13 plus pinned Arch Linux packages for libFLAC, libogg and libzstd, verifies
every SHA-256 (it fails closed on a mismatch) and runs its own self-check at the end. For reference, the AppImage built in
the Arch container from commit 3eb8874 was 20,388,344 bytes; a rebuild on the Deck will not be byte-identical because the
bundled Python tarball is the "latest" python-build-standalone release.

If a step fails, the log line says which: a download (network), a hash (the pinned package changed on the server: report it,
do not edit the pin without checking), or the self-check (a real defect).

## 3. The AppImage itself, the way a user runs it (about 5 minutes)

```
packaging/smoke_test.sh dist/Simple_ROM_Organiser-*-x86_64.AppImage         # expect: SMOKE TEST PASSED
dist/Simple_ROM_Organiser-*-x86_64.AppImage --self-check                    # expect: SELF-CHECK PASSED
```

In the self-check output you must see these two lines, with a path inside the mounted AppImage:

```
OK    libFLAC /tmp/.mount_Simple.../tools/lib/libFLAC.so.14 is loaded from the bundle
OK    libzstd /tmp/.mount_Simple.../tools/lib/libzstd.so.1 is loaded from the bundle
```

That proves it does not rely on the system's libraries. If either says it came from the system, or the self-check prints
`FAIL bundle incomplete`, the build or the bundle lookup is broken: stop and fix. (`EXTRACT_AND_RUN=1 packaging/smoke_test.sh`
runs it without FUSE; the Deck has FUSE, so use the normal way first: that is a point the Docker runs could not prove.)

Install it as a user would and start it from Desktop Mode:

```
packaging/install.sh                 # puts ~/Applications/Simple_ROM_Organiser.AppImage + an app-menu entry in $HOME only
```

Launch "Simple ROM Organiser" from the application menu (and, if the user adds it, from Steam). The browser must open the UI.
Log file when launched without a terminal: `~/.local/state/simple-rom-organiser/app.log`.

## 4. Real use on the Deck (about 30-60 minutes; needs the user's ROM folders)

Use the UI (or ask the user which folders). For each, say what happened:

1. **Scan a PlayStation / Dreamcast folder that holds CHDs** (read-only). Expect "identified"; "Verify fully" must end
   "verified". The user's four reference discs (Spider PS1, Tony Hawk's Pro Skater 4 PS2, Alienfront Online and Dead or Alive 2
   for Dreamcast) are the known-good set.
2. **Convert a CD set and a DVD** (a `.cue`/`.bin`, a `.gdi` set, a PS2 `.iso`) with the built-in writer, standard preset.
   Expect "written by built-in", then a verified result, and the originals moved to `_converted_originals` and restorable with
   Undo. Do it once with the Zstandard preset (`Convert` step, warning about emulators from 2024 or later) and check the file
   loads in the Deck's emulators.
3. **GameCube**: point a GameCube folder with `.rvz` files at the app (platform "Nintendo GameCube", Redump DAT). Expect
   "rvz" chips and correct Redump names. The user's `Super Mario Sunshine (USA, Canada).rvz` rebuilds to SHA-1
   `8d094f2c5c112aba9660f0478b16c7f2caf63cbf`, size 1,459,978,240.
4. **The eight new cartridge systems** (Game Boy, Game Boy Color, Nintendo DS, Mega Drive/Genesis, Master System, Game Gear,
   32X, Atari Lynx): scan one folder each; matches should be plausible. Last Deck numbers (before this pass): Mega Drive 646,
   Master System 249, 32X 31, Lynx 68 files matched; the unmatched ones are hacks, translations, homebrew and BIOS files.
5. Watch for: Task Manager / `ps` showing leftover `romorg` worker processes after a cancel or after a job ends (there must be
   none), the UI wording being the Deck's (the "Discover (Desktop Mode)" hint appears only when chdman is chosen and missing).

### Saves (Amendment 30, added 2026-10-08)

The library rules have a new setting, "Games you have saves for" (keep = default / archive their saves with them / leave the
saves), and a build shows how many saves it will rename, keep or archive. On the Deck, with RetroArch installed and its
`saves` folder holding a few `.srm` / `.state` files: (a) in Library > Rules the setting is there; with "Keep them" a game
that has a save is not archived although the rules would (the preview says "kept: you have saves"); (b) rename a ROM through
a build and check the save in `saves/<core>/` got the new name (Flycast `.A1.bin` memory cards too); (c) "Archive their saves
with them" puts them under `<archive>/<system>/_saves/<core>/` and Undo brings ROM and saves back; (d) nothing is ever moved
next to a ROM. A loose `.srm` beside a ROM now goes to `_other` in the Collection sort.

## 5. Speed (about 20 minutes; compare with the old numbers)

```
python3 tools/bench_chdwrite.py <set.cue|.gdi|.iso>... --preset default,zstd --repeat 2 --verify
python3 tools/bench_chd.py disc "<a real .chd>" --strategies sched
```

Old Deck numbers (8 threads, from `docs/HANDOVER_WINDOWS.md` section 4, built-in writer, standard preset): PlayStation CD
429 MB 15.8 s, Dreamcast GD-ROM 1.2 GB 44.4 s, PS2 ISO 1.5 GB as DVD 39.7 s; GameCube Sunshine hash 3.4 s with 4 workers. The
windows pass made the writer faster in `_build_map`, the worker pool and its shutdown, so equal or better is expected. If a
number is clearly worse, find out why before accepting it.

## 6. What to write down (add it to `docs/HANDOVER_DECK.md` under a new "Deck results" heading and commit)

- the date, SteamOS version (`cat /etc/os-release`), glibc (`ldd --version | head -1`), kernel (`uname -r`)
- the commit you tested (`git log --oneline -1`) and, for the AppImage, its size and `sha256sum`
- step 1: the unittest summary line and the self-check result; step 3: both smoke-test lines and the two "loaded from the
  bundle" lines; step 4: one line per item above; step 5: the timings next to the old ones
- every failure: the exact command, the output, what you changed, and whether the same check passes afterwards
- anything you could not test

## 7. Known and accepted (do not "fix")

- Windows-only: long paths over 260 characters, and chdman with non-ASCII folders. Not our problem.
- The "leave these folders alone" setting is not wanted yet; do not build it.
- 12 No-Intro DATs are downloaded the first time (about 15 MB); the TOSEC pack is large.
- Without libFLAC a CHD's audio is stored with LZMA (valid, larger). The AppImage always has libFLAC; this only matters for
  the dev checks in step 1.

## 8. If something is badly broken

The pre-Windows-pass state is commit `ad26484`; the state with the Windows pass but before the bug check is `247cac5`. To
see whether a problem is new: `git stash; git checkout <commit>`, rebuild or rerun, compare. Use `git bisect` between
`ad26484` and `main` (63 commits) if you need the commit that introduced it. Tell the user before reverting anything.
