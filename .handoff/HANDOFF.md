# Handover: v0.3, Amiga WHDLoad work (parked 2026-10-18)

The user parked the WHDLoad work at this point. **Don't resume it unless they ask.** Start by asking what they want next. The other v0.3 items haven't been started: the Steam Deck gamepad UI, and faster zip/7z checksumming and CHD conversion.

## Standing rules (from the user and the memory files)
- Commit only when the user asks. This machine can't push. Never touch git config or remotes.
- The code is stdlib-only Python with a vanilla JS UI. `index.html` must contain no literal `http://` or `https://`. Tests must not hit the network.
- After a change:
  1. Run the full suite: `python3 -m unittest discover -s tests`. It takes about 5 minutes; 1,632 tests passed (51 skipped) on 2026-10-18.
  2. Rebuild and smoke test: `bash packaging/build_pyz.sh && bash packaging/build_appimage.sh && EXTRACT_AND_RUN=1 bash packaging/smoke_test.sh`
  3. Clean up `/tmp/appimage_extracted_*` and `~/.cache/rtest`.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- **Never download Retroplay pre-installed WHDLoad packs.** They contain game data. Install packages from whdload.de are fine.
- The user's real data dir is `~/.local/share/simple-rom-organiser`. The one under `~/.var/app/com.visualstudio.code/...` is a test leftover with no Amiga folder.
- The memory files in `~/.claude/projects/-home-deck-Dev-simple-rom-organiser/memory/` hold the user's preferences. Read `v0-3-plan.md` first.

## Git state
- Branch `v0.3`. The last commit is `59d1ee5`: the WHDLoad installs list, matching to the user's games, downloads next to the disks, and the WHDLoad system removed.
- **Uncommitted spike work.** It's tested, but the user hasn't asked for a commit.
  - New modules:
    - `romorg/adf.py`: reads AmigaDOS OFS/FFS disks, and guards against directory loops.
    - `romorg/amigainstall.py`: an Amiga Installer script interpreter that runs a WHDLoad install on ADFs in memory and writes the `.lha`.
      - The install's hard disk is the volume `RomorgHD:`. A floppy labelled "Work" is that floppy, not the hard disk (Ninja Mission has one).
      - Copying a folder into itself raises an error.
    - `romorg/whdready.py`: stores which disk sets built, in `data/whdinstalls/ready.json`.
  - `romorg/whdinstalls.py`:
    - `package()` caches install packages.
    - Matcher: titles with " / " match on either name. A "short title" rule matches the install's title when it's the start of the game's title and the rest begins with meets/in/and/versus/vs. It needs at least two words and skips " & " compilations.
  - `library.py`: rule `prefer_whdload` (on by default, Amiga only). Among a game's versions, it prefers one recorded as ready.
  - `server.py`: `POST /api/whdload/check`, the `whdcheck` job.
  - `app.js`: the "Check which versions WHDLoad can install" button.
  - Tests: `tests/test_amigainstall.py` (14 tests); additions to `tests/test_whdinstalls.py`.
  - `docs/ARCHITECTURE.md` amendment 42; README.
- Not built: writing the built `.lha` next to the disks for real, scanner awareness of those `.lha` files, and a build UI.

## What we learned
- **Coverage, top 250 English Amiga games:**
  - 104 build: 87 copy real game files, 17 are whole-disk-image (DIC) installs.
  - 130 don't build: about 98 need original-format disks (RawDIC, or non-AmigaDOS cracks); 16 need xfdDecrunch; the rest are one-offs.
  - 14 have no install match; most are name mismatches (Lotus Turbo Challenge 2 = "Lotus 2", "Oh No! More Lemmings", SWOS editions, Super Off Road, Populous II, Gloom Deluxe).
  - 2 aren't owned.
- A re-check with every related install and every complete set, `_superseded` and `_excluded` included, found **no new real wins**.
- **The check's success test is too loose.** It counts "the script ran and left a `.slave`" as success. Known false positives: DIC images of cracked or CD32-rip disks, demo installs on demo disks, data disks. Three planned fixes, not built:
  1. Fail any result that copied nothing from the disks.
  2. Mark DIC results "unverified".
  3. Pair demo and data-disk sets only with demo installs.
- **Why TOSEC fits badly:**
  - Of 2,712 installs, 1,567 are RawDIC, which needs the original track format. ADFs can't store it; originals are preserved as SPS IPF files.
  - Slaves check loaded data with CRC16 (801 of the 1,633 packages that ship source) and abort with "wrong version". Cracks that change the main program fail there.
  - Matching file CRCs against the slave binary without running it was tried: it's noise.
  - The real check needs a 68000 emulator, or a PUAE test by the user.
- **Cannon Fodder:**
  - Only the CD32 rip builds, with `CannonFodderCD.lha`, after unpacking the rip's `cannon1.lha`/`cannon2.lha` (`f/` → `Fodder/`) into a `MemDisk("Cannon_Fodder")`. That was done by hand; the check can't do it.
  - No floppy copy builds.
- **Seven test builds** are in `~/MEGA/Emulation/roms/amiga-whdload-test/`: CannonFodder2, CannonFodderCD32, MonkeyIsland2, HistoryLine, TheClue, Moonstone, Gobliins2. The user hasn't reported whether they start in PUAE; ask.
- **Alternatives discussed** (the user parked the decision):
  1. Plain ADF + `.m3u` in PUAE with turbo floppy speed: the recommended default.
  2. Official hard-disk installers run by our interpreter: most promising; not measured.
  3. Cracker `[HD]` versions.
  4. Per-game core overrides (machine model, Kickstart), the way FS-UAE's database works.

