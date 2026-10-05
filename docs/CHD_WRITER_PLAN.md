# Plan: a built-in CHD writer (drop chdman altogether)

Status: **plan only, nothing implemented.** Written 2026-10-05 after the reader reached read parity (commit
`a8c5366`, `docs/ARCHITECTURE.md` Amendment 21). This file is meant to be picked up cold by another session: it
states the goal, what is already proven, the work in order, how each step is checked, and what is still unknown.

## 1. Goal

Write CHD files without chdman, so the app no longer needs it for anything.

"Feature parity" and "faster" are defined here so they can be tested:

- **Correct**: for the same input, the CHD's header **SHA-1 and data SHA-1 equal chdman's**. The header SHA-1
  covers the data and the metadata, not the compression, so this is achievable without reproducing chdman's
  compressed bytes. In addition `chdman verify` accepts the file and `chdman extractcd/extractdvd` gives back the
  input, byte for byte.
- **Compatible**: emulators that read chdman's files read ours (same container version 5, same codecs, same
  metadata text).
- **Size**: within 0.5 % of chdman's file for the same codec list.
- **Faster**: less wall time than chdman 0.289 on the same machine with all cores, at that size.

Bit-identical output to chdman is **not** a goal (it would need chdman's exact LZMA / FLAC encoder builds).

## 2. Scope, in tiers

| Tier | What | Why |
|---|---|---|
| 1 | `createcd` from `.cue`+`.bin`, `.gdi`, `.iso`; `createdvd` from `.iso`. Default codecs. | Everything the app does today (`discsys.py` convert, `chdtool.create_cd` / `create_dvd`). Finishing this tier removes the chdman dependency for the app. |
| 2 | `createraw`, `createhd`, `copy` (recompress), `-op` parent output, `-c` codec choice incl. `none`, `-hs` hunk size, `addmeta` / `delmeta` | The rest of what chdman writes for discs and disks. Needs `huff` and `flac` data encoders. |
| 3 | `createld` (AVI in, `avhu` encoder), the exotic CD inputs chdman accepts (`.toc`, `.nrg`, `.cdi`) | Parity in the literal sense; the app has no use for it. **Ask the user before starting this tier.** |

**Decided by the user (2026-10-05): tier 1 is the whole job.** The aim is to remove the dependency, not to
re-implement chdman. Tiers 2 and 3 are listed for completeness only; do not start them unless asked. The one piece
of tier 2 that is in scope is the optional Zstandard preset (`cdzs` for CDs, `zstd` for DVDs), see section 6.

chdman only writes version 5, so old versions are out of scope for writing.

## 3. What is already proven (measured 2026-10-05, Steam Deck, chdman 0.289)

A spike fed CHDs made by the **test** builder (`tests/chdtestlib.py`: `build_chd`, `build_dvd_chd`) to
`chdman verify`:

| Our output | chdman's verdict |
|---|---|
| v5 header, compressed map (flat Huffman, RLE), metadata | accepted |
| `cdlz` / `lzma` from Python's `lzma` (`FORMAT_RAW`, filters of `chd._lzma_filters`) | "Raw SHA1 verification successful" |
| `cdzl` / `zlib` from Python's `zlib` (raw deflate, level 9) | accepted |
| ECC stripping + ECC bitmap (`cdecc`) | accepted |
| `huff` from the test encoder (`chdtestlib.huff_compress`) | accepted |
| uncompressed v5 | accepted ("no verification to be done") |
| header "overall" SHA-1 | **rejected**: the test builder writes a fake one. `Chd.overall_sha1()` in the reader is the correct formula. |
| `cdfl` / `flac` from the test FLAC encoder (`chdtestlib.flac_stream`) | **rejected**: "Decompression error"; the `flac` data variant crashed chdman with heap corruption. Our own libFLAC binding decodes the same frames, so MAME's decoder is stricter (see risk R1). |

Baseline to beat, one PlayStation disc (429 MB, one MODE2 track), `chdman createcd`:

| Run | Wall time | Size |
|---|---|---|
| default (`cdlz,cdzl,cdfl`), 8 threads | 17.2 s (25 MB/s) | 285.4 MB |
| `-np 1` | 68.8 s (6.2 MB/s) | 285.4 MB |
| `-c cdzs,cdzl,cdfl`, 8 threads | 35.4 s | 285.2 MB |
| audio-only CD, 420 MB, default, 8 threads | 16.8 s | 249 MB |

Python's `lzma` on the same hunks: **7.2 MB/s per core**. So LZMA is the cost for both tools, and the two are in
the same class per core. See section 6 for what that means for "faster".

## 4. Design

New modules (stdlib only; C libraries loaded through ctypes, never required - the same rule as the reader):

- `romorg/cdimage.py` - inputs. Parses `.cue` (multi-file, `INDEX 00/01`, `PREGAP`/`POSTGAP`, track modes),
  `.gdi`, `.iso` into a track list plus a frame source that yields 2448-byte frames exactly as chdman lays them
  out: audio byte-swapped to big-endian, cooked sectors at the start of the frame, zeroed subcode, every track
  padded to a multiple of 4 frames, GD-ROM pad frames. It also produces the metadata text (`CHT2` / `CHGD` /
  `DVD `). `discsys.py` already has Redump cue and GDI knowledge to reuse.
- `romorg/chdcomp.py` - one compressor per codec, the inverse of the reader: `cdlz`, `cdzl`, `cdzs`, `cdfl`,
  `lzma`, `zlib`, `zstd`, `flac`, `huff` (and `avhu` in tier 3). Each returns bytes or "not smaller". The `huff`
  encoder moves here from `tests/chdtestlib.py` and needs MAME's exact tree limits (16 bits).
- `romorg/flacenc.py` - FLAC frames through libFLAC's stream encoder (ctypes), configured like MAME's
  `flac_encoder` (fixed block size from the hunk size, 16 bit, 44.1 kHz, 2 channels, frames only, no stream
  header). Fallback when no libFLAC loads: do not offer `cdfl`; audio then goes to `cdlz` / `cdzl` (valid, larger).
- `romorg/chdwrite.py` - the container: hunk de-duplication (CRC-16 + SHA-1 → `SELF` entries), optional parent
  matching (`PARENT` entries, tier 2), compressed map (Huffman + RLE, map CRC-16), metadata chain with checksum
  flags, header with data SHA-1 and overall SHA-1. Writes to a temporary name and renames at the end.
- Parallelism: worker **processes**, reusing `chdsched` / `chdworker` (protocol gets a "compress frames
  [first, count) of this source" request; the reply is the compressed hunks with their CRC-16 and SHA-1). The parent
  process reads input, de-duplicates, orders results and writes. Bounded queue so memory stays flat.
- Integration: `chd_writer = "chdman" | "builtin" | "auto"` next to the existing `chd_engine`. `discsys.convert`
  keeps its flow (write `.romorg.part`, verify with the reader against the source hashes, then replace). **chdman
  stays the default until the gate in phase 4 is passed on the user's real discs**, and stays available as a
  fallback afterwards unless the user says to remove it.

## 5. Phases, each with its check

Every phase ends with the full test suite green and, where a chdman is found, a comparison against it. Add a
`tools/bench_chdwrite.py` early (like `tools/bench_chd.py`): same input through chdman and through the writer; prints
time, size, SHA-1 equality, `chdman verify` result.

**Phase 0 - references (half a day).**
Pick three real inputs and record chdman's time, size and SHA-1 for each: a PlayStation disc with audio tracks, a
Dreamcast GD-ROM set, a PS2 DVD ISO. Small generated inputs go into `tests/fixtures/chd` with chdman's SHA-1s, so
the suite checks parity without chdman installed.

**Phase 1 - container and `createdvd`.**
ISO in, v5 out with `lzma` / `zlib` / `none`, single process. Check: header SHA-1 = chdman's for the same ISO,
`chdman verify` passes, the reader round-trips. This is the smallest end-to-end slice and settles the map and SHA-1
code.

**Phase 2 - `createcd` for data.**
`cdimage.py` for `.iso`, single-track and multi-track `.cue`, `.gdi`; `cdlz` / `cdzl` with ECC stripping. Check:
header SHA-1 = chdman's on the Redump layouts the app meets (PS1 single track, PS1 data + audio with audio stored as
`cdzl`, PS2 CD, Dreamcast GDI), `chdman extractcd` returns the input files.
The SHA-1 equality is the oracle for all the fiddly layout rules (pregaps, padding, metadata text): if one number is
off, it fails.

**Phase 3 - FLAC audio (`cdfl`).** See risk R1 first; start this phase with a spike.
Check: `chdman verify` accepts audio hunks; size of an audio-heavy disc within 0.5 % of chdman's.

**Phase 4 - parallel and fast; the gate.**
Workers, de-duplication before compression, the trial-skipping of section 6. Check on the three references:
time < chdman, size within 0.5 %, SHA-1 equal, memory bounded. **Gate: report the table to the user. Only when it
passes does `chd_writer` default to the built-in writer.**

**Phase 5 - wire into the app.**
`discsys.convert` uses the writer; progress, cancel, free-space check, crash-safe temp files, undo as today; UI
texts ("needs chdman" disappears from Convert); self-check writes and re-reads a tiny CHD; packaging docs; Windows
entry point for the worker.

**Phase 5b - stop bundling chdman.** Once the gate has passed: the AppImage and the Windows zip no longer ship
chdman (`packaging/build_appimage.sh`, `romorg/bundle.py`, the third-party notices, the self-check's bundle lines).
An installed chdman (PATH, the MAME Flatpak, the folders `chdtool` already searches) is still detected and stays
selectable: `chd_engine = "chdman"` for reading, `chd_writer = "chdman"` for writing.

**Phase 5c - the Zstandard preset.** A "fast (Zstandard)" choice in the Convert step, off by default, with the
compatibility note of section 6 next to it. Needs libzstd for compressing (present on SteamOS; hide the choice
where no library loads).

*Not planned (kept for reference):*

**Phase 6 - tier 2.** `createraw`, `createhd` (`GDDD` metadata, CHS guess like chdman), `copy`, parents, codec and
hunk-size options, `zstd` / `cdzs` through libzstd, `huff` and `flac` data encoders, `addmeta` / `delmeta`. Each
checked against the matching chdman command.

**Phase 7 - tier 3, only if the user wants it.** AVI reader, `avhu` encoder, `createld`; `.toc` / `.nrg` inputs.

**Then**: the user decides whether chdman detection, bundling and the fallback are removed.

## 6. Where "faster" can come from (and where it cannot)

Per core we will not out-compress chdman: both spend their time in LZMA at the same speed. chdman already uses
every core. The gains have to come from doing less:

1. **No wasted compression of repeated hunks.** De-duplicate (CRC-16 + SHA-1) *before* compressing. Real discs have
   many repeats: the user's PS2 discs have up to 20 % `SELF` hunks (Bully: 58,029 of 283,010).
2. **No hopeless trials.** chdman compresses every hunk with every codec in the list and keeps the smallest. Skip
   FLAC on data tracks and LZMA on audio tracks; try `zlib` only where LZMA barely gained. Each skip must be
   measured for size: the budget is 0.5 %.
3. **Early "stored" decision** for hunks that do not compress (encrypted / already compressed data).
4. **Cheap ECC check**: batch it (`cdecc` already works on many sectors at once).
5. **Optional fast preset** (not the default; the user wants it offered): `cdzs` / `zstd` at a moderate libzstd
   level. chdman's own `cdzs` run was *slower* than its default (35 s vs 17 s) because it uses a very high level.
   Zstandard CHDs exist since MAME 0.262 (February 2024) and chdman itself does not use them by default "to ensure
   maximum compatibility". Readers need a libchdr from 2024 or later: current DuckStation, PCSX2, Flycast and
   redream have it; older builds and some libretro cores do not (SwanStation had an open request for it). The UI
   text must say: "smaller wait, also loads faster in the emulator, but needs an emulator from 2024 or later -
   check yours before converting a whole collection". Verify the emulator list again when writing that text.

Honest expectation: a modest win at equal size (the hypothesis is 1.2x to 1.5x on typical discs, more on discs with
many repeats), a large win with the fast preset. If phase 4 shows no win at equal size, say so and let the user
choose between "same speed, no dependency" and stopping.

## 7. Risks and open questions

- **R1 - FLAC frames MAME accepts.** The test encoder's frames are rejected by chdman although libFLAC decodes
  them. MAME's decoder is fed a synthetic stream header with a block size derived from the hunk size, and reads
  frame by frame into fixed buffers, so block size (and probably the exact frame layout) must match what MAME's
  encoder would write. Spike: encode with libFLAC using MAME's settings (see `flac.cpp` in MAME: block size =
  `hunk bytes / 4`, halved until at most 2048), run `chdman verify`. If libFLAC cannot be made to satisfy it, `cdfl`
  is left out and audio is stored with `cdlz` - measure the size cost before accepting that.
- **R2 - FLAC encoder availability.** libFLAC is on SteamOS and in the AppImage. The Windows build bundles only
  libsndfile; whether its FLAC output can be cut into acceptable frames is unknown.
- **R3 - LZMA settings.** Python's `lzma` output is accepted (proven), but the ratio must match chdman's level:
  compare sizes in phase 1 and tune `nice_len` / `mf` / `depth` in the filter chain if we are larger.
- **R4 - layout rules of `createcd`.** Pregap / postgap handling, `PGTYPE`, multi-file cues and GD-ROM areas are
  easy to get subtly wrong. Mitigation: the SHA-1 oracle on every layout, and refusing inputs the parser does not
  fully understand (fall back to chdman while it exists).
- **R5 - memory and ordering** with many workers on a machine that often has only 2 GiB free (the Deck). Bounded
  in-flight hunks; reuse `chdsched`'s memory-aware worker count.
- **R6 - data safety.** A wrong CHD that replaces the user's only copy is the worst outcome. Keep: write to a part
  file, verify every track hash against the source with the reader, replace only then, never delete sources unless
  the user chose that. During rollout also run `chdman verify` on the result when a chdman is present.
- **R7 - Windows** has never been run from this machine; the writer needs a pass there before it becomes the default
  on Windows.

## 8. How to resume

- Read `docs/ARCHITECTURE.md` Amendments 14, 20, 21 (reader, scheduler, codecs) and `romorg/chd.py`,
  `romorg/chdhuff.py`, `romorg/chdsched.py`, `romorg/chdworker.py`, `tests/chdtestlib.py` (it already contains a
  working v5 writer for tests: map, metadata, `cdlz` / `cdzl` / `cdzs` / `huff` hunks).
- A chdman to compare with: the AppImage bundles one (`tools/chdman` + `tools/lib`, see `romorg/bundle.py`), or the
  Arch `mame-tools` package unpacked anywhere and run with `LD_LIBRARY_PATH`. On the Steam Deck the editor runs in a
  Flatpak sandbox: host programs are started with `flatpak-spawn --host`.
- Real chdman-made fixtures and the script that generated them: `tests/fixtures/chd/`.
- Tests: `python3 -m unittest discover -s tests` (958 tests at the time of writing).
- Start with phase 0 and phase 1; do not change the convert default before the phase 4 gate.

## 9. Decisions (answered by the user, 2026-10-05)

1. Tier 3, and tier 2 beyond the Zstandard preset: **not wanted.** Removing the dependency is the goal.
2. chdman after the gate: **not packaged any more; still usable as an option when the user has MAME / chdman
   installed.**
3. Fast Zstandard preset: **yes, offer it** (not as the default).