## Official hard-disk installers (measured after parking, 2026-10-18)
The user asked: of the 144 top-250 games where WHDLoad fails or has no install, how many have an installer we can run? Script: `~/.cache/whdwork/hdinst.py`; results in `hdinst.json`.
- **Grouping gotchas:**
  - In TOSEC, only disk 1 carries the crack's tags, and disks 2..n are shared untagged files. Group sets with `library.variant_sets`, never by file name.
  - `variant_sets` only returns versions the library rules keep. For "any copy you own", pass `exclude=frozenset()`: cracker HD versions (`[m HD]`, `[h HD]`) are excluded by default, and they're where the installers live.
- **Results:**
  - 1 runs today: Roadkill, 126 files / 2.4 MB.
  - 4 Installer-language scripts fail on interpreter gaps:
    - The Clue!: copies from `SYS:` (`sys:system/format`), and its disk is named "TheClouDiskN".
    - Worms: writes to `LIBS:`.
    - Alien Breed and The Lion King: copy a whole `DFn:` disk.
  - 21 have AmigaDOS shell-script installers, which we don't interpret: Super Stardust, Turrican II, Indiana Jones 4, Simon the Sorcerer, Flashback, Syndicate, Chaos Engine, Superfrog, Star Trek, Falcon, It Came from the Desert, Caesar, Transarctica and others.
  - 18 only have compiled install programs, which would need a 68000 emulator: Ultima V, Dune II, both Eye of the Beholders, Black Crypt, Red Baron, Battle Isle, Populous II and others.
  - 100 have no installer, mostly trackloaded action games. 48 of them are multi-disk (35 two-disk, 8 three, 3 four, 2 five or more).
  - Installers are detected by name (install/hd/hard) or by Installer-language content, so a compiled installer with another name would be missed.

## PUAE test results (2026-10-19)
The user tested the seven builds. Gobliins 2 and Moonstone play.
- Cause of the other failures: PUAE's helper boot script (`~/MEGA/Emulation/assets/saves/PUAE/WHDLoad/S/WHDLoad-Startup`) finds the `.slave`, then runs the game's **icon**. WHDLoad takes `Slave=` and the default tool from that icon. Our interpreter skipped the Installer's `tooltype`, so icons kept the package template: a wrong slave name, or no `WHDLoad` default tool.
- Fixed, uncommitted:
  - `f_tooltype` with `edit_icon`/`icon_info` (Amiga DiskObject editing).
  - `version_names`, with version tries ordered by the disk count named in the choice text ("AGA version (5 disks)").
  - The choices parser no longer stops at a `)` inside a string.
  - `DF0:` with no file means the first disk.
  - Tests added; full suite OK (1,633).
- Rebuilt into the test folder: CannonFodder2, TheClue (now the AGA version), MonkeyIsland2, HistoryLine.
  - HistoryLine has two icons, Vorspann (intro) and Programm (game), and no `HistoryLine.info`, so PUAE shows its icon picker.
- Retest (2026-10-19), all run as A1200:
  - Cannon Fodder 2 now plays, so 3 of 7 play: Gobliins 2, Moonstone, Cannon Fodder 2.
  - Monkey Island 2: "illegal instruction".
    - Ruled out: the Kickstart (`kick34005.A500` CRC c4f0f55f, with RTB, is present) and the crack. The slave's second search pattern ("5.2."+$B240) is missing from every copy of `monkey2`, including uncracked retail originals; all are version 5.2.02.
    - Suspect: A1200 caches with KickEmu 1.3. Asked the user to try model A500, and the `MonkeyIsland2-NoCache.lha` build.
  - The Clue! (AGA): "memory exception". No packer signature in `TC.Base`, and no slave source in the package. The PSG crack is unconfirmed. Need the exact WHDLoad requester text.
- Screenshots are in `~/.local/share/Steam/steamapps/common/RetroArch/screenshots/`; read them directly.
- Monkey Island 2 resolved:
  - The NoCache build still failed with `Illegal Instruction at $30`.
  - Ruled out our ADF reader: all 38 files compared byte-exact by both read methods.
  - Cause: the Conet crack modified `monkey2` (CRC 1ae99ab2). The uncracked DE/ES originals and the U.S. Gold `[cr DTC]` copy share `a7201762`.
  - Built `MonkeyIsland2-DTC.lha` from the DTC disk 1 (in `_superseded`) plus the shared disks 2..11.
- **Idea for the feature:** rank a game's copies by whether their main files match an uncracked `[cp ...]` original. TOSEC often has one, and cracks that left the program untouched are the ones WHDLoad slaves accept.
- PUAE starts `<slave name>.info` if it exists. Historyline has only Vorspann/Programm icons, so `HistoryLine.info` was added as a copy of Programm.info. The real feature should do this when no icon matches the slave.
- Waiting on the user's retest. Cannon Fodder CD32's icon looked right; its exact error is unknown.
- These fixes change earlier counts (version tries, `DF0:`). Re-run `fullcheck.py` before quoting numbers.

## Headless PUAE boot tests (2026-10-19)
- RetroArch (Steam) can run headless:
  - `retroarch -c <cfg> -L cores/puae_libretro.so --max-frames=N --max-frames-ss --max-frames-ss-path=X.png game.lha`
  - Use a separate config with null video, audio and input drivers, scratch saves, `system_directory` set to the user's bios folder, and `config_save_on_exit=false`.
  - The binary needs `libCg.so` from SteamLinuxRuntime_sniper, added to `LD_LIBRARY_PATH`.
  - Kept in `~/.cache/whdwork/puae/` (`run.sh <lha> <model> <frames> <png>`; its paths still point at the old scratch folder, so fix them before use).
  - 16 runs take about 1 minute. Combine the screenshots with ffmpeg hstack/vstack (no drawtext filter available).
- Results:
  - Boot: CannonFodder2, CannonFodderCD32, HistoryLine (main menu), Moonstone, TheClue AGA, MonkeyIsland2-DTC (reaches its code-wheel screen).
  - MonkeyIsland2 (Conet copy) fails: Illegal Instruction at $30.
  - The user's The Clue! memory exception doesn't happen headless: probably their PUAE settings.

## 100-game boot test (2026-10-19)
`~/.cache/whdwork/boot100.py`:
- Random 100 games (seed 20261019), CD32 skipped, AGA copies first, then the most original copies.
- Official install script only, plus the icon-alias rule.
- Boots headless on an A1200 (8 MB Fast RAM), then an A500.
- `puae/shot.py` classifies the screenshots (stdlib PNG decoder). Two false positives (The Pawn CLI error, Floor 13 disk error) were caught by eye.
- Results are in `boot2..boot6/results.json`.

**74 of 100 boot**: 32 from uncracked copies, 33 from plain cracks, 9 from fixed/trained/hacked. 64 boot on the first copy tried; 73 as an A1200. Rage and Mentor are doubtful.

26 don't boot:
- 13: WHDLoad "damaged/unsupported version".
- 9: CPU exceptions.
- Marble (hacked copies lack c/bootscr), Floor 13 (disk error).
- G.nius (DIC seek failure: version mismatch).
- Super SkidMarks: **corrected**, not a memory problem. Only 1 of 6 copies was tried: an HD crack the install accepted only as its "CD³² release", a wrong build. The real floppy v2.2 (PSG, 8 disks) needs RawDIC for its track and car disks, so it can't be built. Fixes:
  - `RawDisk`: non-AmigaDOS ADFs no longer stop an install up front (DIC can image them).
  - `Package.install(cd=…)` skips CD versions for floppy sets; the server passes `cd` when the disk names say CD32/CDTV/CD rip.
  - Full suite OK (1,637).
  - The RawDisk change may let more DIC installs build: re-run `fullcheck.py` to measure.

General fixes from this test (uncommitted, tests added):
- `write_lha` writes empty drawers (On the Ball's save drawer).
- DIC accepts `SIZE=$hex` and blanks `SKIPTRACK` tracks.
- Fixed 3D World Soccer and On the Ball.

Note: "[f AGA Fix]" was treated as an AGA version. Only `(AGA)` should count; The Pawn's uncracked copy was tried late because of it.

### After the RawDisk / CD-version fixes (2026-10-19)
- `fullcheck.py` re-run (the old results are in `fullcheck-before.json`). Games whose best copy builds:
  - game files 923 → 968; disk image 201 → 489; fails 1,294 → 957.
  - 326 games went fails → build. The only one that got worse is Super SkidMarks, correctly: its old success was a wrong CD build.
- Boot test of 50 random newly building games (`boot9`): **37 boot**, checked by eye.
- Re-run of the original 100's failures by name (`boot10`, `BOOT_LIST`, AGA = "(AGA)" only, up to 40 copies): 6 more boot (Battle Valley, Cabal, Exile, Manchester United Europe, The Munsters, Rick Dangerous 2).
- **The original 100 now: 80 boot, 19 don't, 1 no build (Super SkidMarks, RawDIC).**
- Gotcha: `boot100.py` draws its random 100 from `fullcheck.json`, so after a re-check use `BOOT_LIST` (titles, numbered from 101), not `BOOT_ONLY`.

## Shipped WHDLoad database + AGA/CD32 boot run (2026-10-19)
- `romorg/whdknown.json.gz` is built by `~/.cache/whdwork/make_whdknown.py` from `fullcheck.json`, `boot*/results.json` and `bootall/results_checked.json`.
  - 17,764 sets: 270 boot, 6,168 install files, 2,961 install images, 417 install but don't boot, 7,948 don't install.
  - `whdready.level()` / `known()`; `_pick_best` ranks by level. Uncommitted when written.
- `bootall.py` covered every AGA and CD32 copy: 697 copies, installs re-downloaded and current, A1200 default, files named without PUAE machine words.
  - AGA: 117 of 571 copies boot (9 doubtful); 38 of 152 games have a booting copy.
  - CD32: 0 of 126. Rips aren't laid out as the CD installs expect ("unsupported version" / "object not found").
  - The eye check found 9 false positives: Blade "insert disk 2", D-Generation Software Failure, Fantastic Dizzy and Wheels on Fire ReadMe shown, Guardian Kickstart insert-disk screen.
- PUAE picks the machine from file-name words (AGA, OCS, ECS, CD32, (A500), (MD) ...), so the app must name `.lha` files without them (and strip a leftover "()").

## State at the end of 2026-10-19 (after the full boot run)
- Committed: the database `romorg/whdknown.json.gz` (17,417 TOSEC sets: 2,454 boot, 3,058 install but don't boot, 4,413 install files/images, 7,492 don't install), strict pairing, the disk-matching ladder, tests.
- All the boot runs are finished. Results and scripts in `~/.cache/whdwork/`: `fullcheck.json` (installs), `bootall`/`bootrest`/`bootfix` (`results.jsonl`, shots, `eye_by_name.json`), `make_whdknown.py` rebuilds the database from them at any time, `pipeline.sh` / `bootfix.py` resume.
- Game level, whole collection (3,836 titles): 1,005 boot (26.2%), 361 build but none boot, 868 install exists but nothing builds, 1,529 no install, 73 demo only. Top 500 English: 194 boot.
- Failure shares (7,635 copies that don't build): 55% RawDIC, 12% xfdDecrunch, 10% asks for a disk none is. Of builds that don't boot: ~63% "unsupported version" (a different release / altered files), 12% DOS error (file missing), 6% CPU exception.
- Disk matching: label ignoring case/punctuation, containment, the disk that has the awaited file, number (Arabic / Roman), save disk = blank, the only disk, then asked-for order (flagged guessed; boots 40% vs 43% for solid).
- Notes: the app refuses to move saves while RetroArch runs, so save tests fail while headless runs are going. `/tmp/romorg-whd-*` dirs leak when a run is killed (clean old ones).
- Not done: the `.lha` writing next to the disks in the real feature, scanner awareness of those, the build UI, the library filter (rank copies by the shipped level), and a boot check inside the app.

## SPS / IPF and the RawDIC problem (2026-10-20)
- Committed 47f5821: the Amiga system has `Commodore Amiga - Games - SPS` (TOSEC's DAT of the SPS releases: 3,018 games, 5,576 IPFs, CRC32/MD5/SHA-1) and `... - [Unofficial IPF]` (801), `.ipf` extension, `Platform.image_set_dats` (rom names become "<game> - <file>", only .ipf kept; 680 SPS file names repeat across games).
- No `capsimg.so` on the Deck (`~/MEGA/Emulation/bios`): PUAE cannot read IPF at all until it is placed there.
- TOSEC [Unofficial IPF] files share no checksum with the Internet Archive IPF collection (which largely equals the SPS set: 5,411 of 6,013 SPS DAT entries match by size+CRC32).
- SPS coverage of the gaps: top-500 English 270 of 306 gaps; 495 of the 610 titles stuck on RawDIC (81%).
- RawDIC exploration (`~/.cache/whdwork/rawdic/`: `RawDIC.i` from github.com/wepl/winstalls/include, `islave.py` static reader, `islaves.json`, `ipf_doc.txt` = the community IPF spec v1.6):
  - 739 imagers in the install packages, all parse. Class A pure data (standard DOS decoders, no callbacks) 12%, B custom track decoders only 54%, C also init/disk callbacks 34%. Median size 280/440/704 bytes, max 50 KB. 438 are single-disk.
  - The RawDIC API is 10 calls (ReadTrack, NextSync, NextMFMword, SaveFile, SaveDiskFile, AppendFile, AppendDiskFile, DMFM_STANDARD, Print, SetSync); the structures are in RawDIC.i.
  - IPF spec: records with CRC32, INFO / IMGE / DATA, block descriptors, data-stream and gap-stream elements, CAPS and SPS encoders, no compression.
- Options: (1) run the real RawDIC in headless PUAE (needs capsimg, the RawDIC binary, disk swapping for multi-disk) ; (2) a Python clone: IPF reader + 68000 interpreter + RawDIC shim, running the real imagers (~2.5k lines, stdlib only); (3) per-game Python imagers (Cannon Fodder style). Recommended: 2, validated first with synthetic MFM encoded from known tracks, then with the user's IPFs and the headless boot test.

## RawDIC built (2026-10-19, evening; not committed yet)
- New modules: `romorg/m68k.py` (68000 interpreter), `romorg/rawdic.py` (RawDIC library and state machine, ported from github.com/wepl/RawDIC `slave.asm` / `RawDIC.asm` / `systeminit.asm`, source in `~/.cache/whdwork/rawdic/src/`), `romorg/ipf.py` (IPF reader). Hooked in `amigainstall._shell` (`RawDIC SLAVE=...`), `amigainstall.floppy` (ADF or IPF; `.tracks`). Tests: `tests/test_rawdic.py` (hand-assembled imagers, IPF writer). ARCHITECTURE AMENDMENT 43.
- CPU check: every SingleStepTests 68000 vector without exceptions passes (227,817; `~/.cache/whdwork/m68ktests/check.py`, vectors in `v1/`). Run it again after any m68k change: `python3 check.py "*.json.bin" 100000`.
- Gotchas found by reading RawDIC's asm: its CRC16 puts each byte into the LOW byte (`eor.b`) - not CRC-CCITT; the process's current-drawer lock is never 0 (imagers skip CurrentDir back when it is); the MFM buffer has 4 sync words in front; the read is $7C04 bytes with no index sync.
- Imagers that read a disk twice (Elite: "insert the same disk twice") get the disk their v2 name numbers, or the only disk.
- Limits: 150M instructions per imaging run, 10M per track decoder call (a decoder lost in a crack's data loops over memory for ever).
- Results on the user's ADFs (`~/.cache/whdwork/rawcheck/`: build.jsonl, boot.jsonl, shots/): of 3,818 copies (586 games) that stopped at RawDIC, 902 copies / 168 games now build; 93 games boot (all new: 1,056 -> 1,149 booting games), e.g. Turrican, R-Type, Rick Dangerous, Elite, Bloodwych, Putty Squad, Street Fighter II, Out Run. Most that build but don't boot show WHDLoad's "data/disk-files are damaged or unsupported version": cracked data. The other 418 games stop on format (checksum / no sync / missing sector): they need the original disks (SPS IPFs).
- `make_whdknown.py` now also reads rawcheck (builds -> level 1, boots -> 3); the shipped list was rebuilt.
- Not proven yet on real IPFs (none on the Deck). The IPF reader is checked against an IPF writer (sync / data elements, gap streams, start bit) and bit-exact AmigaDOS MFM. Next: the user's first SPS files - check them against the SPS DAT, run a RawDIC install from them, boot it.

## SPS library, xfdDecrunch, matching, OpenRetro (2026-10-20)
- SPS set (the user's `commodore-amiga-games-sps/`, 3,014 zips): all 5,571 IPFs match TOSEC's SPS DAT by checksum (276 under an older game name); my IPF reader read every one (CAPS and SPS encoders). `~/.cache/whdwork/spscheck/` (verify.jsonl, build.jsonl, boot.jsonl).
- SPS builds: 2,111 of 2,555 paired releases build (574 through RawDIC on the real IPFs); then with xfdDecrunch 116 of the 133 that needed it.
- `romorg/xfd.py`: the real xfdmaster.library (+ LIBS:xfd externals) in the 68000 interpreter; the XFD package is fetched from Aminet (`whdinstalls.xfd_package`), never shipped (licence: freely distributable, noncommercial, kept whole). Work copy: `~/.cache/whdwork/xfd/pkg/` (also the developer package with includes/autodocs). TOSEC copies that needed it: 613 of 773 build (rerun of the rest after the `run` fix in progress).
- Matcher fixes (articles, umlauts, install main titles, editions, aliases): 53 TOSEC titles newly paired; only 9 of the top-500 multi-disk games really have no install. `~/.cache/whdwork/newpairs/` (559 copies, 376 build).
- OpenRetro (`romorg/openretro.py`, `romorg/amigahw.py`): synced by the updater; joins 7 more title groups; machine per release (A500/A600/A1200/CD32) shown in the WHDLoad card. Raw dump for analysis: `~/.cache/whdwork/oagd/amiga.json`.
- Boot chain (`~/.cache/whdwork/bootchain.sh`, resumable, runs again from where it stopped): SPS boots (gaps first) -> newpairs boots (`bootgen.py RUN_DIR=newpairs`) -> xfd TOSEC boots (`RUN_DIR=xfd`). After it: `make_whdknown.py` should also read spscheck/boot.jsonl, newpairs/boot.jsonl and xfd/boot.jsonl (not wired yet; SPS rows are keyed by zip, not TOSEC disk names - the shipped list needs an IPF section or the SPS names).
- Boot chain finished (2026-10-20 18:40; ran as a temporary user systemd service across power cuts, removed after): SPS 1,567 of 1,745 games boot (679 of 806 gaps); newpairs 45 of 63; xfd TOSEC 73 of 89; fixups 1 of 4. WHDLoad installs with a booting copy: 1,019 -> 1,793 of 2,843. `romorg/whdknown.json.gz` rebuilt from everything (20,330 sets, SPS releases keyed by the names the app gives their IPFs).
- Why SPS games did not boot (2026-10-20 evening): ~80 were booting (the old screen classifier; `puae/shot.py classify3` now knows
  errors by their title text: the WHDLoad requester, the Workbench file picker, the AmigaDOS shell); disk order, the wrong Install
  script, a missing icon: fixed in amigainstall; reruns in `~/.cache/whdwork/rerun/`. SPS now 1,631 of 1,745; WHDLoad installs with
  a booting copy 1,019 -> 1,850. Left: ~26 a different release than the install's, ~20 a file the slave wants is not in the build
  (not explained yet), 6 need WHDLoad 20 (PUAE's own is 19.1: 20.1 dropped into its WHDLoad/C ends at a shell - not solved), ~34
  black screens, a few CPU exceptions on any machine.
- Results files survive a power cut: `~/.cache/whdwork/jsonlsafe.py` (boot stages clean a damaged last line at start).
- Not committed yet (as of writing): xfd, OpenRetro, amigahw, foreach / Join / xadUnFile / run joining, UI column, rebuilt whdknown, docs AMENDMENT 44/45. Full suite (1,681), pyz, AppImage, smoke test pass.

## Night of 2026-10-20: chipset hints, the installs' own programs, answers tried (not committed yet)
- Chipset hints (`amigainstall.chipset_hints`, passed by `server.whdload_check`): native AGA games (no CD32 conversions) with no
  booting AGA copy, rerun (`~/.cache/whdwork/rerun/rerun4.py`, `boot4.jsonl`): 35 of 66 now boot.
- `romorg/amigados.py` (new, `tests/test_amigados.py`): runs the install package's own CLI programs in the 68000 interpreter with a
  small exec / dos.library on the installer's volumes. Civilization (DecrunchCIV), Premier Manager 1-3 (PMFC), Reunion, Crystal
  Dragon, It Came from the Desert, Realms of Arkania, Tower of Souls ... now build.
- `amigainstall`: answer search on failure, Assign, scratch writes, getsum / patmatch, @execute-dir, DEVS:Kickstarts (Subwar 2050
  boots), save disk "No", the package's only slave, Install-<game> / <game>Install scripts. AMENDMENT 46.
- Overnight run, resumable (`~/.cache/whdwork/night/chain.sh`, run it again after a power cut): stage A rebuilds every copy that
  did not build of the games with no booting copy (`buildA.jsonl`), stage B boots up to two newly building copies a game
  (`bootB.jsonl`). `make_whdknown.py` reads both (and `rerun/boot4.jsonl`, `night/eye.jsonl`: Wacky Races and International
  Soccer were booting - the classifier took their title screens for requesters). Helpers: `night/fails.py` (causes of the
  remaining failures), `lib.py`, `show.py <title>` (build), `dbg.py <title>` (trace), `montage.py`.
- Later the same night: Patcher modules run (`amigados.run_patcher`, from Patcher's own source in WHDLoad_dev.lha - fetched to
  `~/.cache/whdwork/whdldev/`; 10 Patcher games now boot: Deflektor, Encounter, Rod-Land, Supremacy, Onslaught, Realms, Thomas
  the Tank Engine 2, Flip-it & Magnose, F-16 Combat Pilot, Lamborghini), packages with no script (`install_scriptless`), damaged
  directory chains skipped (`adf`), xfdDecrunch of a missing file a return code, SetEnv / ENV:, system volumes never a floppy,
  C: tools there, `amigahw` expansion memory. Stages C (A4000 + more memory: nothing new) and D (`bootD.jsonl`).
- Result (by eye checked: Burnout and Total Football show their ReadMe - not booting, corrected in the jsonl with `result_v1`):
  WHDLoad install titles with a booting copy 1,803 -> 1,884 of 2,314; non-CD32 titles with none 450 -> 369. `whdknown.json.gz`
  rebuilt (20,339 sets, 4,879 boot). Full suite 1,697 OK (5 failures once under load from 6 PUAE runs - passed twice since),
  pyz / AppImage / smoke test pass. Nothing committed.
- What is left (see `night/fails_by_cause.json`): mostly copies that are another release than the install's (RawDIC finds no
  sync, "images disk 3, this version has 2", the slave wants files the copy does not have, CPU exceptions); install slaves
  (ConquerorInstall.Slave: WHDLoad's resload would have to be emulated); imagers that drive the hardware (Striker); WHDLoad 20;
  absolute-address crunchers (Twintris: xfdDecrunch asks what header to add); NTSC-only RoboCop 3D (gets past the requester with
  `puae_video_standard = "NTSC"`, not judged); stage B ran ~15 minutes with ENV: resolving to the only floppy (fixed) - a few
  of its builds may have failed falsely.
- 2026-10-21 (user: "yes please" to: the slave choice, matcher misses, the SPS failures, low-colour verdicts):
  - Worms DC never needed more memory: PUAE's helper (`saves/PUAE/WHDLoad/S/Startup-Sequence`) takes `List #?.slav#?` - several
    slaves, any order. `amigainstall.puae_launcher` adds `load` (the helper runs `DH0:load`). Worms, Fuzzball, Dizzy Collection,
    Total Football boot on the A1200 so. The A4000 rule of amigahw removed.
  - Matcher: nickname / possessive (last try, exact only), words in another order: 15 titles paired, 11 boot
    (`night/newpairs2.jsonl`; Oh No! More Lemmings is the data-disk release - wants Lemmings disk 1).
  - Kid Chaos (flags after library calls, SetMode, run's rc), Battle Isle (search from the last question; icons skipped),
    Civilization (version answer only once - it had regressed), Beast III (IPF disk order retry), Hybris (decoder 40M steps; 14 min).
  - The classifier (`puae/shot.py classify3`, work-side only): a two-colour text screen is the game - 26 games were booting
    (`night/eye.jsonl`).
  - Stage E (`night/stageE.py`, `E.log`, resumable): regression rebuild of every newly booting game (159: all build), then every
    failing non-CD32 game's two cleanest copies built and booted (`bootE.jsonl`). Then `make_whdknown.py` (reads newpairs2,
    slaves_test, eye; add bootE), fails.py, top_missing.py for the new top 25.
- CD32 / CDTV (2026-10-21, user's decision: no new platform): AMENDMENT 47 - the TOSEC CD DATs in the Amiga system, a disc decided
  whole, counted with its floppies, CD32 > AGA > CDTV > ECS > OCS across DATs, rule `cd_conversion` (608 files of the user's).
  Stage E: 48 more boot (Burnout, Sorceror's Apprentice only show their ReadMe - corrected); titles with installs (no
  conversions) with a booting copy 1,801 -> 1,948 of 2,251; whdknown rebuilt (no conversions). Full suite 1,707 OK, pyz / AppImage /
  smoke pass. Found, older than this: the Files tree's "missing" (542) is not the totals' (3,100) on the Amiga - the SPS and
  Unofficial IPF DATs count one game per IPF file; not fixed yet.
- 2026-10-21, later (user: "fix the count bug. anything common against the 300ish..."): the count bug was the SPS / Unofficial IPF
  images parsed by their file name - fixed, AMENDMENT 48 (tree = totals: 231 missing, 3,446 owned). The ~300 non-conversion titles
  with an install and no booting copy (`night/remaining303.json`, `rem_causes.json`): 153 do not build (RawDIC 80: no sync 41,
  checksum 24, sector 11, CRC 4 - 23 of them fail on their SPS originals too, which points at our IPF reading / RawDIC; DIC disk
  count 9; waiting for a disk 9; Patcher), 144 build but do not boot (WHDLoad requester 126: ~50 unsupported version, ~36 DOS error
  205 - a file the slave wants is not on the copy, ~15 CPU exceptions, ~12 WHDLoad 20; a Workbench file picker 7; black 7).
  Icons fixed (`repair_slave_tooltypes`, `night/slavefix.py` / `slavefix.jsonl`): Cygnus 8, Indy Temple of Doom, Mr Blobby, Beast III,
  Franko boot; Doman finds its slave now but is an unsupported version. Santa's Xmas Caper ([cr MST]) boots by eye. 297 left.
  whdknown rebuilt (20,237 sets). Next proposed: the 23 SPS originals RawDIC rejects.
- 2026-10-21, evening: protections. `/tmp/claude-1000/prot/` (prot.py, final.py, spscrack.py, ask.py): of the booting installs of
  games OpenRetro marks with a manual / code-wheel check, which ReadMes say the install removes it (264 of 302) - and where the SPS
  original still asks and a crack exists. Stage G (`night/stageG.py`, `bootG.jsonl`; `stageG2.py` 12000-frame runs) built every crack
  of those games: 17 boot (Cruise for a Corpse and Obsession still show the check from the crack). `night/lib.py` job(): a set
  whose other disks are named differently took the uncracked disk 1 - fixed, the 7 affected rows of B / E / newpairs2 rerun
  (`diskfix.jsonl`). Then the user: "prioritise cracked versions when the whdload doesnt remove it" - superseding the SPS original
  (asked, chosen): AMENDMENT 49, rule `prefer_unprotected`, `~/.cache/whdwork/protection.py` -> `protection.json` -> `manual_kept`.
  The user's data dir has no OpenRetro file yet (fetched with the next update): until then the rule does nothing.
- 2026-10-22 (user: "lets fix our problems" - the top 250's games with an install and no booting copy): AMENDMENT 50. Oh No!
  More Lemmings' SPS original boots (its screen is the game's manual check - my by-eye verdict was wrong; corrected in
  newpairs2.jsonl). `night/top250_now.py` (the app's ranking): 239 of 250 have an install, 227 booted. Fixed: matcher (Hall of
  Light first, subtitle rule, Lotus 2, `_NOT_ITS_INSTALL`; 79 titles change - /tmp/claude-1000/prot/match_changes.json), xadUnFile
  skips an unreadable file (Project-X), Delete patterns, absolute crunchers (Twintris), RawDIC 5 return-address image (Lollypop).
  The Chaos Engine: black on PUAE's default A1200 CPU settings, runs with puae_cpu_compatibility "exact" - not changed (the user's).
  Stage I (`night/stageI.py`, `bootI.jsonl`): the changed pairings and the fixed games built and booted; stage J (`stageJ.py`,
  `bootJ.jsonl`): every black-screen game again with "exact" CPU compatibility. Running 6 PUAE workers and the test suite at once
  ran the Deck out of memory (VS Code restarted) - run them one after the other.
- Games with no install at all: 45 of 60 readable native AGA ones are plain AmigaDOS disks with a startup-sequence (night/
  aga_noinstall_kinds.json). PUAE's WHDLoad mode shows a file picker for an archive with no slave; an HD-style install of them
  would need a hardfile or a launcher - not started, for the user to decide.

## Cannon Fodder floppy from cracked ADFs (2026-10-19)
- The RawDIC imager (source: github.com/wepl/winstalls/games/CannonFodder, copied to `~/.cache/whdwork/cannonfodder/cfsrc`) reads Sensible's SOS filesystem.
  - It saves all files except fload/FODDERS/disk1/disk2/disk2.raw/disk3, RNC1-unpacks FODDERC into `fodderc`, and builds `fodder.rel` by diffing FODDERC ($80000 build) against FODDERF ($200000 build).
  - The slave CRC16-checks the first $2000 bytes of fodderc against en1 $b157, en2 $a1c0, de $7b22, fr $c3ce, it $5723.
- The crackers kept SOS on standard tracks. `~/.cache/whdwork/cannonfodder/cfbuild.py` reads it (stdlib RNC1 decoder with CRC check, rel builder). It tries 11 or 12 blocks per track, with or without the side swap, and several directory positions.
- Results:
  - **FLT (all 7 variants) builds en1 and boots to the recruitment hill**: `~/MEGA/Emulation/roms/amiga-whdload-test/CannonFodder-FLT.lha`.
  - **TCB Italian** builds and boots.
  - CSL (the user's kept copy): FODDERC/S/F replaced by one 207 KB cracked self-unpacking program. Impossible.
  - TRSI German: disk 3's block chain breaks.
  - HD Illegal: `fodder` is a relocatable hunk version. Its first $2000 bytes match en1 ($b157), but the layout differs later, so not usable.
- This is per-game reverse engineering. It only works because the cracks kept the game's own file layout. It doesn't scale to RawDIC installs in general.

## Every copy against WHDLoad (2026-10-19)
Script: `~/.cache/whdwork/fullcheck.py`. Results in `fullcheck.json`; archived copies that beat the kept one are in `upgrades.json`.
- Scope: every complete copy (22,956) of every Amiga game, kept and archived. Demo installs are paired only with demo copies; data disks are skipped.
- 2,434 non-demo games have an install:
  - 923 build with game files;
  - 201 only as a whole-disk image;
  - 16 copy nothing;
  - 1,294 fail.
- 44 games: the kept copy fails, but an archived copy builds game files.
  - 37 are in `_superseded`. The `prefer_whdload` rule would pick them, but **the real data dir has no `ready.json`**: the check has only ever run on test copies.
  - 7 are in `_excluded` (non-English, a bad dump, a virus-tagged copy): the user decides.
- 101 games have no kept copy, but an archived one builds. Mostly non-English or betas.

## Bug check of the library build (2026-10-21, not committed yet)
- The user asked for a bug check of the Amiga and other-platform library build, with good test coverage. Results and repros:
  `/tmp/claude-1000/bugcheck/result.json` (44 confirmed); the review of the fixes: the session scratchpad's `wf_result.json`.
- Everything is fixed with a regression test: see Amendment 54 in `docs/ARCHITECTURE.md`. Full suite: 1,819 tests, 51 skipped, OK.
  Packaging and smoke test pass. Tree = totals on a copy of the real data (Amiga, Dreamcast, PlayStation).
- Behaviour the user may notice: with combined titles, an edition's own complete set beats a set with a borrowed disk; Amiga
  "prefer saved" now works; CD32 / CDTV discs follow the language rules; "Complete multi-disk sets only" now on for Dreamcast /
  PlayStation / PlayStation 2 (as Settings always said); an "always exclude" on one disk makes the rest of its set incomplete.

## WHDLoad games not known to boot (2026-10-22, not committed yet)
- User: fix what can be fixed of the ~95 WHDLoad games a build makes that were not seen booting. Amendment 55.
- Work dir `~/.cache/whdwork/fix90/`: `targets.py` (the build's targets from a scan of a data copy in ~/.cache/rtest/data),
  `boot95.py [out.jsonl] [titles]` (built with the app's whdbuild, booted in PUAE; env PUAE_MODEL, PUAE_OPTS), `look.py`, `steps.py`,
  `trace_dos.py` (DOS calls of an install's programs), results `boot95*.jsonl`, shots in `shots/`.
- `make_whdknown.py` now reads fix90's results and re-keys renamed names by SHA-1 - it needs a data dir with the current DATs
  (ROMORG_DATA_DIR, default ~/.cache/rtest/data: copy the real one there first).
- 1,964 of 1,980 known to boot. Left (16) in the amendment. Then (user, Amendment 56): another language when nothing in yours runs,
  and the shipped list by checksum: 2,080 of 2,088 targets known to boot. Left: King's Quest II's UnADF step and 7 cracks.

## The 337 kept games with an install but no WHDLoad build (2026-10-22, IN PROGRESS - pick up here)
- Committed before this: 5546f74. Nothing in the repo changed since. All the work below is in `~/.cache/whdwork/fix90/`.
- User asked: check the 67 "never tried" and the 76 "SPS with no result" (of the 337, from `targets.json` "no_target") and fix what can be.
- Jobs: `jobs143.py` -> `targets143.json` (123 found); `jobs20.py` -> `targets20.json` (14 more via the library's .m3u / SPS number).
  6 not found at all: ABC's Wide World of Sports - Boxing, Goblins 3, Hole-In-One, Inertia Drive, Holiday Lemmings 1993, Mikro Mortal
  Tennis (disks probably in archives).
- Boot runs: `TARGETS=<json> python3 boot95.py <out.jsonl>` (3 workers; setsid nohup for long runs). Results: `boot143.jsonl`,
  `boot20.jsonl`; 63 of those failed only because my cache cleanup deleted the PNG `rerun.py` reads (fixed: boot95.py builds the
  decruncher itself) - rerun of those 64 in `boot_redo.jsonl` was STOPPED at 27/64: rerun the rest
  (`targets_redo.json`, the harness skips titles already in the jsonl).
- Real failures so far (~73): RawDIC on cracks (needs originals: not fixable) and on 5 SPS originals (Borobodur, Carthage, Dragon's
  Lair III, Future Bike, Nippon Safes - our IPF reading / RawDIC); Patcher imagers (Encounter, Fears, Rainbow Warrior); one-offs that may
  be the interpreter: Dungeon Master II split archive "dm2_arcsplit#?" (xadUnFileM), installs copying `libs:` / `EmeticSkimmer:c/joke` /
  `EVIL:REALSAVE` / `Ap2-Install:AP2`, FateFC and the Roger Rabbit imager crash in amigados, "DIC: images disk 0", disk label Desert3,
  a CD version chosen for floppies (CD32_TEST:), Mafdet bad OFS block, Quantox.lha missing from ~/.cache/whdpkgs; 7 wait for a disk the
  user lacks. Plan was: finish the rerun, list all failures, fan the interpreter one-offs out to read-only investigators, apply fixes,
  boot again, rebuild the shipped list (`make_whdknown.py` - add the new jsonl files; needs ~/.cache/rtest/data, a copy of the data dir).
- Disk: /home was full (host-side writer, unknown); the user freed it (360 GB free on 2026-10-22).
- UPDATE (later 2026-10-22, stopped by the user again): the rerun finished, but 63 games still show "FileNotFoundError ...
  spscheck/shots/BMX_Simulator-0-4000.png": the screen classifier (`puae/shot.py classify3`, `_REF`) needs two reference shots my
  cache cleanup deleted - so those games BUILT and BOOTED, only the screen check failed. The two shots are regenerated now
  (`spscheck/shots/BMX_Simulator-0-4000.png` from Epic's WHDLoad requester, `Deathbringer_The_Sword_of_Abaddon-0-4000.png` from an
  archive with no slave: PUAE's file picker) - CHECK them by eye, then rerun just those 63 (delete their rows from boot_redo.jsonl, or
  write a new jsonl) to get their real verdicts. Never delete `*.png` under ~/.cache/whdwork again: `_REF` and `puae/ref_shell.png`
  are inputs.
- Real failures so far (137 games tried, 1 boots: A.G.E.; 6 black screens: Chaos Engine, Operation Stealth, Leading Lap, Space Hulk,
  Star Trek 25th, RoboCop 3D): RawDIC ~30 (no sync on cracks; checksums on SPS originals: Borobodur, Carthage, Dragon's Lair III,
  Future Bike, Nippon Safes, SkidMarks 1/2, Utopia); Patcher 9 (load routine failed; "Disk imaging was not successful"); DIC "images
  disk 0" (Heimdall, Behind the Iron Gate); xadUnFile split archives (Dungeon Master II dm2_arcsplit#?, Pinball BrainDamage
  PinballBD.00#?); copies from other volumes (libs:, EVIL:REALSAVE, EmeticSkimmer:c/joke, Thomas Pinball prefs, Ap2-Install:AP2);
  a CD version chosen for floppies (Rise of the Robots: CD32_TEST:); FateFC runs too long; Roger Rabbit imager calls an unknown
  dos.library vector; xfdDecrunch fails (Xmas Lemmings 91, Caesar); 7 wait for a disk the user lacks; Quantox.lha not cached.

## STATE AT THE END OF 2026-10-23
- Round 1 and round 2 install fixes (Amendment 57) and the user's own check by checksum: committed (see git log).
- The 25 'levels 3, no target' were all built under another region's title (Bad Dudes as Bad Dudes vs. Dragon Ninja ...): not missing;
  the real count of kept games with an install and no build is ~285.
- Build: 2,115 WHDLoad games, 2,106 known to boot; 310 kept games with an install and no build left (`fix90/targets.json` no_target).
- Left to try: the 310's other groups (installs but never boots 77, does not install 92 - mostly cracks; "levels 3" 25 likely title
  mismatches in my count), Perihelion/Conqueror (a WHDLoad install-slave runner), Fears (Patcher cycles), WHDLoad 20 for Oilmania and
  Piracy (user's PUAE setup). `make_whdknown.py` reads all fix90 jsonl files; rebuild it with a fresh ~/.cache/rtest/data copy.

## Work files (outside the repo)
`~/.cache/whdwork/`:
- `drive.py`: an in-process server on `~/.cache/rtest/data`.
- `run.py`: full check plus the top-250 report.
- `recheck.py`: the broad re-check.
- `classify.py`: game files vs disk image vs nothing.
- `hitfiles.py`
- Results: `top250.json`, `recheck.json`, `classify.json`, `ready.json`

Other locations:
- `~/.cache/whdpkgs/`: 2,712 install `.lha` files (146 MB).
- To recreate the test data:
  1. `cp -a ~/.local/share/simple-rom-organiser ~/.cache/rtest/data`
  2. Copy `~/.cache/whdpkgs/*.lha` into `~/.cache/rtest/data/whdinstalls/packages/`
  3. Copy `ready.json` into `~/.cache/rtest/data/whdinstalls/`
  4. Fetch `installs.json` with `whdinstalls.download_and_build()` (one network call, allowed), with `ROMORG_DATA_DIR` set.
- Long runs: use `setsid nohup`, because background jobs die at session end. Don't `pkill -f` a pattern that appears in your own command line.
